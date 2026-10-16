"""Behavioral checks for the dependency-sources draft implementation."""

from __future__ import annotations

import hashlib
import io
import logging
import shutil
import subprocess
import tarfile
import zipfile
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import pytest
import respx

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.transport import HttpResponse
from nab_project import _sources as source_materializer
from nab_project import toml_io
from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.develop import expand_credentials, read_develop_sources, select_sources
from nab_project.fetch import FetchCoordinator
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import ArchivePin, LocalPin, build_pylock
from nab_project.resolve import ResolveResult, build_lock_input, resolve_for_targets
from nab_provider._vendor.packaging.version import Version
from nab_provider.archive_admission import (
    ArchiveConfig,
    ArchivePolicy,
    UnsupportedArchiveError,
)
from nab_provider.errors import (
    ConfigError,
    SdistHashMismatchError,
    SourceNameMismatchError,
    UnsupportedSdistError,
    UnsupportedWheelError,
)
from nab_provider.metadata import WheelMetadata
from nab_provider.overrides import IndexOverride
from nab_provider.pep508 import parse_requirement
from nab_provider.policy import (
    ArchiveSource,
    BuildPolicy,
    DistPolicy,
    LocalSource,
    SourceRequest,
)
from nab_provider.records import IndexConfig
from nab_provider.target import ResolveTarget
from nab_provider.testing import pkg_override
from nab_provider.vcs_admission import UnsupportedVcsError, VcsConfig, VcsPolicy


class NoNetwork:
    """Fail any network request made by an entirely local resolution."""

    def __init__(self) -> None:
        self.closed = False

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        msg = f"unexpected network request: {url}"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        self.closed = True


def archive_outcome() -> AbstractContextManager[object]:
    """Check archive resolution or the platform's documented extraction refusal."""
    if hasattr(tarfile, "data_filter"):
        return nullcontext()
    return pytest.raises(UnsupportedSdistError, match="tar data filter")


def write(path: Path, body: str) -> Path:
    """Write a pyproject fixture, creating its project directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def project(
    path: Path,
    name: str = "root",
    version: str = "1",
    dependencies: str = "[]",
    tail: str = "",
) -> Path:
    """Create a static project with optional development declarations."""
    return write(
        path / "pyproject.toml",
        f'[project]\nname="{name}"\nversion="{version}"\ndependencies={dependencies}\n{tail}',
    )


def resolve(
    path: Path, *, inputs: ResolveInputs | None = None, **kwargs: object
) -> ResolveResult:
    """Run the production project resolver with a transport that refuses network."""
    if inputs is None:
        inputs = ResolveInputs(
            develop_sources=read_develop_sources(path), build_policy=BuildPolicy.NEVER
        )
    transport = NoNetwork()
    result = resolve_for_targets(
        path, transport, targets=[ResolveTarget.for_host()], inputs=inputs, **kwargs
    )
    assert transport.closed
    return result


def archive_inputs(path: Path, *, require_hashes: bool = True) -> ResolveInputs:
    """Apply the hash requirement independently of source selection."""
    return ResolveInputs(
        develop_sources=read_develop_sources(path),
        build_policy=BuildPolicy.NEVER,
        archive=ArchiveConfig(require_hashes=require_hashes),
    )


def test_paths_transitives_extras_and_dependency_tables(tmp_path: Path) -> None:
    project(
        tmp_path / "b",
        "b",
        dependencies='["c"]',
        tail='[develop.sources]\nc={path="../wrong"}\n',
    )
    project(
        tmp_path / "c", "c", tail='[project.optional-dependencies]\nfeature=["d"]\n'
    )
    project(tmp_path / "d", "d")
    path = project(
        tmp_path,
        dependencies='["b", "c[feature]"]',
        tail='[develop.sources]\nb="b"\nc={path="c",editable=true}\nd="d"\n',
    )
    result = resolve(path)
    result.raise_for_failure()
    lock = result.target_results[0].lock
    assert lock is not None
    assert set(lock.pins) == {"b", "c", "d"}
    assert isinstance(lock.pins["c"], LocalPin)
    assert lock.pins["c"].editable
    assert not lock.pins["b"].editable
    assert lock.dependencies["b"] == ("c",)


def test_transitive_direct_reference_is_replaced(tmp_path: Path) -> None:
    project(
        tmp_path / "b",
        "b",
        dependencies='["c[feature] @ https://invalid.example/c.whl"]',
    )
    project(
        tmp_path / "c", "c", tail='[project.optional-dependencies]\nfeature=["d"]\n'
    )
    project(tmp_path / "d", "d")
    path = project(
        tmp_path, dependencies='["b"]', tail='[develop.sources]\nb="b"\nc="c"\nd="d"\n'
    )
    result = resolve(path)
    result.raise_for_failure()
    assert set(result.target_results[0].pins) == {"b", "c", "d"}


def test_unmatched_source_warns_once_across_targets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project(tmp_path / "core", "core", dependencies='["leaf"]')
    project(tmp_path / "leaf", "leaf")
    path = project(
        tmp_path,
        dependencies='["core"]',
        tail='[develop.sources]\ncore="core"\nleaf="leaf"\ncor="core"\n',
    )
    with caplog.at_level(logging.WARNING):
        result = resolve_for_targets(
            path,
            NoNetwork(),
            targets=[
                ResolveTarget.for_host_python("3.12"),
                ResolveTarget.for_host_python("3.13"),
            ],
            inputs=ResolveInputs(develop_sources=read_develop_sources(path)),
        )
    result.raise_for_failure()
    assert [set(target.pins) for target in result.target_results] == [
        {"core", "leaf"},
        {"core", "leaf"},
    ]
    assert len(caplog.records) == 1
    assert "source for cor matches no resolved dependency" in caplog.text


def test_inactive_sources_do_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project(tmp_path / "core", "core")
    path = project(
        tmp_path,
        dependencies='["core"]',
        tail="""[develop.sources]
core="core"
older={path="missing",marker="python_version < '1'"}
optional={path="missing",extra="feature"}
grouped={path="missing",group="dev"}
""",
    )
    with caplog.at_level(logging.WARNING):
        resolve(path).raise_for_failure()
    assert not caplog.records


@pytest.mark.parametrize("selector", ["extra", "group"])
def test_unmatched_source_in_active_selection_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, selector: str
) -> None:
    project(tmp_path / "core", "core")
    path = project(
        tmp_path,
        tail=f"""[project.optional-dependencies]
