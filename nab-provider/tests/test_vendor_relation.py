"""Tests for the vendored patch's ``VersionRange.relation``.

``relation`` answers subset and disjointness in one interval walk, so a spec
grid checks it against both the separate predicates and the intersection
form, alongside hand-written expectations.
"""

from __future__ import annotations

import itertools

import pytest

from nab_provider._vendor.packaging.ranges import RangeRelation, VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet

# Single and double bounds, exclusions, wildcards, an epoch, a post, a local,
# a pre-release naming clause, and the two ``===`` shapes.
SPECS = [
    "",
    ">=1.0",
    ">1.0",
    "<2.0",
    "<=2.0",
    ">=1.0,<2.0",
    ">1.0,<=2.0",
    ">=1.5,<1.8",
    ">=2.0,<1.0",
    "==1.5",
    "!=1.5",
    "==1.*",
    "!=1.*",
    "~=1.4",
    ">=1.0.post1",
    "==1.0+local",
    ">=1!0.5",
    ">=1.0a1",
    "===1.5",
    "===frobnicate",
]


def _range(spec: str) -> VersionRange:
    return SpecifierSet(spec).to_range()


class TestRelation:
    def test_containment(self) -> None:
        assert (
            _range(">=1.5,<1.8").relation(_range(">=1.0,<2.0")) is RangeRelation.SUBSET
        )

    def test_separation(self) -> None:
        assert _range(">=1.0,<2.0").relation(_range(">=5.0")) is RangeRelation.DISJOINT

    def test_overlap(self) -> None:
        assert (
            _range(">=1.0,<2.0").relation(_range(">=1.5,<3.0"))
            is RangeRelation.OVERLAPPING
        )

    def test_empty_self_is_subset_and_disjoint(self) -> None:
        assert VersionRange.empty().relation(_range(">=1.0")) is RangeRelation.EMPTY

    def test_identity_non_empty(self) -> None:
        r = _range(">=1.0,<2.0")
        assert r.relation(r) is RangeRelation.SUBSET

    def test_identity_empty(self) -> None:
        empty = VersionRange.empty()
        assert empty.relation(empty) is RangeRelation.EMPTY

    def test_structurally_equal_ranges(self) -> None:
        assert (
            _range(">=1.0,<2.0").relation(_range(">=1.0,<2.0")) is RangeRelation.SUBSET
        )

    def test_structurally_equal_empty_ranges(self) -> None:
        assert (
            _range(">=2.0,<1.0").relation(_range(">=3.0,<1.0")) is RangeRelation.EMPTY
        )

    def test_multi_interval_subset_of_full(self) -> None:
        assert _range("!=1.5").relation(VersionRange.full()) is RangeRelation.SUBSET

    def test_full_is_not_subset_of_punctured(self) -> None:
        assert (
            VersionRange.full().relation(_range("!=1.5")) is RangeRelation.OVERLAPPING
        )

    def test_interval_spanning_a_gap_is_not_a_subset(self) -> None:
        # >=1.0,<=3.0 covers 2.0, which !=2.0 removes, so the left interval
        # spans both right intervals rather than sitting inside either.
        assert (
            _range(">=1.0,<=3.0").relation(_range("!=2.0")) is RangeRelation.OVERLAPPING
        )

    def test_touching_intervals_stay_disjoint(self) -> None:
        assert _range("<1.0").relation(_range(">=1.0")) is RangeRelation.DISJOINT

    def test_differently_spelled_equal_ranges(self) -> None:
        assert (
            _range(">=1.0,<2.0").relation(_range(">=1.0,<2.0,<3.0"))
            is RangeRelation.SUBSET
        )

    def test_policy_mismatch_raises(self) -> None:
        configured = SpecifierSet(">=1.0", prereleases=True).to_range()
        with pytest.raises(ValueError, match="pre-release"):
            configured.relation(_range(">=1.0"))

    def test_literal_range_defers_to_the_algebra(self) -> None:
        literal = _range("===1.5")
        outer = _range(">=1.0,<2.0")
        assert literal.relation(outer) is RangeRelation(
            (literal.is_subset(outer), literal.is_disjoint(outer))
        )

    def test_prerelease_excluding_policy_defers_to_the_algebra(self) -> None:
        strict = SpecifierSet(">=1.0", prereleases=False).to_range()
        other = SpecifierSet(">=1.0,<2.0", prereleases=False).to_range()
        assert strict.relation(other) is RangeRelation(
            (strict.is_subset(other), strict.is_disjoint(other))
        )

    @pytest.mark.parametrize(("left", "right"), list(itertools.product(SPECS, SPECS)))
    def test_matches_the_intersection_form(self, left: str, right: str) -> None:
        a, b = _range(left), _range(right)
        assert a.relation(b) is RangeRelation((a.is_subset(b), (a & b).is_empty))

    @pytest.mark.parametrize(("left", "right"), list(itertools.product(SPECS, SPECS)))
    def test_matches_the_separate_predicates(self, left: str, right: str) -> None:
        a, b = _range(left), _range(right)
        assert a.relation(b) is RangeRelation((a.is_subset(b), a.is_disjoint(b)))
