"""Lock native PyPy wheels through the public CLI and select them for installation."""

from __future__ import annotations

import hashlib
import zipfile
from typing import TYPE_CHECKING

import pytest

from nab.cli import run
from nab_project.toml_io import loads
from nab_provider._vendor.packaging.pylock import Pylock, PylockSelectError
from nab_provider._vendor.packaging.tags import Tag
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

if TYPE_CHECKING:
    from pathlib import Path


def _indexed_wheel(directory: Path, tag: str) -> str:
    """Write a dependency-free wheel and its hashed local-index entry."""
    filename = f"demo-1.0-{tag}.whl"
    with zipfile.ZipFile(directory / filename, "w") as wheel:
        wheel.writestr(
            "demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n\n",
        )
        wheel.writestr("demo-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    path = directory / filename
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with (directory / "index.html").open("a", encoding="utf-8") as page:
        page.write(f'<a href="{filename}#sha256={digest}">{filename}</a>\n')
    return filename


def _lock_project(project: Path, *flags: str) -> Pylock:
    """Run an offline CLI lock, parse the emitted document, and validate it."""
    output = project.with_name("pylock.toml")
    assert (
        run(
            [
                "lock",
                str(project),
                "--offline",
                "--no-cache",
                "--format",
                "pylock",
                "--output",
                str(output),
                *flags,
            ]
        )
        == 0
    )
    lock = Pylock.from_dict(loads(output.read_text(encoding="utf-8")))
    lock.validate()
    return lock


@pytest.mark.parametrize("abi", [None, "pp80"])
@pytest.mark.parametrize("declaration", ["pyproject", "nab.toml", "cli"])
def test_lock_retains_installable_pypy_artifacts(
    tmp_path: Path, abi: str | None, declaration: str
) -> None:
    """An open ABI retains both versions; an exact ABI retains only its wheels."""
    index = tmp_path / "wheels"
    index.mkdir()
    package_index = index / "demo"
    package_index.mkdir()
    filenames = {
        (platform, suffix): _indexed_wheel(
            package_index, f"pp311-pypy311_{suffix}-{platform}"
        )
        for platform in ("win_amd64", "macosx_11_0_arm64")
        for suffix in ("pp73", "pp80")
    }
    project = tmp_path / "pyproject.toml"
    knob = f', pypy-abi = "{abi}"' if abi else ""
    matrix = (
        '[tool.nab.matrix]\npython = "==3.11"\nimplementations = ["pypy"]\n'
        f'platforms = [{{ id = "windows_amd64"{knob} }}, {{ id = "macos_arm64"{knob} }}]\n'
    )
    project.write_text(
        '[project]\nname = "example"\nversion = "1.0"\ndependencies = ["demo==1.0"]\n'
        '[tool.nab]\nmode = "universal"\n'
        f'[[tool.nab.indexes]]\nname = "local"\nurl = "{index.as_uri()}"\n'
        + (matrix if declaration == "pyproject" else ""),
        encoding="utf-8",
    )
    if declaration == "nab.toml":
        (tmp_path / "nab.toml").write_text(
            matrix.replace("tool.nab.", ""), encoding="utf-8"
        )
    flags: list[str] = []
    if declaration == "cli":
        flags = [
            "--project-matrix-python",
            "==3.11",
            "--project-matrix-implementations",
            "pypy",
            "--project-matrix-platforms",
            "windows_amd64",
            *([f"pypy-abi={abi}"] if abi else []),
            "macos_arm64",
            *([f"pypy-abi={abi}"] if abi else []),
        ]
    lock = _lock_project(project, *flags)
    emitted = {
        wheel.name for package in lock.packages for wheel in package.wheels or ()
    }
    assert emitted == {
        filename
        for (_, suffix), filename in filenames.items()
        if abi is None or suffix == abi
    }

    for platform_id, platform_tag in (
        ("windows_amd64", "win_amd64"),
        ("macos_arm64", "macosx_11_0_arm64"),
    ):
        target = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec(platform_id), implementation="pypy"
        )
        for suffix in ("pp73", "pp80") if abi is None else (abi,):
            selected = list(
                lock.select(
                    environment=target.marker_env,
                    tags=[Tag("pp311", f"pypy311_{suffix}", platform_tag)],
                )
            )
            assert len(selected) == 1
            assert selected[0][1].name == filenames[(platform_tag, suffix)]

        if abi is not None:
            with pytest.raises(PylockSelectError):
                list(
                    lock.select(
                        environment=target.marker_env,
                        tags=[Tag("pp311", "pypy311_pp73", platform_tag)],
                    )
                )


def test_pypy_override_abi_keeps_cpython_artifacts(tmp_path: Path) -> None:
    """An exact ABI in PyPy's platform override leaves CPython targets intact."""
    index = tmp_path / "index"
    package_index = index / "demo"
    package_index.mkdir(parents=True)
    expected = set()
    for platform in ("win_amd64", "macosx_11_0_arm64"):
        expected.add(_indexed_wheel(package_index, f"cp311-cp311-{platform}"))
        expected.add(_indexed_wheel(package_index, f"pp311-pypy311_pp80-{platform}"))
        _indexed_wheel(package_index, f"pp311-pypy311_pp73-{platform}")

    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "example"\nversion = "1.0"\ndependencies = ["demo==1.0"]\n'
        '[tool.nab]\nmode = "universal"\n'
        f'[[tool.nab.indexes]]\nname = "local"\nurl = "{index.as_uri()}"\n'
        '[tool.nab.matrix]\npython = "==3.11"\n'
        'platforms = ["windows_amd64", "macos_arm64"]\n'
        '[[tool.nab.matrix.include]]\nimplementations = ["pypy"]\n'
        'platforms = [{ id = "windows_amd64", pypy-abi = "pp80" }, { id = "macos_arm64", pypy-abi = "pp80" }]\n',
        encoding="utf-8",
    )
    lock = _lock_project(project)
    assert {
        wheel.name for package in lock.packages for wheel in package.wheels or ()
    } == expected

    for platform_id, platform_tag in (
        ("windows_amd64", "win_amd64"),
        ("macos_arm64", "macosx_11_0_arm64"),
    ):
        for implementation, interpreter, abi in (
            ("cpython", "cp311", "cp311"),
            ("pypy", "pp311", "pypy311_pp80"),
        ):
            target = ResolveTarget.for_declared(
                python_version="3.11",
                spec=PlatformSpec(platform_id),
                implementation=implementation,
            )
            selected = list(
                lock.select(
                    environment=target.marker_env,
                    tags=[Tag(interpreter, abi, platform_tag)],
                )
            )
            assert len(selected) == 1
            assert (
                selected[0][1].name
                == f"demo-1.0-{interpreter}-{abi}-{platform_tag}.whl"
            )