feature=["core"]
[dependency-groups]
feature=["core"]
[develop.sources]
core="core"
cor={{path="core",{selector}="feature"}}
""",
    )
    selection = {"extras" if selector == "extra" else "groups": ("feature",)}
    with caplog.at_level(logging.WARNING):
        resolve(path, **selection).raise_for_failure()
    assert len(caplog.records) == 1
    assert "source for cor matches no resolved dependency" in caplog.text


def test_failed_resolution_does_not_report_unused_sources(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project(tmp_path / "core", "core")
    path = project(
        tmp_path,
        dependencies='["core>=2"]',
        tail='[develop.sources]\ncore="core"\n',
    )
    with caplog.at_level(logging.WARNING):
        result = resolve(path)
    assert not result.success
    assert not caplog.records


def test_original_marker_and_source_version_still_checked(tmp_path: Path) -> None:
    project(tmp_path / "b", "b")
    path = project(tmp_path, dependencies='["b>=2"]', tail='[develop.sources]\nb="b"\n')
    assert not resolve(path).success
    path = project(
        tmp_path,
        dependencies="[\"b; python_version < '1'\"]",
        tail='[develop.sources]\nb="missing"\n',
    )
    result = resolve(path)
    assert result.success
    assert result.target_results[0].pins == {}


def test_scope_merge_paths_and_no_ancestor_search(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(
        root / "pyproject.toml",
        '[develop.sources]\nFoo_Bar="root-foo"\nother="root-other"\n',
    )
    write(root / "middle" / "pyproject.toml", '[develop.sources]\nignored="bad"\n')
    target = project(
        root / "middle" / "target", tail='[develop.sources]\nfoo-bar="target-foo"\n'
    )
    entries = read_develop_sources(target, invocation_root=root)
    assert {entry.name: Path(entry.location) for entry in entries} == {
        "foo-bar": target.parent / "target-foo",
        "other": root / "root-other",
    }
    assert [
        entry.name
        for entry in read_develop_sources(target, invocation_root=target.parent)
    ] == ["foo-bar"]
    assert [
        entry.name
        for entry in read_develop_sources(
            target, invocation_root=tmp_path / "elsewhere"
        )
    ] == ["foo-bar"]


def test_group_only_root(tmp_path: Path) -> None:
    project(tmp_path / "b", "b")
    path = write(
        tmp_path / "pyproject.toml",
        '[dependency-groups]\ndev=["b"]\n[develop.sources]\nb="b"\n',
    )
    result = resolve(path, groups=("dev",))
    result.raise_for_failure()
    assert set(result.target_results[0].pins) == {"b"}


def test_native_overrides_by_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project(tmp_path / "b", "b")
    project(tmp_path / "c", "c")
    path = project(
        tmp_path,
        dependencies='["b", "c"]',
        tail='[develop.sources]\nb="wrong"\nc="c"\n',
    )
    inputs = ResolveInputs(
        local_sources=(LocalSource("b", str(tmp_path / "b")),),
        develop_sources=read_develop_sources(path),
    )
    with caplog.at_level(logging.WARNING):
        result = resolve(path, inputs=inputs)
    result.raise_for_failure()
    assert set(result.target_results[0].pins) == {"b", "c"}


def test_ordered_marker_sources_do_not_share_materializations(tmp_path: Path) -> None:
    project(tmp_path / "old", "b", "1")
    project(tmp_path / "new", "b", "2")
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb=[{path="old", marker="python_version < \'3.13\'"}, {path="new"}]\n',
    )
    inputs = ResolveInputs(develop_sources=read_develop_sources(path))
    transport = NoNetwork()
    result = resolve_for_targets(
        path,
        transport,
        targets=[
            ResolveTarget.for_host_python("3.12"),
            ResolveTarget.for_host_python("3.13"),
        ],
        inputs=inputs,
    )
    result.raise_for_failure()
    assert [str(tr.pins["b"]) for tr in result.target_results] == ["1", "2"]


def test_extra_source_conflicts(tmp_path: Path) -> None:
    project(tmp_path / "one", "b")
    project(tmp_path / "two", "b")
    path = project(
        tmp_path,
        tail='[project.optional-dependencies]\none=["b"]\ntwo=["b"]\n[develop.sources]\nb=[{path="one",extra="one"},{path="two",extra="two"}]\n',
    )
    assert resolve(path, extras=("one",)).success
    with pytest.raises(ConfigError, match="conflicting sources"):
        resolve(path, extras=("one", "two"))


def test_transitive_occurrence_conflicts_with_extra_source(tmp_path: Path) -> None:
    project(tmp_path / "a", "a", dependencies='["b"]')
    project(tmp_path / "one", "b")
    project(tmp_path / "two", "b")
    path = project(
        tmp_path,
        dependencies='["a"]',
        tail='[project.optional-dependencies]\none=["b"]\n[develop.sources]\na="a"\nb=[{path="one",extra="one"},{path="two"}]\n',
    )
    with pytest.raises(ConfigError, match="transitive occurrence"):
        resolve(path, extras=("one",))


@pytest.mark.parametrize(
    "source",
    [
        'b=""',
        "b=[]",
        "b=3",
        "b={}",
        'b={path="a",url="https://x"}',
        'b={path="a",unknown=true}',
        'b={path="a",editable="yes"}',
        'b={path="a",extra="x",group="y"}',
        'b={git="git+https://x"}',
        'b={git="https://x",tag="x",rev="y"}',
        'b={path="a",subdirectory="../bad"}',
        'b={path="a",hashes=[]}',
        'b={path="a",hashes={sha256="xyz"}}',
        'b={path="a",marker="bad"}',
        'b={path="a",marker=3}',
        'b={url="https://[broken/core.zip"}',
        'b={url="https://example.org/core bad.zip"}',
        '"bad name"="a"',
        'Foo_Bar="a"\nfoo-bar="b"',
    ],
)
def test_reject_invalid_sources(tmp_path: Path, source: str) -> None:
    path = write(tmp_path / "pyproject.toml", "[develop.sources]\n" + source + "\n")
    with pytest.raises(ConfigError):
        read_develop_sources(path)


def test_credentials_expand_only_in_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_USER", "name")
    assert (
        expand_credentials("https://${SOURCE_USER}@example.com/${SOURCE_USER}")
        == "https://name@example.com/${SOURCE_USER}"
    )
    assert (
        expand_credentials("https://example.com/${SOURCE_USER}")
        == "https://example.com/${SOURCE_USER}"
    )
    monkeypatch.delenv("SOURCE_USER")
    with pytest.raises(ConfigError, match="not set"):
        expand_credentials("https://${SOURCE_USER}@example.com")


def wheel(
    directory: Path,
    name: str,
    version: str,
    dependencies: tuple[str, ...] = (),
    *,
    extras: tuple[str, ...] = (),
    requires_python: str | None = None,
) -> None:
    """Create a minimal wheel that the local Simple API client can read."""
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {req}\n" for req in dependencies)
    metadata += "".join(f"Provides-Extra: {extra}\n" for extra in extras)
    if requires_python is not None:
        metadata += f"Requires-Python: {requires_python}\n"
    with zipfile.ZipFile(directory / filename, "w") as archive:
        archive.writestr(f"{info}/METADATA", metadata)
        archive.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")


def test_strict_index_routing_and_unrelated_dependencies(tmp_path: Path) -> None:
    primary, private = tmp_path / "primary", tmp_path / "private"
    wheel(primary, "b", "9")
    wheel(primary, "c", "1")
    wheel(private, "b", "1", ("c",))
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{index="{private.as_uri()}"}}\n',
    )
    inputs = ResolveInputs(
        indexes=(
            IndexConfig("primary", primary.as_uri()),
            IndexConfig("private", private.as_uri() + "/"),
        ),
        develop_sources=read_develop_sources(path),
    )
    result = resolve(path, inputs=inputs)
    result.raise_for_failure()
    assert {
        name: str(version) for name, version in result.target_results[0].pins.items()
    } == {"b": "1", "c": "1"}

    (private / "b-1-py3-none-any.whl").unlink()
    assert not resolve(path, inputs=inputs).success


def test_two_indexes_with_same_version_keep_separate_metadata(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    wheel(first, "b", "1", ("c",))
    wheel(first, "c", "1")
    wheel(second, "b", "1", ("d",))
    wheel(second, "d", "1")
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb=[{{index="{first.as_uri()}",marker="python_version < \'3.13\'"}},{{index="{second.as_uri()}"}}]\n',
    )
    inputs = ResolveInputs(
        indexes=(
            IndexConfig("first", first.as_uri()),
            IndexConfig("second", second.as_uri()),
        ),
        develop_sources=read_develop_sources(path),
    )
    result = resolve_for_targets(
        path,
        NoNetwork(),
        targets=[
            ResolveTarget.for_host_python("3.12"),
            ResolveTarget.for_host_python("3.13"),
        ],
        inputs=inputs,
    )
    result.raise_for_failure()
    assert [set(tr.pins) for tr in result.target_results] == [{"b", "c"}, {"b", "d"}]
    lock = build_pylock(build_lock_input(result, inputs=inputs), lock_dir=tmp_path)
    assert {str(package.index) for package in lock.packages if package.name == "b"} == {
        first.as_uri(),
        second.as_uri(),
    }


def source_archive(path: Path, version: str = "1") -> bytes:
    """Write a source archive with static metadata and return its exact bytes."""
    content = f'[project]\nname="b"\nversion="{version}"\n'.encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("b/pyproject.toml")
        info.size = len(content)
        info.mtime = 0
        archive.addfile(info, io.BytesIO(content))
    return path.read_bytes()


def test_optional_input_hashes_still_produce_hashed_locks(tmp_path: Path) -> None:
    archive = tmp_path / "b.tar.gz"
    data = source_archive(archive)
    path = project(
        tmp_path, dependencies='["b"]', tail='[develop.sources]\nb={path="b.tar.gz"}\n'
    )
    with archive_outcome():
        result = resolve(path, inputs=archive_inputs(path, require_hashes=False))
        result.raise_for_failure()
        pin = result.target_results[0].lock.pins["b"]
        assert isinstance(pin, ArchivePin)
        assert pin.hashes == (("sha256", hashlib.sha256(data).hexdigest()),)

        # A second unpinned input may have new bytes and therefore a new output hash.
        data = source_archive(archive, "2")
        result = resolve(path, inputs=archive_inputs(path, require_hashes=False))
        result.raise_for_failure()
        assert result.target_results[0].lock.pins["b"].hashes == (
            ("sha256", hashlib.sha256(data).hexdigest()),
        )


def test_all_declared_supported_archive_hashes_are_verified(tmp_path: Path) -> None:
    archive = tmp_path / "b.tar.gz"
    data = source_archive(archive)
    correct = hashlib.sha256(data).hexdigest()
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{url="{archive.as_uri()}",hashes={{sha256="{correct}",sha512="00"}}}}\n',
    )
    with pytest.raises(SdistHashMismatchError):
        resolve(path, inputs=archive_inputs(path))


def test_source_markers_split_minor_targets(tmp_path: Path) -> None:
    project(tmp_path / "one", "b", "1")
    project(tmp_path / "two", "b", "2")
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb=[{path="one",marker="python_full_version < \'3.12.5\'"},{path="two"}]\n',
    )
    result = resolve_for_targets(
        path,
        NoNetwork(),
        targets=[ResolveTarget.for_host_python("3.12")],
        inputs=ResolveInputs(develop_sources=read_develop_sources(path)),
    )
    result.raise_for_failure()
    assert {str(tr.pins["b"]) for tr in result.target_results} == {"1", "2"}


def test_build_requirements_ignore_standard_sources(tmp_path: Path) -> None:
    primary = tmp_path / "index"
    wheel(primary, "b", "1")
    project(tmp_path / "local", "b", "2")
    path = project(
        tmp_path,
        tail='[build-system]\nrequires=["b"]\n[develop.sources]\nb={path="local"}\n',
    )
    result = resolve(
        path,
        build_requirements=True,
        inputs=ResolveInputs(
            indexes=(IndexConfig("primary", primary.as_uri()),),
            develop_sources=read_develop_sources(path),
        ),
    )
    result.raise_for_failure()
    assert str(result.target_results[0].pins["b"]) == "1"


def test_group_selectors_do_not_apply_to_other_occurrences(tmp_path: Path) -> None:
    project(tmp_path / "one", "b", "1")
    project(tmp_path / "two", "b", "2")
    path = project(
        tmp_path,
        tail='[dependency-groups]\none=["b"]\ntwo=["b"]\n[develop.sources]\nb=[{path="one",group="one"},{path="two",group="two"}]\n',
    )
    assert str(resolve(path, groups=("one",)).target_results[0].pins["b"]) == "1"
    assert str(resolve(path, groups=("two",)).target_results[0].pins["b"]) == "2"


@pytest.mark.parametrize(
    "body", ["develop=3", "[develop]\nunknown=3", "[develop]\nsources=3"]
)
def test_reject_invalid_develop_table(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        read_develop_sources(write(tmp_path / "pyproject.toml", body))


@pytest.mark.parametrize(
    "source",
    [
        'b={path="b.whl",subdirectory="x"}',
        'b={path="b.whl",editable=true}',
        'b={url="https://x/b.whl",subdirectory="x"}',
        'b={path="dir",hashes={sha256="00"}}',
        'b={path="b.tar.gz",editable=true}',
    ],
)
def test_invalid_file_source_options(tmp_path: Path, source: str) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "b.tar.gz").touch()
    with pytest.raises(ConfigError):
        read_develop_sources(
            write(tmp_path / "pyproject.toml", "[develop.sources]\n" + source)
        )


def test_source_subdirectory_is_decoded_for_archives(tmp_path: Path) -> None:
    archive = tmp_path / "b.tar.gz"
    content = b'[project]\nname="b"\nversion="1"\n'
    with tarfile.open(archive, "w:gz") as stream:
        info = tarfile.TarInfo("b/sub dir/pyproject.toml")
        info.size = len(content)
        stream.addfile(info, io.BytesIO(content))
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={path="b.tar.gz",subdirectory="sub dir"}\n',
    )
    with archive_outcome():
        result = resolve(path, inputs=archive_inputs(path, require_hashes=False))
        result.raise_for_failure()
        assert str(result.target_results[0].pins["b"]) == "1"


@pytest.mark.parametrize("kind", ["hg", "bzr", "svn"])
def test_unsupported_vcs_is_reported(tmp_path: Path, kind: str) -> None:
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{{kind}="https://example.invalid/b"}}\n',
    )
    with pytest.raises(NotImplementedError, match="not implemented"):
        resolve(path)


def test_invalid_wheel_filename_and_hashes_are_reported(tmp_path: Path) -> None:
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={url="https://example.invalid/b.whl"}\n',
    )
    with pytest.raises(UnsupportedWheelError, match="Invalid wheel filename"):
        resolve(path, inputs=archive_inputs(path, require_hashes=False))
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={url="https://example.invalid/b.tar.gz",hashes={unknown="00"}}\n',
    )
    with pytest.raises(ConfigError, match="no supported hash"):
        resolve(path, inputs=archive_inputs(path, require_hashes=False))


def test_unreachable_alternatives_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = project(tmp_path, tail='[develop.sources]\nb=[{path="one"},{path="two"}]\n')
    with caplog.at_level(logging.WARNING):
        entries = read_develop_sources(path)
    assert len(entries) == 2
    assert len(caplog.records) == 1


def test_target_urls_keep_their_explicit_origin(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = project(
        tmp_path,
        dependencies='["b @ https://example.invalid/b.whl", "c @ https://example.invalid/c.whl"]',
        tail='[develop.sources]\nb="local"\n',
    )
    with caplog.at_level(logging.WARNING):
        selected = select_sources(
            read_develop_sources(path),
            ResolveTarget.for_host(),
            [
                parse_requirement("b @ https://example.invalid/b.whl"),
                parse_requirement("c @ https://example.invalid/c.whl"),
            ],
            None,
        )
    assert selected.entries == {}
    assert selected.protected == {"b", "c"}
    assert len(caplog.records) == 1


def test_scoping_stopped_coordinator_is_refused() -> None:
    coordinator = FetchCoordinator(NoNetwork())
    with pytest.raises(RuntimeError, match="stopped coordinator"):
        coordinator.scoped_sources(ResolveInputs(), ResolveInputs(), cache_dir=None)


def test_conflicting_generated_index_name_is_refused(tmp_path: Path) -> None:
    url = (tmp_path / "index").as_uri()
    label = "develop-" + hashlib.sha256(url.encode()).hexdigest()
    path = project(
        tmp_path, dependencies='["b"]', tail=f'[develop.sources]\nb={{index="{url}"}}\n'
    )
    inputs = ResolveInputs(
        indexes=(IndexConfig(label, "https://example.invalid/other"),),
        develop_sources=read_develop_sources(path),
    )
    with pytest.raises(ConfigError, match="already configured for another URL"):
        resolve(path, inputs=inputs)


@respx.mock
def test_scoped_http_transport_uses_owning_event_loop(tmp_path: Path) -> None:
    data = source_archive(tmp_path / "b.tar.gz")
    url = "https://example.test/b.tar.gz?token=fixture"
    route = respx.get(url).respond(200, content=data)
    path = project(
        tmp_path, dependencies='["b"]', tail=f'[develop.sources]\nb={{url="{url}"}}\n'
    )
    with archive_outcome():
        result = resolve_for_targets(
            path,
            HttpxAsyncTransport(),
            targets=[ResolveTarget.for_host()],
            inputs=archive_inputs(path, require_hashes=False),
        )
        result.raise_for_failure()
        assert route.call_count == 1
        assert str(result.target_results[0].pins["b"]) == "1"


@pytest.mark.parametrize("revision_kind", ["rev", "tag", "branch", "default"])
def test_git_sources_use_typed_revisions(tmp_path: Path, revision_kind: str) -> None:
    repo = tmp_path / "repository"
    project(repo / "subdir", "b")

    executable = shutil.which("git")
    assert executable is not None

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 - this fixture supplies every Git argument
            [executable, *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "--initial-branch=main")
    git("add", ".")
    git(
        "-c",
        "user.name=Damian Shaw",
        "-c",
        "user.email=damian.peter.shaw@gmail.com",
        "commit",
        "-m",
        "Add fixture",
    )
    sha = git("rev-parse", "HEAD")
    git("tag", "v1")
    rev = {"rev": sha, "tag": "v1", "branch": "main", "default": None}[revision_kind]
    selector = f',{revision_kind}="{rev}"' if rev is not None else ""
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{git="{repo.as_uri()}",subdirectory="subdir"{selector}}}\n',
    )
    inputs = ResolveInputs(
        develop_sources=read_develop_sources(path),
        vcs=VcsConfig(require_pin=revision_kind == "rev"),
    )
    result = resolve(path, inputs=inputs, offline=True, cache_dir=tmp_path / "cache")
    result.raise_for_failure()
    pin = result.target_results[0].lock.pins["b"]
    assert pin.commit_id == sha
    if revision_kind == "rev":
        with pytest.raises(UnsupportedVcsError, match="block"):
            resolve(
                path,
                inputs=inputs.replace(vcs=VcsConfig(policy=VcsPolicy.BLOCK)),
                offline=True,
                cache_dir=tmp_path / "cache",
            )


def test_unreadable_or_invalid_source_file_reports_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    with pytest.raises(ConfigError):
        read_develop_sources(missing)
    path = write(tmp_path / "pyproject.toml", "[[[")
    with pytest.raises(ConfigError):
        read_develop_sources(path)


def test_unused_git_source_does_not_require_vcs_permission(tmp_path: Path) -> None:
    path = project(
        tmp_path, tail='[develop.sources]\nb={git="https://example.invalid/b"}\n'
    )
    assert resolve(path).success


def test_used_git_source_retains_vcs_policy(tmp_path: Path) -> None:
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={git="https://example.invalid/b"}\n',
    )
    with pytest.raises(UnsupportedVcsError):
        resolve(path)


def test_combined_build_group_rejects_standard_source_roots(tmp_path: Path) -> None:
    project(tmp_path / "b", "b")
    path = project(
        tmp_path, tail='[build-system]\nrequires=["b"]\n[develop.sources]\nb="b"\n'
    )
    inputs = ResolveInputs(
        build_group="build", develop_sources=read_develop_sources(path)
    )
    with pytest.raises(ConfigError, match="build-group roots"):
        resolve(path, inputs=inputs)


def test_combined_build_group_rejects_transitive_standard_sources(
    tmp_path: Path,
) -> None:
    index = tmp_path / "index"
    wheel(index, "backend", "1", ("b",))
    project(tmp_path / "b", "b")
    path = project(
        tmp_path,
        tail='[build-system]\nrequires=["backend"]\n[develop.sources]\nb="b"\n',
    )
    inputs = ResolveInputs(
        build_group="build",
        indexes=(IndexConfig("local", index.as_uri()),),
        develop_sources=read_develop_sources(path),
    )
    with pytest.raises(ConfigError, match="combined build-group lock"):
        resolve(path, inputs=inputs)


def test_combined_build_group_with_no_source_overlap(tmp_path: Path) -> None:
    index = tmp_path / "index"
    wheel(index, "backend", "1", ("c",))
    wheel(index, "c", "1")
    project(tmp_path / "b", "b")
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[build-system]\nrequires=["backend","c"]\n[develop.sources]\nb="b"\n',
    )
    inputs = ResolveInputs(
        build_group="build",
        indexes=(IndexConfig("local", index.as_uri()),),
        develop_sources=read_develop_sources(path),
    )
    result = resolve(path, inputs=inputs)
    result.raise_for_failure()
    assert set(result.target_results[0].pins) == {"backend", "b", "c"}


@respx.mock
def test_identical_source_scopes_share_downloads(tmp_path: Path) -> None:
    data = source_archive(tmp_path / "b.tar.gz")
    url = "https://example.test/b.tar.gz"
    route = respx.get(url).respond(200, content=data)
    path = project(
        tmp_path, dependencies='["b"]', tail=f'[develop.sources]\nb={{url="{url}"}}\n'
    )
    inputs = archive_inputs(path, require_hashes=False)
    with archive_outcome():
        result = resolve_for_targets(
            path,
            HttpxAsyncTransport(),
            targets=[
                ResolveTarget.for_host_python("3.12"),
                ResolveTarget.for_host_python("3.13"),
            ],
            inputs=inputs,
        )
        result.raise_for_failure()
        assert route.call_count == 1
        assert {tr.lock.pins["b"].hashes for tr in result.target_results} == {
            (("sha256", hashlib.sha256(data).hexdigest()),)
        }


def test_preparsed_sources_use_the_original_document(tmp_path: Path) -> None:
    """Use the host's parsed document even if the file changes afterwards."""
    path = project(tmp_path, tail='[develop.sources]\nb="b"\n')
    document = toml_io.load_path(path)
    path.write_text('[develop.sources]\nb="changed"\n')

    sources = read_develop_sources(path, invocation_root=tmp_path, document=document)

    assert len(sources) == 1
    assert sources[0].location == str(tmp_path / "b")


