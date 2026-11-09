"""Candidate ordering retains version text, preferences and withdrawal status."""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.requirements import Requirement
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
