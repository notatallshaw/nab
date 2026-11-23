"""Artifact selection and overrides under provider distribution and metadata policies."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nab_provider._provider.metadata_resolver import cache_deps_from_metadata
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import OverrideConflictError
from nab_provider.metadata import WheelMetadata
from nab_provider.overrides import IndexOverride, PackageOverride
from nab_provider.policy import MetadataPolicy
from nab_provider.provider import (
    BuildPolicy,
    DistPolicy,
    ExtrasMode,
    ForeignMetadataError,
    IncompatiblePythonError,
    LocalSource,
    MetadataError,
    MissingExtraError,
    Provider,
    UnsupportedSdistError,
)
from nab_provider.records import SdistFile, WheelFile
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_sdist,
    make_wheel,
    pkg_override,
)

from .provider_fixtures import PYTHON_312 as _PY312

V = Version


class TestMetadataBlockerCount:
    """The metadata-error block lists versions, not look-ahead rejections."""

    _META = (
        "Metadata-Version: 2.1\nName: pkg\nVersion: {ver}\nRequires-Python: >=3.13\n\n"
    )

    def _provider(self, preferred: str | None) -> Provider:
        versions = ("1.0", "2.0", "3.0")
        coordinator = make_coordinator(
            [make_wheel(v) for v in versions],
            metadata_by_version={v: self._META.format(ver=v) for v in versions},
            package="pkg",
        )
        return Provider(
            coordinator,
            target=_PY312,
            root_requirements={"pkg": VersionRange.full(admit_arbitrary=False)},
            preferences=None if preferred is None else {"pkg": V(preferred)},
        )

    def test_lists_each_version_once_without_a_preference(self) -> None:
        """Three unusable versions read as three lines."""
        provider = self._provider(None)
        assert provider.choose_version("pkg", VersionRange.full()) is None
        diagnostic = provider.get_no_versions_reason("pkg")
        assert diagnostic is not None
        assert diagnostic.short == (
            "every version in range was rejected on its metadata"
        )
        assert len(diagnostic.detail) == 3
        assert "pkg 3.0 requires Python >=3.13" in diagnostic.detail[0]

    def test_preferred_version_is_not_counted_twice(self) -> None:
        """A preference re-checked by the full scan must not inflate the count.

        ``_preferred_version`` checks 2.0 first; when its metadata raises, the
        scan reaches 2.0 again.
        """
        provider = self._provider("2.0")
        assert provider.choose_version("pkg", VersionRange.full()) is None
        diagnostic = provider.get_no_versions_reason("pkg")
        assert diagnostic is not None
        assert len(diagnostic.detail) == 3
        # The version listed first is the first failure, here the preferred one.
        assert "pkg 2.0 requires Python >=3.13" in diagnostic.detail[0]


class TestEffectiveTrustUnverified:
    """``effective_trust_unverified`` reads overrides then the global."""

    def test_override_sets_trust(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            package_overrides=(pkg_override("foo", dist_trust_unverified_deps=True),),
        )
        assert provider.effective_trust_unverified("foo", V("1.0")) is True
        assert provider.effective_trust_unverified("bar", V("1.0")) is False

    def test_per_index_trust(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            index_overrides={
                "internal": IndexOverride(dist_trust_unverified_deps=True)
            },
        )
        assert provider.effective_trust_unverified("foo", V("1.0"), "internal") is True
        assert provider.effective_trust_unverified("foo", V("1.0"), "pypi") is False


class TestDependenciesOverride:
    """A per-package ``dependencies`` override replaces runtime deps."""

    def test_effective_dependencies_version_scoped(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            package_overrides=(
                pkg_override("foo <= 2", dependencies=(Requirement("bar>=1"),)),
            ),
        )
        in_range = provider.effective_dependencies("foo", V("1.0"))
        assert in_range is not None
        assert [str(r) for r in in_range] == ["bar>=1"]
        # Out of range and unknown package both fall through to None.
        assert provider.effective_dependencies("foo", V("5.0")) is None
        assert provider.effective_dependencies("other", V("1.0")) is None

    def test_effective_dependencies_none_without_override(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        assert provider.effective_dependencies("foo", V("1.0")) is None

    def test_substitution_replaces_base_deps(self) -> None:
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: original>=1\n"
            ),
            package="foo",
        )
        provider = Provider(
            coordinator,
            package_overrides=(
                pkg_override("foo", dependencies=(Requirement("replacement>=2"),)),
            ),
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "replacement" in deps
        assert "original" not in deps

    def test_empty_list_yields_zero_deps(self) -> None:
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: original>=1\n"
            ),
            package="foo",
        )
        provider = Provider(
            coordinator,
            package_overrides=(pkg_override("foo", dependencies=()),),
        )
        assert provider.get_dependencies("foo", V("1.0")) == {}

    def test_undeclared_extra_request_raises(self) -> None:
        # The override declares no ``provides-extra``, so the parsed
        # ``security`` extra no longer exists; requesting it raises.
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Provides-Extra: security\n"
                'Requires-Dist: cryptography>=2.0; extra == "security"\n'
            ),
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            extras_mode=ExtrasMode.ERROR_USER,
            root_extras={("foo", "security")},
            package_overrides=(
                pkg_override("foo", dependencies=(Requirement("bar>=1"),)),
            ),
        )
        with pytest.raises(MissingExtraError):
            provider.get_dependencies("foo[security]", V("1.0"))

    def test_funnel_does_not_mutate_shared_metadata(self) -> None:
        # The raw parse is shared across tuples via ``store_parsed_metadata``,
        # so the funnel must build a fresh record, never mutate the input.
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            package_overrides=(
                pkg_override("foo", dependencies=(Requirement("replacement>=2"),)),
            ),
        )
        shared = WheelMetadata(
            name="foo",
            version=V("1.0"),
            requires_dist=[Requirement("original>=1")],
            provides_extra=["security"],
        )
        cache_key = ("foo", V("1.0"))
        cache_deps_from_metadata(provider, cache_key, shared)
        # The shared input object is untouched.
        assert [str(r) for r in shared.requires_dist] == ["original>=1"]
        assert shared.provides_extra == ["security"]
        # The cached record is a fresh, overridden object.
        cached = provider.metadata_cache[cache_key]
        assert cached is not shared
        assert [str(r) for r in cached.requires_dist] == ["replacement>=2"]
        assert cached.provides_extra == []
        assert "replacement" in provider.deps_cache[cache_key]
        assert "original" not in provider.deps_cache[cache_key]


class TestEffectiveMetadataOverrideFields:
    """``effective_requires_python`` / ``effective_provides_extra`` lookups."""

    def test_requires_python_version_scoped(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            package_overrides=(pkg_override("foo <= 2", requires_python=">=3.6"),),
        )
        assert provider.effective_requires_python("foo", V("1.0")) == ">=3.6"
        assert provider.effective_requires_python("foo", V("5.0")) is None

    def test_requires_python_none_without_override(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        assert provider.effective_requires_python("foo", V("1.0")) is None

    def test_provides_extra_version_scoped(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            package_overrides=(pkg_override("foo <= 2", provides_extra=("dotenv",)),),
        )
        assert provider.effective_provides_extra("foo", V("1.0")) == ("dotenv",)
        assert provider.effective_provides_extra("foo", V("5.0")) is None

    def test_provides_extra_none_without_override(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        assert provider.effective_provides_extra("foo", V("1.0")) is None


class TestMetadataOverrideForSource:
    """Local/VCS sources select bare-name-only metadata overrides."""

    def _provider(self, *overrides: PackageOverride) -> Provider:
        coordinator = make_coordinator([], package="foo")
        return Provider(
            coordinator,
            local_sources=[LocalSource("foo", "/nonexistent")],
            package_overrides=overrides,
            build_policy=BuildPolicy.NEVER,
        )

    def test_bare_name_dependencies_apply(self) -> None:
        provider = self._provider(
            pkg_override("foo", dependencies=(Requirement("dep-a>=1"),))
        )
        deps, rp, pe = provider.effective_metadata_override("foo", V("1.0"))
        assert [str(r) for r in deps or ()] == ["dep-a>=1"]
        assert rp is None
        assert pe is None

    def test_version_scoped_does_not_apply(self) -> None:
        # A version-scoped metadata override does not govern a source: the
        # materialised version is not knowable when writing the selector.
        provider = self._provider(
            pkg_override("foo <= 2", dependencies=(Requirement("dep-a>=1"),))
        )
        assert provider.effective_metadata_override("foo", V("1.0")) == (
            None,
            None,
            None,
        )

    def test_other_package_does_not_apply(self) -> None:
        provider = self._provider(
            pkg_override("other", dependencies=(Requirement("dep-a>=1"),))
        )
        assert provider.effective_metadata_override("foo", V("1.0")) == (
            None,
            None,
            None,
        )

    def test_bare_name_setting_only_one_field(self) -> None:
        # A bare-name override that sets only requires-python leaves the other
        # two fields unset.
        provider = self._provider(pkg_override("foo", requires_python=">=3.6"))
        deps, rp, pe = provider.effective_metadata_override("foo", V("1.0"))
        assert deps is None
        assert rp == ">=3.6"
        assert pe is None

    def test_bare_name_setting_no_metadata_field(self) -> None:
        # A bare-name override that sets only a policy field contributes no
        # metadata override for the source.
        provider = self._provider(pkg_override("foo", build_policy=BuildPolicy.NEVER))
        assert provider.effective_metadata_override("foo", V("1.0")) == (
            None,
            None,
            None,
        )

    def test_provides_extra_applies(self) -> None:
        provider = self._provider(pkg_override("foo", provides_extra=("dotenv",)))
        deps, rp, pe = provider.effective_metadata_override("foo", V("1.0"))
        assert deps is None
        assert rp is None
        assert pe == ("dotenv",)

    def test_fields_from_different_bare_entries(self) -> None:
        # Two bare-name entries setting different fields: each field is
        # taken from the entry that sets it.
        provider = self._provider(
            pkg_override("foo", dependencies=(Requirement("dep-a>=1"),)),
            pkg_override("foo", requires_python=">=3.6"),
        )
        deps, rp, pe = provider.effective_metadata_override("foo", V("1.0"))
        assert [str(r) for r in deps or ()] == ["dep-a>=1"]
        assert rp == ">=3.6"
        assert pe is None

    def test_metadata_override_source_branch(self) -> None:
        provider = self._provider(
            pkg_override(
                "foo",
                dependencies=(Requirement("dep-a"),),
                requires_python=">=3.6",
                provides_extra=("dotenv",),
            )
        )
        deps, rp, pe = provider.effective_metadata_override("foo", V("1.0"))
        assert [str(r) for r in deps or ()] == ["dep-a"]
        assert rp == ">=3.6"
        assert pe == ("dotenv",)

    def test_metadata_override_index_branch(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            package_overrides=(
                pkg_override("foo <= 2", dependencies=(Requirement("dep-a"),)),
            ),
        )
        deps, rp, pe = provider.effective_metadata_override("foo", V("1.0"))
        assert [str(r) for r in deps or ()] == ["dep-a"]
        assert rp is None
        assert pe is None


class TestMetadataFunnelBundle:
    """The funnel replaces each of the three metadata fields per-field."""

    @staticmethod
    def _parsed() -> WheelMetadata:
        return WheelMetadata(
            name="foo",
            version=V("1.0"),
            requires_python=SpecifierSet(">=3.8"),
            requires_dist=[
                Requirement("original>=1"),
                Requirement('sec-dep; extra == "security"'),
            ],
            provides_extra=["security"],
        )

    def _apply(self, **body: object) -> Provider:
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            package_overrides=(pkg_override("foo", **body),),
        )
        cache_deps_from_metadata(provider, ("foo", V("1.0")), self._parsed())
        return provider

    def test_dependencies_only(self) -> None:
        # deps set, rp/pe unset: requires_dist replaced, requires_python
        # kept from parsed, provides_extra emptied.
        cached = self._apply(
            dependencies=(Requirement("replacement>=2"),)
        ).metadata_cache[("foo", V("1.0"))]
        assert [str(r) for r in cached.requires_dist] == ["replacement>=2"]
        assert cached.requires_python == SpecifierSet(">=3.8")
        assert cached.provides_extra == []

    def test_requires_python_only(self) -> None:
        # rp set, deps/pe unset: requires_python replaced, requires_dist and
        # provides_extra kept from parsed.  A non-deps override leaves the dep
        # list intact, so the parsed extras stay coherent and must survive.
        provider = self._apply(requires_python=">=3.6")
        cache_key = ("foo", V("1.0"))
        cached = provider.metadata_cache[cache_key]
        assert cached.requires_python == SpecifierSet(">=3.6")
        assert [str(r) for r in cached.requires_dist] == [
            "original>=1",
            'sec-dep; extra == "security"',
        ]
        assert cached.provides_extra == ["security"]
        # The parsed extra's dep is still reachable under that extra.
        assert "sec-dep" in provider.extra_deps_map[cache_key]["security"]

    def test_provides_extra_only(self) -> None:
        # pe set, deps/rp unset: provides_extra replaced, requires_dist and
        # requires_python kept from parsed.
        provider = self._apply(provides_extra=("security",))
        cached = provider.metadata_cache[("foo", V("1.0"))]
        assert cached.provides_extra == ["security"]
        assert cached.requires_python == SpecifierSet(">=3.8")
        assert [str(r) for r in cached.requires_dist] == [
            "original>=1",
            'sec-dep; extra == "security"',
        ]
        # The declared extra keeps its extra-markered dep.
        assert "sec-dep" in provider.extra_deps_map[("foo", V("1.0"))]["security"]

    def test_full_bundle_extras_coherent(self) -> None:
        provider = self._apply(
            dependencies=(
                Requirement("werkzeug>=0.14"),
                Requirement('click>=5.1; extra == "dotenv"'),
            ),
            requires_python=">=3.6",
            provides_extra=("dotenv",),
        )
        cache_key = ("foo", V("1.0"))
        cached = provider.metadata_cache[cache_key]
        assert cached.requires_python == SpecifierSet(">=3.6")
        assert cached.provides_extra == ["dotenv"]
        # werkzeug is a base dep; click lives under the declared dotenv extra.
        assert "werkzeug" in provider.deps_cache[cache_key]
        assert "click" not in provider.deps_cache[cache_key]
        assert "click" in provider.extra_deps_map[cache_key]["dotenv"]
        # The parsed ``security`` extra is gone (override is authoritative).
        assert "security" not in provider.extra_deps_map[cache_key]


class TestRequiresPythonListingGate:
    """``excluded_by_python`` consults the requires-python override."""

    def test_widen_admits_across_minor_boundary(self) -> None:
        # Real >=3.10 would exclude a 3.9 target; the override widens to
        # >=3.9, so the version is admitted.
        coordinator = make_coordinator(
            [make_wheel("1.0", requires_python=">=3.10")], package="foo"
        )
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.9.0"),
            package_overrides=(pkg_override("foo", requires_python=">=3.9"),),
        )
        assert [v for v, _ in provider.fetch_versions("foo")] == [V("1.0")]

    def test_narrow_rejects_across_minor_boundary(self) -> None:
        # Real >=3.6 would admit a 3.10 target; the override narrows to
        # >=3.11, so the version is rejected.
        coordinator = make_coordinator(
            [make_wheel("1.0", requires_python=">=3.6")], package="foo"
        )
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.10.0"),
            package_overrides=(pkg_override("foo", requires_python=">=3.11"),),
        )
        assert provider.fetch_versions("foo") == []

    def test_empty_specifier_admits(self) -> None:
        # An override of "" (no Python requirement) widens to admit anything.
        coordinator = make_coordinator(
            [make_wheel("1.0", requires_python=">=3.99")], package="foo"
        )
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.9.0"),
            package_overrides=(pkg_override("foo", requires_python=""),),
        )
        assert [v for v, _ in provider.fetch_versions("foo")] == [V("1.0")]

    def test_override_does_not_poison_shared_cache(self) -> None:
        # foo and bar both declare >=3.11 (same raw string). foo widens to
        # >=3.0; bar has no override. On 3.10, foo is admitted and bar stays
        # excluded.  The override shares the string-keyed cache under its own
        # ">=3.0" key, so it never poisons the ">=3.11" entry bar reads.
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("1.0", requires_python=">=3.11")],
                "bar": [make_wheel("1.0", requires_python=">=3.11")],
            },
        )
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.10.0"),
            package_overrides=(pkg_override("foo", requires_python=">=3.0"),),
        )
        assert [v for v, _ in provider.fetch_versions("foo")] == [V("1.0")]
        assert provider.fetch_versions("bar") == []
        assert provider.requires_python_cache == {">=3.0": False, ">=3.11": True}

    def test_override_without_python_version_not_excluded(self) -> None:
        # With no Python target the override cannot compare, so it does not
        # exclude the version.
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=None,
            package_overrides=(pkg_override("foo", requires_python=">=3.11"),),
        )
        assert len(provider.fetch_versions("foo")) == 1


class TestRequiresPythonMetadataGate:
    """Wheel METADATA can reject a candidate admitted by the listing.

    The Simple-API requires-python hint is optional, so a listing that omits
    it admits the version; the fetched METADATA carries the authoritative
    field, and ``get_dependencies`` rejects a candidate it marks incompatible.
    """

    @staticmethod
    def _coordinator(requires_python: str | None) -> FakeFetchPort:
        body = f"Requires-Python: {requires_python}\n" if requires_python else ""
        return make_coordinator(
            [make_wheel("1.0", requires_python=None)],
            package="foo",
            metadata_text=f"Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n{body}\n",
        )

    def test_excludes_when_listing_omits_hint(self) -> None:
        provider = Provider(
            self._coordinator(">=3.12"),
            target=ResolveTarget.for_host_python("3.8.0"),
        )
        with pytest.raises(IncompatiblePythonError, match="requires Python >=3.12"):
            provider.get_dependencies("foo", V("1.0"))

    def test_admits_when_metadata_python_is_compatible(self) -> None:
        provider = Provider(
            self._coordinator(">=3.8"),
            target=ResolveTarget.for_host_python("3.8.0"),
        )
        assert provider.get_dependencies("foo", V("1.0")) == {}

    def test_admits_when_metadata_omits_requires_python(self) -> None:
        provider = Provider(
            self._coordinator(None),
            target=ResolveTarget.for_host_python("3.8.0"),
        )
        assert provider.get_dependencies("foo", V("1.0")) == {}

    def test_no_target_skips_metadata_gate(self) -> None:
        provider = Provider(self._coordinator(">=3.99"), target=None)
        assert provider.get_dependencies("foo", V("1.0")) == {}

    def test_override_replaces_incompatible_metadata_python(self) -> None:
        provider = Provider(
            self._coordinator(">=3.12"),
            target=ResolveTarget.for_host_python("3.8.0"),
            package_overrides=(pkg_override("foo", requires_python=">=3.8"),),
        )
        assert provider.get_dependencies("foo", V("1.0")) == {}

    def test_rejection_caches_invalid_and_leaves_no_deps(self) -> None:
        provider = Provider(
            self._coordinator(">=3.12"),
            target=ResolveTarget.for_host_python("3.8.0"),
        )
        with pytest.raises(IncompatiblePythonError):
            provider.get_dependencies("foo", V("1.0"))
        assert ("foo", V("1.0")) not in provider.deps_cache
        assert provider.has_invalid_metadata("foo", V("1.0"))
        with pytest.raises(MetadataError, match="requires Python >=3.12"):
            provider.get_dependencies("foo", V("1.0"))

    def test_prefetch_batch_skips_incompatible_non_first_version(self) -> None:
        # The prefetch batch parses a non-first candidate's METADATA directly,
        # so an incompatible Requires-Python there must skip that version, not
        # abort the scan. 3.0 and 2.0 exclude the 3.8 target; the scan leaves
        # them un-cached and falls through to 1.0.
        def _meta(version: str, requires_python: str) -> str:
            return (
                f"Metadata-Version: 2.1\nName: foo\nVersion: {version}\n"
                f"Requires-Python: {requires_python}\n\n"
            )

        coordinator = make_coordinator(
            [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={
                "3.0": _meta("3.0", ">=3.99"),
                "2.0": _meta("2.0", ">=3.99"),
                "1.0": _meta("1.0", ">=3.8"),
            },
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.8.0"),
            root_requirements={"foo": VersionRange.full()},
        )
        assert provider.choose_version("foo", VersionRange.full()) == V("1.0")
        assert ("foo", V("2.0")) not in provider.deps_cache
        assert provider.has_invalid_metadata("foo", V("2.0"))


class TestForeignMetadataGate:
    """An index candidate's METADATA must declare the release it was served as.

    Otherwise a wheel (or its :pep:`658` sidecar) naming another release
    supplies that release's dependencies as the served candidate's.
    """

    @staticmethod
    def _coordinator(name: str, version: str = "1.0") -> FakeFetchPort:
        return make_coordinator(
            [make_wheel("1.0")],
            package="foo",
            metadata_text=(
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                "Requires-Dist: leftpad\n\n"
            ),
        )

    def test_rejects_foreign_name(self) -> None:
        provider = Provider(self._coordinator("bar"), target=_PY312)
        with pytest.raises(ForeignMetadataError, match="declares bar==1.0"):
            provider.get_dependencies("foo", V("1.0"))

    def test_rejects_foreign_version(self) -> None:
        provider = Provider(self._coordinator("foo", "99.0"), target=_PY312)
        with pytest.raises(ForeignMetadataError, match="declares foo==99.0"):
            provider.get_dependencies("foo", V("1.0"))

    def test_foreign_name_deps_never_cached(self) -> None:
        provider = Provider(self._coordinator("bar"), target=_PY312)
        with pytest.raises(ForeignMetadataError):
            provider.get_dependencies("foo", V("1.0"))
        assert ("foo", V("1.0")) not in provider.deps_cache
        assert ("foo", V("1.0")) not in provider.metadata_cache
        assert provider.has_invalid_metadata("foo", V("1.0"))
        with pytest.raises(MetadataError, match="declares bar==1.0"):
            provider.get_dependencies("foo", V("1.0"))

    def test_admits_normalized_name_and_version(self) -> None:
        """Case, separator, whitespace, and zero-padding differences still match."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            package="foo-bar",
            metadata_text=(
                "Metadata-Version: 2.1\nName:  Foo_Bar \nVersion: 1.0.0\n"
                "Requires-Dist: leftpad\n\n"
            ),
        )
        provider = Provider(coordinator, target=_PY312)
        assert "leftpad" in provider.get_dependencies("foo-bar", V("1.0"))

    def test_rejects_foreign_name_from_sdist_pkg_info(self) -> None:
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            package="foo",
            sdist_pkg_info=(
                "Metadata-Version: 2.2\nName: bar\nVersion: 1.0\n"
                "Requires-Dist: leftpad\n"
            ),
        )
        provider = Provider(coordinator, target=_PY312, build_policy=BuildPolicy.NEVER)
        with pytest.raises(ForeignMetadataError, match="declares bar==1.0"):
            provider.get_dependencies("foo", V("1.0"))

    def test_prefetch_batch_continues_past_foreign_metadata(self) -> None:
        """A rejected candidate the scan may never pick does not abort the batch."""

        def _meta(name: str, version: str) -> str:
            return f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"

        coordinator = make_coordinator(
            [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={
                "3.0": _meta("bar", "3.0"),
                "2.0": _meta("foo", "9.0"),
                "1.0": _meta("foo", "1.0"),
            },
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full()},
        )
        assert provider.choose_version("foo", VersionRange.full()) == V("1.0")
        assert ("foo", V("3.0")) not in provider.deps_cache
        assert ("foo", V("2.0")) not in provider.deps_cache


