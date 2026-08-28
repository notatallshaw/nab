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
them directly; the supported public API is ``__init__``, ``resolve``,
``solve``, and ``stats``.

Specification: https://github.com/dart-lang/pub/blob/master/doc/solver.md
Original blog post: https://nex3.medium.com/pubgrub-2fb6470504f
Rust implementation: https://github.com/pubgrub-rs/pubgrub
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Generic, Protocol

from . import conflict, decide, incompat_index, propagate
from ._compat import override
from .decision_queue import DecisionQueue
from .errors import ResolutionError
from .partial_solution import PartialSolution
from .ranges import Range
from .result import build_solution_data
from .root import ROOT
from .types import (
    Incompatibility,
    IncompatibilityCause,
    IncompatibilityState,
    PackageType,
    RangeProtocol,
    RootRequirement,
    SetRelation,
    Term,
    VersionType,
)

if TYPE_CHECKING:
    from typing_extensions import TypeIs

__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "BaseProvider",
    "Incompatibility",
    "IncompatibilityCause",
    "IncompatibilityState",
    "ResolutionError",
    "Resolver",
    "ResolverObserver",
    "ResolverProvider",
    "ResolverStats",
    "RootRequirement",
    "SetRelation",
    "Solution",
    "Term",
]

DEFAULT_MAX_ITERATIONS = 200_000


