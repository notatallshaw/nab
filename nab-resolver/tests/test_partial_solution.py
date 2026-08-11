"""Tests for PartialSolution: assignment tracking and decision levels."""

from __future__ import annotations

from nab_resolver.partial_solution import PartialSolution
from nab_resolver.ranges import Range
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term


class TestAssignments:
    def test_empty_solution(self) -> None:
        ps = PartialSolution()
        assert ps.decision_level == 0
        assert ps.get("foo") is None

    def test_trail_length_counts_assignments(self) -> None:
        ps = PartialSolution()
        inc = Incompatibility(
            [Term("bar", Range.at_least(5))],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        assert ps.trail_length == 0

        ps.decide("foo", 5)
        ps.derive("bar", Range.at_least(5), positive=False, cause=inc)
        assert ps.trail_length == 2

        ps.backtrack(0)
        assert ps.trail_length == 0

    def test_decide(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 5)
        assert ps.decision_level == 1
        r = ps.get("foo")
        assert r is not None
        assert 5 in r

    def test_derive(self) -> None:
        ps = PartialSolution()
        inc = Incompatibility(
            [Term("foo", Range.at_least(5))],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        # Derive: foo must NOT be >= 5
        ps.derive("foo", Range.at_least(5), positive=False, cause=inc)
        r = ps.get("foo")
        assert r is not None
        assert 3 in r  # less_than(5) contains 3
        assert 5 not in r

    def test_positive_derive_on_decided_does_not_add_to_undecided(self) -> None:
        """A positive derivation on an already-decided package keeps it
        out of ``_undecided``; the package is already pinned.
        """
        ps = PartialSolution()
        ps.decide("foo", 5)
        inc = Incompatibility(
            [Term("foo", Range.at_least(3))],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        ps.derive("foo", Range.at_least(3), positive=True, cause=inc)
        assert "foo" not in ps._undecided
        assert "foo" in ps._decided_versions

    def test_multiple_decisions_increment_level(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 3)
        assert ps.decision_level == 1
        ps.decide("bar", 2)
        assert ps.decision_level == 2

    def test_backtrack(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 3)
        ps.decide("bar", 2)
        ps.backtrack(1)
        assert ps.decision_level == 1
        assert ps.get("bar") is None
        assert ps.get("foo") is not None

    def test_backtrack_to_zero(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 3)
        ps.derive(
            "bar",
            Range.at_least(2),
            positive=True,
            cause=Incompatibility(
                [],
                cause=IncompatibilityCause.ROOT,
            ),
        )
        ps.backtrack(0)
        assert ps.decision_level == 0
        assert ps.get("foo") is None
        assert ps.get("bar") is None

    def test_satisfier_returns_assignment(self) -> None:
        """satisfier(term) returns the earliest assignment that causes
        the term to be satisfied."""
        ps = PartialSolution()
        ps.decide("foo", 3)
        term = Term("foo", Range.at_least(2))
        s = ps.satisfier(term)
        assert s is not None
        assert s.package == "foo"

    def test_satisfier_returns_none_for_unsatisfied(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 3)
        term = Term("foo", Range.at_least(5))
        s = ps.satisfier(term)
        assert s is None


class TestMultipleDerivations:
    def test_multiple_positive_derivations(self) -> None:
        """Multiple positive derivations narrow the range."""
        ps = PartialSolution()
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        ps.derive("foo", Range.at_least(1), positive=True, cause=inc)
        ps.derive("foo", Range.less_than(5), positive=True, cause=inc)
        r = ps.get("foo")
        assert r is not None
        assert 3 in r
        assert 0 not in r
        assert 5 not in r

    def test_multiple_negative_derivations(self) -> None:
        """Multiple negative derivations expand the excluded range."""
        ps = PartialSolution()
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        ps.derive("foo", Range.singleton(2), positive=False, cause=inc)
        ps.derive("foo", Range.singleton(3), positive=False, cause=inc)
        r = ps.get("foo")
        assert r is not None
        assert 1 in r
        assert 2 not in r
        assert 3 not in r
        assert 4 in r

    def test_backtrack_removes_negative_derivations(self) -> None:
        """Backtracking removes negative derivations too."""
        ps = PartialSolution()
        ps.decide("foo", 5)
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        ps.derive("bar", Range.singleton(3), positive=False, cause=inc)
        ps.backtrack(0)
        assert ps.get("bar") is None

    def test_backtrack_rebuilds_positive_cache(self) -> None:
        """Backtracking correctly rebuilds the positive range cache."""
        ps = PartialSolution()
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        # Level 0: two positive derivations that survive backtracking
        ps.derive("foo", Range.at_least(1), positive=True, cause=inc)
        ps.derive("foo", Range.less_than(10), positive=True, cause=inc)
        # Level 1: decision that will be removed
        ps.decide("bar", 1)
        # Backtrack to 0: _rebuild_caches must merge the two positive derivations
        ps.backtrack(0)
        r = ps.get("foo")
        assert r is not None
        assert 3 in r  # [1, 10) contains 3
        assert 0 not in r  # below 1
        assert 10 not in r  # at or above 10

    def test_backtrack_rebuilds_negative_cache(self) -> None:
        """Backtracking correctly rebuilds the negative range cache."""
        ps = PartialSolution()
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        # Level 0: negative derivation survives
        ps.derive("foo", Range.singleton(5), positive=False, cause=inc)
        # Level 1: decision + more derivations
        ps.decide("bar", 1)
        ps.derive("foo", Range.singleton(3), positive=False, cause=inc)
        # Backtrack to 0: the level-1 negative derivation is removed
        ps.backtrack(0)
        r = ps.get("foo")
        assert r is not None
        assert 5 not in r  # still excluded from level 0
        assert 3 in r  # no longer excluded (was level 1)

    def test_backtrack_keeps_decision_with_later_positive_derivation(self) -> None:
        """A surviving decision stays decided even when a positive
        derivation on the same package follows it in the trail."""
        ps = PartialSolution()
        ps.decide("foo", 3)
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        ps.derive("foo", Range.between(1, 10), positive=True, cause=inc)
        # Removes nothing: every assignment is at the current level.
        ps.backtrack(ps.decision_level)
        assert ps.decisions() == {"foo": 3}
        assert ps.undecided_packages() == set()

    def test_satisfier_with_multiple_positive_derivations(self) -> None:
        """satisfier walks through multiple positive derivations."""
        ps = PartialSolution()
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        ps.derive("foo", Range.at_least(1), positive=True, cause=inc)
        ps.derive("foo", Range.less_than(10), positive=True, cause=inc)
        # foo is now [1, 10). The satisfier for "foo >= 5" should be
        # the first derivation that makes the cumulative range satisfy.
        term = Term("foo", Range.between(1, 10))
        s = ps.satisfier(term)
        assert s is not None

    def test_satisfier_with_multiple_negative_derivations(self) -> None:
        """satisfier accumulates multiple negative derivations."""
        ps = PartialSolution()
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        ps.derive("foo", Range.singleton(5), positive=False, cause=inc)
        ps.derive("foo", Range.singleton(3), positive=False, cause=inc)
        # foo is "anything except 5 and 3"
        # A negative term for both 5 AND 3 is only satisfied after
        # both derivations are accumulated.
        combined_range = Range.singleton(5) | Range.singleton(3)
        term = Term("foo", combined_range, positive=False)
        s = ps.satisfier(term)
        assert s is not None
        # The satisfier should be the second derivation (the one that
        # completes the negative range)
        assert s is ps.assignments_for("foo")[-1]

    def test_satisfier_with_negative_derivation(self) -> None:
        """satisfier handles negative derivations correctly."""
        ps = PartialSolution()
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        ps.derive("foo", Range.singleton(5), positive=False, cause=inc)
        # foo is now "anything except 5"
        term = Term("foo", Range.singleton(5), positive=False)
        s = ps.satisfier(term)
        assert s is not None


def _linear_satisfier(ps: PartialSolution, term: Term) -> object:
    """Reference linear scan, independent of the binary-search satisfier."""
    cum_pos = cum_neg = None
    is_positive = term.is_positive()
    for a in ps._assignments_by_package.get(term.package, ()):
        if a.is_decision or a.positive:
            cum_pos = a.accumulated_range
        else:
            cum_neg = a.accumulated_range
        if cum_pos is not None:
            eff = cum_pos if cum_neg is None else cum_pos & ~cum_neg
        else:
            assert cum_neg is not None
            eff = ~cum_neg
        if (not is_positive or cum_pos is not None) and term.satisfies(eff):
            return a
    return None


class TestSatisfierBinarySearch:
    """The earliest-satisfier search and its O(1) effective-range support."""

    def _root(self) -> Incompatibility:
        return Incompatibility([], cause=IncompatibilityCause.ROOT)

    def test_satisfier_unknown_package(self) -> None:
        ps = PartialSolution()
        assert ps.satisfier(Term("missing", Range.full())) is None

    def test_satisfier_positive_only_trail(self) -> None:
        ps = PartialSolution()
        for upper in (100, 50, 20, 10):
            ps.derive("foo", Range.less_than(upper), positive=True, cause=self._root())
        # foo is now [.., 10); "foo < 30" first holds once it narrows under 30.
        s = ps.satisfier(Term("foo", Range.less_than(30)))
        assert s is not None
        assert s is _linear_satisfier(ps, Term("foo", Range.less_than(30)))

    def test_satisfier_negative_only_trail(self) -> None:
        ps = PartialSolution()
        ps.derive("foo", Range.at_least(5), positive=False, cause=self._root())
        ps.derive("foo", Range.at_least(3), positive=False, cause=self._root())
        term = Term("foo", Range.at_least(3), positive=False)
        s = ps.satisfier(term)
        assert s is not None
        assert s is _linear_satisfier(ps, term)

    def test_satisfier_positive_term_negative_only_trail_is_none(self) -> None:
        ps = PartialSolution()
        ps.derive("foo", Range.at_least(5), positive=False, cause=self._root())
        assert ps.satisfier(Term("foo", Range.less_than(5))) is None

    def test_satisfier_mixed_trail(self) -> None:
        ps = PartialSolution()
        ps.derive("foo", Range.at_least(1), positive=True, cause=self._root())
        ps.derive("foo", Range.less_than(100), positive=True, cause=self._root())
        ps.derive("foo", Range.less_than(10), positive=False, cause=self._root())
        term = Term("foo", Range.at_least(10))
        s = ps.satisfier(term)
        assert s is not None
        assert s is _linear_satisfier(ps, term)

    def test_satisfier_matches_linear_over_mixed_trail(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 50)
        ps.backtrack(0)
        for kind, version in [
            (True, 1),
            (False, 5),
            (True, 2),
            (False, 90),
            (True, 3),
            (False, 40),
        ]:
            rng = Range.at_least(version) if kind else Range.greater_than(version)
            ps.derive("foo", rng, positive=kind, cause=self._root())
        for lo in range(0, 95, 7):
            for positive in (True, False):
                term = Term("foo", Range.at_least(lo), positive=positive)
                assert ps.satisfier(term) is _linear_satisfier(ps, term)


class TestDecisionMap:
    def test_decisions(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 3)
        ps.decide("bar", 1)
        d = ps.decisions()
        assert d == {"foo": 3, "bar": 1}

    def test_undecided_packages(self) -> None:
        ps = PartialSolution()
        # Derive a positive range for foo (but don't decide a version)
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        ps.derive("foo", Range.at_least(1), positive=True, cause=inc)
        ps.decide("bar", 1)
        assert "foo" in ps.undecided_packages()
        assert "bar" not in ps.undecided_packages()


class TestContradictionEpoch:
    def test_a_narrowing_that_leaves_versions_holds_the_epoch(self) -> None:
        """Only a rollback un-contradicts a term while ranges keep versions."""
        ps = PartialSolution()
        inc = Incompatibility(
            [Term("bar", Range.at_least(5))],
            cause=IncompatibilityCause.NO_VERSIONS,
        )

        assert ps.contradiction_epoch == 0

        ps.decide("foo", 5)
        ps.derive("bar", Range.at_least(5), positive=False, cause=inc)
        assert ps.contradiction_epoch == 0

        ps.backtrack(0)
        assert ps.contradiction_epoch == 1

    def test_an_emptied_range_advances_the_epoch(self) -> None:
        """An empty range reads as satisfied on either polarity."""
        ps = PartialSolution()
        inc = Incompatibility(
            [Term("bar", Range.singleton(1))],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        ps.derive("bar", Range.singleton(1), positive=True, cause=inc)

        assert ps.contradiction_epoch == 0

        ps.derive("bar", Range.singleton(1), positive=False, cause=inc)

        effective = ps.get("bar")
        assert effective is not None
        assert effective.is_empty
        assert ps.contradiction_epoch == 1
