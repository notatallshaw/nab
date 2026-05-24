"""CrossHair-driven property tests for ``Range`` and ``Term``.

`CrossHair`_ uses concolic / symbolic execution backed by Z3 to find
needle-in-haystack counterexamples that random property testing
(Hypothesis) tends to miss.  It works well on the kind of
domain-specific Boolean-algebra code that the PubGrub resolver core
is built on.

This file mirrors a subset of ``test_ranges_set_algebra`` and
``test_pubgrub_term`` against integer versions, but uses Hypothesis's
``backend="crosshair"`` shim so each property is checked by Z3
instead of random sampling.

CrossHair runs much slower than the random backend (5x to 100x) so
this file is gated behind the ``crosshair`` pytest marker.  Run it
opt-in with ``pytest -m crosshair``; it is excluded from the default
property-test suite via :data:`pytestmark`.

.. _CrossHair: https://crosshair.readthedocs.io/
"""

from __future__ import annotations

import pytest

pytest.importorskip("hypothesis_crosshair_provider")

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nab_resolver.ranges import Range
from nab_resolver.types import Term

pytestmark = pytest.mark.crosshair


CROSSHAIR_SETTINGS = settings(
    backend="crosshair",
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.large_base_example],
)
"""Settings for CrossHair-backed property tests.

CrossHair explores path space rather than input space, so a small
``max_examples`` is usually enough to enumerate every distinct
execution path.  Per-test budgets in the 30-300 second range are
typical.
"""


def _int_range_from_kind(kind: int, lo: int, hi: int) -> Range[int]:
    """Build a ``Range[int]`` from a discrete ``kind`` and two bounds.

    CrossHair handles integer-typed parameters and discrete cases
    well; constructing the ``Range`` from primitive ints lets Z3
    enumerate every range shape symbolically.
    """
    if kind == 0:
        return Range.empty()
    if kind == 1:
        return Range.full()
    if kind == 2:
        return Range.singleton(lo)
    if kind == 3:
        return Range.at_least(lo)
    if kind == 4:
        return Range.less_than(hi)
    if lo >= hi:
        return Range.empty()
    return Range.between(lo, hi)


@given(
    kind=st.integers(min_value=0, max_value=5),
    lo=st.integers(min_value=0, max_value=20),
    hi=st.integers(min_value=0, max_value=20),
    probe=st.integers(min_value=0, max_value=20),
)
@CROSSHAIR_SETTINGS
def test_double_complement_pointwise(kind: int, lo: int, hi: int, probe: int) -> None:
    """``v in R`` iff ``v in ~~R`` for every constructed Range and probe v.

    Boolean-lattice double-negation law.  A pointwise check makes
    this test sensitive to interval-representation bugs that an
    equality check would miss.
    """
    range_ = _int_range_from_kind(kind, lo, hi)
    assert (probe in range_) == (probe in ~~range_)


@given(
    kind_a=st.integers(min_value=0, max_value=5),
    lo_a=st.integers(min_value=0, max_value=20),
    hi_a=st.integers(min_value=0, max_value=20),
    kind_b=st.integers(min_value=0, max_value=5),
    lo_b=st.integers(min_value=0, max_value=20),
    hi_b=st.integers(min_value=0, max_value=20),
    probe=st.integers(min_value=0, max_value=20),
)
@CROSSHAIR_SETTINGS
def test_de_morgan_intersection_pointwise(
    kind_a: int,
    lo_a: int,
    hi_a: int,
    kind_b: int,
    lo_b: int,
    hi_b: int,
    probe: int,
) -> None:
    """``~(A & B) == ~A | ~B`` pointwise.

    De Morgan's intersection law.  CrossHair will manufacture
    interval boundary inputs that random testing would only stumble
    on by accident.
    """
    range_a = _int_range_from_kind(kind_a, lo_a, hi_a)
    range_b = _int_range_from_kind(kind_b, lo_b, hi_b)
    left = ~(range_a & range_b)
    right = ~range_a | ~range_b
    assert (probe in left) == (probe in right)


@given(
    kind_a=st.integers(min_value=0, max_value=5),
    lo_a=st.integers(min_value=0, max_value=20),
    hi_a=st.integers(min_value=0, max_value=20),
    kind_b=st.integers(min_value=0, max_value=5),
    lo_b=st.integers(min_value=0, max_value=20),
    hi_b=st.integers(min_value=0, max_value=20),
    probe=st.integers(min_value=0, max_value=20),
)
@CROSSHAIR_SETTINGS
def test_intersection_subset_of_both(
    kind_a: int,
    lo_a: int,
    hi_a: int,
    kind_b: int,
    lo_b: int,
    hi_b: int,
    probe: int,
) -> None:
    """If ``v in A & B`` then ``v in A`` and ``v in B``."""
    range_a = _int_range_from_kind(kind_a, lo_a, hi_a)
    range_b = _int_range_from_kind(kind_b, lo_b, hi_b)
    intersection = range_a & range_b
    if probe in intersection:
        assert probe in range_a
        assert probe in range_b


@given(
    kind=st.integers(min_value=0, max_value=5),
    lo=st.integers(min_value=0, max_value=20),
    hi=st.integers(min_value=0, max_value=20),
    polarity=st.booleans(),
    probe=st.integers(min_value=0, max_value=20),
)
@CROSSHAIR_SETTINGS
def test_term_double_negation_pointwise(
    kind: int, lo: int, hi: int, polarity: bool, probe: int
) -> None:
    """``not not T`` allows ``v`` iff ``T`` allows ``v``.

    Stated at the Term level (with polarity).  Combines the Range
    double-complement law with the polarity flip in ``Term.negate``.
    """
    range_ = _int_range_from_kind(kind, lo, hi)
    term = Term("pkg", range_, positive=polarity)
    double = term.negate().negate()

    def _allows(t: Term[str, int]) -> bool:
        return probe in t.constraint if t.is_positive() else probe not in t.constraint

    assert _allows(term) == _allows(double)


@given(
    kind=st.integers(min_value=0, max_value=5),
    lo=st.integers(min_value=0, max_value=20),
    hi=st.integers(min_value=0, max_value=20),
    probe=st.integers(min_value=0, max_value=20),
)
@CROSSHAIR_SETTINGS
def test_complement_partition(kind: int, lo: int, hi: int, probe: int) -> None:
    """For every version ``v``, exactly one of ``v in R`` and ``v in ~R`` holds.

    A range and its complement partition the universe; a Z3 model
    that violates this would expose a subtle interval-boundary bug
    in either ``__contains__`` or ``__invert__``.
    """
    range_ = _int_range_from_kind(kind, lo, hi)
    complement = ~range_
    assert (probe in range_) is not (probe in complement)