class Solution(Generic[PackageType, VersionType]):
    """Pins and dependency relationships from a finished resolution.

    ``pins`` maps every transitively reachable package to its decided
    version.  ``edges`` are distinct ``(parent, child)`` pairs in
    breadth-first order from ``roots``.  Both endpoints of each edge are
    keys of ``pins``.  ``roots`` are the packages the caller required
    directly, in requirement order.

    Immutable.  No ``__slots__``: pickle and ``copy`` restore a slotted
    instance by assignment, which :meth:`__setattr__` refuses.
    """

    __match_args__ = ("pins", "edges", "roots")

    pins: dict[PackageType, VersionType]
    edges: tuple[tuple[PackageType, PackageType], ...]
    roots: tuple[PackageType, ...]

    def __init__(
        self,
        pins: dict[PackageType, VersionType],
        edges: tuple[tuple[PackageType, PackageType], ...],
        roots: tuple[PackageType, ...],
    ) -> None:
        """Record the pins, edges and roots of a finished resolution."""
        object.__setattr__(self, "pins", pins)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "roots", roots)

    @override
    def __setattr__(self, name: str, value: object) -> None:
        """Refuse the write."""
        message = f"cannot assign to field {name!r}"
        raise AttributeError(message)

    @override
    def __delattr__(self, name: str) -> None:
        """Refuse the deletion."""
        message = f"cannot delete field {name!r}"
        raise AttributeError(message)

    @override
    def __eq__(self, other: object) -> bool:
        """Compare pins, edges and roots."""
        if not isinstance(other, Solution):
            return NotImplemented
        return (self.pins, self.edges, self.roots) == (
            other.pins,
            other.edges,
            other.roots,
        )

    @override
    def __hash__(self) -> int:
        """Hash pins, edges and roots together.

        Immutable and comparable, so it declares a hash; the ``pins`` dict
        makes the call itself raise :exc:`TypeError`.
        """
        return hash((self.pins, self.edges, self.roots))

    @override
    def __repr__(self) -> str:
        """Return a debug representation."""
        return (
            f"{type(self).__qualname__}(pins={self.pins!r}, "
            f"edges={self.edges!r}, roots={self.roots!r})"
        )


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

    def has_satisfying_version(
        self, package: PackageType, version_range: RangeProtocol[VersionType]
    ) -> bool:
        """Return whether ``choose_version`` would pick a version in ``version_range``.

        A diagnostic query with no lasting side effect: it is used to attribute a
        ``NO_VERSIONS`` failure to a user constraint only when the un-narrowed
        range still offers a version the constraint clipped away.  Providers that
        queue clauses or record state during ``choose_version`` must not let any
        of that escape here.
        """
        ...

    def get_dependencies(
        self, package: PackageType, version: VersionType
    ) -> Mapping[PackageType, RangeProtocol[VersionType]]:
        """Return ``{dependency_package: required_range}`` for this version.

        Asked only about a version ``choose_version`` returned, immediately
        after that decision is recorded.  The virtual root sentinel is never
        passed.

        The same pair is asked more than once: a backjump that re-decides a
        version asks again, and building the final :class:`Solution` asks once
        per pin.  Cache by package and version.
        """
        ...

    def begin_decision_scan(self) -> Callable[[PackageType], bool] | None:
        """Announce the start of one decision scan, and offer an arrival probe.

        The scan reads sort keys from ``prioritize`` and ``is_ready``, so both
        must answer from state that does not move until the next call.
        Providers whose answers depend on another thread freeze that state
        here; for providers with no such state this is a no-op.

        Keys are cached across scans.  A package's key is read again when its
        allowed range moves, when the counts passed to ``prioritize`` move, and
        on every scan while ``is_ready`` returns False.  A priority that moves
        for a reason only the provider can see fits none of those and may go
        unread, so a provider whose priority is still settling returns False
        from ``is_ready`` until it has.

        A returned probe replaces that every-scan re-read, and is a promise:
        while it answers False for a package, and that package's range and the
        counts you were last passed are unchanged, ``prioritize`` and
        ``is_ready`` must answer what they last answered.  Neither is called
        for the package meanwhile, over any number of consecutive scans, so a
        provider that starts its fetch inside ``prioritize`` would wait on a
        fetch it never begins.  Answering True is always safe.  Return ``None``
        to decline the probe and keep the every-scan re-read; a provider
        wrapping another returns the inner probe.
        """
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

        Returning False also holds the package on the scan's re-read list, so a
        provider whose ``prioritize`` key is still moving keeps returning False
        until it settles.  A provider that returns a probe from
        ``begin_decision_scan`` gives that re-read up: while the probe answers
        False the key is not read at all.
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

    def widen_decision(
        self, package: PackageType, version: VersionType
    ) -> RangeProtocol[VersionType] | None:
        """Return a widened stand-in for ``version`` in dependency clauses, or None.

        Called at most once per decision, after ``get_dependencies``
        returned a non-empty mapping for ``version``, so a provider may
        answer from what that call cached.

        Soundness contract: the returned range must contain ``version``, and
        every version inside it that could ever be chosen for ``package`` in
        this resolution must have exactly the dependencies being recorded
        for ``version``; versions inside it that can never be selected are
        harmless.  ``None`` keeps the exact singleton.  Widening merges
        dependency clauses for adjacent rejected versions into contiguous
        ranges instead of one hole per version, and lets a single clause
        reject a whole run of same-dependency versions.
        """
        ...

    def narrow_for_display(
        self, package: PackageType, constraint: RangeProtocol[VersionType]
    ) -> RangeProtocol[VersionType]:
        """Map a possibly-widened ``constraint`` back onto known versions.

        Applied at error-render time only; the derivation state is never
        mutated.  The renderer narrows every originally-positive term, so
        ``package`` may be the virtual root sentinel or another package
        outside the provider's namespace; return such constraints
        unchanged, as providers that do not widen do for all input.

        Soundness contract: the result must hold the same known versions as
        ``constraint``.  The renderer reports what a narrowing drops as a range
        holding no version, so dropping one that exists states a falsehood.
        """
        ...


