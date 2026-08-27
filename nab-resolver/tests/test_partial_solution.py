"""Tests for PartialSolution: assignment tracking and decision levels."""

from __future__ import annotations

import gc
from typing import Any

import pytest

from nab_resolver import partial_solution
from nab_resolver.partial_solution import Assignment, PartialSolution
from nab_resolver.ranges import Range
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

# Incompatibility declares no __eq__, so entries that have to compare equal
# share one cause object.
_CAUSE: Incompatibility[str, int] = Incompatibility(
    [Term("foo", Range.at_least(2))], cause=IncompatibilityCause.NO_VERSIONS
)


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

    def test_backtrack_keeps_untouched_effective_ranges_cached(self) -> None:
        """Only a package whose trail the backjump popped loses its cached range."""
        ps = PartialSolution()
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        # foo sits at level 0; the decision on bar puts bar and baz above it.
        ps.derive("foo", Range.at_least(1), positive=True, cause=inc)
        ps.decide("bar", 1)
        ps.derive("baz", Range.at_least(2), positive=True, cause=inc)

        foo_range = ps.get("foo")
        ps.backtrack(0)

        assert ps._effective_range_cache["foo"] is foo_range

        assert "bar" not in ps._effective_range_cache
        assert "baz" not in ps._effective_range_cache

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


class TestSnapshots:
    """A map handed out keeps reading as it did when it was taken."""

    def test_a_snapshot_does_not_show_a_later_decision(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 3)
        snapshot = ps.decisions()

        ps.decide("bar", 1)

        assert dict(snapshot) == {"foo": 3}
        assert len(snapshot) == 1
        assert "bar" not in snapshot
        assert snapshot.get("bar") is None
        assert snapshot.get("bar", 9) == 9

        assert "foo" in snapshot
        assert snapshot.get("foo") == 3
        assert snapshot["foo"] == 3

    def test_reading_a_package_the_snapshot_never_held(self) -> None:
        ps = PartialSolution()
        snapshot = ps.decisions()

        ps.decide("foo", 3)

        assert dict(snapshot) == {}
        assert len(snapshot) == 0
        with pytest.raises(KeyError):
            snapshot["foo"]

    def test_positive_ranges_keep_the_range_they_were_taken_with(self) -> None:
        ps = PartialSolution()
        inc = Incompatibility([], cause=IncompatibilityCause.ROOT)
        ps.derive("foo", Range.at_least(1), positive=True, cause=inc)
        snapshot = ps.positive_ranges()
        taken_with = snapshot["foo"]

        ps.derive("foo", Range.at_most(5), positive=True, cause=inc)
        ps.derive("foo", Range.at_most(4), positive=True, cause=inc)

        assert snapshot["foo"] is taken_with
        assert snapshot.get("foo") is taken_with
        assert list(snapshot) == ["foo"]
        assert ps.positive_ranges()["foo"] != taken_with

        ps.backtrack(0)

        assert snapshot.get("foo") is taken_with

    def test_backtracking_leaves_the_snapshot_alone(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 3)
        ps.decide("bar", 1)
        snapshot = ps.decisions()

        ps.backtrack(1)
        ps.decide("baz", 7)

        assert dict(snapshot) == {"foo": 3, "bar": 1}
        assert snapshot["bar"] == 1
        assert "baz" not in snapshot

    def test_backtracking_does_not_resurrect_a_package(self) -> None:
        """A package absent when the snapshot was taken stays absent."""
        ps = PartialSolution()
        snapshot = ps.decisions()
        ps.decide("foo", 3)

        ps.backtrack(0)
        ps.decide("foo", 4)

        assert dict(snapshot) == {}
        assert "foo" not in snapshot

    def test_two_outstanding_snapshots_hold_their_own_state(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 3)
        first = ps.decisions()
        ps.decide("bar", 1)
        second = ps.decisions()

        ps.decide("baz", 7)

        assert dict(first) == {"foo": 3}
        assert dict(second) == {"foo": 3, "bar": 1}

    def test_a_frozen_package_keeps_its_place_in_the_order(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 3)
        ps.decide("bar", 1)
        snapshot = ps.decisions()

        assert list(snapshot) == ["foo", "bar"]

        ps.decide("foo", 4)

        assert list(snapshot) == ["foo", "bar"]
        assert list(dict(snapshot)) == ["foo", "bar"]

    def test_a_detached_snapshot_reads_as_of_its_own_moment(self) -> None:
        ps = PartialSolution()
        ps.decide("foo", 3)
        ps.decide("bar", 1)
        snapshot = ps.decisions()

        ps.backtrack(1)
        ps.decide("baz", 7)
        ps.decide("foo", 4)

        assert dict(snapshot) == {"foo": 3, "bar": 1}
        assert list(snapshot) == ["foo", "bar"]
        assert len(snapshot) == 2
        assert snapshot["foo"] == 3
        assert "baz" not in snapshot

    def test_a_released_snapshot_drops_out_of_the_register(self) -> None:
        """Freezing, detaching and taking all step over a dead reference."""
        ps = PartialSolution()
        ps.decide("foo", 3)
        dropped = ps.decisions()
        kept = ps.positive_ranges()
        del dropped
        gc.collect()

        ps.decide("bar", 1)
        ps.backtrack(1)
        latest = ps.decisions()

        assert len(ps._decision_snapshots) == 1
        assert dict(latest) == {"foo": 3}
        assert list(kept) == ["foo"]


