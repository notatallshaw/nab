"""Drive the resolver with candidates prepared and ordered by a host."""

from __future__ import annotations

from collections import defaultdict, deque
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

    from .resolver import Solution


PackageType = TypeVar("PackageType", bound=Hashable)
CandidateKeyType = TypeVar("CandidateKeyType", bound=Hashable)

__all__ = [
    "CandidateHost",
    "CandidateProvider",
    "CandidateRequirement",
    "PreparedCandidate",
]


class CandidateRequirement(Generic[PackageType, CandidateKeyType]):
    """A solver restriction retaining the host's original requirement object."""

    __slots__ = ("constraint", "origin", "package")

    def __init__(
        self,
        package: PackageType,
        constraint: RangeProtocol[CandidateKeyType],
        origin: object,
    ) -> None:
        """Retain stable host inputs for the duration of a resolve."""
        self.package = package
        self.constraint = constraint
        self.origin = origin


class PreparedCandidate(Generic[CandidateKeyType]):
    """A usable candidate retaining the host's prepared install object."""

    __slots__ = ("key", "origin")

    def __init__(self, key: CandidateKeyType, origin: object) -> None:
        """Associate one stable candidate key with its host object."""
        self.key = key
        self.origin = origin


class CandidateHost(Protocol[PackageType, CandidateKeyType]):
    """Prepare candidates using the host's current original requirements.

    Eligibility for a fixed requirement set must not change because unrelated
    metadata was cached. Source registries identify candidates; they do not
    make every discovered source selectable.
    """

    def iter_candidates(
        self,
        package: PackageType,
        allowed: RangeProtocol[CandidateKeyType],
        requirements: Mapping[
            PackageType, Sequence[CandidateRequirement[PackageType, CandidateKeyType]]
        ],
    ) -> Iterable[PreparedCandidate[CandidateKeyType]]:
        """Yield usable candidates in native host order, preserving file fallbacks."""
        ...

    def get_dependencies(
        self, candidate: PreparedCandidate[CandidateKeyType]
    ) -> Iterable[CandidateRequirement[PackageType, CandidateKeyType]]:
        """Bind active dependencies without changing unrelated source eligibility."""
        ...

    def priority(
        self,
        package: PackageType,
        requirements: Sequence[CandidateRequirement[PackageType, CandidateKeyType]],
    ) -> Any:
        """Rank this package from its declarations and fixed host policy.

        The result must remain stable while these declarations are unchanged.
        """
        ...


class _QueryFeedback(Generic[PackageType]):
    """Count contextual failures for one solve without changing candidate admission."""

    __slots__ = ("parent_failures", "target_failures")

    def __init__(self) -> None:
        self.parent_failures: dict[PackageType, int] = {}
        self.target_failures: dict[PackageType, int] = {}

    def clear(self) -> None:
        """Start a new solve with no inherited ordering feedback."""
        self.parent_failures.clear()
        self.target_failures.clear()

    def record(self, package: PackageType, parents: Iterable[PackageType]) -> None:
        """Credit the query target and each current declaring package."""
        self.target_failures[package] = self.target_failures.get(package, 0) + 1
        for parent in parents:
            self.parent_failures[parent] = self.parent_failures.get(parent, 0) + 1

    def priority(
        self, package: PackageType, host_priority: Any
    ) -> tuple[int, int, Any]:
        """Prefer declaring packages, then failed targets, before the host's key."""
        return (
            -self.parent_failures.get(package, 0),
            -self.target_failures.get(package, 0),
            host_priority,
        )


