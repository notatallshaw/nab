"""Tests for the interval-list invariant that ``Range.__init__`` documents.

``Range.__init__`` neither checks nor normalizes, so the invariant is only
worth stating if the promised surface upholds it.  The census here builds a
range from every classmethod and closes the result under every operator, then
checks each one against a restatement of the invariant that does not share a
line with the implementation.

The same pool feeds a differential over the set predicates.  Its oracle is each
predicate's set-algebra definition, written out below rather than called
through ``Range``, so a fault in a helper the predicates share cannot move the
expected answer along with the result.

The rest pins what ``_normalize_intervals`` repairs, which is less than the
invariant asks for, and what ``Range`` does with hand-built lists that break
it.
"""

from __future__ import annotations

import collections
import itertools
from functools import cache

from nab_resolver.ranges import (
    NEGATIVE_INFINITY,
    POSITIVE_INFINITY,
    Interval,
    Range,
    _interval_is_empty,
    _normalize_intervals,
)
from nab_resolver.types import RangeRelation

VERSIONS = (1, 2, 3, 5, 8)
"""Version pool for the generated ranges.  The gaps make touching reachable."""

PROBES = tuple(range(10))
"""Membership probes, wide enough to straddle every bound built from VERSIONS."""

SECOND_ROUND_SAMPLE = 60
"""How many first-round ranges get run through the operators a second time."""

DIFFERENTIAL_POPULATION = 180
"""How many distinct pool ranges the differential draws its pairs from.  Capping
the count rather than striding the whole list is what bounds the work: pairing is
quadratic, and a ``__hash__`` that stopped deduplicating would otherwise take the
pool from a few hundred ranges to nineteen thousand."""

WIDENING_CHANGES_THIS = (
    "This input breaks the Range invariant, so set algebra does not decide it. "
    "A change that makes the walks total over any interval list should update "
    "this assertion rather than be reverted for failing it."
)
"""Failure message for the assertions that pin behaviour on illegal input."""

OUT_OF_CONTRACT_LISTS: tuple[tuple[Interval, ...], ...] = (
    ((5, True, 8, False), (1, True, 3, False)),
    ((1, True, 5, False), (2, True, 8, False)),
    ((1, True, 2, False), (2, True, 3, False)),
    ((3, False, 3, False),),
    ((5, True, 4, True),),
    ((NEGATIVE_INFINITY, True, 3, False),),
    ((1, True, POSITIVE_INFINITY, True),),
    ((8, True, 9, False), (3, False, 3, False), (1, True, 2, True)),
)
"""One list per way the invariant can break: order, overlap, touch, empty,
reversed, either inclusive infinity, and all of them at once."""

AT_NEGATIVE_INFINITY: Interval = (
    NEGATIVE_INFINITY,
    True,
    NEGATIVE_INFINITY,
    False,
)
"""``[-inf, -inf)``, which puts ``-inf`` in the upper slot it is barred from."""

AT_POSITIVE_INFINITY: Interval = (
    POSITIVE_INFINITY,
    True,
    POSITIVE_INFINITY,
    False,
)
"""``[+inf, +inf)``, the same for ``+inf`` and the lower slot."""

STARTS_ABOVE_EVERYTHING: Interval = (POSITIVE_INFINITY, True, 1, True)
"""``[+inf, 1]``: a finite upper bound over a ``+inf`` lower bound."""

ENDS_BELOW_EVERYTHING: Interval = (1, True, NEGATIVE_INFINITY, True)
"""``[1, -inf]``: a ``-inf`` upper bound under a finite lower bound."""


def holds_no_version(interval: Interval) -> bool:
    """Return whether ``interval`` denotes the empty set."""
    lower, lower_inclusive, upper, upper_inclusive = interval
    if lower is NEGATIVE_INFINITY or upper is POSITIVE_INFINITY:
        return False
    if lower > upper:
        return True
    return lower == upper and not (lower_inclusive and upper_inclusive)


def leaves_a_gap_before(left: Interval, right: Interval) -> bool:
    """Return whether ``left`` ends below ``right`` sharing neither a version nor a bound."""
    upper, upper_inclusive = left[2], left[3]
    lower, lower_inclusive = right[0], right[1]
    if upper is POSITIVE_INFINITY or lower is NEGATIVE_INFINITY:
        return False
    if upper == lower:
        return not upper_inclusive and not lower_inclusive
    return bool(upper < lower)


