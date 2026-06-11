"""Adversarial satisfier probes: terms built from the trail's own constraints.

Random probe constraints rarely flip satisfaction mid-trail.  Here the
probe terms are algebraic combinations (identity, complement, pairwise
union and intersection) of the constraint reps used in the operations
themselves, so the earliest-satisfier boundary lands at interior trail
entries far more often, stressing the binary search and the stored
cumulative-range snapshots across backtracks.
"""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.partial_solution import PartialSolution
from nab_resolver.types import Term

from .rep_model import (
    PROBE_PACKAGES,
    ModelPS,
    Rep,
    apply_op,
    operations,
    rep_and,
    rep_not,
    rep_or,
    rep_to_range,
)
from .strategies import DEEP_SETTINGS

pytestmark = pytest.mark.property


def _derived_probe_reps(ops: list[tuple[object, ...]]) -> list[Rep]:
    """Combine the ops' own constraint reps into adversarial probes."""
    base: list[Rep] = []
    for op in ops:
        if op[0] == "derive":
            rep = op[2]
            assert isinstance(rep, tuple)
            base.append(rep)
    out: list[Rep] = []
    for rep in base[:5]:
        out.append(rep)
        out.append(rep_not(rep))
    for left, right in itertools.pairwise(base):
        out.append(rep_or(left, right))
        out.append(rep_and(left, rep_not(right)))
    return out[:10]


@given(
    ops=st.lists(operations(), min_size=2, max_size=30),
    pkg_picks=st.lists(st.sampled_from(PROBE_PACKAGES), min_size=1, max_size=3),
    polarity=st.booleans(),
)
@DEEP_SETTINGS
def test_satisfier_with_trail_derived_probes(
    ops: list[tuple[object, ...]],
    pkg_picks: list[str],
    polarity: bool,
) -> None:
    """Satisfier agrees with the model on trail-derived probes."""
    ps: PartialSolution[str, int] = PartialSolution()
    model = ModelPS()
    probe_reps = _derived_probe_reps(ops)
    for op in ops:
        apply_op(ps, model, op)
        for pkg in pkg_picks:
            entries = ps.assignments_for(pkg)
            for rep in probe_reps:
                for positive in (polarity, not polarity):
                    term: Term[str, int] = Term(
                        pkg, rep_to_range(rep), positive=positive
                    )
                    impl = ps.satisfier(term)
                    idx = model.satisfier_index(pkg, rep, positive=positive)
                    if idx is None:
                        assert impl is None
                    else:
                        assert impl is not None
                        assert impl is entries[idx]
