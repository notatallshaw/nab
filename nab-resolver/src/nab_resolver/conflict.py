"""Conflict resolution and backtracking for the PubGrub resolver.

Owns the conflict-resolution loop, the most-recent-satisfier
search, the always-learn force-resolution check, the targeted
backtrack queue, and the catastrophic restart handler.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#conflict-resolution
"""

from __future__ import annotations

__lazy_modules__ = (
    "nab_resolver.errors",
    "nab_resolver.incompat_index",
    "nab_resolver.partial_solution",
    "nab_resolver.report",
    "nab_resolver.root",
    "nab_resolver.types",
)

from typing import TYPE_CHECKING, Any

from .errors import ResolutionError, ResolutionInvariantError, ResolutionStalledError
from .incompat_index import add_incompatibility
from .partial_solution import PartialSolution
from .report import format_error
from .root import ROOT
from .types import Incompatibility, IncompatibilityCause

if TYPE_CHECKING:
    from .partial_solution import Assignment
    from .resolver import Resolver
    from .types import PackageType, Term, VersionType


__all__ = [
    "apply_targeted_backtrack",
    "conflict_credit_target",
    "conflict_resolution",
    "find_most_recent_satisfier",
    "force_targeted_backtrack",
    "is_terminal_incompatibility",
    "maybe_restart",
    "maybe_targeted_backtrack",
    "recompute_previous_level",
    "try_force_resolution_step",
    "update_culprit_counts",
]


# Soundness check for try_force_resolution_step: a single-term
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
                    format_range=resolver.format_range,
                ),
                incompatibility=current_incompatibility,
            )

        satisfier_result = find_most_recent_satisfier(
            resolver.solution, current_incompatibility
        )
        most_recent_satisfier = satisfier_result[0]
        most_recent_satisfier_term = satisfier_result[1]
        previous_satisfier_level = satisfier_result[2]

        can_backjump = (
            most_recent_satisfier.is_decision
            or previous_satisfier_level != most_recent_satisfier.decision_level
        )

        if can_backjump:
            step = try_force_resolution_step(
                resolver,
                current_incompatibility,
                most_recent_satisfier,
                most_recent_satisfier_term,
            )
            if step is not None:
                (
                    current_incompatibility,
                    most_recent_satisfier,
                    most_recent_satisfier_term,
                    previous_satisfier_level,
                ) = step
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
                        format_range=resolver.format_range,
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
            resolver.priority_epoch += 1

            update_culprit_counts(
                resolver,
                current_incompatibility,
                affected_package,
                most_recent_satisfier,
            )

            return current_incompatibility

        # Can't backjump yet: resolve with the satisfier's cause.
        cause = most_recent_satisfier.cause
        if cause is None:
            msg = (
                f"missing derivation cause for {most_recent_satisfier.package!r} "
                f"at trail index {most_recent_satisfier.trail_index}"
            )
            raise ResolutionInvariantError(msg, incompatibility=current_incompatibility)
        is_derived = True

        resolved_terms = _prior_cause(
            current_incompatibility,
            cause,
            most_recent_satisfier_term.package,
        )

        current_incompatibility = Incompatibility(
            resolved_terms,
            cause=IncompatibilityCause.DERIVED,
            cause_left=current_incompatibility,
            cause_right=cause,
        )

    stalled_message = (
        f"Conflict resolution made no progress in {step_budget} steps; this is "
        "a resolver bug rather than an unsatisfiable requirement. Stalled on "
        f"{current_incompatibility!r}"
    )
    raise ResolutionStalledError(
        stalled_message, incompatibility=current_incompatibility
    )


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
        resolver.priority_epoch += 1
        count = resolver.stats.package_culprit_counts[package]
        if (
            count >= threshold
            and count % threshold == 0
            and package not in resolver.pending_targeted_backtrack
        ):
            resolver.pending_targeted_backtrack.append(package)


def conflict_credit_target(
    satisfier: Assignment[PackageType, VersionType],
) -> PackageType:
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


