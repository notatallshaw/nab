"""Tests for the vendored patch's ``assume_sorted`` keyword on ``filter``.

Given ``assume_sorted`` the range bisects each interval instead of testing every
entry. The items yielded and the order they come in are the contract, and the
order is the candidate order ``choose_version`` walks, so the grid tests compare
every calling shape against the same call without the keyword.
"""

from __future__ import annotations

from typing import Any

import pytest

from nab_python._vendor.packaging import ranges
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.version import Version

V = Version

# Single and double bounds, exclusions, wildcards, an epoch, a post, a local,
# pre-release naming clauses, and the two ``===`` shapes.
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
    ">1.0a1",
    "==1.0a1",
    "<1.0.post0.dev0",
    ">=1.0.dev0,<1.0",
    "===1.5",
    "===frobnicate",
]

# Ascending, with the repeats a real listing carries when one version ships
# both a wheel and an sdist.
LISTING = [
    V(text)
    for text in (
        "0.9",
        "1.0.dev1",
        "1.0a1",
        "1.0rc1",
        "1.0",
        "1.0+local",
        "1.0.post1",
        "1.4",
        "1.5",
        "1.5",
        "1.8",
        "1.9.9",
        "2.0",
        "2.0",
        "2.0",
        "3.0",
        "1!0.5",
        "1!1.0",
    )
]

DESCENDING = list(reversed(LISTING))


def _range(spec: str) -> VersionRange:
    return SpecifierSet(spec).to_range()


def _paired(versions: list[Version]) -> list[tuple[Version, str]]:
    """The listing shape nab holds: a version paired with its file."""
    return [(version, f"pkg-{version}.whl") for version in versions]


def _first(entry: tuple[Any, str]) -> Any:
    return entry[0]


def _split_region() -> VersionRange:
    """A range whose opt-in region covers only part of its bounds.

    A plain specifier set always autodetects a region equal to its own bounds,
    so the interesting shape only arises from set algebra: unioning a
    pre-release-naming range with one that names none leaves the opt-in
    confined to the first interval.
    """
    return _range(">=1.0a1,<1.5") | _range(">=2.0,<3.0")