def invariant_violations(intervals: tuple[Interval, ...]) -> list[str]:
    """Return one message per way ``intervals`` breaks the documented invariant."""
    problems: list[str] = []

    for interval in intervals:
        lower, lower_inclusive, upper, upper_inclusive = interval
        if lower is NEGATIVE_INFINITY and lower_inclusive:
            problems.append(f"inclusive -inf: {interval!r}")
        if upper is POSITIVE_INFINITY and upper_inclusive:
            problems.append(f"inclusive +inf: {interval!r}")
        if holds_no_version(interval):
            problems.append(f"empty interval: {interval!r}")

    for left, right in itertools.pairwise(intervals):
        if not leaves_a_gap_before(left, right):
            problems.append(f"{left!r} leaves no gap before {right!r}")

    return problems


def constructed_ranges() -> list[Range[int]]:
    """Return every range the public classmethods build over ``VERSIONS``."""
    ranges = [Range.empty(), Range.full()]
    for version in VERSIONS:
        ranges += [
            Range.singleton(version),
            Range.at_least(version),
            Range.greater_than(version),
            Range.at_most(version),
            Range.less_than(version),
        ]
    ranges += [
        Range.between(lower, upper)
        for lower, upper in itertools.product(VERSIONS, repeat=2)
    ]
    return ranges


def combined(ranges: list[Range[int]]) -> list[Range[int]]:
    """Return every ``~`` of one range and every ``&``, ``|`` and ``-`` of a pair."""
    results = [~range_ for range_ in ranges]
    for left, right in itertools.product(ranges, repeat=2):
        results += [left & right, left | right, left - right]
    return results


