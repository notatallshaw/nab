"""Benchmark-suite fixtures.

``benchmark_import_path`` cannot live in either conftest above it: the umbrella
sdist ships the top-level one and no benchmarks directory, and a conftest at
``nab-project/tests/`` would collide with the umbrella suite's ``tests.conftest``
when one process collects both trees.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_BENCHMARKS_DIR = Path(__file__).resolve().parents[2] / "benchmarks"


@pytest.fixture
def benchmark_import_path() -> Iterator[None]:
    """Make the benchmark scripts importable by bare name, for one test only.

    The scripts run directly, so a test that loads one by file path has to
    supply that import path. Teardown restores the whole list because
    ``strategy_sweep`` and ``_profile_runner`` insert the directory for
    themselves and never take it off.
    """
    restore = sys.path[:]
    sys.path.insert(0, str(_BENCHMARKS_DIR))
    try:
        yield
    finally:
        sys.path[:] = restore
