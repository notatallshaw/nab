# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
"""Public :class:`VersionRange` API and supporting range helpers.

The :class:`VersionRange` class exposes a set-algebra view of the
versions accepted by a :class:`~packaging.specifiers.Specifier` or
:class:`~packaging.specifiers.SpecifierSet`. Private helpers in this
module also drive the range-filter hot path used by
:meth:`Specifier.contains` / :meth:`Specifier.filter` and
:meth:`SpecifierSet.contains` / :meth:`SpecifierSet.filter`.

.. testsetup::

    from packaging.ranges import VersionRange
    from packaging.specifiers import Specifier, SpecifierSet
    from packaging.version import Version
"""

from __future__ import annotations

import enum
import functools
import typing
from typing import (
    TYPE_CHECKING,
    Any,
    Final,
    Union,
)

from .version import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from .specifiers import Specifier, SpecifierSet


__all__ = ["VersionRange"]


def __dir__() -> list[str]:
    return __all__


#: The smallest possible PEP 440 version. No valid version is less than this.
_MIN_VERSION: Final[Version] = Version("0.dev0")

#: Packed pickle form of a single bound: ``(version_str_or_None,
#: inclusive, kind_or_None)``. Uses only strings, bools, and ``None``
#: so the format stays stable across packaging releases.
_PackedBound = tuple[Union[str, None], bool, Union[str, None]]


def _trim_release(release: tuple[int, ...]) -> tuple[int, ...]:
    """Strip trailing zeros from a release tuple."""
    end = len(release)
    while end > 1 and release[end - 1] == 0:
        end -= 1
    return release if end == len(release) else release[:end]


def _next_prefix_dev0(version: Version) -> Version:
    """Smallest version in the next prefix: ``1.2 -> 1.3.dev0``."""
    release = (*version.release[:-1], version.release[-1] + 1)
    return Version.from_parts(epoch=version.epoch, release=release, dev=0)


def _base_dev0(version: Version) -> Version:
    """The ``.dev0`` of a version's base release: ``1.2 -> 1.2.dev0``."""
    return Version.from_parts(epoch=version.epoch, release=version.release, dev=0)


def _coerce_version(version: Version | str) -> Version | None:
    """Parse *version*; ``None`` if invalid."""
    if not isinstance(version, Version):
        try:
            version = Version(version)
        except InvalidVersion:
            return None
    return version


class _BoundaryKind(enum.Enum):
    """Where a boundary marker sits in the version ordering."""

    AFTER_LOCALS = enum.auto()  # after V+local, before V.post0
    AFTER_POSTS = enum.auto()  # after V.postN, before next release


@functools.total_ordering
class _BoundaryVersion:
    """A synthetic point between two real PEP 440 versions.

    PEP 440 specifier semantics imply boundaries between real versions
    (``<=1.0`` includes ``1.0+local``; ``>1.0`` excludes ``1.0.post0``).
    Relative to a base version V::

        V < V+local < AFTER_LOCALS(V) < V.post0 < AFTER_POSTS(V)

    AFTER_LOCALS is the upper bound of ``<=V``, ``==V``, ``!=V`` (no
    local), and the lower bound of the upper-side range of ``!=V``.
    AFTER_POSTS is the lower bound of ``>V`` (V final or pre-release),
    excluding V's post-releases per PEP 440.
    """

    __slots__ = (
        "_cached_dev",
        "_cached_epoch",
        "_cached_post",
        "_cached_pre",
        "_cached_trimmed_release",
        "_kind",
        "version",
    )

    def __init__(self, version: Version, kind: _BoundaryKind) -> None:
        self.version = version
        self._kind = kind
        self._cached_trimmed_release = _trim_release(version.release)
        self._cached_epoch = version.epoch
        self._cached_pre = version.pre
        self._cached_post = version.post
        self._cached_dev = version.dev

    def _is_family(self, other: Version) -> bool:
        """Is ``other`` a version that this boundary sorts above?"""
        if other.epoch != self._cached_epoch:
            return False
        # Inline release-trim comparison: other.release matches the
        # trimmed release iff its leading slice is equal and any extra
        # components are zero. Avoids _trim_release's tuple allocation.
        other_release = other.release
        trimmed_release = self._cached_trimmed_release
        trimmed_length = len(trimmed_release)
        if len(other_release) < trimmed_length:
            return False
        if other_release[:trimmed_length] != trimmed_release:
            return False
        for i in range(trimmed_length, len(other_release)):
            if other_release[i] != 0:
                return False
        if other.pre != self._cached_pre:
            return False
        if self._kind == _BoundaryKind.AFTER_LOCALS:
            # Local family: same public version, any local label.
            return other.post == self._cached_post and other.dev == self._cached_dev
        # Post family: V itself + any post-release of V.
        return other.dev == self._cached_dev or other.post is not None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _BoundaryVersion):
            return self.version == other.version and self._kind == other._kind
        return NotImplemented

    def __lt__(self, other: _BoundaryVersion | Version) -> bool:
        if isinstance(other, _BoundaryVersion):
            if self.version != other.version:
                return self.version < other.version
            return self._kind.value < other._kind.value  # pragma: no cover
        # boundary < other_version iff V < other AND other not in family.
        # The cheap V >= other path short-circuits before the family check.
        if not (self.version < other):
            return False
        return not self._is_family(other)

    def __gt__(self, other: _BoundaryVersion | Version) -> bool:
        # Defined directly to bypass functools.total_ordering's
        # NotImplemented round-trip on reflected ``Version < boundary``.
        if isinstance(other, _BoundaryVersion):
            if self.version != other.version:
                return self.version > other.version
            return self._kind.value > other._kind.value
        if self.version >= other:
            return True
        return self._is_family(other)

    def __hash__(self) -> int:
        return hash((self.version, self._kind))

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.version!r}, {self._kind.name})"


if TYPE_CHECKING:
    _VersionOrBoundary = Union[Version, _BoundaryVersion, None]


def _make_above_after_posts(version: Version) -> Callable[[Version], bool]:
    """Predicate ``parsed > AFTER_POSTS(v)`` for a lower bound.

    Per PEP 440, ``>V`` excludes V's post-releases unless V is itself
    a post-release. AFTER_POSTS sits above V and every V.postN (with
    or without local), and just below the next release.
    """
    version_ge = version.__ge__
    version_epoch = version.epoch
    version_pre = version.pre
    version_dev = version.dev
    version_release_trimmed = _trim_release(version.release)
    trimmed_length = len(version_release_trimmed)

    def above(parsed: Version) -> bool:
        if version_ge(parsed):
            return False
        # parsed > v cmpkey-wise: above the boundary iff NOT in v's
        # post family.
        if parsed.epoch != version_epoch:
            return True
        parsed_release = parsed.release
        if len(parsed_release) < trimmed_length:
            return True
        if parsed_release[:trimmed_length] != version_release_trimmed:
            return True
        for i in range(trimmed_length, len(parsed_release)):
            if parsed_release[i] != 0:
                return True
        if parsed.pre != version_pre:
            return True
        # In post family iff: same dev as v (covers v itself + v+local),
        # or any post-release (covers v.postN + v.postN+local).
        if parsed.dev == version_dev or parsed.post is not None:
            return False
        # Different dev with no post means parsed sorts before v
        # cmpkey-wise, in which case version_ge returned True already.
        return False  # pragma: no cover

    return above


