"""Differential property tests for ``VersionRange.relation``.

Over random ranges, ``relation`` is checked against both equivalent
expressions: ``(is_subset, is_disjoint)`` asked separately, and
``(is_subset, (self & other).is_empty)``.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_python._vendor.packaging.ranges import RangeRelation, VersionRange
from nab_python._vendor.packaging.specifiers import SpecifierSet

from .strategies import PROPERTY_SETTINGS, range_specs

pytestmark = pytest.mark.property


@st.composite
def _ranges(draw: st.DrawFn) -> VersionRange:
    """A range under the default pre-release policy."""
    return SpecifierSet(draw(range_specs())).to_range()


@given(left=_ranges(), right=_ranges())
@PROPERTY_SETTINGS
def test_relation_matches_the_separate_predicates(
    left: VersionRange, right: VersionRange
) -> None:
    """relation agrees with is_subset and is_disjoint asked separately."""
    assert left.relation(right) is RangeRelation(
        (left.is_subset(right), left.is_disjoint(right))
    )


@given(left=_ranges(), right=_ranges())
@PROPERTY_SETTINGS
def test_relation_matches_the_intersection_form(
    left: VersionRange, right: VersionRange
) -> None:
    """relation agrees with subset plus an empty intersection."""
    assert left.relation(right) is RangeRelation(
        (left.is_subset(right), (left & right).is_empty)
    )


@given(left=_ranges(), right=_ranges())
@PROPERTY_SETTINGS
def test_relation_reports_both_only_for_an_empty_left(
    left: VersionRange, right: VersionRange
) -> None:
    """Subset and disjoint hold together exactly when left is empty."""
    relation = left.relation(right)
    assert (relation.is_subset and relation.is_disjoint) == left.is_empty
    assert (relation is RangeRelation.EMPTY) == left.is_empty
