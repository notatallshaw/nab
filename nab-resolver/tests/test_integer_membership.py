"""Integer point reuse retains the generic range membership contract."""

from __future__ import annotations

import copy
import gc
import weakref
from typing import TYPE_CHECKING

import pytest

from nab_resolver._compat import override
from nab_resolver.ranges import (
    NEGATIVE_INFINITY,
    POSITIVE_INFINITY,
    IntegerRangeMembership,
    Range,
)

if TYPE_CHECKING:
    from collections.abc import Hashable

    from nab_resolver.ranges import Interval


class _RejectingRange(Range[int]):
    """A supported range subclass with its own membership rule."""

    @override
    def __contains__(self, value: object) -> bool:
        return False


class _UnhashableInt(int):
    """Keep integer comparisons while refusing hashed membership."""

    # The fallback must handle an int that violates its static hash contract.
    __hash__ = None  # pyright: ignore[reportAssignmentType]


class _TrackedFloat(float):
    """Expose the lifetime of a generic range bound through a weak reference."""


@pytest.mark.parametrize(
    ("intervals", "probes", "expected"),
    [
        ((), (0, 1), (False, False)),
        (((1, True, 1, True),), (0, 1, 2), (False, True, False)),
        (
            ((1, True, 2, False), (4, True, 5, False)),
            (1, 2, 3, 4),
            (True, False, False, True),
        ),
        (
            ((1, True, 4, False), (7, True, 9, False)),
            (0, 1, 3, 4, 6, 7, 8, 9),
            (False, True, True, False, False, True, True, False),
        ),
        (
            ((7, True, 9, False), (1, True, 4, False)),
            (1, 4, 7, 9),
            (True, False, True, False),
        ),
        (
            ((5, True, 5, True), (-3, True, -3, True), (5, True, 5, True)),
            (-3, 0, 5, 6),
            (True, False, True, False),
        ),
        (((1, False, 1, True),), (1,), (False,)),
        (((1, True, 1, False),), (1,), (False,)),
        (((1, True, 3, True),), (0, 1, 2, 3, 4), (False, True, True, True, False)),
        (((1, True, 1.0, True),), (0, 1, 2), (False, True, False)),
        (((1.0, True, 1, True),), (0, 1, 2), (False, True, False)),
        (((True, True, True, True),), (0, 1, 2), (False, True, False)),
        (
            ((NEGATIVE_INFINITY, False, POSITIVE_INFINITY, False),),
            (-1, 0, 1),
            (True, True, True),
        ),
        (((1, True, 1, True),), (0.0, 1.0, 2.0), (False, True, False)),
    ],
)
def test_membership_matches_fresh_and_reused_scan(
    intervals: tuple[Interval, ...],
    probes: tuple[Hashable, ...],
    expected: tuple[bool, ...],
) -> None:
    version_range: Range[Hashable] = Range(intervals)
    lookup = IntegerRangeMembership()
    assert tuple(probe in version_range for probe in probes) == expected

    assert tuple(lookup.contains(version_range, probe) for probe in probes) == expected
    assert tuple(lookup.contains(version_range, probe) for probe in probes) == expected

    lookup.clear()
    assert tuple(lookup.contains(version_range, probe) for probe in probes) == expected


def test_range_subclass_retains_its_membership_rule() -> None:
    version_range = _RejectingRange(((1, True, 1, True),))
    lookup = IntegerRangeMembership()

    assert not lookup.contains(version_range, 1)


def test_unhashable_probe_uses_comparisons_after_warming_points() -> None:
    version_range: Range[int] = Range.singleton(1)
    lookup = IntegerRangeMembership()
    assert lookup.contains(version_range, 1)
    probe = _UnhashableInt(1)
    with pytest.raises(TypeError):
        hash(probe)

    assert lookup.contains(version_range, probe)


def test_copied_lookup_retains_queried_owners_until_clear() -> None:
    bound = _TrackedFloat(1.5)
    owner: Range[float] = Range.singleton(bound)
    reference = weakref.ref(bound)
    first = IntegerRangeMembership()
    assert not first.contains(owner, 1)
    lookup = copy.deepcopy(first)
    first.clear()
    assert not lookup.contains(owner, 1)

    del bound, owner
    gc.collect()
    assert reference() is not None

    lookup.clear()
    gc.collect()
    assert reference() is None
