"""Tests for the installed ``nab`` command's process entry."""

from __future__ import annotations

import builtins
import gc
import importlib
from pathlib import Path
from typing import Any

import pytest
import tomli

from nab._entry import console_entry

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.usefixtures("restored_gc_state")
def test_imports_the_cli_while_the_collector_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collector is off at the moment the ``nab.cli`` import runs.

    The saving is the import running with the collector off, so the state is
    read from inside the import: an import hoisted out of the disabled window
    has to turn this test red.
    """
    enabled_at_import: list[bool] = []
    real_import = builtins.__import__

    def record_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "nab.cli":
            enabled_at_import.append(gc.isenabled())
        return real_import(name, *args, **kwargs)

    # A real freeze would make every object the test session holds permanent.
    monkeypatch.setattr(gc, "freeze", lambda: None)

    monkeypatch.setattr("nab.cli.console_entry", lambda: None)
    monkeypatch.setattr(builtins, "__import__", record_import)

    console_entry()

    assert enabled_at_import == [False]


@pytest.mark.usefixtures("restored_gc_state")
def test_freezes_the_import_graph_before_enabling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The freeze runs while the collector is still off, the CLI once it is on."""
    events: list[tuple[str, bool]] = []
    monkeypatch.setattr(gc, "freeze", lambda: events.append(("freeze", gc.isenabled())))
    monkeypatch.setattr(
        "nab.cli.console_entry", lambda: events.append(("cli", gc.isenabled()))
    )

    console_entry()

    assert events == [("freeze", False), ("cli", True)]


@pytest.mark.usefixtures("restored_gc_state")
def test_without_gc_freeze_still_reenables_the_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collector comes back on an interpreter without ``gc.freeze``."""
    enabled_during: list[bool] = []
    monkeypatch.delattr(gc, "freeze")
    monkeypatch.setattr(
        "nab.cli.console_entry", lambda: enabled_during.append(gc.isenabled())
    )

    console_entry()

    assert enabled_during == [True]


def test_console_script_runs_the_collector_off_entry() -> None:
    """``[project.scripts]`` points the ``nab`` command at this module.

    The console script is the only thing that reaches it, so a target left on
    ``nab.cli`` would pass every other test.
    """
    pyproject = tomli.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    module_name, _, attribute = pyproject["project"]["scripts"]["nab"].partition(":")

    assert getattr(importlib.import_module(module_name), attribute) is console_entry
