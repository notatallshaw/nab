"""CLI-suite fixtures.

Scoped to the umbrella ``nab`` package's tests so the other workspaces'
suites (which need not have ``nab`` installed) do not import it.
"""

from __future__ import annotations

import gc
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from nab import _run
from nab.config.ladder import SourceRoots
from nab.output import reset_run

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager


@pytest.fixture(autouse=True)
def _reset_nab_output(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin nab's output state so the CLI suite is deterministic.

    Two hazards this guards against:

    * CI sets ``FORCE_COLOR=1`` so its tool logs are coloured, which would
      otherwise make nab wrap its message tokens in ANSI and break the plain
      output the assertions expect.  Both variables are cleared rather than
      relying on ``NO_COLOR`` winning: a case that wants the isatty decision
      clears ``NO_COLOR``, and an ambient ``FORCE_COLOR`` left behind would
      paint whatever it looked at.
    * ``nab.output.begin`` sets a module-level printer (bound to the run's
      streams) and installs a logging handler on the nab loggers; without the
      reset a test that runs ``main`` would leak the printer (whose captured
      stream is closed once the test ends) and the handler into later tests.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    yield
    reset_run()


@pytest.fixture
def restored_gc_state() -> Iterator[None]:
    """Restore ``gc.isenabled()`` after the test.

    A test that lets the CLI switch the collector off leaks that state into
    the rest of the session when the CLI's own restore is what broke. The
    permanent generation is not part of this; see :func:`stubbed_gc_freeze`.
    """
    enabled = gc.isenabled()
    try:
        yield
    finally:
        if enabled:
            gc.enable()
        else:
            gc.disable()


@pytest.fixture
def stubbed_gc_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``gc.freeze`` with a no-op for a test that runs the real entry.

    :func:`nab._entry.console_entry` freezes the import graph, and there is no
    partial unfreeze to undo it, so a real call would leave everything the
    session holds in the permanent generation.
    """
    monkeypatch.setattr(gc, "freeze", lambda: None)


@pytest.fixture
def hermetic_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config discovery at a tmp system/user/project tree.

    Returns the project dir. The system and user files point at tmp paths a
    test can write, so nothing reads the real ``~/.config``.
    """
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    def fake_roots(_: Path) -> SourceRoots:
        return SourceRoots(
            system_toml=tmp_path / "sys" / "nab.toml",
            user_toml=tmp_path / "usr" / "nab.toml",
            project_dir=project_dir,
        )

    monkeypatch.setattr(_run, "config_search_roots", fake_roots)
    monkeypatch.delenv("NAB_OFFLINE", raising=False)
    monkeypatch.delenv("NAB_CACHE_DIR", raising=False)
    monkeypatch.delenv("NAB_RESOLUTION", raising=False)
    return project_dir


@contextmanager
def _as_fifo(target: Path) -> Iterator[None]:
    real_os_stat, real_path_stat = os.stat, Path.stat
    piped = os.fspath(target)
    fifo = os.stat_result((stat.S_IFIFO | 0o644, *([0] * 9)))

    def fifo_os_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == piped:
            return fifo
        return real_os_stat(path, *args, **kwargs)

    def fifo_path_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(self) == piped:
            return fifo
        return real_path_stat(self, *args, **kwargs)

    with (
        patch.object(os, "stat", fifo_os_stat),
        patch.object(Path, "stat", fifo_path_stat),
    ):
        yield


@pytest.fixture
def as_fifo() -> Callable[[Path], AbstractContextManager[None]]:
    """Simulate a named pipe at a path.

    Inside ``as_fifo(p)`` a stat of ``p`` reports a FIFO, which is what bash
    process substitution and a piped ``/dev/stdin`` hand a command that takes a
    project path. ``os.mkfifo`` is POSIX-only, so the state is simulated rather
    than created.

    Both stat routes are patched. On Python 3.10 ``Path.stat`` calls an
    ``os.stat`` bound into pathlib at import time, so patching ``os.stat``
    alone leaves every ``pathlib`` presence check reading the real file.
    """
    return _as_fifo
