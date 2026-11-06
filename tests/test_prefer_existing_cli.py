"""Previous-lock CLI policy uses current provider facts and preserves defaults."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from shlex import split

import pytest

from nab import _lock
from nab.cli import run
from nab_project import toml_io
from nab_provider.records import DistFile, IndexConfig
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_wheel,
)
from nab_provider.yanking import append_yanked_file


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

    assert registry.command("--prefer-existing") == 0

    assert registry.versions() == {"a": "1", "b": "1"}
    assert registry.ports[-1].calls_to("request_metadata")


def test_default_still_selects_new_highest_releases(registry: Registry) -> None:
    assert registry.command() == 0
    registry.add_release()

    assert registry.command() == 0

    assert registry.versions() == {"a": "2", "b": "2"}


def test_missing_prior_uses_a_fresh_resolution(registry: Registry) -> None:
    registry.add_release()

    assert registry.command("--prefer-existing") == 0

    assert registry.versions() == {"a": "2", "b": "2"}


@pytest.mark.parametrize("contents", ["not valid TOML [", 'lock-version="999"'])
def test_invalid_prior_uses_a_fresh_resolution(
    registry: Registry, contents: str
) -> None:
    registry.output.write_text(contents)
    registry.add_release()

    assert registry.command("--prefer-existing") == 0

    assert registry.versions() == {"a": "2", "b": "2"}


def test_current_artifact_hashes_replace_previous_records(registry: Registry) -> None:
    assert registry.command() == 0
    registry.digest = "d" * 64

    assert registry.command("--prefer-existing") == 0

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

    assert registry.command("--prefer-existing") == 0

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

    assert registry.command("--prefer-existing") == 0


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
        prefer_existing=True,
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
    assert registry.command("--prefer-existing", *arguments) == 1
    assert not registry.output.exists()
