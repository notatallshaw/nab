"""Local file-index declarations through the real configuration and lock paths."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from nab.cli import run
from nab.config.model import read_pyproject_config
from nab_project import toml_io
from nab_provider.testing import make_metadata

from ._local_artifacts import make_wheel_archive, publish_artifacts


def _project(tmp_path: Path, url: str, dependencies: tuple[str, ...] = ()) -> Path:
    """Write an offline project with one explicitly configured index."""
    path = tmp_path / "pyproject.toml"
    path.write_text(
        f'[project]\nname="app"\nversion="1"\ndependencies={json.dumps(dependencies)}\n'
        '[tool.nab]\nbuild-policy="never"\n'
        '[tool.nab.matrix]\npython="==3.14"\nplatforms=["linux_x86_64"]\n'
        f'[[tool.nab.indexes]]\nname="local"\nurl={json.dumps(url)}\n'
    )
    return path


@pytest.mark.parametrize(
    "url",
    [
        "file://localhost/unused%00",
        pytest.param(
            "file://server/share",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="Windows supports UNC file authorities"
            ),
        ),
    ],
)
def test_invalid_file_index_is_a_cli_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], url: str
) -> None:
    """Reject invalid sources even when the project needs no packages."""
    path = _project(tmp_path, url)
    lock = tmp_path / "pylock.toml"
    assert run(("lock", str(path), "--offline", "--output", str(lock))) == 1
    assert not lock.exists()
    assert "invalid file-index URL" in capsys.readouterr().err


@pytest.mark.parametrize("suffix", ["", "?download=1", "#fragment", "?x=1#fragment"])
def test_localhost_file_index_keeps_query_and_fragment(
    tmp_path: Path, suffix: str
) -> None:
    """A localhost URL still selects the wheel from its local directory."""
    index = tmp_path / "index"
    wheel = make_wheel_archive(
        "demo", "1.0", make_metadata("demo", "1.0"), "py3-none-any"
    )
    publish_artifacts(index, "demo", {"demo-1.0-py3-none-any.whl": wheel})
    url = index.as_uri().replace("file://", "file://localhost", 1) + suffix
    path = _project(tmp_path, url, ("demo==1.0",))
    lock = tmp_path / "pylock.toml"
    assert run(("lock", str(path), "--offline", "--output", str(lock))) == 0
    (package,) = toml_io.load_path(lock)["packages"]
    assert package["index"] == url
    assert package["wheels"][0]["hashes"]["sha256"] == hashlib.sha256(wheel).hexdigest()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows UNC paths")
def test_unc_file_index_is_not_rebased(tmp_path: Path) -> None:
    """Windows retains the declared UNC authority without requiring a live share."""
    url = "file://server/share"
    path = _project(tmp_path, url)
    assert read_pyproject_config(path).inputs.indexes[0].url == url
