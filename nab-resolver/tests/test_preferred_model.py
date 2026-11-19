"""Preferred model verification consumes isolated state and proves its graph."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from nab_resolver._compat import override
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver, ResolverObserver
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    RootRequirement,
    Term,
)

from .preferred_provider import Graph, GraphProvider


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


def test_dependency_can_force_a_different_preferred_version() -> None:
    provider = GraphProvider({"a": {1: {"b": Range.at_least(2)}}, "b": {1: {}, 2: {}}})
    hint = {"a": 1, "b": 1}
    provider.preferences = hint

    assert Resolver(provider).try_resolve_preferred({"a": Range.full()}, hint) == {
        "a": 1,
        "b": 2,
    }


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
