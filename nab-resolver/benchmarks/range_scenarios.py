"""Benchmark ``nab_resolver.Range`` through the resolver that defaults to it.

Usage:
    python nab-resolver/benchmarks/range_scenarios.py
    python nab-resolver/benchmarks/range_scenarios.py --scenario conflict-free-fanout
    python nab-resolver/benchmarks/range_scenarios.py --repeats 9 --json out.json

``Range`` is what ``Resolver`` and ``PartialSolution`` fall back to when a
consumer passes no ``range_type``, and nab-project always passes packaging's
``VersionRange``, so nothing in nab's other benchmark suites runs it. A change
to Range's algebra can win on one graph shape and lose on another, so both
regimes run here: ``wrong-package-backtracking`` drives the set predicates and
the differences that fragment a range, and ``conflict-free-fanout`` is almost
all membership tests against single-interval ranges.

Every scenario reports its Range call counts, how many ``__hash__`` calls found
the memo empty, the intervals per range that membership was tested against, and
a digest of the solution it reached. All of it holds across machines and
interpreters except the ``__hash__`` count, which follows how often CPython's
dicts ask a key for its hash and so differs between versions. The memo misses
beside it are what a change to hashing shows up in. Timing holds on no machine,
so quote a CPU number only from a serialized run, and read two runs whose
digests differ as two different questions.

``README.md`` next to this script covers what the numbers do and do not settle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from nab_resolver.resolver import ResolverStats
    from nab_resolver.types import RangeProtocol

# package -> version -> dependency -> required range.
Graph = dict[str, dict[int, dict[str, Range[int]]]]

FANOUT_RELEASES = 50
DEFAULT_REPEATS = 5

# The Range methods worth counting: the set predicates, the operators an
# implementation might answer them with, and the equality and hash a dict keyed
# by ranges calls.
COUNTED_METHODS = (
    "__and__",
    "__contains__",
    "__eq__",
    "__hash__",
    "__invert__",
    "__or__",
    "__sub__",
    "is_disjoint",
    "is_subset",
    "relation",
)


def wrong_package_backtracking_graph(releases: int) -> Graph:
    """Build the graph where every root but the oldest is unsatisfiable.

    Root ``k`` wants ``left k`` and ``right k-1``, which pin different versions
    of one shared package, so the resolver works its way down from the newest
    root and conflicts on the way.
    """
    graph: Graph = {"root": {}, "left": {}, "right": {}, "shared": {}}
    for index in range(1, releases + 1):
        lagging = max(1, index - 1)
        graph["shared"][index] = {}
        graph["left"][index] = {"shared": Range.singleton(index)}
        graph["right"][lagging] = {"shared": Range.singleton(lagging)}
        graph["root"][index] = {
            "left": Range.singleton(index),
            "right": Range.singleton(lagging),
        }
    return graph


def conflict_free_fanout_graph(packages: int) -> Graph:
    """Build one root depending on ``packages`` leaves of ``FANOUT_RELEASES`` each.

    Nothing depends on anything else, so no range ever gains a second interval
    and the resolve is dominated by membership tests during prioritization.
    """
    root: dict[str, Range[int]] = {}
    graph: Graph = {"root": {1: root}}
    for index in range(packages):
        name = f"p{index}"
        graph[name] = {version: {} for version in range(1, FANOUT_RELEASES + 1)}
        root[name] = Range.at_least(2)
    return graph


@dataclass(frozen=True)
class Scenario:
    """One graph shape and the size the standard run builds it at."""

    id: str
    size: int
    build: Callable[[int], Graph]


SCENARIOS = (
    Scenario("wrong-package-backtracking", 64, wrong_package_backtracking_graph),
    Scenario("conflict-free-fanout", 120, conflict_free_fanout_graph),
)


class GraphProvider(BaseProvider[str, int]):
    """Answer from an in-memory graph, newest release first."""

    def __init__(self, graph: Graph) -> None:
        """Index ``graph``'s releases newest first for the version scans."""
        self._graph = graph
        self._newest_first = {
            package: sorted(releases, reverse=True)
            for package, releases in graph.items()
        }

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        """Return the newest release of ``package`` inside ``version_range``."""
        for version in self._newest_first.get(package, ()):
            if version in version_range:
                return version
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        """Report whether any release of ``package`` is inside ``version_range``.

        Owed to ``ResolverProvider`` and not supplied by ``BaseProvider``. No
        scenario here reaches it.
        """
        return any(v in version_range for v in self._newest_first.get(package, ()))

    def get_dependencies(self, package: str, version: int) -> Mapping[str, Range[int]]:
        """Return the recorded dependency ranges for one release."""
        return self._graph.get(package, {}).get(version, {})

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Decide the package with the fewest releases still in range first."""
        del conflict_counts, culprit_counts
        return sum(1 for v in self._newest_first.get(package, ()) if v in version_range)

    def widen_decision(self, package: str, version: int) -> None:
        """Keep the exact singleton: nothing here widens a decision."""
        del package, version


def solution_digest(solution: Mapping[str, int]) -> str:
    """Identify a solution by its contents, so two separate runs can be compared.

    A change to ``Range`` that reaches a different answer agrees with itself on
    every repeat, so nothing inside one run can see it.
    """
    lines = "\n".join(
        f"{package} {version}" for package, version in sorted(solution.items())
    )
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Resolution:
    """One resolve: the solution, the search counters, and the range type used."""

    solution: dict[str, int]
    stats: ResolverStats[str]
    range_type: str

    @property
    def digest(self) -> str:
        """Short stable identity for the solution this resolve reached."""
        return solution_digest(self.solution)


def resolve_once(graph: Graph) -> Resolution:
    """Resolve ``graph`` with every ``Resolver`` argument left at its default.

    The range type is read back off the resolver rather than declared, so a
    report cannot claim to have exercised ``Range`` when it did not.
    """
    resolver: Resolver[str, int] = Resolver(GraphProvider(graph))
    solution = resolver.resolve({"root": Range.full()})
    range_type = resolver.range_type
    return Resolution(
        solution=solution,
        stats=resolver.stats,
        range_type=f"{range_type.__module__}.{range_type.__qualname__}",
    )


@dataclass
class IntervalCensus:
    """Intervals per receiver, counted over every membership test."""

    tests: int = 0
    intervals: int = 0
    largest: int = 0

    def record(self, count: int) -> None:
        """Fold in one membership test against a range of ``count`` intervals."""
        self.tests += 1
        self.intervals += count
        self.largest = max(self.largest, count)

    @property
    def mean(self) -> float:
        """Mean intervals per membership test, or zero when none ran."""
        return self.intervals / self.tests if self.tests else 0.0


@dataclass
class RangeTraffic:
    """What one resolve asked of ``Range``."""

    counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(COUNTED_METHODS, 0)
    )
    census: IntervalCensus = field(default_factory=IntervalCensus)
    # Calls that found the hash memo empty and so walked the intervals.
    hash_misses: int = 0


def count_range_traffic(graph: Graph) -> RangeTraffic:
    """Resolve ``graph`` once with ``Range``'s methods wrapped in counters.

    The wrappers are far too expensive to leave in place while timing, so this
    is its own resolve and the timed repeats run against the untouched class.
    """
    traffic = RangeTraffic()
    originals = {name: getattr(Range, name) for name in COUNTED_METHODS}

    def counter(name: str, original: Any) -> Any:
        """Wrap one Range method so every call to it is tallied."""

        def counted(self: Range[int], *args: Any, **kwargs: Any) -> Any:
            traffic.counts[name] += 1
            return original(self, *args, **kwargs)

        return counted

    def membership_counter(original: Any) -> Any:
        """Wrap ``__contains__``, which also censuses its receiver's intervals."""

        def counted(self: Range[int], version: object) -> bool:
            traffic.counts["__contains__"] += 1
            traffic.census.record(len(self._intervals))
            return bool(original(self, version))

        return counted

    def hash_counter(original: Any) -> Any:
        """Wrap ``__hash__``, separating the calls the memo already answers."""

        def counted(self: Range[int]) -> int:
            traffic.counts["__hash__"] += 1
            if self._hash == 0:
                traffic.hash_misses += 1
            return int(original(self))

        return counted

    for name, original in originals.items():
        if name == "__contains__":
            setattr(Range, name, membership_counter(original))
        elif name == "__hash__":
            setattr(Range, name, hash_counter(original))
        else:
            setattr(Range, name, counter(name, original))

    try:
        resolve_once(graph)
    finally:
        for name, original in originals.items():
            setattr(Range, name, original)

    return traffic


