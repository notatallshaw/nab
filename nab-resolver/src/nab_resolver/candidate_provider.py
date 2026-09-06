"""Drive the resolver with candidates prepared and ordered by a host."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from ._compat import override
from .priority import (
    MAX_PRECHECK_BACKTRACKS,
    PRECHECK_REJECTION_THRESHOLD,
    compute_tier,
)
from .resolver import BaseProvider
from .types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    RootRequirement,
    Term,
)

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


class _QueryFeedback(Generic[_PackageT]):
    """Count contextual failures for one solve without changing candidate admission."""

    __slots__ = ("parent_failures", "target_failures")

    def __init__(self) -> None:
        self.parent_failures: dict[_PackageT, int] = {}
        self.target_failures: dict[_PackageT, int] = {}

    def clear(self) -> None:
        """Start a new solve with no inherited ordering feedback."""
        self.parent_failures.clear()
        self.target_failures.clear()

    def record(self, package: _PackageT, parents: Iterable[_PackageT]) -> None:
        """Credit the query target and each current declaring package."""
        self.target_failures[package] = self.target_failures.get(package, 0) + 1
        for parent in parents:
            self.parent_failures[parent] = self.parent_failures.get(parent, 0) + 1

    def priority(self, package: _PackageT, host_priority: Any) -> tuple[int, int, Any]:
        """Prefer declaring packages, then failed targets, before the host's key."""
        return (
            -self.parent_failures.get(package, 0),
            -self.target_failures.get(package, 0),
            host_priority,
        )


class _PrecheckFeedback(Generic[_PackageT, _KeyT]):
    """Bound retreat requests from distinct candidates sharing a selected blocker."""

    __slots__ = ("counts", "rejected", "targets")

    def __init__(self) -> None:
        self.rejected: dict[tuple[_PackageT, _PackageT, _KeyT], set[_KeyT]] = {}
        self.counts: dict[_PackageT, int] = {}
        self.targets: list[_PackageT] = []

    def clear(self) -> None:
        """Forget rejection history at the start of a new solve."""
        self.rejected.clear()
        self.counts.clear()
        self.targets.clear()

    def record(
        self, package: _PackageT, key: _KeyT, blocker: _PackageT, blocked_key: _KeyT
    ) -> None:
        """Record a distinct candidate and request a retreat at the shared threshold."""
        requests = self.counts.get(blocker, 0)
        if requests >= MAX_PRECHECK_BACKTRACKS:
            return
        rejected = self.rejected.setdefault((package, blocker, blocked_key), set())
        rejected.add(key)
        if (
            len(rejected) >= PRECHECK_REJECTION_THRESHOLD
            and blocker not in self.targets
        ):
            self.targets.append(blocker)
            self.counts[blocker] = requests + 1
            rejected.clear()

    def consume_targets(self) -> list[_PackageT]:
        """Drain requested blockers without clearing their per-solve counts."""
        targets, self.targets = self.targets, []
        return targets

    def requested(self, package: _PackageT) -> bool:
        """Whether the package has been named in a retreat request this solve."""
        return package in self.counts


