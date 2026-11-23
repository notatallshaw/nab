"""Dependency metadata for base packages and their extras."""

from __future__ import annotations

import gc
import weakref

import pytest
from packaging.ranges import VersionRange
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from nab_provider._provider.metadata_resolver import (
    add_classified_dep,
    classify_requirement,
)
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator, make_wheel

from .provider_fixtures import PYTHON_312, SECURITY_EXTRA_METADATA


class TestAddClassifiedDep:
    """``add_classified_dep`` intersects duplicate dependency names."""

    def test_base_deps_intersect(self) -> None:
        """A name seen twice as a base dep folds to the intersection."""
        ranges: dict[str, VersionRange] = {}
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {}
        add_classified_dep(Requirement("bar>=2.0"), set(), base, extra_map, ranges)
        add_classified_dep(Requirement("bar<5.0"), set(), base, extra_map, ranges)

        assert Version("3.0") in base["bar"]
        assert Version("1.0") not in base["bar"]
        assert Version("9.0") not in base["bar"]

        # Folding must leave the shared memoized range unchanged.
        assert Version("9.0") in ranges[">=2.0"]

    def test_extra_deps_intersect(self) -> None:
        """A name seen twice under one extra folds to the intersection."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {"x": {}}
        add_classified_dep(Requirement("bar>=2.0"), {"x"}, base, extra_map, {})
        add_classified_dep(Requirement("bar<5.0"), {"x"}, base, extra_map, {})
        assert Version("3.0") in extra_map["x"]["bar"]
        assert Version("1.0") not in extra_map["x"]["bar"]
        assert Version("9.0") not in extra_map["x"]["bar"]

    def test_repeated_specifier_builds_one_range(self) -> None:
        """Two deps using one specifier share the range built for it."""
        ranges: dict[str, VersionRange] = {}
        base: dict[str, VersionRange] = {}
        add_classified_dep(Requirement("bar>=2.0"), set(), base, {}, ranges)
        add_classified_dep(Requirement("baz >= 2.0"), set(), base, {}, ranges)
        assert list(ranges) == [">=2.0"]
        assert base["bar"] is base["baz"]

    def test_base_multi_extra_splits_into_one_proxy_each(self) -> None:
        """``bar[a,b]`` as a base dep records ``bar`` plus a proxy per extra."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {}
        add_classified_dep(Requirement("bar[a,b]>=1.0"), set(), base, extra_map, {})
        assert Version("2.0") in base["bar"]
        assert Version("0.5") not in base["bar"]
        assert base["bar[a]"] == VersionRange.full(admit_arbitrary=False)
        assert base["bar[b]"] == VersionRange.full(admit_arbitrary=False)

    def test_extra_gated_multi_extra_splits_into_one_proxy_each(self) -> None:
        """``bar[a,b]`` under extra ``x`` records both proxies in that bucket."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {"x": {}}
        add_classified_dep(Requirement("bar[a,b]>=1.0"), {"x"}, base, extra_map, {})
        assert Version("2.0") in extra_map["x"]["bar"]
        assert extra_map["x"]["bar[a]"] == VersionRange.full(admit_arbitrary=False)
        assert extra_map["x"]["bar[b]"] == VersionRange.full(admit_arbitrary=False)
        assert "bar" not in base

    def test_multi_extra_proxy_names_normalized(self) -> None:
        """Proxy keys for the dep's extras are PEP 685 normalized."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {}
        add_classified_dep(Requirement("bar[A_x,B.y]"), set(), base, extra_map, {})
        assert "bar[a-x]" in base
        assert "bar[b-y]" in base

    def test_base_multi_extra_proxy_keys_sorted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Base-dep proxy keys come out sorted regardless of extras set order."""
        req = Requirement("bar[x,y,z]")
        monkeypatch.setattr(req, "extras", ["z", "y", "x"])
        base: dict[str, VersionRange] = {}
        add_classified_dep(req, set(), base, {}, {})
        assert list(base) == ["bar", "bar[x]", "bar[y]", "bar[z]"]

    def test_extra_gated_multi_extra_proxy_keys_sorted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A requirement such as bar[x,y,z] produces extra keys in sorted order."""
        req = Requirement("bar[x,y,z]")
        monkeypatch.setattr(req, "extras", ["z", "y", "x"])
        extra_map: dict[str, dict[str, VersionRange]] = {"g": {}}
        add_classified_dep(req, {"g"}, {}, extra_map, {})
        assert list(extra_map["g"]) == ["bar", "bar[x]", "bar[y]", "bar[z]"]


