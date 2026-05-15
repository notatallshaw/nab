"""Tests for Term and Incompatibility types."""

from __future__ import annotations

from nab_resolver.ranges import Range
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term


class TestTerm:
    def test_positive_term(self) -> None:
        t = Term("foo", Range.at_least(2))
        assert t.package == "foo"
        assert t.is_positive()
        assert 3 in t.constraint

    def test_negative_term(self) -> None:
        t = Term("foo", Range.at_least(2), positive=False)
        assert not t.is_positive()

    def test_satisfies_positive(self) -> None:
        """A positive term is satisfied when the partial solution's range
        for the package is a subset of the term's range."""
        t = Term("foo", Range.at_least(2))
        # If foo is assigned [3, 3], that's a subset of [2, +inf)
        assert t.satisfies(Range.singleton(3))
        # [1, 1] is NOT a subset of [2, +inf)
        assert not t.satisfies(Range.singleton(1))

    def test_satisfies_negative(self) -> None:
        """A negative term is satisfied when the partial solution's range
        for the package doesn't intersect the term's range."""
        t = Term("foo", Range.at_least(5), positive=False)
        # foo assigned [2, 2] doesn't intersect [5, +inf) -> satisfied
        assert t.satisfies(Range.singleton(2))
        # foo assigned [6, 6] intersects [5, +inf) -> NOT satisfied
        assert not t.satisfies(Range.singleton(6))

    def test_negate(self) -> None:
        t = Term("foo", Range.at_least(2))
        n = t.negate()
        assert n.package == "foo"
        assert not n.is_positive()
        assert n.constraint == t.constraint

    def test_intersect_same_package(self) -> None:
        a = Term("foo", Range.at_least(2))
        b = Term("foo", Range.less_than(5))
        c = a.intersect(b)
        assert c is not None
        assert 3 in c.constraint
        assert 6 not in c.constraint

    def test_intersect_two_negatives(self) -> None:
        """not(A) AND not(B) = not(A | B)"""
        a = Term("foo", Range.singleton(2), positive=False)
        b = Term("foo", Range.singleton(3), positive=False)
        result = a.intersect(b)
        assert result is not None
        assert not result.is_positive()
        # The negative range should be the union: {2} | {3}
        assert 2 in result.constraint
        assert 3 in result.constraint

    def test_intersect_positive_and_negative(self) -> None:
        """positive AND not(negative) = positive minus negative"""
        positive = Term("foo", Range.at_least(1))
        negative = Term("foo", Range.singleton(3), positive=False)
        result = positive.intersect(negative)
        assert result is not None
        assert result.is_positive()
        assert 2 in result.constraint
        assert 3 not in result.constraint

    def test_intersect_negative_and_positive(self) -> None:
        """Same as above but with args reversed."""
        negative = Term("foo", Range.singleton(3), positive=False)
        positive = Term("foo", Range.at_least(1))
        result = negative.intersect(positive)
        assert result is not None
        assert result.is_positive()
        assert 2 in result.constraint
        assert 3 not in result.constraint

    def test_intersect_different_packages_returns_none(self) -> None:
        a = Term("foo", Range.at_least(2))
        b = Term("bar", Range.less_than(5))
        assert a.intersect(b) is None


class TestIncompatibility:
    def test_create(self) -> None:
        t1 = Term("foo", Range.at_least(2))
        t2 = Term("bar", Range.less_than(1))
        inc = Incompatibility([t1, t2], cause=IncompatibilityCause.DEPENDENCY)
        assert len(inc.terms) == 2
        assert inc.terms[0].package == "foo"
        assert inc.terms[1].package == "bar"

    def test_single_term_incompatibility(self) -> None:
        """A single-term incompatibility means the package is unavailable
        in that range."""
        t = Term("foo", Range.at_least(99))
        inc = Incompatibility([t], cause=IncompatibilityCause.NO_VERSIONS)
        assert len(inc.terms) == 1