class CandidateProvider(BaseProvider[_PackageT, _KeyT]):
    """Keep candidate identity and active requirements around a host's selection."""

    def __init__(
        self,
        host: CandidateHost[_PackageT, _KeyT],
        roots: Sequence[CandidateRequirement[_PackageT, _KeyT]],
        *,
        query_feedback: bool = False,
        conflict_feedback: bool = False,
        dependency_precheck: bool = False,
        precheck_feedback: bool = False,
    ) -> None:
        """Snapshot roots and configure optional ordering and dependency prechecks."""
        if precheck_feedback and not dependency_precheck:
            message = "precheck_feedback requires dependency_precheck"
            raise ValueError(message)
        self.host = host
        self.roots = tuple(roots)
        self._query_feedback = _QueryFeedback[_PackageT]() if query_feedback else None
        self._conflict_feedback = conflict_feedback
        self._dependency_precheck = dependency_precheck
        self._precheck_feedback = (
            _PrecheckFeedback[_PackageT, _KeyT]() if precheck_feedback else None
        )
        self._positive_ranges: Mapping[_PackageT, RangeProtocol[_KeyT]] = {}
        self._pending: list[Incompatibility[_PackageT, _KeyT]] = []
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

    @override
    def begin_resolution(self) -> None:
        """Clear ordering feedback while retaining prepared candidate metadata."""
        if self._query_feedback is not None:
            self._query_feedback.clear()
        self._pending.clear()
        if self._precheck_feedback is not None:
            self._precheck_feedback.clear()
        if self._dependency_precheck:
            self._positive_ranges = {}
            self._decisions = {}
            self._active_cache = None

    @override
    def receive_contextual_failure(self, package: _PackageT) -> bool:
        """Credit the failed query and its currently decided declaring packages."""
        if self._query_feedback is None:
            return False

        parents = (
            parent
            for parent, key in self._decisions.items()
            if any(
                cause.package == package
                for cause in self._causes.get((parent, key), ())
            )
        )
        self._query_feedback.record(package, parents)
        return True

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
        """Prepare a candidate or queue its conflicting dependency before selection."""
        for candidate in self.host.iter_candidates(
            package, version_range, self.active_requirements()
        ):
            if candidate.key in version_range:
                self._selected[package, candidate.key] = candidate
                if self._dependency_precheck and self._precheck_dependencies(
                    package, candidate.key, version_range
                ):
                    return None
                return candidate.key
        return None

    def _precheck_dependencies(
        self,
        package: _PackageT,
        key: _KeyT,
        allowed: RangeProtocol[_KeyT],
    ) -> bool:
        """Queue a dependency conflict without deciding the rejected candidate."""
        dependencies = self.get_dependencies(package, key)
        if package in dependencies:
            return False

        for dependency, required in dependencies.items():
            if dependency not in self._decisions:
                continue
            assignment = self._positive_ranges.get(dependency)
            if assignment is None or not assignment.is_disjoint(required):
                continue
            selected = type(required).singleton(self._decisions[dependency])
            if not selected.is_disjoint(required):
                continue

            self._pending.append(
                Incompatibility(
                    [
                        Term(package, type(allowed).singleton(key), positive=True),
                        Term(dependency, required, positive=False),
                    ],
                    cause=IncompatibilityCause.DEPENDENCY,
                )
            )
            if self._precheck_feedback is not None:
                self._precheck_feedback.record(
                    package, key, dependency, self._decisions[dependency]
                )
            return True
        return False

    @override
    def consume_pending_clauses(self) -> list[Incompatibility[_PackageT, _KeyT]]:
        """Drain prechecked dependencies before an ordinary absence is recorded."""
        pending, self._pending = self._pending, []
        return pending

    @override
    def consume_force_backtrack_targets(self) -> list[_PackageT]:
        """Request bounded retreats after repeated proven precheck blockers."""
        if self._precheck_feedback is None:
            return []
        return self._precheck_feedback.consume_targets()

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
        if self._dependency_precheck:
            self._positive_ranges = positive_ranges
        self._decisions = decisions
        self._active_cache = None

    def prioritize(
        self,
        package: _PackageT,
        version_range: RangeProtocol[_KeyT],
        conflict_counts: Mapping[_PackageT, int],
        culprit_counts: Mapping[_PackageT, int] | None = None,
    ) -> Any:
        """Order query feedback (parent, target), conflict tier, and host preference."""
        del version_range
        priority = self.host.priority(package, self.active_requirements())
        if self._conflict_feedback or self._precheck_feedback is not None:
            affected = conflict_counts.get(package, 0) if self._conflict_feedback else 0
            counts = culprit_counts if self._conflict_feedback else None
            culprit = 0 if counts is None else counts.get(package, 0)
            forced = (
                self._precheck_feedback is not None
                and self._precheck_feedback.requested(package)
            )
            priority = (
                compute_tier(
                    package, affected, culprit, counts, force_backtracked=forced
                ),
                priority,
            )
        if self._query_feedback is not None:
            return self._query_feedback.priority(package, priority)
        return priority

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
