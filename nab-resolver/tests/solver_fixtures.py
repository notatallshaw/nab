"""Integer-version fixtures shared by the resolver tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from nab_resolver._compat import override
from nab_resolver.errors import ResolutionError, ResolutionTerminatedError
from nab_resolver.ranges import Range
from nab_resolver.types import Incompatibility, RangeProtocol, RangeRelation


class DictProvider:
    """In-memory provider for testing. Packages are strings, versions are ints."""

    def __init__(self, packages: dict[str, dict[int, dict[str, Range]]]) -> None:
        self._packages = packages

    def _get_versions(self, package: str) -> list[int]:
        if package not in self._packages:
            return []
        return sorted(self._packages[package].keys(), reverse=True)

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        for version in self._get_versions(package):
            if version in version_range:
                return version
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return any(v in version_range for v in self._get_versions(package))

    def get_dependencies(self, package: str, version: int) -> dict[str, Range]:
        return self._packages.get(package, {}).get(version, {})

    def begin_decision_scan(self) -> None:
        """No-op: nothing in this stub moves under a scan."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> object:
        versions = self._get_versions(package)
        return sum(1 for v in versions if v in version_range)

    def is_ready(self, package: str) -> bool:
        """All packages decidable immediately in tests."""
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        """No-op: test provider does not use partial solution state."""

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """No queued clauses for this in-memory provider."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal from this provider."""
        return []

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """No widening: dependency clauses keep the exact decided version."""
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        """Identity: constraints render as stored."""
        return constraint


class FlaggedRange:
    """``Range[int]`` plus VersionRange's arbitrary-string admission flag.

    A metadata marker changes equality without changing membership.
    ``full()`` sets the flag, ``empty()`` clears it, and the four operations
    below carry it by VersionRange's rules.  As there, the flag admits strings
    only at full bounds, so a full-bounded flagged range is a strict superset
    of its unflagged twin and ``is_subset`` checks that flag. ``===`` literals
    and pre-release regions are left out; the subset identity under test needs neither.
    """

    __slots__ = ("arbitrary", "base", "metadata")

    # Counts every ``__eq__`` call, so a test can pin one that is skipped.
    eq_calls: ClassVar[int] = 0

    def __init__(
        self, base: Range[int], *, arbitrary: bool = False, metadata: bool = False
    ) -> None:
        self.base = base
        self.arbitrary = arbitrary
        self.metadata = metadata

    @classmethod
    def empty(cls) -> FlaggedRange:
        return cls(Range.empty())

    @classmethod
    def full(cls) -> FlaggedRange:
        return cls(Range.full(), arbitrary=True)

    @classmethod
    def singleton(cls, version: int) -> FlaggedRange:
        return cls(Range.singleton(version))

    @property
    def is_empty(self) -> bool:
        return self.base.is_empty

    def arbitrary_active(self) -> bool:
        """Whether the flag admits strings under ``VersionRange`` rules."""
        return self.arbitrary and self.base == Range.full()

    def __contains__(self, version: int) -> bool:
        """Test membership; the flag admits no integer."""
        return version in self.base

    def __and__(self, other: Any) -> FlaggedRange:
        """Intersect; the flag needs both sides and a result with versions."""
        new_base = self.base & other.base
        return FlaggedRange(
            new_base,
            arbitrary=self.arbitrary and other.arbitrary and not new_base.is_empty,
            metadata=self.metadata or other.metadata,
        )

    def __or__(self, other: Any) -> FlaggedRange:
        """Union; only an operand that has versions passes the flag on.

        An empty operand carries the flag inertly, so ``~~full()`` is
        ``full()`` again while re-widening bounds does not revive the
        admission.
        """
        new_base = self.base | other.base
        if new_base.is_empty:
            arbitrary = self.arbitrary or other.arbitrary
        else:
            arbitrary = (self.arbitrary and not self.base.is_empty) or (
                other.arbitrary and not other.base.is_empty
            )
        return FlaggedRange(
            new_base, arbitrary=arbitrary, metadata=self.metadata or other.metadata
        )

    def __invert__(self) -> FlaggedRange:
        """Complement, carrying the flag through."""
        return FlaggedRange(
            ~self.base, arbitrary=self.arbitrary, metadata=self.metadata
        )

    def __sub__(self, other: Any) -> FlaggedRange:
        """Difference, dropping the flag unless nothing was removed."""
        new_base = self.base - other.base
        return FlaggedRange(
            new_base,
            arbitrary=self.arbitrary and new_base == self.base,
            metadata=self.metadata,
        )

    def is_subset(self, other: FlaggedRange) -> bool:
        """Subset on versions; a live admission needs another live one."""
        if self.arbitrary_active() and not other.arbitrary_active():
            return False
        return self.base.is_subset(other.base)

    def is_superset(self, other: FlaggedRange) -> bool:
        return other.is_subset(self)

    def is_disjoint(self, other: FlaggedRange) -> bool:
        """Disjoint when the intersection lacks versions and arbitrary admission."""
        combined = self & other
        return combined.base.is_empty and not combined.arbitrary_active()

    def relation(self, other: FlaggedRange) -> RangeRelation:
        """Classify against ``other``, so the flag reaches term classification."""
        subset = self.is_subset(other)
        disjoint = self.is_disjoint(other)
        if subset:
            return RangeRelation.EMPTY if disjoint else RangeRelation.SUBSET
        return RangeRelation.DISJOINT if disjoint else RangeRelation.OVERLAPPING

    @override
    def __eq__(self, other: object) -> bool:
        """Equal when the interval sets and the flags both match."""
        FlaggedRange.eq_calls += 1
        return (
            isinstance(other, FlaggedRange)
            and self.base == other.base
            and self.arbitrary == other.arbitrary
            and self.metadata == other.metadata
        )

    @override
    def __hash__(self) -> int:
        """Hash interval membership and metadata."""
        return hash((self.base, self.arbitrary, self.metadata))

    @override
    def __repr__(self) -> str:
        """Render the interval set, naming the flag when it is set."""
        suffix = ", arbitrary" if self.arbitrary else ""
        if self.metadata:
            suffix += ", metadata"
        return f"FlaggedRange({self.base!r}{suffix})"


def is_proven_conflict(error: ResolutionError) -> bool:
    """A solver fault can carry a diagnostic incompatibility without proving a conflict."""
    return (
        not isinstance(error, ResolutionTerminatedError)
        and error.incompatibility is not None
    )
