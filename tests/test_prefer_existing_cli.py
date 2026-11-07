"""Previous-lock CLI policy uses current provider facts and preserves defaults."""

from __future__ import annotations

import os
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from shlex import split
from typing import TYPE_CHECKING

import pytest

from nab import _lock
from nab.cli import run
from nab_project import toml_io, toml_writer
from nab_project.lock_policy import LockPolicy
from nab_provider.records import DistFile, IndexConfig
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_wheel,
)
from nab_provider.yanking import append_yanked_file

if TYPE_CHECKING:
    from nab_provider.target import ResolveTarget


class Registry:
    """Publish new facts through a fresh real in-memory fetch port per command."""

    def __init__(self, directory: Path) -> None:
        self.project = directory / "pyproject.toml"
        self.output = directory / "pylock.toml"
        self.declarations = {"a": {"1": ("b==1",)}, "b": {"1": ()}}
        self.requires_python: dict[tuple[str, str], str] = {}
        self.withdrawn: set[tuple[str, str]] = set()
        self.domain = "https://current.example"
        self.digest = "b" * 64
        self.upload_time: str | None = None
        self.ports: list[FakeFetchPort] = []
        self.project.write_text(
            '[project]\nname="example"\nversion="1"\nrequires-python=">=3.12,<3.13"\n'
            'dependencies=["a"]\n[tool.nab]\nbuild-policy="never"\ndecision-order="stable"\n'
            '[tool.nab.matrix]\npython=">=3.12,<3.13"\nplatforms=["linux_x86_64","windows_amd64"]\n'
        )

    def coordinator(
        self, _transport: object, **_kwargs: object
    ) -> nullcontext[FakeFetchPort]:
        listings: dict[str, list[DistFile]] = {}
        metadata: dict[str, str] = {}
        for package, versions in self.declarations.items():
            files: list[DistFile] = []
            for version, dependencies in versions.items():
                filename = f"{package.replace('-', '_')}-{version}-py3-none-any.whl"
                wheel = make_wheel(
                    version,
                    package=package.replace("-", "_"),
                    url=f"{self.domain}/{filename}",
                    hashes=(("sha256", self.digest),),
                    upload_time=self.upload_time,
                )
                assert wheel.metadata_url is not None
                metadata[wheel.metadata_url] = make_metadata(
                    package,
                    version,
                    *dependencies,
                    requires_python=self.requires_python.get((package, version)),
                )
                if (package, version) in self.withdrawn:
                    files = append_yanked_file(files, wheel, reason="withdrawn")
                else:
                    files.append(wheel)
            listings[package] = files
        port = make_coordinator(listings=listings, metadata_by_url=metadata)
        if self.domain != "https://current.example":
            port.indexes = [IndexConfig("changed", self.domain + "/simple/")]
            for package in listings:
                port.index.store_listing_index(package, "changed")
        self.ports.append(port)
        return nullcontext(port)

    def command(self, *arguments: str) -> int:
        return run(
            (
                "--no-progress",
                "lock",
                str(self.project),
                "--output",
                str(self.output),
                *arguments,
            )
        )

    def versions(self) -> dict[str, str]:
        return {
            package["name"]: package["version"]
            for package in toml_io.load_path(self.output)["packages"]
        }

    def add_release(self) -> None:
        self.declarations["a"]["2"] = ("b==2",)
        self.declarations["b"]["2"] = ()


@pytest.fixture
def registry(hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch) -> Registry:
    value = Registry(hermetic_roots)
    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", value.coordinator)
    return value


def test_opt_in_keeps_valid_prior_choices_and_requests_current_metadata(
    registry: Registry,
) -> None:
    assert registry.command() == 0
    registry.add_release()

    assert registry.command("--lock-policy", "prefer-existing") == 0

    assert registry.versions() == {"a": "1", "b": "1"}
    assert registry.ports[-1].calls_to("request_metadata")


def test_default_still_selects_new_highest_releases(registry: Registry) -> None:
    assert registry.command() == 0
    registry.add_release()

    assert registry.command() == 0

    assert registry.versions() == {"a": "2", "b": "2"}


