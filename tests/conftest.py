"""CLI-suite fixtures.

Scoped to the umbrella ``nab`` package's tests so the other workspaces'
suites (which need not have ``nab`` installed) do not import it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nab import cli
from nab.output import reset_log_handlers
from nab_python.config_sources import SourceRoots

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_nab_output(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin nab's output state so the CLI suite is deterministic.

    Two hazards this guards against:

    * CI sets ``FORCE_COLOR=1`` so its tool logs are coloured, which would
      otherwise make nab wrap its message tokens in ANSI and break the plain
      output the assertions expect.  ``NO_COLOR`` wins over ``FORCE_COLOR`` in
      :func:`~nab.output.should_color`, so it forces nab's colour off here
      regardless of the ambient environment; the colour behaviour itself is
      covered by unit tests that set the choice explicitly.
    * ``nab.cli.main`` sets a module-level printer (bound to the run's streams)
      and installs a logging handler on the nab loggers; without the reset a
      test that runs ``main`` would leak the printer (whose captured stream is
      closed once the test ends) and the handler into later tests.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    yield
    cli._printer = None
    reset_log_handlers()


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

    monkeypatch.setattr(cli, "_config_search_roots", fake_roots)
    monkeypatch.delenv("NAB_OFFLINE", raising=False)
    monkeypatch.delenv("NAB_CACHE_DIR", raising=False)
    monkeypatch.delenv("NAB_RESOLUTION", raising=False)
    return project_dir
