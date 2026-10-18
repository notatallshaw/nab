"""Apply scoped diagnostic remedies through the real config parser and resolver."""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nab.cli import run
from nab.config.model import read_pyproject_config
from nab_index.client import WheelFile
from nab_project._testing.coordinator_fake import make_coordinator

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture


def _scoped_entry(surface: str, selector: str, body: str) -> str:
    """Render a scoped table or a rule shared with an unrelated package."""
    if surface == "table":
        return f'\n[tool.nab.packages."{selector}"]\n{body}'
    selectors = [selector, selector.replace("pkg", "other")]
    return f"\n[[tool.nab.package-rules]]\nmatch = {json.dumps(selectors)}\n{body}"


def _apply_remedy(body: str, diagnostic: str) -> str:
    """Apply the scoped edit named by the diagnostic."""
    match = re.search(
        r"add a package-rules entry with match = (\[.*?\]) and (.*?)(?:, or|$)",
        diagnostic,
        re.MULTILINE,
    )
    assert match is not None, diagnostic
    return body + f"\n[[tool.nab.package-rules]]\nmatch = {match[1]}\n{match[2]}\n"


@pytest.mark.parametrize("version", ["3.0", "3.0.0", "3.0rc1", "3.0+local"])
@pytest.mark.parametrize("surface", ["table", "rule"])
@pytest.mark.parametrize("field", ["uploaded-prior-to", "dist-policy"])
@pytest.mark.parametrize("verbosity", [[], ["-v"]])
def test_scoped_remedy_parses_and_admits_the_refused_wheel(
    hermetic_roots: Path,
    capsys: CaptureFixture[str],
    version: str,
    surface: str,
    field: str,
    verbosity: list[str],
) -> None:
    if field == "uploaded-prior-to":
        global_restriction = "uploaded-prior-to = 2015-01-01T00:00:00Z\n"
        field_exception = "uploaded-prior-to = false"
        other_policy = 'dist-policy = "wheel-only"\n'
    else:
        global_restriction = 'dist-policy = "sdist-only"\n'
        field_exception = 'dist-policy = "wheel-or-sdist"'
        other_policy = "uploaded-prior-to = false\n"

    body = (
        f'[project]\nname="root"\nversion="1"\ndependencies=["pkg=={version}"]\n'
        '[tool.nab]\nbuild-policy="never"\n'
        + global_restriction
        + _scoped_entry(surface, "pkg<2", field_exception + "\n" + other_policy)
        + _scoped_entry(surface, "pkg>=2", other_policy)
        + _scoped_entry(surface, "pkg==3.0+other", field_exception + "\n")
    )
    path = hermetic_roots / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    original = read_pyproject_config(path, discover_workspace=False)

    coordinator = make_coordinator(
        [
            WheelFile(
                filename=f"pkg-{version}-py3-none-any.whl",
                url=f"https://example.com/pkg-{version}-py3-none-any.whl",
                version=version,
                requires_python=None,
                has_metadata=True,
                upload_time="2020-01-01T00:00:00Z",
            )
        ],
        package="pkg",
        auto_metadata=True,
    )
    args = (
        *verbosity,
        "lock",
        str(path),
        "--no-cache",
        "--no-workspace-discovery",
        "--no-progress",
        "--python",
        "3.12",
        "--format",
        "requirements-without-hashes",
        "--output",
        "-",
    )
    with patch(
        "nab_project.resolve.FetchCoordinator", return_value=nullcontext(coordinator)
    ):
        assert run(args) == 1
        diagnostic = capsys.readouterr().err
        assert field in diagnostic

        path.write_text(_apply_remedy(body, diagnostic), encoding="utf-8")
        repaired = read_pyproject_config(path, discover_workspace=False)
        assert (
            repaired.resolve_inputs().package_overrides[:-1]
            == original.resolve_inputs().package_overrides
        )

        assert run(args) == 0
        assert capsys.readouterr().out == f"pkg=={version}\n"
