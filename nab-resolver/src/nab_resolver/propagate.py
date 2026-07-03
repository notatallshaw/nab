"""Unit propagation for the PubGrub resolver.

When an incompatibility has all but one term satisfied, the
remaining term's negation is derived (unit rule).  This module
owns that loop plus the per-term/per-incompatibility evaluators
that classify each term as SATISFIED, CONTRADICTED, or UNDETERMINED.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#unit-propagation
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any

from .types import IncompatibilityState, SetRelation, Term

if TYPE_CHECKING:
    from .resolver import Resolver
    from .types import Incompatibility, RangeProtocol

__all__ = [
    "classify_intersection",
    "evaluate_incompatibility",
    "term_relation",
    "unit_propagation",
]

# Upper bound on relation_cache size; cleared on overflow to bound memory.
RELATION_CACHE_MAX = 100_000


def unit_propagation(
    resolver: Resolver[Any, Any], changed_package: Any
) -> Incompatibility[Any, Any] | None:
    """Propagate constraints from incompatibilities.

    When an incompatibility has all but one term satisfied, the
    remaining term's negation is derived (unit rule). Returns a
    conflicting incompatibility if all terms are satisfied, or
    None if propagation completes without conflict.

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#unit-propagation
    """
    propagation_queue: deque[Any] = deque([changed_package])
    in_queue: set[Any] = {changed_package}

    while propagation_queue:
        package = propagation_queue.popleft()
        in_queue.discard(package)
        related_indices = resolver.package_to_incompatibilities.get(package, [])

        for incompatibility_index in related_indices:
            incompatibility = resolver.incompatibilities[incompatibility_index]
            evaluation = evaluate_incompatibility(resolver, incompatibility)

            if evaluation is IncompatibilityState.CONFLICT:
                return incompatibility

            if isinstance(evaluation, Term):
                negated_term = evaluation.negate()
                range_before = resolver.solution.get(negated_term.package)
                resolver.solution.derive(
                    negated_term.package,
                    negated_term.constraint,
                    positive=negated_term.is_positive(),
                    cause=incompatibility,
                )
                range_after = resolver.solution.get(negated_term.package)

                if range_before != range_after:
                    resolver.stats.derivations += 1
                    resolver.observer.on_derivation(
                        negated_term.package,
                        positive=negated_term.is_positive(),
                        cause=incompatibility,
                    )
                    if negated_term.package not in in_queue:
                        propagation_queue.append(negated_term.package)
                        in_queue.add(negated_term.package)

    return None


def evaluate_incompatibility(
    resolver: Resolver[Any, Any], incompatibility: Incompatibility[Any, Any]
) -> IncompatibilityState | Term[Any, Any] | None:
    """Evaluate an incompatibility against the current partial solution.

    Returns:
      ``IncompatibilityState.CONFLICT``: all terms satisfied
      ``Term``: exactly one undetermined term (unit propagation candidate)
      ``None``: 0 or 2+ undetermined terms (nothing to do yet)
    """
    undetermined_term: Term[Any, Any] | None = None

    for term in incompatibility.terms:
        relation = term_relation(resolver, term)
        if relation is SetRelation.SATISFIED:
            continue
        if relation is SetRelation.CONTRADICTED:
            return None
        if undetermined_term is not None:
            return None
        undetermined_term = term

    if undetermined_term is not None:
        return undetermined_term
    return IncompatibilityState.CONFLICT


def term_relation(resolver: Resolver[Any, Any], term: Term[Any, Any]) -> SetRelation:
    """Check how the partial solution relates to this term.

    A term can only be satisfied or contradicted when the package has a
    positive constraint; without one it might not participate in the
    solution (negative terms are trivially true for absent packages).

    See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#term
    """
    assignment = resolver.solution.get(term.package)
    if assignment is None:
        return SetRelation.UNDETERMINED

    positive = term.is_positive()
    cache = resolver.relation_cache
    key = (positive, assignment, term.constraint)
    result = cache.get(key)
    if result is None:
        intersection = assignment & term.constraint
        result = classify_intersection(term, assignment, intersection)
        if len(cache) >= RELATION_CACHE_MAX:
            cache.clear()
        cache[key] = result

    needs_positive = (positive and result is SetRelation.SATISFIED) or (
        not positive and result is SetRelation.CONTRADICTED
    )
    if needs_positive and not resolver.solution.has_positive_constraint(term.package):
        return SetRelation.UNDETERMINED

    return result


def classify_intersection(
    term: Term[Any, Any],
    assignment: RangeProtocol[Any],
    intersection: RangeProtocol[Any],
) -> SetRelation:
    """Classify a term against its precomputed assignment intersection.

    Positive term: satisfied when the assignment is a subset of the
    constraint; contradicted when the intersection is empty.
    Negative term: satisfied when the intersection is empty; contradicted
    when the assignment falls entirely inside the forbidden range.
    """
    if term.is_positive():
        satisfied = assignment.is_subset(term.constraint)
        contradicted = intersection.is_empty
    else:
        satisfied = intersection.is_empty
        contradicted = assignment.is_subset(term.constraint)

    if satisfied:
        return SetRelation.SATISFIED
    if contradicted:
        return SetRelation.CONTRADICTED
    return SetRelation.UNDETERMINED
