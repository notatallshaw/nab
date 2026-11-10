"""Proxy defaults remain universal and live choices avoid yank-scope lookups."""

from __future__ import annotations

from cProfile import Profile
from functools import partial

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator
from nab_provider.yank_candidates import Candidate
from nab_provider.yank_preference import PreferenceScope
from nab_provider.yanked_resolution import PermissionProxy, YankProxyProvider
from nab_resolver.ranges import Range


def proxy(*, scope: PreferenceScope | None = None) -> YankProxyProvider:
    """Construct an actual query without fetching remote metadata."""
    factory = partial(Provider, make_coordinator())
    if scope is None:
        scope = PreferenceScope()
    return YankProxyProvider(
        factory(), factory, (), (), {}, preferences={}, scope=scope
    )


@pytest.mark.parametrize("withdrawn", [False, True])
@pytest.mark.parametrize("live_only", [False, True])
@pytest.mark.parametrize("fixed", [False, True])
def test_eligibility_keeps_scope_and_fixed_choices(
    *, withdrawn: bool, live_only: bool, fixed: bool
) -> None:
    candidate = Candidate("dep[x]", Version("1"), withdrawn=withdrawn)
    scope = PreferenceScope(
        live=frozenset({"dep"}) if live_only else frozenset(),
        fixed=(Candidate("dep[x]", Version("2"), withdrawn=withdrawn),)
        if fixed
        else (),
    )
    search = proxy(scope=scope)

    with Profile() as profile:
        eligible = search.eligible(candidate)

    assert eligible is (not fixed and not (withdrawn and live_only))
    if not withdrawn:
        assert not any(
            entry.code is Candidate.base.fget.__code__ for entry in profile.getstats()
        )
    if not fixed:
        assert not any(
            entry.code is Candidate.__eq__.__code__ for entry in profile.getstats()
        )


def test_encoded_defaults_do_not_narrow_after_an_intersection() -> None:
    search = proxy()
    choices = tuple(
        Candidate("dep", Version(str(version)), withdrawn=False) for version in (1, 2)
    )
    search.catalogue.candidates["dep"] = choices
    narrow = Requirement("dep==1").specifier.to_range()
    universal = Requirement("dep").specifier.to_range()

    with Profile() as profile:
        first = search.encode_range("dep", narrow)
        second = search.encode_range("dep", universal)

    assert {item.version for token, item in search.items.items() if token in first} == {
        Version("1")
    }
    assert {
        item.version for token, item in search.items.items() if token in second
    } == {
        Version("1"),
        Version("2"),
    }
    assert not any(
        entry.code is VersionRange.full.__func__.__code__
        for entry in profile.getstats()
    )


def test_narrowing_returned_proof_choices_does_not_change_defaults() -> None:
    search = proxy()
    candidate = Candidate("dep", Version("1"), withdrawn=True)
    token = search.identify(candidate)
    package = PermissionProxy(token)
    dependencies = search.get_dependencies("dep", token)
    dependencies[package] &= Range.singleton(1)

    again = search.get_dependencies("dep", token)

    assert 1 in dependencies[package]
    assert 2 not in dependencies[package]
    assert 1 in again[package]
    assert 2 in again[package]