def _make_above_after_locals(version: Version) -> Callable[[Version], bool]:
    """Predicate ``parsed > AFTER_LOCALS(v)`` for a lower bound.

    Used by the upper-side range of ``!=v`` (when *v* has no local
    segment). AFTER_LOCALS sits above v and every ``v+local`` but
    just below ``v.post0``.
    """
    version_ge = version.__ge__
    version_epoch = version.epoch
    version_pre = version.pre
    version_post = version.post
    version_dev = version.dev
    version_release_trimmed = _trim_release(version.release)
    trimmed_length = len(version_release_trimmed)

    def above(parsed: Version) -> bool:
        if version_ge(parsed):
            return False
        # parsed > v cmpkey-wise: above the boundary iff NOT in v's
        # local family (same public version, any local segment).
        if parsed.epoch != version_epoch:
            return True
        parsed_release = parsed.release
        if len(parsed_release) < trimmed_length:
            return True
        if parsed_release[:trimmed_length] != version_release_trimmed:
            return True
        for i in range(trimmed_length, len(parsed_release)):
            if parsed_release[i] != 0:
                return True
        if parsed.pre != version_pre:
            return True
        if parsed.post != version_post:
            return True
        return parsed.dev != version_dev

    return above


def _make_below_after_locals(version: Version) -> Callable[[Version], bool]:
    """Predicate ``parsed <= AFTER_LOCALS(v)`` for an upper bound.

    Used by ``<=v``, ``==v``, ``!=v`` (no local). ``parsed`` is at or
    below the boundary when it is at or below v cmpkey-wise, or when
    it is in v's local family.
    """
    version_ge = version.__ge__
    version_epoch = version.epoch
    version_pre = version.pre
    version_post = version.post
    version_dev = version.dev
    version_release_trimmed = _trim_release(version.release)
    trimmed_length = len(version_release_trimmed)

    def below(parsed: Version) -> bool:
        if version_ge(parsed):
            return True
        # parsed > v cmpkey-wise: below the boundary iff in v's local
        # family.
        if parsed.epoch != version_epoch:
            return False
        parsed_release = parsed.release
        if len(parsed_release) < trimmed_length:
            return False
        if parsed_release[:trimmed_length] != version_release_trimmed:
            return False
        for i in range(trimmed_length, len(parsed_release)):
            if parsed_release[i] != 0:
                return False
        if parsed.pre != version_pre:
            return False
        if parsed.post != version_post:
            return False
        return parsed.dev == version_dev

    return below


def _make_below_after_posts(version: Version) -> Callable[[Version], bool]:
    """Predicate ``parsed <= AFTER_POSTS(v)`` for an upper bound.

    Mirror of :func:`_make_above_after_posts`. Produced only by
    :meth:`VersionRange.complement` of a range whose lower bound is
    AFTER_POSTS(v). ``parsed`` is at or below the boundary when it is
    at or below v cmpkey-wise, or when it is in v's post family.
    """
    version_ge = version.__ge__
    version_epoch = version.epoch
    version_pre = version.pre
    version_dev = version.dev
    version_release_trimmed = _trim_release(version.release)
    trimmed_length = len(version_release_trimmed)

    def below(parsed: Version) -> bool:
        if version_ge(parsed):
            return True
        # parsed > v cmpkey-wise: below the boundary iff in v's post family.
        if parsed.epoch != version_epoch:
            return False
        parsed_release = parsed.release
        if len(parsed_release) < trimmed_length:
            return False
        if parsed_release[:trimmed_length] != version_release_trimmed:
            return False
        for i in range(trimmed_length, len(parsed_release)):
            if parsed_release[i] != 0:
                return False
        if parsed.pre != version_pre:
            return False
        # Same dev as v with no post means parsed sorts <= v already
        # (handled by version_ge above); reach here only with parsed.post set.
        return parsed.dev == version_dev or parsed.post is not None

    return below


@functools.total_ordering
class _LowerBound:
    """Lower bound of a version range.

    A ``version`` of ``None`` is unbounded below (-inf). At equal
    versions, ``[v`` sorts before ``(v`` (inclusive starts earlier).
    """

    __slots__ = ("_above", "inclusive", "version")

    def __init__(self, version: _VersionOrBoundary, inclusive: bool) -> None:
        self.version = version
        self.inclusive = inclusive
        # Pre-bind a predicate "is parsed at or above this lower
        # bound?" for the hot filter / contains loops. One direct
        # call per check, no operator-dispatch chain.
        if version is None:
            self._above: Callable[[Version], bool] | None = None
        elif isinstance(version, _BoundaryVersion):
            # >v produces an AFTER_POSTS lower bound; the upper-side
            # range of !=v produces an AFTER_LOCALS lower bound.
            if version._kind == _BoundaryKind.AFTER_POSTS:
                self._above = _make_above_after_posts(version.version)
            else:
                self._above = _make_above_after_locals(version.version)
        elif inclusive:
            self._above = version.__le__
        else:
            self._above = version.__lt__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _LowerBound):
            return NotImplemented  # pragma: no cover
        return self.version == other.version and self.inclusive == other.inclusive

    def __lt__(self, other: _LowerBound) -> bool:
        if not isinstance(other, _LowerBound):  # pragma: no cover
            return NotImplemented
        # -inf < anything (except -inf itself).
        if self.version is None:
            return other.version is not None
        if other.version is None:
            return False
        if self.version != other.version:
            return self.version < other.version
        # ``[v < (v``: inclusive starts earlier.
        return self.inclusive and not other.inclusive

    def __hash__(self) -> int:
        return hash((self.version, self.inclusive))

    def __repr__(self) -> str:
        bracket = "[" if self.inclusive else "("
        return f"<{self.__class__.__name__} {bracket}{self.version!r}>"


@functools.total_ordering
class _UpperBound:
    """Upper bound of a version range.

    A ``version`` of ``None`` is unbounded above (+inf). At equal
    versions, ``v)`` sorts before ``v]`` (exclusive ends earlier).
    """

    __slots__ = ("_below", "inclusive", "version")

    def __init__(self, version: _VersionOrBoundary, inclusive: bool) -> None:
        self.version = version
        self.inclusive = inclusive
        # Pre-bind a predicate "is parsed at or below this upper
        # bound?". See _LowerBound for the rationale.
        if version is None:
            self._below: Callable[[Version], bool] | None = None
        elif isinstance(version, _BoundaryVersion):
            # Standard specifiers only ever produce AFTER_LOCALS upper
            # bounds (from <=v / ==v / !=v with no local). Complement
            # reverses bound roles, so a range whose lower bound is
            # AFTER_POSTS(v) becomes an upper bound after complementing.
            if version._kind == _BoundaryKind.AFTER_LOCALS:
                self._below = _make_below_after_locals(version.version)
            else:
                self._below = _make_below_after_posts(version.version)
        elif inclusive:
            self._below = version.__ge__
        else:
            self._below = version.__gt__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _UpperBound):
            return NotImplemented  # pragma: no cover
        return self.version == other.version and self.inclusive == other.inclusive

    def __lt__(self, other: _UpperBound) -> bool:
        if not isinstance(other, _UpperBound):  # pragma: no cover
            return NotImplemented
        # Nothing < +inf (except +inf itself).
        if self.version is None:
            return False
        if other.version is None:
            return True
        if self.version != other.version:
            return self.version < other.version
        # ``v) < v]``: exclusive ends earlier.
        return not self.inclusive and other.inclusive

    def __hash__(self) -> int:
        return hash((self.version, self.inclusive))

    def __repr__(self) -> str:
        bracket = "]" if self.inclusive else ")"
        return f"<{self.__class__.__name__} {self.version!r}{bracket}>"


if TYPE_CHECKING:
    #: A single contiguous version range, as a (lower, upper) pair.
    _VersionRange = tuple[_LowerBound, _UpperBound]


_NEG_INF = _LowerBound(None, False)
_POS_INF = _UpperBound(None, False)
_FULL_RANGE: tuple[_VersionRange] = ((_NEG_INF, _POS_INF),)


