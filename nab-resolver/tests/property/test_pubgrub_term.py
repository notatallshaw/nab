"""Property tests verifying ``Term`` matches the PubGrub specification.

PubGrub is the dependency-resolution algorithm by Natalie Weizenbaum,
originally implemented in Dart's `pub`_.  Each section of this file
quotes a paragraph of `solver.md`_'s "Definitions / Term" section
verbatim and adds a property test for the invariant that paragraph
states.  Identifiers in the quotations are typeset with backticks
matching the original document.

.. _pub: https://github.com/dart-lang/pub
.. _solver.md: https://github.com/dart-lang/pub/blob/master/doc/solver.md#term
"""

# ruff: noqa: RUF002, RUF003
# RUF002 / RUF003: spec quotations preserve set-theoretic operators
#         (∩, ∪, ⊆, ∅, ¬, ∧, ∨) verbatim.
# E501: long quoted lines are reproduced as written.

from __future__ import annotations

import pytest
from hypothesis import given

from nab_resolver.ranges import Range
from nab_resolver.report import union_terms
from nab_resolver.types import Term

from .strategies import (
    DEEP_SETTINGS,
    PROPERTY_SETTINGS,
    VERSION_RANGE,
    non_empty_ranges,
    non_empty_terms,
    terms,
    version_ranges,
)

pytestmark = pytest.mark.property


def _term_allows(term: Term[str, int], version: int) -> bool:
    """Return ``True`` if ``term`` "allows" ``version``.

    Positive terms allow versions in their constraint; negative terms
    allow versions outside their constraint.
    """
    if term.is_positive():
        return version in term.constraint
    return version not in term.constraint


class TestQuoteTermAsStatement:
    """solver.md § Term, paragraph 1:

    > "The fundamental unit on which Pubgrub operates is a ``Term``,
    > which represents a statement about a package that may be true or
    > false for a given selection of package versions. For example,
    > ``foo ^1.0.0`` is a term that's true if ``foo 1.2.3`` is selected
    > and false if ``foo 2.3.4`` is selected. Conversely, ``not foo
    > ^1.0.0`` is false if ``foo 1.2.3`` is selected and true if ``foo
    > 2.3.4`` is selected or if no version of foo is selected at all."

    The invariant: for every concrete version ``v`` and every range
    ``R``, ``v`` satisfies the positive term ``R`` iff it does not
    satisfy the negative term ``not R``.
    """

    @given(constraint=version_ranges())
    @DEEP_SETTINGS
    def test_positive_negative_polarities_disagree_pointwise(
        self, constraint: Range[int]
    ) -> None:
        """``v in R`` iff ``v not in (not R)`` for every version ``v``."""
        positive = Term("pkg", constraint, positive=True)
        negative = Term("pkg", constraint, positive=False)
        for version in VERSION_RANGE:
            assert _term_allows(positive, version) is not _term_allows(
                negative, version
            )


class TestQuoteTermsDenoteSets:
    """solver.md § Term, paragraph 4:

    > "Terms can be viewed as denoting sets of allowed versions, with
    > negative terms denoting the complement of the corresponding
    > positive term. Set relations and operations can be defined
    > accordingly."

    Three invariants follow from "negative terms denote the
    complement":

    1. Negation flips polarity.
    2. Negation preserves the constraint and package identity.
    3. Double negation is the identity (Boolean lattice law).

    Reference:
    https://github.com/dart-lang/pub/blob/master/doc/solver.md#term
    """

    @given(term=terms())
    @PROPERTY_SETTINGS
    def test_negation_flips_polarity(self, term: Term[str, int]) -> None:
        """``polarity(not T)`` differs from ``polarity(T)``."""
        assert term.negate().is_positive() != term.is_positive()

    @given(term=terms())
    @PROPERTY_SETTINGS
    def test_negation_preserves_package(self, term: Term[str, int]) -> None:
        """``package(not T) == package(T)`` and ``constraint(not T) == constraint(T)``."""
        negated = term.negate()
        assert negated.package == term.package
        assert negated.constraint == term.constraint

    @given(term=terms())
    @PROPERTY_SETTINGS
    def test_double_negation_polarity(self, term: Term[str, int]) -> None:
        """``polarity(not not T) == polarity(T)``."""
        assert term.negate().negate().is_positive() == term.is_positive()

    @given(term=terms())
    @PROPERTY_SETTINGS
    def test_double_negation_constraint(self, term: Term[str, int]) -> None:
        """``constraint(not not T) == constraint(T)``."""
        assert term.negate().negate().constraint == term.constraint


