"""Tests for Range - version set with interval operations.

Red-green development: each test is written before the implementation.
We use int as the version type for simplicity.
"""

from __future__ import annotations

from nab_resolver.ranges import Range


class TestRangeConstruction:
    def test_empty_range(self) -> None:
        r = Range.empty()
        assert r.is_empty

    def test_any_range(self) -> None:
        r = Range.full()
        assert not r.is_empty

    def test_exact_version(self) -> None:
        r = Range.singleton(5)
        assert 5 in r
        assert 4 not in r
        assert 6 not in r

    def test_at_least(self) -> None:
        r = Range.at_least(3)
        assert 3 in r
        assert 4 in r
        assert 100 in r
        assert 2 not in r

    def test_less_than(self) -> None:
        r = Range.less_than(3)
        assert 2 in r
        assert 0 in r
        assert 3 not in r
        assert 4 not in r

    def test_between(self) -> None:
        r = Range.between(2, 5)
        assert 2 in r
        assert 3 in r
        assert 4 in r
        assert 5 not in r
        assert 1 not in r

    def test_between_equal_bounds_is_empty(self) -> None:
        r = Range.between(3, 3)
        assert r.is_empty
        assert 3 not in r
        assert r == Range.empty()

    def test_between_inverted_bounds_is_empty(self) -> None:
        r = Range.between(5, 3)
        assert r.is_empty
        assert r == Range.empty()
        assert ~~r == r


class TestRangeOperations:
    def test_intersection_overlapping(self) -> None:
        a = Range.at_least(2)
        b = Range.less_than(5)
        c = a & b
        assert 3 in c
        assert 1 not in c
        assert 5 not in c

    def test_intersection_disjoint(self) -> None:
        a = Range.less_than(2)
        b = Range.at_least(5)
        c = a & b
        assert c.is_empty

    def test_union(self) -> None:
        a = Range.less_than(2)
        b = Range.at_least(5)
        c = a | b
        assert 1 in c
        assert 6 in c
        assert 3 not in c

    def test_complement_of_at_least(self) -> None:
        r = Range.at_least(3)
        c = ~r
        assert 2 in c
        assert 3 not in c

    def test_complement_of_empty_is_any(self) -> None:
        r = Range.empty()
        c = ~r
        assert not c.is_empty
        assert 0 in c
        assert 999 in c

    def test_complement_of_any_is_empty(self) -> None:
        r = Range.full()
        c = ~r
        assert c.is_empty


class TestRangeEquality:
    def test_equal_ranges(self) -> None:
        a = Range.between(2, 5)
        b = Range.between(2, 5)
        assert a == b

    def test_unequal_ranges(self) -> None:
        a = Range.between(2, 5)
        b = Range.between(2, 6)
        assert a != b

    def test_hash_consistent(self) -> None:
        a = Range.between(2, 5)
        b = Range.between(2, 5)
        assert hash(a) == hash(b)
        d = {a: "test"}
        assert d[b] == "test"

    def test_not_equal_to_non_range(self) -> None:
        assert Range.full() != "not a range"