class _PrecheckFeedback(Generic[PackageType, CandidateKeyType]):
    """Bound retreat requests from distinct candidates sharing a selected blocker."""

    __slots__ = ("counts", "parents", "rejected", "targets")

    def __init__(self) -> None:
        self.rejected: dict[
            tuple[PackageType, PackageType, CandidateKeyType], set[CandidateKeyType]
        ] = {}
        self.counts: dict[PackageType, int] = {}
        self.parents: dict[PackageType, set[PackageType]] = {}
        self.targets: list[PackageType] = []

    def clear(self) -> None:
        """Forget rejection history at the start of a new solve."""
        self.rejected.clear()
        self.counts.clear()
        self.parents.clear()
        self.targets.clear()

    def record(
        self,
        package: PackageType,
        key: CandidateKeyType,
        blocker: PackageType,
        blocked_key: CandidateKeyType,
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
            self.parents.setdefault(blocker, set()).add(package)
            rejected.clear()

    def consume_targets(self) -> list[PackageType]:
        """Drain requested blockers without clearing their per-solve counts."""
        targets = self.targets
        self.targets = []
        return targets

    def requested(self, package: PackageType) -> bool:
        """Whether a requesting parent still has to be decided."""
        return package in self.parents

    def decided(self, package: PackageType) -> bool:
        """Expire blockers whose requesting parents have all been decided."""
        if not self.parents:
            return False
        expired = []
        for blocker, parents in self.parents.items():
            parents.discard(package)
            if not parents:
                expired.append(blocker)

        for blocker in expired:
            del self.parents[blocker]

        return bool(expired)


class CandidateProvider(BaseProvider[PackageType, CandidateKeyType]):
    """Keep candidate identity and active requirements around a host's selection."""

    def __init__(
        self,
        host: CandidateHost[PackageType, CandidateKeyType],
        roots: Sequence[CandidateRequirement[PackageType, CandidateKeyType]],
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
        self._query_feedback = _QueryFeedback[PackageType]() if query_feedback else None
        self._conflict_feedback = conflict_feedback
        self._dependency_precheck = dependency_precheck
        self._precheck_feedback = (
            _PrecheckFeedback[PackageType, CandidateKeyType]()
            if precheck_feedback
            else None
        )

        self._positive_ranges: Mapping[
            PackageType, RangeProtocol[CandidateKeyType]
        ] = {}
        self._pending: list[Incompatibility[PackageType, CandidateKeyType]] = []
        self._decisions: Mapping[PackageType, CandidateKeyType] = {}
        self._selected: dict[
            tuple[PackageType, CandidateKeyType], PreparedCandidate[CandidateKeyType]
        ] = {}
        self._causes: dict[
            tuple[PackageType, CandidateKeyType],
            tuple[CandidateRequirement[PackageType, CandidateKeyType], ...],
        ] = {}
        self._dependencies: dict[
            tuple[PackageType, CandidateKeyType],
            dict[PackageType, RangeProtocol[CandidateKeyType]],
        ] = {}

        # A decision snapshot owns its immutable declaration view.
        self._active_cache: (
            Mapping[
                PackageType,
                tuple[CandidateRequirement[PackageType, CandidateKeyType], ...],
            ]
            | None
        ) = None
        self._priority_requirements: Mapping[
            PackageType, tuple[CandidateRequirement[PackageType, CandidateKeyType], ...]
        ] = {}

    @override
    def begin_resolution(self) -> None:
        """Clear ordering feedback while retaining prepared candidate metadata."""
        if self._query_feedback is not None:
            self._query_feedback.clear()

        self._pending.clear()
        self._priority_requirements = {}
        if self._precheck_feedback is not None:
            self._precheck_feedback.clear()

        if self._dependency_precheck:
            self._positive_ranges = {}
            self._decisions = {}
            self._active_cache = None

    @override
    def receive_contextual_failure(self, package: PackageType) -> bool:
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

    @override
    def receive_decision(self, package: PackageType, version: CandidateKeyType) -> bool:
        """Expire forced demotion once the requesting parents are decided."""
        del version
        if self._precheck_feedback is None:
            return False

        return self._precheck_feedback.decided(package)

    def root_requirements(self) -> list[RootRequirement[PackageType, CandidateKeyType]]:
        """Return solver roots with their original host provenance attached."""
        return [
            RootRequirement(root.package, root.constraint, root.origin)
            for root in self.roots
        ]

    def active_requirements(
        self,
    ) -> Mapping[
        PackageType, tuple[CandidateRequirement[PackageType, CandidateKeyType], ...]
    ]:
        """Return read-only requirement collections for the decision snapshot."""
        if self._active_cache is None:
            self._active_cache = self._build_active_requirements()
        return self._active_cache

    def consume_priority_changes(self) -> set[PackageType]:
        """Report declaration changes even when the solver's ranges did not move."""
        current = self.active_requirements()
        previous = self._priority_requirements
        self._priority_requirements = current
        return {
            package
            for package in previous.keys() | current.keys()
            if previous.get(package) != current.get(package)
        }

    def _build_active_requirements(
        self,
    ) -> Mapping[
        PackageType, tuple[CandidateRequirement[PackageType, CandidateKeyType], ...]
    ]:
        """Collect roots and dependencies of candidates in the current solution."""
        requirements: dict[
            PackageType, list[CandidateRequirement[PackageType, CandidateKeyType]]
        ] = defaultdict(list)
        for root in self.roots:
            requirements[root.package].append(root)

        for package, key in self._decisions.items():
            for cause in self._causes.get((package, key), ()):
                requirements[cause.package].append(cause)

        return MappingProxyType(
            {package: tuple(causes) for package, causes in requirements.items()}
        )

    def choose_version(
        self, package: PackageType, version_range: RangeProtocol[CandidateKeyType]
    ) -> CandidateKeyType | None:
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
        package: PackageType,
        key: CandidateKeyType,
        allowed: RangeProtocol[CandidateKeyType],
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
            selected = required.singleton(self._decisions[dependency])
            if not selected.is_disjoint(required):
                continue

            self._pending.append(
                Incompatibility(
                    [
                        Term(package, allowed.singleton(key), positive=True),
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
    def consume_pending_clauses(
        self,
    ) -> list[Incompatibility[PackageType, CandidateKeyType]]:
        """Drain prechecked dependencies before an ordinary absence is recorded."""
        pending = self._pending
        self._pending = []
        return pending

    @override
    def consume_force_backtrack_targets(self) -> list[PackageType]:
        """Request bounded retreats after repeated proven precheck blockers."""
        if self._precheck_feedback is None:
            return []

        return self._precheck_feedback.consume_targets()

    @override
    def is_query_ready(self, package: PackageType) -> bool:
        """Require original declarations before an early provisional absence."""
        return bool(self.active_requirements().get(package, ()))

    def has_satisfying_version(
        self, package: PackageType, version_range: RangeProtocol[CandidateKeyType]
    ) -> bool:
        """Probe candidates without recording a solver selection."""
        return any(
            candidate.key in version_range
            for candidate in self.host.iter_candidates(
                package, version_range, self.active_requirements()
            )
        )

    def widen_decision(self, package: PackageType, version: CandidateKeyType) -> None:
        """Keep dependency clauses tied to one candidate identity."""
        del package, version

    def get_dependencies(
        self, package: PackageType, version: CandidateKeyType
    ) -> dict[PackageType, RangeProtocol[CandidateKeyType]]:
        """Return dependency restrictions, stopping at the first empty result."""
        key = package, version
        cached = self._dependencies.get(key)
        if cached is not None:
            return cached

        candidate = self._selected[key]
        causes = []
        dependencies: dict[PackageType, RangeProtocol[CandidateKeyType]] = {}
        for cause in self.host.get_dependencies(candidate):
            causes.append(cause)
            previous = dependencies.get(cause.package)
            dependencies[cause.package] = (
                cause.constraint if previous is None else previous & cause.constraint
            )
            if dependencies[cause.package].is_empty:
                break

        # Metadata survives backtracking; only active declarations expire.
        self._active_cache = None
        self._causes[key] = tuple(causes)
        self._dependencies[key] = dependencies
        return dependencies

    @override
    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[PackageType, RangeProtocol[CandidateKeyType]],
        decisions: Mapping[PackageType, CandidateKeyType],
    ) -> None:
        """Retain the current decisions so rejected parents lose their requirements."""
        if self._dependency_precheck:
            self._positive_ranges = positive_ranges

        self._decisions = decisions
        self._active_cache = None

    def prioritize(
        self,
        package: PackageType,
        version_range: RangeProtocol[CandidateKeyType],
        conflict_counts: Mapping[PackageType, int],
        culprit_counts: Mapping[PackageType, int] | None = None,
    ) -> Any:
        """Order query feedback (parent, target), conflict tier, and host preference."""
        del version_range
        priority = self.host.priority(
            package, self.active_requirements().get(package, ())
        )
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

    def prepared(
        self, package: PackageType, key: CandidateKeyType
    ) -> PreparedCandidate[CandidateKeyType]:
        """Return the exact prepared candidate used by a solver decision."""
        return self._selected[package, key]

    def validate_solution(
        self,
        solution: Solution[PackageType, CandidateKeyType],
        constraints: Mapping[PackageType, RangeProtocol[CandidateKeyType]]
        | None = None,
    ) -> bool:
        """Check complete declarations, constraints and final host admission."""
        requirements = self._validation_requirements(solution)
        if requirements is None or requirements.keys() != solution.pins.keys():
            return False

        for package, key in solution.pins.items():
            constraint = None if constraints is None else constraints.get(package)
            if constraint is not None and key not in constraint:
                return False
            if any(key not in cause.constraint for cause in requirements[package]):
                return False

            allowed = requirements[package][0].constraint.singleton(key)
            if not any(
                offered.key == key
                for offered in self.host.iter_candidates(package, allowed, requirements)
            ):
                return False
        return True

    def _validation_requirements(
        self, solution: Solution[PackageType, CandidateKeyType]
    ) -> (
        dict[PackageType, list[CandidateRequirement[PackageType, CandidateKeyType]]]
        | None
    ):
        """Rebuild reachable declarations, requiring a prepared pin for each package."""
        requirements: dict[
            PackageType, list[CandidateRequirement[PackageType, CandidateKeyType]]
        ] = defaultdict(list)
        pending = deque(root.package for root in self.roots)
        for root in self.roots:
            requirements[root.package].append(root)

        visited = set()
        while pending:
            package = pending.popleft()
            if package in visited:
                continue
            if package not in solution.pins:
                return None

            candidate = self._selected.get((package, solution.pins[package]))
            if candidate is None:
                return None

            visited.add(package)
            for cause in self.host.get_dependencies(candidate):
                requirements[cause.package].append(cause)
                pending.append(cause.package)
        return requirements

    def causes_for(
        self, package: PackageType, key: CandidateKeyType
    ) -> tuple[CandidateRequirement[PackageType, CandidateKeyType], ...]:
        """Return the host dependency declarations consumed for this candidate."""
        return self._causes.get((package, key), ())

    def recorded_candidates(
        self,
    ) -> Iterable[tuple[PackageType, PreparedCandidate[CandidateKeyType]]]:
        """Iterate prepared decisions retained for failure provenance."""
        return (
            (package, candidate) for (package, _), candidate in self._selected.items()
        )
