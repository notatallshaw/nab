"""Decision making for the PubGrub resolver.

Picks the next undecided package, asks the provider for a version,
and records ``NO_VERSIONS`` clauses or constraint clauses as needed.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#decision-making
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .incompat_index import add_incompatibility
from .root import ROOT
from .types import Incompatibility, IncompatibilityCause, RangeProtocol, Term

if TYPE_CHECKING:
    from .resolver import Resolver

__all__ = [
    "absorb_pending_clauses",
    "absorb_redundant_requirement",
    "choose_package_to_decide",
    "choose_version",
    "record_no_versions",
]


def choose_package_to_decide(resolver: Resolver[Any, Any]) -> Any | None:
    """Choose the next undecided package, or None if all decided.

    Prefers ``is_ready`` packages so resolution keeps making progress while
    other listings/metadata are still in flight.  ``begin_decision_scan`` marks
    the start of the scan so a provider fed by another thread can hold the
    state behind its sort key still: ``min`` only picks correctly over a key
    that does not move while it runs.
    """
    undecided = resolver.solution.undecided_packages()
    undecided.discard(ROOT)
    if not undecided:
        return None

    resolver.provider.begin_decision_scan()

    conflict_counts = resolver.stats.package_conflict_counts
    culprit_counts = resolver.stats.package_culprit_counts
    get_range = resolver.solution.get
    any_range = resolver.range_type.full()
    prioritize = resolver.provider.prioritize
    is_ready = resolver.provider.is_ready
    tiebreak_cache = resolver.tiebreak_cache
    root_order = resolver.root_package_order

    def sort_key(package: Any) -> tuple[Any, ...]:
        priority = prioritize(
            package,
            get_range(package) or any_range,
            conflict_counts,
            culprit_counts,
        )
        ready_penalty = 0 if is_ready(package) else 1
        tiebreak = tiebreak_cache.get(package)
        if tiebreak is None:
            tiebreak = root_order.get(package)
            if tiebreak is None:
                tiebreak = (1, 0, str(package))
            tiebreak_cache[package] = tiebreak
        return (ready_penalty, priority, tiebreak)

    return min(undecided, key=sort_key)


def choose_version(resolver: Resolver[Any, Any], package: Any) -> Any | None:
    """Ask the provider to pick a version within the allowed range.

    A user constraint narrows the acceptable range here rather than acting
    as an incompatibility: it restricts which version is picked but never
    forces the package into the solution.
    """
    current_range = resolver.solution.get(package) or resolver.range_type.full()
    constraint = resolver.constraints.get(package)
    if constraint is not None:
        current_range = current_range & constraint
    resolver.provider.receive_partial_solution_hint(
        resolver.solution.positive_ranges(),
        resolver.solution.decisions(),
    )
    return resolver.provider.choose_version(package, current_range)


def absorb_pending_clauses(resolver: Resolver[Any, Any]) -> bool:
    """Drain provider-queued incompatibilities into the formula.

    Look-ahead providers push binary clauses like
    ``{candidate==v, blocking_decision==w}`` instead of relying on the
    broader ``NO_VERSIONS`` clause.  Returns True so the caller can suppress
    the default ``NO_VERSIONS`` clause this turn.
    """
    clauses = list(resolver.provider.consume_pending_clauses())
    for incompatibility in clauses:
        add_incompatibility(resolver, incompatibility)
    return bool(clauses)


def absorb_redundant_requirement(
    resolver: Resolver[Any, Any],
    package: Any,
    requirement: RangeProtocol[Any],
    cause: Incompatibility[Any, Any],
) -> None:
    """Derive a requirement that refines a package's range without narrowing it.

    Unit propagation acts only on requirements that narrow a package's version
    set. When a version-set-redundant requirement still yields a different
    range, that difference is a refinement the range type carries (such as a
    pre-release opt-in), so derive it onto the package. The derivation rides the
    parent decision level, is undone on backtracking, and leaves every term
    relation unchanged, so nothing re-propagates.
    """
    positive = resolver.solution.positive_range(package)
    if positive is None:
        return

    # Intersect first: an unchanged range short-circuits before the costlier
    # subset test, which then separates a refinement from a narrowing.
    folded = positive & requirement
    if folded != positive and positive.is_subset(requirement):
        resolver.solution.derive(package, requirement, positive=True, cause=cause)
        resolver.stats.derivations += 1
        resolver.observer.on_derivation(package, positive=True, cause=cause)


def _constraint_hid_a_version(
    resolver: Resolver[Any, Any], package: Any, current_range: RangeProtocol[Any]
) -> bool:
    """Return whether a user constraint is why ``choose_version`` found nothing.

    ``choose_version`` already returned None over the constraint-narrowed range.
    Ask whether the un-narrowed ``current_range`` would still yield a version: a
    hit means the constraint clipped away what would otherwise have been chosen,
    so the constraint is the cause; a miss means the package fails on its own (no
    versions, or none satisfy the requirement) and the constraint is irrelevant.
    """
    return resolver.provider.has_satisfying_version(package, current_range)


def record_no_versions(
    resolver: Resolver[Any, Any], package: Any, *, had_pending: bool
) -> None:
    """Add the default ``NO_VERSIONS`` clause for ``package``.

    Skipped when the provider already supplied context-aware clauses;
    otherwise the broad clause would persist past the backjump that lifts
    the supporting decisions.
    """
    if had_pending:
        return

    current_range = resolver.solution.get(package) or resolver.range_type.full()
    resolver.observer.on_no_versions(package, current_range)

    constraint = resolver.constraints.get(package)
    constrained = constraint is not None and _constraint_hid_a_version(
        resolver, package, current_range
    )
    if constrained:
        cause = IncompatibilityCause.CONSTRAINT
        constraint_range = constraint
    else:
        cause = IncompatibilityCause.NO_VERSIONS
        constraint_range = None

    add_incompatibility(
        resolver,
        Incompatibility(
            [Term(package, current_range, positive=True)],
            cause=cause,
            constraint_range=constraint_range,
        ),
    )