class TestQuoteSatisfaction:
    """solver.md § Term, paragraphs 2 and 6:

    > "We say that a set of terms ``S`` 'satisfies' a term ``t`` if
    > ``t`` must be true whenever every term in ``S`` is true.
    > Conversely, ``S`` 'contradicts' ``t`` if ``t`` must be false
    > whenever every term in ``S`` is true."

    > "Given a term ``t`` and a set of terms ``S``, we have the
    > following identities:
    >
    > * ``S`` satisfies ``t`` if and only if ``⋂S ⊆ t``.
    > * ``S`` contradicts ``t`` if and only if ``⋂S`` is disjoint with
    >   ``t``."

    Translated to ``Range`` operations on a single-element set:

    * Positive term satisfied by ``S`` iff ``S ⊆ constraint``.
    * Negative term satisfied by ``S`` iff ``S ∩ constraint == ∅``.
    * Positive term contradicted by ``S`` iff ``S ∩ constraint == ∅``.
    * Negative term contradicted by ``S`` iff ``S ⊆ constraint``.

    Reference:
    https://github.com/dart-lang/pub/blob/master/doc/solver.md#term
    """

    @given(constraint=version_ranges(), assignment=non_empty_ranges())
    @DEEP_SETTINGS
    def test_positive_satisfies_iff_subset(
        self, constraint: Range[int], assignment: Range[int]
    ) -> None:
        """Positive term: ``satisfies(S)`` iff ``S ⊆ constraint``."""
        term = Term("pkg", constraint, positive=True)
        intersection = assignment & constraint
        expected = intersection == assignment
        assert term.satisfies(assignment) == expected

    @given(constraint=version_ranges(), assignment=non_empty_ranges())
    @DEEP_SETTINGS
    def test_negative_satisfies_iff_disjoint(
        self, constraint: Range[int], assignment: Range[int]
    ) -> None:
        """Negative term: ``satisfies(S)`` iff ``S ∩ constraint == ∅``."""
        term = Term("pkg", constraint, positive=False)
        intersection = assignment & constraint
        expected = intersection.is_empty
        assert term.satisfies(assignment) == expected


