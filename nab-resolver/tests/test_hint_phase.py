"""Keep host requirement snapshots current before deciding package order."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import pytest

from nab_resolver.candidate_provider import (
    CandidateProvider,
    CandidateRequirement,
    PreparedCandidate,
)
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.types import RangeProtocol


class PinFirstHost:
    """Prefer explicit pins, including dependencies of the latest decision."""

    def __init__(self) -> None:
        self.parent = PreparedCandidate(1, object())
        self.child = PreparedCandidate(1, object())
        self.competitor = PreparedCandidate(1, object())
        self.dependency = CandidateRequirement(20, Range.singleton(1), "==1")
        self.queries: list[int] = []

    def iter_candidates(
        self,
        package: int,
        allowed: RangeProtocol[int],
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> Iterable[PreparedCandidate[int]]:
        self.queries.append(package)
        yield {10: self.parent, 20: self.child, 30: self.competitor}[package]

    def get_dependencies(
        self, candidate: PreparedCandidate[int]
    ) -> Iterable[CandidateRequirement[int, int]]:
        if candidate is self.parent:
            yield self.dependency

    def priority(
        self,
        package: int,
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> tuple[bool, bool]:
        active = requirements.get(package, ())
        return (not any(item.origin == "==1" for item in active), not bool(active))


class HintRecorder(CandidateProvider[int, int]):
    """Retain delivered decisions to compare phase order and snapshot lifetime."""

    def __init__(self, host: PinFirstHost) -> None:
        super().__init__(
            host,
            [
                CandidateRequirement(10, Range.singleton(1), "==1"),
                CandidateRequirement(30, Range.full(), ">=1"),
            ],
        )
        self.hints: list[Mapping[Any, int]] = []

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[int, RangeProtocol[int]],
        decisions: Mapping[int, int],
    ) -> None:
        self.hints.append(decisions)
        super().receive_partial_solution_hint(positive_ranges, decisions)


@pytest.mark.parametrize("dynamic", [False, True])
def test_new_parent_pin_precedes_unpinned_competitor(*, dynamic: bool) -> None:
    host = PinFirstHost()
    provider = HintRecorder(host)
    resolver = Resolver(
        provider, availability_generation=(lambda: 0) if dynamic else None
    )

    result = resolver.solve(provider.root_requirements())

    assert result.pins == {10: 1, 20: 1, 30: 1}
    assert host.queries == [10, 20, 30]
    assert len(provider.hints) == len(host.queries)
    assert [set(hint) & {10, 20, 30} for hint in provider.hints] == [
        set(),
        {10},
        {10, 20},
    ]


def test_deferred_refresh_reads_the_current_parent_requirements() -> None:
    host = PinFirstHost()
    provider = HintRecorder(host)
    active_checks: list[bool] = []
    resolvers: list[Resolver[int, int]] = []

    def generation() -> int:
        """Observe each active refresh without changing availability."""
        if resolvers and resolvers[0].solution.undecided_packages():
            expected = 10 in resolvers[0].solution.decisions()
            active_checks.append((20 in provider.active_requirements()) == expected)
        return 0

    resolver = Resolver(provider, availability_generation=generation)
    resolvers.append(resolver)
    resolver.solve(provider.root_requirements())

    assert active_checks
    assert all(active_checks)


def test_empty_solve_does_not_deliver_a_hint() -> None:
    host = PinFirstHost()
    provider = HintRecorder(host)
    refreshes: list[int] = []

    def generation() -> int:
        refreshes.append(0)
        return 0

    resolver = Resolver(provider, availability_generation=generation)
    before = len(refreshes)

    assert resolver.solve({}).pins == {}

    assert provider.hints == []
    assert host.queries == []
    assert len(refreshes) - before == 1
