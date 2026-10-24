"""Preferred model verification consumes isolated state and proves its graph."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver._compat import override
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver, ResolverObserver
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    RootRequirement,
    Term,
)

Graph = dict[str, dict[int, dict[str, Range[int]]]]


class GraphProvider(BaseProvider[str, int]):
    """A finite package registry with explicit version preferences and facts."""

    def __init__(self, packages: Graph) -> None:
        self.packages = packages
        self.preferences: dict[str, int] = {}
        self.choices: list[str] = []
        self.reads: list[str] = []
        self.pending: list[Incompatibility[str, int]] = []
        self.forced: list[str] = []
        self.on_choice: Callable[[GraphProvider, str], None] | None = None
        self.on_metadata: Callable[[GraphProvider, str], None] | None = None

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        self.choices.append(package)
        if self.on_choice is not None:
            self.on_choice(self, package)
        versions = self.packages.get(package, {})
        preferred = self.preferences.get(package)
        if (
            preferred is not None
            and preferred in versions
            and preferred in version_range
        ):
            return preferred
        return next(
            (v for v in sorted(versions, reverse=True) if v in version_range), None
        )

    def get_dependencies(self, package: str, version: int) -> Mapping[str, Range[int]]:
        self.reads.append(package)
        if self.on_metadata is not None:
            self.on_metadata(self, package)
        return self.packages[package][version]

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Choose the package with the fewest currently available versions."""
        return sum(
            version in version_range for version in self.packages.get(package, {})
        )

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        """Read availability without changing provider state."""
        return any(
            version in version_range for version in self.packages.get(package, {})
        )

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """Keep dependency facts specific to their selected version."""
        return None

    @override
    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        pending, self.pending = self.pending, []
        return pending

    @override
    def consume_force_backtrack_targets(self) -> list[str]:
        forced, self.forced = self.forced, []
        return forced


class Observer(ResolverObserver[str, int]):
    """Retain only externally visible accepted-decision notifications."""

    def __init__(self) -> None:
        self.decisions: list[tuple[str, int, int]] = []

    @override
    def on_decision(self, package: str, version: int, level: int) -> None:
        self.decisions.append((package, version, level))


def test_cycle_and_unused_hints_preserve_reachable_pins() -> None:
    provider = GraphProvider(
        {"a": {1: {"b": Range.singleton(2)}}, "b": {2: {"a": Range.full()}}}
    )
    observer = Observer()
    resolver = Resolver(provider, observer=observer)

    assert resolver.try_resolve_preferred(
        {"a": Range.full()}, {"a": 1, "b": 2, "unused": 9}
    ) == {"a": 1, "b": 2}
    assert provider.reads == ["a", "b"]
    assert observer.decisions == [("a", 1, 2), ("b", 2, 3)]

    with pytest.raises(RuntimeError):
        resolver.resolve({"a": Range.full()})
    with pytest.raises(RuntimeError):
        resolver.try_resolve_preferred({"a": Range.full()}, {"a": 1})


@pytest.mark.parametrize("hint", [{}, {"a": 1}])
def test_missing_or_unpreferred_choice_refuses_without_notifications(
    hint: dict[str, int],
) -> None:
    observer = Observer()
    resolver = Resolver(GraphProvider({"a": {1: {}, 2: {}}}), observer=observer)

    assert resolver.try_resolve_preferred({"a": Range.full()}, hint) is None
    assert observer.decisions == []
    assert resolver.observer is observer

    with pytest.raises(RuntimeError):
        resolver.resolve({"a": Range.full()})


def test_no_available_version_refuses() -> None:
    resolver = Resolver(GraphProvider({}))

    assert resolver.try_resolve_preferred({"a": Range.full()}, {"a": 1}) is None


def test_previously_used_resolver_cannot_be_a_trial() -> None:
    resolver = Resolver(GraphProvider({"a": {1: {}}}))

    assert resolver.resolve({"a": Range.full()}) == {"a": 1}

    with pytest.raises(RuntimeError):
        resolver.try_resolve_preferred({"a": Range.full()}, {"a": 1})


