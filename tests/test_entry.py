"""Tests for the process entry both the ``nab`` command and ``python -m nab`` use."""

from __future__ import annotations

import builtins
import gc
import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import tomli

from nab._entry import console_entry

REPO_ROOT = Path(__file__).resolve().parents[1]

_TYPING_PROBE = """
import sys

import nab._entry

assert "typing" not in sys.modules, "importing nab._entry loaded typing"
"""


@pytest.mark.usefixtures("restored_gc_state", "stubbed_gc_freeze")
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

    Nothing else reads that declaration, so a target left on ``nab.cli`` would
    pass every other test in this file.
    """
    pyproject = tomli.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    module_name, _, attribute = pyproject["project"]["scripts"]["nab"].partition(":")

    assert getattr(importlib.import_module(module_name), attribute) is console_entry


def test_the_entry_module_loads_no_typing() -> None:
    """Importing this module pulls in no ``typing``.

    A ``TYPE_CHECKING`` import is still an ``import typing`` at runtime, and
    this module is imported before :func:`console_entry` switches the
    collector off. Only a fresh interpreter can see it: the test session has
    ``typing`` loaded before collection starts.
    """
    # coverage's .pth imports coverage, and so typing, in any child it starts.
    env = dict(os.environ)
    env.pop("COVERAGE_PROCESS_START", None)
    env.pop("COVERAGE_PROCESS_CONFIG", None)

    subprocess.run(  # noqa: S603 - the probe is this file's own source
        [sys.executable, "-c", _TYPING_PROBE], check=True, env=env
    )