@pytest.mark.parametrize("kind", ["path", "url"])
def test_direct_wheel_sources_keep_extras_dependencies_and_hashes(
    tmp_path: Path, kind: str
) -> None:
    wheel(
        tmp_path,
        "b",
        "1",
        ('c; extra == "feature"',),
        extras=("feature",),
        requires_python=">=3.10",
    )
    artifact = tmp_path / "b-1-py3-none-any.whl"
    location = artifact.name if kind == "path" else artifact.as_uri()
    project(tmp_path / "c", "c")
    path = project(
        tmp_path,
        dependencies='["b[feature]"]',
        tail=f'[develop.sources]\nb={{{kind}="{location}"}}\nc="c"\n',
    )
    result = resolve(path, inputs=archive_inputs(path, require_hashes=False))
    result.raise_for_failure()
    lock = result.target_results[0].lock
    assert lock is not None
    assert set(lock.pins) == {"b", "c"}
    pin = lock.pins["b"]
    assert isinstance(pin, ArchivePin)
    assert pin.url == artifact.as_uri()
    assert pin.hashes == (
        ("sha256", hashlib.sha256(artifact.read_bytes()).hexdigest()),
    )

    emitted = build_pylock(build_lock_input(result), lock_dir=tmp_path)
    package = next(package for package in emitted.packages if package.name == "b")
    assert package.archive is not None
    assert package.archive.url == artifact.as_uri()


