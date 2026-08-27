"""Differential property test for ``Range.relation``.

``relation`` answers "subset" and "disjoint" in one pass over the two interval
lists.  One oracle composes ``is_subset`` and ``is_disjoint`` instead, so a walk
that disagrees with either predicate fails here.  A second oracle reads
``__contains__`` alone, and catches a fault ``relation`` shares with
``is_subset``.

Every generated interval list satisfies the invariant ``Range.__init__``
documents, which is what the walks assume.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.ranges import (
    NEGATIVE_INFINITY,
    POSITIVE_INFINITY,
    Interval,
    Range,
)
from nab_resolver.types import RangeRelation

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

MAX_INTERVALS = 3
BLOCK_WIDTH = 4
"""Distance between the blocks successive intervals are cut from."""

BLOCK_SPAN = 2
"""How far above its block an interval's bounds may reach.  Below
``BLOCK_WIDTH``, so consecutive intervals always leave a gap."""

PROBES = tuple(step / 2 for step in range(-2, 2 * BLOCK_WIDTH * MAX_INTERVALS))
"""Membership probes at half steps, so every interval the generator can draw
holds one.  The pool reaches past both ends of the blocks."""


@st.composite
def canonical_ranges(draw: st.DrawFn) -> Range[int]:
    """Generate a ``Range[int]`` whose interval list satisfies the invariant.

    Each interval is cut from its own block of values, which is what keeps the
    list sorted and leaves a gap between neighbours.  A degenerate ``[v, v]``
    is drawn inclusive at both ends, the only way it holds a version.  The
    outer bound at either end may be replaced by its infinity sentinel, which
    is how ``Range.full()`` is reached.
    """
    count = draw(st.integers(min_value=0, max_value=MAX_INTERVALS))
    intervals: list[Interval] = []

    for block in range(count):
        base = block * BLOCK_WIDTH
        lower = base + draw(st.integers(min_value=0, max_value=BLOCK_SPAN))
        upper = draw(st.integers(min_value=lower, max_value=base + BLOCK_SPAN))
        if lower == upper:
            intervals.append((lower, True, upper, True))
        else:
            intervals.append((lower, draw(st.booleans()), upper, draw(st.booleans())))

    if intervals and draw(st.booleans()):
        _, _, upper_bound, upper_inclusive = intervals[0]
        intervals[0] = (NEGATIVE_INFINITY, False, upper_bound, upper_inclusive)
    if intervals and draw(st.booleans()):
        lower_bound, lower_inclusive, _, _ = intervals[-1]
        intervals[-1] = (lower_bound, lower_inclusive, POSITIVE_INFINITY, False)

    return Range(tuple(intervals))


def composed_relation(left: Range[int], right: Range[int]) -> RangeRelation:
    """Return the relation composed from ``is_empty``, ``is_subset`` and ``is_disjoint``."""
    if left.is_empty:
        return RangeRelation.EMPTY
    if left.is_subset(right):
        return RangeRelation.SUBSET
    if left.is_disjoint(right):
        return RangeRelation.DISJOINT
    return RangeRelation.OVERLAPPING


def membership_relation(left: Range[int], right: Range[int]) -> RangeRelation:
    """Return the relation from :data:`PROBES` and ``__contains__`` alone.

    Sound on generated ranges because every finite bound is an integer inside
    the probe pool's span and the probes are half steps, so every non-empty
    interval those bounds can build holds a probe.
    """
    held = [probe for probe in PROBES if probe in left]
    if not held:
        return RangeRelation.EMPTY
    if all(probe in right for probe in held):
        return RangeRelation.SUBSET
    if any(probe in right for probe in held):
        return RangeRelation.OVERLAPPING
    return RangeRelation.DISJOINT


class TestRelationMatchesTheSeparatePredicates:
    """``relation`` agrees with ``is_subset`` and ``is_disjoint``, and with membership."""

    @given(left=canonical_ranges(), right=canonical_ranges())
    @PROPERTY_SETTINGS
    def test_relation_matches_the_composition(
        self, left: Range[int], right: Range[int]
    ) -> None:
        assert left.relation(right) is composed_relation(left, right)

    @given(left=canonical_ranges(), right=canonical_ranges())
    @PROPERTY_SETTINGS
    def test_relation_matches_membership(
        self, left: Range[int], right: Range[int]
    ) -> None:
        assert left.relation(right) is membership_relation(left, right)

    @given(range_=canonical_ranges())
    @PROPERTY_SETTINGS
    def test_a_generated_range_is_empty_exactly_when_no_probe_lands_in_it(
        self, range_: Range[int]
    ) -> None:
        """Pins what the emptiness leg of :func:`membership_relation` leans on."""
        assert range_.is_empty == all(probe not in range_ for probe in PROBES)
