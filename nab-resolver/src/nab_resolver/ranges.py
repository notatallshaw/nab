"""Generic version range with interval operations.

A Range represents a set of versions as a sorted list of non-overlapping
intervals. Supports intersection, union, complement, and containment.

This is equivalent to pubgrub-rs's ``version_ranges::Ranges<V>``:
https://github.com/pubgrub-rs/pubgrub/tree/release/version-ranges

The type parameter V can be any ordered, hashable type. The simple test
provider uses int; the Python provider uses packaging.version.Version.
"""

from __future__ import annotations

from typing import Any, Generic, TypeAlias

from typing_extensions import override

from .types import VersionType

__all__ = [
    "NEGATIVE_INFINITY",
    "POSITIVE_INFINITY",
    "Bound",
    "Interval",
    "Range",
]


class _NegativeInfinity:
    """Sentinel that sorts before every version.

    Used as the lower bound of unbounded intervals like ``(-inf, 5)``.
    Use the module-level ``NEGATIVE_INFINITY`` constant, not this class.
    """

    def __lt__(self, other: object) -> bool:
        return not isinstance(other, _NegativeInfinity)

    def __le__(self, other: object) -> bool:
        return True

    def __gt__(self, other: object) -> bool:
        return False

    def __ge__(self, other: object) -> bool:
        return isinstance(other, _NegativeInfinity)

    @override
    def __eq__(self, other: object) -> bool:
        """Test equality by comparing interval tuples."""
        return isinstance(other, _NegativeInfinity)

    @override
    def __hash__(self) -> int:
        """Hash based on interval tuples."""
        return hash("_NegativeInfinity")

    @override
    def __repr__(self) -> str:
        return "-inf"


class _PositiveInfinity:
    """Sentinel that sorts after every version.

    Used as the upper bound of unbounded intervals like ``[5, +inf)``.
    Use the module-level ``POSITIVE_INFINITY`` constant, not this class.
    """

    def __lt__(self, other: object) -> bool:
        return False

    def __le__(self, other: object) -> bool:
        return isinstance(other, _PositiveInfinity)

    def __gt__(self, other: object) -> bool:
        return not isinstance(other, _PositiveInfinity)

    def __ge__(self, other: object) -> bool:
        return True

    @override
    def __eq__(self, other: object) -> bool:
        """Test equality by comparing interval tuples."""
        return isinstance(other, _PositiveInfinity)

    @override
    def __hash__(self) -> int:
        """Hash based on interval tuples."""
        return hash("_PositiveInfinity")

    @override
    def __repr__(self) -> str:
        return "+inf"


NEGATIVE_INFINITY = _NegativeInfinity()
POSITIVE_INFINITY = _PositiveInfinity()

# An interval is (lower, lower_inclusive, upper, upper_inclusive)
# where lower/upper can be NEGATIVE_INFINITY/POSITIVE_INFINITY for unbounded.
Bound: TypeAlias = Any  # V | _NegativeInfinity | _PositiveInfinity
Interval: TypeAlias = tuple[Bound, bool, Bound, bool]


def _max_lower_bound(left: Interval, right: Interval) -> tuple[Bound, bool]:
    """Return the higher of two lower bounds (for intersection)."""
    left_lower, left_lower_inc = left[0], left[1]
    right_lower, right_lower_inc = right[0], right[1]
    if left_lower == right_lower:
        return left_lower, left_lower_inc and right_lower_inc
    if left_lower is NEGATIVE_INFINITY or (
        right_lower is not NEGATIVE_INFINITY and left_lower < right_lower
    ):
        return right_lower, right_lower_inc
    return left_lower, left_lower_inc


def _min_upper_bound(left: Interval, right: Interval) -> tuple[Bound, bool]:
    """Return the lower of two upper bounds (for intersection)."""
    left_upper, left_upper_inc = left[2], left[3]
    right_upper, right_upper_inc = right[2], right[3]
    if left_upper == right_upper:
        return left_upper, left_upper_inc and right_upper_inc
    if left_upper is POSITIVE_INFINITY or (
        right_upper is not POSITIVE_INFINITY and left_upper > right_upper
    ):
        return right_upper, right_upper_inc
    return left_upper, left_upper_inc


def _interval_is_empty(
    lower: Bound,
    *,
    lower_inclusive: bool,
    upper: Bound,
    upper_inclusive: bool,
) -> bool:
    """Return True if the interval contains no versions."""
    if lower is NEGATIVE_INFINITY or upper is POSITIVE_INFINITY:
        return False
    if lower > upper:
        return True
    return lower == upper and not (lower_inclusive and upper_inclusive)


