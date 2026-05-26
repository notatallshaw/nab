"""PubGrub dependency resolver.

Implements unit propagation, conflict resolution with clause learning, and
non-chronological backjumping.  PubGrub was designed by Natalie Weizenbaum
for Dart's pub, adapting CDCL (conflict-driven clause learning) from SAT
solving to version resolution.

The phase functions live in :mod:`nab_resolver.propagate`,
:mod:`nab_resolver.conflict`, :mod:`nab_resolver.decide`, and
:mod:`nab_resolver.incompat_index`.  ``Resolver`` is a thin coordinator that
holds shared state and delegates to those modules.  State attributes are
named without leading underscores so the phase modules can read and mutate
them directly; the supported public API is ``__init__``, ``resolve``, and
``stats``.

Specification: https://github.com/dart-lang/pub/blob/master/doc/solver.md
Original blog post: https://nex3.medium.com/pubgrub-2fb6470504f
Rust implementation: https://github.com/pubgrub-rs/pubgrub
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Protocol

from . import conflict, decide, incompat_index, propagate
from .errors import ResolutionError
from .partial_solution import PartialSolution
from .ranges import Range
from .result import build_reachable_decisions
from .root import ROOT
from .types import (
    Incompatibility,
    IncompatibilityCause,
    IncompatibilityState,
    PackageType,
    RangeProtocol,
    SetRelation,
    Term,
    VersionType,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


__all__ = [
    "Incompatibility",
    "IncompatibilityCause",
    "IncompatibilityState",
    "ResolutionError",
    "Resolver",
    "ResolverObserver",
    "ResolverProvider",
    "ResolverStats",
    "SetRelation",
    "Term",
]


class ResolverProvider(Protocol[PackageType, VersionType]):
    """Interface for supplying version and dependency information.

    Modeled after pubgrub-rs v0.3+ ``DependencyProvider``:
    https://docs.rs/pubgrub/latest/pubgrub/trait.DependencyProvider.html
    """

    def choose_version(
        self, package: PackageType, version_range: RangeProtocol[VersionType]
    ) -> VersionType | None:
        """Pick a version for package within version_range, or None."""
        ...

    def get_dependencies(
        self, package: PackageType, version: VersionType
    ) -> Mapping[PackageType, RangeProtocol[VersionType]]:
        """Return ``{dependency_package: required_range}`` for this version."""
        ...

    def prioritize(
        self,
        package: PackageType,
        version_range: RangeProtocol[VersionType],
        conflict_counts: Mapping[PackageType, int],
        culprit_counts: Mapping[PackageType, int] | None = None,
    ) -> Any:
        """Return a sort key for deciding which package to resolve next.

        Lower values resolve first.  ``conflict_counts`` tracks how often a
        decision on this package was discarded; ``culprit_counts`` tracks how
        often this package was decided earlier and caused another's decision
        to be discarded.
        """
        ...

    def is_ready(self, package: PackageType) -> bool:
        """Return True when the provider can answer cheaply for ``package``.

        Lets the resolver prefer ready packages while async fetches are still
        in flight.  Providers without an async layer should return True.
        """
        ...

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[PackageType, RangeProtocol[VersionType]],
        decisions: Mapping[PackageType, VersionType],
    ) -> None:
        """Accept a snapshot of positive ranges and decisions.

        Called before ``choose_version`` so providers can forward-check the
        candidate's dependencies against accumulated constraints.
        ``decisions`` is the subset with concrete versions (not derivations);
        decision-based reasoning is safer because decisions cannot be undone
        in isolation.  Default is a no-op.
        """
        ...

    def consume_pending_clauses(
        self,
    ) -> list[Incompatibility[PackageType, VersionType]]:
        """Return incompatibilities the provider queued during ``choose_version``.

        Drained after every ``choose_version`` call.  When non-empty AND
        ``choose_version`` returned None, the resolver suppresses the default
        ``NO_VERSIONS`` clause (which would persist across backjumps) so the
        provider's context-aware clauses become the source of truth.
        """
        ...

    def consume_force_backtrack_targets(self) -> list[PackageType]:
        """Return packages the provider wants force-backtracked.

        Drained after every ``choose_version`` call. When non-empty,
        the resolver bumps each package's culprit count past the
        demote threshold, queues it, and fires
        ``apply_targeted_backtrack`` without waiting for the normal
        conflict-count gate.

        Providers without a force-backtrack signal return an empty list.
        """
        ...


@dataclass
class ResolverStats(Generic[PackageType]):
    """Running statistics for resolution observability.

    Inspired by SAT solver statistics (MiniSat, CaDiCaL) which track
    decisions, conflicts, propagations, and restarts as standard metrics.
    See: https://minisat.se/MiniSat.html
    """

    rounds: int = 0
    decisions: int = 0
    conflicts: int = 0
    derivations: int = 0
    backjumps: int = 0
    restarts: int = 0
    targeted_backtracks: int = 0
    incompatibilities_learned: int = 0
    conflict_threshold_crossings: int = 0
    culprit_threshold_crossings: int = 0
    package_conflict_counts: defaultdict[PackageType, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    package_culprit_counts: defaultdict[PackageType, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    # Conflict-depth histograms: learned-clause term count and backjump
    # distance (from_level - target), each mapping value -> frequency.
    learned_clause_term_counts: defaultdict[int, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    backjump_distances: defaultdict[int, int] = field(
        default_factory=lambda: defaultdict(int)
    )


class ResolverObserver(Generic[PackageType, VersionType]):
    """Override methods to observe resolution events."""

    def on_decision(
        self, package: PackageType, version: VersionType, level: int
    ) -> None:
        """Handle a version decision event."""

    def on_derivation(
        self,
        package: PackageType,
        *,
        positive: bool,
        cause: Incompatibility[PackageType, VersionType],
    ) -> None:
        """Handle a derivation from unit propagation."""

    def on_conflict(
        self, incompatibility: Incompatibility[PackageType, VersionType]
    ) -> None:
        """Handle a conflict detection event."""

    def on_learned(
        self, incompatibility: Incompatibility[PackageType, VersionType]
    ) -> None:
        """Handle a learned incompatibility event."""

    def on_backjump(self, from_level: int, to_level: int) -> None:
        """Handle a backjump event."""

    def on_no_versions(
        self, package: PackageType, version_range: RangeProtocol[VersionType]
    ) -> None:
        """Handle a no-versions-available event."""

    def on_conflict_step(
        self,
        incompatibility: Incompatibility[PackageType, VersionType],
        *,
        satisfier_package: PackageType,
        satisfier_is_decision: bool,
        satisfier_level: int,
        previous_level: int,
        can_backjump: bool,
    ) -> None:
        """Handle one iteration of the conflict resolution loop."""


class Resolver(Generic[PackageType, VersionType]):
    """PubGrub dependency resolver.

    The main loop follows the PubGrub specification:
    1. Unit propagation: derive constraints from incompatibilities
    2. Conflict resolution: learn new incompatibilities and backjump
    3. Decision making: pick the next package and version to try

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#the-algorithm
    """

    _RESTART_THRESHOLD = 5
    _MAX_RESTARTS = 3

    # A package re-queues every multiple of CULPRIT_THRESHOLD so persistent
    # lock-step clusters keep getting pruned.  TARGETED_BT_MIN_CONFLICTS keeps
    # short scenarios from paying the backtrack tax.
    CULPRIT_THRESHOLD = 5
    TARGETED_BT_MIN_CONFLICTS = 30
    MAX_TARGETED_BACKTRACKS = 64

    # Conflict count that triggers a conflict_threshold_crossings increment.
    # Must match the threshold providers use to classify a package as affected.
    CONFLICT_THRESHOLD = 5

    def __init__(
        self,
        provider: ResolverProvider[PackageType, VersionType],
        observer: ResolverObserver[PackageType, VersionType] | None = None,
        max_iterations: int = 200_000,
        range_type: type[RangeProtocol[Any]] = Range,
        root_version: Any = 1,
    ) -> None:
        """Create a resolver with the given provider and optional observer.

        The ``root_version`` is a sentinel passed to ``range_type.singleton()``
        to build the virtual root package's range.  The default ``1``
        works for :class:`~nab_resolver.ranges.Range` (which accepts any
        comparable type) but a PEP 440 range type such as
        :class:`packaging.ranges.VersionRange` requires a parseable
        version string or :class:`~packaging.version.Version` here.
        """
        self.provider = provider
        self.observer: ResolverObserver[PackageType, VersionType] = (
            observer or ResolverObserver()
        )
        self.max_iterations = max_iterations
        self.range_type = range_type
        self.root_version = root_version

        self.incompatibilities: list[Incompatibility[Any, Any]] = []
        self.package_to_incompatibilities: defaultdict[Any, list[int]] = defaultdict(
            list
        )

        # Keyed by (package, dep_package, dep_constraint, dep_positive); used
        # to merge mergeable dependency clauses (pubgrub-rs's merge_dependents).
        self.dependency_index: dict[Any, int] = {}

        self.solution: PartialSolution[Any, Any] = PartialSolution(
            range_type=range_type
        )
        self.stats: ResolverStats[PackageType] = ResolverStats()

        self.constraints: Mapping[PackageType, RangeProtocol[VersionType]] = {}
        self.injected_constraints: set[PackageType] = set()
        self.root_package_order: dict[PackageType, tuple[int, int, str]] = {}
        self.pending_targeted_backtrack: list[PackageType] = []

        # Memoises the tiebreak tuple in choose_package_to_decide.
        self.tiebreak_cache: dict[PackageType, tuple[int, int, str]] = {}

    def resolve(
        self,
        requirements: Mapping[PackageType, RangeProtocol[VersionType]],
        constraints: Mapping[PackageType, RangeProtocol[VersionType]] | None = None,
    ) -> dict[PackageType, VersionType]:
        """Resolve requirements and return ``{package: version}``.

        Constraints restrict a package's version range but do not cause
        it to be installed.  They are injected lazily: only when the
        resolver is about to decide a constrained package (meaning
        something already depends on it).

        Raises ``ResolutionError`` if no solution exists.
        """
        self._reset(constraints)
        self._add_root_requirements(requirements)

        # Threshold doubles each restart (geometric schedule).
        restart_threshold = self._RESTART_THRESHOLD
        restarts_remaining = self._MAX_RESTARTS

        changed_package: Any = ROOT

        for _ in range(self.max_iterations):
            self.stats.rounds += 1

            # Phase 1: Unit propagation.
            conflicting_incompatibility = propagate.unit_propagation(
                self, changed_package
            )

            if conflicting_incompatibility is not None:
                changed_package, restart_threshold, restarts_remaining = (
                    self._handle_conflict(
                        conflicting_incompatibility,
                        restart_threshold,
                        restarts_remaining,
                    )
                )
                continue

            # Phase 3: Decision making.
            next_package = decide.choose_package_to_decide(self)
            if next_package is None:
                # All packages decided; the spec requires filtering out
                # any unreachable extras before returning.
                return self._build_result()

            changed_package = self._decide_next(next_package)

        exceeded_message = f"Resolution exceeded {self.max_iterations} iterations"
        raise ResolutionError(exceeded_message)

    def _handle_conflict(
        self,
        conflicting_incompatibility: Incompatibility[Any, Any],
        restart_threshold: int,
        restarts_remaining: int,
    ) -> tuple[Any, int, int]:
        """Run conflict resolution, targeted backtrack, and restart phases."""
        self.stats.conflicts += 1
        self.observer.on_conflict(conflicting_incompatibility)
        learned = conflict.conflict_resolution(self, conflicting_incompatibility)
        # Re-propagate from the learned clause's first package.
        changed_package: Any = learned.terms[0].package

        triggering = conflict.maybe_targeted_backtrack(self)
        if triggering is not None:
            changed_package = triggering

        restart_threshold, restarts_remaining, restarted = conflict.maybe_restart(
            self, restart_threshold, restarts_remaining
        )
        if restarted:
            changed_package = ROOT
        return changed_package, restart_threshold, restarts_remaining

    def _decide_next(self, next_package: Any) -> Any:
        """Run the decision phase for ``next_package``. Return next changed package."""
        if decide.inject_constraint(self, next_package):
            return next_package

        chosen_version = decide.choose_version(self, next_package)
        had_pending = decide.absorb_pending_clauses(self)

        # Provider-driven force back-track. When the provider returns
        # a tentative candidate and queues blockers, jump to the
        # blockers before the candidate is decided.
        force_targets = list(self.provider.consume_force_backtrack_targets())
        if force_targets:
            triggering = conflict.force_targeted_backtrack(self, force_targets)
            if triggering is not None:
                return triggering

        if chosen_version is None:
            decide.record_no_versions(self, next_package, had_pending=had_pending)
            return next_package

        self.solution.decide(next_package, chosen_version)
        self.stats.decisions += 1
        self.observer.on_decision(
            next_package, chosen_version, self.solution.decision_level
        )

        dependencies = self.provider.get_dependencies(next_package, chosen_version)
        for dependency_package, dependency_range in dependencies.items():
            incompat_index.add_incompatibility(
                self,
                Incompatibility(
                    [
                        Term(
                            next_package,
                            self.range_type.singleton(chosen_version),
                            positive=True,
                        ),
                        Term(dependency_package, dependency_range, positive=False),
                    ],
                    cause=IncompatibilityCause.DEPENDENCY,
                ),
            )
        return next_package

    def _build_result(self) -> dict[PackageType, VersionType]:
        """Build the final result, including only reachable packages.

        Per the PubGrub spec, the solution must not contain extra packages:
        "all selected packages are transitively reachable from the root."
        """
        return build_reachable_decisions(
            self.solution.decisions(),
            self.incompatibilities,
            self.provider.get_dependencies,
            root_sentinel=ROOT,
        )

    def _reset(
        self,
        constraints: Mapping[PackageType, RangeProtocol[VersionType]] | None,
    ) -> None:
        """Reset solver state for a new resolution."""
        self.incompatibilities.clear()
        self.package_to_incompatibilities.clear()
        self.dependency_index.clear()
        self.solution = PartialSolution(range_type=self.range_type)
        self.stats = ResolverStats()

        self.constraints = constraints or {}
        self.injected_constraints.clear()
        self.root_package_order.clear()
        self.pending_targeted_backtrack.clear()
        self.tiebreak_cache.clear()

    def _add_root_requirements(
        self, requirements: Mapping[PackageType, RangeProtocol[VersionType]]
    ) -> None:
        """Create root incompatibilities and decide the root package."""
        for idx, (package, required_range) in enumerate(requirements.items()):
            root_term: Term[Any, Any] = Term(
                ROOT, self.range_type.singleton(self.root_version), positive=True
            )
            incompat_index.add_incompatibility(
                self,
                Incompatibility(
                    [root_term, Term(package, required_range, positive=False)],
                    cause=IncompatibilityCause.ROOT,
                ),
            )
            self.root_package_order[package] = (0, idx, "")
        self.solution.decide(ROOT, self.root_version)
        self.stats.decisions += 1