def test_missing_prior_uses_a_fresh_resolution(registry: Registry) -> None:
    registry.add_release()

    assert registry.command("--lock-policy", "prefer-existing") == 0

    assert registry.versions() == {"a": "2", "b": "2"}


@pytest.mark.parametrize("contents", ["not valid TOML [", 'lock-version="999"'])
def test_invalid_prior_uses_a_fresh_resolution(
    registry: Registry, contents: str
) -> None:
    registry.output.write_text(contents)
    registry.add_release()

    assert registry.command("--lock-policy", "prefer-existing") == 0

    assert registry.versions() == {"a": "2", "b": "2"}


def test_current_artifact_hashes_replace_previous_records(registry: Registry) -> None:
    assert registry.command() == 0
    registry.digest = "d" * 64

    assert registry.command("--lock-policy", "prefer-existing") == 0

    assert registry.versions() == {"a": "1", "b": "1"}
    for package in toml_io.load_path(registry.output)["packages"]:
        assert all(wheel["hashes"]["sha256"] == "d" * 64 for wheel in package["wheels"])


@pytest.mark.parametrize(
    "change", ["metadata", "python", "removed", "yanked", "source", "new-package"]
)
def test_current_changes_repair_the_model(registry: Registry, change: str) -> None:
    assert registry.command() == 0
    registry.add_release()
    expected = {"a": "1", "b": "2"}
    if change in {"metadata", "source"}:
        registry.declarations["a"]["1"] = ("b==2",)
        if change == "source":
            registry.domain = "https://changed.example"
            registry.digest = "c" * 64
    elif change == "python":
        registry.requires_python[("a", "1")] = ">=3.13"
        expected["a"] = "2"
    elif change == "removed":
        del registry.declarations["a"]["1"]
        expected["a"] = "2"
    elif change == "yanked":
        registry.withdrawn.add(("a", "1"))
        expected["a"] = "2"
    else:
        registry.declarations["a"]["1"] = ("new-package",)
        registry.declarations["new-package"] = {"1": ()}
        expected = {"a": "1", "new-package": "1"}

    assert registry.command("--lock-policy", "prefer-existing") == 0

    assert registry.versions() == expected
    if change == "source":
        for package in toml_io.load_path(registry.output)["packages"]:
            assert all(
                wheel["url"].startswith(registry.domain) for wheel in package["wheels"]
            )