class TestQuoteTermIntersection:
    """solver.md § Term, paragraph 5 (set-operation examples):

    > "* ``foo ^1.0.0 ∪ foo ^2.0.0`` is ``foo >=1.0.0 <3.0.0``.
    > * ``foo >=1.0.0 ∩ not foo >=2.0.0`` is ``foo ^1.0.0``.
    > * ``foo ^1.0.0 \\ foo ^1.5.0`` is ``foo >=1.0.0 <1.5.0``."

    The intersection of two terms is itself a term whose denoted set
    equals the intersection of the inputs' denoted sets.  The
    implementation has three cases: positive ∩ positive uses
    ``Range.__and__``, negative ∩ negative uses De Morgan, mixed
    polarity uses set difference.

    Reference:
    https://github.com/dart-lang/pub/blob/master/doc/solver.md#term
    """

    @given(left=non_empty_terms(), right=non_empty_terms())
    @DEEP_SETTINGS
    def test_intersection_is_subset_of_both(
        self, left: Term[str, int], right: Term[str, int]
    ) -> None:
        """If ``v`` is allowed by ``T1 ∩ T2`` then ``T1`` and ``T2`` allow ``v``."""
        result = left.intersect(right)
        assert result is not None
        for version in VERSION_RANGE:
            if _term_allows(result, version):
                assert _term_allows(left, version)
                assert _term_allows(right, version)

    @given(left=terms(), right=terms())
    @DEEP_SETTINGS
    def test_intersection_commutative(
        self, left: Term[str, int], right: Term[str, int]
    ) -> None:
        """``T1 ∩ T2`` and ``T2 ∩ T1`` agree pointwise."""
        ab = left.intersect(right)
        ba = right.intersect(left)
        assert ab is not None
        assert ba is not None
        for version in VERSION_RANGE:
            assert _term_allows(ab, version) == _term_allows(ba, version)

    @given(term=terms())
    @PROPERTY_SETTINGS
    def test_intersection_with_self(self, term: Term[str, int]) -> None:
        """``T ∩ T`` allows the same versions as ``T``."""
        result = term.intersect(term)
        assert result is not None
        for version in VERSION_RANGE:
            assert _term_allows(result, version) == _term_allows(term, version)

    @given(left=non_empty_terms(), right=non_empty_terms())
    @DEEP_SETTINGS
    def test_intersection_polarity(
        self, left: Term[str, int], right: Term[str, int]
    ) -> None:
        """If either input is positive the intersection is positive.

        Negative ∩ negative reduces to ``not(A ∪ B)`` which is the
        only case yielding a negative result.  This invariant is
        important for downstream callers that assume any positive
        derivation eventually surfaces a positive term.
        """
        result = left.intersect(right)
        assert result is not None
        if left.is_positive() or right.is_positive():
            assert result.is_positive()
        else:
            assert not result.is_positive()

    @given(constraint=version_ranges())
    @DEEP_SETTINGS
    def test_complementary_polarities_yield_empty_positive(
        self, constraint: Range[int]
    ) -> None:
        """``positive(R) ∩ negative(R)`` produces the empty positive term.

        A version cannot simultaneously be inside ``R`` and outside
        ``R``: the contradiction must surface as the empty positive
        term, not as ``None``.  ``None`` is reserved for the
        package-mismatch case; collapsing the contradiction to
        ``None`` would hide a derivation that PubGrub uses to learn
        the root incompatibility.
        """
        positive = Term("pkg", constraint, positive=True)
        negative = Term("pkg", constraint, positive=False)
        result = positive.intersect(negative)
        assert result is not None
        assert result.is_positive()
        assert result.constraint.is_empty


class TestRangeContainsSingleton:
    """``Range.contains(v)`` is the pointwise contract used by every
    other property in this file.

    Stated formally: ``v in R`` iff ``R ∩ singleton(v)`` is non-empty.
    The implementation uses interval boundary checks; the property
    test catches any disagreement between the two formulations.
    """

    @given(range_=non_empty_ranges(), version=...)
    @PROPERTY_SETTINGS
    def test_contains_iff_intersection_with_singleton_nonempty(
        self, range_: Range[int], version: int
    ) -> None:
        """``v in R`` iff ``R ∩ singleton(v)`` is non-empty."""
        assert (version in range_) == (not (range_ & Range.singleton(version)).is_empty)


