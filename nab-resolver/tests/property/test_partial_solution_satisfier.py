"""Property tests for the earliest-satisfier binary search.

``PartialSolution.satisfier`` finds the earliest trail entry that
satisfies a term with a binary search rather than a linear scan.  The
search is licensed by one invariant: along a package's chronological
trail the effective range only narrows (positive derivations intersect,
negative derivations union), so ``term.satisfies`` is monotone (once
true at an entry it stays true for every later entry).  The search
reads ``cum_positive`` / ``cum_negative`` stored on each entry, so a
stored-field or backtrack bug would silently return the wrong
satisfier.

These properties drive random decide/derive/backtrack sequences and
check the invariant directly and that the binary search agrees with an
independent linear scan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.partial_solution import PartialSolution
from nab_resolver.ranges import Range
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

from .strategies import PROPERTY_SETTINGS, terms, version_ranges

if TYPE_CHECKING:
    from nab_resolver.partial_solution import Assignment
    from nab_resolver.types import RangeProtocol

pytestmark = pytest.mark.property

_PKG = "pkg"
_VERSIONS = range(1, 21)

Op = tuple[str, "Range[int] | int"]


def _root() -> Incompatibility[str, int]:
    return Incompatibility([], cause=IncompatibilityCause.ROOT)


_operations = st.one_of(
    st.tuples(st.just("pos"), version_ranges()),
    st.tuples(st.just("neg"), version_ranges()),
    st.tuples(st.just("decide"), st.integers(min_value=1, max_value=20)),
    st.tuples(st.just("backtrack"), st.integers(min_value=0, max_value=8)),
)


def _member(rng: RangeProtocol[int]) -> int | None:
    """First version in the pool that ``rng`` admits, or None."""
    return next((v for v in _VERSIONS if v in rng), None)


def _apply(ps: PartialSolution[str, int], op: Op) -> None:
    """Apply one generated operation, staying within reachable states.

    The resolver only decides an undecided package that already carries a
    positive constraint, choosing a version inside its current range, so a
    decision never widens the effective range and monotonicity holds.
    """
    kind, payload = op
    if kind == "pos" and isinstance(payload, Range):
        ps.derive(_PKG, payload, positive=True, cause=_root())
    elif kind == "neg" and isinstance(payload, Range):
        ps.derive(_PKG, payload, positive=False, cause=_root())
    elif kind == "decide" and isinstance(payload, int):
        if _PKG in ps.decisions() or not ps.has_positive_constraint(_PKG):
            return
        effective = ps.get(_PKG)
        assert effective is not None
        chosen = payload if payload in effective else _member(effective)
        if chosen is not None:
            ps.decide(_PKG, chosen)
    elif kind == "backtrack" and isinstance(payload, int):
        ps.backtrack(payload % (ps.decision_level + 1))


def _effective(
    cum_pos: RangeProtocol[int] | None, cum_neg: RangeProtocol[int] | None
) -> RangeProtocol[int] | None:
    """Combine accumulated positive and negative ranges, as ``get`` does."""
    if cum_pos is None and cum_neg is None:
        return None
    if cum_pos is None:
        assert cum_neg is not None
        return ~cum_neg
    return cum_pos if cum_neg is None else cum_pos & ~cum_neg


def _linear_satisfier(
    ps: PartialSolution[str, int], term: Term[str, int]
) -> Assignment[str, int] | None:
    """Earliest satisfying entry by a plain scan, independent of stored fields."""
    cum_pos: RangeProtocol[int] | None = None
    cum_neg: RangeProtocol[int] | None = None
    is_positive = term.is_positive()
    for entry in ps.assignments_for(term.package):
        if entry.is_decision or entry.positive:
            cum_pos = entry.accumulated_range
        else:
            cum_neg = entry.accumulated_range
        effective = _effective(cum_pos, cum_neg)
        if not is_positive or cum_pos is not None:
            assert effective is not None
            if term.satisfies(effective):
                return entry
    return None


@given(
    ops=st.lists(_operations, min_size=1, max_size=14),
    probes=st.lists(terms(), min_size=1, max_size=4),
)
@PROPERTY_SETTINGS
def test_satisfied_at_is_monotone(ops: list[Op], probes: list[Term[str, int]]) -> None:
    """``_satisfied_at`` never flips back to false later in the trail."""
    ps: PartialSolution[str, int] = PartialSolution()
    for op in ops:
        _apply(ps, op)
        for term in probes:
            is_positive = term.is_positive()
            flags = [
                ps._satisfied_at(entry, term, is_positive=is_positive)
                for entry in ps.assignments_for(term.package)
            ]
            assert all(flags[i] <= flags[i + 1] for i in range(len(flags) - 1))


@given(
    ops=st.lists(_operations, min_size=1, max_size=14),
    probes=st.lists(terms(), min_size=1, max_size=4),
)
@PROPERTY_SETTINGS
def test_satisfier_matches_linear_scan(
    ops: list[Op], probes: list[Term[str, int]]
) -> None:
    """The binary search returns the same entry as a linear scan."""
    ps: PartialSolution[str, int] = PartialSolution()
    for op in ops:
        _apply(ps, op)
        for term in probes:
            assert ps.satisfier(term) is _linear_satisfier(ps, term)
