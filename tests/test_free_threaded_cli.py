"""Resolve both CPython wheel ABIs through the public CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nab.cli import run
from nab_project.toml_io import loads
from nab_provider._vendor.packaging.pylock import Pylock
from nab_provider._vendor.packaging.tags import Tag
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

from ._local_artifacts import make_wheel_archive, publish_artifacts

if TYPE_CHECKING:
    from pathlib import Path


def _project(tmp_path: Path, *, declaration: str = "pyproject") -> Path:
    """Declare a local wheel index and an opt-in matrix on the requested surface."""
    project = tmp_path / "pyproject.toml"
    matrix = (
        '[tool.nab.matrix]\npython = "==3.14"\n'
        'platforms = ["linux_x86_64"]\ninclude-free-threaded = true\n'
    )
    project.write_text(
        '[project]\nname = "example"\nversion = "1.0"\ndependencies = []\n'
        '[dependency-groups]\ndev = ["demo"]\n'
        '[tool.nab]\ndist-policy = "wheel-only"\n'
        f'[[tool.nab.indexes]]\nname = "local"\nurl = "{(tmp_path / "index").as_uri()}"\n'
        + (matrix if declaration == "pyproject" else ""),
        encoding="utf-8",
    )
    if declaration == "nab.toml":
        (tmp_path / "nab.toml").write_text(
            matrix.replace("[tool.nab.matrix]", "[matrix]"), encoding="utf-8"
        )
    return project


def _publish(tmp_path: Path, version: str, abi: str, *dependencies: str) -> str:
    """Add one native wheel with target-specific dependency metadata."""
    tag = f"cp314-{abi}-manylinux_2_17_x86_64"
    filename = f"demo-{version}-{tag}.whl"
    metadata = f"Metadata-Version: 2.1\nName: demo\nVersion: {version}\n" + "".join(
        f"Requires-Dist: {dependency}\n" for dependency in dependencies
    )
    index = tmp_path / "index"
    existing = {path.name: path.read_bytes() for path in (index / "demo").glob("*.whl")}
    publish_artifacts(
        index,
        "demo",
        {**existing, filename: make_wheel_archive("demo", version, metadata, tag)},
    )
    return filename


def _lock(project: Path, *flags: str) -> int:
    return run(
        (
            "lock",
            str(project),
            "--all-groups",
            "--offline",
            "--no-cache",
            "--output",
            str(project.with_name("pylock.toml")),
            *flags,
        )
    )


@pytest.mark.parametrize("declaration", ["pyproject", "nab.toml", "cli"])
def test_one_lock_selects_both_wheel_abis(tmp_path: Path, declaration: str) -> None:
    regular = _publish(tmp_path, "1.0", "cp314")
    threaded = _publish(tmp_path, "1.0", "cp314t")
    project = _project(tmp_path, declaration=declaration)
    flags = (
        (
            "--project-matrix-python",
            "==3.14",
            "--project-matrix-platforms",
            "linux_x86_64",
            "--project-matrix-include-free-threaded",
        )
        if declaration == "cli"
        else ()
    )
    assert _lock(project, *flags) == 0

    document = loads(project.with_name("pylock.toml").read_text(encoding="utf-8"))
    lock = Pylock.from_dict(document)
    lock.validate()
    assert len(lock.packages) == 1
    assert {wheel.name for wheel in lock.packages[0].wheels or ()} == {
        regular,
        threaded,
    }
    target = ResolveTarget.for_declared(
        python_version="3.14", spec=PlatformSpec("linux_x86_64")
    )
    for abi, filename in (("cp314", regular), ("cp314t", threaded)):
        selected = list(
            lock.select(
                environment=target.marker_env,
                dependency_groups=["dev"],
                tags=[Tag("cp314", abi, "manylinux_2_17_x86_64")],
            )
        )
        assert len(selected) == 1
        assert selected[0][1].name == filename


def test_cli_can_disable_the_file_setting(tmp_path: Path) -> None:
    regular = _publish(tmp_path, "1.0", "cp314")
    project = _project(tmp_path)
    assert _lock(project, "--no-project-matrix-include-free-threaded") == 0
    document = loads(project.with_name("pylock.toml").read_text(encoding="utf-8"))
    assert [wheel["name"] for wheel in document["packages"][0]["wheels"]] == [regular]


def test_incompatible_abi_pins_leave_the_existing_lock_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(tmp_path, "2.0", "cp314")
    _publish(tmp_path, "1.0", "cp314t")
    project = _project(tmp_path)
    output = project.with_name("pylock.toml")
    output.write_text("existing lock\n", encoding="utf-8")
    assert _lock(project) == 1
    assert output.read_text(encoding="utf-8") == "existing lock\n"
    error = capsys.readouterr().err
    assert "demo" in error
    assert "2.0" in error
    assert "1.0" in error


def test_config_reports_the_selected_builds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    assert run(("config", "get", "matrix", "--path", str(project))) == 0
    assert "include-free-threaded=true" in capsys.readouterr().out


def test_a_missing_free_threaded_wheel_refuses_the_lock(tmp_path: Path) -> None:
    _publish(tmp_path, "1.0", "cp314")
    project = _project(tmp_path)
    assert _lock(project) == 1
    assert not project.with_name("pylock.toml").exists()


def test_build_specific_dependencies_refuse_a_shared_lock(tmp_path: Path) -> None:
    _publish(tmp_path, "1.0", "cp314", "ordinary-dep")
    _publish(tmp_path, "1.0", "cp314t", "threaded-dep")
    for name in ("ordinary-dep", "threaded-dep"):
        wheel_name = name.replace("-", "_")
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
        publish_artifacts(
            tmp_path / "index",
            name,
            {
                f"{wheel_name}-1.0-py3-none-any.whl": make_wheel_archive(
                    wheel_name, "1.0", metadata, "py3-none-any"
                )
            },
        )
    project = _project(tmp_path)
    assert _lock(project) == 1
    assert not project.with_name("pylock.toml").exists()


@pytest.mark.parametrize("change", ["none", "artifact", "targets"])
def test_universal_locked_checks_content_without_rewriting(
    tmp_path: Path, change: str
) -> None:
    ordinary = _publish(tmp_path, "1.0", "cp314")
    _publish(tmp_path, "1.0", "cp314t")
    project = _project(tmp_path)
    project.write_text(
        project.read_text().replace("[tool.nab]\n", '[tool.nab]\nmode = "universal"\n')
    )
    assert _lock(project) == 0
    output = project.with_name("pylock.toml")
    before = output.read_bytes()
    if change == "artifact":
        directory = tmp_path / "index" / "demo"
        artifacts = {path.name: path.read_bytes() for path in directory.glob("*.whl")}
        artifacts[ordinary] = make_wheel_archive(
            "demo",
            "1.0",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\nSummary: changed artifact\n",
            "cp314-cp314-manylinux_2_17_x86_64",
        )
        publish_artifacts(tmp_path / "index", "demo", artifacts)
    elif change == "targets":
        project.write_text(
            project.read_text().replace(
                "include-free-threaded = true", "include-free-threaded = false"
            )
        )

    assert _lock(project, "--locked") == (0 if change == "none" else 1)
    assert output.read_bytes() == before


def test_universal_locked_does_not_apply_another_environments_constraint(
    tmp_path: Path,
) -> None:
    publish_artifacts(
        tmp_path / "index",
        "demo",
        {
            "demo-1.0-py3-none-any.whl": make_wheel_archive(
                "demo",
                "1.0",
                "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
                "py3-none-any",
            )
        },
    )
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "example"\nversion = "1.0"\n'
        "dependencies = [\"demo==1.0; sys_platform == 'win32'\"]\n"
        '[tool.nab]\nmode = "universal"\n'
        "constraints = [\"demo>=2; sys_platform == 'linux'\"]\n"
        f'[[tool.nab.indexes]]\nname = "local"\nurl = "{(tmp_path / "index").as_uri()}"\n'
        '[tool.nab.matrix]\npython = "==3.14"\nplatforms = ["linux_x86_64", "windows_amd64"]\n',
        encoding="utf-8",
    )
    assert _lock(project) == 0
    before = project.with_name("pylock.toml").read_bytes()
    assert _lock(project, "--locked") == 0
    assert project.with_name("pylock.toml").read_bytes() == before
