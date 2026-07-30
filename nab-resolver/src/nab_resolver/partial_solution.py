"""Partial solution: ordered assignment list with decision levels.

The partial solution is a chronological trail of assignments.  Each
assignment constrains a package's allowed versions and is either a
decision (the resolver picks a specific version to try) or a
derivation (a constraint deduced by unit propagation).

Each decision opens a new "decision level".  Backtracking removes
all assignments above a target level, which is cheaper than copying
the entire state on every decision.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#partial-solution
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, cast

from .ranges import Range
from .types import PackageType, RangeProtocol, VersionType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .types import Incompatibility, Term


__all__ = [
    "Assignment",
    "PartialSolution",
]


# Distinguishes "cached miss" from a legitimate cached None.
_UNSET = object()


@dataclass(slots=True)
class Assignment(Generic[PackageType, VersionType]):
    """A single entry in the partial solution trail."""

    package: PackageType
    """Which package this assignment constrains."""

    accumulated_range: RangeProtocol[VersionType]
    """The cumulative range for this package at the time of assignment."""

    decision_level: int
    """The decision depth when this assignment was made."""

    is_decision: bool
    """True if this is a version choice; False if derived by propagation."""

    trail_index: int = 0
    """Chronological position in the assignment trail."""

    version: VersionType | None = None
    """The chosen version (only set for decisions)."""

    cause: Incompatibility[PackageType, VersionType] | None = None
    """The incompatibility that forced this derivation (only for derivations)."""

    positive: bool = True
    """Whether this constrains the package positively or negatively."""

    cum_positive: RangeProtocol[VersionType] | None = None
    """Latest positive accumulated range for the package as of this entry."""

    cum_negative: RangeProtocol[VersionType] | None = None
    """Latest negative accumulated range for the package as of this entry."""

    package_index: int = 0
    """Position in the package's own assignment trail."""


