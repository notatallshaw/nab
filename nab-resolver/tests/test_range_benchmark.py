"""Tests for the Range benchmark under ``nab-resolver/benchmarks``.

The benchmark exists to catch a change to ``Range`` that helps a backtracking
resolve and hurts a wide one, so what has to hold is that its two scenarios
really are those two regimes, that both go through the resolver's default range
type, and that the traffic each reports is the traffic it reported before. Each
scenario runs here shrunk to ``SUITE_SIZES``; the sizes the standard run
declares are pinned on their own.

These carry the ``benchmark`` marker, so they run under ``nox -s benchmarks``
rather than in the resolver workspace.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest

from nab_resolver.ranges import Range

pytestmark = pytest.mark.benchmark

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmarks" / "range_scenarios.py"
MODULE_NAME = "_nab_range_scenarios"

# Small enough to run in a fraction of a second, large enough to backtrack and
# to fan out.
SUITE_SIZES = {"wrong-package-backtracking": 8, "conflict-free-fanout": 6}


class Profile(NamedTuple):
    """What one scenario asks of the resolver and of Range at its suite size.

    The ``__hash__`` call count is left out because it is the one reported
    number that differs between interpreters. ``hash_misses`` is the part of it
    a change to hashing shows up in, and that part is the same on 3.10 through
    3.14.
    """

    rounds: int
    decisions: int
    conflicts: int
    membership_tests: int
    intervals_seen: int
    largest_range: int
    hash_misses: int
    equality_calls: int


SUITE_PROFILE = {
    "wrong-package-backtracking": Profile(
        rounds=41,
        decisions=27,
        conflicts=14,
        membership_tests=448,
        intervals_seen=892,
        largest_range=8,
        hash_misses=116,
        equality_calls=471,
    ),
    "conflict-free-fanout": Profile(
        rounds=8,
        decisions=8,
        conflicts=0,
        membership_tests=1058,
        intervals_seen=1058,
        largest_range=1,
        hash_misses=12,
        equality_calls=48,
    ),
}


@pytest.fixture(scope="module")
def benchmark() -> Iterator[ModuleType]:
    """Import the benchmark script, which lives outside any package.

    The module has to be in ``sys.modules`` before it executes, or the
    dataclasses in it cannot resolve their own annotations.
    """
    spec = importlib.util.spec_from_file_location(MODULE_NAME, BENCHMARK)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[MODULE_NAME]


def scenario_of(benchmark: ModuleType, scenario_id: str) -> Any:
    """Look one declared scenario up by id rather than by position."""
    return next(s for s in benchmark.SCENARIOS if s.id == scenario_id)


def measure_small(benchmark: ModuleType, scenario_id: str) -> Any:
    """Run one declared scenario shrunk to the size the suite runs it at."""
    scenario = scenario_of(benchmark, scenario_id)
    return benchmark.measure(
        replace(scenario, size=SUITE_SIZES[scenario_id]), repeats=1
    )


def test_standard_run_declares_the_full_scale_sizes(benchmark: ModuleType) -> None:
    assert {s.id: s.size for s in benchmark.SCENARIOS} == {
        "wrong-package-backtracking": 64,
        "conflict-free-fanout": 120,
    }
    assert benchmark.FANOUT_RELEASES == 50
    assert benchmark.DEFAULT_REPEATS == 5


@pytest.mark.parametrize("scenario_id", list(SUITE_SIZES))
def test_scenario_resolves_through_the_default_range_type(
    benchmark: ModuleType, scenario_id: str
) -> None:
    measurement = measure_small(benchmark, scenario_id)

    assert measurement.resolution.range_type == "nab_resolver.ranges.Range"
    assert measurement.traffic.counts["__contains__"] > 0
    assert len(measurement.cpu_seconds) == 1


def test_backtracking_scenario_works_the_set_predicates(benchmark: ModuleType) -> None:
    measurement = measure_small(benchmark, "wrong-package-backtracking")
    counts = measurement.traffic.counts

    assert measurement.resolution.stats.conflicts > 0
    assert counts["is_subset"] > 0
    assert counts["is_disjoint"] > 0
    assert counts["__sub__"] > 0

    # Carving a negative term out of a positive one splits an interval in two,
    # so the ranges under test stop carrying a single one.
    assert measurement.traffic.census.largest > 1


def test_fanout_scenario_stays_conflict_free_and_single_interval(
    benchmark: ModuleType,
) -> None:
    measurement = measure_small(benchmark, "conflict-free-fanout")
    counts = measurement.traffic.counts

    assert measurement.resolution.stats.conflicts == 0
    assert counts["is_disjoint"] == 0
    assert counts["__sub__"] == 0

    assert measurement.traffic.census.largest == 1
    assert measurement.traffic.census.mean == 1.0


@pytest.mark.parametrize("scenario_id", list(SUITE_SIZES))
def test_scenario_pins_its_search_and_membership_profile(
    benchmark: ModuleType, scenario_id: str
) -> None:
    """Hold each scenario to the workload the report says it measured.

    The regime assertions pass on any graph that merely conflicts or merely
    fans out, and most of the membership tests come from ``GraphProvider``
    scanning its own release lists rather than from the resolver. An edit that
    leaves the answer correct can still move what a comparison between two
    ``Range`` implementations is read off, so these numbers change deliberately
    or not at all.
    """
    measurement = measure_small(benchmark, scenario_id)
    stats = measurement.resolution.stats
    census = measurement.traffic.census

    assert (
        Profile(
            rounds=stats.rounds,
            decisions=stats.decisions,
            conflicts=stats.conflicts,
            membership_tests=census.tests,
            intervals_seen=census.intervals,
            largest_range=census.largest,
            hash_misses=measurement.traffic.hash_misses,
            equality_calls=measurement.traffic.counts["__eq__"],
        )
        == SUITE_PROFILE[scenario_id]
    )


def test_digest_separates_two_runs_that_each_agree_with_themselves(
    benchmark: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-run solution identity, which the repeat guard cannot supply.

    A ``Range`` change that deterministically resolves to different pins agrees
    with itself on every repeat, so ``measure`` stays quiet and the digest is
    the only thing separating the two runs. It therefore has to reach the JSON
    report, which is what an A/B actually diffs.
    """
    baseline = measure_small(benchmark, "conflict-free-fanout")

    def oldest_in_range(_self: Any, _package: str, version_range: Any) -> int | None:
        for version in range(1, benchmark.FANOUT_RELEASES + 1):
            if version in version_range:
                return version
        return None

    monkeypatch.setattr(benchmark.GraphProvider, "choose_version", oldest_in_range)
    drifted = measure_small(benchmark, "conflict-free-fanout")

    assert drifted.resolution.solution != baseline.resolution.solution
    assert drifted.resolution.digest != baseline.resolution.digest

    report = benchmark.build_report([baseline, drifted], repeats=1)
    assert [scenario["digest"] for scenario in report["scenarios"]] == [
        baseline.resolution.digest,
        drifted.resolution.digest,
    ]