class TestInfinitySentinels:
    """Test the comparison operators on the infinity sentinels."""

    def test_negative_infinity_comparisons(self) -> None:
        from nab_resolver.ranges import NEGATIVE_INFINITY

        assert NEGATIVE_INFINITY < 0
        assert NEGATIVE_INFINITY <= 0
        assert NEGATIVE_INFINITY <= NEGATIVE_INFINITY
        assert not (NEGATIVE_INFINITY > 0)
        assert NEGATIVE_INFINITY >= NEGATIVE_INFINITY
        assert not (NEGATIVE_INFINITY >= 0)
        assert NEGATIVE_INFINITY == NEGATIVE_INFINITY
        assert NEGATIVE_INFINITY != 0
        assert repr(NEGATIVE_INFINITY) == "-inf"

    def test_positive_infinity_comparisons(self) -> None:
        from nab_resolver.ranges import POSITIVE_INFINITY

        assert POSITIVE_INFINITY > 0
        assert POSITIVE_INFINITY >= 0
        assert POSITIVE_INFINITY >= POSITIVE_INFINITY
        assert not (POSITIVE_INFINITY < 0)
        assert POSITIVE_INFINITY <= POSITIVE_INFINITY
        assert not (POSITIVE_INFINITY <= 0)
        assert POSITIVE_INFINITY == POSITIVE_INFINITY
        assert POSITIVE_INFINITY != 0
        assert repr(POSITIVE_INFINITY) == "+inf"

    def test_infinity_hashing(self) -> None:
        from nab_resolver.ranges import NEGATIVE_INFINITY, POSITIVE_INFINITY

        hashable_set = {NEGATIVE_INFINITY, POSITIVE_INFINITY}
        assert len(hashable_set) == 2
        assert hash(NEGATIVE_INFINITY) == hash(NEGATIVE_INFINITY)
        assert hash(POSITIVE_INFINITY) == hash(POSITIVE_INFINITY)

    def test_module_constants_exist(self) -> None:
        from nab_resolver.ranges import NEGATIVE_INFINITY, POSITIVE_INFINITY

        assert isinstance(NEGATIVE_INFINITY, type(NEGATIVE_INFINITY))
        assert isinstance(POSITIVE_INFINITY, type(POSITIVE_INFINITY))


class TestRangeContainment:
    """Test __contains__ edge cases."""

    def test_exact_boundary_inclusive(self) -> None:
        r = Range.between(2, 5)
        assert 2 in r
        assert 5 not in r

    def test_at_most(self) -> None:
        r = Range.at_most(3)
        assert 3 in r
        assert 4 not in r
        assert 0 in r

    def test_greater_than(self) -> None:
        r = Range.greater_than(3)
        assert 3 not in r
        assert 4 in r

    def test_empty_contains_nothing(self) -> None:
        r = Range.empty()
        assert 0 not in r
        assert 999 not in r


class TestRangeUnionEdgeCases:
    def test_union_adjacent_intervals(self) -> None:
        """[1, 3) | [3, 5) should merge to [1, 5)."""
        a = Range.between(1, 3)
        b = Range.between(3, 5)
        c = a | b
        assert 1 in c
        assert 3 in c
        assert 4 in c
        assert 5 not in c

    def test_union_overlapping(self) -> None:
        a = Range.between(1, 4)
        b = Range.between(3, 6)
        c = a | b
        assert 1 in c
        assert 5 in c
        assert 6 not in c

    def test_union_with_empty(self) -> None:
        a = Range.between(1, 5)
        b = Range.empty()
        assert (a | b) == a

    def test_union_two_empties(self) -> None:
        assert (Range.empty() | Range.empty()).is_empty

    def test_union_disjoint(self) -> None:
        a = Range.between(1, 3)
        b = Range.between(5, 7)
        c = a | b
        assert 2 in c
        assert 4 not in c
        assert 6 in c

    def test_union_unbounded_overlap(self) -> None:
        """[1, +inf) | [3, +inf) should merge to [1, +inf)."""
        a = Range.at_least(1)
        b = Range.at_least(3)
        c = a | b
        assert 0 not in c
        assert 1 in c
        assert 100 in c

    def test_union_same_upper_bound(self) -> None:
        """[1, 5) | [3, 5) should merge to [1, 5)."""
        a = Range.between(1, 5)
        b = Range.between(3, 5)
        c = a | b
        assert 1 in c
        assert 4 in c
        assert 5 not in c

    def test_union_first_contains_second(self) -> None:
        """[1, 10) | [3, 5) should be [1, 10)."""
        a = Range.between(1, 10)
        b = Range.between(3, 5)
        c = a | b
        assert c == a

    def test_union_both_unbounded_below(self) -> None:
        """(-inf, 3) | (-inf, 5) should merge to (-inf, 5)."""
        a = Range.less_than(3)
        b = Range.less_than(5)
        c = a | b
        assert 4 in c
        assert 5 not in c

    def test_union_non_adjacent_touching(self) -> None:
        """(1, 3) | (3, 5) should NOT merge (gap at 3)."""
        a = Range(((1, False, 3, False),))  # (1, 3)
        b = Range(((3, False, 5, False),))  # (3, 5)
        c = a | b
        assert 2 in c
        assert 3 not in c  # gap at 3
        assert 4 in c

    def test_union_with_positive_infinity(self) -> None:
        """[1, 5) | [3, +inf) should be [1, +inf)."""
        a = Range.between(1, 5)
        b = Range.at_least(3)
        c = a | b
        assert 0 not in c
        assert 1 in c
        assert 100 in c