def try_force_resolution_step(
    resolver: Resolver[PackageType, VersionType],
    incompatibility: Incompatibility[PackageType, VersionType],
    satisfier: Assignment[PackageType, VersionType],
    satisfier_term: Term[PackageType, VersionType],
) -> (
    tuple[
        Incompatibility[PackageType, VersionType],
        Assignment[PackageType, VersionType],
        Term[PackageType, VersionType],
        int,
    ]
    | None
):
    """Resolve a single-term NO_VERSIONS clause after a soundness check.

    Standard PubGrub backjumps to root for single-term clauses, losing the
    supporting decisions; resolving once with the satisfier's cause exposes
    them so later propagation can skip the bad decision.

    Return the clause and its checked satisfier and backjump level.
    Return ``None`` when the resolved clause collapses to <2 terms (the
    eliminated package's conditioning would be lost) or when it is not
    assert-eligible.
    """
    if (
        satisfier.is_decision
        or len(incompatibility.terms) != _SINGLE_TERM
        or satisfier.cause is None
    ):
        return None

    resolved_terms = _prior_cause(
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

    forced_recent, forced_term, forced_prev_level = find_most_recent_satisfier(
        resolver.solution, forced
    )
    forced_can_backjump = (
        forced_recent.is_decision or forced_prev_level != forced_recent.decision_level
    )
    if not forced_can_backjump:
        return None

    return forced, forced_recent, forced_term, forced_prev_level


def _prior_cause(
    incompatibility: Incompatibility[PackageType, VersionType],
    satisfier_cause: Incompatibility[PackageType, VersionType],
    shared_package: PackageType,
) -> list[Term[PackageType, VersionType]]:
    """Compute the prior cause by resolving two incompatibilities.

    Follows pubgrub-rs's prior_cause: for the shared package, union
    the terms (and drop if the union is a tautology). For other shared
    packages, intersect the terms. For packages in only one side,
    keep as-is.

    Reference: https://github.com/pubgrub-rs/pubgrub
    """
    incompat_terms: dict[PackageType, Term[PackageType, VersionType]] = {
        term.package: term for term in incompatibility.terms
    }
    cause_terms: dict[PackageType, Term[PackageType, VersionType]] = {
        term.package: term for term in satisfier_cause.terms
    }

    result: list[Term[PackageType, VersionType]] = []

    # Resolving the shared package drops it when the union covers every version.
    incompat_shared = incompat_terms.pop(shared_package, None)
    cause_shared = cause_terms.pop(shared_package, None)
    if incompat_shared is not None and cause_shared is not None:
        unioned = incompat_shared.union(cause_shared)
        if unioned is not None:
            result.append(unioned)
    elif incompat_shared is not None:
        result.append(incompat_shared)
    elif cause_shared is not None:
        result.append(cause_shared)

    # Retain the left clause's order followed by terms unique to the cause.
    for package, term in incompat_terms.items():
        other = cause_terms.pop(package, None)
        result.append(term if other is None else term.intersect(other))

    result.extend(cause_terms.values())
    return result


def find_most_recent_satisfier(
    solution: PartialSolution[PackageType, VersionType],
    incompatibility: Incompatibility[PackageType, VersionType],
) -> tuple[Assignment[PackageType, VersionType], Term[PackageType, VersionType], int]:
    """Find the most recently assigned satisfier across all terms.

    Returns ``(satisfier, term, previous_satisfier_level)``.  The previous
    level is the highest among the other terms' satisfiers and bounds how
    far back the resolver can jump.  Refined when the satisfier is partial
    (earlier assignments also contributed).
    """
    latest: (
        tuple[Assignment[PackageType, VersionType], Term[PackageType, VersionType]]
        | None
    ) = None
    previous_level = 1  # root's level

    for term in incompatibility.terms:
        satisfier = solution.satisfier(term)
        if satisfier is None:
            msg = f"no trail assignment satisfies {term!r} in the reported conflict"
            raise ResolutionInvariantError(msg, incompatibility=incompatibility)

        if latest is None or satisfier.trail_index > latest[0].trail_index:
            if latest is not None:
                # The displaced satisfier now counts toward the backjump bound.
                previous_level = max(previous_level, latest[0].decision_level)
            latest = satisfier, term
        else:
            previous_level = max(previous_level, satisfier.decision_level)

    if latest is None:
        msg = "cannot find a trail assignment for an empty incompatibility"
        raise ResolutionInvariantError(msg, incompatibility=incompatibility)

    satisfier, term = latest
    previous_level = recompute_previous_level(solution, satisfier, term, previous_level)
    return satisfier, term, previous_level


def recompute_previous_level(
    solution: PartialSolution[PackageType, VersionType],
    satisfier: Assignment[PackageType, VersionType],
    satisfier_term: Term[PackageType, VersionType],
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
    if difference.constraint.is_empty:
        return current_previous_level

    difference_satisfier = solution.satisfier(difference.negate())
    if difference_satisfier is not None:
        return max(current_previous_level, difference_satisfier.decision_level)
    return current_previous_level


def is_terminal_incompatibility(
    incompatibility: Incompatibility[PackageType, VersionType],
) -> bool:
    """Check whether this incompatibility proves resolution is impossible."""
    if not incompatibility.terms:
        return True
    return all(term.package is ROOT for term in incompatibility.terms)


def maybe_targeted_backtrack(
    resolver: Resolver[PackageType, VersionType],
) -> PackageType | None:
    """Run :func:`apply_targeted_backtrack` when its conditions hold.

    Pending must be non-empty and total conflicts must reach
    ``TARGETED_BT_MIN_CONFLICTS``. Pending culprits persist.
    """
    if (
        resolver.pending_targeted_backtrack
        and resolver.stats.conflicts >= resolver.TARGETED_BT_MIN_CONFLICTS
    ):
        return apply_targeted_backtrack(resolver)
    return None


def maybe_restart(
    resolver: Resolver[PackageType, VersionType],
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
    resolver.solution = PartialSolution(
        range_type=resolver.range_type,
        contradiction_epoch=resolver.solution.contradiction_epoch + 1,
    )
    resolver.solution.decide(ROOT, resolver.root_version)
    resolver.decision_queue.clear()
    resolver.pending_targeted_backtrack.clear()
    resolver.stats.targeted_backtracks = 0

    return restart_threshold * 2, restarts_remaining - 1, True


def force_targeted_backtrack(
    resolver: Resolver[PackageType, VersionType], packages: list[PackageType]
) -> PackageType | None:
    """Apply a targeted back-track before the normal threshold.

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
            resolver.priority_epoch += 1
        if package not in resolver.pending_targeted_backtrack:
            resolver.pending_targeted_backtrack.append(package)

    return apply_targeted_backtrack(resolver)


def apply_targeted_backtrack(
    resolver: Resolver[PackageType, VersionType],
) -> PackageType | None:
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
    triggering_package: PackageType | None = None

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
