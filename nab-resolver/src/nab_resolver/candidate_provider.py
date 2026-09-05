"""Drive the resolver with candidates prepared and ordered by a host."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from ._compat import override
from .resolver import BaseProvider
from .types import RangeProtocol, RootRequirement

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


_PackageT = TypeVar("_PackageT", bound=Hashable)
_KeyT = TypeVar("_KeyT", bound=Hashable)

__all__ = [
    "CandidateHost",
    "CandidateProvider",
    "CandidateRequirement",
    "PreparedCandidate",
]


class CandidateRequirement(Generic[_PackageT, _KeyT]):
    """A solver restriction retaining the host's original requirement object."""

    __slots__ = ("constraint", "origin", "package")

    def __init__(
        self, package: _PackageT, constraint: RangeProtocol[_KeyT], origin: object
    ) -> None:
        """Retain stable host inputs for the duration of a resolve."""
        self.package = package
        self.constraint = constraint
        self.origin = origin


class PreparedCandidate(Generic[_KeyT]):
    """A usable candidate retaining the host's prepared install object."""

    __slots__ = ("key", "origin")

    def __init__(self, key: _KeyT, origin: object) -> None:
        """Associate one stable candidate key with its host object."""
        self.key = key
        self.origin = origin


class CandidateHost(Protocol[_PackageT, _KeyT]):
    """Prepare candidates using the host's current original requirements.

    Eligibility for a fixed requirement set must not change because unrelated
    metadata was cached. Source registries identify candidates; they do not
    make every discovered source selectable.
    """

    def iter_candidates(
        self,
        package: _PackageT,
        allowed: RangeProtocol[_KeyT],
        requirements: Mapping[
            _PackageT, Sequence[CandidateRequirement[_PackageT, _KeyT]]
        ],
    ) -> Iterable[PreparedCandidate[_KeyT]]:
        """Yield usable candidates in native host order, preserving file fallbacks."""
        ...

    def get_dependencies(
        self, candidate: PreparedCandidate[_KeyT]
    ) -> Iterable[CandidateRequirement[_PackageT, _KeyT]]:
        """Bind active dependencies without changing unrelated source eligibility."""
        ...

    def priority(
        self,
        package: _PackageT,
        requirements: Mapping[
            _PackageT, Sequence[CandidateRequirement[_PackageT, _KeyT]]
        ],
    ) -> Any:
        """Order package decisions without preparing candidate metadata."""
        ...