class TestQuoteResolutionIdentity:
    """solver.md § Conflict Resolution, paragraph 4 (resolution rule):

    > "given any incompatibilities ``{t1, q}`` and ``{t2, r}``, we
    > can derive ``{q, r, t1 ∪ t2}``, since either ``t1`` or ``t2``
    > is true in every solution in which ``t1 ∪ t2`` is true. This
    > reduces to ``{q, r}`` in any case where ``not t2 ⊆ t1`` (that
    > is, where ``not t2`` satisfies ``t1``), including the case
    > above where ``t1 = t`` and ``t2 = not t``."

    The PubGrub conflict-resolution loop merges two incompatibilities
    by computing the union of their term sets for each shared
    package.  The implementation, :func:`union_terms`, has to satisfy
    this identity for arbitrary polarities.

    Reference:
    https://github.com/dart-lang/pub/blob/master/doc/solver.md#conflict-resolution
    """

    @given(left=non_empty_terms(), right=non_empty_terms())
    @DEEP_SETTINGS
    def test_union_terms_covers_both(
        self, left: Term[str, int], right: Term[str, int]
    ) -> None:
        """If ``v`` is allowed by ``T1`` or ``T2`` then ``T1 ∪ T2`` allows ``v``."""
        result = union_terms(left, right)
        if result is None:
            return
        for version in VERSION_RANGE:
            if _term_allows(left, version) or _term_allows(right, version):
                assert _term_allows(result, version)

    @given(left=non_empty_terms(), right=non_empty_terms())
    @DEEP_SETTINGS
    def test_union_terms_commutative(
        self, left: Term[str, int], right: Term[str, int]
    ) -> None:
        """``T1 ∪ T2`` and ``T2 ∪ T1`` agree pointwise."""
        ab = union_terms(left, right)
        ba = union_terms(right, left)
        if ab is None:
            assert ba is None
            return
        assert ba is not None
        for version in VERSION_RANGE:
            assert _term_allows(ab, version) == _term_allows(ba, version)

    @given(left=non_empty_ranges(), right=non_empty_ranges())
    @DEEP_SETTINGS
    def test_resolution_identity_positive_positive(
        self, left: Range[int], right: Range[int]
    ) -> None:
        """``positive(A) ∪ positive(B)`` agrees with ``A ∪ B``.

        The result is kept even when ``A ∪ B`` is the full range: a
        positive term still requires the package to be selected, so it
        is never a tautology and never reduces out of the resolvent.
        """
        term_left = Term("pkg", left, positive=True)
        term_right = Term("pkg", right, positive=True)
        result = union_terms(term_left, term_right)
        range_union = left | right
        assert result is not None
        assert result.is_positive()
        for version in VERSION_RANGE:
            assert (version in result.constraint) == (version in range_union)

    @given(left=non_empty_ranges(), right=non_empty_ranges())
    @DEEP_SETTINGS
    def test_resolution_identity_negative_negative(
        self, left: Range[int], right: Range[int]
    ) -> None:
        """``negative(A) ∪ negative(B) == negative(A ∩ B)`` (De Morgan)."""
        term_left = Term("pkg", left, positive=False)
        term_right = Term("pkg", right, positive=False)
        result = union_terms(term_left, term_right)
        range_intersection = left & right
        if range_intersection.is_empty:
            assert result is None
            return
        assert result is not None
        assert not result.is_positive()
        for version in VERSION_RANGE:
            assert (version in result.constraint) == (version in range_intersection)


class TestUnionViaDeMorgan:
    """De Morgan applied to terms: ``T1 ∪ T2 == not(not T1 ∩ not T2)``.

    Used by :func:`union_terms` internally.  The property test verifies
    that both formulations agree pointwise for arbitrary polarities.

    Reference:
    https://en.wikipedia.org/wiki/De_Morgan%27s_laws
    """

    @given(left=non_empty_terms(), right=non_empty_terms())
    @DEEP_SETTINGS
    def test_union_via_intersection_of_negations(
        self, left: Term[str, int], right: Term[str, int]
    ) -> None:
        """``T1 ∪ T2 == not(not T1 ∩ not T2)`` for every version pointwise."""
        union = union_terms(left, right)
        de_morgan = left.negate().intersect(right.negate())
        assert de_morgan is not None
        for version in VERSION_RANGE:
            union_allows = True if union is None else _term_allows(union, version)
            de_morgan_allows = not _term_allows(de_morgan, version)
            assert union_allows == de_morgan_allows
