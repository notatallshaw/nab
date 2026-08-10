"""Property tests verifying ``Range`` satisfies the standard set-algebra laws.

``Range[V]`` represents a set of versions as a sorted list of
non-overlapping intervals.  The PubGrub algorithm relies on the type
behaving like a Boolean algebra over those intervals.  This file walks
the laws of set algebra (commutativity, associativity, idempotence,
identity, complement, and De Morgan's laws) and adds a property test
for each one.

The tests construct random ``Range[int]`` values and verify pointwise
on a small enumeration domain that the law holds for every version
in the test pool.  Pointwise comparison rules out subtle interval
representation bugs that an equality check would miss when two
``Range`` instances denote the same set with different normal forms.

Reference: https://en.wikipedia.org/wiki/Algebra_of_sets
"""

# ruff: noqa: RUF002
# RUF002 / RUF003: allow set-theory operators in docstrings.

from __future__ import annotations

import operator
from functools import reduce

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.ranges import (
    NEGATIVE_INFINITY,
    POSITIVE_INFINITY,
    Range,
)

from .strategies import (
    DEEP_SETTINGS,
    PROPERTY_SETTINGS,
    VERSION_RANGE,
    version_ranges,
)

pytestmark = pytest.mark.property


class TestSingleton:
    """A singleton range ``{v}`` contains exactly the version ``v``.

    The contains check is the most basic invariant that property tests
    exercise: every other property reduces to "for every version v, v in
    R agrees with the algebraic equivalent".
    """

    @given(version=...)
    @PROPERTY_SETTINGS
    def test_singleton_contains_exactly_v(self, version: int) -> None:
        """``v in singleton(v)`` and ``v' not in singleton(v)`` for ``v' != v``."""
        singleton = Range.singleton(version)
        assert version in singleton
        assert (version - 1) not in singleton
        assert (version + 1) not in singleton


class TestFromVersions:
    """``from_versions`` builds the canonical range over a finite version set.

    The oracles are a Python ``set`` and a sorted interval tuple, not a
    fold of ``singleton`` with ``|``, so a fault in the union machinery
    cannot cancel out against the constructor.
    """

    @given(versions=st.lists(st.sampled_from(VERSION_RANGE), max_size=8))
    @DEEP_SETTINGS
    def test_contains_exactly_the_given_versions(self, versions: list[int]) -> None:
        """``v in from_versions(vs)`` iff ``v`` appears in ``vs``."""
        built = Range.from_versions(versions)
        wanted = set(versions)
        for version in VERSION_RANGE:
            assert (version in built) == (version in wanted)

    @given(versions=st.lists(st.sampled_from(VERSION_RANGE), max_size=8))
    @DEEP_SETTINGS
    def test_intervals_are_the_distinct_versions_in_order(
        self, versions: list[int]
    ) -> None:
        """One inclusive singleton per distinct version, ascending.

        Membership alone would let an unsorted or repeating interval list
        through, and equality and hashing both read that list.
        """
        assert Range.from_versions(versions)._intervals == tuple(
            (version, True, version, True) for version in sorted(set(versions))
        )


class TestComplement:
    """The complement of ``R`` is ``Universe \\ R``: it contains exactly
    those versions ``v`` for which ``v not in R``.

    This is one of the fundamental Boolean-algebra laws used by PubGrub
    when computing negative terms.
    """

    @given(version=...)
    @PROPERTY_SETTINGS
    def test_complement_is_disjoint(self, version: int) -> None:
        """A range and its complement never overlap pointwise."""
        original = Range.at_least(version)
        complement = ~original
        for test_version in range(max(1, version - 5), version + 6):
            assert not (test_version in original and test_version in complement)

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_double_complement_is_identity(self, range_: Range[int]) -> None:
        """``~~A == A`` (Boolean lattice double-negation law)."""
        assert ~~range_ == range_

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_complement_intersection_is_empty(self, range_: Range[int]) -> None:
        """``A ∩ ~A == ∅`` (Boolean lattice law of complement)."""
        assert (range_ & ~range_).is_empty

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_complement_union_is_universal(self, range_: Range[int]) -> None:
        """``A ∪ ~A == U`` (Boolean lattice law of complement)."""
        union = range_ | ~range_
        for version in VERSION_RANGE:
            assert version in union