class BaseProvider(Generic[PackageType, VersionType]):
    """Defaults for the six provider methods a synchronous provider does not need.

    Supplies ``begin_decision_scan``, ``is_ready``,
    ``receive_partial_solution_hint``, ``consume_pending_clauses``,
    ``consume_force_backtrack_targets`` and ``narrow_for_display``, the six
    :class:`ResolverProvider` methods with nothing to do when there is no async
    layer, no queued clauses and no widening.  A subclass still owes
    ``choose_version``, ``has_satisfying_version``, ``get_dependencies``,
    ``prioritize`` and ``widen_decision``.

    Subclassing is optional; the resolver accepts anything that satisfies the
    protocol.  Nothing re-exports this, so import it as
    ``from nab_resolver.resolver import BaseProvider``.
    """

    def begin_decision_scan(self) -> Callable[[PackageType], bool] | None:
        """Freeze nothing and offer no probe: no state moves between scans."""
        return None

    def is_ready(self, package: PackageType) -> bool:
        """Report every package ready, since answers do not wait on a fetch."""
        del package
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[PackageType, RangeProtocol[VersionType]],
        decisions: Mapping[PackageType, VersionType],
    ) -> None:
        """Drop the snapshot: nothing here forward-checks against it.

        A provider that inherits this is not called, and the resolver does
        not build the two snapshots it would be handed.  Writing an
        equivalent no-op of your own pays for both.
        """
        del positive_ranges, decisions

    def consume_pending_clauses(
        self,
    ) -> list[Incompatibility[PackageType, VersionType]]:
        """Return no clauses: ``choose_version`` queues none."""
        return []

    def consume_force_backtrack_targets(self) -> list[PackageType]:
        """Return no targets: there is no force-backtrack signal to give."""
        return []

    def narrow_for_display(
        self, package: PackageType, constraint: RangeProtocol[VersionType]
    ) -> RangeProtocol[VersionType]:
        """Return the constraint unchanged, as a provider that never widens does.

        A subclass whose ``widen_decision`` widens overrides this as well, or
        its error text carries widened ranges instead of known versions.
        """
        del package
        return constraint


_BASE_PARTIAL_SOLUTION_HINT = BaseProvider.receive_partial_solution_hint


def _provider_with_inherited_hint(
    provider: ResolverProvider[PackageType, VersionType],
) -> ResolverProvider[PackageType, VersionType] | None:
    """Return ``provider`` when its hint is :class:`BaseProvider`'s no-op.

    Any other shape gives None, so anything that might read the hint keeps
    being called: an override on the class, an override on the instance, and
    a structural implementer.  A provider with no such method gives None as
    well, and so fails where the resolver calls it rather than here.
    """
    hint = getattr(provider, "receive_partial_solution_hint", None)
    if getattr(hint, "__func__", None) is _BASE_PARTIAL_SOLUTION_HINT:
        return provider
    return None


# Every counter in declaration order.  ``__slots__`` holds the same names
# sorted, so equality, the repr and ``__match_args__`` read this one instead.
_STAT_FIELDS: Final = (
    "rounds",
    "decisions",
    "conflicts",
    "derivations",
    "backjumps",
    "restarts",
    "targeted_backtracks",
    "incompatibilities_learned",
    "package_conflict_counts",
    "package_culprit_counts",
)