class TestSortedFilter:
    def test_filters_a_closed_interval(self) -> None:
        listing = [V(v) for v in ("3.0", "2.0", "1.5", "1.0")]
        filtered = _range(">=1.0,<2.0").filter(listing, assume_sorted="descending")
        assert list(filtered) == [V("1.5"), V("1.0")]

    def test_filters_the_same_listing_ascending(self) -> None:
        listing = [V(v) for v in ("1.0", "1.5", "2.0", "3.0")]
        filtered = _range(">=1.0,<2.0").filter(listing, assume_sorted="ascending")
        assert list(filtered) == [V("1.0"), V("1.5")]

    def test_keeps_duplicate_entries(self) -> None:
        listing = [V(v) for v in ("2.0", "1.5", "1.5", "1.0")]
        filtered = _range(">=1.0,<2.0").filter(listing, assume_sorted="descending")
        assert list(filtered) == [V("1.5"), V("1.5"), V("1.0")]

    def test_empty_sequence_in_both_directions(self) -> None:
        assert list(_range(">=1.0").filter([], assume_sorted="ascending")) == []
        assert list(_range(">=1.0").filter([], assume_sorted="descending")) == []

    def test_single_entry_is_sorted_either_way(self) -> None:
        listing = [V("1.5")]
        assert list(_range(">=1.0").filter(listing, assume_sorted="ascending")) == (
            listing
        )
        assert list(_range(">=1.0").filter(listing, assume_sorted="descending")) == (
            listing
        )

    def test_all_equal_entries_are_sorted_either_way(self) -> None:
        listing = [V("1.5")] * 4
        assert list(_range(">=1.0").filter(listing, assume_sorted="ascending")) == (
            listing
        )
        assert list(_range(">=1.0").filter(listing, assume_sorted="descending")) == (
            listing
        )

    def test_empty_range(self) -> None:
        filtered = VersionRange.empty().filter(DESCENDING, assume_sorted="descending")
        assert list(filtered) == []

    def test_full_range_keeps_every_entry(self) -> None:
        finals = [v for v in DESCENDING if not v.is_prerelease]
        full = VersionRange.full(admit_arbitrary=False)
        assert list(full.filter(finals, assume_sorted="descending")) == finals

    def test_multi_interval_range_stays_in_sequence_order(self) -> None:
        """The bounds ascend and the listing descends, so the walk runs backwards."""
        listing = [V(v) for v in ("3.0", "2.0", "1.5", "1.0")]
        filtered = _range("!=1.5").filter(listing, assume_sorted="descending")
        assert list(filtered) == [V("3.0"), V("2.0"), V("1.0")]

    def test_exclusive_bounds(self) -> None:
        listing = [V(v) for v in ("2.0", "1.5", "1.0")]
        filtered = _range(">1.0,<2.0").filter(listing, assume_sorted="descending")
        assert list(filtered) == [V("1.5")]

    def test_every_entry_below_the_range(self) -> None:
        listing = [V(v) for v in ("1.5", "1.0")]
        filtered = _range(">=5.0").filter(listing, assume_sorted="descending")
        assert list(filtered) == []

    def test_every_entry_above_the_range(self) -> None:
        listing = [V(v) for v in ("1.5", "1.0")]
        filtered = _range("<0.5").filter(listing, assume_sorted="descending")
        assert list(filtered) == []

    def test_the_result_is_lazy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A caller taking one candidate locates one interval, not all of them."""
        located: list[tuple[int, int]] = []
        real = ranges._partition_indexes

        def spy(*args: Any, **kwargs: Any) -> tuple[int, int]:
            result = real(*args, **kwargs)
            located.append(result)
            return result

        monkeypatch.setattr(ranges, "_partition_indexes", spy)
        multi = _range("!=1.5,!=2.0")
        assert len(multi._bounds) == 3
        filtered = multi.filter(DESCENDING, assume_sorted="descending")
        assert next(iter(filtered)) == DESCENDING[0]
        assert len(located) == 1


class TestSortedFilterLiterals:
    """``===`` names members the bounds do not describe, so bisection is off."""

    def test_admit_literal_falls_back_to_the_entry_walk(self) -> None:
        listing = [V(v) for v in ("2.0", "1.5", "1.0")]
        literal = _range("===1.5")
        assert literal._admit
        assert list(literal.filter(listing, assume_sorted="descending")) == [V("1.5")]

    def test_reject_literal_falls_back_to_the_entry_walk(self) -> None:
        listing = [V(v) for v in ("2.0", "1.5", "1.0")]
        rejecting = ~_range("===1.5")
        assert rejecting._reject
        filtered = list(rejecting.filter(listing, assume_sorted="descending"))
        assert filtered == [V("2.0"), V("1.0")]

    def test_the_fallback_ignores_a_wrong_declared_order(self) -> None:
        literal = _range("===1.5")
        assert list(literal.filter(LISTING, assume_sorted="descending")) == [
            V("1.5"),
            V("1.5"),
        ]

    def test_non_version_admit_literal(self) -> None:
        literal = _range("===frobnicate")
        filtered = literal.filter(["1.5", "frobnicate"], assume_sorted="ascending")
        assert list(filtered) == ["frobnicate"]


class TestSortedFilterArbitraryAdmission:
    """Live arbitrary admission does not turn bisection off.

    It only takes strings that do not parse as a version, and those have no
    place in version order, so the sorted walk rejects one rather than gating
    on the flag.
    """

    def test_live_admission_still_bisects(self) -> None:
        full = VersionRange.full()
        assert full._arbitrary_active()
        listing = [V(v) for v in ("3.0", "2.0", "1.0")]
        assert list(full.filter(listing, assume_sorted="descending")) == listing

    def test_an_admitted_string_has_no_place_in_version_order(self) -> None:
        full = VersionRange.full()
        listing = ["3.0", "2.0", "frobnicate", "1.0"]
        with pytest.raises(ValueError, match="does not parse as a version"):
            list(full.filter(listing, assume_sorted="descending"))
        assert list(full.filter(listing)) == listing

    def test_a_wrong_declared_order_is_caught_here_too(self) -> None:
        full = VersionRange.full()
        with pytest.raises(ValueError, match="assume_sorted='descending'"):
            full.filter(LISTING, assume_sorted="descending")

    def test_the_empty_specifier_set_bisects_too(self) -> None:
        universal = _range("")
        assert universal._arbitrary_active()
        listing = [V(v) for v in ("3.0", "2.0", "1.0")]
        assert list(universal.filter(listing, assume_sorted="descending")) == listing

    def test_an_inert_arbitrary_flag_still_bisects(self) -> None:
        narrowed = VersionRange.full() & _range(">=1.0,<2.0")
        assert not narrowed._arbitrary_active()
        listing = [V(v) for v in ("2.0", "1.5", "1.0")]
        filtered = narrowed.filter(listing, assume_sorted="descending")
        assert list(filtered) == [V("1.5"), V("1.0")]

    def test_an_inert_flag_rejects_a_string_with_no_place_in_order(self) -> None:
        narrowed = VersionRange.full() & _range(">=1.0,<2.0")
        with pytest.raises(ValueError, match="does not parse as a version"):
            narrowed.filter(["frobnicate", "1.5"], assume_sorted="ascending")


class TestSortedFilterPrereleasePolicy:
    def test_configured_excluding_policy_drops_prereleases(self) -> None:
        listing = [V(v) for v in ("1.5", "1.0", "1.0a1")]
        strict = SpecifierSet(">=1.0.dev0", prereleases=False).to_range()
        filtered = strict.filter(listing, assume_sorted="descending")
        assert list(filtered) == [V("1.5"), V("1.0")]

    def test_configured_admitting_policy_keeps_prereleases(self) -> None:
        listing = [V(v) for v in ("1.5", "1.0", "1.0a1")]
        loose = SpecifierSet(">=1.0.dev0", prereleases=True).to_range()
        assert list(loose.filter(listing, assume_sorted="descending")) == listing

    def test_prereleases_argument_overrides_the_configured_policy(self) -> None:
        listing = [V(v) for v in ("1.5", "1.0", "1.0a1")]
        strict = SpecifierSet(">=1.0.dev0", prereleases=False).to_range()
        filtered = strict.filter(listing, prereleases=True, assume_sorted="descending")
        assert list(filtered) == listing
        loose = SpecifierSet(">=1.0.dev0", prereleases=True).to_range()
        relaxed = loose.filter(listing, prereleases=False, assume_sorted="descending")
        assert list(relaxed) == [V("1.5"), V("1.0")]

    def test_default_policy_drops_a_prerelease_behind_a_final(self) -> None:
        listing = [V(v) for v in ("1.5", "1.0", "1.0a1")]
        plain = _range(">=0.9,<2.0")
        assert not plain._pre_region
        filtered = plain.filter(listing, assume_sorted="descending")
        assert list(filtered) == [V("1.5"), V("1.0")]

    def test_default_policy_flushes_the_buffer_with_no_final(self) -> None:
        listing = [V(v) for v in ("1.5rc1", "1.0a1")]
        plain = _range(">=0.9,<2.0")
        assert not plain._pre_region
        assert list(plain.filter(listing, assume_sorted="descending")) == listing

    def test_a_region_spanning_the_bounds_keeps_every_entry(self) -> None:
        listing = [V(v) for v in ("1.5", "1.0", "1.0a1")]
        spanning = _range(">=1.0a1,<2.0")
        assert spanning._pre_region == spanning._bounds
        assert list(spanning.filter(listing, assume_sorted="descending")) == listing

    def test_an_in_region_prerelease_is_admitted_in_place(self) -> None:
        """Force-admission keeps position; a buffered one would move to the tail."""
        split = _split_region()
        assert split._pre_region != split._bounds
        listing = [V(v) for v in ("2.5", "2.5rc1", "2.0", "1.4", "1.0", "1.0rc1")]
        assert list(split.filter(listing, assume_sorted="descending")) == [
            V("2.5"),
            V("2.0"),
            V("1.4"),
            V("1.0"),
            V("1.0rc1"),
        ]

    def test_out_of_region_prereleases_buffer_behind_the_in_region_ones(self) -> None:
        """With no final in range the buffer flushes, reordering the output."""
        split = _split_region()
        listing = [V(v) for v in ("2.5rc1", "1.0rc1", "1.0a1")]
        assert list(split.filter(listing, assume_sorted="descending")) == [
            V("1.0rc1"),
            V("1.0a1"),
            V("2.5rc1"),
        ]


class TestSortedFilterKey:
    def test_filters_paired_entries(self) -> None:
        listing = _paired([V(v) for v in ("2.0", "1.5", "1.0")])
        filtered = _range(">=1.0,<2.0").filter(
            listing, key=_first, assume_sorted="descending"
        )
        assert list(filtered) == listing[1:]

    def test_filters_paired_entries_ascending(self) -> None:
        listing = _paired([V(v) for v in ("1.0", "1.5", "2.0")])
        filtered = _range(">=1.0,<2.0").filter(
            listing, key=_first, assume_sorted="ascending"
        )
        assert list(filtered) == listing[:2]

    def test_key_over_a_literal_range_falls_back_to_the_entry_walk(self) -> None:
        listing = _paired([V(v) for v in ("2.0", "1.5", "1.0")])
        filtered = _range("===1.5").filter(
            listing, key=_first, assume_sorted="descending"
        )
        assert list(filtered) == [listing[1]]

    def test_key_returning_a_string(self) -> None:
        listing = [("2.0", "c.whl"), ("1.5", "b.whl"), ("1.0", "a.whl")]
        filtered = _range(">=1.0,<2.0").filter(
            listing, key=_first, assume_sorted="descending"
        )
        assert list(filtered) == listing[1:]

    def test_key_projected_entries_decide_the_order(self) -> None:
        with pytest.raises(ValueError, match="assume_sorted='ascending'"):
            _range(">=1.0").filter(
                _paired(DESCENDING), key=_first, assume_sorted="ascending"
            )


class TestSortedFilterOrderContract:
    """The order and parse promises are checked before the iterator is handed back."""

    def test_ascending_claim_over_a_descending_sequence(self) -> None:
        with pytest.raises(ValueError, match="assume_sorted='ascending'"):
            _range(">=1.0").filter(DESCENDING, assume_sorted="ascending")

    def test_descending_claim_over_an_ascending_sequence(self) -> None:
        with pytest.raises(ValueError, match="assume_sorted='descending'"):
            _range(">=1.0").filter(LISTING, assume_sorted="descending")

    def test_unparsable_endpoint_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="'frobnicate' does not parse"):
            _range(">=1.0").filter(["1.0", "frobnicate"], assume_sorted="ascending")

    def test_an_unrecognised_order_is_rejected(self) -> None:
        for claimed in ("Descending", "desc", "sorted", ""):
            with pytest.raises(ValueError, match="must be 'ascending' or 'descending'"):
                _range(">=1.0").filter(DESCENDING, assume_sorted=claimed)  # type: ignore[arg-type]

    def test_an_unrecognised_order_is_rejected_before_the_length_check(self) -> None:
        with pytest.raises(ValueError, match="must be 'ascending' or 'descending'"):
            _range(">=1.0").filter([V("1.5")], assume_sorted="desc")  # type: ignore[arg-type]

    def test_a_string_sequence_filters_by_version(self) -> None:
        # ``Version.__le__(str)`` returns the truthy ``NotImplemented``, so a
        # bisection over raw entries would read every string as in bounds.
        r = _range(">=1.0")
        assert list(r.filter(["0.1", "0.2"], assume_sorted="ascending")) == []
        assert list(r.filter(["1.1", "1.2"], assume_sorted="ascending")) == [
            "1.1",
            "1.2",
        ]


class TestSortedFilterMatchesTheEntryWalk:
    """Every calling shape agrees with the same call without the keyword."""

    @pytest.mark.parametrize("spec", SPECS)
    def test_ascending(self, spec: str) -> None:
        r = _range(spec)
        assert list(r.filter(LISTING, assume_sorted="ascending")) == list(
            r.filter(LISTING)
        )

    @pytest.mark.parametrize("spec", SPECS)
    def test_descending(self, spec: str) -> None:
        r = _range(spec)
        assert list(r.filter(DESCENDING, assume_sorted="descending")) == list(
            r.filter(DESCENDING)
        )

    @pytest.mark.parametrize("spec", SPECS)
    @pytest.mark.parametrize("prereleases", [None, True, False])
    def test_every_prefix_under_every_policy(
        self, spec: str, prereleases: bool | None
    ) -> None:
        r = _range(spec)
        for cut in range(len(LISTING) + 1):
            window = LISTING[:cut]
            expected = list(r.filter(window, prereleases))
            assert list(r.filter(window, prereleases, assume_sorted="ascending")) == (
                expected
            )
            reverse = list(reversed(window))
            assert list(
                r.filter(reverse, prereleases, assume_sorted="descending")
            ) == list(r.filter(reverse, prereleases))

    @pytest.mark.parametrize("spec", SPECS)
    def test_key_over_every_prefix(self, spec: str) -> None:
        r = _range(spec)
        for cut in range(len(LISTING) + 1):
            paired = _paired(LISTING[:cut])
            assert list(r.filter(paired, key=_first, assume_sorted="ascending")) == (
                list(r.filter(paired, key=_first))
            )
            reverse = _paired(list(reversed(LISTING[:cut])))
            assert list(r.filter(reverse, key=_first, assume_sorted="descending")) == (
                list(r.filter(reverse, key=_first))
            )

    @pytest.mark.parametrize("spec", SPECS)
    def test_configured_policies_over_every_prefix(self, spec: str) -> None:
        for prereleases in (True, False):
            r = SpecifierSet(spec, prereleases=prereleases).to_range()
            for cut in range(len(LISTING) + 1):
                window = LISTING[:cut]
                assert list(r.filter(window, assume_sorted="ascending")) == list(
                    r.filter(window)
                )
                reverse = list(reversed(window))
                assert list(r.filter(reverse, assume_sorted="descending")) == list(
                    r.filter(reverse)
                )

    @pytest.mark.parametrize("spec", SPECS)
    def test_algebra_derived_ranges(self, spec: str) -> None:
        base = _range(spec)
        others = (_range(">=1.0a1,<1.5"), _range(">=2.0,<3.0"), _range("!=1.5"))
        for other in others:
            for derived in (base & other, base | other, base - other, ~base):
                assert list(derived.filter(DESCENDING, assume_sorted="descending")) == (
                    list(derived.filter(DESCENDING))
                )
