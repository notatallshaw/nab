"""Decision making for the PubGrub resolver.

Picks the next undecided package, asks the provider for a version,
and records ``NO_VERSIONS`` clauses or constraint clauses as needed.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#decision-making
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .incompat_index import add_incompatibility
from .root import ROOT
from .types import Incompatibility, IncompatibilityCause, Term

if TYPE_CHECKING:
    from .resolver import Resolver

__all__ = [
    "absorb_pending_clauses",
    "choose_package_to_decide",
    "choose_version",
    "inject_constraint",
    "record_no_versions",
]


def choose_package_to_decide(resolver: Resolver[Any, Any]) -> Any | None:
    """Choose the next undecided package, or None if all decided.

    Prefers ``is_ready`` packages so resolution keeps making progress while
    other listings/metadata are still in flight.
    """
    undecided = resolver.solution.undecided_packages()
    undecided.discard(ROOT)
    if not undecided:
        return None

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


def inject_constraint(resolver: Resolver[Any, Any], package: Any) -> bool:
    """Inject a CONSTRAINT incompatibility for a package, if applicable.

    Lazy: only fires the first time the package is about to be decided.
    Returns True if an incompatibility was injected (caller re-propagates).
    """
    if package not in resolver.constraints:
        return False
    if package in resolver.injected_constraints:
        return False

    resolver.injected_constraints.add(package)
    complement = ~resolver.constraints[package]
    if complement.is_empty:
        return False

    add_incompatibility(
        resolver,
        Incompatibility(
            [Term(package, complement, positive=True)],
            cause=IncompatibilityCause.CONSTRAINT,
        ),
    )
    return True


def choose_version(resolver: Resolver[Any, Any], package: Any) -> Any | None:
    """Ask the provider to pick a version within the allowed range."""
    current_range = resolver.solution.get(package) or resolver.range_type.full()
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
    add_incompatibility(
        resolver,
        Incompatibility(
            [Term(package, current_range, positive=True)],
            cause=IncompatibilityCause.NO_VERSIONS,
        ),
    )