def test_duplicate_roots_and_constraints_are_intersected() -> None:
    roots: list[RootRequirement[str, int]] = [
        RootRequirement("a", Range.at_least(1), "first"),
        RootRequirement("a", Range.less_than(3), "second"),
    ]
    provider = GraphProvider({"a": {1: {}, 2: {}, 3: {}}})

    assert Resolver(provider).try_resolve_preferred(
        roots, {"a": 2}, {"a": Range.at_least(2)}
    ) == {"a": 2}
    assert Resolver(GraphProvider(provider.packages)).try_resolve_preferred(
        roots, {"a": 3}
    ) == {"a": 2}

    impossible: list[RootRequirement[str, int]] = [
        *roots,
        RootRequirement("a", Range.empty(), "third"),
    ]

    assert (
        Resolver(GraphProvider(provider.packages)).try_resolve_preferred(
            impossible, {"a": 2}
        )
        is None
    )


@pytest.mark.parametrize("constrained", [False, True])
def test_requirements_can_force_a_different_preferred_version(
    constrained: bool,
) -> None:
    provider = GraphProvider({"a": {1: {}, 2: {}}})
    provider.preferences = {"a": 1}
    roots = {"a": Range.full() if constrained else Range.singleton(2)}
    constraints = {"a": Range.singleton(2)} if constrained else None

    assert Resolver(provider).try_resolve_preferred(roots, {"a": 1}, constraints) == {
        "a": 2
    }


@pytest.mark.parametrize(
    ("packages", "expected"),
    [
        ({}, None),
        ({"a": {1: {"a": Range.singleton(1)}}}, {"a": 1}),
        ({"a": {1: {"a": Range.singleton(2)}}}, None),
    ],
)
def test_self_dependencies_keep_their_actual_requirement(
    packages: Graph, expected: dict[str, int] | None
) -> None:
    assert (
        Resolver(GraphProvider(packages)).try_resolve_preferred(
            {"a": Range.full()}, {"a": 1}
        )
        == expected
    )


@pytest.mark.parametrize(
    ("cap", "roots", "hint", "expected"),
    [
        (0, {}, {}, None),
        (1, {}, {}, {}),
        (1, {"a": Range.full()}, {"a": 1}, None),
        (2, {"a": Range.full()}, {"a": 1}, {"a": 1}),
    ],
)
def test_trial_respects_the_resolver_iteration_budget(
    cap: int,
    roots: dict[str, Range[int]],
    hint: dict[str, int],
    expected: dict[str, int] | None,
) -> None:
    assert (
        Resolver(
            GraphProvider({"a": {1: {}}}), max_iterations=cap
        ).try_resolve_preferred(roots, hint)
        == expected
    )


@pytest.mark.parametrize("stage", ["choice", "metadata"])
def test_force_backtrack_requests_refuse_the_trial(stage: str) -> None:
    provider = GraphProvider({"a": {1: {}}})

    def force(current: GraphProvider, _package: str) -> None:
        current.forced.append("a")

    if stage == "choice":
        provider.on_choice = force
    else:
        provider.on_metadata = force

    assert (
        Resolver(provider).try_resolve_preferred({"a": Range.full()}, {"a": 1}) is None
    )


@pytest.mark.parametrize("stage", ["choice", "metadata"])
def test_pending_facts_must_be_satisfied_by_the_complete_model(stage: str) -> None:
    provider = GraphProvider({"a": {1: {}}, "b": {1: {}}})
    forbidden: Incompatibility[str, int] = Incompatibility(
        [Term("a", Range.singleton(1)), Term("b", Range.singleton(1))],
        IncompatibilityCause.DEPENDENCY,
    )

    def emit(current: GraphProvider, package: str) -> None:
        if package == "b":
            current.pending.append(forbidden)

    if stage == "choice":
        provider.on_choice = emit
    else:
        provider.on_metadata = emit

    assert (
        Resolver(provider).try_resolve_preferred(
            {"a": Range.full(), "b": Range.full()}, {"a": 1, "b": 1}
        )
        is None
    )


def test_late_constraint_keeps_the_provider_choice_and_ignores_unreachable_constraints() -> (
    None
):
    provider = GraphProvider({"a": {1: {}, 2: {}}, "b": {1: {"a": Range.at_least(2)}}})
    hint = {"a": 2, "b": 1}

    assert (
        Resolver(provider).try_resolve_preferred(
            {"a": Range.full(), "b": Range.full()},
            hint,
            {"a": Range.at_most(2), "unused": Range.empty()},
        )
        == hint
    )
    assert provider.choices == ["a", "b", "a"]