class CandidateProvider(BaseProvider[_PackageT, _KeyT]):
    """Keep candidate identity and active requirements around a host's selection."""

    def __init__(
        self,
        host: CandidateHost[_PackageT, _KeyT],
        roots: Sequence[CandidateRequirement[_PackageT, _KeyT]],
    ) -> None:
        """Snapshot roots and retain selected dependency provenance."""
        self.host = host
        self.roots = tuple(roots)
        self._decisions: Mapping[_PackageT, _KeyT] = {}
        self._selected: dict[tuple[_PackageT, _KeyT], PreparedCandidate[_KeyT]] = {}
        self._causes: dict[
            tuple[_PackageT, _KeyT], tuple[CandidateRequirement[_PackageT, _KeyT], ...]
        ] = {}
        self._dependencies: dict[
            tuple[_PackageT, _KeyT], dict[_PackageT, RangeProtocol[_KeyT]]
        ] = {}
        self._active_cache: (
            Mapping[_PackageT, tuple[CandidateRequirement[_PackageT, _KeyT], ...]]
            | None
        ) = None

    def root_requirements(self) -> list[RootRequirement[_PackageT, _KeyT]]:
        """Return solver roots with their original host provenance attached."""
        return [
            RootRequirement(root.package, root.constraint, root.origin)
            for root in self.roots
        ]

    def active_requirements(
        self,
    ) -> Mapping[_PackageT, tuple[CandidateRequirement[_PackageT, _KeyT], ...]]:
        """Return read-only requirement collections for the decision snapshot."""
        if self._active_cache is None:
            self._active_cache = self._build_active_requirements()
        return self._active_cache

    def _build_active_requirements(
        self,
    ) -> Mapping[_PackageT, tuple[CandidateRequirement[_PackageT, _KeyT], ...]]:
        """Collect roots and dependencies of candidates in the current solution."""
        requirements: dict[_PackageT, list[CandidateRequirement[_PackageT, _KeyT]]] = (
            defaultdict(list)
        )
        for root in self.roots:
            requirements[root.package].append(root)
        for package, key in self._decisions.items():
            for cause in self._causes.get((package, key), ()):
                requirements[cause.package].append(cause)
        return MappingProxyType(
            {package: tuple(causes) for package, causes in requirements.items()}
        )

    def choose_version(
        self, package: _PackageT, version_range: RangeProtocol[_KeyT]
    ) -> _KeyT | None:
        """Take the first prepared candidate admitted by the solver's bounds."""
        for candidate in self.host.iter_candidates(
            package, version_range, self.active_requirements()
        ):
            if candidate.key in version_range:
                self._selected[package, candidate.key] = candidate
                return candidate.key
        return None

    def has_satisfying_version(
        self, package: _PackageT, version_range: RangeProtocol[_KeyT]
    ) -> bool:
        """Probe candidates without recording a solver selection."""
        return any(
            candidate.key in version_range
            for candidate in self.host.iter_candidates(
                package, version_range, self.active_requirements()
            )
        )

    def widen_decision(self, package: _PackageT, version: _KeyT) -> None:
        """Keep dependency clauses tied to one candidate identity."""
        del package, version

    def get_dependencies(
        self, package: _PackageT, version: _KeyT
    ) -> dict[_PackageT, RangeProtocol[_KeyT]]:
        """Return dependency restrictions, stopping at the first empty result."""
        key = package, version
        cached = self._dependencies.get(key)
        if cached is not None:
            return cached
        candidate = self._selected[key]
        causes = []
        dependencies: dict[_PackageT, RangeProtocol[_KeyT]] = {}
        for cause in self.host.get_dependencies(candidate):
            causes.append(cause)
            previous = dependencies.get(cause.package)
            dependencies[cause.package] = (
                cause.constraint if previous is None else previous & cause.constraint
            )
            if dependencies[cause.package].is_empty:
                break
        self._active_cache = None
        self._causes[key] = tuple(causes)
        self._dependencies[key] = dependencies
        return dependencies

    @override
    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[_PackageT, RangeProtocol[_KeyT]],
        decisions: Mapping[_PackageT, _KeyT],
    ) -> None:
        """Retain the current decisions so rejected parents lose their requirements."""
        del positive_ranges
        self._decisions = decisions
        self._active_cache = None

    def prioritize(
        self,
        package: _PackageT,
        version_range: RangeProtocol[_KeyT],
        conflict_counts: Mapping[_PackageT, int],
        culprit_counts: Mapping[_PackageT, int] | None = None,
    ) -> Any:
        """Use the host's package priority without listing candidates."""
        del version_range, conflict_counts, culprit_counts
        return self.host.priority(package, self.active_requirements())

    def prepared(self, package: _PackageT, key: _KeyT) -> PreparedCandidate[_KeyT]:
        """Return the exact prepared candidate used by a solver decision."""
        return self._selected[package, key]

    def causes_for(
        self, package: _PackageT, key: _KeyT
    ) -> tuple[CandidateRequirement[_PackageT, _KeyT], ...]:
        """Return the host dependency declarations consumed for this candidate."""
        return self._causes.get((package, key), ())

    def recorded_candidates(
        self,
    ) -> Iterable[tuple[_PackageT, PreparedCandidate[_KeyT]]]:
        """Iterate prepared decisions retained for failure provenance."""
        return (
            (package, candidate) for (package, _), candidate in self._selected.items()
        )