class TestEffectiveFieldResolution:
    """Per-package vs per-index resolution and cross-surface conflicts."""

    def test_per_package_outside_range_falls_through(self) -> None:
        # A version-scoped override does not apply outside its range; the
        # global default is used.
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            package_overrides=(
                pkg_override("foo <= 2", dist_policy=DistPolicy.SDIST_ONLY),
            ),
        )
        assert provider.effective_dist_policy("foo", V("1.0")) is DistPolicy.SDIST_ONLY
        assert (
            provider.effective_dist_policy("foo", V("5.0")) is DistPolicy.WHEEL_OR_SDIST
        )

    def test_per_index_applies_when_no_per_package(self) -> None:
        coordinator = make_coordinator([], package="foo")
        coordinator.index.store_listing_index("foo", "internal")
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            index_overrides={
                "internal": IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)
            },
        )
        assert (
            provider.effective_dist_policy("foo", V("1.0"), "internal")
            is DistPolicy.WHEEL_ONLY
        )

    def test_cross_surface_conflict_raises(self) -> None:
        # A per-package override whose range contains the version AND a
        # per-index override for the serving index both set dist-policy.
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            package_overrides=(
                pkg_override("foo <= 2", dist_policy=DistPolicy.SDIST_ONLY),
            ),
            index_overrides={
                "internal": IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)
            },
        )
        with pytest.raises(OverrideConflictError, match="override conflict for foo"):
            provider.effective_dist_policy("foo", V("1.0"), "internal")

    def test_cross_surface_no_conflict_outside_range(self) -> None:
        # The same package at a version OUTSIDE the per-package range: only
        # the per-index override applies, no conflict.
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            package_overrides=(
                pkg_override("foo <= 2", dist_policy=DistPolicy.SDIST_ONLY),
            ),
            index_overrides={
                "internal": IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)
            },
        )
        assert (
            provider.effective_dist_policy("foo", V("5.0"), "internal")
            is DistPolicy.WHEEL_ONLY
        )

    def test_conflict_propagates_through_filter(self) -> None:
        # The conflict is raised at the filter site, not swallowed as a
        # backtrack: a candidate version inside the range fails loud.
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        coordinator.index.store_listing_index("foo", "internal")
        provider = Provider(
            coordinator,
            package_overrides=(
                pkg_override("foo <= 2", dist_policy=DistPolicy.SDIST_ONLY),
            ),
            index_overrides={
                "internal": IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)
            },
        )
        with pytest.raises(OverrideConflictError):
            provider.fetch_versions("foo")

    def test_single_per_package_not_a_conflict(self) -> None:
        # One per-package override plus the global default never conflicts.
        coordinator = make_coordinator([], package="foo")
        coordinator.index.store_listing_index("foo", "internal")
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            package_overrides=(pkg_override("foo", dist_policy=DistPolicy.SDIST_ONLY),),
        )
        assert (
            provider.effective_dist_policy("foo", V("1.0"), "internal")
            is DistPolicy.SDIST_ONLY
        )

    def test_build_policy_skips_dist_only_override(self) -> None:
        # A dist-only override must not be read when querying build policy.
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            build_policy=BuildPolicy.BUILD_LOCAL,
            package_overrides=(
                pkg_override("foo", dist_policy=DistPolicy.SDIST_ONLY),
                pkg_override("foo", build_policy=BuildPolicy.BUILD_REMOTE),
            ),
        )
        assert (
            provider.effective_build_policy("foo", V("1.0")) is BuildPolicy.BUILD_REMOTE
        )

    def test_package_override_for_other_field_does_not_set_upload_time(self) -> None:
        # A package override that sets only dist-policy must not be read as
        # setting uploaded-prior-to; the global cutoff still applies.
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            uploaded_prior_to=cutoff,
            package_overrides=(pkg_override("foo", dist_policy=DistPolicy.SDIST_ONLY),),
        )
        assert provider.effective_uploaded_prior_to("foo", V("1.0")) == cutoff

    def test_per_index_upload_time_disabled(self) -> None:
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            uploaded_prior_to=cutoff,
            index_overrides={
                "internal": IndexOverride(uploaded_prior_to_disabled=True)
            },
        )
        assert provider.effective_uploaded_prior_to("foo", V("1.0"), "internal") is None

    def test_per_index_override_for_other_field_leaves_upload_time(self) -> None:
        # A per-index override that sets only dist-policy does not set
        # uploaded-prior-to; the global cutoff still applies.
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            uploaded_prior_to=cutoff,
            index_overrides={
                "internal": IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)
            },
        )
        assert (
            provider.effective_uploaded_prior_to("foo", V("1.0"), "internal") == cutoff
        )

    def test_build_policy_for_bare_name_source_override(self) -> None:
        # A bare-name build override governs a synthetic local/VCS source.
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            build_policy=BuildPolicy.NEVER,
            package_overrides=(
                pkg_override("foo", build_policy=BuildPolicy.BUILD_REMOTE),
            ),
        )
        assert (
            provider.effective_build_policy_for_source("foo")
            is BuildPolicy.BUILD_REMOTE
        )

    def test_build_policy_for_source_ignores_version_scoped_override(self) -> None:
        # A version-scoped build override does not govern a source decision.
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            build_policy=BuildPolicy.NEVER,
            package_overrides=(
                pkg_override("foo <= 2", build_policy=BuildPolicy.BUILD_REMOTE),
            ),
        )
        assert provider.effective_build_policy_for_source("foo") is BuildPolicy.NEVER


