"""Use ordinary conflict feedback without changing native candidate admission."""

from collections.abc import Iterable, Mapping, Sequence

import pytest

from nab_resolver.candidate_provider import (
    CandidateProvider,
    CandidateRequirement,
    PreparedCandidate,
)
from nab_resolver.priority import compute_tier
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.types import RangeProtocol


class UpperBoundHost:
    """Offer consumers whose metadata rejects the newest earlier hub choice."""

    def __init__(self) -> None:
        self.hubs = [PreparedCandidate(version, (10, version)) for version in (2, 1)]
        self.consumers = [
            PreparedCandidate(version, (20, version)) for version in range(12, 0, -1)
        ]

    def iter_candidates(
        self,
        package: int,
        allowed: RangeProtocol[int],
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> Iterable[PreparedCandidate[int]]:
        yield from self.hubs if package == 10 else self.consumers

    def get_dependencies(
        self, candidate: PreparedCandidate[int]
    ) -> Iterable[CandidateRequirement[int, int]]:
        if candidate in self.consumers:
            yield CandidateRequirement(10, Range.less_than(2), object())

    def priority(
        self,
        package: int,
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> int:
        return package


def make_provider(
    host: UpperBoundHost, *, query: bool = False, conflict: bool = False
) -> CandidateProvider[int, int]:
    """Keep both root preferences and candidate catalogs identical across policies."""
    return CandidateProvider(
        host,
        [CandidateRequirement(package, Range.full(), object()) for package in (10, 20)],
        query_feedback=query,
        conflict_feedback=conflict,
    )


@pytest.mark.parametrize(
    ("affected", "culprit", "others", "forced", "expected"),
    [
        (0, 0, None, False, 1),
        (4, 4, {2: 0}, False, 1),
        (5, 20, {2: 1}, True, 0),
        (0, 5, {}, False, 2),
        (0, 8, {2: 4}, False, 1),
        (0, 9, {2: 4}, False, 2),
        (0, 5, None, False, 1),
        (0, 0, None, True, 2),
    ],
)
def test_shared_conflict_tier_boundaries(
    affected: int,
    culprit: int,
    others: Mapping[int, int] | None,
    forced: bool,
    expected: int,
) -> None:
    assert (
        compute_tier(1, affected, culprit, others, force_backtracked=forced) == expected
    )


def test_feedback_defaults_keep_the_host_preference() -> None:
    provider = make_provider(UpperBoundHost())
    assert provider.prioritize(10, Range.full(), {10: 20}, {10: 50}) == 10
    assert provider.prioritize(20, Range.full(), {10: 20}, {10: 50}) == 20


def test_conflict_feedback_reorders_without_changing_candidates() -> None:
    host = UpperBoundHost()
    provider = make_provider(host, conflict=True)
    counts = {20: 5}
    culprits = {10: 8, 20: 2}
    assert provider.prioritize(
        20, Range.full(), counts, culprits
    ) < provider.prioritize(10, Range.full(), counts, culprits)
    assert provider.choose_version(10, Range.full()) == 2
    assert provider.prepared(10, 2) is host.hubs[0]
    assert provider.prioritize(10, Range.full(), {}, None) == (1, 10)


def test_query_parent_and_target_feedback_precede_conflict_tiers() -> None:
    host = UpperBoundHost()
    provider = make_provider(host, query=True, conflict=True)
    assert provider.choose_version(20, Range.full()) == 12
    provider.get_dependencies(20, 12)
    provider.receive_partial_solution_hint({}, {20: 12})
    assert provider.receive_contextual_failure(10)

    # The declaring consumer still precedes its failed hub, even if it is a culprit.
    assert provider.prioritize(
        20, Range.full(), {10: 5}, {20: 20}
    ) < provider.prioritize(10, Range.full(), {10: 5}, {20: 20})
    # The failed hub still precedes an unmentioned package with an affected tier.
    assert provider.prioritize(
        10, Range.full(), {30: 5}, {10: 20}
    ) < provider.prioritize(30, Range.full(), {30: 5}, {10: 20})


def test_repeated_upper_bound_conflicts_preserve_pins() -> None:
    rows = []
    for enabled in (False, True):
        host = UpperBoundHost()
        provider = make_provider(host, conflict=enabled)
        resolver = Resolver(provider, availability_generation=lambda: 0)
        solution = resolver.solve(provider.root_requirements())
        rows.append((solution.pins, resolver.stats.decisions, resolver.stats.conflicts))
    assert rows[0][0] == rows[1][0] == {10: 1, 20: 12}
    assert rows[1][1] < rows[0][1]
    assert rows[1][2] < rows[0][2]
