"""Differential tests: PartialSolution vs a replay-from-scratch model.

The single-package satisfier properties elsewhere in this suite cover
interval-shaped ranges only.  Here random decide/derive/backtrack
sequences over multiple interleaved packages, with finite and cofinite
constraint shapes, are applied in lockstep to the real class and to
:class:`ModelPS`, then every public query is compared:

- trail bookkeeping (levels monotone non-decreasing, decision count
  equals ``decision_level``, ``trail_index``/``package_index`` match);
- state queries (``get`` membership, ``decisions``,
  ``undecided_packages``, ``has_positive_constraint``);
- the binary-search ``satisfier``;
- backtrack removing exactly the assignments above the target level.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.partial_solution import PartialSolution
from nab_resolver.types import Term

from .rep_model import (
    ALL_PROBES,
    PROBE_PACKAGES,
    ModelPS,
    Rep,
    apply_op,
    operations,
    probe_terms,
    rep_and,
    rep_member,
    rep_not,
    rep_to_range,
)
from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property


def _check_trail_invariants(ps: PartialSolution[str, int], model: ModelPS) -> None:
    """Trail levels, indices, and decision counts match the model."""
    trail = ps._assignments
    levels = [a.decision_level for a in trail]
    assert levels == sorted(levels), "trail levels must be non-decreasing"
    assert all(lv <= model.level for lv in levels)
    assert ps.decision_level == model.level
    assert sum(1 for a in trail if a.is_decision) == ps.decision_level, (
        "decision count must equal decision_level (dense levels)"
    )
    for i, a in enumerate(trail):
        assert a.trail_index == i
    for pkg in PROBE_PACKAGES:
        entries = list(ps.assignments_for(pkg))
        assert entries == [a for a in trail if a.package == pkg]
        for j, a in enumerate(entries):
            assert a.package_index == j


def _check_state(ps: PartialSolution[str, int], model: ModelPS) -> None:
    """Every public state query agrees with the replayed model."""
    for pkg in PROBE_PACKAGES:
        pos, neg, decided, neg_recorded = model.state(pkg)
        assert ps.has_positive_constraint(pkg) == (pos is not None)
        got = ps.get(pkg)
        if pos is None and not neg_recorded:
            assert got is None, f"get({pkg!r}) should be None, got {got!r}"
            continue
        assert got is not None
        eff = rep_and(pos, rep_not(neg)) if pos is not None else rep_not(neg)
        for v in ALL_PROBES:
            assert (v in got) == rep_member(v, eff), (
                f"get({pkg!r}) membership mismatch at {v}: impl={got!r}"
            )
        if decided is None:
            assert pkg not in ps.decisions()
    assert ps.decisions() == model.decided_map()
    assert ps.undecided_packages() == model.undecided()


@given(ops=st.lists(operations(), min_size=1, max_size=18))
@PROPERTY_SETTINGS
def test_state_matches_model(ops: list[tuple[object, ...]]) -> None:
    """Every public state query agrees with replay-from-scratch after each op."""
    ps: PartialSolution[str, int] = PartialSolution()
    model = ModelPS()
    for op in ops:
        apply_op(ps, model, op)
        _check_trail_invariants(ps, model)
        _check_state(ps, model)


@given(
    ops=st.lists(operations(), min_size=1, max_size=18),
    probes=st.lists(probe_terms(), min_size=1, max_size=4),
)
@PROPERTY_SETTINGS
def test_satisfier_matches_model(
    ops: list[tuple[object, ...]], probes: list[tuple[str, Rep, bool]]
) -> None:
    """Binary-search satisfier agrees with the model."""
    ps: PartialSolution[str, int] = PartialSolution()
    model = ModelPS()
    for op in ops:
        apply_op(ps, model, op)
        for pkg, rep, positive in probes:
            term: Term[str, int] = Term(pkg, rep_to_range(rep), positive=positive)
            impl = ps.satisfier(term)
            idx = model.satisfier_index(pkg, rep, positive=positive)
            entries = ps.assignments_for(pkg)
            if idx is None:
                assert impl is None, f"satisfier should be None, got {impl!r}"
            else:
                assert impl is not None, (
                    f"satisfier missing for {term!r}, model index {idx}"
                )
                assert impl is entries[idx], (
                    f"satisfier mismatch for {term!r}: impl index "
                    f"{impl.package_index}, model index {idx}"
                )


@given(ops=st.lists(operations(), min_size=1, max_size=18))
@PROPERTY_SETTINGS
def test_backtrack_removes_only_above_target(ops: list[tuple[object, ...]]) -> None:
    """After backtrack(L) the surviving trail is exactly the level<=L prefix."""
    ps: PartialSolution[str, int] = PartialSolution()
    model = ModelPS()
    for op in ops:
        if op[0] == "backtrack":
            before = list(ps._assignments)
            raw = op[1]
            assert isinstance(raw, int)
            target = raw % (model.level + 1)
            expected = [a for a in before if a.decision_level <= target]
        else:
            expected = None
        apply_op(ps, model, op)
        if expected is not None:
            assert ps._assignments == expected
            assert all(a.decision_level <= ps.decision_level for a in ps._assignments)
