"""Resolve version constraints independently for each candidate source."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from nab_provider._vendor.packaging.ranges import VersionRange

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from nab_provider._vendor.packaging.version import Version

__all__ = ["CandidateKey", "CandidateRange"]

_CANDIDATE_VERSIONS = VersionRange.full(admit_arbitrary=False)


@dataclass(frozen=True, order=True)
class CandidateKey:
    """Identify a distribution by its version and installer source."""

    version: Version
    source: str

    def __str__(self) -> str:
        """Render the PEP 440 version for host diagnostics."""
        return str(self.version)


class _RangeRelation(Enum):
    EMPTY = (True, True)
    SUBSET = (True, False)
    DISJOINT = (False, True)
    OVERLAPPING = (False, False)

    @property
    def is_subset(self) -> bool:
        """Whether every admitted candidate is also in the other constraint."""
        return self.value[0]

    @property
    def is_disjoint(self) -> bool:
        """Whether the constraints admit no shared candidate."""
        return self.value[1]


@dataclass(frozen=True, init=False)
class CandidateRange:
    """An immutable default version range with finite source-specific replacements."""

    default: VersionRange
    overrides: tuple[tuple[str, VersionRange], ...]
    _hash: int

    def __init__(
        self,
        default: VersionRange,
        overrides: Mapping[str, VersionRange] | Iterable[tuple[str, VersionRange]] = (),
    ) -> None:
        """Snapshot source bounds with the default prerelease configuration."""
        default = default & _CANDIDATE_VERSIONS
        sources = {
            source: value & _CANDIDATE_VERSIONS
            for source, value in dict(overrides).items()
        }
        normalized = tuple(
            sorted(
                (source, value) for source, value in sources.items() if value != default
            )
        )
        object.__setattr__(self, "default", default)
        object.__setattr__(self, "overrides", normalized)
        object.__setattr__(self, "_hash", hash((default, normalized)))

    @classmethod
    def empty(cls) -> CandidateRange:
        """Return a constraint admitting no candidate."""
        return cls(VersionRange.empty())

    @classmethod
    def full(cls) -> CandidateRange:
        """Return every PEP 440 version on every source."""
        return cls(_CANDIDATE_VERSIONS)

    @classmethod
    def singleton(cls, candidate: CandidateKey) -> CandidateRange:
        """Return a constraint admitting this candidate identity alone."""
        return cls.for_source(
            candidate.source, VersionRange.singleton(candidate.version)
        )

    @classmethod
    def for_source(
        cls, source: str, versions: VersionRange | None = None
    ) -> CandidateRange:
        """Restrict the given source and exclude every other source."""
        if versions is None:
            versions = _CANDIDATE_VERSIONS
        return cls(VersionRange.empty(), [(source, versions)])

    def for_versions(self, source: str) -> VersionRange:
        """Return the version constraint applying to one source."""
        for key, value in self.overrides:
            if key == source:
                return value
        return self.default

    @property
    def is_empty(self) -> bool:
        """Whether no version is admitted on any source."""
        return self.default.is_empty and all(
            versions.is_empty for _, versions in self.overrides
        )

    def __contains__(self, candidate: CandidateKey) -> bool:
        """Check the version bounds on the candidate's source."""
        return candidate.version in self.for_versions(candidate.source)

    def _combine(
        self,
        other: CandidateRange,
        operation: Callable[[VersionRange, VersionRange], VersionRange],
    ) -> CandidateRange:
        """Apply one range operation to every affected source coordinate."""
        default = operation(self.default, other.default)
        sources = {source for source, _ in self.overrides + other.overrides}
        return CandidateRange(
            default,
            (
                (
                    source,
                    operation(self.for_versions(source), other.for_versions(source)),
                )
                for source in sources
            ),
        )

    def __and__(self, other: object) -> CandidateRange:
        """Intersect each source coordinate."""
        if not isinstance(other, CandidateRange):
            return NotImplemented
        return self._combine(other, VersionRange.__and__)

    def __or__(self, other: object) -> CandidateRange:
        """Union each source coordinate."""
        if not isinstance(other, CandidateRange):
            return NotImplemented
        return self._combine(other, VersionRange.__or__)

    def __sub__(self, other: object) -> CandidateRange:
        """Remove the other constraint on each source coordinate."""
        if not isinstance(other, CandidateRange):
            return NotImplemented
        return self._combine(other, VersionRange.__sub__)

    def __invert__(self) -> CandidateRange:
        """Complement the bounds independently on each source."""
        return CandidateRange(
            ~self.default, ((source, ~value) for source, value in self.overrides)
        )

    def is_subset(self, other: CandidateRange) -> bool:
        """Whether every admitted candidate is also in the other constraint."""
        return (self - other).is_empty

    def is_superset(self, other: CandidateRange) -> bool:
        """Whether every candidate in the other constraint is admitted here."""
        return other.is_subset(self)

    def is_disjoint(self, other: CandidateRange) -> bool:
        """Whether the constraints admit no shared candidate."""
        return (self & other).is_empty

    def relation(self, other: CandidateRange) -> _RangeRelation:
        """Return the subset and disjoint range flags the resolver reads."""
        return _RangeRelation((self.is_subset(other), self.is_disjoint(other)))

    def __eq__(self, other: object) -> bool:
        """Compare normalized source coordinates and admission policy."""
        if not isinstance(other, CandidateRange):
            return NotImplemented
        return self.default == other.default and self.overrides == other.overrides

    def __hash__(self) -> int:
        """Return the hash fixed when the constraint was constructed."""
        return self._hash

    def __str__(self) -> str:
        """Render the version bounds and exceptional source coordinates."""
        return f"{self.default!r}; sources={self.overrides!r}"