def _range_is_empty(lower: _LowerBound, upper: _UpperBound) -> bool:
    """True when the range ``(lower, upper)`` contains no versions."""
    if lower.version is None or upper.version is None:
        return False
    if lower.version == upper.version:
        return not (lower.inclusive and upper.inclusive)
    return lower.version > upper.version


def _intersect_ranges(
    left: Sequence[_VersionRange],
    right: Sequence[_VersionRange],
) -> list[_VersionRange]:
    """Intersect two sorted, non-overlapping range lists (two-pointer merge)."""
    result: list[_VersionRange] = []
    left_index = right_index = 0
    while left_index < len(left) and right_index < len(right):
        left_lower, left_upper = left[left_index]
        right_lower, right_upper = right[right_index]

        lower = max(left_lower, right_lower)
        upper = min(left_upper, right_upper)

        if not _range_is_empty(lower, upper):
            result.append((lower, upper))

        # Advance whichever side has the smaller upper bound.
        if left_upper < right_upper:
            left_index += 1
        else:
            right_index += 1

    return result


def _union_ranges(
    left: Sequence[_VersionRange],
    right: Sequence[_VersionRange],
) -> list[_VersionRange]:
    """Union two sorted, non-overlapping range lists.

    Linear merge over the two pre-sorted inputs followed by a single
    coalescing pass: adjacent or overlapping ranges collapse so the
    result is itself sorted and non-overlapping.
    """
    if not left:
        return list(right)
    if not right:
        return list(left)

    # Merge two sorted lists by lower bound (linear, no resort).
    merged_input: list[_VersionRange] = []
    left_index = right_index = 0
    while left_index < len(left) and right_index < len(right):
        if left[left_index][0] <= right[right_index][0]:
            merged_input.append(left[left_index])
            left_index += 1
        else:
            merged_input.append(right[right_index])
            right_index += 1
    merged_input.extend(left[left_index:])
    merged_input.extend(right[right_index:])

    merged: list[_VersionRange] = [merged_input[0]]
    for lower, upper in merged_input[1:]:
        prev_lower, prev_upper = merged[-1]

        # Adjacent ranges merge when the previous upper sits at or past
        # the new lower; +inf/-inf short-circuits collapse the
        # unbounded cases.
        if prev_upper.version is None:
            overlaps = True
        elif lower.version is None:
            overlaps = True  # pragma: no cover (merged_input sorted by lower)
        elif prev_upper.version > lower.version:
            overlaps = True
        elif prev_upper.version == lower.version:
            overlaps = prev_upper.inclusive or lower.inclusive
        else:
            overlaps = False

        if overlaps:
            new_upper = max(prev_upper, upper)
            merged[-1] = (prev_lower, new_upper)
        else:
            merged.append((lower, upper))

    return merged


def _complement_ranges(
    ranges: Sequence[_VersionRange],
) -> list[_VersionRange]:
    """Complement a sorted, non-overlapping range list.

    Yields the gaps between ranges plus a leading gap before the first
    range and a trailing gap after the last. Bound inclusivity flips
    so complement-of-complement round-trips back to the input.
    """
    if not ranges:
        return list(_FULL_RANGE)

    result: list[_VersionRange] = []
    prev_upper: _UpperBound | None = None

    for lower, upper in ranges:
        if prev_upper is None:
            # Leading gap from -inf up to the first range's lower.
            if lower.version is not None:
                gap_upper = _UpperBound(lower.version, not lower.inclusive)
                result.append((_NEG_INF, gap_upper))
        else:
            gap_lower = _LowerBound(prev_upper.version, not prev_upper.inclusive)
            gap_upper = _UpperBound(lower.version, not lower.inclusive)
            # Adjacent ranges in the input are non-touching by
            # construction, so the gap between them is non-empty.
            if not _range_is_empty(gap_lower, gap_upper):  # pragma: no branch
                result.append((gap_lower, gap_upper))
        prev_upper = upper

    # Trailing gap from the final range's upper to +inf.
    if prev_upper is not None and prev_upper.version is not None:
        gap_lower = _LowerBound(prev_upper.version, not prev_upper.inclusive)
        result.append((gap_lower, _POS_INF))

    return result


def _filter_by_ranges(
    ranges: Sequence[_VersionRange],
    iterable: Iterable[Any],
    key: Callable[[Any], Version | str] | None,
    prereleases: bool | None,
) -> Iterator[Any]:
    """Filter *iterable* against precomputed version *ranges*.

    With ``prereleases=None``, the PEP 440 default applies: pre-releases
    are excluded unless no final matches, in which case buffered
    pre-releases come out at the end.
    """
    if prereleases is None:
        # PEP 440 default: yield finals immediately; buffer
        # pre-releases until at least one final has been emitted.
        nonfinal_buffer: list[Any] = []
        found_final = False

        if len(ranges) == 1:
            lower, upper = ranges[0]
            above = lower._above
            below = upper._below
            for item in iterable:
                parsed = _coerce_version(item if key is None else key(item))
                if parsed is None:
                    continue
                if above is not None and not above(parsed):
                    continue
                if below is not None and not below(parsed):
                    continue
                if parsed.is_prerelease:
                    if not found_final:
                        nonfinal_buffer.append(item)
                else:
                    found_final = True
                    yield item
            if not found_final:
                yield from nonfinal_buffer
            return

        for item in iterable:
            parsed = _coerce_version(item if key is None else key(item))
            if parsed is None:
                continue
            for lower, upper in ranges:
                above = lower._above
                if above is not None and not above(parsed):
                    break
                below = upper._below
                if below is None or below(parsed):
                    if parsed.is_prerelease:
                        if not found_final:
                            nonfinal_buffer.append(item)
                    else:
                        found_final = True
                        yield item
                    break
        if not found_final:
            yield from nonfinal_buffer
        return

    exclude_prereleases = prereleases is False

    if len(ranges) == 1:
        # Hot path: most specifiers and small SpecifierSets reduce to
        # a single contiguous range.
        lower, upper = ranges[0]
        above = lower._above
        below = upper._below
        for item in iterable:
            parsed = _coerce_version(item if key is None else key(item))
            if parsed is None:
                continue
            if exclude_prereleases and parsed.is_prerelease:
                continue
            if above is not None and not above(parsed):
                continue
            if below is None or below(parsed):
                yield item
        return

    for item in iterable:
        parsed = _coerce_version(item if key is None else key(item))
        if parsed is None:
            continue
        if exclude_prereleases and parsed.is_prerelease:
            continue
        for lower, upper in ranges:
            above = lower._above
            if above is not None and not above(parsed):
                break
            below = upper._below
            if below is None or below(parsed):
                yield item
                break


def _matches_bounds_only(
    bounds: Sequence[_VersionRange],
    item: Version,
) -> bool:
    """Pure-bounds membership check for a parsed Version."""
    if not bounds:
        return False
    if len(bounds) == 1:
        lower, upper = bounds[0]
        above = lower._above
        if above is not None and not above(item):
            return False
        below = upper._below
        return below is None or below(item)
    for lower, upper in bounds:
        above = lower._above
        if above is not None and not above(item):
            return False
        below = upper._below
        if below is None or below(item):
            return True
    return False


def _bound_match_string(bounds: Sequence[_VersionRange], s: str) -> bool:
    """Bound-only check for the case-folded string *s*.

    Full-range bounds admit any string. Other shapes require *s* to
    parse and fall inside the intervals.
    """
    if tuple(bounds) == _FULL_RANGE:
        return True
    parsed = _coerce_version(s)
    if parsed is None:
        return False
    return _matches_bounds_only(bounds, parsed)


