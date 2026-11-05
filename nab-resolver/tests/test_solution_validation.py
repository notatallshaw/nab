"""Keep admission checks inside the original PubGrub search and work budget."""

from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.errors import (
    ResolutionError,
    ResolutionLimitError,
    ResolutionTerminatedError,
)
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver, Solution
from nab_resolver.types import IncompatibilityCause

from .property.providers import FuzzProvider


def test_rejected_combinations_reset_for_the_next_solve() -> None:
    graph = {"app": {1: {}, 2: {}}}
    resolver = Resolver(FuzzProvider(graph))
    roots = {"app": Range.full()}
    assert resolver.solve(
        roots, solution_validator=lambda solution: solution.pins["app"] == 1
    ).pins == {"app": 1}
    assert resolver.solve(roots).pins == {"app": 2}


def test_an_empty_solution_can_be_validated_or_rejected() -> None:
    resolver = Resolver(FuzzProvider({}))
    assert (
        resolver.solve({}, solution_validator=lambda solution: not solution.pins).pins
        == {}
    )
    with pytest.raises(ResolutionError, match="rejected the empty solution"):
        resolver.solve({}, solution_validator=lambda solution: False)


def test_validator_errors_are_not_retried() -> None:
    calls = 0

    def validate(solution: Solution[str, int]) -> bool:
        nonlocal calls
        calls += 1
        raise OSError("host metadata failed")

    with pytest.raises(OSError, match="host metadata failed"):
        Resolver(FuzzProvider({"app": {1: {}}})).solve(
            {"app": Range.full()}, solution_validator=validate
        )
    assert calls == 1


def test_validation_does_not_get_a_fresh_solver_budget() -> None:
    resolver = Resolver(
        FuzzProvider({"app": {i: {} for i in range(20)}}), max_iterations=4
    )
    with pytest.raises(ResolutionLimitError, match="4 iterations"):
        resolver.solve({"app": Range.full()}, solution_validator=lambda solution: False)
    assert resolver.stats.rounds == 4


def test_constraints_and_unreachable_packages_keep_their_meaning() -> None:
    graph = {
        "app": {1: {"lib": Range.full()}},
        "lib": {1: {}, 2: {}},
        "unused": {1: {}},
    }
    seen = []

    def validate(solution: Solution[str, int]) -> bool:
        seen.append(dict(solution.pins))
        return solution.pins["lib"] == 1

    result = Resolver(FuzzProvider(graph)).solve(
        {"app": Range.full()},
        {"unused": Range.empty(), "lib": Range.singleton(1)},
        solution_validator=validate,
    )
    assert result.pins == {"app": 1, "lib": 1}
    assert seen == [result.pins]


@given(st.sets(st.tuples(st.integers(1, 2), st.integers(1, 2)), max_size=4))
def test_complete_assignment_rejection_matches_finite_oracle(
    rejected: set[tuple[int, int]],
) -> None:
    graph = {
        "app": {1: {"lib": Range.full()}, 2: {"lib": Range.full()}},
        "lib": {1: {}, 2: {}},
    }
    expected = {
        (app, lib)
        for app, lib in product((1, 2), repeat=2)
        if (app, lib) not in rejected
    }

    def validate(solution: Solution[str, int]) -> bool:
        return (solution.pins["app"], solution.pins["lib"]) not in rejected

    resolver = Resolver(FuzzProvider(graph), max_iterations=100)
    try:
        result = resolver.solve({"app": Range.full()}, solution_validator=validate)
    except ResolutionTerminatedError:
        raise
    except ResolutionError:
        assert not expected
    else:
        assert (result.pins["app"], result.pins["lib"]) in expected


def test_rejection_does_not_exclude_one_member_of_a_valid_combination() -> None:
    graph = {
        "app": {1: {"lib": Range.full()}, 2: {"lib": Range.full()}},
        "lib": {1: {}, 2: {}},
    }
    result = Resolver(FuzzProvider(graph)).solve(
        {"app": Range.full()},
        solution_validator=lambda proposed: proposed.pins == {"app": 1, "lib": 1},
    )
    assert result.pins == {"app": 1, "lib": 1}


@pytest.mark.parametrize(
    "graph",
    [
        {"app": {1: {}}},
        {"app": {1: {"lib": Range.singleton(1)}}, "lib": {1: {}}},
    ],
)
def test_admission_failure_does_not_claim_a_failed_candidate_query(
    graph: dict[str, dict[int, dict[str, Range[int]]]],
) -> None:
    provider = FuzzProvider(graph)
    with pytest.raises(ResolutionError) as caught:
        Resolver(provider).solve(
            {"app": Range.singleton(1)},
            solution_validator=lambda proposed: False,
        )
    report = str(caught.value)
    assert "host rules reject the selection" in report
    assert "no versions of" not in report
    assert provider.choose_version("app", Range.singleton(1)) == 1
    stack = [caught.value.incompatibility]
    rejections = []
    while stack:
        clause = stack.pop()
        if clause is None:
            continue
        if clause.cause is IncompatibilityCause.SOLUTION_REJECTED:
            rejections.append(clause)
        stack.extend((clause.cause_left, clause.cause_right))
    assert rejections


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("mutation", ["remove", "replace", "add"])
def test_validator_cannot_mutate_proposal_pins(accepted: bool, mutation: str) -> None:
    def validate(solution: Solution[str, int]) -> bool:
        if mutation == "remove":
            solution.pins.clear()
        elif mutation == "replace":
            solution.pins["app"] = 100
        else:
            solution.pins["unreachable"] = 1
        return accepted

    resolver = Resolver(FuzzProvider({"app": {1: {}, 2: {}}}))
    with pytest.raises(ValueError, match="must not mutate the proposed solution"):
        resolver.solve({"app": Range.full()}, solution_validator=validate)

    assert not any(
        clause.cause is IncompatibilityCause.SOLUTION_REJECTED
        for clause in resolver.incompatibilities
    )
    assert resolver.solve({"app": Range.full()}).pins == {"app": 2}
