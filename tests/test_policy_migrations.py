"""Removed combined policies fail with migrations for their original scope."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab.cli import run


@pytest.mark.parametrize(
    ("legacy", "replacement"),
    [("prefer-wheel", "wheel-or-sdist"), ("sdist-install", "sdist-only")],
)
def test_removed_cli_choice_has_an_executable_replacement(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, legacy: str, replacement: str
) -> None:
    assert run(("lock", "--project-dist-policy", legacy)) != 0
    error = capsys.readouterr().err
    assert f"dist-policy '{legacy}' was removed" in error
    assert (
        f"--project-dist-policy {replacement} --project-metadata-policy prefer-wheel"
        in error
    )
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "demo"\nversion = "1.0"\n')
    assert (
        run(
            (
                "config",
                "get",
                "metadata-policy",
                "--path",
                str(path),
                "--project-dist-policy",
                replacement,
                "--project-metadata-policy",
                "prefer-wheel",
            )
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "prefer-wheel"