_EXTRAS_METADATA = {
    "1.0": (
        "Metadata-Version: 2.1\n"
        "Name: pkg\n"
        "Version: 1.0\n"
        "Provides-Extra: async\n"
        "Provides-Extra: Google_Auth\n"
        "Provides-Extra: all\n"
        "Requires-Dist: attrs>=22.1.0\n"
        'Requires-Dist: importlib-metadata>=6.5; python_version < "3.12"\n'
        'Requires-Dist: legacy; extra != "async"\n'
        'Requires-Dist: aiohttp>=3.9; extra == "async"\n'
        'Requires-Dist: aiohttp<4; extra == "async"\n'
        'Requires-Dist: google-auth>=2.15; extra == "google-auth"\n'
        'Requires-Dist: shared>=1; extra == "async" or extra == "all"\n'
        'Requires-Dist: pkg[async,google-auth]; extra == "all"\n'
        'Requires-Dist: rich>=12; extra == "all" and python_version >= "3.9"\n'
        'Requires-Dist: dataclasses; extra == "all" and python_version < "3.7"\n'
        'Requires-Dist: unlisted; extra == "no-such-extra"\n'
        'Requires-Dist: elsewhere; sys_platform == "nonesuch"\n'
    ),
    "2.0": (
        "Metadata-Version: 2.1\n"
        "Name: pkg\n"
        "Version: 2.0\n"
        "Provides-Extra: async\n"
        "Requires-Dist: attrs>=23\n"
        'Requires-Dist: anyio>=4; extra == "async"\n'
    ),
}
"""Two releases of one package whose extras differ."""


def _one_pass_extra_deps(
    provider: Provider, cache_key: tuple[str, Version]
) -> dict[str, dict[str, VersionRange]]:
    """Split a parsed release's requirements by extra in a single pass.

    The reference the lazily built maps are compared against: it classifies
    every requirement up front, through the same helpers the provider uses.
    """
    metadata = provider.metadata_cache[cache_key]
    provided: set[str] = {canonicalize_name(e) for e in metadata.provides_extra}
    base: dict[str, VersionRange] = {}
    extra_map: dict[str, dict[str, VersionRange]] = {e: {} for e in provided}
    for req in metadata.requires_dist:
        req_extras = classify_requirement(provider, req, provided)
        if req_extras is None or req.url is not None:
            continue
        add_classified_dep(req, req_extras, base, extra_map, provider.specifier_ranges)
    return extra_map