class Range(Generic[VersionType]):
    """A set of versions represented as sorted, non-overlapping intervals.

    Modeled after pubgrub-rs ``version_ranges::Ranges<V>``:
    https://docs.rs/version-ranges/latest/version_ranges/struct.Ranges.html

    Each interval is ``(lower, lower_inclusive, upper, upper_inclusive)``.
    The list is sorted by lower bound and intervals do not overlap or touch.
    """

    __slots__ = ("_intervals",)

    def __init__(self, intervals: tuple[Interval, ...] = ()) -> None:
        """Create a range from pre-sorted, non-overlapping intervals."""
        self._intervals = intervals

    @classmethod
    def empty(cls) -> Range[VersionType]:
        """Create a range containing no versions."""
        return cls(())

    @classmethod
    def full(cls) -> Range[VersionType]:
        """Create a range containing all versions.

        Mirrors :meth:`packaging.ranges.VersionRange.full`.
        """
        return cls(((NEGATIVE_INFINITY, False, POSITIVE_INFINITY, False),))

    @classmethod
    def singleton(cls, version: VersionType) -> Range[VersionType]:
        """Create a range containing exactly one version.

        Mirrors :meth:`packaging.ranges.VersionRange.singleton`.
        """
        return cls(((version, True, version, True),))

    @classmethod
    def at_least(cls, version: VersionType) -> Range[VersionType]:
        """Create ``[version, +inf)``."""
        return cls(((version, True, POSITIVE_INFINITY, False),))

    @classmethod
    def greater_than(cls, version: VersionType) -> Range[VersionType]:
        """Create ``(version, +inf)``."""
        return cls(((version, False, POSITIVE_INFINITY, False),))

    @classmethod
    def at_most(cls, version: VersionType) -> Range[VersionType]:
        """Create ``(-inf, version]``."""
        return cls(((NEGATIVE_INFINITY, False, version, True),))

    @classmethod
    def less_than(cls, version: VersionType) -> Range[VersionType]:
        """Create ``(-inf, version)``."""
        return cls(((NEGATIVE_INFINITY, False, version, False),))

    @classmethod
    def between(cls, lower: VersionType, upper: VersionType) -> Range[VersionType]:
        """Create ``[lower, upper)``, or the empty range if ``lower >= upper``."""
        if _interval_is_empty(
            lower, lower_inclusive=True, upper=upper, upper_inclusive=False
        ):
            return cls(())
        return cls(((lower, True, upper, False),))

    @property
    def is_empty(self) -> bool:
        """``True`` if this range contains no versions."""
        return len(self._intervals) == 0

    def __contains__(self, version: object) -> bool:
        """Test whether version falls within this range."""
        for lower, lower_inclusive, upper, upper_inclusive in self._intervals:
            if lower is not NEGATIVE_INFINITY and (
                version < lower or (version == lower and not lower_inclusive)
            ):
                continue
            if upper is not POSITIVE_INFINITY and (
                version > upper or (version == upper and not upper_inclusive)
            ):
                continue
            return True
        return False

    def __and__(self, other: object) -> Range[VersionType]:
        """Compute the intersection of two ranges (versions in both)."""
        if not isinstance(other, Range):
            return NotImplemented
        result: list[Interval] = []
        left_index = right_index = 0
        while left_index < len(self._intervals) and right_index < len(other._intervals):
            left_interval = self._intervals[left_index]
            right_interval = other._intervals[right_index]

            inter_lower, inter_lower_inc = _max_lower_bound(
                left_interval, right_interval
            )
            inter_upper, inter_upper_inc = _min_upper_bound(
                left_interval, right_interval
            )

            if not _interval_is_empty(
                inter_lower,
                lower_inclusive=inter_lower_inc,
                upper=inter_upper,
                upper_inclusive=inter_upper_inc,
            ):
                result.append(
                    (inter_lower, inter_lower_inc, inter_upper, inter_upper_inc)
                )

            # Advance the side with the smaller upper bound
            left_upper = left_interval[2]
            right_upper = right_interval[2]
            if left_upper == right_upper:
                left_index += 1
                right_index += 1
            elif left_upper is POSITIVE_INFINITY or (
                right_upper is not POSITIVE_INFINITY and left_upper > right_upper
            ):
                right_index += 1
            else:
                left_index += 1

        return Range(tuple(result))

    def __or__(self, other: object) -> Range[VersionType]:
        """Union of two ranges (versions in either)."""
        if not isinstance(other, Range):
            return NotImplemented
        all_intervals = list(self._intervals) + list(other._intervals)
        return Range(_normalize_intervals(all_intervals))

    def __invert__(self) -> Range[VersionType]:
        """Complement (versions NOT in this range)."""
        if self.is_empty:
            return Range.full()

        result: list[Interval] = []
        previous_upper: Bound = NEGATIVE_INFINITY
        previous_upper_inclusive = False

        for lower, lower_inclusive, upper, upper_inclusive in self._intervals:
            # Gap between the previous interval's upper and this lower.
            if (
                previous_upper is not NEGATIVE_INFINITY
                or lower is not NEGATIVE_INFINITY
            ):
                gap_lower = previous_upper
                gap_lower_inclusive = (
                    not previous_upper_inclusive
                    and previous_upper is not NEGATIVE_INFINITY
                )
                gap_upper = lower
                gap_upper_inclusive = (
                    not lower_inclusive and lower is not POSITIVE_INFINITY
                )

                if (
                    gap_lower is NEGATIVE_INFINITY
                    or gap_upper is POSITIVE_INFINITY
                    or gap_lower < gap_upper
                    or (
                        gap_lower == gap_upper
                        and gap_lower_inclusive
                        and gap_upper_inclusive
                    )
                ):
                    result.append(
                        (gap_lower, gap_lower_inclusive, gap_upper, gap_upper_inclusive)
                    )

            previous_upper = upper
            previous_upper_inclusive = upper_inclusive

        # Trailing gap after the last interval.
        if previous_upper is not POSITIVE_INFINITY:
            result.append(
                (previous_upper, not previous_upper_inclusive, POSITIVE_INFINITY, False)
            )

        return Range(tuple(result))

    def __sub__(self, other: object) -> Range[VersionType]:
        """Set difference: versions in self but not in other."""
        if not isinstance(other, Range):
            return NotImplemented
        return self & ~other

    def is_subset(self, other: Range[VersionType]) -> bool:
        """Return whether every version in self is also in other."""
        return (self - other).is_empty

    def is_superset(self, other: Range[VersionType]) -> bool:
        """Return whether every version in other is also in self."""
        return other.is_subset(self)

    def is_disjoint(self, other: Range[VersionType]) -> bool:
        """Return whether self and other share no version."""
        return (self & other).is_empty

    @override
    def __eq__(self, other: object) -> bool:
        """Test equality by comparing interval tuples."""
        if not isinstance(other, Range):
            return NotImplemented
        return self._intervals == other._intervals

    @override
    def __hash__(self) -> int:
        """Hash based on interval tuples."""
        return hash(self._intervals)

    @override
    def __repr__(self) -> str:
        """Return a debug representation."""
        return f"Range({self._intervals!r})"

    @override
    def __str__(self) -> str:
        """Return a human-readable representation."""
        if self.is_empty:
            return "<empty>"
        if self._intervals == ((NEGATIVE_INFINITY, False, POSITIVE_INFINITY, False),):
            return "*"
        parts = []
        for lower, lower_inclusive, upper, upper_inclusive in self._intervals:
            if lower == upper and lower_inclusive and upper_inclusive:
                parts.append(str(lower))
            else:
                left_bracket = "[" if lower_inclusive else "("
                right_bracket = "]" if upper_inclusive else ")"
                parts.append(f"{left_bracket}{lower}, {upper}{right_bracket}")
        return " | ".join(parts)

    def __bool__(self) -> bool:
        """Return True if this range is non-empty."""
        return not self.is_empty


