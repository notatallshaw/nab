"""Tests for BaseProvider, the defaults a synchronous provider inherits.

The class turns an eleven-method protocol into a five-method one, so these pin
both halves of that split and then drive a subclass through a real resolve and
a real failure.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from nab_resolver.ranges import Range
from nab_resolver.resolver import (
    BaseProvider,
    ResolutionError,
    Resolver,
    ResolverProvider,
)
from nab_resolver.types import RangeProtocol

# The split BaseProvider's docstring promises, spelled out so a protocol
# method that lands in neither half fails a test rather than a review.
SUPPLIED = frozenset(
    {
        "begin_decision_scan",
        "consume_force_backtrack_targets",
        "consume_pending_clauses",
        "is_ready",
        "narrow_for_display",
        "receive_partial_solution_hint",
    }
)

OWED = frozenset(
    {
        "choose_version",
        "get_dependencies",
        "has_satisfying_version",
        "prioritize",
        "widen_decision",
    }
)

Graph = dict[str, dict[int, dict[str, Range[int]]]]


class NewestFirstProvider(BaseProvider[str, int]):
    """The five owed methods over an in-memory graph, newest version wins."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    def _versions(self, package: str) -> list[int]:
        """Versions of ``package``, newest first."""
        return sorted(self._graph.get(package, {}), reverse=True)

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        return next((v for v in self._versions(package) if v in version_range), None)

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return any(v in version_range for v in self._versions(package))

    def get_dependencies(self, package: str, version: int) -> Mapping[str, Range[int]]:
        return self._graph.get(package, {}).get(version, {})

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        return sum(1 for v in self._versions(package) if v in version_range)

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        return None


class TestTheSplit:
    def test_supplies_exactly_the_documented_defaults(self) -> None:
        defined = {name for name in vars(BaseProvider) if not name.startswith("_")}
        assert defined == SUPPLIED

    def test_leaves_the_owed_methods_to_the_subclass(self) -> None:
        assert OWED.isdisjoint(dir(BaseProvider))

    def test_the_two_halves_cover_the_protocol(self) -> None:
        """A method added to the protocol has to be sorted into one half."""
        declared = {
            name
            for name, value in vars(ResolverProvider).items()
            if not name.startswith("_") and callable(value)
        }
        assert declared == SUPPLIED | OWED


class TestDefaults:
    def test_beginning_a_scan_freezes_nothing(self) -> None:
        assert BaseProvider[str, int]().begin_decision_scan() is None

    def test_every_package_is_ready(self) -> None:
        assert BaseProvider[str, int]().is_ready("anything") is True

    def test_the_partial_solution_hint_is_dropped(self) -> None:
        hint = BaseProvider[str, int]().receive_partial_solution_hint(
            {"foo": Range.at_least(1)}, {"foo": 1}
        )
        assert hint is None

    def test_no_clauses_are_queued(self) -> None:
        assert BaseProvider[str, int]().consume_pending_clauses() == []

    def test_no_backtrack_targets_are_forced(self) -> None:
        assert BaseProvider[str, int]().consume_force_backtrack_targets() == []

    def test_each_drain_hands_back_its_own_list(self) -> None:
        """The resolver mutates what it drains, so a shared list would leak."""
        provider = BaseProvider[str, int]()

        clauses = provider.consume_pending_clauses()
        assert clauses is not provider.consume_pending_clauses()

        targets = provider.consume_force_backtrack_targets()
        assert targets is not provider.consume_force_backtrack_targets()

    def test_display_narrowing_is_the_identity(self) -> None:
        constraint = Range.at_least(2)
        narrowed = BaseProvider[str, int]().narrow_for_display("foo", constraint)
        assert narrowed is constraint


class TestSubclassDrivesARealResolve:
    def test_five_methods_are_enough_to_resolve(self) -> None:
        provider = NewestFirstProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {2: {"bar": Range.at_least(1)}, 1: {}},
                "bar": {2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)

        assert resolver.resolve({"root": Range.singleton(1)}) == {
            "root": 1,
            "foo": 2,
            "bar": 2,
        }

    def test_five_methods_are_enough_to_report_a_conflict(self) -> None:
        """The failure path renders through the inherited ``narrow_for_display``."""
        provider = NewestFirstProvider(
            {
                "root": {1: {"foo": Range.full(), "bar": Range.full()}},
                "foo": {1: {"baz": Range.at_least(2)}},
                "bar": {1: {"baz": Range.less_than(2)}},
                "baz": {2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)

        with pytest.raises(ResolutionError) as excinfo:
            resolver.resolve({"root": Range.singleton(1)})

        assert "baz" in str(excinfo.value)
