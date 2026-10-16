"""CLI configuration, workspace integration and disabling of development sources."""

import hashlib
import zipfile
from pathlib import Path
from typing import Any

import pytest
import tomli

from nab._lock import _LockRun
from nab.cli import main
from nab.config.model import read_pyproject_config
from nab_provider._vendor.packaging.markers import Marker
from nab_provider.errors import ConfigError
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget


@pytest.mark.parametrize("key", ["local-sources", "vcs-sources", "archive-sources"])
@pytest.mark.parametrize("surface", ["pyproject", "project-file"])
def test_removed_source_options_are_rejected(
    tmp_path: Path, key: str, surface: str
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname="app"\nversion="1"\n')
    if surface == "pyproject":
        path.write_text(path.read_text() + f"[tool.nab]\n{key}=[]\n")
    else:
        (tmp_path / "nab.toml").write_text(f"{key}=[]\n")

    with pytest.raises(ConfigError, match=key):
        read_pyproject_config(path)


@pytest.mark.parametrize("invocation_directory", ["root", "app"])
def test_workspace_omission_keeps_external_source_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invocation_directory: str
) -> None:
    """Native members stay discoverable and omittable alongside standard sources."""
    (tmp_path / "pyproject.toml").write_text("""[tool.nab.workspace]
members=["core"]
[develop.sources]
core={path="core"}
""")
    for directory, document in {
        "core": '[project]\nname="core"\nversion="1"\ndependencies=["leaf>=2"]\n',
        "leaf": '[project]\nname="leaf"\nversion="2"\n',
        "app": """[project]
name="app"
version="1"
dependencies=["core"]
[develop.sources]
leaf={path="../leaf"}
""",
    }.items():
        project = tmp_path / directory
        project.mkdir()
        (project / "pyproject.toml").write_text(document)

    app = tmp_path / "app"
    monkeypatch.chdir(tmp_path if invocation_directory == "root" else app)
    output = tmp_path / "pylock.toml"
    main(
        [
            "lock",
            str(app / "pyproject.toml"),
            "--no-emit-workspace",
            "--offline",
            "--no-cache",
            "--output",
            str(output),
        ]
    )
    packages = tomli.loads(output.read_text())["packages"]
    assert [package["name"] for package in packages] == ["leaf"]
    assert packages[0]["directory"]["path"] == "leaf"


def test_no_sources_bypasses_source_validation(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname="root"\nversion="1"\ndependencies=[]\n[develop.sources]\nb={}\n'
    )
    config = read_pyproject_config(path, use_sources=False)
    assert config.develop_sources == ()
    output = tmp_path / "pylock.toml"
    main(
        [
            "lock",
            str(path),
            "--no-sources",
            "--offline",
            "--no-cache",
            "--output",
            str(output),
        ]
    )
    assert output.is_file()


def test_locked_refresh_command_retains_no_sources() -> None:
    run = _LockRun(
        path=Path("pyproject.toml"),
        output=None,
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
        sources=False,
    )
    assert run.refresh_command() == "nab lock --no-sources"


def test_conflict_base_pass_keeps_common_extra_source(tmp_path: Path) -> None:
    """A common extra keeps its source when exclusive selections fork the lock."""
    for directory, name, version in [
        ("one", "b", "1"),
        ("two", "b", "2"),
        ("common", "c", "1"),
    ]:
        target = tmp_path / directory
        target.mkdir()
        (target / "pyproject.toml").write_text(
            f'[project]\nname="{name}"\nversion="{version}"\n'
        )
    path = tmp_path / "pyproject.toml"
    path.write_text("""[project]
name="root"
version="1"
[project.optional-dependencies]
one=["b"]
two=["b"]
common=["c"]
[develop.sources]
b=[{path="one",extra="one"},{path="two",extra="two"}]
c={path="common",extra="common"}
[tool.nab]
conflicts=[[{extra="one"},{extra="two"}]]
""")
    output = tmp_path / "pylock.toml"
    main(
        [
            "lock",
            str(path),
            "--extras",
            "one",
            "two",
            "common",
            "--offline",
            "--no-cache",
            "--output",
            str(output),
        ]
    )
    lock = tomli.loads(output.read_text())
    assert [package["name"] for package in lock["packages"]] == ["b", "b", "c"]


@pytest.fixture
def source_workspace(tmp_path: Path) -> Path:
    """An implicit member and an alternative checkout share one external dependency."""
    for directory, document in {
        "core": '[project]\nname="core"\nversion="1"\ndependencies=["leaf>=1"]\n',
        "core-fix": '[project]\nname="core"\nversion="2"\ndependencies=["leaf>=2"]\n',
        "leaf": '[project]\nname="leaf"\nversion="2"\n',
    }.items():
        project = tmp_path / directory
        project.mkdir()
        (project / "pyproject.toml").write_text(document)
    path = tmp_path / "pyproject.toml"
    path.write_text("""[project]
name="app"
version="1"
dependencies=["core>=1"]
[tool.nab.workspace]
members=["core"]
[develop.sources]
leaf={path="leaf"}
""")
    return path


def _lock_workspace(path: Path, *options: str) -> dict[str, Any]:
    """Lock a local fixture through the CLI and return its parsed lock."""
    output = path.parent / "pylock.toml"
    main(
        [
            "lock",
            str(path),
            "--offline",
            "--no-cache",
            "--output",
            str(output),
            *options,
        ]
    )
    return tomli.loads(output.read_text())


