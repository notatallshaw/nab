"""Property tests for ``VersionRange.from_bounds`` and ``VersionRange.snap_bounds``.

Over random ascending universes mixing final, pre-release, post, dev,
local, and epoch versions: a ``from_bounds``-built open gap around a listed
version contains that version and no other listed version, and unions of
adjacent gaps coalesce into one interval per maximal run of consecutive
universe indexes. ``snap_bounds`` over a random subset of a universe returns
a subset of the original that agrees with it on every given version.
"""

from __future__ import annotations

import bisect
from functools import reduce

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.version import Version

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

_POOL = sorted(
    Version(text)
    for text in (
        "0.1",
        "1.0.dev1",
        "1.0.dev2",
        "1.0a1",
        "1.0a2",
        "1.0b1",
        "1.0rc1",
        "1.0",
        "1.0+cu118",
        "1.0.post1",
        "1.1",
        "2.0b1",
        "2.0",
        "2.0+local",
        "2.0.post1",
        "3.0",
        "1!0.5",
        "1!1.0.dev3",
        "1!1.0",
        "2!0.1",
    )
)


def _gap(universe: list[Version], version: Version) -> VersionRange:
    """The open gap around ``version`` between its listed neighbors."""
    below = bisect.bisect_left(universe, version)
    above = bisect.bisect_right(universe, version)
    prev = universe[below - 1] if below else None
    nxt = universe[above] if above < len(universe) else None
    return VersionRange.from_bounds(prev, nxt, include_lower=False, include_upper=False)


@st.composite
def _universes(draw: st.DrawFn) -> list[Version]:
    return sorted(
        draw(st.lists(st.sampled_from(_POOL), min_size=1, max_size=8, unique=True))
    )


@st.composite
def _universes_with_subset(draw: st.DrawFn) -> tuple[list[Version], list[int]]:
    universe = draw(_universes())
    indexes = sorted(
        draw(st.sets(st.integers(min_value=0, max_value=len(universe) - 1), min_size=1))
    )
    return universe, indexes


@PROPERTY_SETTINGS
@given(universe=_universes())
def test_gap_contains_self_and_no_other_listed(universe: list[Version]) -> None:
    for position, version in enumerate(universe):
        gap = _gap(universe, version)
        assert version in gap
        for other_position, other in enumerate(universe):
            if other_position != position:
                assert gap.is_disjoint(VersionRange.singleton(other))


@PROPERTY_SETTINGS
@given(pair=_universes_with_subset())
def test_union_of_adjacent_gaps_coalesces(
    pair: tuple[list[Version], list[int]],
) -> None:
    universe, indexes = pair
    gaps = [_gap(universe, universe[index]) for index in indexes]
    union = reduce(lambda left, right: left | right, gaps)
    runs = sum(
        1
        for position, index in enumerate(indexes)
        if position == 0 or index != indexes[position - 1] + 1
    )
    assert len(union._bounds) == runs


@PROPERTY_SETTINGS
@given(pair=_universes_with_subset())
def test_snap_bounds_is_subset_and_agrees_on_universe(
    pair: tuple[list[Version], list[int]],
) -> None:
    universe, indexes = pair
    original = reduce(
        lambda left, right: left | right,
        (_gap(universe, universe[index]) for index in indexes),
    )
    simplified = original.snap_bounds(universe)
    assert simplified.is_subset(original)
    for version in universe:
        assert (version in simplified) == (version in original)