class ResolverStats(Generic[PackageType]):
    """Running statistics for resolution observability.

    Inspired by SAT solver statistics (MiniSat, CaDiCaL) which track
    decisions, conflicts, propagations, and restarts as standard metrics.
    See: https://minisat.se/MiniSat.html
    """

    __slots__ = (
        "backjumps",
        "conflicts",
        "decisions",
        "derivations",
        "incompatibilities_learned",
        "package_conflict_counts",
        "package_culprit_counts",
        "restarts",
        "rounds",
        "targeted_backtracks",
    )

    __match_args__ = _STAT_FIELDS

    rounds: int
    decisions: int
    conflicts: int
    derivations: int
    backjumps: int
    restarts: int
    targeted_backtracks: int
    incompatibilities_learned: int
    package_conflict_counts: defaultdict[PackageType, int]
    package_culprit_counts: defaultdict[PackageType, int]

    def __init__(  # noqa: PLR0913, PLR0917 - one parameter per counter
        self,
        rounds: int = 0,
        decisions: int = 0,
        conflicts: int = 0,
        derivations: int = 0,
        backjumps: int = 0,
        restarts: int = 0,
        targeted_backtracks: int = 0,
        incompatibilities_learned: int = 0,
        package_conflict_counts: defaultdict[PackageType, int] | None = None,
        package_culprit_counts: defaultdict[PackageType, int] | None = None,
    ) -> None:
        """Start every counter at the value given, zero and empty by default."""
        self.rounds = rounds
        self.decisions = decisions
        self.conflicts = conflicts
        self.derivations = derivations
        self.backjumps = backjumps
        self.restarts = restarts
        self.targeted_backtracks = targeted_backtracks
        self.incompatibilities_learned = incompatibilities_learned

        if package_conflict_counts is None:
            package_conflict_counts = defaultdict(int)
        if package_culprit_counts is None:
            package_culprit_counts = defaultdict(int)
        self.package_conflict_counts = package_conflict_counts
        self.package_culprit_counts = package_culprit_counts

    @override
    def __eq__(self, other: object) -> bool:
        """Compare every counter."""
        if not isinstance(other, ResolverStats):
            return NotImplemented
        return tuple(getattr(self, name) for name in _STAT_FIELDS) == tuple(
            getattr(other, name) for name in _STAT_FIELDS
        )

    __hash__ = None  # type: ignore[assignment]

    @override
    def __repr__(self) -> str:
        """Return a debug representation."""
        counters = ", ".join(f"{name}={getattr(self, name)!r}" for name in _STAT_FIELDS)
        return f"{type(self).__qualname__}({counters})"


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


def _is_root_sequence(
    requirements: Mapping[PackageType, RangeProtocol[VersionType]]
    | Sequence[RootRequirement[PackageType, VersionType]],
) -> TypeIs[Sequence[RootRequirement[PackageType, VersionType]]]:
    """Return whether the caller passed the one-clause-per-requirement form.

    A ``TypeIs`` rather than a bare ``isinstance``: one type can satisfy both
    members of the union, so plain narrowing can leave an intersection that
    has lost the mapping's value type.  ty needs the sequence rather than the
    mapping as the narrowed side to keep those parameters, while the test
    itself stays on ``Mapping`` so every non-mapping iterable is still taken
    as the sequence form.
    """
    return not isinstance(requirements, Mapping)


def _as_root_requirements(
    requirements: Mapping[PackageType, RangeProtocol[VersionType]]
    | Sequence[RootRequirement[PackageType, VersionType]],
) -> Sequence[RootRequirement[PackageType, VersionType]]:
    """Accept either shape ``Resolver.resolve`` takes and return the sequence."""
    if _is_root_sequence(requirements):
        return requirements
    # The parameters are spelled out because ``constraint`` is a contravariant
    # protocol, which gives the version parameter no inference site.  The
    # suppression is ty's: it reads the sequence member of the union as still
    # live in the negative branch of a generic ``TypeIs``.
    return [
        RootRequirement[PackageType, VersionType](package, required_range)
        for package, required_range in requirements.items()  # ty: ignore[unresolved-attribute]
    ]


