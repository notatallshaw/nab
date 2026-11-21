"""Differential property tests for ``VersionRange.relation``.

Over random ranges, ``relation`` is checked against separate subset and
disjointness calls and against an explicit intersection.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_provider._vendor.packaging.ranges import RangeRelation, VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet

from .settings import PROPERTY_SETTINGS

pytestmark = pytest.mark.property


# The version pool includes pre-releases, development and post releases, local
# versions, and a second epoch.
_RANGE_VERSION_POOL = [
    "0.9",
    "1.0.dev1",
    "1.0a1",
    "1.0rc1",
    "1.0",
    "1.0+local",
    "1.0.post1",
    "1.4",
    "1.5",
    "1.8",
    "1.9.9",
    "2.0",
    "3.0",
    "1!0.5",
    "1!1.0",
]

_ORDERED_OPERATORS = [">=", "<=", ">", "<"]
_EQUALITY_OPERATORS = ["==", "!="]


@st.composite
def _range_clauses(draw: st.DrawFn) -> str:
    """One specifier clause over ``_RANGE_VERSION_POOL``.

    Local versions and ``.*`` wildcards are only legal on some operators,
    so each operator is paired with versions it accepts.
    """
    kind = draw(st.integers(min_value=0, max_value=3))
    if kind == 0:
        operator = draw(st.sampled_from(_ORDERED_OPERATORS))
        version = draw(
            st.sampled_from([v for v in _RANGE_VERSION_POOL if "+" not in v])
        )
        return f"{operator}{version}"
    if kind == 1:
        operator = draw(st.sampled_from(_EQUALITY_OPERATORS))
        return f"{operator}{draw(st.sampled_from(_RANGE_VERSION_POOL))}"
    if kind == 2:
        operator = draw(st.sampled_from(_EQUALITY_OPERATORS))
        base = draw(st.sampled_from(["1", "1.0", "2", "1!1"]))
        return f"{operator}{base}.*"
    return f"~={draw(st.sampled_from(['1.4', '1.0.0', '2.0']))}"


@st.composite
def _range_specs(draw: st.DrawFn) -> str:
    """A specifier set of zero to three clauses, or a ``===`` literal."""
    if draw(st.integers(min_value=0, max_value=7)) == 0:
        literal = draw(st.sampled_from([*_RANGE_VERSION_POOL, "frobnicate"]))
        return f"==={literal}"
    return ",".join(draw(st.lists(_range_clauses(), min_size=0, max_size=3)))


@st.composite
def _ranges(draw: st.DrawFn) -> VersionRange:
    """A range under the default pre-release policy."""
    return SpecifierSet(draw(_range_specs())).to_range()


@given(left=_ranges(), right=_ranges())
@PROPERTY_SETTINGS
def test_relation_matches_the_separate_predicates(
    left: VersionRange, right: VersionRange
) -> None:
    """relation agrees with is_subset and is_disjoint asked separately."""
    relation = left.relation(right)
    assert relation.is_subset == left.is_subset(right)
    assert relation.is_disjoint == left.is_disjoint(right)


@given(left=_ranges(), right=_ranges())
@PROPERTY_SETTINGS
def test_relation_matches_the_intersection_form(
    left: VersionRange, right: VersionRange
) -> None:
    """relation agrees with subset plus an empty intersection."""
    relation = left.relation(right)
    assert relation.is_subset == left.is_subset(right)
    assert relation.is_disjoint == (left & right).is_empty


@given(left=_ranges(), right=_ranges())
@PROPERTY_SETTINGS
def test_relation_reports_both_only_for_an_empty_left(
    left: VersionRange, right: VersionRange
) -> None:
    """Subset and disjoint hold together exactly when left is empty."""
    relation = left.relation(right)
    assert (relation.is_subset and relation.is_disjoint) == left.is_empty
    assert (relation is RangeRelation.EMPTY) == left.is_empty
