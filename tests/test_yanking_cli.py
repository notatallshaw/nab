"""Yank admission through the CLI, local HTML index and generated wheel metadata."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
import tomli

from nab.cli import run
from nab_provider.testing import make_metadata

from ._local_artifacts import make_wheel_archive

if TYPE_CHECKING:
    from pathlib import Path


def publish_wheel(
    index: Path, name: str, dependencies: tuple[str, ...], *, yanked: bool
) -> None:
    """Publish one valid wheel and its file-level withdrawal flag in a local index."""
    package = index / name
    package.mkdir(parents=True)
    filename = f"{name}-1-py3-none-any.whl"
    metadata = make_metadata(name, "1", *dependencies, metadata_version="2.2")
    payload = make_wheel_archive(name, "1", metadata, "py3-none-any")
    (package / filename).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    flag = ' data-yanked="incorrect dependency declaration"' if yanked else ""
    (package / "index.html").write_text(
        f'<a href="{filename}#sha256={digest}"{flag}>{filename}</a>\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("dependency", "requested", "status"),
    [
        ("dep==1", "dep>=1", 1),
        ("dep>=1", "dep>=1", 1),
        ("dep==1", "dep==1", 0),
        ("dep>=1", "dep==1", 0),
    ],
)
def test_cli_may_require_an_input_pin_for_a_transitive_yank(
    hermetic_roots: Path,
    capsys: pytest.CaptureFixture[str],
    dependency: str,
    requested: str,
    status: int,
) -> None:
    index = hermetic_roots / "index"
    publish_wheel(index, "app", (dependency,), yanked=False)
    publish_wheel(index, "dep", (), yanked=True)
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        f'[project]\nname="probe"\nversion="1"\ndependencies=["app", "{requested}"]\n'
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