@pytest.mark.parametrize("omit", [False, True])
@pytest.mark.parametrize("source", ['{path="core"}', '{path=".",subdirectory="core"}'])
def test_standard_member_source_controls_editability_and_omission(
    source_workspace: Path, source: str, omit: bool
) -> None:
    path = source_workspace
    path.write_text(path.read_text() + f"core={source}\n")
    lock = _lock_workspace(path, *(["--no-emit-workspace"] if omit else []))
    packages = {package["name"]: package for package in lock["packages"]}
    assert set(packages) == ({"leaf"} if omit else {"core", "leaf"})
    assert "marker" not in packages["leaf"]
    if not omit:
        assert packages["core"]["directory"]["editable"] is False


@pytest.mark.parametrize(
    "source", ['{path="core-fix"}', '{path=".",subdirectory="core-fix"}']
)
def test_standard_member_override_is_retained_with_workspace_omission(
    source_workspace: Path, source: str
) -> None:
    path = source_workspace
    document = path.read_text().replace('"core>=1"', '"core>=2"')
    path.write_text(document + f"core={source}\n")
    lock = _lock_workspace(path, "--no-emit-workspace")
    packages = {package["name"]: package for package in lock["packages"]}
    assert set(packages) == {"core", "leaf"}
    directory = packages["core"]["directory"]
    assert (
        path.parent / directory["path"] / directory.get("subdirectory", ".")
    ).resolve() == path.parent / "core-fix"
    assert directory["editable"] is False
    assert {child["name"] for child in packages["core"]["dependencies"]} == {"leaf"}


def test_inactive_member_source_uses_workspace_fallback(source_workspace: Path) -> None:
    path = source_workspace
    path.write_text(
        path.read_text() + 'core={path="missing",marker="python_version < \'3.0\'"}\n'
    )
    lock = _lock_workspace(path)
    packages = {package["name"]: package for package in lock["packages"]}
    assert packages["core"]["directory"] == {"path": "core", "editable": True}


def test_no_sources_keeps_workspace_fallback(source_workspace: Path) -> None:
    path = source_workspace
    (path.parent / "core" / "pyproject.toml").write_text(
        '[project]\nname="core"\nversion="1"\n'
    )
    path.write_text(path.read_text() + 'core={path="core-fix"}\n')
    lock = _lock_workspace(path, "--no-sources")
    (package,) = lock["packages"]
    assert package["name"] == "core"
    assert package["directory"] == {"path": "core", "editable": True}


def test_no_workspace_discovery_keeps_standard_sources(source_workspace: Path) -> None:
    path = source_workspace
    path.write_text(path.read_text() + 'core={path="core",editable=false}\n')
    lock = _lock_workspace(path, "--no-workspace-discovery", "--no-emit-workspace")
    packages = {package["name"]: package for package in lock["packages"]}
    assert set(packages) == {"core", "leaf"}
    assert packages["core"]["directory"]["editable"] is False


def test_member_source_can_change_by_target_without_losing_external_pin(
    source_workspace: Path,
) -> None:
    path = source_workspace
    path.write_text(
        path.read_text()
        + """core={path="core-fix",marker="python_version >= '3.13'"}
[tool.nab]
mode="universal"
[tool.nab.matrix]
python=">=3.12,<3.14"
platforms=["linux_x86_64"]
"""
    )
    lock = _lock_workspace(path, "--no-emit-workspace")
    packages = {package["name"]: package for package in lock["packages"]}
    assert set(packages) == {"core", "leaf"}
    assert packages["core"]["directory"]["path"] == "core-fix"
    marker = Marker(packages["core"]["marker"])
    for version, included in [("3.12", False), ("3.13", True)]:
        target = ResolveTarget.for_declared(
            python_version=version, spec=PlatformSpec("linux_x86_64")
        )
        assert marker.evaluate(target.marker_env) is included
    assert "marker" not in packages["leaf"]


def test_member_override_lock_can_be_checked(source_workspace: Path) -> None:
    path = source_workspace
    path.write_text(path.read_text() + 'core={path="core-fix"}\n')
    first = _lock_workspace(path, "--no-emit-workspace")
    assert _lock_workspace(path, "--no-emit-workspace", "--locked") == first


def test_direct_wheel_replacing_member_is_not_omitted(source_workspace: Path) -> None:
    path = source_workspace
    artifact = path.parent / "core-2-py3-none-any.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "core-2.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: core\nVersion: 2\nRequires-Dist: leaf>=2\n",
        )
    path.write_text(
        path.read_text().replace(
            '"core>=1"',
            f'"core @ {artifact.as_uri()}#sha256={hashlib.sha256(artifact.read_bytes()).hexdigest()}"',
        )
    )
    path.write_text(path.read_text())
    lock = _lock_workspace(path, "--no-emit-workspace")
    packages = {package["name"]: package for package in lock["packages"]}
    assert set(packages) == {"core", "leaf"}
    assert packages["core"]["archive"]["url"] == artifact.as_uri()


@pytest.mark.parametrize("use_extra", [False, True])
def test_extra_source_overrides_member_only_when_selected(
    source_workspace: Path, use_extra: bool
) -> None:
    path = source_workspace
    path.write_text(
        path.read_text()
        + """core={path="core-fix",extra="fork"}
[project.optional-dependencies]
fork=["core>=2"]
"""
    )
    options = ["--no-emit-workspace"]
    if use_extra:
        options.extend(["--extras", "fork"])
    lock = _lock_workspace(path, *options)
    packages = {package["name"]: package for package in lock["packages"]}
    assert set(packages) == ({"core", "leaf"} if use_extra else {"leaf"})
    if use_extra:
        assert packages["core"]["directory"]["path"] == "core-fix"


def test_invalid_standard_source_does_not_fall_back_to_member(
    source_workspace: Path,
) -> None:
    path = source_workspace
    path.write_text(path.read_text() + 'core={path="missing"}\n')
    with pytest.raises(SystemExit, match="1"):
        _lock_workspace(path)
    assert not (path.parent / "pylock.toml").exists()
