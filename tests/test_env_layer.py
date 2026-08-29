"""Tests for the ``NAB_*`` environment layer the config ladder reads."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from nab.cli import main

if TYPE_CHECKING:
    from pathlib import Path

_PROJECT = '[project]\nname = "probe"\nversion = "0.1"\ndependencies = []\n'

_TYPO_WARNING = "NAB_OFLINE is not a recognized nab setting"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(
            (
                "lock",
                "{project}/pyproject.toml",
                "--output",
                "{project}/pylock.toml",
                "--cache-dir",
                "{project}/cache",
            ),
            id="lock",
        ),
        pytest.param(
            (
                "download",
                "{project}/pyproject.toml",
                "--output",
                "{project}/wheels",
                "--cache-dir",
                "{project}/cache",
            ),
            id="download",
        ),
        pytest.param(
            ("config", "list", "--path", "{project}/pyproject.toml"),
            id="config-list",
        ),
        pytest.param(("cache", "dir"), id="cache-dir"),
    ],
)
def test_unknown_env_warning_fires_once(
    argv: tuple[str, ...],
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every command reads the env layer once, so a typo warns once.

    The count is the assertion: zero would mean the guard stopped
    running, and two that the command built the layer twice.
    """
    (hermetic_roots / "pyproject.toml").write_text(_PROJECT)
    monkeypatch.setenv("NAB_OFLINE", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["nab", *(part.format(project=hermetic_roots) for part in argv)],
    )

    main()

    assert capsys.readouterr().err.count(_TYPO_WARNING) == 1