class Resolver(Generic[PackageType, VersionType]):
    """PubGrub dependency resolver.

    The main loop follows the PubGrub specification:
    1. Unit propagation: derive constraints from incompatibilities
    2. Conflict resolution: learn new incompatibilities and backjump
    3. Decision making: pick the next package and version to try

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#the-algorithm
    """

    # Compared against one package's conflict count, not the total.
    _RESTART_THRESHOLD = 8
    _MAX_RESTARTS = 3

    # A package re-queues every multiple of CULPRIT_THRESHOLD so persistent
    # lock-step clusters keep getting pruned.  TARGETED_BT_MIN_CONFLICTS keeps
    # short scenarios from paying the backtrack tax.
    CULPRIT_THRESHOLD = 5
    TARGETED_BT_MIN_CONFLICTS = 30
    MAX_TARGETED_BACKTRACKS = 64

    def __init__(
        self,
        provider: ResolverProvider[PackageType, VersionType],
        observer: ResolverObserver[PackageType, VersionType] | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        range_type: type[RangeProtocol[Any]] = Range,
        root_version: Any = 1,
        format_range: Callable[[Any], str] = str,
    ) -> None:
        """Create a resolver with the given provider and optional observer.

        The ``root_version`` is a sentinel passed to ``range_type.singleton()``
        to build the virtual root package's range.  The default ``1``
        works for :class:`~nab_resolver.ranges.Range` (which accepts any
        comparable type) but a PEP 440 range type such as
        :class:`packaging.ranges.VersionRange` requires a parseable
        version string or :class:`~packaging.version.Version` here.

        ``format_range`` renders a constraint in a failure report.  It travels
        with ``range_type``: the default ``str`` reads well for
        :class:`~nab_resolver.ranges.Range`, while a range type whose ``str``
        is a debug representation needs its own.
        """
        self.provider = provider

        # Recording the provider rather than a flag keeps the answer tied to
        # the object it was asked about, so a provider swapped into
        # ``self.provider`` mid-resolve is not this one and is sent the hint.
        self._hint_ignoring_provider = _provider_with_inherited_hint(provider)

        self.observer: ResolverObserver[PackageType, VersionType] = (
            observer or ResolverObserver()
        )
        self.max_iterations = max_iterations
        self.range_type = range_type
        self.root_version = root_version
        self.format_range = format_range

        # The widest value the type expresses is the identity a caller folds
        # with, but a term needs a top its own difference agrees with, since
        # conflict resolution relies on ``x.is_subset((x - y) | y)``.
        self.fold_identity: RangeProtocol[Any] = range_type.full()
        self.term_top: RangeProtocol[Any] = ~range_type.empty()

        # as_term_range's answer per supplied range object, keyed by id() with
        # term_range_keepalive holding the keys alive. Uncapped, unlike
        # propagate's token memo: the keys are clause and decision ranges rather
        # than every propagation probe.
        self.term_range_by_id: dict[int, RangeProtocol[Any]] = {}
        self.term_range_keepalive: list[RangeProtocol[Any]] = []

        self.incompatibilities: list[Incompatibility[Any, Any]] = []
        self.package_to_incompatibilities: defaultdict[Any, list[int]] = defaultdict(
            list
        )

        # Per clause, the contradiction epoch in which one of its terms was
        # last seen contradicted; unit propagation skips a clause whose stamp
        # is still current.
        self.clause_contradicted_at: list[int] = []

        # Keyed by (package, dep_package, dep_constraint, dep_positive); used
        # to merge mergeable dependency clauses (pubgrub-rs's merge_dependents).
        self.dependency_index: dict[Any, int] = {}

        self.solution: PartialSolution[Any, Any] = PartialSolution(
            range_type=range_type
        )
        self.stats: ResolverStats[PackageType] = ResolverStats()

        self.constraints: Mapping[PackageType, RangeProtocol[VersionType]] = {}
        self.root_package_order: dict[PackageType, tuple[int, int, str]] = {}
        self.pending_targeted_backtrack: list[PackageType] = []

        # Memoises the tiebreak tuple in choose_package_to_decide.
        self.tiebreak_cache: dict[PackageType, tuple[int, int, str]] = {}

        # The undecided packages ordered by sort key. priority_epoch advances
        # when a count a key reads moves: the conflict and culprit counts, and
        # the provider's force-backtrack count. It invalidates every key.
        self.decision_queue: DecisionQueue[PackageType] = DecisionQueue()
        self.priority_epoch = 0

        # Memoises term_relation's pre-adjustment SetRelation, keyed by
        # (positive, assignment token, constraint token). Cleared on overflow.
        self.relation_cache: dict[tuple[bool, int, int], SetRelation] = {}

        # relation_cache_on goes off while the memo's hit rate does not pay for
        # the key it builds. A window of probes decides that: relation_gate_hits
        # counts its hits, relation_gate_probes_left is the window less its
        # misses, and a miss judges the window once the hits cover what is left.
        self.relation_cache_on = True
        self.relation_gate_hits = 0
        self.relation_gate_probes_left = propagate.RELATION_GATE_WINDOW

        # One token per distinct range, so a relation-cache probe compares ints
        # rather than bound structures. The counter never rewinds, so clearing
        # this table alone only strands relation_cache entries; it can never
        # point a live one at a different range.
        self.range_tokens: dict[RangeProtocol[Any], int] = {}
        self.next_range_token = 0

        # range_token_by_id is keyed by id(), and interned_ranges keeps those
        # objects alive so an address is never reused under a live entry, which
        # is why the two are wiped together.
        self.range_token_by_id: dict[int, int] = {}
        self.interned_ranges: list[RangeProtocol[Any]] = []

    def as_term_range(self, range_: RangeProtocol[Any]) -> RangeProtocol[VersionType]:
        """Return the term constraint to record for a supplied range.

        Only a range equal to ``full()`` is substituted.  A range that breaks
        the identity :class:`~nab_resolver.types.RangeProtocol` documents
        without equalling ``full()``, such as ``full()`` minus an ``===``
        literal, passes through and reaches conflict resolution's step budget.

        Memoised per supplied range object.
        """
        key = id(range_)
        result = self.term_range_by_id.get(key)
        if result is None:
            result = self.term_top if range_ == self.fold_identity else range_
            self.term_range_by_id[key] = result
            self.term_range_keepalive.append(range_)
        return result

    def resolve(
        self,
        requirements: Mapping[PackageType, RangeProtocol[VersionType]]
        | Sequence[RootRequirement[PackageType, VersionType]],
        constraints: Mapping[PackageType, RangeProtocol[VersionType]] | None = None,
    ) -> dict[PackageType, VersionType]:
        """Resolve requirements and return ``{package: version}``.

        The pins of :meth:`solve`, for a caller that has no use for the
        dependency graph.
        """
        return self.solve(requirements, constraints).pins

    def solve(
        self,
        requirements: Mapping[PackageType, RangeProtocol[VersionType]]
        | Sequence[RootRequirement[PackageType, VersionType]],
        constraints: Mapping[PackageType, RangeProtocol[VersionType]] | None = None,
    ) -> Solution[PackageType, VersionType]:
        """Resolve requirements and return the pins, roots, and edges.

        ``requirements`` is either one range per package, or a sequence of
        :class:`~nab_resolver.types.RootRequirement` when the caller has more
        than one requirement on a package and wants each named as written in
        the failure report.

        Constraints restrict a package's version range but do not cause
        it to be installed.  They are injected lazily: only when the
        resolver is about to decide a constrained package (meaning
        something already depends on it).

        A supplied range equal to ``range_type.full()`` is recorded as
        ``~range_type.empty()``, which may be strictly narrower; see
        :class:`~nab_resolver.types.RangeProtocol`.

        Raises ``ResolutionError`` if no solution exists.
        """
        self._reset(constraints)
        self._add_root_requirements(_as_root_requirements(requirements))

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
        chosen_version = decide.choose_version(self, next_package)
        had_pending = decide.absorb_pending_clauses(self)

        # Provider-driven force back-track. When the provider returns
        # a tentative candidate and queues blockers, jump to the
        # blockers before the candidate is decided.
        force_targets = list(self.provider.consume_force_backtrack_targets())
        if force_targets:
            self.priority_epoch += 1
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
        if not dependencies:
            return next_package
        exact_range = self.range_type.singleton(chosen_version)
        widened = self.provider.widen_decision(next_package, chosen_version)
        parent_range = exact_range if widened is None else self.as_term_range(widened)
        for dependency_package, supplied_range in dependencies.items():
            dependency_range = self.as_term_range(supplied_range)
            cross_package = dependency_package != next_package
            if not cross_package:
                # An incompatibility holds at most one term per package,
                # so self-dependency terms merge to {v} & ~range: empty
                # (a vacuous clause) when the range contains the chosen
                # version, else exactly {v}.  The exact singleton is kept:
                # widening a single-term clause only degrades error text.
                if chosen_version in dependency_range:
                    continue
                terms = [Term(next_package, exact_range, positive=True)]
            else:
                terms = [
                    Term(next_package, parent_range, positive=True),
                    Term(dependency_package, dependency_range, positive=False),
                ]

            # The merged term drops the required range, so the clause carries
            # it for the report.
            incompatibility = Incompatibility(
                terms,
                cause=IncompatibilityCause.DEPENDENCY,
                dependency_range=None if cross_package else dependency_range,
            )
            incompat_index.add_incompatibility(self, incompatibility)

            if cross_package:
                decide.absorb_redundant_requirement(
                    self, dependency_package, dependency_range, incompatibility
                )
        return next_package

    def _build_result(self) -> Solution[PackageType, VersionType]:
        """Build the final result, including only reachable packages.

        Per the PubGrub spec, the solution must not contain extra packages:
        "all selected packages are transitively reachable from the root."
        """
        pins, edges, roots = build_solution_data(
            self.solution.decisions(),
            self.incompatibilities,
            self.provider.get_dependencies,
            root_sentinel=ROOT,
        )
        return Solution(pins=pins, edges=edges, roots=roots)

    def _reset(
        self,
        constraints: Mapping[PackageType, RangeProtocol[VersionType]] | None,
    ) -> None:
        """Reset solver state for a new resolution."""
        self.incompatibilities.clear()
        self.package_to_incompatibilities.clear()
        self.clause_contradicted_at.clear()
        self.dependency_index.clear()
        self.solution = PartialSolution(range_type=self.range_type)
        self.stats = ResolverStats()

        self.constraints = constraints or {}
        self.root_package_order.clear()
        self.pending_targeted_backtrack.clear()
        self.tiebreak_cache.clear()
        self.decision_queue.clear()
        self.priority_epoch = 0
        self.relation_cache.clear()
        self.relation_cache_on = True
        self.relation_gate_hits = 0
        self.relation_gate_probes_left = propagate.RELATION_GATE_WINDOW
        self.range_tokens.clear()
        self.range_token_by_id.clear()
        self.interned_ranges.clear()
        self.term_range_by_id.clear()
        self.term_range_keepalive.clear()

        # Re-asked here, so a hook installed since the last resolve is honoured.
        self._hint_ignoring_provider = _provider_with_inherited_hint(self.provider)

    def _add_root_requirements(
        self, requirements: Sequence[RootRequirement[PackageType, VersionType]]
    ) -> None:
        """Create one root incompatibility per requirement, and decide root."""
        for idx, root in enumerate(requirements):
            term_range = self.as_term_range(root.constraint)
            root_term: Term[Any, Any] = Term(
                ROOT, self.range_type.singleton(self.root_version), positive=True
            )
            incompat_index.add_incompatibility(
                self,
                Incompatibility(
                    [root_term, Term(root.package, term_range, positive=False)],
                    cause=IncompatibilityCause.ROOT,
                    origin=root.origin,
                ),
            )
            # First mention fixes the tiebreak, so naming a package twice
            # does not push its decision later.
            self.root_package_order.setdefault(root.package, (0, idx, ""))
        self.solution.decide(ROOT, self.root_version)
        self.stats.decisions += 1
