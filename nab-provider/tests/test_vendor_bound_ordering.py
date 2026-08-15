"""Tests for the vendored patch's ``LowerBound`` / ``UpperBound`` ordering.

The bounds order under ``functools.total_ordering``, with ``LowerBound.__gt__``,
``LowerBound.__le__``, and ``UpperBound.__gt__`` written out because the interval
walks call those three directly. Every pair over a bound pool is checked against
the shim's expansion of ``__lt__`` and ``__eq__``, against the total-order laws,
and against the ``_above`` / ``_below`` predicates the same bounds carry. The
pool holds an unbounded bound spelled both inclusive and exclusive, the pair the
constructor collapses to one point.
"""

from __future__ import annotations

import itertools
import operator
from typing import TYPE_CHECKING, TypeVar

import pytest

from nab_provider._vendor.packaging._ranges import (
    NEG_INF,
    POS_INF,
    BoundaryKind,
    BoundaryVersion,
    LowerBound,
    UpperBound,
)
from nab_provider._vendor.packaging.ranges import (
    RangeRelation,
    _relate_bounds,
    _subset_bounds,
)
from nab_provider._vendor.packaging.version import Version

if TYPE_CHECKING:
    from collections.abc import Callable

V = Version

BoundT = TypeVar("BoundT", LowerBound, UpperBound)

BOUND_VERSIONS = [
    None,
    V("1.0"),
    V("1.0+local"),
    V("1.0.post1"),
    V("2.0"),
    BoundaryVersion(V("1.0"), BoundaryKind.AFTER_LOCALS),
    BoundaryVersion(V("1.0"), BoundaryKind.AFTER_POSTS),
]

LOWER_BOUNDS = [
    LowerBound(version, inclusive)
    for version in BOUND_VERSIONS
    for inclusive in (True, False)
]
UPPER_BOUNDS = [
    UpperBound(version, inclusive)
    for version in BOUND_VERSIONS
    for inclusive in (True, False)
]

#: Versions straddling every point the pool's bounds can cut at.
PROBE_VERSIONS = [
    V(text)
    for text in (
        "0.dev0",
        "0",
        "1.0.dev0",
        "1.0a1",
        "1.0",
        "1.0+local",
        "1.0+zzz",
        "1.0.post0",
        "1.0.post1",
        "1.0.post1+local",
        "1.0.1",
        "2.0",
        "2.0+local",
        "3.0",
    )
]


def _shim(left: BoundT, right: BoundT) -> tuple[bool, bool, bool]:
    """``(gt, ge, le)`` as ``functools.total_ordering`` derives them."""
    less = left < right
    equal = left == right
    return (not less and not equal), (not less), (less or equal)


def _admits_lower(bound: LowerBound, version: Version) -> bool:
    return bound._above is None or bound._above(version)


def _admits_upper(bound: UpperBound, version: Version) -> bool:
    return bound._below is None or bound._below(version)


def _check_total_order(pool: list[BoundT]) -> None:
    for left, right in itertools.product(pool, pool):
        less, equal, greater = left < right, left == right, left > right
        assert [less, equal, greater].count(True) == 1, (left, right)
        assert not (greater and right > left), (left, right)
        assert left <= right or right <= left, (left, right)
    for left, middle, right in itertools.product(pool, pool, pool):
        if left < middle < right:
            assert left < right, (left, middle, right)
        if left <= middle <= right:
            assert left <= right, (left, middle, right)