def _lowest_release_at_or_above(
    value: Version | _BoundaryVersion | None,
) -> Version | None:
    """Smallest non-pre-release version at or above *value*, or None."""
    if value is None:
        return None
    if isinstance(value, _BoundaryVersion):
        inner_version = value.version
        if inner_version.is_prerelease:
            # AFTER_LOCALS(1.0a1) -> nearest non-pre is 1.0
            return inner_version.__replace__(pre=None, dev=None, local=None)
        # AFTER_LOCALS(1.0) -> nearest non-pre is 1.0.post0
        # AFTER_LOCALS(1.0.post0) -> nearest non-pre is 1.0.post1
        next_post = (inner_version.post + 1) if inner_version.post is not None else 0
        return inner_version.__replace__(post=next_post, local=None)
    if not value.is_prerelease:
        return value
    # Strip pre/dev to get the final or post-release form.
    return value.__replace__(pre=None, dev=None, local=None)


def _ranges_are_prerelease_only(ranges: Sequence[_VersionRange]) -> bool:
    """``True`` when every range in *ranges* contains only pre-releases.

    Used to detect unsatisfiable specifier sets when ``prereleases=False``:
    if every range is pre-release-only, every contained version is excluded.
    """
    for lower, upper in ranges:
        nearest = _lowest_release_at_or_above(lower.version)
        if nearest is None:
            return False
        if upper.version is None or nearest < upper.version:
            return False
        if nearest == upper.version and upper.inclusive:
            return False
    return True


def _wildcard_ranges(op: str, base: Version) -> list[_VersionRange]:
    """Ranges for ``==V.*`` and ``!=V.*``.

    ``==1.2.*`` -> ``[1.2.dev0, 1.3.dev0)``;  ``!=1.2.*`` -> complement.
    """
    lower = _base_dev0(base)
    upper = _next_prefix_dev0(base)
    if op == "==":
        return [(_LowerBound(lower, True), _UpperBound(upper, False))]
    # !=
    return [
        (_NEG_INF, _UpperBound(lower, False)),
        (_LowerBound(upper, True), _POS_INF),
    ]


def _standard_ranges(op: str, version: Version, has_local: bool) -> list[_VersionRange]:
    """Ranges for the standard PEP 440 operators (no wildcard, no ===).

    *has_local* indicates whether the spec string included a ``+local``
    segment; relevant only for ``==`` / ``!=`` to decide whether the
    upper bound includes V's local family.
    """
    if op == ">=":
        return [(_LowerBound(version, True), _POS_INF)]

    if op == "<=":
        return [
            (
                _NEG_INF,
                _UpperBound(
                    _BoundaryVersion(version, _BoundaryKind.AFTER_LOCALS), True
                ),
            )
        ]

    if op == ">":
        if version.dev is not None:
            # >V.devN: dev versions have no post-releases, so the
            # next real version is V.dev(N+1).
            lower_bound = version.__replace__(dev=version.dev + 1, local=None)
            return [(_LowerBound(lower_bound, True), _POS_INF)]
        if version.post is not None:
            # >V.postN: next real version is V.post(N+1).dev0.
            lower_bound = version.__replace__(post=version.post + 1, dev=0, local=None)
            return [(_LowerBound(lower_bound, True), _POS_INF)]
        # >V (final or pre-release V): exclude V itself, V+local, and
        # every V.postN per PEP 440.
        return [
            (
                _LowerBound(
                    _BoundaryVersion(version, _BoundaryKind.AFTER_POSTS), False
                ),
                _POS_INF,
            )
        ]

    if op == "<":
        # <V excludes pre-releases of V when V is not a pre-release.
        # V.dev0 is the earliest pre-release of V.
        bound = (
            version if version.is_prerelease else version.__replace__(dev=0, local=None)
        )
        if bound <= _MIN_VERSION:
            return []
        return [(_NEG_INF, _UpperBound(bound, False))]

    # ==, !=: local versions of V match when the spec has no local segment.
    after_locals = _BoundaryVersion(version, _BoundaryKind.AFTER_LOCALS)
    upper = version if has_local else after_locals

    if op == "==":
        return [(_LowerBound(version, True), _UpperBound(upper, True))]

    if op == "!=":
        return [
            (_NEG_INF, _UpperBound(version, False)),
            (_LowerBound(upper, False), _POS_INF),
        ]

    if op == "~=":
        prefix = version.__replace__(release=version.release[:-1])
        return [
            (_LowerBound(version, True), _UpperBound(_next_prefix_dev0(prefix), False))
        ]

    raise ValueError(f"Unknown operator: {op!r}")  # pragma: no cover


def _format_lower(bound: _LowerBound) -> str:
    if bound.version is None:
        return "(-inf"
    bracket = "[" if bound.inclusive else "("
    inner = (
        bound.version.version
        if isinstance(bound.version, _BoundaryVersion)
        else bound.version
    )
    return f"{bracket}{inner}"


def _format_upper(bound: _UpperBound) -> str:
    if bound.version is None:
        return "+inf)"
    bracket = "]" if bound.inclusive else ")"
    inner = (
        bound.version.version
        if isinstance(bound.version, _BoundaryVersion)
        else bound.version
    )
    return f"{inner}{bracket}"


def _pack_bound(bound: _LowerBound | _UpperBound) -> _PackedBound:
    """Serialize a bound to a primitive triple. See _PackedBound."""
    bound_version = bound.version
    if bound_version is None:
        return (None, bound.inclusive, None)
    if isinstance(bound_version, _BoundaryVersion):
        return (str(bound_version.version), bound.inclusive, bound_version._kind.name)
    return (str(bound_version), bound.inclusive, None)


def _unpack_bound(
    cls: type[_LowerBound | _UpperBound],
    packed: _PackedBound,
) -> _LowerBound | _UpperBound:
    """Reverse of _pack_bound."""
    version_str, inclusive, kind_name = packed
    if version_str is None:
        return cls(None, inclusive)
    base = Version(version_str)
    if kind_name is not None:
        return cls(_BoundaryVersion(base, _BoundaryKind[kind_name]), inclusive)
    return cls(base, inclusive)


def _restore_version_range(
    packed_bounds: tuple[tuple[_PackedBound, _PackedBound], ...],
    arbitrary: str | None = None,
    admit: tuple[str, ...] | None = None,
    reject: tuple[str, ...] | None = None,
) -> VersionRange:
    """Pickle restorer; bypasses the ``__new__`` guard via ``_build``.

    The ``arbitrary`` arg is the pre-admit/reject slot from earlier
    betas. New pickles pass ``admit`` and ``reject`` instead. The
    matched set is preserved either way.
    """
    bounds = tuple(
        (
            typing.cast("_LowerBound", _unpack_bound(_LowerBound, lower)),
            typing.cast("_UpperBound", _unpack_bound(_UpperBound, upper)),
        )
        for lower, upper in packed_bounds
    )
    if admit is not None or reject is not None:
        return VersionRange._build(
            bounds,
            admit=frozenset(admit or ()),
            reject=frozenset(reject or ()),
        )
    if arbitrary is None:
        return VersionRange._build(bounds)
    # Legacy ``arbitrary`` matched ``{arbitrary}`` if the literal was
    # in bounds, empty otherwise.
    literal_lower = arbitrary.lower()
    legacy_range = VersionRange._build(bounds)
    if literal_lower in legacy_range:
        return VersionRange._build((), admit=frozenset({literal_lower}))
    return VersionRange._build(())


