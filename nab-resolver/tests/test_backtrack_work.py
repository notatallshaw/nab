"""Backtracking visits only the package trails whose assignments it removes."""

from __future__ import annotations

from collections import UserDict
from typing import SupportsIndex

import pytest

from nab_resolver._compat import override
from nab_resolver.partial_solution import Assignment, PartialSolution
from nab_resolver.ranges import Range
from nab_resolver.types import Incompatibility, IncompatibilityCause, RangeProtocol


class CountingTrails(UserDict[str, list[Assignment[str, int]]]):
    """Count trail lookups, including lookups through mapping iteration."""

    def __init__(self, entries: dict[str, list[Assignment[str, int]]]) -> None:
        super().__init__(entries)
        self.visits: list[str] = []

    @override
    def __getitem__(self, package: str) -> list[Assignment[str, int]]:
        """Record a visit before returning the requested trail."""
        self.visits.append(package)
        return super().__getitem__(package)


@pytest.mark.parametrize("retained", [0, 32, 512])
@pytest.mark.parametrize("removed", [0, 1, 3])
def test_trail_visits_depend_on_removed_packages(
    monkeypatch: pytest.MonkeyPatch, retained: int, removed: int
) -> None:
    solution: PartialSolution[str, int] = PartialSolution()
    for index in range(retained):
        solution.decide(f"kept-{index}", 0)
    for index in range(removed):
        solution.decide(f"removed-{index}", 1)
    trails = CountingTrails(solution._assignments_by_package)
    monkeypatch.setattr(solution, "_assignments_by_package", trails)

    solution.backtrack(retained)

    assert sorted(trails.visits) == sorted(f"removed-{i}" for i in range(removed))
    assert dict(solution.decisions()) == {f"kept-{i}": 0 for i in range(retained)}
    assert solution.trail_length == retained
    assert solution.decision_level == retained


def test_interleaved_changes_restore_each_affected_package_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    solution: PartialSolution[str, int] = PartialSolution()
    cause: Incompatibility[str, int] = Incompatibility(
        [], cause=IncompatibilityCause.ROOT
    )
    allowed = Range.at_least(1)
    excluded = Range.singleton(9)
    solution.decide("kept", 0)
    solution.derive("mixed", allowed, positive=True, cause=cause)
    solution.derive("mixed", excluded, positive=False, cause=cause)
    solution.derive("negative", excluded, positive=False, cause=cause)
    before_decisions = solution.decisions()
    before_ranges = solution.positive_ranges()
    mixed_before = solution.get("mixed")
    negative_before = solution.get("negative")

    solution.decide("mixed", 2)
    solution.derive("negative", allowed, positive=True, cause=cause)
    solution.derive("mixed", Range.singleton(3), positive=False, cause=cause)
    solution.decide("removed", 5)
    solution.derive("mixed", Range.less_than(8), positive=True, cause=cause)
    solution.derive("negative", Range.singleton(7), positive=False, cause=cause)
    later_decisions = solution.decisions()
    trails = CountingTrails(solution._assignments_by_package)
    monkeypatch.setattr(solution, "_assignments_by_package", trails)

    solution.backtrack(1)

    assert sorted(trails.visits) == ["mixed", "negative", "removed"]
    assert dict(solution.decisions()) == dict(before_decisions) == {"kept": 0}
    assert dict(solution.positive_ranges()) == dict(before_ranges)
    assert dict(later_decisions) == {"kept": 0, "mixed": 2, "removed": 5}
    assert solution.get("mixed") == mixed_before
    assert solution.get("negative") == negative_before
    assert solution.get("removed") is None
    assert solution.undecided_packages() == {"mixed"}
    assert solution.trail_length == 4


class CountingCache(UserDict[str, RangeProtocol[int] | None]):
    """Count cache probes, including misses during invalidation."""

    def __init__(self, entries: dict[str, RangeProtocol[int] | None]) -> None:
        super().__init__(entries)
        self.probes: list[str] = []

    @override
    def __getitem__(self, package: str) -> RangeProtocol[int] | None:
        """Record a probe before returning the cached range."""
        self.probes.append(package)
        return super().__getitem__(package)