def _make_sdist(version: str, package: str = "foo") -> SdistFile:
    """Build a minimal :class:`SdistFile`."""
    return SdistFile(
        filename=f"{package}-{version}.tar.gz",
        url=f"https://example.com/{package}-{version}.tar.gz",
        version=version,
        requires_python=None,
        upload_time=None,
    )


class TestDistPolicyOverrides:
    """``SDIST_ONLY`` policy and per-package / per-index overrides on the filter."""

    def test_global_sdist_only_drops_wheels(self) -> None:
        files: list[WheelFile | SdistFile] = [
            make_wheel("1.0"),
            _make_sdist("1.0"),
        ]
        coordinator = make_coordinator(files, package="foo")
        provider = Provider(coordinator, dist_policy=DistPolicy.SDIST_ONLY)
        kept = provider.fetch_versions("foo")
        # Wheel filtered out; sdist remains.
        assert len(kept) == 1
        assert isinstance(kept[0][1], SdistFile)

    def test_override_sdist_only_for_one_package(self) -> None:
        # Global policy is wheel-or-sdist; override forces sdist for ``lxml`` only.
        files: list[WheelFile | SdistFile] = [
            make_wheel("1.0"),
            _make_sdist("1.0"),
        ]
        coordinator = make_coordinator(files, package="lxml")
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            package_overrides=(
                pkg_override("lxml", dist_policy=DistPolicy.SDIST_ONLY),
            ),
        )
        kept = provider.fetch_versions("lxml")
        assert len(kept) == 1
        assert isinstance(kept[0][1], SdistFile)

    def test_version_scoped_dist_policy(self) -> None:
        # ``lxml <= 2 -> sdist-only`` and ``lxml >= 3 -> wheel-only``: lxml
        # 1.0 resolves sdist-only, lxml 3.0 wheel-only, lxml 2.5 the global
        # default.  Prove behaviour at the filter site.
        files: list[WheelFile | SdistFile] = [
            make_wheel("3.0"),
            _make_sdist("3.0"),
            make_wheel("2.5"),
            _make_sdist("2.5"),
            make_wheel("1.0"),
            _make_sdist("1.0"),
        ]
        coordinator = make_coordinator(files, package="lxml")
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            package_overrides=(
                pkg_override("lxml <= 2", dist_policy=DistPolicy.SDIST_ONLY),
                pkg_override("lxml >= 3", dist_policy=DistPolicy.WHEEL_ONLY),
            ),
        )
        kept = provider.fetch_versions("lxml")
        by_version: dict[Version, list[WheelFile | SdistFile]] = {}
        for version, dist in kept:
            by_version.setdefault(version, []).append(dist)
        # 1.0 (<=2): sdist-only kept.
        assert [type(d).__name__ for d in by_version[V("1.0")]] == ["SdistFile"]
        # 3.0 (>=3): wheel-only kept.
        assert [type(d).__name__ for d in by_version[V("3.0")]] == ["WheelFile"]
        # 2.5 (no override): both kinds kept under the global default.
        assert {type(d).__name__ for d in by_version[V("2.5")]} == {
            "WheelFile",
            "SdistFile",
        }

    def test_override_does_not_apply_to_other_packages(self) -> None:
        # Override targets ``lxml``; ``foo`` keeps the global policy.
        files: list[WheelFile | SdistFile] = [
            make_wheel("1.0"),
            _make_sdist("1.0"),
        ]
        coordinator = make_coordinator(files, package="foo")
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            package_overrides=(
                pkg_override("lxml", dist_policy=DistPolicy.SDIST_ONLY),
            ),
        )
        kept = provider.fetch_versions("foo")
        # Both kept since ``foo`` is not the target of the override.
        assert len(kept) == 2

    def test_override_wheel_only_relaxes_global_sdist_only(self) -> None:
        # Global SDIST_ONLY drops wheels; override keeps them for ``foo``.
        files: list[WheelFile | SdistFile] = [
            make_wheel("1.0"),
            _make_sdist("1.0"),
        ]
        coordinator = make_coordinator(files, package="foo")
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.SDIST_ONLY,
            package_overrides=(pkg_override("foo", dist_policy=DistPolicy.WHEEL_ONLY),),
        )
        kept = provider.fetch_versions("foo")
        # Only the wheel survives; the override flips both directions.
        assert len(kept) == 1
        assert isinstance(kept[0][1], WheelFile)

    def test_per_index_dist_policy_takes_effect(self) -> None:
        # A package served from ``internal`` gets WHEEL_ONLY while the
        # global default keeps both kinds; proves per-index wiring.
        files: list[WheelFile | SdistFile] = [
            make_wheel("1.0"),
            _make_sdist("1.0"),
        ]
        coordinator = make_coordinator(files, package="foo")
        coordinator.index.store_listing_index("foo", "internal")
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            index_overrides={
                "internal": IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)
            },
        )
        kept = provider.fetch_versions("foo")
        assert len(kept) == 1
        assert isinstance(kept[0][1], WheelFile)

    def test_effective_dist_policy_lookup(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            package_overrides=(
                pkg_override("lxml", dist_policy=DistPolicy.SDIST_ONLY),
            ),
        )
        assert provider.effective_dist_policy("lxml", V("1.0")) is DistPolicy.SDIST_ONLY
        assert (
            provider.effective_dist_policy("foo", V("1.0")) is DistPolicy.WHEEL_OR_SDIST
        )

    def test_per_index_effective_lookup(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            index_overrides={
                "internal": IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)
            },
        )
        assert (
            provider.effective_dist_policy("foo", V("1.0"), "internal")
            is DistPolicy.WHEEL_ONLY
        )
        assert (
            provider.effective_dist_policy("foo", V("1.0"), "pypi")
            is DistPolicy.WHEEL_OR_SDIST
        )


