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

# Bound once so the hot paths load a module global instead of a class attribute.
_SATISFIED_REL = SetRelation.SATISFIED
_CONTRADICTED_REL = SetRelation.CONTRADICTED
_UNDETERMINED_REL = SetRelation.UNDETERMINED
_CONTRADICTED_STATE = IncompatibilityState.CONTRADICTED
_CONFLICT_STATE = IncompatibilityState.CONFLICT

__all__ = [
    "classify_relation",
    "evaluate_incompatibility",
    "term_relation",
    "unit_propagation",
]

# Upper bound on relation_cache size; cleared on overflow to bound memory.
RELATION_CACHE_MAX = 100_000

# A probe that misses builds its key for nothing, and on a conflict-heavy
# resolve most probes miss.  So the memo runs only while its hit rate over a
# window of probes pays for the key, and a longer window re-samples it later in
# case the resolve changes shape.
RELATION_GATE_WINDOW = 4_096
RELATION_GATE_MIN_HITS = 1_024
RELATION_GATE_RECHECK = 65_536

# Upper bound on the address-keyed token memo, cleared on overflow together
# with the range objects it holds alive.  A larger cap wipes less often and so
# holds on to more ranges, which costs peak memory.
RANGE_ID_MEMO_MAX = 8_192


def unit_propagation(
    resolver: Resolver[Any, Any], changed_package: Any
) -> Incompatibility[Any, Any] | None:
    """Propagate constraints from incompatibilities.

    When an incompatibility has all but one term satisfied, the
    remaining term's negation is derived (unit rule). Returns a
    conflicting incompatibility if all terms are satisfied, or
    None if propagation completes without conflict.

    One contradicted term settles a clause for the rest of the solution's
    contradiction epoch.  Each clause records the epoch it was last settled in,
    and is skipped while that stamp is current.

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#unit-propagation
    """
    propagation_queue: deque[Any] = deque([changed_package])
    in_queue: set[Any] = {changed_package}

    contradicted_at = resolver.clause_contradicted_at
    epoch = resolver.solution.contradiction_epoch

    while propagation_queue:
        package = propagation_queue.popleft()
        in_queue.discard(package)
        related_indices = resolver.package_to_incompatibilities.get(package, [])

        for incompatibility_index in related_indices:
            if contradicted_at[incompatibility_index] == epoch:
                continue

            incompatibility = resolver.incompatibilities[incompatibility_index]
            evaluation = evaluate_incompatibility(resolver, incompatibility)

            if evaluation is _CONTRADICTED_STATE:
                contradicted_at[incompatibility_index] = epoch
                continue

            if evaluation is _CONFLICT_STATE:
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

                # A derive that empties a range advances the epoch, which
                # retires the stamps taken before it.
                epoch = resolver.solution.contradiction_epoch

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
      ``IncompatibilityState.CONTRADICTED``: some term is contradicted
      ``Term``: exactly one undetermined term (unit propagation candidate)
      ``None``: two or more undetermined terms (nothing to do yet)
    """
    undetermined_term: Term[Any, Any] | None = None

    for term in incompatibility.terms:
        relation = term_relation(resolver, term)
        if relation is _SATISFIED_REL:
            continue
        if relation is _CONTRADICTED_REL:
            return _CONTRADICTED_STATE
        if undetermined_term is not None:
            return None
        undetermined_term = term

    if undetermined_term is not None:
        return undetermined_term
    return _CONFLICT_STATE


def _intern_range(resolver: Resolver[Any, Any], range_: RangeProtocol[Any]) -> int:
    """Return the token for ``range_``, minting one the first time it is seen.

    Equal ranges share a token, so the relation cache keys on range value
    rather than on identity.  The address memo the caller reads first is only
    sound while the object it answers for is alive, so ``interned_ranges``
    holds on to every range that memo records.
    """
    tokens = resolver.range_tokens
    token = tokens.get(range_)
    if token is None:
        token = resolver.next_range_token
        resolver.next_range_token = token + 1
        tokens[range_] = token

    id_tokens = resolver.range_token_by_id
    if len(id_tokens) >= RANGE_ID_MEMO_MAX:
        id_tokens.clear()
        resolver.interned_ranges.clear()
    id_tokens[id(range_)] = token
    resolver.interned_ranges.append(range_)

    return token


def _resample_relation_gate(resolver: Resolver[Any, Any]) -> None:
    """Judge the window of probes that just ended and open the next one.

    While the memo is on, the window counts hits, and too few switch the memo
    off and drop the entries it collected.  While it is off, the window is only
    the wait before the memo is tried again.
    """
    window = RELATION_GATE_WINDOW
    if not resolver.relation_cache_on:
        resolver.relation_cache_on = True
    elif resolver.relation_gate_hits < RELATION_GATE_MIN_HITS:
        resolver.relation_cache_on = False
        resolver.relation_cache.clear()
        window = RELATION_GATE_RECHECK

    resolver.relation_gate_hits = 0
    resolver.relation_gate_probes_left = window


def term_relation(resolver: Resolver[Any, Any], term: Term[Any, Any]) -> SetRelation:
    """Check how the partial solution relates to this term.

    A term can only be satisfied or contradicted when the package has a
    positive constraint; without one it might not participate in the
    solution (negative terms are trivially true for absent packages).

    See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#term
    """
    assignment = resolver.solution.get(term.package)
    if assignment is None:
        return _UNDETERMINED_REL

    positive = term.is_positive()
    constraint = term.constraint

    # key stays None while the memo is off, so a miss below stores nothing.
    key = None
    result = None
    if resolver.relation_cache_on:
        # Both lookups stay inline because most probes hit while the memo is
        # on; only a miss pays for the call into _intern_range.
        id_tokens = resolver.range_token_by_id
        assignment_token = id_tokens.get(id(assignment))
        if assignment_token is None:
            assignment_token = _intern_range(resolver, assignment)
        constraint_token = id_tokens.get(id(constraint))
        if constraint_token is None:
            constraint_token = _intern_range(resolver, constraint)

        key = (positive, assignment_token, constraint_token)
        result = resolver.relation_cache.get(key)

    if result is None:
        relation = assignment.relation(constraint)
        result = classify_relation(
            term, subset=relation.is_subset, disjoint=relation.is_disjoint
        )

        # A hit must not write this counter, so it is charged here and not above.
        probes_left = resolver.relation_gate_probes_left - 1
        resolver.relation_gate_probes_left = probes_left

        if key is not None:
            cache = resolver.relation_cache
            if len(cache) >= RELATION_CACHE_MAX:
                cache.clear()
            cache[key] = result

        # Nothing hits while the memo is off, so the recheck wait runs its
        # full length.
        if probes_left <= resolver.relation_gate_hits:
            _resample_relation_gate(resolver)
    else:
        resolver.relation_gate_hits += 1

    needs_positive = (positive and result is _SATISFIED_REL) or (
        not positive and result is _CONTRADICTED_REL
    )
    if needs_positive and not resolver.solution.has_positive_constraint(term.package):
        return _UNDETERMINED_REL

    return result


def classify_relation(
    term: Term[Any, Any],
    *,
    subset: bool,
    disjoint: bool,
) -> SetRelation:
    """Classify a term from the assignment's relation to its constraint.

    ``subset`` and ``disjoint`` describe the assignment against
    ``term.constraint``, as returned by ``RangeProtocol.relation``.

    Positive term: satisfied when the assignment is a subset of the
    constraint; contradicted when the two are disjoint.
    Negative term: the two swap, since a negative term forbids the
    constraint's versions.
    """
    if term.is_positive():
        satisfied, contradicted = subset, disjoint
    else:
        satisfied, contradicted = disjoint, subset

    if satisfied:
        return _SATISFIED_REL
    if contradicted:
        return _CONTRADICTED_REL
    return _UNDETERMINED_REL
