"""Resolve dynamic sdists for a declared matrix through the offline CLI."""

from __future__ import annotations

import io
import re
import tarfile
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest

from nab.cli import run
from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.toml_io import loads
from nab_provider._vendor.packaging.pylock import PackageSdist, Pylock
from nab_provider._vendor.packaging.tags import Tag
from nab_provider.metadata import parse_metadata
from nab_provider.records import IndexConfig
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator as make_provider_coordinator
from nab_provider.testing import make_metadata, make_sdist, make_wheel

from ._local_artifacts import make_wheel_archive, publish_artifacts

if TYPE_CHECKING:
    from pathlib import Path

    from nab_provider.testing import FakeFetchPort


def _dynamic_sdist(version: str = "6.0.0") -> bytes:
    """Publish dependency fields that require reconciliation with a wheel."""
    metadata = make_metadata(
        "watchdog",
        version,
        "stale-dependency",
        metadata_version="2.2",
        requires_python=">=3.10",
    )
    members = {
        "PKG-INFO": metadata.rstrip() + "\nDynamic: Requires-Dist\n\n",
        "pyproject.toml": (
            f'[project]\nname = "watchdog"\nversion = "{version}"\n'
            'requires-python = ">=3.10"\ndynamic = ["dependencies"]\n'
        ),
    }
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as archive:
        for name, text in members.items():
            payload = text.encode("utf-8")
            member = tarfile.TarInfo(f"watchdog-{version}/{name}")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return content.getvalue()


@pytest.fixture
def release_project(hermetic_roots: Path) -> Path:
    """Offer a dynamic sdist and one wheel that cannot install on Python 3.14."""
    index = hermetic_roots / "index"
    tag = "cp313-cp313-macosx_11_0_arm64"
    publish_artifacts(
        index,
        "watchdog",
        {
            "watchdog-6.0.0.tar.gz": _dynamic_sdist(),
            f"watchdog-6.0.0-{tag}.whl": make_wheel_archive(
                "watchdog",
                "6.0.0",
                make_metadata(
                    "watchdog",
                    "6.0.0",
                    'macos-dep; sys_platform == "darwin"',
                    'linux-dep; sys_platform == "linux"',
                    'old-python; python_version < "3.14"',
                    metadata_version="2.2",
                    requires_python=">=3.10",
                ),
                tag,
            ),
        },
    )
    for name in ("macos-dep", "linux-dep"):
        wheel_name = name.replace("-", "_")
        publish_artifacts(
            index,
            name,
            {
                f"{wheel_name}-1-py3-none-any.whl": make_wheel_archive(
                    wheel_name, "1", make_metadata(name, "1"), "py3-none-any"
                )
            },
        )
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname = "example"\nversion = "1"\n'
        'requires-python = ">=3.10"\ndependencies = ["watchdog==6.0.0"]\n'
        '[tool.nab]\nmode = "universal"\n'
        '[tool.nab.matrix]\npython = "==3.14"\n'
        'platforms = ["macos_arm64", "linux_x86_64"]\n'
        f'[[tool.nab.indexes]]\nname = "local"\nurl = "{index.as_uri()}"\n',
        encoding="utf-8",
    )
    return project


def _lock(project: Path, *flags: str) -> int:
    """Resolve to stdout without reading or populating a remote cache."""
    return run(
        ["lock", str(project), "--offline", "--no-cache", "--output", "-", *flags]
    )


def _fallback_release(project: Path) -> None:
    """Offer three dynamic releases before an older readable wheel."""
    artifacts = {
        f"watchdog-{version}.tar.gz": _dynamic_sdist(version)
        for version in ("6.0.0", "6.1.0", "6.2.0")
    }
    artifacts["watchdog-5.0.3-py3-none-any.whl"] = make_wheel_archive(
        "watchdog",
        "5.0.3",
        make_metadata("watchdog", "5.0.3"),
        "py3-none-any",
    )
    publish_artifacts(project.parent / "index", "watchdog", artifacts)


