"""The ``benchmark_import_path`` fixture, checked on its own.

Everywhere else it is a precondition for loading a script, so a wrong directory
surfaces there only as an import error.
"""

from __future__ import annotations

import sys
from importlib.machinery import PathFinder
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_BENCHMARKS = Path(__file__).resolve().parents[2] / "benchmarks"
_SIBLINGS = ("benchmark_datetime", "benchmark_host", "universal_result")


@pytest.fixture
def sys_path_restored() -> Iterator[None]:
    """Check sys.path is back where it started once the import path is torn down.

    Request this ahead of ``benchmark_import_path``: same-scope fixtures
    finalize in reverse setup order.
    """
    assert str(_BENCHMARKS) not in sys.path
    before = sys.path[:]

    yield

    assert sys.path == before


@pytest.mark.usefixtures("benchmark_import_path")
def test_siblings_resolve_to_the_benchmark_scripts() -> None:
    """Each name resolves to the script of that name under nab-project/benchmarks.

    The directory is recomputed here rather than read off the fixture, so a
    wrong or missing insert fails.
    """
    for name in _SIBLINGS:
        spec = PathFinder.find_spec(name)

        assert spec is not None
        assert spec.origin == str(_BENCHMARKS / f"{name}.py")


def test_teardown_drops_the_copy_an_executed_script_adds(
    sys_path_restored: None, benchmark_import_path: None
) -> None:
    """The insert stands in for ``strategy_sweep`` or ``_profile_runner`` running.

    ``sys_path_restored`` is what checks teardown took it off again.
    """
    sys.path.insert(0, str(_BENCHMARKS))


def test_no_bare_name_reaches_a_script_without_the_fixture() -> None:
    """A sys.path search for the sibling names comes up empty in this suite.

    It says nothing about sys.modules, where an already-loaded script leaves
    its siblings.
    """
    for name in _SIBLINGS:
        assert PathFinder.find_spec(name) is None