@pytest.mark.parametrize("declaration", ["path", "url", "both"])
def test_zip_source_reads_static_metadata_without_a_backend(
    tmp_path: Path, declaration: str
) -> None:
    artifact = tmp_path / "b.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "checkout/packages/b/pyproject.toml",
            '[project]\nname="b"\nversion="1"\ndependencies=["c"]\n',
        )
    project(tmp_path / "c", "c")
    source = (
        'path="b.zip",subdirectory="packages/b"'
        if declaration == "path"
        else f'url="{artifact.as_uri()}#subdirectory=packages/b"'
    )
    if declaration == "both":
        source += ',subdirectory="packages/b"'
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{{source}}}\nc="c"\n',
    )
    result = resolve(path, inputs=archive_inputs(path, require_hashes=False))
    result.raise_for_failure()
    lock = result.target_results[0].lock
    assert lock is not None
    assert set(lock.pins) == {"b", "c"}
    pin = lock.pins["b"]
    assert isinstance(pin, ArchivePin)
    assert pin.url == artifact.as_uri()
    assert pin.subdirectory == "packages/b"
    assert pin.hashes == (
        ("sha256", hashlib.sha256(artifact.read_bytes()).hexdigest()),
    )


@respx.mock
def test_hashed_wheel_cache_reuses_verified_bytes_offline(tmp_path: Path) -> None:
    wheel(tmp_path, "b", "1")
    artifact = tmp_path / "b-1-py3-none-any.whl"
    data = artifact.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    url = "https://example.test/b-1-py3-none-any.whl"
    route = respx.get(url).respond(200, content=data)
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{url="{url}",hashes={{sha256="{digest}"}}}}\n',
    )
    cache = tmp_path / "cache"
    inputs = archive_inputs(path)
    first = resolve_for_targets(
        path,
        HttpxAsyncTransport(),
        targets=[ResolveTarget.for_host()],
        cache_dir=cache,
        inputs=inputs,
    )
    first.raise_for_failure()
    second = resolve_for_targets(
        path,
        NoNetwork(),
        targets=[ResolveTarget.for_host()],
        cache_dir=cache,
        offline=True,
        inputs=inputs,
    )
    second.raise_for_failure()
    assert route.call_count == 1
    assert first.target_results[0].lock == second.target_results[0].lock
    with pytest.raises(UnsupportedArchiveError, match="block"):
        resolve_for_targets(
            path,
            NoNetwork(),
            targets=[ResolveTarget.for_host()],
            cache_dir=cache,
            offline=True,
            inputs=inputs.replace(archive=ArchiveConfig(policy=ArchivePolicy.BLOCK)),
        )
    assert route.call_count == 1
    (cached,) = cache.rglob(artifact.name)
    cached.write_bytes(b"changed cached wheel")
    with pytest.raises(SdistHashMismatchError):
        resolve_for_targets(
            path,
            NoNetwork(),
            targets=[ResolveTarget.for_host()],
            cache_dir=cache,
            offline=True,
            inputs=inputs,
        )
    assert route.call_count == 1