def test_counting_leaves_range_as_it_found_it(benchmark: ModuleType) -> None:
    """Counting patches a shipped class, so it has to put every method back."""
    original = {name: getattr(Range, name) for name in benchmark.COUNTED_METHODS}

    scenario = scenario_of(benchmark, "wrong-package-backtracking")
    benchmark.count_range_traffic(scenario.build(4))

    assert {
        name: getattr(Range, name) for name in benchmark.COUNTED_METHODS
    } == original


def test_repeat_that_selects_another_solution_is_reported(
    benchmark: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard that stops one run being averaged over two different answers."""
    real_resolve = benchmark.resolve_once
    resolves = itertools.count()

    def drifting(graph: Any) -> Any:
        resolution = real_resolve(graph)
        if next(resolves) == 0:
            return resolution
        return replace(resolution, solution={**resolution.solution, "root": -1})

    monkeypatch.setattr(benchmark, "resolve_once", drifting)

    scenario = replace(scenario_of(benchmark, "wrong-package-backtracking"), size=4)
    with pytest.raises(RuntimeError, match="different solution"):
        benchmark.measure(scenario, repeats=1)


def test_command_line_run_prints_and_writes_one_scenario(
    benchmark: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The invocation the README documents, shrunk to a size the suite affords.

    The JSON is what an A/B diffs, so the digest has to survive the round trip
    to the file and agree with the same scenario measured directly.
    """
    monkeypatch.setattr(
        benchmark,
        "SCENARIOS",
        tuple(
            replace(scenario, size=SUITE_SIZES[scenario.id])
            for scenario in benchmark.SCENARIOS
        ),
    )
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "range_scenarios.py",
            "--scenario",
            "conflict-free-fanout",
            "--repeats",
            "1",
            "--json",
            str(report_path),
        ],
    )

    benchmark.main()

    printed = capsys.readouterr().out
    report = json.loads(report_path.read_text(encoding="utf-8"))
    (scenario_report,) = report["scenarios"]

    assert report["repeats"] == 1
    assert scenario_report["id"] == "conflict-free-fanout"
    assert (
        scenario_report["digest"]
        == measure_small(benchmark, "conflict-free-fanout").resolution.digest
    )

    # The table row carries the digest and the block under it the call counts.
    assert scenario_report["digest"] in printed
    calls = scenario_report["range_calls"]
    assert f"conflict-free-fanout: __and__={calls['__and__']}" in printed


@pytest.mark.parametrize(
    ("argument", "complaint"),
    [
        (["--repeats", "0"], "--repeats must be at least 1"),
        (["--scenario", "no-such-shape"], "unknown --scenario values"),
    ],
)
def test_command_line_refuses_a_run_it_cannot_make(
    benchmark: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argument: list[str],
    complaint: str,
) -> None:
    """Both arguments the parser turns away before any scenario is built."""
    monkeypatch.setattr(sys, "argv", ["range_scenarios.py", *argument])

    with pytest.raises(SystemExit):
        benchmark.main()

    assert complaint in capsys.readouterr().err