def _normalize_intervals(intervals: list[Interval]) -> tuple[Interval, ...]:
    """Sort intervals by lower bound and merge overlapping or adjacent ones."""
    if not intervals:
        return ()

    def sort_key(interval: Interval) -> tuple[Any, ...]:
        lower, lower_inclusive, _upper, _upper_inclusive = interval
        if lower is NEGATIVE_INFINITY:
            return (0,)
        return (1, lower, 0 if lower_inclusive else 1)

    intervals.sort(key=sort_key)

    merged: list[Interval] = [intervals[0]]
    for lower, lower_inclusive, upper, upper_inclusive in intervals[1:]:
        merged_lower, merged_lower_inclusive, merged_upper, merged_upper_inclusive = (
            merged[-1]
        )

        # Infinities at the boundary always overlap; otherwise compare values.
        intervals_overlap = (
            merged_upper is POSITIVE_INFINITY
            or lower is NEGATIVE_INFINITY
            or (
                merged_upper > lower
                or (
                    merged_upper == lower
                    and (merged_upper_inclusive or lower_inclusive)
                )
            )
        )

        if intervals_overlap:
            if merged_upper is POSITIVE_INFINITY or upper is POSITIVE_INFINITY:
                new_upper: Bound = POSITIVE_INFINITY
                new_upper_inclusive = False
            elif merged_upper > upper:
                new_upper, new_upper_inclusive = merged_upper, merged_upper_inclusive
            elif merged_upper == upper:
                new_upper = merged_upper
                new_upper_inclusive = merged_upper_inclusive or upper_inclusive
            else:
                new_upper, new_upper_inclusive = upper, upper_inclusive
            merged[-1] = (
                merged_lower,
                merged_lower_inclusive,
                new_upper,
                new_upper_inclusive,
            )
        else:
            merged.append((lower, lower_inclusive, upper, upper_inclusive))

    return tuple(merged)
