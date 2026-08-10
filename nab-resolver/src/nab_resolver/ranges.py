"""Generic version range with interval operations.

A Range represents a set of versions as a canonical list of intervals; see
``Range.__init__`` for what makes a list canonical. Supports intersection,
union, complement, and containment.

This is equivalent to pubgrub-rs's ``version_ranges::Ranges<V>``:
https://github.com/pubgrub-rs/pubgrub/tree/release/version-ranges

The type parameter V can be any ordered, hashable type. The simple test
provider uses int; the Python provider uses packaging.version.Version.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeAlias, cast

from ._compat import override
from .types import RangeRelation, VersionType

if TYPE_CHECKING:
    from collections.abc import Iterable

# Bound once so the hot return paths load a module global instead of a class
# attribute.
_EMPTY_REL = RangeRelation.EMPTY
_SUBSET_REL = RangeRelation.SUBSET
_DISJOINT_REL = RangeRelation.DISJOINT
_OVERLAPPING_REL = RangeRelation.OVERLAPPING

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


def _ends_before(interval: Interval, other: Interval) -> bool:
    """Return whether ``interval`` finishes below everything in ``other``."""
    upper, upper_inclusive = interval[2], interval[3]
    lower, lower_inclusive = other[0], other[1]
    if upper is POSITIVE_INFINITY or lower is NEGATIVE_INFINITY:
        return False
    if upper == lower:
        # They meet at a point, which they share only if both ends include it.
        return not (upper_inclusive and lower_inclusive)
    return bool(upper < lower)


def _advance_left(left: Interval, right: Interval) -> bool:
    """Return whether a walk over two interval lists should step ``left``.

    Whichever interval ends first cannot meet anything further along the other
    list, so it is the one to retire.
    """
    left_upper = left[2]
    right_upper = right[2]
    if left_upper is POSITIVE_INFINITY:
        return right_upper is POSITIVE_INFINITY
    if right_upper is POSITIVE_INFINITY:
        return True
    return bool(left_upper <= right_upper)


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
    """A set of versions represented as a canonical list of intervals.

    Modeled after pubgrub-rs ``version_ranges::Ranges<V>``:
    https://docs.rs/version-ranges/latest/version_ranges/struct.Ranges.html

    Each interval is ``(lower, lower_inclusive, upper, upper_inclusive)``.
    See :meth:`__init__` for the invariant the interval list must satisfy.
    """

    __slots__ = ("_hash", "_intervals")

    def __init__(self, intervals: tuple[Interval, ...] = ()) -> None:
        """Create a range from intervals that already satisfy the invariant.

        The intervals must be sorted by lower bound, must not overlap or
        touch, must each hold at least one version, and must be exclusive at
        ``NEGATIVE_INFINITY`` and ``POSITIVE_INFINITY``.  Every operator
        returns through here, so this neither checks nor normalizes: the
        classmethods and the operators maintain the invariant, and a caller
        that assembles its own tuple owns it.

        Equality and hashing compare interval tuples, so two lists denoting
        the same set must be the same list, and
        ``((1, True, 2, False), (2, True, 3, True))`` is not a legal way to
        write ``[1, 3]``.
        """
        self._intervals = intervals
        self._hash = 0

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
        For a set of versions use :meth:`from_versions`.
        """
        return cls(((version, True, version, True),))

    @classmethod
    def from_versions(cls, versions: Iterable[VersionType]) -> Range[VersionType]:
        """Create a range holding exactly the given versions.

        The iterable is consumed once and equal versions collapse.
        Distinct versions never merge: a range has no notion of one
        version following another, so the result still excludes
        everything strictly between them.

        Cheaper than folding :meth:`singleton` with ``|``.
        """
        # sorted() needs an ordering bound, which VersionType does not declare.
        distinct = sorted(set(cast("Iterable[Any]", versions)))
        return cls(tuple((version, True, version, True) for version in distinct))

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
        """Set difference: versions in self but not in other.

        Carves each of this range's intervals against ``other``'s in a single
        walk of both lists, so the complement of ``other`` is never built.
        """
        if not isinstance(other, Range):
            return NotImplemented

        right_intervals = other._intervals
        right_count = len(right_intervals)
        result: list[Interval] = []
        right_index = 0

        for left in self._intervals:
            lower, lower_inclusive, upper, upper_inclusive = left

            # A right interval below this one is below every later one too.
            while right_index < right_count and _ends_before(
                right_intervals[right_index], left
            ):
                right_index += 1

            fully_covered = False
            scan = right_index

            while scan < right_count:
                right = right_intervals[scan]

                # Carving moves the remainder's lower end only, so its upper end
                # is still left's and this comparison can read left directly.
                if _ends_before(left, right):
                    break

                right_lower, right_lower_inc, right_upper, right_upper_inc = right

                # Whatever of the remainder sits below this right interval survives.
                if right_lower is not NEGATIVE_INFINITY and not _interval_is_empty(
                    lower,
                    lower_inclusive=lower_inclusive,
                    upper=right_lower,
                    upper_inclusive=not right_lower_inc,
                ):
                    result.append(
                        (lower, lower_inclusive, right_lower, not right_lower_inc)
                    )

                if right_upper is POSITIVE_INFINITY:
                    fully_covered = True
                    break

                # Resume above this right interval.
                lower, lower_inclusive = right_upper, not right_upper_inc
                if _interval_is_empty(
                    lower,
                    lower_inclusive=lower_inclusive,
                    upper=upper,
                    upper_inclusive=upper_inclusive,
                ):
                    fully_covered = True
                    break

                scan += 1

            if not fully_covered and not _interval_is_empty(
                lower,
                lower_inclusive=lower_inclusive,
                upper=upper,
                upper_inclusive=upper_inclusive,
            ):
                result.append((lower, lower_inclusive, upper, upper_inclusive))

        return Range(tuple(result))

    def is_subset(self, other: Range[VersionType]) -> bool:
        """Return whether every version in self is also in other.

        Walks both interval lists once and stops at the first uncovered
        interval.  This leans on the invariant: consecutive intervals in
        ``other`` always leave a gap, so an interval of self is covered only
        when a single interval of other holds all of it.
        """
        right_intervals = other._intervals
        right_count = len(right_intervals)
        right_index = 0

        for left in self._intervals:
            while right_index < right_count and _ends_before(
                right_intervals[right_index], left
            ):
                right_index += 1

            if right_index >= right_count:
                return False

            right = right_intervals[right_index]
            lower, lower_inclusive = _max_lower_bound(left, right)
            upper, upper_inclusive = _min_upper_bound(left, right)

            if (lower, lower_inclusive, upper, upper_inclusive) != left:
                return False

        return True

    def is_superset(self, other: Range[VersionType]) -> bool:
        """Return whether every version in other is also in self."""
        return other.is_subset(self)

    def is_disjoint(self, other: Range[VersionType]) -> bool:
        """Return whether self and other share no version.

        Stops at the first shared version rather than building the whole
        intersection.
        """
        left_intervals = self._intervals
        right_intervals = other._intervals
        left_count = len(left_intervals)
        right_count = len(right_intervals)
        left_index = right_index = 0

        while left_index < left_count and right_index < right_count:
            left = left_intervals[left_index]
            right = right_intervals[right_index]

            lower, lower_inclusive = _max_lower_bound(left, right)
            upper, upper_inclusive = _min_upper_bound(left, right)

            if not _interval_is_empty(
                lower,
                lower_inclusive=lower_inclusive,
                upper=upper,
                upper_inclusive=upper_inclusive,
            ):
                return False

            if _advance_left(left, right):
                left_index += 1
            else:
                right_index += 1

        return True

    def relation(self, other: Range[VersionType]) -> RangeRelation:
        """Return how self's members sit against other's.

        The empty range is both a subset and disjoint, so it is answered ahead
        of the walks instead of by running both of them.
        """
        if self.is_empty:
            return _EMPTY_REL
        if self.is_subset(other):
            return _SUBSET_REL
        if self.is_disjoint(other):
            return _DISJOINT_REL
        return _OVERLAPPING_REL

    @override
    def __eq__(self, other: object) -> bool:
        """Test equality by comparing interval tuples."""
        if not isinstance(other, Range):
            return NotImplemented
        return self._intervals == other._intervals

    @override
    def __hash__(self) -> int:
        """Hash the interval tuple once and keep the answer.

        A range is immutable and is hashed over and over as a cache key, and
        each hash walks every interval and every bound in it.  Zero marks "not
        computed yet", so a tuple that really hashes to zero is stored as one.
        """
        cached = self._hash
        if cached == 0:
            cached = hash(self._intervals) or 1
            self._hash = cached
        return cached

    def __getstate__(self) -> tuple[tuple[Interval, ...]]:
        """Return the intervals alone, keeping the memo out of the pickle.

        The infinity sentinels hash as plain strings, so a memo computed in
        one process is wrong in a process running a different
        ``PYTHONHASHSEED``.  The wrapping tuple is what makes the empty range
        survive: protocols 0 and 1 discard a pickle state that is falsy, and
        an empty range's intervals are ``()``.
        """
        return (self._intervals,)

    def __setstate__(self, state: tuple[tuple[Interval, ...]]) -> None:
        """Restore from :meth:`__getstate__`, leaving the hash to be recomputed."""
        (self._intervals,) = state
        self._hash = 0

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
    """Sort intervals by lower bound and merge overlapping or adjacent ones.

    Order, overlap and touching are all it repairs.  An empty interval, a
    reversed one or an inclusive infinity bound comes through untouched unless
    a merge happens to absorb or rebuild it, so this is not a way to normalize
    a list that breaks the ``Range`` invariant.
    """
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
