"""Candidate range encoding preserves holes, constraints and yank restrictions."""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator
from nab_provider.yank_candidates import Candidate
from nab_provider.yank_preference import PreferenceScope
from nab_provider.yanked_resolution import YankProxyProvider


@pytest.mark.parametrize(
    ("specifier", "live_only", "expected"),
    [
        (">=1", False, {1, 3, 4}),
        (">=1", True, {1, 3}),
        (">=2", True, {1}),
        (">=4", False, set()),
    ],
)
def test_encoding_keeps_sparse_candidate_identities(
    specifier: str, live_only: bool, expected: set[int]
) -> None:
    provider = Provider(make_coordinator())
    search = YankProxyProvider(
        provider,
        lambda: provider,
        [],
        [],
        {"dep": Requirement("dep!=2").specifier.to_range()},
        preferences={},
        scope=PreferenceScope(live=frozenset({"dep"}) if live_only else frozenset()),
    )
    candidates = (
        Candidate("dep", Version("3"), withdrawn=False),
        Candidate("dep", Version("2"), withdrawn=False),
        Candidate("dep", Version("1"), withdrawn=False),
        Candidate("dep", Version("3"), withdrawn=True),
    )
    search.catalogue.candidates["dep"] = candidates
    assert [search.identify(candidate) for candidate in candidates] == [1, 2, 3, 4]

    encoded = search.encode_range(
        "dep", Requirement("dep" + specifier).specifier.to_range()
    )

    assert {token for token in range(6) if token in encoded} == expected
    assert [search.identify(candidate) for candidate in candidates] == [1, 2, 3, 4]
    assert not search.catalogue.facts
    assert not search.catalogue.provider.coordinator.calls_to("request_metadata")