SDIST_PKG_INFO = (
    "Metadata-Version: 2.2\n"
    "Name: pkg\n"
    "Version: 1.0\n"
    "Requires-Dist: dep-a>=1.0\n"
    "Requires-Dist: dep-b\n"
)


PRE_22_SDIST_PKG_INFO = (
    "Metadata-Version: 2.1\n"
    "Name: pkg\n"
    "Version: 1.0\n"
    "Requires-Dist: dep-a>=1.0\n"
    "Requires-Dist: dep-b\n"
)


class TestDistPolicy:
    def test_wheel_only_ignores_sdists(self) -> None:
        """WHEEL_ONLY policy filters out sdists from the listing."""
        coordinator = make_coordinator(
            [make_wheel("1.0"), make_sdist("0.9")],
            metadata_text="Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n",
        )
        provider = Provider(coordinator, dist_policy=DistPolicy.WHEEL_ONLY)
        versions = provider.fetch_versions("pkg")
        assert len(versions) == 1
        assert isinstance(versions[0][1], WheelFile)

    def test_allow_includes_sdists(self) -> None:
        """WHEEL_OR_SDIST policy includes both wheels and sdists."""
        coordinator = make_coordinator(
            [make_wheel("1.0"), make_sdist("0.9")],
            metadata_text="Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n",
        )
        provider = Provider(coordinator, dist_policy=DistPolicy.WHEEL_OR_SDIST)
        versions = provider.fetch_versions("pkg")
        assert len(versions) == 2

    def test_prefer_wheel_wheels_before_sdists(self) -> None:
        """PREFER_WHEEL sorts wheels before sdists at same version."""
        coordinator = make_coordinator(
            [make_sdist("1.0"), make_wheel("1.0")],
            metadata_text="Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n",
        )
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            metadata_policy=MetadataPolicy.PREFER_WHEEL,
        )
        versions = provider.fetch_versions("pkg")
        assert len(versions) == 2
        assert isinstance(versions[0][1], WheelFile)
        assert isinstance(versions[1][1], SdistFile)

    def test_prefer_wheel_wheels_before_sdists_under_overrides(self) -> None:
        """An override that leaves the policy alone keeps PREFER_WHEEL's order."""
        coordinator = make_coordinator(
            [make_sdist("1.0"), make_wheel("1.0")],
            metadata_text="Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n",
        )
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            metadata_policy=MetadataPolicy.PREFER_WHEEL,
            package_overrides=(pkg_override("pkg", provides_extra=("e",)),),
        )
        versions = provider.fetch_versions("pkg")
        assert len(versions) == 2
        assert isinstance(versions[0][1], WheelFile)
        assert isinstance(versions[1][1], SdistFile)

    def test_source_install_keeps_wheels_and_sorts_wheels_first(self) -> None:
        """source-only installation with wheel metadata behaves like WHEEL_OR_SDIST for the listing.

        Wheels stay in the listing (the resolver reads metadata from
        them), but they sort before the sdist at the same version so
        the metadata picker hits the cheapest source first.  The
        lockfile pin is what filters wheels out, not this stage.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0"), make_wheel("1.0")],
            metadata_text="Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n",
        )
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.SDIST_ONLY,
            metadata_policy=MetadataPolicy.PREFER_WHEEL,
        )
        versions = provider.fetch_versions("pkg")
        assert len(versions) == 2
        assert isinstance(versions[0][1], WheelFile)
        assert isinstance(versions[1][1], SdistFile)

    def test_source_install_drops_wheel_only_version(self) -> None:
        """A wheel-only version is not a candidate under source-only installation with wheel metadata.

        The newest release (2.0) publishes only a wheel, so it has no
        source to install and is dropped before the resolver sees it.
        The older 1.0 ships a wheel and an sdist, so it stays: the
        wheel as the cheap metadata source, sorted before the sdist.
        """
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0"), make_sdist("1.0")],
            metadata_text="Metadata-Version: 2.1\nName: pkg\nVersion: 2.0\n",
        )
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.SDIST_ONLY,
            metadata_policy=MetadataPolicy.PREFER_WHEEL,
        )
        versions = provider.fetch_versions("pkg")
        assert [str(v) for v, _ in versions] == ["1.0", "1.0"]
        assert isinstance(versions[0][1], WheelFile)
        assert isinstance(versions[1][1], SdistFile)

    def test_source_install_resolves_from_wheel_metadata(self) -> None:
        """A dynamic-deps sdist is not built when a wheel publishes the deps.

        The package ships both a wheel (with PEP 658 ``Requires-Dist:
        dep-a``) and an sdist whose PKG-INFO marks dependencies as
        Dynamic.  Under source-only installation with wheel metadata the resolver should pick the
        wheel's metadata text and skip the sdist build entirely.
        """
        coordinator = make_coordinator(
            [make_wheel("1.0"), make_sdist("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: dep-a\n"
            ),
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.SDIST_ONLY,
            metadata_policy=MetadataPolicy.PREFER_WHEEL,
        )
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps
        # No build invocation was needed: the dynamic-deps sdist was
        # ignored in favour of the wheel METADATA.
        assert provider.stats.excluded_by_build_policy == 0

    def test_sdist_deps_from_pkg_info(self) -> None:
        """Sdist dependencies extracted from PKG-INFO."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=SDIST_PKG_INFO,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps
        assert "dep-b" in deps

    def test_pre_22_sdist_deps_not_trusted(self) -> None:
        """A pre-2.2 sdist PKG-INFO is not PEP 643 static, so under NEVER
        its Requires-Dist surfaces UnsupportedSdistError instead of
        resolving against unverified declared deps.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PRE_22_SDIST_PKG_INFO,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pre_22_sdist_deps_trusted_with_opt_out(self) -> None:
        """The trust-unverified opt-out restores trusting a pre-2.2
        sdist's PKG-INFO Requires-Dist as final.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PRE_22_SDIST_PKG_INFO,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            trust_unverified_sdist_deps=True,
        )
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps
        assert "dep-b" in deps

    def test_opt_out_still_routes_explicit_dynamic_deps(self) -> None:
        """Even with the opt-out, an explicit PEP 643 Dynamic dependency
        field still forces the dynamic path; under NEVER it raises.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            trust_unverified_sdist_deps=True,
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_opt_out_still_routes_dynamic_provides_extra(self) -> None:
        """A Dynamic Provides-Extra also forces the dynamic path under the
        opt-out: the other DEPENDENCY_FIELDS member, so under NEVER it raises.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_PROVIDES_EXTRA,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            trust_unverified_sdist_deps=True,
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_per_package_override_trusts_pre_22_sdist_deps(self) -> None:
        """A per-package override body trusts pre-2.2 PKG-INFO deps through
        get_dependencies, just like the constructor flag.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PRE_22_SDIST_PKG_INFO,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            package_overrides=(pkg_override("pkg", dist_trust_unverified_deps=True),),
        )
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps
        assert "dep-b" in deps

    def test_per_index_override_trusts_pre_22_sdist_deps(self) -> None:
        """A per-index override body trusts the same deps for the served
        package, while a package off that index still raises under NEVER.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PRE_22_SDIST_PKG_INFO,
        )
        coordinator.index.store_listing_index("pkg", "internal")
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            index_overrides={
                "internal": IndexOverride(dist_trust_unverified_deps=True)
            },
        )
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps
        assert "dep-b" in deps

    def test_per_index_override_does_not_trust_off_index(self) -> None:
        """The per-index trust override does not extend to a package served
        from a different index, which still raises under NEVER.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PRE_22_SDIST_PKG_INFO,
        )
        coordinator.index.store_listing_index("pkg", "pypi")
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            index_overrides={
                "internal": IndexOverride(dist_trust_unverified_deps=True)
            },
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_cross_surface_trust_conflict_aborts_get_dependencies(self) -> None:
        """Matching per-package and per-index trust overrides raise
        OverrideConflictError instead of being swallowed as invalid metadata.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PRE_22_SDIST_PKG_INFO,
        )
        coordinator.index.store_listing_index("pkg", "internal")
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            package_overrides=(pkg_override("pkg", dist_trust_unverified_deps=True),),
            index_overrides={
                "internal": IndexOverride(dist_trust_unverified_deps=True)
            },
        )
        with pytest.raises(OverrideConflictError, match="override conflict for pkg"):
            provider.get_dependencies("pkg", V("1.0"))
        assert not provider.has_invalid_metadata("pkg", V("1.0"))

    def test_sdist_no_pkg_info_raises(self) -> None:
        """Report an unreadable sdist PKG-INFO, not a missing sdist."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=None,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(
            MetadataError, match="the sdist has no readable PKG-INFO"
        ) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))
        assert "no sdist available" not in str(excinfo.value)

    def test_no_pep658_with_no_dist_policy_raises(self) -> None:
        """Raise MetadataError when no PEP 658 and sdists disabled."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False)],
        )
        provider = Provider(coordinator, dist_policy=DistPolicy.WHEEL_ONLY)
        with pytest.raises(MetadataError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_no_pep658_inline_path_raises(self) -> None:
        """MetadataError when requesting version without PEP 658 metadata."""
        # Of the two versions, only v2 has metadata.
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0", has_metadata=False)],
        )
        provider = Provider(coordinator, dist_policy=DistPolicy.WHEEL_ONLY)
        provider.fetch_versions("pkg")
        # v1.0 has no metadata_url and sdists are disabled.
        with pytest.raises(MetadataError, match="no sdist"):
            provider.get_dependencies("pkg", V("1.0"))

    def test_wheel_no_pep658_falls_back_to_sdist(self) -> None:
        """When wheel has no PEP 658 metadata and sdists allowed, use sdist."""
        coordinator = make_coordinator(
            [
                make_wheel("2.0"),
                make_wheel("1.0", has_metadata=False),
                make_sdist("1.0"),
            ],
            sdist_pkg_info=SDIST_PKG_INFO,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        provider.fetch_versions("pkg")

        # Ask for v1.0 which has no PEP 658, should fall back to sdist.
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps

    def test_listing_includes_both_types(self) -> None:
        """A listing stores both wheels and sdists."""
        files = [make_wheel("1.0"), make_sdist("0.9")]
        coordinator = make_coordinator(files)
        provider = Provider(coordinator, dist_policy=DistPolicy.WHEEL_OR_SDIST)
        provider.fetch_versions("pkg")
        listing = coordinator.index.get_listing("pkg")
        assert listing is not None
        assert len(listing) == 2

    def test_sdist_tar_error_returns_none(self) -> None:
        """Tar extraction failure surfaces as MetadataError."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=None,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(MetadataError):
            provider.get_dependencies("pkg", V("1.0"))


PKG_INFO_DYNAMIC_DEPS = (
    "Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\nDynamic: Requires-Dist\n"
)


PKG_INFO_DYNAMIC_PROVIDES_EXTRA = (
    "Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\n"
    "Requires-Dist: dep-a\nDynamic: Provides-Extra\n"
)


class TestBuildPolicyDefaults:
    def test_default_dist_policy_is_allow(self) -> None:
        """Default dist policy now keeps sdists in the listing."""
        coordinator = make_coordinator([make_wheel("1.0"), make_sdist("0.9")])
        provider = Provider(coordinator)
        versions = provider.fetch_versions("pkg")
        assert len(versions) == 2

    def test_unsupported_sdist_error_is_metadata_error(self) -> None:
        """UnsupportedSdistError must be a subclass of MetadataError."""
        assert issubclass(UnsupportedSdistError, MetadataError)

    def test_build_policy_enum_values(self) -> None:
        """BuildPolicy has the expected three-level vocabulary."""
        assert BuildPolicy.NEVER.value == "never"
        assert BuildPolicy.BUILD_LOCAL.value == "build-local"
        assert BuildPolicy.BUILD_REMOTE.value == "build-remote"