class PartialSolution(Generic[PackageType, VersionType]):
    """Tracks the resolver's current partial solution as a decision trail.

    This is the PubGrub equivalent of a SAT solver's assignment trail.
    See: https://en.wikipedia.org/wiki/Conflict-driven_clause_learning#Organization
    """

    def __init__(self, range_type: type[RangeProtocol[Any]] = Range) -> None:
        """Initialize an empty partial solution."""
        self._range_type = range_type
        self._assignments: list[Assignment[PackageType, VersionType]] = []
        self._decision_level = 0
        self._positive_ranges: dict[PackageType, RangeProtocol[VersionType]] = {}
        self._negative_ranges: dict[PackageType, RangeProtocol[VersionType]] = {}
        self._decided_versions: dict[PackageType, VersionType] = {}

        # Incrementally maintained as set(positive) - set(decided).
        self._undecided: set[PackageType] = set()

        # Memoises positive - negative per package.
        self._effective_range_cache: dict[
            PackageType, RangeProtocol[VersionType] | None
        ] = {}

        # Per-package index of trail entries; lets satisfier() avoid the full scan.
        self._assignments_by_package: defaultdict[
            PackageType, list[Assignment[PackageType, VersionType]]
        ] = defaultdict(list)

    @property
    def decision_level(self) -> int:
        """Return the current decision depth."""
        return self._decision_level

    @property
    def trail_length(self) -> int:
        """Return the number of assignments currently on the trail."""
        return len(self._assignments)

    def assignments_for(
        self, package: PackageType
    ) -> Sequence[Assignment[PackageType, VersionType]]:
        """Return the chronological assignment trail for ``package``.

        Read-only view; callers must not mutate the result.
        """
        entries = self._assignments_by_package.get(package)
        if entries is None:
            return ()
        return entries

    def get(self, package: PackageType) -> RangeProtocol[VersionType] | None:
        """Get the combined allowed range for a package, or None if unassigned.

        Computes ``positive - negative``, cached per package.
        """
        cached = self._effective_range_cache.get(package, _UNSET)
        if cached is not _UNSET:
            return cast("RangeProtocol[VersionType] | None", cached)

        positive = self._positive_ranges.get(package)
        negative = self._negative_ranges.get(package)

        if positive is None and negative is None:
            result: RangeProtocol[VersionType] | None = None
        elif positive is None:
            # No requirement to subtract from. An exclusion-only package is
            # never offered for selection, so the plain complement is enough.
            assert negative is not None
            result = ~negative
        elif negative is None:
            result = positive
        else:
            result = positive - negative

        self._effective_range_cache[package] = result
        return result

    def decide(self, package: PackageType, version: VersionType) -> None:
        """Record a decision: pick a specific version for a package."""
        self._decision_level += 1
        exact_range = self._range_type.singleton(version)

        self._positive_ranges[package] = exact_range
        self._decided_versions[package] = version
        self._effective_range_cache.pop(package, None)
        self._undecided.discard(package)

        package_entries = self._assignments_by_package[package]
        assignment = Assignment(
            package=package,
            accumulated_range=exact_range,
            decision_level=self._decision_level,
            is_decision=True,
            trail_index=len(self._assignments),
            version=version,
            positive=True,
            cum_positive=exact_range,
            cum_negative=self._negative_ranges.get(package),
            package_index=len(package_entries),
        )
        self._assignments.append(assignment)
        package_entries.append(assignment)

    def derive(
        self,
        package: PackageType,
        constraint: RangeProtocol[VersionType],
        *,
        positive: bool,
        cause: Incompatibility[PackageType, VersionType],
    ) -> None:
        """Record a derivation from unit propagation.

        See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#unit-propagation
        """
        if positive:
            # Positive derivation narrows the package's allowed range.
            if package in self._positive_ranges:
                new_range = self._positive_ranges[package] & constraint
            else:
                new_range = self._range_type.full() & constraint
            self._positive_ranges[package] = new_range
            if package not in self._decided_versions:
                self._undecided.add(package)
        else:
            # Negative derivation accumulates excluded versions.
            if package in self._negative_ranges:
                new_range = self._negative_ranges[package] | constraint
            else:
                new_range = self._range_type.empty() | constraint
            self._negative_ranges[package] = new_range

        self._effective_range_cache.pop(package, None)

        package_entries = self._assignments_by_package[package]
        assignment = Assignment(
            package=package,
            accumulated_range=new_range,
            decision_level=self._decision_level,
            is_decision=False,
            trail_index=len(self._assignments),
            cause=cause,
            positive=positive,
            cum_positive=self._positive_ranges.get(package),
            cum_negative=self._negative_ranges.get(package),
            package_index=len(package_entries),
        )
        self._assignments.append(assignment)
        package_entries.append(assignment)

    def backtrack(self, target_level: int) -> None:
        """Remove all assignments above target_level.

        Non-chronological backjumping: skips past irrelevant decision levels
        directly to the cause of the conflict.  Relies on
        ``Assignment.accumulated_range`` already being cumulative, so each
        package's surviving state can be rebuilt without re-intersecting.
        See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#conflict-resolution
        """
        while self._assignments and self._assignments[-1].decision_level > target_level:
            self._assignments.pop()

        self._decision_level = target_level

        empty_packages: list[PackageType] = []
        for package, entries in self._assignments_by_package.items():
            while entries and entries[-1].decision_level > target_level:
                entries.pop()

            if not entries:
                empty_packages.append(package)
                self._positive_ranges.pop(package, None)
                self._negative_ranges.pop(package, None)
                self._decided_versions.pop(package, None)
                self._undecided.discard(package)
            else:
                self._update_package_state_after_backtrack(package, entries)

        for package in empty_packages:
            del self._assignments_by_package[package]

        self._effective_range_cache.clear()

    def _update_package_state_after_backtrack(
        self,
        package: PackageType,
        entries: list[Assignment[PackageType, VersionType]],
    ) -> None:
        """Recompute positive/negative/decided state for a package.

        Each ``Assignment.accumulated_range`` is already cumulative, so the
        latest entry of each kind is enough to rebuild state.  Trail levels
        never decrease, so popping a decision pops every later entry for the
        same package; a surviving decision is always the current one.
        """
        last_pos: RangeProtocol[VersionType] | None = None
        last_neg: RangeProtocol[VersionType] | None = None
        last_decision_version: VersionType | None = None

        for assignment in entries:
            if assignment.is_decision:
                last_pos = assignment.accumulated_range
                last_decision_version = assignment.version
            elif assignment.positive:
                last_pos = assignment.accumulated_range
            else:
                last_neg = assignment.accumulated_range

        if last_pos is None:
            self._positive_ranges.pop(package, None)
        else:
            self._positive_ranges[package] = last_pos

        if last_neg is None:
            self._negative_ranges.pop(package, None)
        else:
            self._negative_ranges[package] = last_neg

        if last_decision_version is None:
            self._decided_versions.pop(package, None)
        else:
            self._decided_versions[package] = last_decision_version

        if last_pos is not None and last_decision_version is None:
            self._undecided.add(package)
        else:
            self._undecided.discard(package)

    def decisions(self) -> dict[PackageType, VersionType]:
        """Return the current decision map: ``{package: version}``."""
        return dict(self._decided_versions)

    def undecided_packages(self) -> set[PackageType]:
        """Return packages with positive constraints but no decision yet.

        Packages with only negative derivations (learned exclusions) are not
        yet known to be required.  Returns a fresh copy so callers can mutate
        without disturbing solver state.
        """
        return set(self._undecided)

    def has_positive_constraint(self, package: PackageType) -> bool:
        """Return True if the package has a positive constraint or decision."""
        return package in self._positive_ranges or package in self._decided_versions

    def positive_ranges(self) -> dict[PackageType, RangeProtocol[VersionType]]:
        """Return a copy of the positive-range map for each package."""
        return dict(self._positive_ranges)

    def positive_range(self, package: PackageType) -> RangeProtocol[VersionType] | None:
        """Return the package's accumulated positive range, or None if unset."""
        return self._positive_ranges.get(package)

    def _satisfied_at(
        self,
        assignment: Assignment[PackageType, VersionType],
        term: Term[PackageType, VersionType],
        *,
        is_positive: bool,
    ) -> bool:
        """Whether the trail up to and including ``assignment`` satisfies term.

        Positive terms need a positive assignment first; negatives alone
        only exclude versions.
        """
        cum_positive = assignment.cum_positive
        if is_positive and cum_positive is None:
            return False

        if cum_positive is None:
            assert assignment.cum_negative is not None
            effective = ~assignment.cum_negative
        elif assignment.cum_negative is None:
            effective = cum_positive
        else:
            effective = cum_positive - assignment.cum_negative

        return term.satisfies(effective)

    def satisfier(
        self, term: Term[PackageType, VersionType]
    ) -> Assignment[PackageType, VersionType] | None:
        """Find the earliest assignment that causes the term to be satisfied.

        The effective range only narrows along the trail, so ``term.satisfies``
        is monotonic: once an entry satisfies the term, every later one does
        too.  That lets a binary search replace the linear scan.
        See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#conflict-resolution
        """
        entries = self._assignments_by_package.get(term.package, ())
        count = len(entries)
        if count == 0:
            return None

        is_positive = term.is_positive()
        if not self._satisfied_at(entries[count - 1], term, is_positive=is_positive):
            return None

        low, high = 0, count - 1
        while low < high:
            mid = (low + high) // 2
            if self._satisfied_at(entries[mid], term, is_positive=is_positive):
                high = mid
            else:
                low = mid + 1
        return entries[low]