class TestRangeOperationMemo:
    """The memo over the range algebra a replayed trail repeats."""

    def _cause(self) -> Incompatibility[str, int]:
        """An incompatibility to stand as the cause of a derivation."""
        return Incompatibility(
            [Term("foo", Range.at_least(5))],
            cause=IncompatibilityCause.NO_VERSIONS,
        )

    def test_a_first_derivation_records_the_constraint_itself(self) -> None:
        """Folding into full() or empty() is skipped, so neither is memoised."""
        ps: PartialSolution[str, int] = PartialSolution()
        cause = self._cause()
        allowed = Range.at_least(1)
        excluded = Range.singleton(4)

        ps.derive("foo", allowed, positive=True, cause=cause)
        ps.derive("bar", excluded, positive=False, cause=cause)

        assert ps.positive_range("foo") is allowed
        assert ps._negative_ranges["bar"] is excluded
        assert ps._range_ops == {}

    def test_a_later_exclusion_unions_through_the_memo(self) -> None:
        ps: PartialSolution[str, int] = PartialSolution()
        cause = self._cause()

        ps.derive("foo", Range.singleton(4), positive=False, cause=cause)
        ps.derive("foo", Range.singleton(7), positive=False, cause=cause)

        excluded = ps._negative_ranges["foo"]
        assert 4 in excluded
        assert 7 in excluded
        assert len(ps._range_ops) == 1

    def test_a_memoised_combine_keeps_its_operands_alive(self) -> None:
        """The memo's id() keys stay valid only while it holds both operands."""
        ps: PartialSolution[str, int] = PartialSolution()
        cause = self._cause()
        current = Range.at_least(1)
        constraint = Range.at_most(9)

        ps.derive("foo", current, positive=True, cause=cause)
        ps.derive("foo", constraint, positive=True, cause=cause)

        assert ps._range_op_operands[-2] is current
        assert ps._range_op_operands[-1] is constraint

    def test_replayed_derivation_reuses_the_range_object(self) -> None:
        """Re-deriving after a backtrack returns the object the first pass built."""
        ps: PartialSolution[str, int] = PartialSolution()
        cause = self._cause()
        constraint = Range.at_most(9)

        ps.derive("foo", Range.at_least(1), positive=True, cause=cause)
        ps.decide("bar", 1)
        ps.derive("foo", constraint, positive=True, cause=cause)
        first = ps.get("foo")

        ps.backtrack(0)
        ps.derive("foo", constraint, positive=True, cause=cause)

        assert ps.get("foo") is first

    def test_overflow_drops_the_memo_and_the_operands_it_holds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing the cap clears both, and the range it answers stays right."""
        monkeypatch.setattr(partial_solution, "RANGE_OP_MEMO_MAX", 1)
        ps: PartialSolution[str, int] = PartialSolution()
        cause = self._cause()

        ps.derive("foo", Range.at_least(1), positive=True, cause=cause)
        ps.derive("foo", Range.at_most(9), positive=True, cause=cause)
        ps.derive("foo", Range.at_most(8), positive=True, cause=cause)

        assert len(ps._range_ops) == 1
        assert len(ps._range_op_operands) == 2

        effective = ps.get("foo")
        assert effective is not None
        assert 5 in effective
        assert 9 not in effective


class TestAssignmentEntries:
    def _entry(self, **overrides: Any) -> Assignment[str, int]:
        """A derivation entry with every field set, ``overrides`` applied."""
        fields: dict[str, Any] = {
            "package": "foo",
            "accumulated_range": Range.at_least(2),
            "decision_level": 1,
            "is_decision": False,
            "trail_index": 4,
            "version": 7,
            "cause": _CAUSE,
            "positive": True,
            "cum_positive": Range.at_least(2),
            "cum_negative": Range.full(),
        }
        return Assignment(**{**fields, **overrides})

    def test_the_optional_fields_default(self) -> None:
        entry = Assignment("foo", Range.at_least(2), 1, is_decision=True)

        assert (entry.trail_index, entry.version, entry.cause) == (0, None, None)
        assert entry.positive
        assert (entry.cum_positive, entry.cum_negative) == (None, None)

    def test_entries_carry_no_instance_dict(self) -> None:
        """One lives per trail step, so the layout is the point."""
        with pytest.raises(AttributeError):
            _ = self._entry().__dict__

    def test_equality_covers_every_field_and_declines_other_types(self) -> None:
        """Vary one field at a time, so no field can drop out of __eq__."""
        others: dict[str, Any] = {
            "package": "bar",
            "accumulated_range": Range.at_least(3),
            "decision_level": 2,
            "is_decision": True,
            "trail_index": 5,
            "version": 8,
            "cause": None,
            "positive": False,
            "cum_positive": Range.at_least(3),
            "cum_negative": Range.at_least(4),
        }
        assert tuple(sorted(others)) == Assignment.__slots__

        entry = self._entry()

        assert entry == self._entry()
        for name, value in others.items():
            assert entry != self._entry(**{name: value}), name

        assert entry.__eq__("foo") is NotImplemented

    def test_a_mutable_trail_entry_is_unhashable(self) -> None:
        assert Assignment.__hash__ is None

    def test_repr_names_the_class_and_every_field_in_declaration_order(self) -> None:
        """A property test formats an entry into its failure message."""
        entry = Assignment(
            "foo", Range.at_least(2), 1, is_decision=False, trail_index=4
        )

        assert repr(entry) == (
            "Assignment(package='foo',"
            " accumulated_range=Range(((2, True, +inf, False),)), decision_level=1,"
            " is_decision=False, trail_index=4, version=None, cause=None,"
            " positive=True, cum_positive=None, cum_negative=None)"
        )

    def test_pattern_matching_reads_every_field_positionally(self) -> None:
        """Ten sub-patterns, in declaration order rather than slot order."""
        match self._entry():
            case Assignment(
                package,
                accumulated_range,
                decision_level,
                is_decision,
                trail_index,
                version,
                cause,
                positive,
                cum_positive,
                cum_negative,
            ):
                assert (package, accumulated_range, decision_level) == (
                    "foo",
                    Range.at_least(2),
                    1,
                )
                assert (is_decision, trail_index, version, cause) == (
                    False,
                    4,
                    7,
                    _CAUSE,
                )
                assert (positive, cum_positive, cum_negative) == (
                    True,
                    Range.at_least(2),
                    Range.full(),
                )