@pytest.mark.parametrize("policy", ["sdist-only", "sdist-install"])
def test_wheel_source_respects_distribution_policy(tmp_path: Path, policy: str) -> None:
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={url="https://example.test/b-1-py3-none-any.whl"}\n',
    )
    inputs = ResolveInputs(
        archive=archive_inputs(path, require_hashes=False).archive,
        develop_sources=read_develop_sources(path),
        dist_policy=DistPolicy(policy),
    )
    with pytest.raises(UnsupportedWheelError, match="conflicts with sdist policy"):
        resolve(path, inputs=inputs)


def test_incompatible_wheel_is_refused_before_fetching(tmp_path: Path) -> None:
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={url="https://example.test/b-1-cp999-cp999-linux_x86_64.whl"}\n',
    )
    with pytest.raises(UnsupportedWheelError, match="incompatible with target"):
        resolve(path, inputs=archive_inputs(path, require_hashes=False))


@pytest.mark.parametrize("use_table", [False, True])
def test_project_direct_wheel_reference_keeps_precedence(
    tmp_path: Path, use_table: bool
) -> None:
    wheel(tmp_path, "b", "1")
    artifact = tmp_path / "b-1-py3-none-any.whl"
    path = project(
        tmp_path,
        dependencies=f'["b @ {artifact.as_uri()}"]',
        tail='[develop.sources]\nb="missing"\n' if use_table else "",
    )
    result = resolve(path, inputs=archive_inputs(path, require_hashes=False))
    result.raise_for_failure()
    lock = result.target_results[0].lock
    assert lock is not None
    pin = lock.pins["b"]
    assert isinstance(pin, ArchivePin)
    assert pin.url == artifact.as_uri()
    assert pin.hashes == (
        ("sha256", hashlib.sha256(artifact.read_bytes()).hexdigest()),
    )


def test_project_direct_reference_matches_transitive_reference(tmp_path: Path) -> None:
    wheel(tmp_path, "c", "1")
    child = (tmp_path / "c-1-py3-none-any.whl").as_uri()
    wheel(tmp_path, "b", "1", (f"c @ {child}",))
    parent = (tmp_path / "b-1-py3-none-any.whl").as_uri()
    path = project(tmp_path, dependencies=f'["b @ {parent}", "c @ {child}"]')
    result = resolve(path, inputs=archive_inputs(path, require_hashes=False))
    result.raise_for_failure()
    lock = result.target_results[0].lock
    assert lock is not None
    assert set(lock.pins) == {"b", "c"}
    assert lock.dependencies["b"] == ("c",)


def test_conflicting_project_direct_references_fail_before_fetching(
    tmp_path: Path,
) -> None:
    path = project(
        tmp_path,
        dependencies='["b @ https://example.test/one.whl", "b @ https://example.test/two.whl"]',
    )
    with pytest.raises(ConfigError, match="conflicting direct sources for 'b'"):
        resolve(path)


def test_conflicting_transitive_direct_reference_is_refused(tmp_path: Path) -> None:
    wheel(tmp_path, "c", "1")
    child = (tmp_path / "c-1-py3-none-any.whl").as_uri()
    wheel(tmp_path, "b", "1", ("c @ https://example.test/other.whl",))
    parent = (tmp_path / "b-1-py3-none-any.whl").as_uri()
    path = project(tmp_path, dependencies=f'["b @ {parent}", "c @ {child}"]')
    with pytest.raises(ConfigError, match="conflicting direct sources for 'c'"):
        resolve(path, inputs=archive_inputs(path, require_hashes=False))


