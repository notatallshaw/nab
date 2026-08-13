"""Tests for ``VersionRange.from_bounds`` and ``VersionRange.snap_bounds``.

``from_bounds`` builds a raw version-order interval, so a decided pre-release,
post, or local version stays inside its own open gap even where the
matching specifier would exclude it. ``snap_bounds`` re-anchors a range's
finite bounds onto known versions for display, returning a subset of self
that agrees with self on every given version.
"""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import InvalidVersion, Version

V = Version


def _range(spec: str) -> VersionRange:
    return SpecifierSet(spec).to_range()


class TestFromBounds:
    def test_closed_default(self) -> None:
        r = VersionRange.from_bounds("1.0", "2.0")
        assert V("1.0") in r
        assert V("2.0") in r
        assert V("0.9") not in r
        assert V("2.1") not in r

    def test_half_open_upper(self) -> None:
        r = VersionRange.from_bounds("1.0", "2.0", include_upper=False)
        assert V("1.0") in r
        assert V("2.0") not in r

    def test_half_open_lower(self) -> None:
        r = VersionRange.from_bounds("1.0", "2.0", include_lower=False)
        assert V("1.0") not in r
        assert V("2.0") in r

    def test_open_both(self) -> None:
        r = VersionRange.from_bounds(
            "1.0", "2.0", include_lower=False, include_upper=False
        )
        assert V("1.0") not in r
        assert V("1.5") in r
        assert V("2.0") not in r

    def test_unbounded_lower(self) -> None:
        r = VersionRange.from_bounds(upper="2.0")
        assert V("0.1") in r
        assert V("2.0") in r
        assert V("2.1") not in r

    def test_unbounded_upper(self) -> None:
        r = VersionRange.from_bounds("1.0")
        assert V("1.0") in r
        assert V("99") in r
        assert V("0.9") not in r

    def test_unbounded_both_equals_full_no_arbitrary(self) -> None:
        assert VersionRange.from_bounds() == VersionRange.full(admit_arbitrary=False)
        assert "garbage" not in VersionRange.from_bounds()

    def test_inverted_is_empty(self) -> None:
        assert VersionRange.from_bounds("2.0", "1.0").is_empty

    def test_equal_bounds_closed_is_singleton(self) -> None:
        assert VersionRange.from_bounds("1.5", "1.5") == VersionRange.singleton("1.5")

    def test_equal_bounds_exclusive_upper_is_empty(self) -> None:
        assert VersionRange.from_bounds("1.5", "1.5", include_upper=False).is_empty

    def test_equal_bounds_exclusive_lower_is_empty(self) -> None:
        assert VersionRange.from_bounds("1.5", "1.5", include_lower=False).is_empty

    def test_accepts_version_objects(self) -> None:
        r = VersionRange.from_bounds(V("1.0"), V("2.0"))
        assert V("1.5") in r

    def test_invalid_lower(self) -> None:
        with pytest.raises(InvalidVersion):
            VersionRange.from_bounds("not a version")

    def test_invalid_upper(self) -> None:
        with pytest.raises(InvalidVersion):
            VersionRange.from_bounds(upper="not a version")

    def test_prereleases_false_excludes(self) -> None:
        r = VersionRange.from_bounds("1.0", "2.0", prereleases=False)
        assert r._prereleases_configured is False
        assert not r.contains("1.5a1")

    def test_prereleases_true_admits(self) -> None:
        r = VersionRange.from_bounds("1.0", "2.0", prereleases=True)
        assert r._prereleases_configured is True
        assert r.contains("1.5a1")

    def test_bounds_only_admits_post_of_lower(self) -> None:
        r = VersionRange.from_bounds("1.0", "2.0", include_lower=False)
        assert V("1.0.post1") in r
        assert V("1.0.post1") not in _range(">1.0,<2.0")

    def test_bounds_only_admits_local(self) -> None:
        r = VersionRange.from_bounds("1.0", "2.0", include_lower=False)
        assert V("1.0+local") in r

    def test_bounds_only_admits_rc_below_upper(self) -> None:
        r = VersionRange.from_bounds("1.0", "2.0")
        assert V("2.0rc1") in r
        assert V("2.0rc1") not in _range(">=1.0,<2.0")

    def test_floor_empty_canonical(self) -> None:
        """(-inf, 0.dev0) holds no version; ``_canonical_floor`` drops the
        interval, which plain bound comparison cannot see with an unbounded
        lower."""
        assert VersionRange.from_bounds(None, "0.dev0", include_upper=False).is_empty

    def test_floor_empty_epoch_variant(self) -> None:
        """0!0.dev0 is the same version as 0.dev0, so it is floor-empty too."""
        assert VersionRange.from_bounds(None, "0!0.dev0", include_upper=False).is_empty

    def test_higher_epoch_floor_not_collapsed(self) -> None:
        r = VersionRange.from_bounds(None, "1!0.dev0", include_upper=False)
        assert not r.is_empty
        assert V("5.0") in r

    def test_inclusive_lower_at_floor_folds_to_full(self) -> None:
        assert VersionRange.from_bounds("0.dev0") == VersionRange.full(
            admit_arbitrary=False
        )


