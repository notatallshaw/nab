"""Candidate ordering retains version text, preferences and withdrawal status."""

from __future__ import annotations

from collections.abc import Sequence
from cProfile import Profile

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider, ResolutionStrategy
from nab_provider.testing import make_coordinator
from nab_provider.yank_candidates import Candidate, YankCandidates


@pytest.mark.parametrize(
    ("specifier", "preferred", "lowest", "expected"),
    [
        ("", None, False, ["2", "1", "1.0", "2rc1", "1rc1"]),
        ("", None, True, ["1", "1.0", "2", "1rc1", "2rc1"]),
        ("", "1.0", False, ["1.0", "2", "1", "2rc1", "1rc1"]),
        ("", "1.0.0", False, ["1", "2", "1.0", "2rc1", "1rc1"]),
        ("", "3", False, ["2", "1", "1.0", "2rc1", "1rc1"]),
        ("", "2", True, ["1", "1.0", "2", "1rc1", "2rc1"]),
        ("===1.0", None, False, ["1.0"]),
        ("<1", None, False, []),
    ],
)
def test_order_retains_equal_version_text_and_prerelease_batches(
    specifier: str,
    preferred: str | None,
    lowest: bool,
    expected: list[str],
) -> None:
    provider = Provider(
        make_coordinator(),
        resolution_strategy=ResolutionStrategy.LOWEST
        if lowest
        else ResolutionStrategy.HIGHEST,
    )
    facts = YankCandidates(
        provider,
        lambda: provider,
        preferences={} if preferred is None else {"dep": Version(preferred)},
    )
    choices = [
        Candidate("dep", Version(text), withdrawn=False)
        for text in ("1.0", "2rc1", "1", "1rc1", "2")
    ]
    allowed = Requirement("dep" + specifier).specifier.to_range()

    actual = facts.ordered("dep", allowed, choices=choices)

    assert [candidate.label for candidate in actual] == expected
    assert all(any(candidate is choice for choice in choices) for candidate in actual)


def test_order_keeps_withdrawn_candidates_separate_and_last() -> None:
    provider = Provider(make_coordinator())
    facts = YankCandidates(provider, lambda: provider, preferences={})
    live = Candidate("dep", Version("1"), withdrawn=False)
    withdrawn = Candidate("dep", Version("1"), withdrawn=True)
    replacement = Candidate("dep", Version("1"), withdrawn=False)

    actual = facts.ordered(
        "dep",
        Requirement("dep").specifier.to_range(),
        choices=[live, withdrawn, replacement],
    )

    assert len(actual) == 2
    assert actual[0] is replacement
    assert actual[1] is withdrawn


@pytest.fixture
def catalogue() -> YankCandidates:
    """Retain one listing with equal version text, pre-releases and withdrawn files."""
    provider = Provider(make_coordinator())
    facts = YankCandidates(provider, lambda: provider, preferences={})
    facts.candidates["dep"] = tuple(
        Candidate("dep", Version(text), withdrawn=withdrawn)
        for withdrawn in (False, True)
        for text in ("1", "1.0", "2rc1", "2", "3rc1", "3")
    )
    return facts


def ordered_with_filter_count(
    catalogue: YankCandidates,
    allowed: VersionRange,
    *,
    choices: Sequence[Candidate] | None = None,
) -> tuple[list[Candidate], int]:
    """Count range filtering through the actual ordering entry point."""
    with Profile() as profile:
        result = catalogue.ordered("dep", allowed, choices=choices)
    calls = sum(
        entry.callcount
        for entry in profile.getstats()
        if entry.code is VersionRange.filter.__code__
    )
    return result, calls


def test_default_order_reuses_equal_ranges_and_returns_a_copy(
    catalogue: YankCandidates,
) -> None:
    allowed = SpecifierSet(">=1,<3").to_range()
    equivalent = SpecifierSet("<3,>=1").to_range()
    assert allowed == equivalent
    assert allowed is not equivalent
    first, first_calls = ordered_with_filter_count(catalogue, allowed)
    expected = first.copy()
    first.clear()

    second, second_calls = ordered_with_filter_count(catalogue, equivalent)

    assert second == expected
    assert second is not first
    assert first_calls > 0
    assert second_calls == 0


