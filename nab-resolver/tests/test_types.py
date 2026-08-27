"""Tests for Term and Incompatibility types."""

from __future__ import annotations

import copy
import pickle
from typing import Any

import pytest

from nab_resolver.ranges import Range
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RootRequirement,
    Term,
)


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


class TestRootRequirement:
    def test_origin_defaults_to_none_and_fields_are_readable(self) -> None:
        bare = RootRequirement("foo", Range.at_least(2))
        tagged = RootRequirement("foo", Range.at_least(2), origin="foo>=2 (line 3)")

        assert (bare.package, bare.constraint, bare.origin) == (
            "foo",
            Range.at_least(2),
            None,
        )
        assert tagged.origin == "foo>=2 (line 3)"

    def test_equality_reads_every_field_and_declines_other_types(self) -> None:
        fields: dict[str, Any] = {
            "package": "foo",
            "constraint": Range.at_least(2),
            "origin": "from the lockfile",
        }
        others: dict[str, Any] = {
            "package": "bar",
            "constraint": Range.at_least(3),
            "origin": "from the command line",
        }
        requirement: RootRequirement[str, int] = RootRequirement(**fields)

        assert requirement == RootRequirement(**fields)
        for name, other in others.items():
            assert requirement != RootRequirement(**{**fields, name: other}), name

        assert requirement.__eq__("foo") is NotImplemented

    def test_equal_requirements_hash_alike(self) -> None:
        requirement = RootRequirement("foo", Range.at_least(2), "origin")
        twin = RootRequirement("foo", Range.at_least(2), "origin")

        assert len({requirement, twin}) == 1

    def test_repr_names_the_class_and_every_field(self) -> None:
        requirement = RootRequirement("foo", Range.singleton(2), "origin")

        assert repr(requirement) == (
            "RootRequirement(package='foo', constraint=Range(((2, True, 2, True),)),"
            " origin='origin')"
        )

    def test_fields_cannot_be_reassigned_or_deleted(self) -> None:
        """A requirement is hashable, so a write after use as a key is a bug."""
        requirement = RootRequirement("foo", Range.at_least(2))

        with pytest.raises(AttributeError, match="cannot assign to field 'package'"):
            requirement.package = "bar"
        with pytest.raises(AttributeError, match="cannot delete field 'package'"):
            del requirement.package

    def test_a_requirement_survives_copying_and_pickling(self) -> None:
        requirement = RootRequirement("foo", Range.at_least(2), "origin")

        assert copy.copy(requirement) == requirement
        assert copy.deepcopy(requirement) == requirement
        assert pickle.loads(pickle.dumps(requirement)) == requirement  # noqa: S301

    def test_subscripted_construction_keeps_working(self) -> None:
        """``resolver._as_root_requirements`` builds these subscripted.

        Subscripted construction tries to set ``__orig_class__``, which
        ``__setattr__`` refuses.
        """
        requirement = RootRequirement[str, int]("foo", Range.at_least(2))

        assert requirement.package == "foo"
        assert not hasattr(requirement, "__orig_class__")

    def test_pattern_matching_reads_the_three_fields_positionally(self) -> None:
        match RootRequirement("foo", Range.at_least(2), "origin"):
            case RootRequirement(package, constraint, origin):
                assert (package, constraint, origin) == (
                    "foo",
                    Range.at_least(2),
                    "origin",
                )