def test_late_conflicting_dependency_refuses() -> None:
    provider = GraphProvider({"a": {1: {}}, "b": {1: {"a": Range.at_least(2)}}})

    assert (
        Resolver(provider).try_resolve_preferred(
            {"a": Range.full(), "b": Range.full()}, {"a": 1, "b": 1}
        )
        is None
    )


def test_late_policy_change_can_refuse_a_still_valid_version() -> None:
    class RefiningProvider(GraphProvider):
        @override
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            if package == "a":
                return 2 if 1 in version_range else 3
            return super().choose_version(package, version_range)

    provider = RefiningProvider(
        {"a": {1: {}, 2: {}, 3: {}}, "b": {1: {"a": Range.at_least(2)}}}
    )

    assert (
        Resolver(provider).try_resolve_preferred(
            {"a": Range.full(), "b": Range.full()}, {"a": 2, "b": 1}
        )
        is None
    )


@pytest.mark.parametrize("signal_kind", ["backtrack", "clause"])
def test_late_policy_recheck_honors_new_provider_requests(signal_kind: str) -> None:
    provider = GraphProvider({"a": {1: {}, 2: {}}, "b": {1: {"a": Range.at_least(2)}}})

    def emit(current: GraphProvider, package: str) -> None:
        if package == "a" and current.choices.count("a") == 2:
            if signal_kind == "backtrack":
                current.forced.append("a")
            else:
                current.pending.append(
                    Incompatibility(
                        [Term("a", Range.singleton(2))], IncompatibilityCause.DEPENDENCY
                    )
                )

    provider.on_choice = emit

    assert (
        Resolver(provider).try_resolve_preferred(
            {"a": Range.full(), "b": Range.full()}, {"a": 2, "b": 1}
        )
        is None
    )


def test_callback_failure_restores_the_observer() -> None:
    observer = Observer()
    provider = GraphProvider({"a": {1: {}}})

    def fail(_provider: GraphProvider, _package: str) -> None:
        raise ValueError("invalid registry metadata")

    provider.on_metadata = fail
    resolver = Resolver(provider, observer=observer)
    with pytest.raises(ValueError, match="invalid registry metadata"):
        resolver.try_resolve_preferred({"a": Range.full()}, {"a": 1})

    assert resolver.observer is observer
    assert observer.decisions == []


@st.composite
def registries(draw: st.DrawFn) -> Graph:
    """Generate finite forward dependency graphs with optional version bounds."""
    names = ("a", "b", "c", "d")
    result: Graph = {}
    for index, name in enumerate(names):
        versions = {}
        for version in (1, 2):
            dependencies = {}
            for dependency in names[index + 1 :]:
                if draw(st.booleans()):
                    dependencies[dependency] = Range.at_least(draw(st.integers(1, 2)))
            versions[version] = dependencies
        result[name] = versions
    return result


@given(registries())
def test_preferred_verification_matches_an_ordinary_success(packages: Graph) -> None:
    roots = {"a": Range.full()}
    expected = Resolver(GraphProvider(packages)).resolve(roots)
    provider = GraphProvider(packages)
    provider.preferences = expected

    assert Resolver(provider).try_resolve_preferred(roots, expected) == expected


def test_late_recheck_receives_complete_immutable_snapshots() -> None:
    class HintProvider(GraphProvider):
        def __init__(self, packages: Graph) -> None:
            super().__init__(packages)
            self.hints: list[
                tuple[Mapping[str, RangeProtocol[int]], Mapping[str, int]]
            ] = []

        @override
        def receive_partial_solution_hint(
            self,
            positive_ranges: Mapping[str, RangeProtocol[int]],
            decisions: Mapping[str, int],
        ) -> None:
            self.hints.append((positive_ranges, decisions))

    provider = HintProvider({"a": {1: {}, 2: {}}, "b": {1: {"a": Range.at_least(2)}}})
    hint = {"a": 2, "b": 1}

    assert (
        Resolver(provider).try_resolve_preferred(
            {"a": Range.full(), "b": Range.full()}, hint
        )
        == hint
    )
    assert "a" not in provider.hints[0][1]
    assert provider.hints[-1][1]["a"] == 2
    assert provider.hints[-1][1]["b"] == 1
