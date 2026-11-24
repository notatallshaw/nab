"""Candidate range encoding preserves holes, constraints and yank restrictions."""

from __future__ import annotations

import gc
import weakref

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from nab_provider.policy import ResolutionStrategy
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator, make_wheel
from nab_provider.yank_candidates import Candidate, CandidateData
from nab_provider.yank_preference import PreferenceScope
from nab_provider.yanked_resolution import MetadataProxy, YankProxyProvider
from nab_provider.yanking import mark_yanked
from nab_resolver.ranges import Range
from nab_resolver.types import RootRequirement


class _TrackedFloat(float):
    """Expose whether a completed proxy search retains a range bound."""


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


@pytest.mark.parametrize(
    ("strategy", "prerelease"),
    [(ResolutionStrategy.HIGHEST, "2rc1"), (ResolutionStrategy.LOWEST, "0.9rc1")],
)
def test_root_requirement_orders_prereleases_with_final_candidates(
    strategy: ResolutionStrategy, prerelease: str
) -> None:
    port = make_coordinator(
        [make_wheel("1", package="dep"), make_wheel(prerelease, package="dep")],
        package="dep",
    )
    provider = Provider(port, resolution_strategy=strategy)
    requirement = Requirement("dep>=0.1rc1")
    allowed = requirement.specifier.to_range()
    search = YankProxyProvider(
        provider,
        lambda: provider,
        [RootRequirement("dep", allowed)],
        [requirement],
        {},
        preferences={},
    )
    encoded = search.encode_range("dep", allowed)

    chosen = search.choose_version("dep", encoded)
    assert chosen is not None
    assert search.items[chosen].version == Version(prerelease)


def test_backtracking_restores_the_order_for_the_selected_parent() -> None:
    provider = Provider(make_coordinator())
    search = YankProxyProvider(provider, lambda: provider, [], [], {}, preferences={})
    stable = Candidate("dep", Version("1"), withdrawn=False)
    preview = Candidate("dep", Version("2rc1"), withdrawn=False)
    older_dep = Candidate("dep", Version("0.5"), withdrawn=False)
    search.catalogue.candidates["dep"] = (stable, preview, older_dep)
    parent = Candidate("app", Version("2"), withdrawn=False)
    older = Candidate("app", Version("1"), withdrawn=False)
    parent_id = search.identify(parent)
    older_id = search.identify(older)
    requirement = Requirement("dep>=1rc1").specifier.to_range()
    search.catalogue.facts[parent] = CandidateData(provider, {"dep": requirement}, ())
    domain = search.get_dependencies(MetadataProxy(parent_id), 0)["dep"]

    search.receive_decision_scan_hint({}, {"app": parent_id})
    assert search.choose_version("dep", domain) == search.identify(preview)

    # Until the parent clause is propagated, the solver can still probe older_dep.
    before_propagation = Range.singleton(search.identify(older_dep))
    assert search.has_satisfying_version("dep", before_propagation)

    search.receive_decision_scan_hint({}, {})
    assert search.choose_version("dep", domain) == search.identify(stable)

    # The replacement parent first has unread metadata, then no dependency on dep.
    search.receive_decision_scan_hint({}, {"app": older_id})
    assert search.choose_version("dep", domain) == search.identify(stable)
    search.catalogue.facts[older] = CandidateData(provider, {}, ())
    assert search.choose_version("dep", domain) == search.identify(stable)


def test_completed_search_releases_membership_owners() -> None:
    provider = Provider(make_coordinator())
    search = YankProxyProvider(provider, lambda: provider, [], [], {}, preferences={})
    candidate = Candidate("dep", Version("1"), withdrawn=False)
    search.catalogue.candidates["dep"] = (candidate,)
    bound = _TrackedFloat(0.5)
    allowed: Range[float] = Range.between(bound, 1.5)
    reference = weakref.ref(bound)
    query = search.new_query(PreferenceScope())
    assert query.choose_version("dep", allowed) == search.identify(candidate)

    del query, bound, allowed
    gc.collect()
    assert reference() is not None

    pins, _provider = search.resolve()
    assert pins == {}
    gc.collect()
    assert reference() is None


def test_withdrawn_files_at_one_release_share_an_alias_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheels = [
        mark_yanked(make_wheel("1.0", tag=f"{build}-py3-none-any"), reason=True)
        for build in range(32)
    ]
    provider = Provider(make_coordinator(wheels), defer_yanked=True)
    assert len(provider.fetch_versions("pkg")) == len(wheels)
    comparisons = 0
    original = Version.__eq__

    def compare(left: Version, right: object) -> bool:
        nonlocal comparisons
        comparisons += 1
        return original(left, right)

    monkeypatch.setattr(Version, "__eq__", compare)
    assert not provider.has_withdrawn_alternative()
    # One pass over the listing and one over this release cover every file.
    assert comparisons <= 2 * len(wheels)