# VersionRange to SpecifierSet conversion is partial: not every range
# has a SpecifierSet form. Examples that have no single specifier:
# - PEP 440 ``<V`` excludes pre-releases of V, so the mathematical
#   complement of ``>=V`` (which keeps those pre-releases) has no
#   single specifier.
# - PEP 440 ``==V`` matches ``V+local`` too, so the strict singleton
#   ``[V, V]`` produced by :meth:`VersionRange.singleton` has none.
# - Disjoint unions whose gap is not a complete ``==V.*`` family or a
#   ``==V`` family cannot be expressed as ``base & !=...``.


def _is_dev0_version(v: Version) -> bool:
    """``True`` when *v* is exactly ``X[.Y]*.dev0``: the form ``<X`` produces."""
    return v.dev == 0 and v.pre is None and v.post is None and v.local is None


class _NotEncodable:
    """Sentinel for "this bound has no PEP 440 specifier representation"."""

    __slots__ = ()


_NOT_ENCODABLE: Final = _NotEncodable()


def _encode_lower(lower: _LowerBound) -> list[str] | _NotEncodable:
    """Encode a lower bound as a list of specifier fragments.

    ``[]`` for ``-inf``, one or more fragments otherwise, or
    ``_NOT_ENCODABLE`` when the shape has no specifier form.
    AFTER_LOCALS lower bounds emit two fragments (``>=V`` plus
    ``!=V``) since the boundary excludes V and every V+local.
    """
    lower_version = lower.version
    if lower_version is None:
        return []
    if isinstance(lower_version, _BoundaryVersion):
        if lower_version._kind == _BoundaryKind.AFTER_POSTS and not lower.inclusive:
            return [f">{lower_version.version}"]
        if lower_version._kind == _BoundaryKind.AFTER_LOCALS:
            # Strictly above V's local family. ``>=V,!=V`` produces
            # ``[V, +inf)`` minus ``[V, AFTER_LOCALS(V)]``, leaving
            # ``(AFTER_LOCALS(V), +inf)``.
            return [f">={lower_version.version}", f"!={lower_version.version}"]
        # AFTER_POSTS lower with inclusive=True is unreachable from
        # any specifier or set-algebra operation; defensive guard.
        return _NOT_ENCODABLE  # pragma: no cover
    if lower.inclusive:
        return [f">={lower_version}"]
    return _NOT_ENCODABLE


def _encode_upper(upper: _UpperBound) -> list[str] | _NotEncodable:
    """Encode an upper bound as a list of specifier fragments.

    ``[]`` for ``+inf``, one or more fragments otherwise, or
    ``_NOT_ENCODABLE`` when the shape has no specifier form.
    """
    upper_version = upper.version
    if upper_version is None:
        return []
    if isinstance(upper_version, _BoundaryVersion):
        if upper_version._kind == _BoundaryKind.AFTER_LOCALS and upper.inclusive:
            return [f"<={upper_version.version}"]
        return _NOT_ENCODABLE
    if not upper.inclusive:
        if _is_dev0_version(upper_version):
            # <V produces upper = V.dev0 (excl); strip the synthetic
            # dev0 to recover the original V.
            return [f"<{upper_version.__replace__(dev=None)}"]
        # V (excl) upper: strictly less than V cmpkey-wise, including
        # V's pre-releases. <=V,!=V produces (-inf, AFTER_LOCALS(V)]
        # minus [V, AFTER_LOCALS(V)], leaving (-inf, V (excl)).
        return [f"<={upper_version}", f"!={upper_version}"]
    return _NOT_ENCODABLE


def _encode_interval(
    lower: _LowerBound,
    upper: _UpperBound,
) -> list[str] | None:
    """Encode one interval as a list of specifier fragments, or ``None``.

    Special-cases ``[V, V]`` (singleton interval) when V carries a
    local segment: ``==V+local`` matches only that literal, so the
    interval round-trips. Without a local, no specifier form exists
    (``==V`` is wider since it also matches ``V+local``).
    """
    if (
        lower.version is not None
        and upper.version is not None
        and not isinstance(lower.version, _BoundaryVersion)
        and not isinstance(upper.version, _BoundaryVersion)
        and lower.inclusive
        and upper.inclusive
        and lower.version == upper.version
        and lower.version.local is not None
    ):
        return [f"=={lower.version}"]
    lower_parts = _encode_lower(lower)
    if isinstance(lower_parts, _NotEncodable):
        return None
    upper_parts = _encode_upper(upper)
    if isinstance(upper_parts, _NotEncodable):
        return None
    return lower_parts + upper_parts


def _detect_not_equal(
    left_upper: _UpperBound,
    right_lower: _LowerBound,
) -> Version | None:
    """If ``[..., V (excl)] [AFTER_LOCALS(V) (excl), ...]`` matches, return V.

    The gap shape ``!=V`` produces when intersected with surrounding
    bounds. Only ``!=V`` pattern that can appear inside a multi-interval
    range.
    """
    if isinstance(left_upper.version, _BoundaryVersion):
        return None
    if left_upper.version is None or left_upper.inclusive:
        return None
    if not isinstance(right_lower.version, _BoundaryVersion):
        return None
    if right_lower.version._kind != _BoundaryKind.AFTER_LOCALS:
        return None
    if right_lower.inclusive:
        # AFTER_LOCALS lower with inclusive=True does not arise from
        # any specifier or set-algebra operation; defensive guard.
        return None  # pragma: no cover
    if right_lower.version.version != left_upper.version:
        # The ``!=V`` pattern is contiguous; mismatched V means a union
        # of unrelated ranges. Defensive.
        return None  # pragma: no cover
    return left_upper.version


def _detect_not_equal_wildcard(
    left_upper: _UpperBound,
    right_lower: _LowerBound,
) -> Version | None:
    """If ``[..., V.dev0 (excl)] [V_next.dev0 (incl), ...]`` matches, return V.

    The gap shape ``!=V.*`` produces. ``V`` and ``V_next`` share an
    epoch and a release prefix differing only in the final component
    being incremented by one. Returns the prefix version (without the
    synthetic ``.dev0``) so the caller can write ``!=V.*``.
    """
    left_upper_v = left_upper.version
    right_lower_v = right_lower.version
    if isinstance(left_upper_v, _BoundaryVersion) or isinstance(
        right_lower_v, _BoundaryVersion
    ):
        return None
    if left_upper_v is None or right_lower_v is None:
        # First-interval upper or last-interval lower at infinity means
        # the interval is the universe and no second interval exists.
        return None  # pragma: no cover
    if left_upper.inclusive or not right_lower.inclusive:
        return None
    if not (_is_dev0_version(left_upper_v) and _is_dev0_version(right_lower_v)):
        return None
    if left_upper_v.epoch != right_lower_v.epoch:
        return None
    left_release = left_upper_v.release
    right_release = right_lower_v.release
    if len(left_release) != len(right_release) or not left_release:
        return None
    # All components except the last must match; the last increments by 1.
    if left_release[:-1] != right_release[:-1]:
        return None
    if right_release[-1] != left_release[-1] + 1:
        return None
    return left_upper_v.__replace__(dev=None)