class TestLazyExtraDeps:
    """``extra_deps_map`` builds a release's per-extra deps when first read."""

    @staticmethod
    def _provider() -> Provider:
        coordinator = make_coordinator(
            [make_wheel("1.0"), make_wheel("2.0")],
            metadata_by_version=_EXTRAS_METADATA,
            package="pkg",
        )
        return Provider(coordinator, target=PYTHON_312)

    def test_matches_a_single_pass_split(self) -> None:
        """Each built map equals the one-pass split, key order included."""
        provider = self._provider()
        for version in ("1.0", "2.0"):
            cache_key = ("pkg", Version(version))
            provider.get_dependencies("pkg", Version(version))

            built = provider.extra_deps_map[cache_key]
            expected = _one_pass_extra_deps(provider, cache_key)

            assert built == expected
            assert list(built) == list(expected)
            for extra, deps in built.items():
                assert list(deps) == list(expected[extra])

    def test_extras_hold_their_own_deps(self) -> None:
        """The split lands each dep under the extras whose markers select it."""
        provider = self._provider()
        provider.get_dependencies("pkg", Version("1.0"))

        built = provider.extra_deps_map[("pkg", Version("1.0"))]

        assert set(built) == {"async", "google-auth", "all"}
        assert set(built["async"]) == {"aiohttp", "shared"}
        assert set(built["google-auth"]) == {"google-auth"}
        assert set(built["all"]) == {
            "pkg",
            "pkg[async]",
            "pkg[google-auth]",
            "rich",
            "shared",
        }

        aiohttp = built["async"]["aiohttp"]

        assert Version("3.9") in aiohttp
        assert Version("3.0") not in aiohttp
        assert Version("4.0") not in aiohttp

    def test_base_deps_do_not_wait_for_the_split(self) -> None:
        """The deps the base environment admits come straight from the parse."""
        provider = self._provider()

        base = provider.get_dependencies("pkg", Version("1.0"))

        assert set(base) == {"attrs", "legacy"}

    def test_key_absent_until_the_release_is_parsed(self) -> None:
        """A key missing means unparsed metadata, not a release without extras."""
        provider = self._provider()
        cache_key = ("pkg", Version("1.0"))

        assert cache_key not in provider.extra_deps_map
        assert provider.extra_deps_map.get(cache_key, {}) == {}

        provider.get_dependencies("pkg", Version("1.0"))

        assert cache_key in provider.extra_deps_map
        assert provider.extra_deps_map.get(cache_key, {}) != {}

    def test_no_marker_is_evaluated_against_an_extra_until_a_read(self) -> None:
        """Parsing leaves the split undone: no marker is bound to an extra yet."""
        provider = self._provider()
        provider.get_dependencies("pkg", Version("1.0"))

        assert provider.marker_extra_cache == {}

        assert provider.extra_deps_map[("pkg", Version("1.0"))]

        assert provider.marker_extra_cache != {}

    def test_membership_leaves_the_split_undone(self) -> None:
        """Asking whether a release is parsed must not build its split."""
        provider = self._provider()
        provider.get_dependencies("pkg", Version("1.0"))

        assert ("pkg", Version("1.0")) in provider.extra_deps_map

        assert provider.marker_extra_cache == {}

    def test_parsed_releases_enumerate_in_parse_order(self) -> None:
        """The mapping reports the releases parsed, newest parse last."""
        provider = self._provider()
        provider.get_dependencies("pkg", Version("2.0"))
        provider.get_dependencies("pkg", Version("1.0"))

        assert list(provider.extra_deps_map) == [
            ("pkg", Version("2.0")),
            ("pkg", Version("1.0")),
        ]
        assert len(provider.extra_deps_map) == 2

    def test_a_built_map_is_kept(self) -> None:
        """The split runs once per release however often the map is read."""
        provider = self._provider()
        provider.get_dependencies("pkg", Version("1.0"))

        first = provider.extra_deps_map[("pkg", Version("1.0"))]

        assert provider.extra_deps_map[("pkg", Version("1.0"))] is first

    def test_base_parse_consults_every_marker(self) -> None:
        """Parsing the release evaluates markers on both base and optional dependencies."""
        provider = self._provider()
        provider.get_dependencies("pkg", Version("1.0"))

        metadata = provider.metadata_cache[("pkg", Version("1.0"))]
        markers = {r.marker for r in metadata.requires_dist if r.marker is not None}

        assert provider.consulted_markers == markers

    def test_building_a_map_consults_no_further_markers(self) -> None:
        """Building the split records no marker the parse had not already."""
        provider = self._provider()
        provider.get_dependencies("pkg", Version("1.0"))
        consulted = set(provider.consulted_markers)

        assert provider.extra_deps_map[("pkg", Version("1.0"))]
        assert provider.consulted_markers == consulted

    def test_reading_the_map_does_not_keep_the_provider_alive(self) -> None:
        """A finished provider dies on its refcount, without waiting for a sweep."""
        provider = self._provider()
        provider.get_dependencies("pkg", Version("1.0"))
        assert provider.extra_deps_map[("pkg", Version("1.0"))]
        ref = weakref.ref(provider)

        gc.disable()
        try:
            del provider
            assert ref() is None
        finally:
            gc.enable()

    def test_deferred_url_dep_stays_out_of_the_split(self) -> None:
        """A URL dep under an unrequested extra is deferred, never a dep entry."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: pkg\n"
                "Version: 1.0\n"
                "Provides-Extra: uvloop\n"
                'Requires-Dist: bar @ https://example.com/bar.whl ; extra == "uvloop"\n'
                'Requires-Dist: baz>=1.0; extra == "uvloop"\n'
            ),
            package="pkg",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        provider.get_dependencies("pkg", Version("1.0"))

        assert set(provider.extra_deps_map[("pkg", Version("1.0"))]["uvloop"]) == {
            "baz"
        }
        assert provider.deferred_url_extras[("pkg", Version("1.0"))]["uvloop"]


class TestExtraRequirements:
    def test_base_deps_not_duplicated(self) -> None:
        """Extra deps don't include deps already in the base package."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=SECURITY_EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        base_deps = provider.get_dependencies("foo", Version("1.0"))
        assert "bar" in base_deps
        extra_deps = provider.get_dependencies("foo[security]", Version("1.0"))
        assert "bar" not in extra_deps

    def test_extra_deps_cached(self) -> None:
        """Second call returns cached extra deps."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=SECURITY_EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        provider.get_dependencies("foo[security]", Version("1.0"))
        provider.get_dependencies("foo[security]", Version("1.0"))
        assert ("foo[security]", Version("1.0")) in provider.deps_cache

    def test_marker_cache_hits_on_repeat(self) -> None:
        """Reclassifying the same Requirement hits the marker caches."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator, target=PYTHON_312)
        base_req = Requirement('bar; python_version >= "3.7"')
        extra_req = Requirement('qux; extra == "security"')

        classify_requirement(provider, base_req, {"security"})
        classify_requirement(provider, base_req, {"security"})
        classify_requirement(provider, extra_req, {"security"})
        classify_requirement(provider, extra_req, {"security"})

        assert provider.marker_base_cache[id(base_req.marker)] is True
        assert provider.marker_base_cache[id(extra_req.marker)] is False
        assert provider.marker_extra_cache[id(extra_req.marker)]["security"] is True

    def test_extra_marker_with_no_provided_extra_match(self) -> None:
        """Ignore a requirement whose marker matches none of the declared extras."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: security\n"
            'Requires-Dist: cryptography; extra == "missing-extra"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        base_deps = provider.get_dependencies("foo", Version("1.0"))
        sec_deps = provider.get_dependencies("foo[security]", Version("1.0"))
        assert "cryptography" not in base_deps
        assert "cryptography" not in sec_deps

    def test_multiple_extras_same_package(self) -> None:
        """Different extras on the same package get separate deps."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: security\n"
            "Provides-Extra: socks\n"
            'Requires-Dist: cryptography; extra == "security"\n'
            'Requires-Dist: pysocks; extra == "socks"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        sec_deps = provider.get_dependencies("foo[security]", Version("1.0"))
        socks_deps = provider.get_dependencies("foo[socks]", Version("1.0"))
        assert "cryptography" in sec_deps
        assert "pysocks" not in sec_deps
        assert "pysocks" in socks_deps
        assert "cryptography" not in socks_deps

    def test_conditional_base_dep_not_extra_gated(self) -> None:
        """A dep with an env marker (not extra) is a base dep."""
        # ``python_version >= "3.0"`` is true everywhere; ``sys_platform``
        # would tie the test to one host OS.
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: dev\n"
            'Requires-Dist: bar>=1.0; python_version >= "3.0"\n'
            'Requires-Dist: baz; extra == "dev"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        base_deps = provider.get_dependencies("foo", Version("1.0"))
        assert "bar" in base_deps
        extra_deps = provider.get_dependencies("foo[dev]", Version("1.0"))
        assert "baz" in extra_deps
        assert "bar" not in extra_deps

    def test_transitive_extras_propagated(self) -> None:
        """Deps with extras create proxy packages in the dep dict."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Requires-Dist: bar[baz]>=1.0\n"
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        deps = provider.get_dependencies("foo", Version("1.0"))
        assert "bar" in deps
        assert "bar[baz]" in deps

    def test_transitive_extras_in_extra_deps(self) -> None:
        """An optional requirement on another package includes its requested extras."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: all\n"
            'Requires-Dist: bar[http]>=1.0; extra == "all"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        deps = provider.get_dependencies("foo[all]", Version("1.0"))
        assert "bar" in deps
        assert "bar[http]" in deps
        assert "foo" in deps

    def test_extra_adds_proxy_already_in_base(self) -> None:
        """Extra still records the proxy even when base deps already include it."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: all\n"
            "Requires-Dist: bar[http]>=1.0\n"
            'Requires-Dist: bar[http]>=1.0; extra == "all"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        base_deps = provider.get_dependencies("foo", Version("1.0"))
        assert "bar[http]" in base_deps
        extra_deps = provider.get_dependencies("foo[all]", Version("1.0"))
        assert "bar[http]" in extra_deps

    def test_extra_tighter_constraint_than_base(self) -> None:
        """An extra can tighten a requirement already present on the base package."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: tight\n"
            "Requires-Dist: bar>=1.0\n"
            'Requires-Dist: bar>=2.0; extra == "tight"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        extra_deps = provider.get_dependencies("foo[tight]", Version("1.0"))
        assert "bar" in extra_deps
        assert Version("1.5") not in extra_deps["bar"]
        assert Version("2.5") in extra_deps["bar"]

    def test_extra_self_constraint_intersects_proxy_pin(self) -> None:
        """An extra that requires its own package must satisfy the selected version."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: bar\n"
            'Requires-Dist: foo>=2; extra == "bar"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        deps = provider.get_dependencies("foo[bar]", Version("1.0"))
        assert deps["foo"].is_empty

    def test_extra_self_constraint_compatible_keeps_pin(self) -> None:
        """A compatible requirement on the base preserves its selected version."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: bar\n"
            'Requires-Dist: foo>=1; extra == "bar"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        deps = provider.get_dependencies("foo[bar]", Version("1.0"))
        assert deps["foo"] == VersionRange.singleton(Version("1.0"))

    def test_extra_adds_proxy_for_existing_base_dep(self) -> None:
        """Extra adds proxy when base dep exists but doesn't have the extra."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: all\n"
            "Requires-Dist: bar>=1.0\n"
            'Requires-Dist: bar[http]>=1.0; extra == "all"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        base_deps = provider.get_dependencies("foo", Version("1.0"))
        assert "bar" in base_deps
        assert "bar[http]" not in base_deps  # base doesn't have the extra
        extra_deps = provider.get_dependencies("foo[all]", Version("1.0"))
        assert "bar[http]" in extra_deps  # extra adds the proxy

    def test_base_multi_extra_dep_creates_all_proxies(self) -> None:
        """A base ``bar[http,socks]`` dep yields both proxies and the base."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Requires-Dist: bar[http,socks]>=1.0\n"
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        deps = provider.get_dependencies("foo", Version("1.0"))
        assert "bar" in deps
        assert "bar[http]" in deps
        assert "bar[socks]" in deps

    def test_extra_gated_multi_extra_dep_creates_all_proxies(self) -> None:
        """An optional requirement on bar[http,socks] includes both extras."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: all\n"
            'Requires-Dist: bar[http,socks]>=1.0; extra == "all"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        deps = provider.get_dependencies("foo[all]", Version("1.0"))
        assert "bar" in deps
        assert "bar[http]" in deps
        assert "bar[socks]" in deps
        assert "foo" in deps

    def test_extra_dep_with_arbitrary_equality_is_literal_range(self) -> None:
        """Extra deps using ``===`` round-trip as a literal-only range."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: dev\n"
            'Requires-Dist: broken===custom; extra == "dev"\n'
            'Requires-Dist: good>=1.0; extra == "dev"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        deps = provider.get_dependencies("foo[dev]", Version("1.0"))
        assert "good" in deps
        assert "broken" in deps
        assert "custom" in deps["broken"]
        assert Version("1.0") not in deps["broken"]

    def test_extra_deps_already_listed(self) -> None:
        """No redundant listing submissions for already-fetched deps."""
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Provides-Extra: security\n"
            'Requires-Dist: cryptography; extra == "security"\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        provider.versions_cache["cryptography"] = [(Version("1.0"), make_wheel("1.0"))]
        deps = provider.get_dependencies("foo[security]", Version("1.0"))
        assert "cryptography" in deps