@pytest.mark.parametrize("repetitions", [1, 8])
def test_backtrack_invalidates_each_changed_package_once(
    monkeypatch: pytest.MonkeyPatch, repetitions: int
) -> None:
    solution: PartialSolution[str, int] = PartialSolution()
    cause: Incompatibility[str, int] = Incompatibility(
        [], cause=IncompatibilityCause.ROOT
    )
    solution.decide("kept", 0)
    solution.derive("mixed", Range.at_least(1), positive=True, cause=cause)
    before = solution.get("mixed")
    kept = solution.get("kept")
    solution.decide("removed", 2)
    for _ in range(repetitions):
        solution.derive("mixed", Range.singleton(9), positive=False, cause=cause)
        solution.derive("removed", Range.full(), positive=True, cause=cause)
    solution.take_changed_packages()
    cache = CountingCache(solution._effective_range_cache)
    monkeypatch.setattr(solution, "_effective_range_cache", cache)

    solution.backtrack(1)

    assert sorted(cache.probes) == ["mixed", "removed"]
    assert solution.take_changed_packages() == {"mixed", "removed"}
    assert solution.get("mixed") is before
    assert solution.get("kept") is kept
    assert solution.get("removed") is None


def test_repeated_backtracks_preserve_trail_indices_and_cumulative_ranges() -> None:
    solution: PartialSolution[str, int] = PartialSolution()
    cause: Incompatibility[str, int] = Incompatibility(
        [], cause=IncompatibilityCause.ROOT
    )
    solution.derive("mixed", Range.at_least(1), positive=True, cause=cause)
    solution.derive("mixed", Range.singleton(9), positive=False, cause=cause)
    initial = solution.get("mixed")
    solution.decide("kept", 1)
    solution.derive("kept", Range.at_least(0), positive=True, cause=cause)
    retained = solution.trail_length

    for version in (2, 3, 4):
        solution.decide("mixed", version)
        solution.derive("mixed", Range.singleton(8), positive=False, cause=cause)
        solution.decide("removed", version)
        solution.derive("mixed", Range.at_least(0), positive=True, cause=cause)
        solution.backtrack(1)

        assert solution.trail_length == retained
        assert [a.trail_index for a in solution._assignments] == list(range(retained))
        assert dict(solution.decisions()) == {"kept": 1}
        assert solution.get("mixed") == initial
        assert solution.get("removed") is None

    solution.backtrack(0)
    assert solution.trail_length == 2
    assert solution.get("mixed") == initial
    assert not solution.decisions()
    solution.backtrack(-1)
    assert solution.trail_length == 0
    assert solution.get("mixed") is None


class CountingEntries(list[Assignment[str, int]]):
    """Count assignments removed individually from a package trail."""

    def __init__(self, entries: list[Assignment[str, int]]) -> None:
        super().__init__(entries)
        self.pops = 0

    @override
    def pop(self, index: SupportsIndex = -1, /) -> Assignment[str, int]:
        self.pops += 1
        return super().pop(index)


def test_fully_removed_package_does_not_pop_individual_entries() -> None:
    solution: PartialSolution[str, int] = PartialSolution()
    cause: Incompatibility[str, int] = Incompatibility(
        [], cause=IncompatibilityCause.ROOT
    )
    solution.decide("removed", 1)
    for _ in range(8):
        solution.derive("removed", Range.full(), positive=True, cause=cause)
    entries = CountingEntries(solution._assignments_by_package["removed"])
    solution._assignments_by_package["removed"] = entries
    retained = solution.assignments_for("removed")

    solution.backtrack(0)

    assert entries.pops == 0
    assert not retained
    assert not solution._assignments_by_package
    assert solution.trail_length == 0
    assert solution.get("removed") is None
    assert not solution.decisions()

    solution.decide("removed", 2)
    assert not retained
    assert len(solution.assignments_for("removed")) == 1
