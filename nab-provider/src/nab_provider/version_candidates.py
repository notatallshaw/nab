"""PEP 440 candidate identities and exact-pin rules for prepared catalogues."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar, Final, Generic, TypeVar

from .value import HashableRecord, Record

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._vendor.packaging.ranges import VersionRange
    from ._vendor.packaging.requirements import Requirement
    from ._vendor.packaging.version import Version

__all__ = [
    "PreparedVersionCatalogue",
    "VersionCandidate",
    "VersionCandidateData",
    "VersionCatalogue",
    "is_pin",
]


class VersionCandidate(HashableRecord):
    """A version-text identity whose metadata may require withdrawal permission.

    Subclasses retain the ``(package, version, *, withdrawn)`` constructor.
    """

    __record_fields__: ClassVar[tuple[str, ...] | None]
    __slots__ = __record_fields__ = ("package", "version", "withdrawn", "label")
    __record_init_fields__: ClassVar[tuple[str, ...] | None] = (
        "package",
        "version",
        "withdrawn",
    )
    __match_args__ = ("package", "version", "withdrawn")

    package: Final[str]
    version: Final[Version]
    withdrawn: Final[bool]
    label: Final[str]

    def __init__(self, package: str, version: Version, *, withdrawn: bool) -> None:
        """Retain the text distinguishing numerically equal artifact versions."""
        self.package = package
        self.version = version
        self.withdrawn = withdrawn
        self.label = str(version)

    @property
    def base(self) -> str:
        """Return the project identifier for a candidate without an extras proxy."""
        return self.package


class VersionCandidateData(Record):
    """Prepared dependency ranges and their original exact-pin declarations."""

    __slots__ = __match_args__ = ("dependencies", "pins")

    dependencies: dict[str, VersionRange]
    pins: tuple[Requirement, ...]

    def __init__(
        self, dependencies: dict[str, VersionRange], pins: tuple[Requirement, ...]
    ) -> None:
        """Retain dependencies and their original admission declarations."""
        self.dependencies = dependencies
        self.pins = pins


def is_pin(requirement: Requirement) -> bool:
    """Recognize original exact equality syntax, including redundant conjuncts."""
    return any(
        item.operator == "==="
        or (item.operator == "==" and not item.version.endswith(".*"))
        for item in requirement.specifier
    )


CandidateType = TypeVar("CandidateType", bound=VersionCandidate)


class VersionCatalogue(ABC, Generic[CandidateType]):
    """Apply PEP 440 ranges and original pin syntax to prepared candidate identities."""

    @staticmethod
    def candidate_package(candidate: CandidateType) -> str:
        """Return the resolver package identifier."""
        return candidate.package

    @staticmethod
    def candidate_base(candidate: CandidateType) -> str:
        """Return the project without an extras proxy."""
        return candidate.base

    @staticmethod
    def candidate_label(candidate: CandidateType) -> str:
        """Return the original version text."""
        return candidate.label

    @staticmethod
    def candidate_withdrawn(candidate: CandidateType) -> bool:
        """Return whether metadata requires withdrawal permission."""
        return candidate.withdrawn

    range_type: ClassVar[type[VersionRange]]

    def full_range(self) -> VersionRange:
        """Return unconstrained bounds in the host range namespace."""
        return self.range_type.full()

    @staticmethod
    def intersect_ranges(left: VersionRange, right: VersionRange) -> VersionRange:
        """Intersect bounds in the host range namespace."""
        return left & right

    @staticmethod
    def range_contains(candidate: CandidateType, allowed: VersionRange) -> bool:
        """Test the candidate against the host range."""
        return candidate.version in allowed

    def pin_allows(self, pin: Requirement, candidate: CandidateType) -> bool:
        """Match the original pin against the candidate in its host namespace."""
        return self.pin_project(pin) == candidate.base and pin.specifier.contains(
            candidate.version, prereleases=True
        )

    @staticmethod
    @abstractmethod
    def pin_project(pin: Requirement) -> str:
        """Normalize the project named by an original pin in the host namespace."""
        ...

    @staticmethod
    @abstractmethod
    def base_name(package: str) -> str:
        """Remove the host's extras proxy from a package identifier."""
        ...

    @staticmethod
    def base_candidate(candidate: CandidateType) -> CandidateType:
        """Construct the same candidate type for its base project."""
        return type(candidate)(
            candidate.base, candidate.version, withdrawn=candidate.withdrawn
        )

    def candidate_prereleases(self, package: str) -> bool | None:
        """Return the host's explicit prerelease policy, or automatic selection."""
        del package
        return None

    def order_candidate_batch(
        self, package: str, candidates: Sequence[CandidateType]
    ) -> list[CandidateType]:
        """Order a release batch, retaining equal-version file order."""
        del package
        return sorted(candidates, key=lambda candidate: candidate.version, reverse=True)

    def order_candidates(
        self,
        package: str,
        allowed: VersionRange,
        choices: Sequence[CandidateType],
    ) -> list[CandidateType]:
        """Order live and withdrawn release batches using the host's policy."""
        result: list[CandidateType] = []
        prereleases = self.candidate_prereleases(package)
        for withdrawn in (False, True):
            remaining = [
                candidate for candidate in choices if candidate.withdrawn == withdrawn
            ]
            while remaining:
                accepted = list(
                    allowed.filter(
                        remaining,
                        key=lambda candidate: candidate.version,
                        prereleases=prereleases,
                    )
                )
                if not accepted:
                    break

                # Automatic prerelease fallback is reconsidered after each batch.
                result.extend(self.order_candidate_batch(package, accepted))
                exhausted = set(accepted)
                remaining = [
                    candidate for candidate in remaining if candidate not in exhausted
                ]
        return result


CandidateDataType = TypeVar("CandidateDataType", bound=VersionCandidateData)


class PreparedVersionCatalogue(
    VersionCatalogue[CandidateType], ABC, Generic[CandidateType, CandidateDataType]
):
    """Retain candidate lists and append-only metadata facts for one resolution."""

    def __init__(self, *, unavailable_reason: str) -> None:
        """Start one empty snapshot and its unavailable-read explanation."""
        self.candidates: dict[str, tuple[CandidateType, ...]] = {}
        self.facts: dict[CandidateType, CandidateDataType | None] = {}
        self.unavailable: set[CandidateType] = set()
        self.missing_listings: set[str] = set()
        self.unavailable_reason = unavailable_reason

    def choices(self, package: str) -> tuple[CandidateType, ...]:
        """Reuse a complete listing without rereading the host's source."""
        cached = self.candidates.get(package)
        if cached is None:
            cached = tuple(dict.fromkeys(self.read_choices(package)))
            self.candidates[package] = cached
        return cached

    @abstractmethod
    def read_choices(self, package: str) -> Sequence[CandidateType]:
        """Read stable candidate identities without preparing their metadata."""
        ...

    def prepare(self, candidate: CandidateType) -> CandidateDataType | None:
        """Cache final metadata outcomes while keeping unavailable reads unresolved."""
        if candidate in self.facts:
            return self.facts[candidate]
        if candidate in self.unavailable:
            return None

        data = self.read_candidate(candidate)
        # An unavailable read cannot certify that this candidate is rejected.
        if candidate not in self.unavailable:
            self.facts[candidate] = data
        return data

    @abstractmethod
    def read_candidate(self, candidate: CandidateType) -> CandidateDataType | None:
        """Prepare metadata or record why a read remains unavailable."""
        ...
