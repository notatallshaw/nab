"""Tests for the process entry both the ``nab`` command and ``python -m nab`` use."""

from __future__ import annotations

import gc
import importlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import tomli

import nab
from nab._entry import console_entry

REPO_ROOT = Path(__file__).resolve().parents[1]

_TYPING_PROBE = """
import sys

sys.path.insert(0, sys.argv[1])
import nab._entry

assert "typing" not in sys.modules, "importing nab._entry loaded typing"
"""


@pytest.mark.usefixtures("restored_gc_state", "stubbed_gc_freeze")
def test_cli_import_pauses_the_collector_and_restores_it_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RefuseCli:
        @staticmethod
        def find_spec(fullname: str, *_args: object) -> None:
            if fullname == "nab.cli":
                assert not gc.isenabled()
                raise ImportError(fullname)

    # Force the real import to reach the finder even if another test loaded the CLI.
    monkeypatch.delitem(sys.modules, "nab.cli", raising=False)
    monkeypatch.delattr(sys.modules["nab"], "cli", raising=False)
    monkeypatch.setattr(sys, "meta_path", [RefuseCli(), *sys.meta_path])

    with pytest.raises(ImportError, match=r"^nab\.cli$"):
        console_entry()

    assert gc.isenabled()


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
    # Hatch's virtualenv and coverage can import typing during site initialization.
    # Add the tested installation explicitly after starting without those hooks.
    package_root = Path(nab.__file__).resolve().parent.parent
    subprocess.run(  # noqa: S603 - the probe is this file's own source
        [sys.executable, "-I", "-S", "-c", _TYPING_PROBE, str(package_root)], check=True
    )