class VersionRange:
    """A set of :class:`~packaging.version.Version` values, expressed as
    a union of disjoint intervals on the PEP 440 version ordering.

    Construct with :meth:`from_specifier` / :meth:`from_specifier_set`,
    or via :meth:`Specifier.to_range` / :meth:`SpecifierSet.to_range`.
    Compose with :meth:`intersection`, :meth:`union`, :meth:`complement`
    (and the ``&`` / ``|`` / ``~`` operator aliases).

    >>> r = VersionRange.from_specifier_set(SpecifierSet(">=1.0,<2.0"))
    >>> "1.5" in r
    True
    >>> "2.0" in r
    False
    >>> bool(VersionRange.from_specifier_set(SpecifierSet(">=2.0,<1.0")))
    False

    PEP 440's ``===`` operator matches a candidate string verbatim
    (case-insensitive) rather than a set of :class:`Version` values.
    Ranges built from ``===`` specifiers still support membership,
    set operations, and conversion back to a :class:`SpecifierSet`;
    matching follows the literal-equality rule instead of the
    version-ordering rule.
    """

    __slots__ = ("_admit", "_bounds", "_reject")
    _bounds: tuple[_VersionRange, ...]
    #: Case-folded strings the range admits in addition to its bounds.
    #: ``===wat`` produces ``_admit = {"wat"}``.
    _admit: frozenset[str]
    #: Case-folded strings the range rejects. Overrides ``_admit`` and
    #: ``_bounds``. Populated by :meth:`complement` of a range whose
    #: ``_admit`` was non-empty.
    _reject: frozenset[str]

    def __new__(cls, *args: object, **kwargs: object) -> VersionRange:  # noqa: PYI034
        raise TypeError(
            "cannot create 'VersionRange' instances directly; use "
            "VersionRange.from_specifier(), "
            "VersionRange.from_specifier_set(), "
            "Specifier.to_range(), or SpecifierSet.to_range() instead"
        )

    @classmethod
    def _build(
        cls,
        bounds: tuple[_VersionRange, ...],
        admit: frozenset[str] = frozenset(),
        reject: frozenset[str] = frozenset(),
    ) -> VersionRange:
        """Internal factory; bypasses :meth:`__new__`.

        Drops admit literals already covered by bounds and reject
        literals already outside bounds. Reject wins over admit on
        overlap.
        """
        if admit and reject:
            admit = admit - reject
        if admit:
            admit = frozenset(s for s in admit if not _bound_match_string(bounds, s))
        if reject:
            reject = frozenset(s for s in reject if _bound_match_string(bounds, s))
        instance = object.__new__(cls)
        instance._bounds = bounds
        instance._admit = admit
        instance._reject = reject
        return instance

    def _has_literals(self) -> bool:
        """``True`` when ``_admit`` or ``_reject`` is non-empty."""
        return bool(self._admit) or bool(self._reject)

    @classmethod
    def empty(cls) -> VersionRange:
        """Return the empty range. No version satisfies it.

        >>> VersionRange.empty().is_empty
        True
        >>> "1.0" in VersionRange.empty()
        False
        """
        return cls._build(())

    @classmethod
    def full(cls) -> VersionRange:
        """Return the full range. Every PEP 440 version satisfies it.

        >>> "1.0" in VersionRange.full()
        True
        >>> VersionRange.full().is_empty
        False
        """
        return cls._build(_FULL_RANGE)

    @classmethod
    def singleton(cls, version: Version | str) -> VersionRange:
        """Return the range that contains only *version*.

        >>> r = VersionRange.singleton("1.2.3")
        >>> "1.2.3" in r
        True
        >>> "1.2.4" in r
        False

        :raises packaging.version.InvalidVersion: if *version* is a
            string that does not parse as a PEP 440 version.
        """
        if not isinstance(version, Version):
            version = Version(version)
        lower = _LowerBound(version, True)
        upper = _UpperBound(version, True)
        return cls._build(((lower, upper),))

    def intersection(self, other: VersionRange) -> VersionRange:
        """Range containing exactly the versions in both *self* and *other*.

        >>> a = VersionRange.from_specifier_set(SpecifierSet(">=1.0"))
        >>> b = VersionRange.from_specifier_set(SpecifierSet("<2.0"))
        >>> ab = VersionRange.from_specifier_set(SpecifierSet(">=1.0,<2.0"))
        >>> a.intersection(b) == ab
        True
        """
        if not self._has_literals() and not other._has_literals():
            return self._build(tuple(_intersect_ranges(self._bounds, other._bounds)))
        new_bounds = tuple(_intersect_ranges(self._bounds, other._bounds))
        return self._combine_literals(other, new_bounds, intersect=True)

    def union(self, other: VersionRange) -> VersionRange:
        """Range containing every version in *self* or *other*.

        >>> a = VersionRange.singleton("1.0")
        >>> b = VersionRange.singleton("2.0")
        >>> "1.0" in a.union(b) and "2.0" in a.union(b)
        True
        >>> "1.5" in a.union(b)
        False
        """
        if not self._has_literals() and not other._has_literals():
            return self._build(tuple(_union_ranges(self._bounds, other._bounds)))
        new_bounds = tuple(_union_ranges(self._bounds, other._bounds))
        return self._combine_literals(other, new_bounds, intersect=False)

    def complement(self) -> VersionRange:
        """Range containing every version *not* in *self*.

        >>> r = VersionRange.from_specifier(Specifier(">=1.0"))
        >>> "0.5" in r.complement()
        True
        >>> "1.5" in r.complement()
        False
        >>> r.complement().complement() == r
        True
        """
        if not self._has_literals():
            return self._build(tuple(_complement_ranges(self._bounds)))
        # Swap the admit and reject sets, complement the bounds.
        # ``_build`` drops anything now redundant against the new bounds.
        return self._build(
            tuple(_complement_ranges(self._bounds)),
            admit=self._reject,
            reject=self._admit,
        )

    def _combine_literals(
        self,
        other: VersionRange,
        new_bounds: tuple[_VersionRange, ...],
        *,
        intersect: bool,
    ) -> VersionRange:
        """Resolve admit/reject for ``self & other`` or ``self | other``.

        The bound-only result is already in *new_bounds*. For each
        literal seen on either side, decide whether the combined
        predicate (AND for intersection, OR for union) admits it, then
        record an explicit admit or reject when the new bounds would
        give the wrong answer on their own.
        """
        admits: set[str] = set()
        rejects: set[str] = set()
        for literal in self._admit | self._reject | other._admit | other._reject:
            self_in = self._matches_literal(literal)
            other_in = other._matches_literal(literal)
            want = (self_in and other_in) if intersect else (self_in or other_in)
            bound_in = _bound_match_string(new_bounds, literal)
            if want and not bound_in:
                admits.add(literal)
            elif not want and bound_in:
                rejects.add(literal)
        return self._build(
            new_bounds, admit=frozenset(admits), reject=frozenset(rejects)
        )

    def _matches_literal(self, literal: str) -> bool:
        """Whether *literal* (case-folded) matches this range's predicate."""
        if literal in self._reject:
            return False
        if literal in self._admit:
            return True
        return _bound_match_string(self._bounds, literal)

    def __and__(self, other: object) -> VersionRange:
        """Operator alias for :meth:`intersection`."""
        if not isinstance(other, VersionRange):
            return NotImplemented
        return self.intersection(other)

    def __or__(self, other: object) -> VersionRange:
        """Operator alias for :meth:`union`."""
        if not isinstance(other, VersionRange):
            return NotImplemented
        return self.union(other)

    def __invert__(self) -> VersionRange:
        """Operator alias for :meth:`complement`."""
        return self.complement()

    def filter(
        self,
        iterable: Iterable[Any],
        key: Callable[[Any], Version | str] | None = None,
        prereleases: bool | None = None,
    ) -> Iterator[Any]:
        """Yield items from *iterable* whose version falls inside the range.

        With *prereleases* ``None`` the PEP 440 default applies:
        pre-releases are buffered and only emitted if no final release
        in *iterable* is in range.

        Filtering matches :class:`SpecifierSet.filter` for the same
        :class:`Specifier` / :class:`SpecifierSet`, including
        :class:`SpecifierSet("")`'s admission of unparsable strings
        and the case-insensitive literal match for ``===``.

        >>> r = VersionRange.from_specifier_set(SpecifierSet(">=1.0,<2.0"))
        >>> list(r.filter(["0.9", "1.5", "2.0"]))
        ['1.5']
        """
        if self._admit or self._reject:
            return self._filter_with_admission(iterable, key, prereleases)
        if self._bounds == _FULL_RANGE:
            # Full-range carve-out: admit any item, parseable or not,
            # so behaviour matches ``SpecifierSet("").filter``.
            return self._filter_with_admission(iterable, key, prereleases)
        return _filter_by_ranges(self._bounds, iterable, key, prereleases)

    def _filter_with_admission(
        self,
        iterable: Iterable[Any],
        key: Callable[[Any], Version | str] | None,
        prereleases: bool | None,
    ) -> Iterator[Any]:
        """Filter for ranges that admit unparsable strings.

        Used by ``===`` ranges (literal admit/reject) and the full-range
        carve-out. Same PEP 440 pre-release buffering for both, with a
        different admission check.
        """
        admit_set = self._admit
        reject_set = self._reject
        full_bounds = self._bounds == _FULL_RANGE

        def admit(item: Any) -> tuple[bool, Version | None]:  # noqa: ANN401
            raw: Version | str = item if key is None else key(item)
            raw_lower = str(raw).lower()
            if reject_set and raw_lower in reject_set:
                return False, None
            if admit_set and raw_lower in admit_set:
                return True, _coerce_version(raw)
            parsed = _coerce_version(raw)
            if parsed is None:
                return full_bounds, None
            if not full_bounds and not self._matches_bounds(parsed):
                return False, None
            return True, parsed

        if prereleases is True:
            for item in iterable:
                ok, _ = admit(item)
                if ok:
                    yield item
            return

        if prereleases is False:
            for item in iterable:
                ok, parsed = admit(item)
                if not ok:
                    continue
                if parsed is not None and parsed.is_prerelease:
                    continue
                yield item
            return

        # PEP 440 default: yield finals immediately; buffer the rest
        # until we know whether any final exists.
        all_nonfinal: list[Any] = []
        arbitrary_strings: list[Any] = []
        found_final = False
        for item in iterable:
            ok, parsed = admit(item)
            if not ok:
                continue
            if parsed is None:
                if found_final:
                    yield item
                else:
                    arbitrary_strings.append(item)
                    all_nonfinal.append(item)
                continue
            if not parsed.is_prerelease:
                if not found_final:
                    yield from arbitrary_strings
                    arbitrary_strings.clear()
                    found_final = True
                yield item
                continue
            if not found_final:
                all_nonfinal.append(item)
        if not found_final:
            yield from all_nonfinal

    @classmethod
    def from_specifier(cls, specifier: Specifier) -> VersionRange:
        """Return the :class:`VersionRange` accepted by *specifier*.

        Results are cached on the *specifier* instance.

        >>> isinstance(VersionRange.from_specifier(Specifier(">=1.0")), VersionRange)
        True
        """
        cached = specifier._range_cache
        if cached is not None:
            return cached

        op = specifier.operator
        if op == "===":
            arb_result = cls._build((), admit=frozenset({specifier.version.lower()}))
            specifier._range_cache = arb_result
            return arb_result

        ver_str = specifier.version
        result: VersionRange
        if ver_str.endswith(".*"):
            base = specifier._require_spec_version(ver_str[:-2])
            result = cls._build(tuple(_wildcard_ranges(op, base)))
        else:
            version = specifier._require_spec_version(ver_str)
            has_local = "+" in ver_str
            result = cls._build(tuple(_standard_ranges(op, version, has_local)))

        specifier._range_cache = result
        return result

    @classmethod
    def from_specifier_set(cls, specifier_set: SpecifierSet) -> VersionRange:
        """Return the :class:`VersionRange` accepted by *specifier_set*.

        The intersection of every specifier in the set. An empty
        :class:`SpecifierSet` yields the unbounded range; an
        unsatisfiable set yields an empty :class:`VersionRange`.
        Results are cached on the *specifier_set* instance.

        >>> isinstance(
        ...     VersionRange.from_specifier_set(SpecifierSet(">=1.0,<2.0")),
        ...     VersionRange,
        ... )
        True
        >>> VersionRange.from_specifier_set(SpecifierSet(">=2.0,<1.0")).is_empty
        True
        """
        cached = specifier_set._range_cache
        if cached is not None:
            return cached

        # ``===`` literals are handled separately from rangelike specs:
        # the rangelike specs build the bounds, and a single literal
        # is admitted only if it also satisfies those bounds.
        arbitrary_specs = [s for s in specifier_set._specs if s.operator == "==="]
        rangelike_specs = [s for s in specifier_set._specs if s.operator != "==="]

        if not rangelike_specs:
            rangelike_result: VersionRange = cls._build(_FULL_RANGE)
        else:
            tmp: VersionRange | None = None
            for s in rangelike_specs:
                sub = cls.from_specifier(s)
                if tmp is None:
                    tmp = sub
                else:
                    tmp = tmp.intersection(sub)
                    if tmp.is_empty:
                        break
            assert tmp is not None
            rangelike_result = tmp

        if not arbitrary_specs:
            specifier_set._range_cache = rangelike_result
            return rangelike_result

        # Each ``===L_i`` requires the candidate's string to equal L_i.
        # Distinct literals can never all match, so the result is empty.
        literals_lower = {s.version.lower() for s in arbitrary_specs}
        result: VersionRange
        if len(literals_lower) > 1:
            result = cls._build(())
        else:
            (literal_lower,) = literals_lower
            if literal_lower in rangelike_result:
                result = cls._build((), admit=frozenset({literal_lower}))
            else:
                result = cls._build(())

        specifier_set._range_cache = result
        return result

    def to_specifier_set(self) -> SpecifierSet | None:
        """Return a single :class:`SpecifierSet` whose
        :meth:`from_specifier_set` yields *self*, or ``None`` if no
        such set exists.

        :class:`SpecifierSet` cannot express every range. PEP 440's
        operator set has no syntax for the strict singleton ``{V}`` or
        for the bounds produced by complementing ``>V``; for those
        ranges the result is ``None``. Use :meth:`to_specifier_sets`
        when a tuple of specifier sets is acceptable. The empty range
        maps to ``SpecifierSet("<0")`` (``<0`` excludes ``0.dev0``,
        the smallest PEP 440 version); the full range maps to
        ``SpecifierSet("")``.

        >>> r = VersionRange.from_specifier_set(SpecifierSet(">=1.0,<2.0"))
        >>> str(r.to_specifier_set())
        '<2.0,>=1.0'
        >>> VersionRange.singleton("1.5").to_specifier_set() is None
        True
        """
        # Local import avoids the circular .specifiers <-> .ranges load.
        from .specifiers import SpecifierSet  # noqa: PLC0415

        if self._reject:
            # No PEP 440 operator excludes a literal string while
            # admitting other versions.
            return None
        if self._admit:
            return self._admit_to_specifier_set()
        if self.is_empty:
            # ``<0`` parses to upper = 0.dev0 (excl), the smallest
            # possible PEP 440 version, so the range contains nothing.
            return SpecifierSet("<0")
        if self._bounds == _FULL_RANGE:
            return SpecifierSet("")

        # Walk left-to-right, merging adjacent intervals whose gap is
        # a ``!=V`` or ``!=V.*`` exclusion. The merged outer bounds
        # plus the chain of ``!=`` fragments form a single SpecifierSet.
        bounds = list(self._bounds)
        outer_lower = bounds[0][0]
        outer_upper = bounds[0][1]
        exclusions: list[str] = []
        for next_lower, next_upper in bounds[1:]:
            not_equal = _detect_not_equal(outer_upper, next_lower)
            not_equal_wildcard = _detect_not_equal_wildcard(outer_upper, next_lower)
            if not_equal is not None:
                exclusions.append(f"!={not_equal}")
            elif not_equal_wildcard is not None:
                exclusions.append(f"!={not_equal_wildcard}.*")
            else:
                return None
            outer_upper = next_upper

        outer_parts = _encode_interval(outer_lower, outer_upper)
        if outer_parts is None:
            return None
        return SpecifierSet(",".join(outer_parts + exclusions))

    def to_specifier_sets(self) -> tuple[SpecifierSet, ...] | None:
        """Return a tuple of :class:`SpecifierSet` whose union equals
        *self*, or ``None`` if no such tuple exists.

        Looser than :meth:`to_specifier_set`: a range that fits a
        single :class:`SpecifierSet` returns a one-tuple, otherwise
        each interval encodes separately. ``None`` only for ranges
        whose individual intervals still have no PEP 440 specifier
        (for example the singleton produced by :meth:`singleton`).

        >>> r = (
        ...     VersionRange.from_specifier_set(SpecifierSet(">=1.0,<2.0"))
        ...     | VersionRange.from_specifier_set(SpecifierSet(">=3.0,<4.0"))
        ... )
        >>> [str(s) for s in r.to_specifier_sets()]
        ['<2.0,>=1.0', '<4.0,>=3.0']
        >>> VersionRange.singleton("1.5").to_specifier_sets() is None
        True
        """
        from .specifiers import SpecifierSet  # noqa: PLC0415

        if self._reject:
            return None
        if self._admit:
            single = self._admit_to_specifier_set()
            if single is None:
                return None
            return (single,)
        if self.is_empty:
            return (SpecifierSet("<0"),)
        if self._bounds == _FULL_RANGE:
            return (SpecifierSet(""),)

        # Prefer the single-set form when it exists; that catches
        # multi-interval ``!=V`` / ``!=V.*`` patterns the per-interval
        # encoder rejects.
        single = self.to_specifier_set()
        if single is not None:
            return (single,)

        out: list[SpecifierSet] = []
        for lower, upper in self._bounds:
            parts = _encode_interval(lower, upper)
            if parts is None:
                return None
            out.append(SpecifierSet(",".join(parts)))
        return tuple(out)

    def _admit_to_specifier_set(self) -> SpecifierSet | None:
        """Encode a single ``===L`` range as ``SpecifierSet("===L")``.

        Returns ``None`` for shapes PEP 440 cannot express: multiple
        admit literals (no ``=== A or === B`` syntax), or admit
        combined with a non-empty bound set.
        """
        from .specifiers import SpecifierSet  # noqa: PLC0415

        if len(self._admit) != 1 or self._bounds:
            return None
        (literal,) = self._admit
        return SpecifierSet(f"==={literal}")

    def __reduce__(self) -> tuple[object, ...]:
        # Pickle to a primitive form (see ``_PackedBound``). The legacy
        # ``arbitrary`` slot is kept for older restorer signatures.
        return (
            _restore_version_range,
            (
                tuple(
                    (_pack_bound(lower), _pack_bound(upper))
                    for lower, upper in self._bounds
                ),
                None,
                tuple(sorted(self._admit)),
                tuple(sorted(self._reject)),
            ),
        )

    @property
    def is_empty(self) -> bool:
        """``True`` if no version or string satisfies this range.

        >>> VersionRange.from_specifier_set(SpecifierSet(">=2,<1")).is_empty
        True
        >>> VersionRange.from_specifier_set(SpecifierSet(">=1,<2")).is_empty
        False
        """
        return not self._bounds and not self._admit

    @property
    def is_prerelease_only(self) -> bool:
        """``True`` when every match is a PEP 440 pre-release.

        Used by :meth:`SpecifierSet.is_unsatisfiable` to detect sets
        that admit no candidate under the default ``prereleases=False``
        reading. Returns ``False`` for the empty range.

        >>> r = VersionRange.from_specifier_set(SpecifierSet(">=1.0a1,<1.0rc1"))
        >>> r.is_prerelease_only
        True
        >>> VersionRange.from_specifier(Specifier(">=1.0")).is_prerelease_only
        False
        """
        if self.is_empty:
            return False
        if self._reject:
            return False
        for literal in self._admit:
            parsed = _coerce_version(literal)
            if parsed is None or not parsed.is_prerelease:
                return False
        if self._bounds:
            return _ranges_are_prerelease_only(self._bounds)
        return True

    def __bool__(self) -> bool:
        """``False`` when the range is empty, ``True`` otherwise.

        >>> bool(VersionRange.from_specifier_set(SpecifierSet(">=1,<2")))
        True
        >>> bool(VersionRange.from_specifier_set(SpecifierSet(">=2,<1")))
        False
        """
        return bool(self._bounds) or bool(self._admit)

    def __contains__(self, item: Version | str) -> bool:
        """Return whether *item* is contained in this range.

        Unparsable strings do not match, except where
        :class:`SpecifierSet` would also match: the full range admits
        any string, and a ``===`` range admits items whose string
        equals the literal case-insensitively.

        >>> r = VersionRange.from_specifier_set(SpecifierSet(">=1.0,<2.0"))
        >>> "1.5" in r
        True
        >>> "2.0" in r
        False
        """
        if self._admit or self._reject:
            item_str = str(item).lower()
            if item_str in self._reject:
                return False
            if item_str in self._admit:
                return True
        if self._bounds == _FULL_RANGE:
            # ``SpecifierSet("")`` admits any string. Match that.
            return True
        if not isinstance(item, Version):
            try:
                item = Version(item)
            except InvalidVersion:
                return False
        return self._matches_bounds(item)

    def _matches_bounds(self, item: Version) -> bool:
        """Bound-only membership check; ignores admit/reject."""
        return _matches_bounds_only(self._bounds, item)

    def __eq__(self, other: object) -> bool:
        """Structural equality. Two ranges are equal when they admit
        exactly the same set of versions and strings.

        >>> a = VersionRange.from_specifier_set(SpecifierSet(">=1.0,<2.0"))
        >>> b = VersionRange.from_specifier_set(SpecifierSet(">=1.0,<2.0"))
        >>> a == b
        True
        """
        if not isinstance(other, VersionRange):
            return NotImplemented
        return (
            self._bounds == other._bounds
            and self._admit == other._admit
            and self._reject == other._reject
        )

    def __hash__(self) -> int:
        if not self._admit and not self._reject:
            return hash(self._bounds)
        return hash((self._bounds, self._admit, self._reject))

    def __repr__(self) -> str:
        """Human-readable representation. Internal layout, debugging only.

        >>> VersionRange.from_specifier_set(SpecifierSet(">=1.0,<2.0"))
        <VersionRange '[1.0, 2.0.dev0)'>
        >>> VersionRange.from_specifier_set(SpecifierSet(""))
        <VersionRange '(-inf, +inf)'>
        >>> VersionRange.from_specifier_set(SpecifierSet(">=2.0,<1.0"))
        <VersionRange '(empty)'>
        >>> VersionRange.from_specifier(Specifier("===wat"))
        <VersionRange '{wat}'>
        """
        if self._bounds:
            bound_body = " | ".join(
                f"{_format_lower(lower)}, {_format_upper(upper)}"
                for lower, upper in self._bounds
            )
        else:
            bound_body = "(empty)" if not self._admit else ""
        parts: list[str] = []
        if bound_body:
            parts.append(bound_body)
        if self._admit:
            parts.append("{" + ", ".join(sorted(self._admit)) + "}")
        body = " | ".join(parts) if parts else "(empty)"
        if self._reject:
            body = f"{body} \\ {{{', '.join(sorted(self._reject))}}}"
        return f"<{self.__class__.__name__} {body!r}>"