def test_inactive_direct_reference_is_not_fetched(tmp_path: Path) -> None:
    path = project(
        tmp_path,
        dependencies="[\"b @ https://example.test/unreadable.whl ; python_version < '1'\"]",
    )
    result = resolve(path)
    result.raise_for_failure()
    assert result.target_results[0].pins == {}


@pytest.mark.parametrize(("name", "version"), [("b", "2"), ("other", "1")])
def test_wheel_source_metadata_must_match_filename(
    tmp_path: Path, name: str, version: str
) -> None:
    artifact = tmp_path / "b-1-py3-none-any.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "b-1.dist-info/METADATA", f"Name: {name}\nVersion: {version}\n"
        )
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{path="{artifact.name}"}}\n',
    )
    with pytest.raises(UnsupportedWheelError, match="does not match wheel filename"):
        resolve(path, inputs=archive_inputs(path, require_hashes=False))


def test_wheel_source_filename_must_match_declared_name(tmp_path: Path) -> None:
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={url="https://example.test/other-1-py3-none-any.whl"}\n',
    )
    with pytest.raises(SourceNameMismatchError, match="filename declares 'other'"):
        resolve(path, inputs=archive_inputs(path, require_hashes=False))


def test_wheel_source_metadata_uses_verified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the cached file cannot change the metadata of verified bytes."""
    wheel(tmp_path, "b", "1")
    artifact = tmp_path / "b-1-py3-none-any.whl"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    atomic_write = source_materializer.atomic_write
    changed: list[Path] = []

    def replace_cache(path: Path, data: bytes) -> None:
        atomic_write(path, data)
        path.write_bytes(b"changed after verification")
        changed.append(path)

    monkeypatch.setattr(source_materializer, "atomic_write", replace_cache)
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{path="{artifact.name}"}}\n',
    )
    result = resolve(path, inputs=archive_inputs(path, require_hashes=False))
    result.raise_for_failure()
    lock = result.target_results[0].lock
    assert lock is not None
    assert len(changed) == 1
    pin = lock.pins["b"]
    assert isinstance(pin, ArchivePin)
    assert pin.version == "1"
    assert pin.hashes == (("sha256", digest),)


def test_wheel_cache_write_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel(tmp_path, "b", "1")

    def denied(path: Path, data: bytes) -> None:
        raise PermissionError("cache is read-only")

    monkeypatch.setattr(source_materializer, "atomic_write", denied)
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={path="b-1-py3-none-any.whl"}\n',
    )
    with pytest.raises(UnsupportedWheelError, match="cache is read-only"):
        resolve(path, inputs=archive_inputs(path, require_hashes=False))


@pytest.mark.parametrize(
    ("url", "cache", "message"),
    [
        (
            "https://example.test/b%2Fbad-1-py3-none-any.whl",
            "present",
            "invalid wheel filename",
        ),
        ("https://example.test/b.whl", "present", "Invalid wheel filename"),
        (
            "https://example.test/b-1-py3-none-any.whl#subdirectory=src",
            "present",
            "cannot have a subdirectory",
        ),
        (
            "https://example.test/b-1-py3-none-any.whl",
            "absent",
            "no archive cache directory",
        ),
    ],
)
def test_wheel_materializer_rejects_invalid_requests_before_fetching(
    tmp_path: Path, url: str, cache: str, message: str
) -> None:
    source = ArchiveSource("b", url)
    request = SourceRequest(
        "b",
        source,
        BuildPolicy.NEVER,
        None,
        tmp_path if cache == "present" else None,
        require_pin=True,
        require_hashes=False,
    )
    with pytest.raises(UnsupportedWheelError, match=message):
        source_materializer.materialize_source(make_coordinator([]), request, None)


def test_invalid_wheel_metadata_is_reported(tmp_path: Path) -> None:
    artifact = tmp_path / "b-1-py3-none-any.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("b-1.dist-info/METADATA", "Name: b\n")
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={path="b-1-py3-none-any.whl"}\n',
    )
    with pytest.raises(UnsupportedWheelError, match="invalid wheel metadata"):
        resolve(path, inputs=archive_inputs(path, require_hashes=False))


@pytest.mark.parametrize("field_hash", [False, True])
def test_archive_url_hash_fragment_is_preserved(
    tmp_path: Path, field_hash: bool
) -> None:
    wheel(tmp_path, "b", "1")
    artifact = tmp_path / "b-1-py3-none-any.whl"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    hashes = f',hashes={{sha256="{digest}"}}' if field_hash else ""
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{url="{artifact.as_uri()}#sha256={digest}"{hashes}}}\n',
    )
    result = resolve(path, inputs=archive_inputs(path))
    result.raise_for_failure()
    lock = result.target_results[0].lock
    assert lock is not None
    pin = lock.pins["b"]
    assert isinstance(pin, ArchivePin)
    assert pin.url == artifact.as_uri()
    assert pin.hashes == (("sha256", digest),)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            'url="https://example.test/b.zip#sha256=00",hashes={sha256="11"}',
            "conflicting sha256 hashes",
        ),
        ('url="https://example.test/b.zip#sha256="', "hash cannot be empty"),
        (
            'url="https://example.test/b.zip#subdirectory=one",subdirectory="two"',
            "conflicting subdirectories",
        ),
        (
            'url="https://example.test/b-1-py3-none-any.whl#subdirectory=one"',
            "wheel cannot have a subdirectory",
        ),
    ],
)
def test_conflicting_archive_url_fields_fail_before_fetching(
    tmp_path: Path, entry: str, message: str
) -> None:
    path = project(
        tmp_path, dependencies='["b"]', tail=f"[develop.sources]\nb={{{entry}}}\n"
    )
    with pytest.raises(ConfigError, match=message):
        resolve(path)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://[::1/b-1-py3-none-any.whl", "direct source for 'b'"),
        ("https://example.test/b-1-py3-none-any.whl#sha256=", "empty hash"),
    ],
)
def test_invalid_direct_url_is_reported_before_fetching(
    tmp_path: Path, url: str, message: str
) -> None:
    path = project(tmp_path, dependencies=f'["b @ {url}"]')
    with pytest.raises(ConfigError, match=message):
        resolve(path)


def test_conditional_direct_references_keep_separate_origins(tmp_path: Path) -> None:
    wheel(tmp_path, "b", "1")
    wheel(tmp_path, "b", "2")
    one = (tmp_path / "b-1-py3-none-any.whl").as_uri()
    two = (tmp_path / "b-2-py3-none-any.whl").as_uri()
    path = project(
        tmp_path,
        dependencies=f"""["b @ {one} ; python_version < '3.13'", "b @ {two} ; python_version >= '3.13'"]""",
    )
    result = resolve_for_targets(
        path,
        NoNetwork(),
        inputs=archive_inputs(path, require_hashes=False),
        targets=[
            ResolveTarget.for_host_python("3.12"),
            ResolveTarget.for_host_python("3.13"),
        ],
    )
    result.raise_for_failure()
    assert [str(target.pins["b"]) for target in result.target_results] == ["1", "2"]


def test_path_resolution_keeps_the_declared_wheel_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file's symlink target name must not replace its declared wheel filename."""
    wheel(tmp_path, "b", "1")
    artifact = tmp_path / "b-1-py3-none-any.whl"
    target = tmp_path / "payload.bin"
    target.write_bytes(artifact.read_bytes())
    resolve_path = Path.resolve

    def physical(path: Path, *args: object, **kwargs: object) -> Path:
        if path == artifact:
            return target
        return resolve_path(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", physical)
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={path="b-1-py3-none-any.whl"}\n',
    )
    result = resolve(path, inputs=archive_inputs(path, require_hashes=False))
    result.raise_for_failure()
    lock = result.target_results[0].lock
    assert lock is not None
    pin = lock.pins["b"]
    assert isinstance(pin, ArchivePin)
    assert pin.url == artifact.as_uri()


