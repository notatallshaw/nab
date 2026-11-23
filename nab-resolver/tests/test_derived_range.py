"""Derivation returns its computed effective range for propagation to reuse."""

from __future__ import annotations

from typing import Any

import pytest

from nab_resolver import propagate
from nab_resolver.incompat_index import add_incompatibility
from nab_resolver.partial_solution import PartialSolution
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from .solver_fixtures import DictProvider, FlaggedRange


@pytest.mark.parametrize("range_name", ["Range", "FlaggedRange"])
@pytest.mark.parametrize("positive", [False, True])
def test_propagation_does_not_reread_the_derived_range(
    monkeypatch: pytest.MonkeyPatch, range_name: str, positive: bool
) -> None:
    range_type: Any = Range if range_name == "Range" else FlaggedRange
    resolver = Resolver(DictProvider({}), range_type=range_type)
    constraint = range_type.singleton(2)
    clause = Incompatibility(
        [Term("foo", constraint, positive=not positive)],
        cause=IncompatibilityCause.ROOT,
    )
    add_incompatibility(resolver, clause)
    get = resolver.solution.get
    lookups: list[str] = []

    def record_get(package: str) -> Any:
        lookups.append(package)
        return get(package)

    monkeypatch.setattr(resolver.solution, "get", record_get)

    assert propagate.unit_propagation(resolver, "foo") is None
    # Two evaluations and the old range lookup. The new trail entry supplies
    # the result of derive() directly.
    assert lookups == ["foo"] * 3
    assert resolver.stats.derivations == 1
    assert get("foo") == (constraint if positive else ~constraint)


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        ([(True, Range.at_least(1))], Range.at_least(1)),
        ([(False, Range.at_least(5))], Range.less_than(5)),
        (
            [(True, Range.at_least(1)), (False, Range.at_least(5))],
            Range.between(1, 5),
        ),
        (
            [(False, Range.at_least(5)), (True, Range.at_least(1))],
            Range.between(1, 5),
        ),
        ([(True, Range.empty())], Range.empty()),
        ([(False, Range.full())], Range.empty()),
        (
            [(True, Range.singleton(2)), (False, Range.singleton(2))],
            Range.empty(),
        ),
        ([(True, Range.empty()), (True, Range.full())], Range.empty()),
        ([(True, Range.at_least(1)), (True, Range.at_least(1))], Range.at_least(1)),
    ],
)
def test_derive_returns_the_effective_range(
    steps: list[tuple[bool, Range[int]]], expected: Range[int]
) -> None:
    solution: PartialSolution[str, int] = PartialSolution()
    cause: Incompatibility[str, int] = Incompatibility(
        [], cause=IncompatibilityCause.ROOT
    )
    for positive, constraint in steps:
        epoch = solution.contradiction_epoch
        result = solution.derive("foo", constraint, positive=positive, cause=cause)
        assert result is solution.get("foo")
        assert solution.contradiction_epoch == epoch + int(result.is_empty)

    assert solution.get("foo") == expected
    assert solution.trail_length == len(steps)
    assert solution.assignments_for("foo")[-1].cause is cause


def test_returned_range_survives_later_derivations_and_backtracking() -> None:
    solution: PartialSolution[str, int] = PartialSolution()
    cause: Incompatibility[str, int] = Incompatibility(
        [], cause=IncompatibilityCause.ROOT
    )
    original = solution.derive("foo", Range.at_least(1), positive=True, cause=cause)
    solution.decide("bar", 0)
    narrowed = solution.derive("foo", Range.at_least(5), positive=False, cause=cause)
    solution.backtrack(0)

    assert solution.get("foo") == original == Range.at_least(1)
    assert narrowed == Range.between(1, 5)
    assert solution.get("bar") is None
