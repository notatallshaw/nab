"""Real ZIP rejection hints remain quiet until relevant terminal failures."""

from __future__ import annotations

import io
import zipfile
from typing import TYPE_CHECKING

import pytest

from nab.cli import run
from nab_provider.testing import make_metadata

from ._local_artifacts import make_wheel_archive, publish_artifacts

if TYPE_CHECKING:
    from pathlib import Path


def _zip_sdist(version: str) -> bytes:
    """Return static metadata for a release available only as a ZIP sdist."""
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(
            f"pkg-{version}/PKG-INFO",
            make_metadata("pkg", version, metadata_version="2.2"),
        )
    return content.getvalue()


@pytest.mark.parametrize("outcome", ["fallback", "unrelated", "zip-failure"])
def test_wide_resolve_emits_one_relevant_zip_action_per_command(
    hermetic_roots: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    index = hermetic_roots / "index"
    releases = {
        f"pkg-{version}.zip": _zip_sdist(version) for version in ("2", "3", "4")
    }
    if outcome != "zip-failure":
        releases["pkg-1-py3-none-any.whl"] = make_wheel_archive(
            "pkg", "1", make_metadata("pkg", "1"), "py3-none-any"
        )
    publish_artifacts(index, "pkg", releases)
    for name, dependencies in (("application", ("pkg>=1",)), ("side", ())):
        publish_artifacts(
            index,
            name,
            {
                f"{name}-1-py3-none-any.whl": make_wheel_archive(
                    name, "1", make_metadata(name, "1", *dependencies), "py3-none-any"
                )
            },
        )
    requirements = '"application", "side"'
    if outcome == "unrelated":
        requirements += ', "absent==1"'
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname = "root"\nversion = "1"\n'
        f"dependencies = [{requirements}]\n"
        '[tool.nab]\nmode = "universal"\n'
        '[tool.nab.matrix]\npython = "==3.13"\n'
        'platforms = ["linux_x86_64", "macos_arm64"]\n'
        f'[[tool.nab.indexes]]\nname = "local"\nurl = "{index.as_uri()}"\n'
    )
    for _ in range(2):
        status = run(["lock", str(project), "--offline", "--no-cache", "--output", "-"])
        assert status == (0 if outcome == "fallback" else 1)
        captured = capsys.readouterr()
        if outcome == "zip-failure":
            assert captured.out == ""
            assert (
                captured.err.count('try: set packages."pkg".allow-zip-sdists = true')
                == 1
            )
            assert captured.err.count("legacy ZIP sdists require") == 2
        else:
            assert "allow-zip-sdists" not in captured.err

    assert "allow-zip-sdists" not in caplog.text