@pytest.mark.parametrize("active", [False, True])
def test_workspace_local_source_is_a_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, active: bool
) -> None:
    project(tmp_path / "member", "b", "1")
    project(tmp_path / "alternative", "b", "2")
    marker = "" if active else ",marker=\"python_version < '3.0'\""
    path = project(
        tmp_path,
        dependencies='["b>=1"]',
        tail=f'[develop.sources]\nb={{path="alternative"{marker}}}\n',
    )
    inputs = ResolveInputs(
        local_sources=(LocalSource("B", str(tmp_path / "member"), editable=True),),
        workspace_member_names=frozenset({"b"}),
        develop_sources=read_develop_sources(path),
    )
    with caplog.at_level(logging.WARNING):
        result = resolve(path, inputs=inputs)
    result.raise_for_failure()
    lock = result.target_results[0].lock
    assert lock is not None
    pin = lock.pins["b"]
    assert isinstance(pin, LocalPin)
    assert pin.version == ("2" if active else "1")
    assert pin.path == str(tmp_path / ("alternative" if active else "member"))
    assert pin.editable is not active
    assert not caplog.records


@pytest.mark.parametrize("kind", ["url", "path"])
def test_archive_table_keeps_hash_policy(tmp_path: Path, kind: str) -> None:
    artifact = tmp_path / "b-1-py3-none-any.whl"
    artifact.touch()
    location = artifact.as_uri() if kind == "url" else artifact.name
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{{kind}="{location}"}}\n',
    )
    with pytest.raises(
        UnsupportedArchiveError, match="requires an expected digest"
    ) as caught:
        resolve(path)
    assert "archive source 'b'" in str(caught.value)
    assert "[tool.nab.archive]" in str(caught.value)


def test_selected_archive_refusal_does_not_use_workspace_fallback(
    tmp_path: Path,
) -> None:
    project(tmp_path / "member", "b")
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={url="https://example.test/b-1-py3-none-any.whl"}\n',
    )
    inputs = ResolveInputs(
        develop_sources=read_develop_sources(path),
        workspace_member_names=frozenset({"b"}),
        local_sources=(LocalSource("b", str(tmp_path / "member")),),
    )
    with pytest.raises(UnsupportedArchiveError, match="requires an expected digest"):
        resolve(path, inputs=inputs)


@pytest.mark.parametrize("kind", ["url", "git"])
def test_unused_remote_source_requires_no_permission(tmp_path: Path, kind: str) -> None:
    path = project(
        tmp_path, tail=f'[develop.sources]\nb={{{kind}="https://example.test/b"}}\n'
    )
    assert resolve(path).success


def test_archive_permission_keeps_expected_hash_required(tmp_path: Path) -> None:
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={url="https://example.test/b-1-py3-none-any.whl"}\n',
    )
    with pytest.raises(UnsupportedArchiveError, match="requires an expected digest"):
        resolve(path, inputs=archive_inputs(path))


@pytest.mark.parametrize("tighten", ["block", "location", "hashes"])
def test_observed_hash_does_not_grant_permission_on_reuse(
    tmp_path: Path, tighten: str
) -> None:
    wheel(tmp_path, "b", "1")
    artifact = tmp_path / "b-1-py3-none-any.whl"
    source = ArchiveSource("b", artifact.as_uri())
    path = project(tmp_path, dependencies='["b"]')
    inputs = archive_inputs(path, require_hashes=False).replace(
        archive_sources=(source,)
    )
    inputs = inputs.replace(
        archive=ArchiveConfig(
            policy=ArchivePolicy.ALLOW,
            allowed_urls=(artifact.as_uri(),),
            require_hashes=False,
        )
    )
    first = resolve(path, inputs=inputs, cache_dir=tmp_path / "cache")
    first.raise_for_failure()
    assert source.url == artifact.as_uri()
    assert first.target_results[0].lock.pins["b"].hashes == (
        ("sha256", hashlib.sha256(artifact.read_bytes()).hexdigest()),
    )
    policy = {
        "block": ArchiveConfig(policy=ArchivePolicy.BLOCK),
        "location": ArchiveConfig(
            policy=ArchivePolicy.ALLOW,
            allowed_urls=("https://example.test/",),
            require_hashes=False,
        ),
        "hashes": ArchiveConfig(
            policy=ArchivePolicy.ALLOW, allowed_urls=(tmp_path.as_uri() + "/",)
        ),
    }[tighten]
    artifact.unlink()
    with pytest.raises(UnsupportedArchiveError):
        resolve(
            path,
            inputs=inputs.replace(archive=policy),
            cache_dir=tmp_path / "cache",
            offline=True,
        )


@pytest.mark.parametrize("version_override", [False, True])
def test_archive_distribution_policy_uses_materialized_version(
    tmp_path: Path, version_override: bool
) -> None:
    artifact = tmp_path / "b.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("pyproject.toml", '[project]\nname="b"\nversion="1"\n')
    path = project(
        tmp_path, dependencies='["b"]', tail='[develop.sources]\nb={path="b.zip"}\n'
    )
    inputs = archive_inputs(path, require_hashes=False).replace(
        dist_policy=DistPolicy.WHEEL_ONLY,
        package_overrides=(pkg_override("b==1", dist_policy=DistPolicy.WHEEL_OR_SDIST),)
        if version_override
        else (),
    )
    if version_override:
        assert resolve(path, inputs=inputs).success
    else:
        with pytest.raises(UnsupportedSdistError, match="wheel-only.*source archive"):
            resolve(path, inputs=inputs)


def test_native_index_route_overrides_implicit_workspace_member(tmp_path: Path) -> None:
    project(tmp_path / "member", "b", "9")
    index = tmp_path / "index"
    wheel(index, "b", "1")
    path = project(
        tmp_path, dependencies='["b"]', tail='[develop.sources]\nb="member"\n'
    )
    inputs = ResolveInputs(
        develop_sources=read_develop_sources(path),
        local_sources=(LocalSource("b", str(tmp_path / "member")),),
        workspace_member_names=frozenset({"b"}),
        indexes=(IndexConfig("internal", index.as_uri()),),
        package_overrides=(pkg_override("b", index="internal"),),
    )
    result = resolve(path, inputs=inputs)
    result.raise_for_failure()
    assert str(result.target_results[0].pins["b"]) == "1"


def test_standard_index_keeps_configured_distribution_policy(tmp_path: Path) -> None:
    index = tmp_path / "index"
    wheel(index, "b", "1")
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{index="{index.as_uri()}"}}\n',
    )
    inputs = ResolveInputs(
        develop_sources=read_develop_sources(path),
        indexes=(IndexConfig("internal", index.as_uri()),),
        index_overrides={"internal": IndexOverride(dist_policy=DistPolicy.SDIST_ONLY)},
    )
    assert not resolve(path, inputs=inputs).success


