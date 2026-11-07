"""Deliver current solver snapshots before ordering and availability refresh."""

from collections.abc import Mapping

import pytest

from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver
from nab_resolver.types import RangeProtocol


class HintProvider(BaseProvider[int, int]):
    """Expose one parent's pin before an unrelated root is selected."""

    def __init__(self) -> None:
        self.decisions: Mapping[int, int] = {}
        self.hints: list[Mapping[int, int]] = []
        self.queries: list[int] = []
        self.priority_calls = 0

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[int, RangeProtocol[int]],
        decisions: Mapping[int, int],
    ) -> None:
        self.decisions = decisions
        self.hints.append(decisions)

    def choose_version(
        self, package: int, version_range: RangeProtocol[int]
    ) -> int | None:
        self.queries.append(package)
        return 1 if 1 in version_range else None

    def has_satisfying_version(
        self, package: int, version_range: RangeProtocol[int]
    ) -> bool:
        return 1 in version_range

    def get_dependencies(self, package: int, version: int) -> dict[int, Range[int]]:
        return {20: Range[int].singleton(1)} if package == 10 else {}

    def prioritize(
        self,
        package: int,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[int, int],
        culprit_counts: Mapping[int, int] | None = None,
    ) -> int:
        self.priority_calls += 1
        return int(package != 10 and not (package == 20 and 10 in self.decisions))

    def widen_decision(self, package: int, version: int) -> None:
        return None


@pytest.mark.parametrize("dynamic", [False, True])
def test_new_parent_pin_precedes_unpinned_competitor(*, dynamic: bool) -> None:
    provider = HintProvider()
    resolver = Resolver(
        provider, availability_generation=(lambda: 0) if dynamic else None
    )
    result = resolver.solve({10: Range[int].singleton(1), 30: Range[int].full()})
    assert result.pins == {10: 1, 20: 1, 30: 1}
    assert provider.queries == [10, 20, 30]
    assert [set(hint) & {10, 20, 30} for hint in provider.hints] == [
        set(),
        {10},
        {10, 20},
    ]


def test_deferred_refresh_reads_the_current_snapshot() -> None:
    provider = HintProvider()
    observations: list[bool] = []
    resolvers: list[Resolver[int, int]] = []

    def generation() -> int:
        if resolvers and resolvers[0].solution.undecided_packages():
            observations.append(provider.decisions == resolvers[0].solution.decisions())
        return 0

    resolver = Resolver(provider, availability_generation=generation)
    resolvers.append(resolver)
    resolver.solve({10: Range[int].singleton(1), 30: Range[int].full()})
    assert observations
    assert all(observations)


def test_empty_solve_delivers_no_hint_or_query() -> None:
    provider = HintProvider()
    resolver = Resolver(provider, availability_generation=lambda: 0)
    assert resolver.solve({}).pins == {}
    assert provider.hints == []
    assert provider.queries == []
