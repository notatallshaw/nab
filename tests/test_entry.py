"""Tests for the process entry both the ``nab`` command and ``python -m nab`` use."""

from __future__ import annotations

import builtins
import gc
import importlib
import os
import subprocess
import sys
from collections.abc import Callable
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

    monkeypatch.setattr("nab.cli.console_entry", lambda _resume: None)
    monkeypatch.setattr(builtins, "__import__", record_import)

    console_entry()

    assert enabled_at_import == [False]


@pytest.mark.usefixtures("restored_gc_state")
def test_the_cli_runs_with_the_collector_off_until_it_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collector stays off until the CLI calls back.

    Everything the CLI imports before that call is inside the freeze.
    """
    events: list[tuple[str, bool]] = []
    monkeypatch.setattr(gc, "freeze", lambda: events.append(("freeze", gc.isenabled())))

    def run_cli(resume: Callable[[], None]) -> None:
        events.append(("cli", gc.isenabled()))
        resume()
        events.append(("resumed", gc.isenabled()))

    monkeypatch.setattr("nab.cli.console_entry", run_cli)

    console_entry()

    assert events == [("cli", False), ("freeze", False), ("resumed", True)]


@pytest.mark.usefixtures("restored_gc_state", "stubbed_gc_freeze")
def test_a_cli_that_never_resumes_still_leaves_the_collector_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module that switches the collector off is the one that guarantees it back."""
    monkeypatch.setattr("nab.cli.console_entry", lambda _resume: None)

    console_entry()

    assert gc.isenabled()


@pytest.mark.usefixtures("restored_gc_state")
def test_a_second_resume_does_not_freeze_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One freeze between the CLI's call and this module's own.

    A freeze on the second would take the command's own graph into the
    permanent generation.
    """
    freezes: list[int] = []
    monkeypatch.setattr(gc, "freeze", lambda: freezes.append(1))
    monkeypatch.setattr("nab.cli.console_entry", lambda resume: resume())

    console_entry()

    assert freezes == [1]


@pytest.mark.usefixtures("restored_gc_state")
def test_without_gc_freeze_still_reenables_the_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collector comes back on an interpreter without ``gc.freeze``."""
    enabled_after: list[bool] = []
    monkeypatch.delattr(gc, "freeze")

    def run_cli(resume: Callable[[], None]) -> None:
        resume()
        enabled_after.append(gc.isenabled())

    monkeypatch.setattr("nab.cli.console_entry", run_cli)

    console_entry()

    assert enabled_after == [True]


@pytest.mark.usefixtures("restored_gc_state", "stubbed_gc_freeze")
def test_a_cli_that_cannot_be_imported_leaves_the_collector_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken install raises out of here rather than out of a paused process."""
    real_import = builtins.__import__

    def refuse_cli(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "nab.cli":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_cli)

    with pytest.raises(ImportError):
        console_entry()

    assert gc.isenabled()


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