class TestIntersection:
    """Intersection ``A ∩ B`` contains exactly the versions in both ``A``
    and ``B``.

    The PubGrub spec assumes intersection is commutative, associative,
    idempotent, and has ``Universe`` as identity.  Each of those is a
    test below.
    """

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_intersection_is_subset_of_both(
        self, left: Range[int], right: Range[int]
    ) -> None:
        """If ``v in A ∩ B`` then ``v in A`` and ``v in B``."""
        intersection = left & right
        for version in VERSION_RANGE:
            if version in intersection:
                assert version in left
                assert version in right

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_commutative(self, left: Range[int], right: Range[int]) -> None:
        """``A ∩ B == B ∩ A``."""
        assert (left & right) == (right & left)

    @given(left=version_ranges(), middle=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_associative(
        self, left: Range[int], middle: Range[int], right: Range[int]
    ) -> None:
        """``(A ∩ B) ∩ C == A ∩ (B ∩ C)``."""
        outer_left = (left & middle) & right
        outer_right = left & (middle & right)
        for version in VERSION_RANGE:
            assert (version in outer_left) == (version in outer_right)

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_idempotent(self, range_: Range[int]) -> None:
        """``A ∩ A == A``."""
        assert (range_ & range_) == range_

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_identity_with_full(self, range_: Range[int]) -> None:
        """``A ∩ Universe == A`` (Universe is the identity element)."""
        assert (range_ & Range.full()) == range_

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_zero_with_empty(self, range_: Range[int]) -> None:
        """``A ∩ ∅ == ∅`` (Empty is the absorbing element)."""
        assert (range_ & Range.empty()).is_empty


class TestUnion:
    """Union ``A ∪ B`` contains exactly the versions in either ``A`` or
    ``B``.

    The PubGrub spec assumes union is commutative, associative,
    idempotent, and has ``∅`` as identity.
    """

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_union_is_superset_of_both(
        self, left: Range[int], right: Range[int]
    ) -> None:
        """If ``v in A`` or ``v in B`` then ``v in A ∪ B``."""
        union = left | right
        for version in VERSION_RANGE:
            if version in left or version in right:
                assert version in union

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_commutative(self, left: Range[int], right: Range[int]) -> None:
        """``A ∪ B == B ∪ A``."""
        assert (left | right) == (right | left)

    @given(left=version_ranges(), middle=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_associative(
        self, left: Range[int], middle: Range[int], right: Range[int]
    ) -> None:
        """``(A ∪ B) ∪ C == A ∪ (B ∪ C)``."""
        outer_left = (left | middle) | right
        outer_right = left | (middle | right)
        for version in VERSION_RANGE:
            assert (version in outer_left) == (version in outer_right)

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_idempotent(self, range_: Range[int]) -> None:
        """``A ∪ A == A``."""
        assert (range_ | range_) == range_

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_identity_with_empty(self, range_: Range[int]) -> None:
        """``A ∪ ∅ == A`` (Empty is the identity element)."""
        assert (range_ | Range.empty()) == range_


class TestDeMorgan:
    """De Morgan's laws relate ``∩``, ``∪``, and complement.

    For sets ``A`` and ``B``: ``~(A ∩ B) == ~A ∪ ~B`` and
    ``~(A ∪ B) == ~A ∩ ~B``.

    PubGrub's term-union derivation in :func:`union_terms` relies on the
    intersection variant: the union of two negative terms reduces to
    the negation of the intersection of their constraints.

    Reference:
        https://en.wikipedia.org/wiki/De_Morgan%27s_laws
    """

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_intersection_complement(self, left: Range[int], right: Range[int]) -> None:
        """``~(A ∩ B) == ~A ∪ ~B``."""
        outer_left = ~(left & right)
        outer_right = ~left | ~right
        for version in VERSION_RANGE:
            assert (version in outer_left) == (version in outer_right)

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_union_complement(self, left: Range[int], right: Range[int]) -> None:
        """``~(A ∪ B) == ~A ∩ ~B``."""
        outer_left = ~(left | right)
        outer_right = ~left & ~right
        for version in VERSION_RANGE:
            assert (version in outer_left) == (version in outer_right)


class TestDistributive:
    """Intersection distributes over union and vice versa.

    ``A ∩ (B ∪ C) == (A ∩ B) ∪ (A ∩ C)``
    ``A ∪ (B ∩ C) == (A ∪ B) ∩ (A ∪ C)``

    These laws make it safe for the resolver to refactor
    intersections and unions without changing their meaning.
    """

    @given(left=version_ranges(), middle=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_intersection_over_union(
        self, left: Range[int], middle: Range[int], right: Range[int]
    ) -> None:
        """``A ∩ (B ∪ C) == (A ∩ B) ∪ (A ∩ C)``."""
        outer_left = left & (middle | right)
        outer_right = (left & middle) | (left & right)
        for version in VERSION_RANGE:
            assert (version in outer_left) == (version in outer_right)

    @given(left=version_ranges(), middle=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_union_over_intersection(
        self, left: Range[int], middle: Range[int], right: Range[int]
    ) -> None:
        """``A ∪ (B ∩ C) == (A ∪ B) ∩ (A ∪ C)``."""
        outer_left = left | (middle & right)
        outer_right = (left | middle) & (left | right)
        for version in VERSION_RANGE:
            assert (version in outer_left) == (version in outer_right)

    @given(left=version_ranges(), middle=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_intersection_over_union_structural(
        self, left: Range[int], middle: Range[int], right: Range[int]
    ) -> None:
        """``(A ∩ B) ∪ (A ∩ C) == A ∩ (B ∪ C)`` as ``Range`` objects.

        The pointwise distributivity tests above check membership
        agreement.  This stronger version asserts the two sides also
        share the same internal interval representation, which exposes
        normalisation gaps that pointwise checks would let through.
        """
        outer_left = (left & middle) | (left & right)
        outer_right = left & (middle | right)
        assert outer_left == outer_right


class TestAbsorption:
    """Absorption laws fold a redundant union or intersection.

    ``A ∪ (A ∩ B) == A``
    ``A ∩ (A ∪ B) == A``

    Reference:
        https://en.wikipedia.org/wiki/Absorption_law
    """

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_union_absorbs_intersection(
        self, left: Range[int], right: Range[int]
    ) -> None:
        """``A ∪ (A ∩ B) == A``."""
        assert (left | (left & right)) == left

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_intersection_absorbs_union(
        self, left: Range[int], right: Range[int]
    ) -> None:
        """``A ∩ (A ∪ B) == A``."""
        assert (left & (left | right)) == left


class TestEmpty:
    """``Range.is_empty`` agrees with pointwise non-membership.

    ``is_empty`` short-circuits on the interval-tuple length while the
    resolver consults membership through ``__contains__``.  When the
    predicate is true the two answers must agree on every concrete
    version: an empty range cannot still contain a sample.

    The implication is one-directional because ``version_ranges`` can
    produce a range whose only members lie outside the small
    enumeration pool (``Range.greater_than(20)`` is the canonical
    case).  Such a range is non-empty by construction yet looks
    pointwise empty to a finite enumerator.
    """

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_is_empty_implies_pointwise_non_membership(
        self, range_: Range[int]
    ) -> None:
        """``R.is_empty`` implies every enumerated version is outside ``R``."""
        if range_.is_empty:
            for version in VERSION_RANGE:
                assert version not in range_


class TestNormalisation:
    """The normalised interval list orders intervals strictly.

    After a union, internal intervals must be pairwise non-overlapping
    and pairwise non-touching.  Two adjacent intervals must either
    leave a strict gap (``prev_upper < next_lower``) or share an
    endpoint with neither side inclusive (``prev_upper == next_lower``
    with both inclusivity flags False).  Without that, equality on
    the interval tuple would mistakenly distinguish two ranges that
    denote the same set, breaking the hash-and-equality contract
    PubGrub relies on for incompatibility deduplication.
    """

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_intervals_pairwise_disjoint_after_union(
        self, left: Range[int], right: Range[int]
    ) -> None:
        """After a union, every consecutive interval pair is strictly separated."""
        result = left | right
        intervals = result._intervals
        for index in range(1, len(intervals)):
            _, _, prev_upper, prev_upper_inclusive = intervals[index - 1]
            next_lower, next_lower_inclusive, _, _ = intervals[index]

            # A normalised list has no infinities mid-sequence: an
            # interior interval with +inf upper would swallow the rest.
            assert prev_upper is not POSITIVE_INFINITY
            assert next_lower is not NEGATIVE_INFINITY

            if prev_upper < next_lower:
                continue

            # Strict gap absent: the only legal touching case is
            # equal endpoints with neither side inclusive.
            assert prev_upper == next_lower
            assert not prev_upper_inclusive
            assert not next_lower_inclusive


@st.composite
def range_lists(draw: st.DrawFn) -> list[Range[int]]:
    """Draw a small list of ranges suitable for an N-ary chain test."""
    return draw(st.lists(version_ranges(), min_size=3, max_size=6))


class TestNAryChain:
    """N-ary intersection/union chains uphold the subset/superset laws.

    For any non-empty list of ranges ``[R0, R1, ..., Rn]``:

    * The intersection chain ``R0 & R1 & ... & Rn`` is a subset of
      every ``Ri``: a version in the chain must be in each input.
    * The union chain ``R0 | R1 | ... | Rn`` is a superset of every
      ``Ri``: a version in some input must be in the union.

    These two laws are the N-ary lifts of the binary subset/superset
    properties exercised by :class:`TestIntersection` and
    :class:`TestUnion`.  Folding via :func:`functools.reduce` over a
    list models how callers in the wild build constraint chains.
    """

    @given(ranges=range_lists())
    @DEEP_SETTINGS
    def test_intersection_chain_is_subset_of_each(
        self, ranges: list[Range[int]]
    ) -> None:
        """``v in (R0 & ... & Rn)`` implies ``v in Ri`` for every ``i``."""
        intersection = reduce(operator.and_, ranges)
        for version in VERSION_RANGE:
            if version in intersection:
                for range_ in ranges:
                    assert version in range_

    @given(ranges=range_lists())
    @DEEP_SETTINGS
    def test_union_chain_is_superset_of_each(self, ranges: list[Range[int]]) -> None:
        """``v in Ri`` for any ``i`` implies ``v in (R0 | ... | Rn)``."""
        union = reduce(operator.or_, ranges)
        for version in VERSION_RANGE:
            for range_ in ranges:
                if version in range_:
                    assert version in union
                    break


class TestDifference:
    """Set difference ``A - B`` contains exactly the versions in ``A`` but
    not in ``B``.

    The solver computes a package's effective range as ``positive -
    negative`` instead of ``positive & ~negative`` so an exclusion can
    never grant pre-release admission.  On the policy-free ``Range[int]``
    the two are identical sets, which these laws pin down.
    """

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_difference_is_intersection_with_complement(
        self, left: Range[int], right: Range[int]
    ) -> None:
        """``v in (A - B)`` iff ``v in A and v not in B``."""
        difference = left - right
        for version in VERSION_RANGE:
            assert (version in difference) == (version in left and version not in right)

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_difference_equals_and_complement(
        self, left: Range[int], right: Range[int]
    ) -> None:
        """``A - B == A & ~B`` (no pre-release policy on ``Range[int]``)."""
        assert (left - right) == (left & ~right)

    @given(left=version_ranges(), right=version_ranges())
    @DEEP_SETTINGS
    def test_difference_disjoint_from_subtrahend(
        self, left: Range[int], right: Range[int]
    ) -> None:
        """``(A - B) ∩ B == ∅``."""
        assert ((left - right) & right).is_empty

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_self_difference_is_empty(self, range_: Range[int]) -> None:
        """``A - A == ∅``."""
        assert (range_ - range_).is_empty

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_difference_identity_with_empty(self, range_: Range[int]) -> None:
        """``A - ∅ == A`` (Empty is the right identity)."""
        assert (range_ - Range.empty()) == range_

    @given(range_=version_ranges())
    @DEEP_SETTINGS
    def test_difference_full_is_empty(self, range_: Range[int]) -> None:
        """``A - Universe == ∅`` (Universe is the absorbing subtrahend)."""
        assert (range_ - Range.full()).is_empty