class TestSnapBounds:
    def test_anchors_finite_bounds_inward(self) -> None:
        r = _range(">=1.0,<2.0")
        assert r.snap_bounds(["1.2", "1.5", "1.8"]) == VersionRange.from_bounds(
            "1.2", "1.8"
        )

    def test_keeps_unbounded_end(self) -> None:
        r = _range(">=1.0")
        assert r.snap_bounds(["1.2", "1.5"]) == VersionRange.from_bounds("1.2")

    def test_witnessless_segment_kept(self) -> None:
        r = _range(">=1.0,<2.0")
        assert r.snap_bounds(["5.0", "6.0"]) == r

    def test_empty_anchors_identity(self) -> None:
        r = _range(">=1.0,<2.0")
        assert r.snap_bounds([]) == r

    def test_empty_range_returns_self(self) -> None:
        r = VersionRange.empty()
        assert r.snap_bounds(["1.0"]) is r

    def test_unsorted_and_duplicate_anchors(self) -> None:
        r = _range(">=1.0,<2.0")
        result = r.snap_bounds(["1.8", "1.2", "1.5", "1.2"])
        assert result == VersionRange.from_bounds("1.2", "1.8")

    def test_mixed_string_and_version_anchors(self) -> None:
        r = _range(">=1.0,<2.0")
        result = r.snap_bounds(["1.2", V("1.8")])
        assert result == VersionRange.from_bounds("1.2", "1.8")

    def test_invalid_anchor_raises(self) -> None:
        with pytest.raises(InvalidVersion):
            _range(">=1.0").snap_bounds(["not a version"])

    def test_multi_segment_independent(self) -> None:
        r = _range(">=1.0,<2.0") | _range(">=3.0,<4.0")
        result = r.snap_bounds(["1.5", "3.5"])
        expected = VersionRange.singleton("1.5") | VersionRange.singleton("3.5")
        assert result == expected

    def test_literal_carriage(self) -> None:
        r = _range(">=1.0,<2.0") | SpecifierSet("===wat").to_range()
        result = r.snap_bounds(["1.2", "1.8"])
        assert result._admit == frozenset({"wat"})
        assert result.contains("wat")

    def test_pre_region_carriage(self) -> None:
        r = _range(">=1.0a1,<2.0")
        assert r._pre_region
        result = r.snap_bounds(["1.0a1", "1.5"])
        assert result._pre_region
        assert list(result.filter([V("1.0a1")])) == [V("1.0a1")]

    def test_configured_policy_carriage(self) -> None:
        r = SpecifierSet(">=1.0,<2.0", prereleases=False).to_range()
        result = r.snap_bounds(["1.2", "1.8"])
        assert result._prereleases_configured is False

    def test_result_is_subset(self) -> None:
        r = _range(">=1.0,<2.0")
        result = r.snap_bounds(["1.2", "1.5", "1.8"])
        assert result.is_subset(r)

    def test_agrees_on_given_versions(self) -> None:
        r = _range(">=1.0,<2.0")
        versions = ["0.5", "1.0", "1.5", "1.9", "2.0", "3.0"]
        result = r.snap_bounds(versions)
        for text in versions:
            assert result.contains(text) == r.contains(text)

    def test_gap_round_trips_to_singleton(self) -> None:
        versions = [V("1.0"), V("2.0"), V("3.0")]
        gap = VersionRange.from_bounds(
            "1.0", "3.0", include_lower=False, include_upper=False
        )
        assert gap.snap_bounds(versions) == VersionRange.singleton("2.0")
