"""Use one iteration budget for speculative and normal searches."""

from collections.abc import Mapping

import pytest

from nab_resolver._compat import override
from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver
from nab_resolver.types import RangeProtocol


class LinearProvider(BaseProvider[int, int]):
    """Reject the first root candidate, then expose one dependency per decision."""

    def __init__(self, length: int) -> None:
        self.length = length
        self.one = Range.singleton(1)

    def choose_version(
        self, package: int, version_range: RangeProtocol[int]
    ) -> int | None:
        if package == -1:
            return None
        if package == 0 and 2 in version_range:
            return 2
        return 1 if 1 in version_range else None

    def has_satisfying_version(
        self, package: int, version_range: RangeProtocol[int]
    ) -> bool:
        return self.choose_version(package, version_range) is not None

    def get_dependencies(self, package: int, version: int) -> dict[int, Range[int]]:
        if package == 0 and version == 2:
            return {-1: self.one}
        return {package + 1: self.one} if package < self.length else {}

    def prioritize(
        self,
        package: int,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[int, int],
        culprit_counts: Mapping[int, int] | None = None,
    ) -> int:
        return package

    def widen_decision(self, package: int, version: int) -> None:
        return None

    @override
    def is_query_ready(self, package: int) -> bool:
        return True


def test_provisional_search_can_use_the_configured_iteration_budget() -> None:
    length = 10_001
    provider = LinearProvider(length)
    resolver = Resolver(
        provider,
        max_iterations=10_050,
        availability_generation=lambda: 0,
        provisional=True,
    )

    solution = resolver.solve({0: Range.full()})

    assert resolver.provisional_absences == 1
    assert resolver.stats.rounds > 10_000
    assert solution.pins == dict.fromkeys(range(length + 1), 1)


@pytest.mark.parametrize("provisional", [False, True])
def test_iteration_exhaustion_remains_inconclusive_in_both_modes(
    provisional: bool,
) -> None:
    provider = LinearProvider(8)
    resolver = Resolver(
        provider,
        max_iterations=4,
        availability_generation=lambda: 0,
        provisional=provisional,
    )

    with pytest.raises(
        ResolutionError, match="Resolution exceeded 4 iterations"
    ) as caught:
        resolver.solve({0: Range.full()})

    assert caught.value.incompatibility is None
    assert resolver.stats.rounds == 4
    assert resolver.provisional_absences == int(provisional)

    fresh = Resolver(LinearProvider(8), availability_generation=lambda: 0)
    assert fresh.solve({0: Range.full()}).pins == dict.fromkeys(range(9), 1)