class TestBoundOrdering:
    @pytest.mark.parametrize(
        ("left", "right"), list(itertools.product(LOWER_BOUNDS, LOWER_BOUNDS))
    )
    def test_lower_bound_matches_total_ordering(
        self, left: LowerBound, right: LowerBound
    ) -> None:
        assert (left > right, left >= right, left <= right) == _shim(left, right)

    @pytest.mark.parametrize(
        ("left", "right"), list(itertools.product(UPPER_BOUNDS, UPPER_BOUNDS))
    )
    def test_upper_bound_matches_total_ordering(
        self, left: UpperBound, right: UpperBound
    ) -> None:
        assert (left > right, left >= right, left <= right) == _shim(left, right)

    def test_the_lower_bound_order_is_total(self) -> None:
        _check_total_order(LOWER_BOUNDS)

    def test_the_upper_bound_order_is_total(self) -> None:
        _check_total_order(UPPER_BOUNDS)

    def test_the_greater_lower_bound_admits_the_narrower_set(self) -> None:
        # intersect_ranges takes max() of two lower bounds as the intersection's
        # floor, which is only the intersection while the order tracks _above.
        for left, right in itertools.product(LOWER_BOUNDS, LOWER_BOUNDS):
            picked = max(left, right)
            for version in PROBE_VERSIONS:
                both = _admits_lower(left, version) and _admits_lower(right, version)
                assert _admits_lower(picked, version) == both, (left, right, version)

    def test_the_lesser_upper_bound_admits_the_narrower_set(self) -> None:
        for left, right in itertools.product(UPPER_BOUNDS, UPPER_BOUNDS):
            picked = min(left, right)
            for version in PROBE_VERSIONS:
                both = _admits_upper(left, version) and _admits_upper(right, version)
                assert _admits_upper(picked, version) == both, (left, right, version)

    def test_unbounded_lower_bounds_ignore_inclusivity(self) -> None:
        # -inf is not a version, so "inclusive of it" has no content: the
        # constructor drops the flag and the two spellings are one point.
        inclusive = LowerBound(None, inclusive=True)
        exclusive = LowerBound(None, inclusive=False)
        assert inclusive.inclusive is False
        assert inclusive == exclusive
        assert not inclusive > exclusive
        assert not exclusive > inclusive
        assert inclusive <= exclusive
        assert exclusive <= inclusive
        assert hash(inclusive) == hash(exclusive)

    def test_unbounded_upper_bounds_ignore_inclusivity(self) -> None:
        inclusive = UpperBound(None, inclusive=True)
        exclusive = UpperBound(None, inclusive=False)
        assert inclusive.inclusive is False
        assert inclusive == exclusive
        assert not inclusive > exclusive
        assert not exclusive > inclusive
        assert inclusive <= exclusive
        assert exclusive <= inclusive
        assert hash(inclusive) == hash(exclusive)

    def test_the_module_infinities_are_canonical(self) -> None:
        assert LowerBound(None, inclusive=True) == NEG_INF
        assert UpperBound(None, inclusive=True) == POS_INF

    def test_max_and_min_keep_the_first_argument_on_a_tie(self) -> None:
        # intersect_ranges reuses whichever bound object won, so a tie must not
        # swap them out.
        pools: list[tuple[list[LowerBound] | list[UpperBound], Callable[..., object]]]
        pools = [(LOWER_BOUNDS, max), (UPPER_BOUNDS, min)]
        for pool, pick in pools:
            for left in pool:
                right = type(left)(left.version, left.inclusive)
                assert pick(left, right) is left
                assert pick(right, left) is right

    def test_the_interval_walks_see_one_unbounded_point(self) -> None:
        # _relate_bounds reads coverage off the identity max() and min() hand
        # back, and _subset_bounds compares the lower bounds outright, so an
        # unbounded end spelled either way has to be the same point to both.
        left = [
            (LowerBound(None, inclusive=True), UpperBound(V("2.0"), inclusive=False))
        ]
        right = [(NEG_INF, POS_INF)]
        assert _relate_bounds(left, right) is RangeRelation.SUBSET
        assert _relate_bounds(right, left) is RangeRelation.OVERLAPPING
        assert _subset_bounds(left, right)

    @pytest.mark.parametrize("compare", [operator.lt, operator.gt, operator.le])
    @pytest.mark.parametrize("cls", [LowerBound, UpperBound])
    def test_the_order_is_the_same_before_and_after_the_key_is_cached(
        self,
        cls: type[BoundT],
        compare: Callable[[BoundT, BoundT], bool],
    ) -> None:
        # A version gains its comparison key on first use, so the first compare
        # runs the operator path and later ones the key path. Any compare caches
        # both keys, so each operator needs a fresh pair to see the cold path.
        versions = ("1.0", "1.0+local", "1.0.post1", "2.0")
        cases = itertools.product(versions, versions, (True, False), (True, False))
        for left_text, right_text, left_inclusive, right_inclusive in cases:
            left = cls(V(left_text), left_inclusive)
            right = cls(V(right_text), right_inclusive)
            assert left.version._key_cache is None
            assert right.version._key_cache is None

            cold = compare(left, right)

            assert left.version._key_cache is not None
            assert right.version._key_cache is not None
            assert compare(left, right) == cold, (left, right)

    @pytest.mark.parametrize("method", ["__lt__", "__gt__", "__ge__", "__le__"])
    @pytest.mark.parametrize("cls", [LowerBound, UpperBound])
    def test_bound_returns_not_implemented_for_other_types(
        self, cls: type[LowerBound | UpperBound], method: str
    ) -> None:
        bound = cls(V("1.0"), inclusive=True)
        assert getattr(bound, method)("1.0") is NotImplemented
        assert bound != "1.0"

    def test_mixing_bound_kinds_is_a_type_error(self) -> None:
        lower = LowerBound(V("1.0"), inclusive=True)
        upper = UpperBound(V("1.0"), inclusive=True)
        with pytest.raises(TypeError):
            _ = lower > upper  # type: ignore[operator]
