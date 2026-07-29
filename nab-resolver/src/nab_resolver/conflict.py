"""Conflict resolution and backtracking for the PubGrub resolver.

Owns the conflict-resolution loop, the most-recent-satisfier
search, the always-learn force-resolution gate, the targeted
backtrack queue, and the catastrophic restart handler.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#conflict-resolution
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import ResolutionError
from .incompat_index import add_incompatibility
from .partial_solution import PartialSolution
from .report import format_error, prior_cause
from .root import ROOT
from .types import Incompatibility, IncompatibilityCause

if TYPE_CHECKING:
    from .partial_solution import Assignment
    from .resolver import Resolver
    from .types import Term


__all__ = [
    "apply_targeted_backtrack",
    "conflict_credit_target",
    "conflict_resolution",
    "find_most_recent_satisfier",
    "force_targeted_backtrack",
    "is_terminal_incompatibility",
    "iterate_force_resolution",
    "maybe_restart",
    "maybe_targeted_backtrack",
    "recompute_previous_level",
    "try_force_resolution_step",
    "update_culprit_counts",
]


# Soundness gate for try_force_resolution_step: a single-term
# incompatibility must resolve to >= 2 terms to keep the eliminated
# package's conditioning.
_SINGLE_TERM = 1
_MIN_RESOLVED_TERMS = 2

# Lowest decision level a targeted-backtrack can land above without
# removing ROOT (decided at level 1 in _add_root_requirements).
_TARGETED_BT_MIN_LEVEL = 2

# Each resolution step replaces the satisfier's term with terms satisfied
# strictly earlier, so the most recent satisfier moves down the trail every
# step and one step per trail entry bounds a coherent loop.  The multiplier is
# headroom over that bound and the floor covers short trails, so only a state
# that has stopped making progress reaches the budget.
_STEPS_PER_TRAIL_ENTRY = 4
_MIN_CONFLICT_STEPS = 32


def conflict_resolution(
    resolver: Resolver[Any, Any],
    conflicting_incompatibility: Incompatibility[Any, Any],
) -> Incompatibility[Any, Any]:
    """Learn a new incompatibility and backjump to the appropriate level.

    Implements PubGrub's conflict resolution algorithm: resolve
    backwards through the assignment trail combining incompatibilities
    until the learned clause has at most one term at the current
    decision level.

    Raises ``ResolutionError`` when the conflict proves the requirements
    unsatisfiable, and also when the loop exceeds its step budget, which
    signals a resolver bug rather than an unsatisfiable input.

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#conflict-resolution
    """
    current_incompatibility = conflicting_incompatibility
    is_derived = False
    step_budget = max(
        _MIN_CONFLICT_STEPS,
        resolver.solution.trail_length * _STEPS_PER_TRAIL_ENTRY,
    )

    for _ in range(step_budget):
        if is_terminal_incompatibility(current_incompatibility):
            raise ResolutionError(
                format_error(
                    current_incompatibility,
                    narrow=resolver.provider.narrow_for_display,
                ),
                incompatibility=current_incompatibility,
            )

        satisfier_result = find_most_recent_satisfier(resolver, current_incompatibility)
        most_recent_satisfier = satisfier_result[0]
        most_recent_satisfier_term = satisfier_result[1]
        previous_satisfier_level = satisfier_result[2]

        can_backjump = (
            most_recent_satisfier.is_decision
            or previous_satisfier_level != most_recent_satisfier.decision_level
        )

        if can_backjump:
            (
                current_incompatibility,
                most_recent_satisfier,
                most_recent_satisfier_term,
                previous_satisfier_level,
                forced_any,
            ) = iterate_force_resolution(
                resolver,
                current_incompatibility,
                most_recent_satisfier,
                most_recent_satisfier_term,
                previous_satisfier_level,
            )
            if forced_any:
                is_derived = True

        resolver.observer.on_conflict_step(
            current_incompatibility,
            satisfier_package=most_recent_satisfier.package,
            satisfier_is_decision=most_recent_satisfier.is_decision,
            satisfier_level=most_recent_satisfier.decision_level,
            previous_level=previous_satisfier_level,
            can_backjump=can_backjump,
        )

        if can_backjump:
            if is_derived:
                add_incompatibility(resolver, current_incompatibility)
                resolver.stats.incompatibilities_learned += 1
                resolver.observer.on_learned(current_incompatibility)

            backjump_target = previous_satisfier_level
            if (
                most_recent_satisfier.is_decision
                and backjump_target >= most_recent_satisfier.decision_level
            ):
                backjump_target = most_recent_satisfier.decision_level - 1

            backjump_target = min(backjump_target, resolver.solution.decision_level)
            backjump_target = max(backjump_target, 0)

            if backjump_target == 0:
                raise ResolutionError(
                    format_error(
                        current_incompatibility,
                        narrow=resolver.provider.narrow_for_display,
                    ),
                    incompatibility=current_incompatibility,
                )

            from_level = resolver.solution.decision_level
            resolver.solution.backtrack(backjump_target)
            resolver.stats.backjumps += 1
            resolver.observer.on_backjump(from_level, backjump_target)

            # Count only one package per conflict so it gets decided first
            # after restart and its dependencies constrain the culprit.
            affected_package = most_recent_satisfier.package
            resolver.stats.package_conflict_counts[
                conflict_credit_target(most_recent_satisfier)
            ] += 1

            update_culprit_counts(
                resolver,
                current_incompatibility,
                affected_package,
                most_recent_satisfier,
            )

            return current_incompatibility

        # Can't backjump yet: resolve with the satisfier's cause.
        assert most_recent_satisfier.cause is not None
        assert most_recent_satisfier_term is not None
        is_derived = True

        resolved_terms = prior_cause(
            current_incompatibility,
            most_recent_satisfier.cause,
            most_recent_satisfier_term.package,
        )

        current_incompatibility = Incompatibility(
            resolved_terms,
            cause=IncompatibilityCause.DERIVED,
            cause_left=current_incompatibility,
            cause_right=most_recent_satisfier.cause,
        )

    stalled_message = (
        f"Conflict resolution made no progress in {step_budget} steps; this is "
        "a resolver bug rather than an unsatisfiable requirement. Stalled on "
        f"{current_incompatibility!r}"
    )
    raise ResolutionError(stalled_message, incompatibility=current_incompatibility)


def update_culprit_counts(
    resolver: Resolver[Any, Any],
    incompatibility: Incompatibility[Any, Any],
    affected_package: Any,
    satisfier: Assignment[Any, Any],
) -> None:
    """Credit non-affected packages in a learned clause as culprits.

    Modelled on uv's ConflictTracker (PR #9843).  Every non-affected,
    non-root package in the clause is a culprit; for single-term NO_VERSIONS
    clauses we walk the satisfier's cause chain instead.  When a culprit
    crosses ``CULPRIT_THRESHOLD`` it gets queued for targeted backtrack.
    """
    # Dict-as-ordered-set: hash-ordered iteration would make the
    # targeted-backtrack queue order vary across processes.
    culprit_packages: dict[Any, None] = {}
    for term in incompatibility.terms:
        package = term.package
        if package is ROOT or package == affected_package:
            continue
        culprit_packages[package] = None

    # Single-term NO_VERSIONS clauses carry the antecedent decisions only
    # via the satisfier's cause; without this, those decisions go uncredited.
    if len(incompatibility.terms) == 1 and satisfier.cause is not None:
        for term in satisfier.cause.terms:
            package = term.package
            if package is ROOT or package == affected_package:
                continue
            culprit_packages[package] = None

    threshold = resolver.CULPRIT_THRESHOLD
    for package in culprit_packages:
        resolver.stats.package_culprit_counts[package] += 1
        count = resolver.stats.package_culprit_counts[package]
        if (
            count >= threshold
            and count % threshold == 0
            and package not in resolver.pending_targeted_backtrack
        ):
            resolver.pending_targeted_backtrack.append(package)


def conflict_credit_target(satisfier: Assignment[Any, Any]) -> Any:
    """Return the package whose decision receives the conflict credit.

    A positive term over a decided package used to be satisfied only by the
    decision itself, so the credit landed on the package whose decision
    triggered the conflict.  A widened parent term is often already
    satisfied by the earlier derivation propagated from its dependency
    clause; crediting the derivation's own package then starves the
    promotion heuristics, while crediting both packages over-promotes the
    whole cluster past the backjump horizon.  The clause's depending parent
    receives the one credit instead.
    """
    if satisfier.is_decision or satisfier.cause is None:
        return satisfier.package
    if satisfier.cause.cause is not IncompatibilityCause.DEPENDENCY:
        return satisfier.package
    parent = satisfier.cause.terms[0].package
    if parent == satisfier.package:
        return satisfier.package
    return parent


def iterate_force_resolution(
    resolver: Resolver[Any, Any],
    incompatibility: Incompatibility[Any, Any],
    satisfier: Assignment[Any, Any],
    satisfier_term: Term[Any, Any],
    previous_satisfier_level: int,
) -> tuple[
    Incompatibility[Any, Any],
    Assignment[Any, Any],
    Term[Any, Any],
    int,
    bool,
]:
    """Iterate :func:`try_force_resolution_step` while it succeeds.

    Returns the (possibly-resolved) incompatibility, refreshed satisfier,
    satisfier term, previous-satisfier level, and a ``forced_any`` flag
    indicating whether at least one resolution step happened.  Stops when
    ``try_force_resolution_step`` declines (returns ``None``).
    """
    forced_any = False

    while True:
        forced = try_force_resolution_step(
            resolver, incompatibility, satisfier, satisfier_term
        )
        if forced is None:
            break
        forced_any = True
        incompatibility = forced
        (
            satisfier,
            satisfier_term,
            previous_satisfier_level,
        ) = find_most_recent_satisfier(resolver, forced)

    return (
        incompatibility,
        satisfier,
        satisfier_term,
        previous_satisfier_level,
        forced_any,
    )


def try_force_resolution_step(
    resolver: Resolver[Any, Any],
    incompatibility: Incompatibility[Any, Any],
    satisfier: Assignment[Any, Any],
    satisfier_term: Term[Any, Any],
) -> Incompatibility[Any, Any] | None:
    """Resolve a single-term NO_VERSIONS clause once with a soundness gate.

    Standard PubGrub backjumps to root for single-term clauses, losing the
    supporting decisions; resolving once with the satisfier's cause exposes
    them so later propagation can skip the bad decision.

    Returns ``None`` when the resolved clause collapses to <2 terms (the
    eliminated package's conditioning would be lost) or when it is not
    assert-eligible.
    """
    if (
        satisfier.is_decision
        or len(incompatibility.terms) != _SINGLE_TERM
        or satisfier.cause is None
    ):
        return None

    resolved_terms = prior_cause(
        incompatibility, satisfier.cause, satisfier_term.package
    )
    if len(resolved_terms) < _MIN_RESOLVED_TERMS:
        return None

    forced = Incompatibility(
        resolved_terms,
        cause=IncompatibilityCause.DERIVED,
        cause_left=incompatibility,
        cause_right=satisfier.cause,
    )

    forced_recent, _, forced_prev_level = find_most_recent_satisfier(resolver, forced)
    forced_can_backjump = (
        forced_recent.is_decision or forced_prev_level != forced_recent.decision_level
    )
    if not forced_can_backjump:
        return None

    return forced


def find_most_recent_satisfier(
    resolver: Resolver[Any, Any], incompatibility: Incompatibility[Any, Any]
) -> tuple[Assignment[Any, Any], Term[Any, Any], int]:
    """Find the most recently assigned satisfier across all terms.

    Returns ``(satisfier, term, previous_satisfier_level)``.  The previous
    level is the highest among the other terms' satisfiers and bounds how
    far back the resolver can jump.  Refined when the satisfier is partial
    (earlier assignments also contributed).
    """
    most_recent: Assignment[Any, Any] | None = None
    most_recent_term: Term[Any, Any] | None = None
    previous_level = 1  # root's level

    for term in incompatibility.terms:
        satisfier = resolver.solution.satisfier(term)
        if satisfier is None:  # pragma: no cover
            unreachable = (
                f"Bug: no satisfier for {term!r} in a satisfied incompatibility"
            )
            raise RuntimeError(unreachable)
        satisfier_index = satisfier.trail_index
        if most_recent is None or (satisfier_index > most_recent.trail_index):
            if most_recent is not None:
                previous_level = max(previous_level, most_recent.decision_level)
            most_recent = satisfier
            most_recent_term = term
        else:
            previous_level = max(previous_level, satisfier.decision_level)

    if most_recent is None:  # pragma: no cover
        unreachable = "Bug: no satisfiers in a satisfied incompatibility"
        raise RuntimeError(unreachable)
    assert most_recent_term is not None

    previous_level = recompute_previous_level(
        resolver, most_recent, most_recent_term, previous_level
    )

    return most_recent, most_recent_term, previous_level


def recompute_previous_level(
    resolver: Resolver[Any, Any],
    satisfier: Assignment[Any, Any],
    satisfier_term: Term[Any, Any],
    current_previous_level: int,
) -> int:
    """Refine previous_level when the satisfier is partial.

    The satisfier's own assertion (the negated cause term) may cover only
    part of the term; the trail must then also exclude the difference
    ``own & ~term``, so the level of the earliest assignment that does is
    folded in.
    """
    if satisfier.cause is None:
        return current_previous_level

    cause_term = None
    for term in satisfier.cause.terms:
        if term.package == satisfier_term.package:
            cause_term = term
            break
    if cause_term is None:
        return current_previous_level

    own_term = cause_term.negate()
    difference = own_term.intersect(satisfier_term.negate())
    assert difference is not None
    if difference.constraint.is_empty:
        return current_previous_level

    difference_satisfier = resolver.solution.satisfier(difference.negate())
    if difference_satisfier is not None:
        return max(current_previous_level, difference_satisfier.decision_level)
    return current_previous_level


def is_terminal_incompatibility(incompatibility: Incompatibility[Any, Any]) -> bool:
    """Check whether this incompatibility proves resolution is impossible."""
    if not incompatibility.terms:
        return True
    return all(term.package is ROOT for term in incompatibility.terms)


def maybe_targeted_backtrack(resolver: Resolver[Any, Any]) -> Any | None:
    """Run :func:`apply_targeted_backtrack` if the gate is open.

    Gate: pending non-empty AND total conflicts past
    ``TARGETED_BT_MIN_CONFLICTS``.  Pending culprits are kept across rounds.
    """
    if (
        resolver.pending_targeted_backtrack
        and resolver.stats.conflicts >= resolver.TARGETED_BT_MIN_CONFLICTS
    ):
        return apply_targeted_backtrack(resolver)
    return None


def maybe_restart(
    resolver: Resolver[Any, Any],
    restart_threshold: int,
    restarts_remaining: int,
) -> tuple[int, int, bool]:
    """Restart the solver if any package crossed ``restart_threshold``.

    Returns ``(new_threshold, new_remaining, restarted)``.  Preserves
    ``incompatibilities`` and ``package_conflict_counts`` across restart.
    """
    if restarts_remaining <= 0:
        return restart_threshold, restarts_remaining, False

    max_count = max(resolver.stats.package_conflict_counts.values(), default=0)
    if max_count < restart_threshold:
        return restart_threshold, restarts_remaining, False

    resolver.stats.restarts += 1
    resolver.solution = PartialSolution(range_type=resolver.range_type)
    resolver.solution.decide(ROOT, resolver.root_version)
    resolver.pending_targeted_backtrack.clear()
    resolver.stats.targeted_backtracks = 0

    return restart_threshold * 2, restarts_remaining - 1, True


def force_targeted_backtrack(
    resolver: Resolver[Any, Any], packages: list[Any]
) -> Any | None:
    """Apply a targeted back-track without waiting for the normal gate.

    Used when the provider supplies direct evidence that the named
    packages are culprits. Bumps each package's culprit count past the
    dominant-culprit threshold, queues it, and applies immediately.

    Returns the back-jumped package, or ``None`` if the back-track did
    not move the decision level.
    """
    if not packages:
        return None

    threshold = resolver.CULPRIT_THRESHOLD
    for package in packages:
        current = resolver.stats.package_culprit_counts[package]
        if current < threshold:
            resolver.stats.package_culprit_counts[package] = threshold
        if package not in resolver.pending_targeted_backtrack:
            resolver.pending_targeted_backtrack.append(package)

    return apply_targeted_backtrack(resolver)


def apply_targeted_backtrack(resolver: Resolver[Any, Any]) -> Any | None:
    """Backtrack to before the earliest pending-culprit assignment.

    Picks the smallest decision-level among queued culprits' first assignments
    (decision OR derivation; derivations count too so propagated culprits in
    cluster conflicts still trigger a backtrack) and jumps to one before it.
    Capped at ``MAX_TARGETED_BACKTRACKS`` per restart segment.

    Level-1 assignments are skipped to preserve ROOT.
    """
    if resolver.stats.targeted_backtracks >= resolver.MAX_TARGETED_BACKTRACKS:
        resolver.pending_targeted_backtrack.clear()
        return None

    target_level = resolver.solution.decision_level
    triggering_package: Any | None = None

    for package in resolver.pending_targeted_backtrack:
        package_entries = resolver.solution.assignments_for(package)
        # Use the package's earliest non-level-1 assignment: jumping
        # further than that would just undo unrelated work.
        for assignment in package_entries:
            if assignment.decision_level < _TARGETED_BT_MIN_LEVEL:
                continue
            candidate = assignment.decision_level - 1
            if candidate < target_level:
                target_level = candidate
                triggering_package = package
            break

    resolver.pending_targeted_backtrack.clear()
    if triggering_package is None or target_level >= resolver.solution.decision_level:
        return None

    resolver.solution.backtrack(target_level)
    resolver.stats.targeted_backtracks += 1
    return triggering_package