@pytest.fixture
def wider_project(release_project: Path) -> Path:
    """Resolve a transitive watchdog dependency alongside two other packages."""
    _fallback_release(release_project)
    publish_artifacts(
        release_project.parent / "index",
        "application",
        {
            "application-1-py3-none-any.whl": make_wheel_archive(
                "application",
                "1",
                make_metadata("application", "1", "watchdog>=5"),
                "py3-none-any",
            )
        },
    )
    release_project.write_text(
        release_project.read_text(encoding="utf-8").replace(
            'dependencies = ["watchdog==6.0.0"]',
            'dependencies = ["application", "macos-dep", "linux-dep"]',
        ),
        encoding="utf-8",
    )
    return release_project


def test_rejected_dynamic_releases_are_quiet_when_an_older_version_resolves(
    wider_project: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _lock(wider_project) == 0

    captured = capsys.readouterr()
    pylock = Pylock.from_dict(loads(captured.out))

    assert {str(package.name) for package in pylock.packages} == {
        "application",
        "watchdog",
        "macos-dep",
        "linux-dep",
    }

    watchdog = next(
        package for package in pylock.packages if package.name == "watchdog"
    )

    assert str(watchdog.version) == "5.0.3"
    assert "assume-universal-deps" not in captured.err + caplog.text


def test_rejected_dynamic_releases_do_not_add_hints_to_an_unrelated_failure(
    wider_project: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    wider_project.write_text(
        wider_project.read_text(encoding="utf-8").replace(
            '"application", "macos-dep", "linux-dep"',
            '"application", "macos-dep", "linux-dep", "absent==1"',
        ),
        encoding="utf-8",
    )

    assert _lock(wider_project) == 1

    captured = capsys.readouterr()

    assert "absent" in captured.err
    assert "assume-universal-deps" not in captured.err + caplog.text


def test_many_dynamic_rejections_produce_one_hint_per_command(
    wider_project: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    wider_project.write_text(
        wider_project.read_text(encoding="utf-8").replace(
            '"application", "macos-dep", "linux-dep"',
            '"application", "macos-dep", "linux-dep", "watchdog>=6"',
        ),
        encoding="utf-8",
    )

    assert _lock(wider_project) == 1

    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err.count("try:") == 1
    assert captured.err.count("assume-universal-deps = true") == 1
    assert "assume-universal-deps" not in caplog.text


@pytest.mark.parametrize("requirement", ["watchdog==6.0.0", "watchdog[events]==6.0.0"])
def test_dynamic_sdist_failure_suggests_a_release_scoped_assertion(
    release_project: Path, capsys: pytest.CaptureFixture[str], requirement: str
) -> None:
    release_project.write_text(
        release_project.read_text(encoding="utf-8").replace(
            '"watchdog==6.0.0"', f'"{requirement}"'
        ),
        encoding="utf-8",
    )

    assert _lock(release_project) == 1

    captured = capsys.readouterr()

    assert captured.out == ""
    assert "every version in range was rejected on its metadata" in captured.err
    assert 'match = ["watchdog==6.0.0"]' in captured.err
    assert "assume-universal-deps = true" in captured.err


@pytest.fixture
def dynamic_hint_project(hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configure a dynamic release with fresh fetch state for each command."""

    def coordinator(_transport: object, **_: object) -> nullcontext[FakeFetchPort]:
        """Keep metadata state independent between attempted repairs."""
        port = make_coordinator(
            [
                make_wheel(
                    "6.0.0", package="watchdog", tag="cp313-cp313-macosx_11_0_arm64"
                ),
                make_sdist("6.0.0", package="watchdog", hashes=(("sha256", "a" * 64),)),
            ],
            package="watchdog",
            metadata_text=make_metadata("watchdog", "6.0.0"),
            sdist_pkg_info=make_metadata("watchdog", "6.0.0", metadata_version="2.2")
            + "Dynamic: Requires-Dist\n",
            sdist_pyproject_toml=(
                '[project]\nname="watchdog"\nversion="6.0.0"\ndynamic=["dependencies"]\n'
            ),
        )

        port.index.store_listing_index("watchdog", "local")
        port.indexes = [IndexConfig("local", "https://example.com/simple/")]
        return nullcontext(port)

    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", coordinator)

    project = hermetic_roots / "pyproject.toml"
    body = (
        '[project]\nname="example"\nversion="1"\n'
        'requires-python=">=3.10"\ndependencies=["watchdog==6.0.0"]\n'
        '[tool.nab]\nmode="universal"\n'
        '[tool.nab.matrix]\npython="==3.14"\nplatforms=["linux_x86_64"]\n'
        '[[tool.nab.indexes]]\nname="local"\nurl="https://example.com/simple/"\n'
    )
    project.write_text(body, encoding="utf-8")
    return project


@pytest.mark.parametrize("duplicate", [False, True], ids=["single-file", "both-files"])
@pytest.mark.parametrize(
    ("policy", "location", "scope"),
    [
        pytest.param(
            '[tool.nab.packages."watchdog<7"]\n',
            "{project}: packages.'watchdog<7'",
            "every version and package matched by this entry",
            id="package",
        ),
        pytest.param(
            '[[tool.nab.package-rules]]\nmatch = ["watchdog<7", "other"]\n',
            "{project}: package-rules[0]",
            "every version and package matched by this entry",
            id="shared-rule",
        ),
        pytest.param(
            "[tool.nab.index.local]\n",
            "index 'local'",
            "every package served by this index",
            id="index",
        ),
    ],
)
def test_dynamic_sdist_hint_repairs_the_existing_assertion(
    dynamic_hint_project: Path,
    capsys: pytest.CaptureFixture[str],
    policy: str,
    location: str,
    scope: str,
    *,
    duplicate: bool,
) -> None:
    project = dynamic_hint_project
    body = (
        project.read_text(encoding="utf-8") + policy + "assume-universal-deps = false\n"
    )
    project.write_text(body, encoding="utf-8")
    declarations = [project]
    if duplicate:
        nab_config = project.with_name("nab.toml")
        nab_config.write_text(
            policy.replace("tool.nab.", "") + "assume-universal-deps = false\n",
            encoding="utf-8",
        )
        declarations.append(nab_config)

    assert _lock(project, "--project-assume-universal-deps") == 1
    failure = capsys.readouterr()
    location = location.format(project=declarations[-1])
    assert f"set assume-universal-deps = true in {location};" in failure.err
    assert scope in failure.err
    if duplicate:
        assert (
            "if declared in both pyproject.toml and nab.toml, update both declarations"
            in failure.err
        )

    for declaration in declarations:
        declaration.write_text(
            declaration.read_text(encoding="utf-8").replace(
                "assume-universal-deps = false", "assume-universal-deps = true"
            ),
            encoding="utf-8",
        )

    assert _lock(project, "--project-assume-universal-deps") == 0

    pylock = Pylock.from_dict(loads(capsys.readouterr().out))
    pylock.validate()
    assert len(pylock.packages) == 1

    watchdog = pylock.packages[0]
    assert watchdog.name == "watchdog"
    assert str(watchdog.version) == "6.0.0"
    assert watchdog.sdist is not None
    assert not watchdog.wheels


@pytest.mark.parametrize(
    "policy",
    [
        pytest.param("", id="default"),
        pytest.param(
            '[tool.nab.packages."watchdog==6.0.0"]\nbuild-policy = "never"\n',
            id="existing-release-table",
        ),
        pytest.param(
            '[tool.nab.packages."watchdog>=7"]\nassume-universal-deps = false\n',
            id="other-range",
        ),
        pytest.param(
            '[tool.nab.packages."watchdog==6.0.0+local"]\nassume-universal-deps = false\n',
            id="local-sibling",
        ),
    ],
)
def test_dynamic_sdist_hint_adds_a_disjoint_assertion(
    dynamic_hint_project: Path,
    capsys: pytest.CaptureFixture[str],
    policy: str,
) -> None:
    project = dynamic_hint_project
    body = project.read_text(encoding="utf-8") + policy
    project.write_text(body, encoding="utf-8")

    assert _lock(project) == 1
    failure = capsys.readouterr()
    match = re.search(
        r"add a (\[\[tool.nab.package-rules\]\]) entry with"
        r" (match = \[.*?\]) and (assume-universal-deps = true)",
        failure.err,
    )
    assert match is not None, failure.err

    project.write_text(body + "\n" + "\n".join(match.groups()) + "\n", encoding="utf-8")

    assert _lock(project) == 0
    pylock = Pylock.from_dict(loads(capsys.readouterr().out))
    pylock.validate()
    assert [str(package.version) for package in pylock.packages] == ["6.0.0"]
    assert pylock.packages[0].sdist is not None
    assert not pylock.packages[0].wheels


@pytest.mark.parametrize("duplicate", [False, True], ids=["single-file", "both-files"])
def test_dynamic_sdist_hint_adds_to_an_existing_bare_table(
    dynamic_hint_project: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    duplicate: bool,
) -> None:
    project = dynamic_hint_project
    body = (
        project.read_text(encoding="utf-8")
        + '[tool.nab.packages.watchdog]\nbuild-policy = "never"\n'
    )
    project.write_text(body, encoding="utf-8")
    declarations = [project]
    if duplicate:
        nab_config = project.with_name("nab.toml")
        nab_config.write_text(
            '[packages.watchdog]\nbuild-policy = "never"\n', encoding="utf-8"
        )
        declarations.append(nab_config)

    assert _lock(project) == 1
    failure = capsys.readouterr()
    assert (
        f"set assume-universal-deps = true in {declarations[-1]}: packages.'watchdog';"
        in failure.err
    )
    if duplicate:
        assert (
            "if declared in both pyproject.toml and nab.toml, update both declarations"
            in failure.err
        )

    for declaration in declarations:
        with declaration.open("a", encoding="utf-8") as stream:
            stream.write("assume-universal-deps = true\n")

    assert _lock(project) == 0
    assert loads(capsys.readouterr().out)["packages"][0]["name"] == "watchdog"


def test_dynamic_sdist_hint_requests_revision_of_a_legacy_selector(
    dynamic_hint_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = dynamic_hint_project
    body = (
        project.read_text(encoding="utf-8")
        + '[tool.nab.packages."watchdog===6.0.0.0"]\nassume-universal-deps = false\n'
    )
    project.write_text(body, encoding="utf-8")

    assert _lock(project) == 1
    failure = capsys.readouterr()
    assert "revise the scoped assume-universal-deps entries" in failure.err
    assert "and assume-universal-deps = true" not in failure.err
    assert "===" not in failure.err

    project.write_text(
        body.replace("watchdog===6.0.0.0", "watchdog<7").replace(
            "assume-universal-deps = false", "assume-universal-deps = true"
        ),
        encoding="utf-8",
    )
    assert _lock(project) == 0
    assert loads(capsys.readouterr().out)["packages"][0]["name"] == "watchdog"


@pytest.mark.parametrize("surface", ["package", "cli"])
def test_assertion_locks_the_sdist_with_target_conditioned_dependencies(
    release_project: Path, capsys: pytest.CaptureFixture[str], surface: str
) -> None:
    flags = []
    if surface == "package":
        with release_project.open("a", encoding="utf-8") as stream:
            stream.write(
                '[tool.nab.packages."watchdog==6.0.0"]\nassume-universal-deps = true\n'
            )
    else:
        flags.append("--project-assume-universal-deps")

    assert _lock(release_project, *flags) == 0

    pylock = Pylock.from_dict(loads(capsys.readouterr().out))
    pylock.validate()
    packages = {str(package.name): package for package in pylock.packages}

    assert set(packages) == {"watchdog", "macos-dep", "linux-dep"}

    watchdog = packages["watchdog"]

    assert str(watchdog.version) == "6.0.0"
    assert watchdog.sdist is not None
    assert watchdog.sdist.filename == "watchdog-6.0.0.tar.gz"
    assert not watchdog.wheels

    for platform, dependency in (
        ("macos_arm64", "macos-dep"),
        ("linux_x86_64", "linux-dep"),
    ):
        target = ResolveTarget.for_declared(
            python_version="3.14", spec=PlatformSpec(platform)
        )
        selected = {
            str(package.name): artifact
            for package, artifact in pylock.select(
                environment=target.marker_env, tags=[Tag("py3", "none", "any")]
            )
        }
        assert set(selected) == {"watchdog", dependency}
        assert isinstance(selected["watchdog"], PackageSdist)


@pytest.mark.parametrize("wheel_state", ["absent", "unreadable"])
@pytest.mark.parametrize("mode", ["universal", "specific"])
@pytest.mark.parametrize("assumed", [False, True], ids=["strict", "already-assumed"])
def test_dynamic_sdist_hint_recovers_missing_wheel_metadata(
    dynamic_hint_project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    wheel_state: str,
    *,
    assumed: bool,
) -> None:
    built = parse_metadata(make_metadata("watchdog", "6.0.0"))
    ports: list[FakeFetchPort] = []
    wheels = (
        []
        if wheel_state == "absent"
        else [make_wheel("6.0.0", package="watchdog", tag="cp39-cp39-win_amd64")]
    )

    def coordinator(_transport: object, **_: object) -> nullcontext[FakeFetchPort]:
        port = make_provider_coordinator(
            [
                *wheels,
                make_sdist("6.0.0", package="watchdog", hashes=(("sha256", "a" * 64),)),
            ],
            package="watchdog",
            sdist_pkg_info=make_metadata("watchdog", "6.0.0", metadata_version="2.2")
            + "Dynamic: Requires-Dist\n",
            build_sdist=lambda *_args: built,
        )
        ports.append(port)
        return nullcontext(port)

    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", coordinator)
    project = dynamic_hint_project
    body = project.read_text(encoding="utf-8")
    if mode == "specific":
        body = body.replace('mode="universal"', 'mode="specific"').replace(
            '[tool.nab.matrix]\npython="==3.14"\nplatforms=["linux_x86_64"]\n', ""
        )
    if assumed:
        body = body.replace("[tool.nab]\n", "[tool.nab]\nassume-universal-deps=true\n")
    project.write_text(body, encoding="utf-8")
    args = ("lock", str(project), "--no-cache", "--output", "-")

    assert run(args) == 1
    failure = capsys.readouterr()
    assert failure.out == ""
    assert 'build-policy = "build-remote"' in failure.err
    assert ("assume-universal-deps = true" in failure.err) is (
        mode == "universal" and not assumed
    )
    assert ports[-1].calls_to("request_built_metadata") == []

    edits = re.findall(
        r"add a (\[\[tool.nab.package-rules\]\]) entry with"
        r" (match = \[.*?\]) and (assume-universal-deps = true|build-policy = \"build-remote\")",
        failure.err,
    )
    assert edits, failure.err
    if wheel_state == "unreadable" and mode == "universal" and not assumed:
        # Applying only the assertion cannot recover an unreadable wheel.
        project.write_text(body + "\n" + "\n".join(edits[0]) + "\n", encoding="utf-8")
        assert run(args) == 1
        assert 'build-policy = "build-remote"' in capsys.readouterr().err
        assert ports[-1].calls_to("request_built_metadata") == []

    project.write_text(
        body + "\n" + "\n\n".join("\n".join(edit) for edit in edits) + "\n",
        encoding="utf-8",
    )

    assert run(args) == 0
    pylock = Pylock.from_dict(loads(capsys.readouterr().out))
    pylock.validate()
    assert [str(package.version) for package in pylock.packages] == ["6.0.0"]
    assert pylock.packages[0].sdist is not None
    assert len(ports[-1].calls_to("request_built_metadata")) == 1