@cache
def public_surface_ranges() -> tuple[Range[int], ...]:
    """Return the ranges reachable from the classmethods through two operator rounds.

    The second round runs a stride sample of the first: the full product is
    tens of millions of ranges, and the shapes stop changing well before that.
    """
    first = constructed_ranges()
    second = combined(first)
    sample = second[:: len(second) // SECOND_ROUND_SAMPLE][:SECOND_ROUND_SAMPLE]
    return tuple(first + second + combined(sample))


@cache
def distinct_pool_ranges() -> tuple[Range[int], ...]:
    """Return the pool with duplicates dropped, in the order they were built."""
    return tuple(dict.fromkeys(public_surface_ranges()))


@cache
def differential_pairs() -> tuple[tuple[Range[int], Range[int]], ...]:
    """Return every ordered pair from an even sample of the distinct pool ranges.

    Deduplicating first is what makes the pairs worth running: the raw pool is
    mostly repeats of the empty range and of single intervals, and the walks
    can only part from set algebra on the multi-interval shapes.
    """
    distinct = distinct_pool_ranges()
    stride = max(1, len(distinct) // DIFFERENTIAL_POPULATION)
    sample = distinct[::stride][:DIFFERENTIAL_POPULATION]
    return tuple((left, right) for left in sample for right in sample)


def oracle_difference(left: Range[int], right: Range[int]) -> Range[int]:
    """Return ``left - right`` by set algebra: meet ``left`` with the complement."""
    return left & ~right


def oracle_is_subset(left: Range[int], right: Range[int]) -> bool:
    """Return whether ``left`` is a subset by set algebra: an empty difference."""
    return (left & ~right).is_empty


def oracle_is_disjoint(left: Range[int], right: Range[int]) -> bool:
    """Return whether the two are disjoint by set algebra: an empty meet."""
    return (left & right).is_empty


def oracle_relation(left: Range[int], right: Range[int]) -> RangeRelation:
    """Return the relation from the two oracle predicates, asking each in turn."""
    if oracle_is_subset(left, right):
        if oracle_is_disjoint(left, right):
            return RangeRelation.EMPTY
        return RangeRelation.SUBSET
    if oracle_is_disjoint(left, right):
        return RangeRelation.DISJOINT
    return RangeRelation.OVERLAPPING


class TestPublicSurfaceUpholdsTheInvariant:
    """Nothing reachable from the promised API produces a non-canonical list.

    This is what lets ``__init__`` state a precondition and check nothing.  It
    is also the answer to whether an inclusive infinity bound needs a runtime
    guard: no constructor and no operator emits one.
    """

    def test_the_pool_reaches_the_shapes_the_invariant_is_about(self) -> None:
        """A pool of nothing but empty and single-interval ranges passes vacuously.

        Order, touching and gaps are only expressible from two intervals up, so
        the multi-interval counts are what say the census means anything.  The
        operators build the same range over and over, so the number of
        constructions is far larger and says much less.
        """
        shapes = collections.Counter(
            len(range_._intervals) for range_ in distinct_pool_ranges()
        )

        assert shapes[1] > 50
        assert shapes[2] > 200
        assert shapes[3] > 20

    def test_no_generated_range_breaks_the_invariant(self) -> None:
        """Sorted, non-overlapping, non-touching, non-empty, exclusive at infinity."""
        violations = [
            f"{range_!r}: {problems}"
            for range_ in public_surface_ranges()
            if (problems := invariant_violations(range_._intervals))
        ]
        assert not violations, violations[:5]

    def test_the_checker_can_fail(self) -> None:
        """Each clause of the invariant rejects a list that breaks only that clause."""
        assert invariant_violations(((NEGATIVE_INFINITY, True, 5, False),))
        assert invariant_violations(((1, True, POSITIVE_INFINITY, True),))
        assert invariant_violations(((3, False, 3, False),))
        assert invariant_violations(((5, True, 6, False), (1, True, 2, False)))
        assert invariant_violations(((1, True, 2, False), (2, True, 3, False)))
        assert invariant_violations(((1, True, 4, False), (2, True, 6, False)))


class TestPredicatesMatchSetAlgebra:
    """The one-pass walks answer what the set-algebra formulations answer.

    Every pair comes from the census pool, so every input satisfies the
    invariant the walks require.
    """

    def test_pairs_are_numerous_enough_to_mean_something(self) -> None:
        """A thin pair set, or one of nothing but single intervals, agrees with anything.

        The upper bound is the other half: pairing is quadratic, so it holds the
        module to a bounded run even if the sample stops being deduplicated.
        """
        pairs = differential_pairs()

        assert 30_000 < len(pairs) <= DIFFERENTIAL_POPULATION**2
        assert max(len(left._intervals) for left, _ in pairs) >= 3

    def test_is_subset(self) -> None:
        mismatches = [
            (left, right)
            for left, right in differential_pairs()
            if left.is_subset(right) != oracle_is_subset(left, right)
        ]
        assert not mismatches, mismatches[:3]

    def test_is_disjoint(self) -> None:
        mismatches = [
            (left, right)
            for left, right in differential_pairs()
            if left.is_disjoint(right) != oracle_is_disjoint(left, right)
        ]
        assert not mismatches, mismatches[:3]

    def test_difference(self) -> None:
        mismatches = [
            (left, right)
            for left, right in differential_pairs()
            if (left - right) != oracle_difference(left, right)
        ]
        assert not mismatches, mismatches[:3]

    def test_relation(self) -> None:
        mismatches = [
            (left, right)
            for left, right in differential_pairs()
            if left.relation(right) is not oracle_relation(left, right)
        ]
        assert not mismatches, mismatches[:3]


class TestPredicatesMatchMembership:
    """A version one range holds and the other does not settles the predicate.

    This shares no code with the oracle above, which is built from ``__and__``
    and ``__invert__``; these read only ``__contains__``.
    """

    def test_a_version_only_left_holds_denies_is_subset(self) -> None:
        for left, right in differential_pairs():
            if any(probe in left and probe not in right for probe in PROBES):
                assert not left.is_subset(right)

    def test_a_version_both_hold_denies_is_disjoint(self) -> None:
        for left, right in differential_pairs():
            if any(probe in left and probe in right for probe in PROBES):
                assert not left.is_disjoint(right)

    def test_difference_holds_exactly_the_versions_only_left_holds(self) -> None:
        for left, right in differential_pairs():
            difference = left - right
            for probe in PROBES:
                assert (probe in difference) == (probe in left and probe not in right)


class TestNormalizeIntervals:
    """What the only repairing path repairs, and what it passes through.

    ``__or__`` runs every union through ``_normalize_intervals``, which makes
    it the closest thing to a normalizing entry point.  How far it goes decides
    whether a caller can repair a bad list by unioning it with a good one.
    """

    def test_sorts_by_lower_bound(self) -> None:
        assert _normalize_intervals([(5, True, 6, False), (1, True, 2, False)]) == (
            (1, True, 2, False),
            (5, True, 6, False),
        )

    def test_merges_overlapping_intervals(self) -> None:
        assert _normalize_intervals([(1, True, 4, False), (2, True, 6, False)]) == (
            (1, True, 6, False),
        )

    def test_merges_touching_intervals(self) -> None:
        assert _normalize_intervals([(1, True, 2, False), (2, True, 3, False)]) == (
            (1, True, 3, False),
        )

    def test_keeps_an_empty_interval(self) -> None:
        assert _normalize_intervals([(3, False, 3, False)]) == ((3, False, 3, False),)

    def test_keeps_a_reversed_interval(self) -> None:
        assert _normalize_intervals([(5, True, 4, True)]) == ((5, True, 4, True),)

    def test_a_merge_absorbs_an_empty_interval(self) -> None:
        """Survival is only survival of what no merge reaches."""
        assert _normalize_intervals([(1, True, 5, False), (3, False, 3, False)]) == (
            (1, True, 5, False),
        )

    def test_a_merge_absorbs_a_reversed_interval(self) -> None:
        assert _normalize_intervals([(1, True, 9, False), (5, True, 4, True)]) == (
            (1, True, 9, False),
        )

    def test_keeps_an_inclusive_negative_infinity(self) -> None:
        assert _normalize_intervals([(NEGATIVE_INFINITY, True, 5, False)]) == (
            (NEGATIVE_INFINITY, True, 5, False),
        )

    def test_keeps_a_lone_inclusive_positive_infinity(self) -> None:
        assert _normalize_intervals([(1, True, POSITIVE_INFINITY, True)]) == (
            (1, True, POSITIVE_INFINITY, True),
        )

    def test_merging_two_positive_infinities_drops_the_flag(self) -> None:
        """The merge arm rebuilds the upper bound, which is the one place it is fixed."""
        assert _normalize_intervals(
            [(1, True, POSITIVE_INFINITY, True), (2, True, POSITIVE_INFINITY, True)]
        ) == ((1, True, POSITIVE_INFINITY, False),)

    def test_union_carries_a_hand_built_flag_through(self) -> None:
        """Unioning with a canonical range does not canonicalize the other side."""
        hand_built: Range[int] = Range(((NEGATIVE_INFINITY, True, 5, False),))
        assert (hand_built | Range.full())._intervals == (
            (NEGATIVE_INFINITY, True, POSITIVE_INFINITY, False),
        )


class TestTheEmptinessPredicate:
    """``_interval_is_empty`` and ``holds_no_version`` state the same rule.

    ``invariant_violations`` reads the test-side one, so the two agreeing is
    what lets its verdicts stand for the module's own.
    """

    def test_they_agree_on_every_shape_of_bound(self) -> None:
        """Both infinities, in both slots, against both flags."""
        bounds = (NEGATIVE_INFINITY, 1, 2, POSITIVE_INFINITY)
        combinations = itertools.product(bounds, (True, False), bounds, (True, False))

        for lower, lower_inclusive, upper, upper_inclusive in combinations:
            interval: Interval = (lower, lower_inclusive, upper, upper_inclusive)
            assert _interval_is_empty(
                lower,
                lower_inclusive=lower_inclusive,
                upper=upper,
                upper_inclusive=upper_inclusive,
            ) == holds_no_version(interval), interval


class TestOutOfContractInputs:
    """Hand-built lists that break the invariant, and what ``Range`` does with them.

    None of these is reachable from the classmethods or the operators, which
    the census above pins.  The assertions record where the implementation
    parts from set algebra, so that widening or narrowing the invariant shows
    up as a failing test rather than as a silent behaviour change.
    """

    def test_inclusive_infinity_splits_equality_from_membership(self) -> None:
        """Two lists denoting ``(-inf, 5)`` compare unequal because their flags differ."""
        exclusive: Range[int] = Range(((NEGATIVE_INFINITY, False, 5, False),))
        inclusive: Range[int] = Range(((NEGATIVE_INFINITY, True, 5, False),))

        assert all((probe in exclusive) == (probe in inclusive) for probe in PROBES)

        assert exclusive != inclusive
        assert {exclusive: "value"}.get(inclusive) is None
        assert len({exclusive, inclusive}) == 2

    def test_touching_intervals_compare_unequal_to_the_merged_form(self) -> None:
        """``[1, 2) | [2, 3]`` and ``[1, 3]`` hold the same versions and compare unequal."""
        touching: Range[int] = Range(((1, True, 2, False), (2, True, 3, True)))
        merged: Range[int] = Range(((1, True, 3, True),))

        assert all((probe in touching) == (probe in merged) for probe in PROBES)
        assert touching != merged

    def test_an_empty_interval_makes_a_range_that_is_not_is_empty(self) -> None:
        """``is_empty`` counts intervals, so a list of empty ones reports non-empty."""
        degenerate: Range[int] = Range(((3, False, 3, False),))

        assert all(probe not in degenerate for probe in PROBES)
        assert not degenerate.is_empty
        assert bool(degenerate)

    def test_touching_intervals_defeat_the_subset_walk(self) -> None:
        """``[1, 3]`` sits inside ``[1, 2) | [2, 3]``, and the walk answers False.

        The walk asks one interval of the right side to hold a whole interval
        of the left, which matches coverage only when the right side leaves a
        gap between its own intervals.
        """
        whole: Range[int] = Range(((1, True, 3, True),))
        split: Range[int] = Range(((1, True, 2, False), (2, True, 3, True)))

        assert oracle_is_subset(whole, split)
        assert not whole.is_subset(split), WIDENING_CHANGES_THIS
        assert whole.relation(split) is RangeRelation.OVERLAPPING, WIDENING_CHANGES_THIS

    def test_an_inclusive_infinity_defeats_the_subset_walk(self) -> None:
        """The walk compares a rebuilt tuple against the left interval, flags included."""
        below_one: Range[int] = Range(((NEGATIVE_INFINITY, True, 1, False),))

        assert oracle_is_subset(below_one, Range.full())
        assert not below_one.is_subset(Range.full()), WIDENING_CHANGES_THIS

    def test_an_interval_at_an_infinity_holds_no_version(self) -> None:
        """The scan finds nothing in either misplaced-sentinel interval.

        Nothing sits at or beyond an infinity.  The walks below still carve and
        compare these intervals, which is the asymmetry the rest of these tests
        turn on.
        """
        assert all(probe not in Range((AT_NEGATIVE_INFINITY,)) for probe in PROBES)
        assert all(probe not in Range((AT_POSITIVE_INFINITY,)) for probe in PROBES)

    def test_the_carve_reads_a_misplaced_sentinel_as_an_infinity(self) -> None:
        """``__sub__`` tests both ends for an infinity by identity before comparing.

        Only a sentinel in a slot the invariant bars reaches those tests, and
        each answer below changes if one of them goes.
        """
        assert (Range((AT_NEGATIVE_INFINITY,)) - Range.at_most(1)).is_empty, (
            WIDENING_CHANGES_THIS
        )
        assert (
            Range((AT_NEGATIVE_INFINITY,)) - Range((AT_NEGATIVE_INFINITY,))
        )._intervals == (AT_NEGATIVE_INFINITY,), WIDENING_CHANGES_THIS

        assert (Range.at_least(1) - Range((STARTS_ABOVE_EVERYTHING,)))._intervals == (
            (1, True, POSITIVE_INFINITY, False),
            (1, False, POSITIVE_INFINITY, False),
        ), WIDENING_CHANGES_THIS

        assert (
            Range((AT_POSITIVE_INFINITY,)) - Range((AT_POSITIVE_INFINITY,))
        )._intervals == (AT_POSITIVE_INFINITY,), WIDENING_CHANGES_THIS
        assert (Range((AT_POSITIVE_INFINITY,)) - Range.singleton(1))._intervals == (
            AT_POSITIVE_INFINITY,
        ), WIDENING_CHANGES_THIS

    def test_the_subset_walk_reads_a_misplaced_sentinel_as_an_infinity(self) -> None:
        """``is_subset`` retires a right interval on the same two tests.

        A ``+inf`` upper bound ends below nothing, and nothing ends below a
        ``-inf`` lower bound, whichever slot the sentinel is sitting in.
        """
        assert Range((STARTS_ABOVE_EVERYTHING,)).is_subset(Range.at_least(1)), (
            WIDENING_CHANGES_THIS
        )
        assert not Range.at_most(1).is_subset(
            Range((AT_NEGATIVE_INFINITY, (NEGATIVE_INFINITY, False, 1, True)))
        ), WIDENING_CHANGES_THIS

    def test_relation_reads_a_misplaced_sentinel_as_an_infinity(self) -> None:
        """``relation`` asks the same question in two places, in both directions.

        Its advance loop and its ends-below test each carry both infinity
        tests, so a pair answers one way round and not the other.
        """
        above_one: Range[int] = Range.at_least(1)
        starts_above: Range[int] = Range((STARTS_ABOVE_EVERYTHING,))

        assert starts_above.relation(above_one) is RangeRelation.SUBSET, (
            WIDENING_CHANGES_THIS
        )
        assert above_one.relation(starts_above) is RangeRelation.OVERLAPPING, (
            WIDENING_CHANGES_THIS
        )

        below_one: Range[int] = Range.at_most(1)
        ends_below: Range[int] = Range((ENDS_BELOW_EVERYTHING,))

        assert ends_below.relation(below_one) is RangeRelation.SUBSET, (
            WIDENING_CHANGES_THIS
        )
        assert below_one.relation(ends_below) is RangeRelation.OVERLAPPING, (
            WIDENING_CHANGES_THIS
        )

    def test_the_intersection_reads_a_misplaced_sentinel_as_an_infinity(self) -> None:
        """``__and__`` and ``is_disjoint`` share the emptiness test on the overlap.

        An overlap bounded by a misplaced sentinel is not empty, because that
        test asks for the sentinel by identity before it compares.
        """
        assert (Range((AT_NEGATIVE_INFINITY,)) & Range.at_most(1))._intervals == (
            (NEGATIVE_INFINITY, False, NEGATIVE_INFINITY, False),
        ), WIDENING_CHANGES_THIS
        assert not Range((AT_NEGATIVE_INFINITY,)).is_disjoint(Range.at_most(1)), (
            WIDENING_CHANGES_THIS
        )

        assert (Range((AT_POSITIVE_INFINITY,)) & Range.at_least(1))._intervals == (
            AT_POSITIVE_INFINITY,
        ), WIDENING_CHANGES_THIS
        assert not Range((AT_POSITIVE_INFINITY,)).is_disjoint(Range.at_least(1)), (
            WIDENING_CHANGES_THIS
        )

    def test_a_shared_endpoint_belongs_to_both_sides_only_if_both_include_it(
        self,
    ) -> None:
        """The walks decide that with ``and`` over the two flags.

        Reaching it takes an interval that holds no version, since a canonical
        list leaves a gap wherever two intervals could share an endpoint.
        """
        assert (
            Range.at_most(1) - Range(((1, True, 1, True), (1, True, 1, False)))
        )._intervals == Range.less_than(1)._intervals, WIDENING_CHANGES_THIS
        assert (
            Range(((1, True, 2, True),)) - Range(((2, False, 1, True),))
        )._intervals == ((1, True, 2, True),), WIDENING_CHANGES_THIS

        assert (Range(((1, True, 1, False),)) - Range.singleton(1)).is_empty, (
            WIDENING_CHANGES_THIS
        )
        assert not Range(((1, True, 1, False),)).is_subset(
            Range(((1, True, 1, False),))
        ), WIDENING_CHANGES_THIS

    def test_an_empty_interval_defeats_the_relation_short_circuit(self) -> None:
        """``relation`` reads ``is_empty`` off the interval count, not off the versions."""
        degenerate: Range[int] = Range(((3, False, 3, False),))
        other: Range[int] = Range(((1, True, 5, False),))

        assert oracle_relation(degenerate, other) is RangeRelation.EMPTY
        assert degenerate.relation(other) is RangeRelation.SUBSET, WIDENING_CHANGES_THIS

    def test_a_reversed_interval_splits_relation_from_the_subset_walk(self) -> None:
        """``[2, 1]`` is a subset for ``is_subset`` and disjoint for ``relation``.

        ``relation`` asks whether the left interval ends below the right one
        before rebuilding the intersection.  A reversed interval both ends
        below and rebuilds to itself, so the two walks disagree.
        """
        reversed_bounds: Range[int] = Range(((2, True, 1, True),))
        other: Range[int] = Range(((2, True, 3, False),))

        assert oracle_relation(reversed_bounds, other) is RangeRelation.EMPTY
        assert reversed_bounds.is_subset(other), WIDENING_CHANGES_THIS
        assert reversed_bounds.relation(other) is RangeRelation.DISJOINT, (
            WIDENING_CHANGES_THIS
        )


class TestMembershipDoesNotNeedTheInvariant:
    """``__contains__`` scans every interval, so it defines membership on any list.

    That asymmetry is why the walks take a precondition and the scan does not.
    """

    def test_membership_decomposes_over_intervals(self) -> None:
        """``v in Range(intervals)`` is ``v`` in any one of them, however they are ordered."""
        for intervals in OUT_OF_CONTRACT_LISTS:
            whole: Range[int] = Range(intervals)
            parts: list[Range[int]] = [Range((interval,)) for interval in intervals]
            for probe in PROBES:
                assert (probe in whole) == any(probe in part for part in parts)
