"""Exercise opaque requirement algebra independently of Python packaging."""

import sys
from collections.abc import Mapping
from itertools import product

import pytest

from nab_resolver.errors import ResolutionError
from nab_resolver.predicate_ranges import PredicateAlgebra, PredicateRange
from nab_resolver.resolver import BaseProvider, Resolver
from nab_resolver.types import RangeProtocol


def test_predicate_and_exact_identity_algebra() -> None:
    algebra = PredicateAlgebra[int]()
    odd = algebra.restriction(lambda candidate: candidate % 2 == 1)
    large = algebra.restriction(lambda candidate: candidate >= 4)
    neither = ~(odd | large)
    assert {candidate for candidate in range(8) if candidate in neither} == {0, 2}
    values = [
        neither,
        PredicateRange.empty(),
        PredicateRange.full(),
        odd,
        large,
        PredicateRange.singleton(3),
        odd - PredicateRange.singleton(5),
    ]

    for left, right in product(values, repeat=2):
        expected_left = {candidate for candidate in range(8) if candidate in left}
        expected_right = {candidate for candidate in range(8) if candidate in right}
        relation = left.relation(right)
        assert relation.is_subset is (expected_left <= expected_right)
        assert relation.is_disjoint is expected_left.isdisjoint(expected_right)
        assert left.is_subset((left - right) | right)
        assert (left & right) == (right & left)
        assert (left | right) == (right | left)
        for candidate in range(8):
            assert (candidate in (left & right)) == (
                candidate in left and candidate in right
            )
            assert (candidate in (left | right)) == (
                candidate in left or candidate in right
            )
            assert (candidate in (left - right)) == (
                candidate in left and candidate not in right
            )
            assert (candidate in ~left) == (candidate not in left)


def test_distinct_candidate_singletons_are_disjoint() -> None:
    left = PredicateRange.singleton(("source-a", 1))
    right = PredicateRange.singleton(("source-b", 1))
    assert left.is_disjoint(right)
    assert (left - right) == left
    assert (left - left).is_empty


def test_ranges_reject_mixed_operation_ownership() -> None:
    left = PredicateAlgebra[int]().restriction(lambda candidate: candidate > 0)
    right = PredicateAlgebra[int]().restriction(lambda candidate: candidate < 3)
    for combine in (left.__and__, left.__or__, left.__sub__):
        with pytest.raises(ValueError, match="different operations"):
            combine(right)
    assert left.is_superset(left & PredicateRange.singleton(1))
    for operation in (left.__and__, left.__or__, left.__sub__, left.__eq__):
        assert operation(object()) is NotImplemented


def test_equivalent_formulas_have_equal_dictionary_keys() -> None:
    algebra = PredicateAlgebra[int]()
    positive = algebra.restriction(lambda candidate: candidate > 0)
    small = algebra.restriction(lambda candidate: candidate < 3)
    left, right = positive & small, ~(~positive | ~small)
    assert {left: "value"}[right] == "value"


def test_deep_formulas_do_not_depend_on_python_recursion_limit() -> None:
    algebra = PredicateAlgebra[int]()
    depth = sys.getrecursionlimit() + 10
    restrictions = [
        algebra.restriction(lambda candidate, cutoff=cutoff: candidate >= cutoff)
        for cutoff in range(depth)
    ]
    left = PredicateRange[int].full()
    right = PredicateRange[int].full()
    for position in reversed(range(depth)):
        if position % 2:
            left &= restrictions[position]
        else:
            right &= restrictions[position]
    combined = left & right
    assert depth in combined
    assert depth - 2 not in combined
    assert depth not in ~combined
    assert depth - 2 in ~combined
    assert combined == ~(~left | ~right)


def test_core_reports_the_missing_package_with_an_opaque_range_type() -> None:
    class Provider(BaseProvider[str, int]):
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            return next(
                (
                    version
                    for version in (2, 1)
                    if package == "app" and version in version_range
                ),
                None,
            )

        def has_satisfying_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> bool:
            return self.choose_version(package, version_range) is not None

        def get_dependencies(
            self, package: str, version: int
        ) -> dict[str, PredicateRange[int]]:
            return {"missing": PredicateRange.singleton(3)}

        def prioritize(
            self,
            package: str,
            version_range: RangeProtocol[int],
            conflict_counts: Mapping[str, int],
            culprit_counts: Mapping[str, int] | None = None,
        ) -> int:
            return 0

        def widen_decision(self, package: str, version: int) -> None:
            return None

    resolver = Resolver(Provider(), range_type=PredicateRange)
    with pytest.raises(ResolutionError) as failure:
        resolver.solve({"app": PredicateRange.full()})
    assert "missing" in str(failure.value)
    assert "app" in str(failure.value)