@dataclass(frozen=True)
class Measurement:
    """One scenario's resolve, its Range traffic, and its CPU samples."""

    scenario: Scenario
    resolution: Resolution
    traffic: RangeTraffic
    cpu_seconds: list[float]

    @property
    def median_cpu(self) -> float:
        """Median process CPU seconds across the timed repeats."""
        return statistics.median(self.cpu_seconds)


def measure(scenario: Scenario, repeats: int) -> Measurement:
    """Run one scenario: once for its counters, then ``repeats`` times for CPU.

    The counted resolve is not the first against this graph, so the hash misses
    it reports come from the ranges a resolve builds rather than from the
    graph's own literals.

    The repeats are checked against the first resolve's solution, which catches
    a resolve that is not reproducible within a run. Comparing answers across
    runs is what the reported digest is for.
    """
    graph = scenario.build(scenario.size)
    resolution = resolve_once(graph)
    traffic = count_range_traffic(graph)

    cpu_seconds = []
    for _ in range(repeats):
        start = time.process_time()
        repeated = resolve_once(graph)
        cpu_seconds.append(time.process_time() - start)
        if repeated.solution != resolution.solution:
            msg = f"{scenario.id}: repeated resolve selected a different solution"
            raise RuntimeError(msg)

    return Measurement(scenario, resolution, traffic, cpu_seconds)


