"""Run the public provider checks on a small graph of integer versions."""

from collections.abc import Mapping
from math import prod

from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider
from nab_resolver.testing import ProviderScenario, check_provider
from nab_resolver.types import RangeProtocol

Graph = dict[str, dict[int, dict[str, Range[int]]]]


class FixtureProvider(BaseProvider[str, int]):
    """Adapt a finite fixture with ordinary newest-first candidate ordering."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        return next(
            (
                version
                for version in sorted(self.graph.get(package, {}), reverse=True)
                if version in version_range
            ),
            None,
        )

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return self.choose_version(package, version_range) is not None

    def get_dependencies(
        self, package: str, version: int
    ) -> Mapping[str, RangeProtocol[int]]:
        return self.graph[package][version]

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        return len(self.graph.get(package, {}))

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        return None


def check_fixture(
    graph: Graph,
    provider: type[FixtureProvider] = FixtureProvider,
    *,
    constraints: dict[str, Range[int]] | None = None,
) -> None:
    """Exercise one test provider through the public third-party check."""
    scenario = ProviderScenario[str, int](
        "custom", {"app": Range.full()}, graph, constraints=constraints
    )
    check_provider(
        lambda _scenario: provider(graph),
        scenario,
        range_type=Range,
        root_version=0,
        max_combinations=prod(max(1, len(versions)) for versions in graph.values()),
    )
