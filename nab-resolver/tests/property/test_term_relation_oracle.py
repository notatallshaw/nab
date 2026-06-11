"""Scenario-semantics oracle for ``term_relation``.

A partial solution constrains a package to either be present with a
version in pos minus neg (when it has a positive constraint), or be
absent or present with any version not in neg (negative-only).  Per
solver.md a term is SATISFIED if true in every consistent scenario,
CONTRADICTED if false in every one, else UNDETERMINED.
``term_relation``'s docstring adds a documented downgrade: without a
positive constraint a positive term is never SATISFIED and a negative
term never CONTRADICTED (the package may be absent), so those corners
accept UNDETERMINED.

The finite/cofinite rep algebra makes the oracle exact over all
integers, covering cofinite shapes (e.g. everything except {2, 5}) that
the interval-based strategies never produce.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.propagate import term_relation
from nab_resolver.resolver import Resolver
from nab_resolver.types import SetRelation, Term

from .providers import FuzzProvider
from .rep_model import (
    ModelPS,
    Rep,
    apply_op,
    operations,
    probe_terms,
    rep_and,
    rep_disjoint,
    rep_is_empty,
    rep_not,
    rep_subset,
    rep_to_range,
)
from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property


def _fresh_resolver() -> Resolver[str, int]:
    """A resolver used only as the term_relation state carrier."""
    return Resolver(FuzzProvider({}))


def _oracle_acceptable(
    pos: Rep | None, neg: Rep, *, neg_recorded: bool, c: Rep, positive: bool
) -> set[SetRelation]:
    """Acceptable results per scenario semantics plus documented downgrades."""
    if pos is None and not neg_recorded:
        # No assignment at all: documented UNDETERMINED.
        return {SetRelation.UNDETERMINED}

    can_be_absent = pos is None
    present = rep_and(pos, rep_not(neg)) if pos is not None else rep_not(neg)

    if rep_is_empty(present) and not can_be_absent:
        # No consistent scenario: vacuously both; only UNDETERMINED is wrong.
        return {SetRelation.SATISFIED, SetRelation.CONTRADICTED}

    if positive:
        all_true = (not can_be_absent) and rep_subset(present, c)
        all_false = rep_disjoint(present, c)
    else:
        all_true = rep_disjoint(present, c)
        all_false = (not can_be_absent) and rep_subset(present, c)

    if all_true:
        return {SetRelation.SATISFIED}
    if all_false:
        if can_be_absent and rep_is_empty(present):
            # Only the absent scenario exists.  classify_intersection's
            # covers-before-empty tie-break reports SATISFIED, which the
            # needs_positive gate downgrades to UNDETERMINED, so the
            # truthful CONTRADICTED is unreachable here.  Sound but lossy.
            return {SetRelation.CONTRADICTED, SetRelation.UNDETERMINED}
        return {SetRelation.CONTRADICTED}
    return {SetRelation.UNDETERMINED}


@given(
    ops=st.lists(operations(), min_size=1, max_size=15),
    probes=st.lists(probe_terms(), min_size=1, max_size=4),
)
@PROPERTY_SETTINGS
def test_term_relation_matches_scenario_semantics(
    ops: list[tuple[object, ...]], probes: list[tuple[str, Rep, bool]]
) -> None:
    """term_relation returns a scenario-semantics-acceptable relation."""
    resolver = _fresh_resolver()
    model = ModelPS()
    for op in ops:
        apply_op(resolver.solution, model, op)
        for pkg, rep, positive in probes:
            term: Term[str, int] = Term(pkg, rep_to_range(rep), positive=positive)
            result = term_relation(resolver, term)
            pos, neg, _, neg_recorded = model.state(pkg)
            acceptable = _oracle_acceptable(
                pos, neg, neg_recorded=neg_recorded, c=rep, positive=positive
            )
            assert result in acceptable, (
                f"term_relation({term!r}) = {result} not in {acceptable}; "
                f"pos={pos}, neg={neg}, neg_recorded={neg_recorded}"
            )
            # Warm- and cold-cache results must agree.
            assert term_relation(resolver, term) is result
            fresh = _fresh_resolver()
            fresh.solution = resolver.solution
            assert term_relation(fresh, term) is result