class TestRangeComplementEdgeCases:
    def test_complement_of_exact(self) -> None:
        r = Range.singleton(5)
        c = ~r
        assert 4 in c
        assert 5 not in c
        assert 6 in c

    def test_complement_of_between(self) -> None:
        r = Range.between(2, 5)
        c = ~r
        assert 1 in c
        assert 2 not in c
        assert 5 in c
        assert 6 in c

    def test_double_complement(self) -> None:
        r = Range.between(2, 5)
        assert ~~r == r

    def test_complement_of_adjacent_points(self) -> None:
        """Complement of [1,1] | [1,1] (after normalization: [1,1])."""
        r = Range.singleton(1) | Range.singleton(1)
        complement = ~r
        assert 0 in complement
        assert 1 not in complement
        assert 2 in complement

    def test_complement_of_touching_intervals(self) -> None:
        """Complement of [1,2) | [2,3) has no gap at 2."""
        r = Range(((1, True, 2, False), (2, True, 3, False)))
        complement = ~r
        assert 0 in complement
        assert 1 not in complement
        assert 2 not in complement  # 2 is in [2,3)
        assert 3 in complement


class TestRangeIntersectionEdgeCases:
    def test_intersection_with_any(self) -> None:
        a = Range.between(2, 5)
        b = Range.full()
        assert (a & b) == a

    def test_intersection_with_empty(self) -> None:
        a = Range.between(2, 5)
        b = Range.empty()
        assert (a & b).is_empty

    def test_intersection_touching_exclusive(self) -> None:
        """[1, 3) & [3, 5) should be empty (3 excluded from both)."""
        a = Range(((1, True, 3, False),))
        b = Range(((3, True, 5, False),))
        c = a & b
        assert c.is_empty

    def test_and_not_implemented(self) -> None:
        assert Range.full().__and__(object()) is NotImplemented  # type: ignore[arg-type]

    def test_or_not_implemented(self) -> None:
        assert Range.full().__or__(object()) is NotImplemented  # type: ignore[arg-type]


class TestRangeDifference:
    def test_difference_removes_subtrahend(self) -> None:
        a = Range.at_least(1)
        b = Range.at_least(5)
        c = a - b
        assert 1 in c
        assert 4 in c
        assert 5 not in c
        assert c == a & ~b

    def test_difference_with_empty_is_self(self) -> None:
        a = Range.between(2, 5)
        assert (a - Range.empty()) == a

    def test_difference_with_full_is_empty(self) -> None:
        a = Range.between(2, 5)
        assert (a - Range.full()).is_empty

    def test_sub_not_implemented(self) -> None:
        assert Range.full().__sub__(object()) is NotImplemented  # type: ignore[arg-type]


class TestRangeSetRelations:
    def test_is_subset_true(self) -> None:
        assert Range.between(2, 4).is_subset(Range.between(1, 5))

    def test_is_subset_false(self) -> None:
        assert not Range.between(1, 5).is_subset(Range.between(2, 4))

    def test_empty_is_subset_of_any(self) -> None:
        assert Range.empty().is_subset(Range.between(2, 4))

    def test_is_superset_true(self) -> None:
        assert Range.between(1, 5).is_superset(Range.between(2, 4))

    def test_is_superset_false(self) -> None:
        assert not Range.between(2, 4).is_superset(Range.between(1, 5))

    def test_is_disjoint_true(self) -> None:
        assert Range.between(1, 3).is_disjoint(Range.between(3, 5))

    def test_is_disjoint_false(self) -> None:
        assert not Range.between(1, 4).is_disjoint(Range.between(2, 5))


class TestRangeStr:
    def test_any_str(self) -> None:
        assert str(Range.full()) == "*"

    def test_empty_str(self) -> None:
        assert str(Range.empty()) == "<empty>"

    def test_exact_str(self) -> None:
        assert str(Range.singleton(5)) == "5"