def test_validated_prior_is_shared_with_the_write_summary(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert registry.command() == 0

    def unexpected_read(_path: Path) -> None:
        pytest.fail("validated prior was reparsed for the write summary")

    monkeypatch.setattr(_lock, "read_lockfile_packages", unexpected_read)

    assert registry.command("--lock-policy", "prefer-existing") == 0


def test_refresh_command_keeps_the_previous_lock_policy(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(registry.project.parent)
    assert registry.command() == 0
    registry.add_release()
    invocation = _lock._LockRun(
        path=Path("pyproject.toml"),
        output=Path("pylock.toml"),
        groups=(),
        all_groups=False,
        extras=(),
        all_extras=False,
        offline=None,
        build_requirements=False,
        workspace_discovery=True,
        no_emit_workspace=False,
        cli_overrides={},
        upgrade=False,
        lock_policy=LockPolicy.PREFER_EXISTING,
    )

    assert run(split(invocation.refresh_command())[1:]) == 0

    assert registry.versions() == {"a": "1", "b": "1"}


@pytest.mark.parametrize(
    "arguments",
    [
        ("--format", "requirements"),
        ("--format", "requirements", "--output", "pins-{platform_id}.txt"),
        ("--output", "-"),
        ("--locked",),
        ("--project-resolution", "lowest"),
    ],
)
def test_inapplicable_modes_are_refused(
    registry: Registry, arguments: tuple[str, ...]
) -> None:
    assert registry.command("--lock-policy", "prefer-existing", *arguments) == 1
    assert not registry.output.exists()


@pytest.mark.parametrize("change", ["release", "metadata", "yank", "deletion", "hash"])
def test_reuse_keeps_a_valid_file_without_remote_discovery(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    assert registry.command() == 0
    before = registry.output.read_bytes()
    identity = registry.output.stat()
    ports = len(registry.ports)
    if change == "release":
        registry.add_release()
    elif change == "metadata":
        registry.declarations["a"]["1"] = ("b==2",)
    elif change == "yank":
        registry.withdrawn.add(("a", "1"))
    elif change == "deletion":
        del registry.declarations["a"]["1"]
    else:
        registry.digest = "c" * 64

    def unexpected_transport(_backend: str) -> None:
        pytest.fail("saved-lock reuse created an index transport")

    monkeypatch.setattr(_lock, "_make_resolve_transport", unexpected_transport)
    registry.output.chmod(0o444)
    try:
        assert registry.command("--lock-policy", "reuse") == 0
    finally:
        registry.output.chmod(identity.st_mode)
    after = registry.output.stat()
    assert registry.output.read_bytes() == before
    assert (after.st_ino, after.st_mtime_ns) == (identity.st_ino, identity.st_mtime_ns)
    assert len(registry.ports) == ports


def test_reuse_accepts_compatible_bounds_without_rewriting(registry: Registry) -> None:
    assert registry.command() == 0
    before = registry.output.read_bytes()
    registry.project.write_text(
        registry.project.read_text().replace(
            'dependencies=["a"]', 'dependencies=["a>=1"]'
        )
    )
    ports = len(registry.ports)

    assert registry.command("--lock-policy", "reuse") == 0

    assert registry.output.read_bytes() == before
    assert len(registry.ports) == ports


def test_reuse_miss_uses_current_preferred_resolution(registry: Registry) -> None:
    assert registry.command() == 0
    registry.add_release()
    registry.declarations["a"]["1"] = ("b==2",)
    registry.declarations["new"] = {"1": ()}
    registry.project.write_text(
        registry.project.read_text().replace(
            'dependencies=["a"]', 'dependencies=["a", "new"]'
        )
    )
    ports = len(registry.ports)

    assert registry.command("--lock-policy", "reuse") == 0

    assert len(registry.ports) == ports + 1
    assert registry.versions() == {"a": "1", "b": "2", "new": "1"}
    assert registry.ports[-1].calls_to("request_metadata")


@pytest.mark.parametrize("contents", [None, "not valid TOML [", 'lock-version="999"'])
def test_reuse_without_a_supported_lock_resolves(
    registry: Registry, contents: str | None
) -> None:
    if contents is not None:
        registry.output.write_text(contents)
    registry.add_release()

    assert registry.command("--lock-policy", "reuse") == 0

    assert registry.versions() == {"a": "2", "b": "2"}


@pytest.mark.parametrize("policy", ["prefer-existing", "reuse"])
@pytest.mark.parametrize(
    "arguments",
    [
        ("--project-resolution", "lowest"),
        ("--locked",),
        ("--output", "-"),
        ("--format", "requirements"),
    ],
)
def test_nondefault_policy_restrictions_precede_valid_hits(
    registry: Registry, policy: str, arguments: tuple[str, ...]
) -> None:
    assert registry.command() == 0
    before = registry.output.read_bytes()
    ports = len(registry.ports)

    assert registry.command("--lock-policy", policy, *arguments) == 1

    assert registry.output.read_bytes() == before
    assert len(registry.ports) == ports


def test_reuse_refuses_reproduction(registry: Registry) -> None:
    assert registry.command() == 0
    assert registry.command("--lock-policy", "reuse", "--reproduce") == 1


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX arbitrary-byte file source identities"
)
@pytest.mark.parametrize("second_bytes", [b"index-\xfe", b"index-\xef\xbf\xbd"])
def test_reuse_declines_the_existing_file_index_fingerprint_collision(
    registry: Registry,
    second_bytes: bytes,
) -> None:
    first = registry.project.parent / os.fsdecode(b"index-\xff")
    second = registry.project.parent / os.fsdecode(second_bytes)
    text = (
        registry.project.read_text()
        + f'\n[[tool.nab.indexes]]\nname="local"\nurl="{first.as_uri()}"\n'
    )
    registry.project.write_text(text)
    assert registry.command() == 0
    ports = len(registry.ports)
    registry.project.write_text(text.replace(first.as_uri(), second.as_uri()))
    registry.digest = "c" * 64

    assert registry.command("--lock-policy", "reuse") == 0

    assert len(registry.ports) == ports + 1
    for package in toml_io.load_path(registry.output)["packages"]:
        assert all(wheel["hashes"]["sha256"] == "c" * 64 for wheel in package["wheels"])


@pytest.mark.parametrize("policy", ["resolve", "prefer-existing", "reuse"])
def test_dynamic_root_dependencies_never_use_a_hidden_backend(
    registry: Registry, policy: str
) -> None:
    assert registry.command() == 0
    marker = registry.project.parent / "backend-ran"
    backend = registry.project.parent / "backend.py"
    backend.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('backend imported')\n"
        "def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):\n"
        "    info = Path(metadata_directory) / 'example-1.dist-info'\n"
        "    info.mkdir()\n"
        "    (info / 'METADATA').write_text('Metadata-Version: 2.4\\nName: example\\nVersion: 1\\nRequires-Dist: a\\n')\n"
        "    return info.name\n"
    )
    text = registry.project.read_text().replace(
        'dependencies=["a"]', 'dynamic=["dependencies"]'
    )
    text = text.replace(
        'build-policy="never"',
        'build-policy="build-remote"\nassume-universal-deps=true',
    )
    registry.project.write_text(
        text
        + '\n[build-system]\nrequires=[]\nbuild-backend="backend"\nbackend-path=["."]\n'
    )
    before = registry.output.read_bytes()

    assert registry.command("--lock-policy", policy) == 1

    assert not marker.exists()
    assert registry.output.read_bytes() == before


@pytest.mark.parametrize("cutoff", [None, "2025-01-01T00:00:00Z", "P7D"])
def test_upgrade_uses_live_resolution_and_reanchors_even_with_reuse(
    registry: Registry, cutoff: str | None
) -> None:
    registry.upload_time = "2000-01-01T00:00:00Z"
    if cutoff is not None:
        registry.project.write_text(
            registry.project.read_text().replace(
                'decision-order="stable"',
                f'decision-order="stable"\nuploaded-prior-to="{cutoff}"',
            )
        )
    assert registry.command() == 0
    document = toml_io.load_path(registry.output)
    old_anchor = datetime(2000, 1, 1, tzinfo=timezone.utc)
    document["tool"]["nab"]["created-at"] = old_anchor
    registry.output.write_text(toml_writer.dumps(document))
    ports = len(registry.ports)

    assert registry.command("--lock-policy", "reuse", "--upgrade") == 0

    assert len(registry.ports) == ports + 1
    assert toml_io.load_path(registry.output)["tool"]["nab"]["created-at"] != old_anchor


@pytest.mark.parametrize("change", ["metadata-assumption", "http-index"])
def test_reuse_checks_current_declared_policy_and_source(
    registry: Registry, change: str
) -> None:
    assert registry.command() == 0
    ports = len(registry.ports)
    arguments: tuple[str, ...] = ()
    if change == "metadata-assumption":
        arguments = ("--project-assume-consistent-metadata",)
    else:
        registry.domain = "https://changed.example"
        registry.digest = "c" * 64
        registry.project.write_text(
            registry.project.read_text()
            + '\n[[tool.nab.indexes]]\nname="changed"\nurl="https://changed.example/simple/"\n'
        )

    assert registry.command("--lock-policy", "reuse", *arguments) == 0

    assert len(registry.ports) == ports + 1
    assert registry.ports[-1].calls_to("request_metadata")
    if change == "http-index":
        for package in toml_io.load_path(registry.output)["packages"]:
            assert all(
                wheel["url"].startswith(registry.domain) for wheel in package["wheels"]
            )


def test_reuse_shares_the_snapshot_target_plan(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert registry.command() == 0
    original = _lock.plan_targets
    calls = 0

    def counted(config: _lock.NabProjectConfig) -> tuple[ResolveTarget, ...]:
        nonlocal calls
        calls += 1
        return original(config)

    monkeypatch.setattr(_lock, "plan_targets", counted)

    assert registry.command("--lock-policy", "reuse") == 0

    assert calls == 1