def build_report(measurements: Sequence[Measurement], repeats: int) -> dict[str, Any]:
    """Assemble the JSON report for a finished run."""
    return {
        "schema": 1,
        "python": platform.python_version(),
        "repeats": repeats,
        "scenarios": [
            {
                "id": measurement.scenario.id,
                "size": measurement.scenario.size,
                "range_type": measurement.resolution.range_type,
                "digest": measurement.resolution.digest,
                "search": {
                    "rounds": measurement.resolution.stats.rounds,
                    "decisions": measurement.resolution.stats.decisions,
                    "conflicts": measurement.resolution.stats.conflicts,
                },
                "range_calls": measurement.traffic.counts,
                "hash_misses": measurement.traffic.hash_misses,
                "intervals": {
                    "max": measurement.traffic.census.largest,
                    "mean": measurement.traffic.census.mean,
                },
                "cpu_seconds": measurement.cpu_seconds,
            }
            for measurement in measurements
        ],
    }


def _print_summary(measurements: Sequence[Measurement]) -> None:
    """Print one row per scenario, then the Range calls behind each row."""
    print(
        "scenario                   size digest       rounds decisions conflicts"
        " intervals_max intervals_mean median_ms"
    )
    for measurement in measurements:
        stats = measurement.resolution.stats
        census = measurement.traffic.census
        print(
            f"{measurement.scenario.id:<26}"
            f" {measurement.scenario.size:>4}"
            f" {measurement.resolution.digest:<12}"
            f" {stats.rounds:>6}"
            f" {stats.decisions:>9}"
            f" {stats.conflicts:>9}"
            f" {census.largest:>13}"
            f" {census.mean:>14.2f}"
            f" {measurement.median_cpu * 1000:>9.2f}"
        )

    print()
    for measurement in measurements:
        calls = " ".join(
            f"{name}={count}"
            for name, count in sorted(measurement.traffic.counts.items())
        )
        misses = measurement.traffic.hash_misses
        print(f"{measurement.scenario.id}: {calls} hash_misses={misses}")


def main() -> None:
    """Run the selected scenarios against the resolver's default range type."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    requested = set(args.scenario)
    unknown = requested - {scenario.id for scenario in SCENARIOS}
    if unknown:
        parser.error(f"unknown --scenario values: {sorted(unknown)}")

    selected = [
        scenario for scenario in SCENARIOS if not requested or scenario.id in requested
    ]
    measurements = [measure(scenario, args.repeats) for scenario in selected]

    _print_summary(measurements)
    if args.json is not None:
        report = build_report(measurements, args.repeats)
        args.json.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


if __name__ == "__main__":
    main()
