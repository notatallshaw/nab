"""Yank admission through the CLI, local HTML index and generated wheel metadata."""

from __future__ import annotations

import hashlib
import zipfile
from typing import TYPE_CHECKING

import pytest
import tomli

from nab.cli import run

if TYPE_CHECKING:
    from pathlib import Path


def publish_wheel(
    index: Path, name: str, dependencies: tuple[str, ...], *, yanked: bool
) -> None:
    """Publish one valid wheel and its file-level withdrawal flag in a local index."""
    package = index / name
    package.mkdir(parents=True)
    filename = f"{name}-1-py3-none-any.whl"
    wheel = package / filename
    metadata = (
        f"Metadata-Version: 2.2\nName: {name}\nVersion: 1\n"
        + "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
        + "\n"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{name}-1.dist-info/METADATA", metadata)
        archive.writestr(
            f"{name}-1.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{name}-1.dist-info/RECORD", "")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    flag = ' data-yanked="incorrect dependency declaration"' if yanked else ""
    (package / "index.html").write_text(
        f'<a href="{filename}#sha256={digest}"{flag}>{filename}</a>\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(("dependency", "status"), [("dep==1", 0), ("dep>=1", 1)])
def test_cli_respects_transitive_pin_before_locking_a_withdrawn_wheel(
    hermetic_roots: Path,
    capsys: pytest.CaptureFixture[str],
    dependency: str,
    status: int,
) -> None:
    index = hermetic_roots / "index"
    publish_wheel(index, "app", (dependency,), yanked=False)
    publish_wheel(index, "dep", (), yanked=True)
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname="probe"\nversion="1"\ndependencies=["app", "dep>=1"]\n'
        f'[[tool.nab.indexes]]\nname="local"\nurl="{index.as_uri()}"\n',
        encoding="utf-8",
    )
    assert run(("lock", str(project), "--output", "-", "--no-cache")) == status
    captured = capsys.readouterr()
    if status == 0:
        lock = tomli.loads(captured.out)
        assert {
            package["name"]: package["version"] for package in lock["packages"]
        } == {"app": "1", "dep": "1"}
        assert "incorrect dependency declaration" in captured.err
        assert "Selected yanked" not in captured.out
    else:
        assert captured.out == ""
        assert "Selected yanked" not in captured.err