def test_unrestricted_order_survives_requirement_filtering(
    catalogue: YankCandidates,
) -> None:
    unrestricted = VersionRange.full()
    limited = SpecifierSet(">=2").to_range()
    expected = catalogue.ordered("dep", unrestricted)
    expected_limited = catalogue.ordered("dep", limited)

    actual, calls = ordered_with_filter_count(catalogue, unrestricted)
    actual_limited, limited_calls = ordered_with_filter_count(catalogue, limited)

    assert actual == expected
    assert actual_limited == expected_limited
    assert calls == limited_calls == 0


def test_only_the_latest_restricted_order_is_retained(
    catalogue: YankCandidates,
) -> None:
    unrestricted = VersionRange.full()
    first = SpecifierSet(">=2").to_range()
    second = SpecifierSet("<2").to_range()
    expected = catalogue.ordered("dep", unrestricted)
    expected_first = catalogue.ordered("dep", first)
    catalogue.ordered("dep", second)

    actual, unrestricted_calls = ordered_with_filter_count(catalogue, unrestricted)
    actual_first, first_calls = ordered_with_filter_count(catalogue, first)

    assert actual == expected
    assert actual_first == expected_first
    assert unrestricted_calls == 0
    assert first_calls > 0


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (SpecifierSet(">=1,<3").to_range(), SpecifierSet(">=2,<3").to_range()),
        (SpecifierSet("===1").to_range(), SpecifierSet("===1.0").to_range()),
        (SpecifierSet("===1").to_range(), ~SpecifierSet("===1").to_range()),
        (
            SpecifierSet("<=2", prereleases=False).to_range(),
            SpecifierSet("<=2", prereleases=True).to_range(),
        ),
        (SpecifierSet("<=2").to_range(), SpecifierSet("<2.post0.dev0").to_range()),
    ],
)
def test_default_order_rechecks_filter_policy(
    catalogue: YankCandidates, first: VersionRange, second: VersionRange
) -> None:
    catalogue.ordered("dep", first)
    expected = catalogue.ordered("dep", second, choices=catalogue.choices("dep"))

    actual, calls = ordered_with_filter_count(catalogue, second)

    assert actual == expected
    assert calls > 0


def test_default_order_rechecks_listing_identity(catalogue: YankCandidates) -> None:
    allowed = VersionRange.full()
    catalogue.ordered("dep", allowed)
    replacement = Candidate("dep", Version("4"), withdrawn=False)
    catalogue.candidates["dep"] = (*catalogue.choices("dep"), replacement)

    actual, calls = ordered_with_filter_count(catalogue, allowed)

    assert actual[0] is replacement
    assert calls > 0


def test_explicit_choices_do_not_use_or_replace_default_order(
    catalogue: YankCandidates,
) -> None:
    allowed = VersionRange.full()
    expected = catalogue.ordered("dep", allowed)
    candidate = Candidate("dep", Version("4"), withdrawn=False)

    explicit, explicit_calls = ordered_with_filter_count(
        catalogue, allowed, choices=[candidate]
    )
    default, default_calls = ordered_with_filter_count(catalogue, allowed)

    assert explicit == [candidate]
    assert explicit_calls > 0
    assert default == expected
    assert default_calls == 0


def test_preferred_choices_bypass_default_order(catalogue: YankCandidates) -> None:
    allowed = VersionRange.full()
    expected = catalogue.ordered("dep", allowed)
    preferences = {"dep": Version("1.0")}
    catalogue.preferences = preferences

    preferred, preferred_calls = ordered_with_filter_count(catalogue, allowed)
    preferences["dep"] = Version("2")
    changed, changed_calls = ordered_with_filter_count(catalogue, allowed)
    preferences.clear()
    restored, restored_calls = ordered_with_filter_count(catalogue, allowed)

    assert preferred[0].label == "1.0"
    assert changed[0].label == "2"
    assert preferred_calls > 0
    assert changed_calls > 0
    assert restored == expected
    assert restored_calls == 0


def test_default_order_rechecks_lowest_direct_policy(
    catalogue: YankCandidates,
) -> None:
    provider = Provider(
        make_coordinator(), resolution_strategy=ResolutionStrategy.LOWEST_DIRECT
    )
    catalogue.provider = provider
    allowed = VersionRange.full()
    highest = catalogue.ordered("dep", allowed)
    catalogue.provider = Provider(
        make_coordinator(),
        resolution_strategy=ResolutionStrategy.LOWEST_DIRECT,
        direct_packages=frozenset({"dep"}),
    )

    lowest, calls = ordered_with_filter_count(catalogue, allowed)

    assert highest[0].label == "3"
    assert lowest[0].label == "1"
    assert calls > 0