@pytest.mark.parametrize(
    "policy",
    [
        VcsConfig(),
        VcsConfig(policy=VcsPolicy.ALLOW),
        VcsConfig(policy=VcsPolicy.ALLOW, allowed_schemes=frozenset({"git+https"})),
        VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://example.test/b",),
        ),
    ],
)
def test_standard_git_keeps_pin_policy(
    tmp_path: Path, policy: VcsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_clone(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused source reached cloning")

    monkeypatch.setattr(source_materializer.vcs, "prepare_clone", refuse_clone)
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail='[develop.sources]\nb={git="https://example.test/b",branch="main"}\n',
    )
    with pytest.raises(UnsupportedVcsError) as caught:
        resolve(
            path,
            inputs=ResolveInputs(
                develop_sources=read_develop_sources(path), vcs=policy
            ),
            cache_dir=tmp_path / "cache",
        )
    assert "vcs source 'b'" in str(caught.value)
    assert "[tool.nab.vcs]" in str(caught.value)
    assert not list((tmp_path / "cache").rglob("HEAD"))


def test_host_does_not_treat_unpinned_archive_as_expected_content(
    tmp_path: Path,
) -> None:
    source = ArchiveSource("b", "https://example.test/b.zip")
    request = SourceRequest(
        "b", source, BuildPolicy.NEVER, None, tmp_path, require_pin=True
    )
    with pytest.raises(
        UnsupportedSdistError, match="expected archive hash is required"
    ):
        source_materializer.materialize_source(
            make_coordinator(),
            request,
            ResolveInputs(),
            transport_factory=NoNetwork,
        )


@pytest.mark.parametrize(
    "permission", [None, BuildPolicy.NEVER, BuildPolicy.BUILD_LOCAL]
)
def test_directory_table_respects_package_build_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, permission: BuildPolicy | None
) -> None:
    project(
        tmp_path / "b",
        "b",
        tail='[build-system]\nrequires=[]\nbuild-backend="example"\n',
    )
    source = tmp_path / "b" / "pyproject.toml"
    source.write_text(
        source.read_text().replace("dependencies=[]", 'dynamic=["dependencies"]')
    )
    path = project(tmp_path, dependencies='["b"]', tail='[develop.sources]\nb="b"\n')
    calls = []
    metadata = WheelMetadata(
        name="b",
        version=Version("1"),
        requires_python=None,
        requires_dist=[],
        provides_extra=[],
    )

    def backend(directory: Path, **kwargs: object) -> WheelMetadata:
        calls.append(directory)
        return metadata

    monkeypatch.setattr("nab_project.build_backend.extract_metadata", backend)
    inputs = ResolveInputs(
        develop_sources=read_develop_sources(path),
        build_policy=BuildPolicy.NEVER,
        package_overrides=(pkg_override("b", build_policy=permission),)
        if permission
        else (),
    )
    if permission is BuildPolicy.BUILD_LOCAL:
        result = resolve(path, inputs=inputs)
        result.raise_for_failure()
        assert calls == [tmp_path / "b"]
    else:
        with pytest.raises(UnsupportedSdistError, match="build-local"):
            resolve(path, inputs=inputs)
        assert calls == []


@pytest.mark.parametrize("direct", [False, True])
def test_selected_wheel_needs_only_its_declaration_and_hash(
    tmp_path: Path, direct: bool
) -> None:
    wheel(tmp_path, "b", "1")
    artifact = tmp_path / "b-1-py3-none-any.whl"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    path = project(
        tmp_path,
        dependencies=f'["b @ {artifact.as_uri()}#sha256={digest}"]'
        if direct
        else '["b"]',
        tail=""
        if direct
        else f'[develop.sources]\nb={{url="{artifact.as_uri()}",hashes={{sha256="{digest}"}}}}\n',
    )
    result = resolve(path)
    result.raise_for_failure()
    pin = result.target_results[0].lock.pins["b"]
    assert isinstance(pin, ArchivePin)
    assert pin.url == artifact.as_uri()
    assert pin.hashes == (("sha256", digest),)


@pytest.mark.parametrize("kind", ["url", "git"])
def test_source_declarations_still_obey_explicit_blocks(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_clone(*args: object, **kwargs: object) -> None:
        raise AssertionError("an explicitly blocked source reached cloning")

    monkeypatch.setattr(source_materializer.vcs, "prepare_clone", refuse_clone)
    entry = (
        'url="https://example.test/b.zip",hashes={sha256="00"}'
        if kind == "url"
        else f'git="https://example.test/b.git",rev="{"a" * 40}"'
    )
    path = project(
        tmp_path, dependencies='["b"]', tail=f"[develop.sources]\nb={{{entry}}}\n"
    )
    inputs = ResolveInputs(
        develop_sources=read_develop_sources(path),
        archive=ArchiveConfig(policy=ArchivePolicy.BLOCK),
        vcs=VcsConfig(policy=VcsPolicy.BLOCK),
    )
    with pytest.raises(
        UnsupportedArchiveError if kind == "url" else UnsupportedVcsError, match="block"
    ):
        resolve(path, inputs=inputs)


def test_source_index_is_not_a_fallback_for_other_packages(tmp_path: Path) -> None:
    primary, private = tmp_path / "primary", tmp_path / "private"
    wheel(primary, "unrelated", "1")
    wheel(private, "b", "1", ("c",))
    wheel(private, "c", "1")
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{index="{private.as_uri()}"}}\n',
    )
    inputs = ResolveInputs(
        indexes=(IndexConfig("primary", primary.as_uri()),),
        develop_sources=read_develop_sources(path),
    )
    assert not resolve(path, inputs=inputs).success
    wheel(primary, "c", "2")
    result = resolve(path, inputs=inputs)
    result.raise_for_failure()
    assert str(result.target_results[0].pins["c"]) == "2"


def test_one_source_index_can_be_declared_for_another_package(tmp_path: Path) -> None:
    primary, private = tmp_path / "primary", tmp_path / "private"
    wheel(primary, "unrelated", "1")
    wheel(private, "b", "1", ("c",))
    wheel(private, "c", "1")
    path = project(
        tmp_path,
        dependencies='["b"]',
        tail=f'[develop.sources]\nb={{index="{private.as_uri()}"}}\nc={{index="{private.as_uri()}"}}\n',
    )
    inputs = ResolveInputs(
        indexes=(IndexConfig("primary", primary.as_uri()),),
        develop_sources=read_develop_sources(path),
    )
    result = resolve(path, inputs=inputs)
    result.raise_for_failure()
    assert set(result.target_results[0].pins) == {"b", "c"}


def test_metadata_requirements_do_not_grant_source_permission(tmp_path: Path) -> None:
    wheel(tmp_path, "b", "1")
    artifact = tmp_path / "b-1-py3-none-any.whl"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    path = project(
        tmp_path, dependencies=f'["b @ {artifact.as_uri()}#sha256={digest}"]'
    )
    with pytest.raises(UnsupportedArchiveError, match='archive.policy="declared"'):
        resolve(path, allow_source_declarations=False)
    inputs = ResolveInputs(
        archive=ArchiveConfig(
            policy=ArchivePolicy.ALLOW, allowed_urls=(artifact.as_uri(),)
        )
    )
    assert resolve(path, inputs=inputs, allow_source_declarations=False).success


def test_other_declarations_do_not_admit_native_archive(tmp_path: Path) -> None:
    wheel(tmp_path, "b", "1")
    artifact = tmp_path / "b-1-py3-none-any.whl"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    project(tmp_path / "c", "c")
    path = project(
        tmp_path, dependencies='["b","c"]', tail='[develop.sources]\nc="c"\n'
    )
    inputs = ResolveInputs(
        develop_sources=read_develop_sources(path),
        archive_sources=(ArchiveSource("b", artifact.as_uri() + "#sha256=" + digest),),
    )
    with pytest.raises(UnsupportedArchiveError, match='archive.policy="declared"'):
        resolve(path, inputs=inputs)
