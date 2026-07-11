"""Tests for ``Provider.widen_decision`` and ``Provider.narrow_for_display``.

The widening universe is the post-filter listing for the normalized base
package: ascending, including pre-release, dev, post, and local versions.
Local, VCS, and archive sources (synthesized single-version listings) and
packages with no cached listing are never widened, and display narrowing
reads caches only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_index.client import WheelFile
from nab_python._packaging_provider import PackagingProvider
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.version import Version
from nab_python.provider import (
    ArchiveSource,
    BuildPolicy,
    LocalSource,
    Provider,
    VcsSource,
)
from nab_resolver.resolver import Resolver
from nab_resolver.root import ROOT

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock

V = Version

_METADATA = "Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"


def _wheel(package: str, version: str) -> WheelFile:
    """Build a WheelFile for ``package`` at ``version``."""
    return WheelFile(
        filename=f"{package}-{version}-py3-none-any.whl",
        url=f"https://example.com/{package}-{version}-py3-none-any.whl",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )


def _graph_coordinator(graph: dict[str, dict[str, list[str]]]) -> MagicMock:
    """Coordinator for ``{package: {version: [Requires-Dist lines]}}``.

    Metadata is pre-stored per ``(package, version)`` so packages may
    share version strings.
    """
    listings = {
        package: [_wheel(package, version) for version in versions]
        for package, versions in graph.items()
    }
    coordinator = make_coordinator(listings=listings)
    for package, versions in graph.items():
        for version, requires in versions.items():
            text = _METADATA.format(name=package, version=version)
            for requirement in requires:
                text += f"Requires-Dist: {requirement}\n"
            coordinator.index.store_metadata(package, version, text + "\n")
    return coordinator


def _listing_provider(package: str, versions: list[str]) -> Provider:
    """Provider with ``package``'s listing already fetched into the cache."""
    coordinator = _graph_coordinator({package: {v: [] for v in versions}})
    provider = Provider(coordinator, python_version="3.12.0")
    provider.fetch_versions(package)
    return provider


class TestWidenDecision:
    def test_none_before_listing_is_cached(self) -> None:
        coordinator = _graph_coordinator({"p": {"1.0": []}})
        provider = Provider(coordinator, python_version="3.12.0")
        assert provider.widen_decision("p", V("1.0")) is None

    def test_universe_includes_prereleases(self) -> None:
        """The listed pre-release fences the widened interval; it must not
        be spanned even though the default filter buffers it."""
        provider = _listing_provider("p", ["2.0", "2.0b1", "1.0"])
        widened = provider.widen_decision("p", V("2.0"))
        assert widened is not None
        assert V("2.0") in widened
        assert V("2.0b1") not in widened
        assert V("1.0") not in widened
        assert V("3.0") in widened

    def test_universe_includes_locals(self) -> None:
        provider = _listing_provider("p", ["2.1", "2.0+cu118", "2.0"])
        widened = provider.widen_decision("p", V("2.0+cu118"))
        assert widened is not None
        assert V("2.0+cu118") in widened
        assert V("2.0") not in widened
        assert V("2.1") not in widened

    def test_extras_proxy_uses_base_universe(self) -> None:
        provider = _listing_provider("foo", ["3.0", "2.0", "1.0"])
        base = provider.widen_decision("foo", V("2.0"))
        proxy = provider.widen_decision("foo[bar]", V("2.0"))
        assert base is not None
        assert proxy is base

    def test_memoized_per_package_version(self) -> None:
        provider = _listing_provider("p", ["3.0", "2.0", "1.0"])
        first = provider.widen_decision("p", V("2.0"))
        second = provider.widen_decision("p", V("2.0"))
        assert first is not None
        assert second is first

    def test_single_listed_version_widens_to_full_bounds(self) -> None:
        provider = _listing_provider("p", ["1.5"])
        widened = provider.widen_decision("p", V("1.5"))
        assert widened is not None
        assert V("0.1") in widened
        assert V("99") in widened
        assert V("1.5rc1") in widened
        assert "garbage" not in widened

    def test_none_for_local_source(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.2.3"\n', encoding="utf-8"
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        version = provider.fetch_versions("foo")[0][0]
        assert provider.widen_decision("foo", version) is None

    def test_none_for_vcs_source(self) -> None:
        coordinator = make_coordinator([], package="bar")
        provider = Provider(coordinator, python_version="3.12.0")
        provider.vcs_sources["bar"] = VcsSource(
            "bar", "git+https://example.com/bar.git@abc123"
        )
        provider.versions_cache["bar"] = [(V("1.0"), _wheel("bar", "1.0"))]
        assert provider.widen_decision("bar", V("1.0")) is None

    def test_none_for_archive_source(self) -> None:
        coordinator = make_coordinator([], package="baz")
        provider = Provider(coordinator, python_version="3.12.0")
        provider.archive_sources["baz"] = ArchiveSource(
            "baz", "https://example.com/baz-1.0.tar.gz#sha256=00"
        )
        provider.versions_cache["baz"] = [(V("1.0"), _wheel("baz", "1.0"))]
        assert provider.widen_decision("baz", V("1.0")) is None


class TestNarrowForDisplay:
    def test_root_sentinel_returns_constraint_unchanged(self) -> None:
        provider = _listing_provider("p", ["1.0"])
        constraint = SpecifierSet(">=1").to_range()
        assert provider.narrow_for_display(ROOT, constraint) is constraint

    def test_narrows_widened_range_onto_listed_versions(self) -> None:
        provider = _listing_provider("p", ["3.0", "2.0", "1.0"])
        widened = provider.widen_decision("p", V("2.0"))
        assert widened is not None
        narrowed = provider.narrow_for_display("p", widened)
        assert V("2.0") in narrowed
        assert V("2.0.post1") not in narrowed
        assert V("2.5") not in narrowed

    def test_unfetched_package_returns_constraint_unchanged(self) -> None:
        provider = _listing_provider("p", ["1.0"])
        constraint = SpecifierSet(">=1,<2").to_range()
        assert provider.narrow_for_display("ghost", constraint) is constraint

    def test_never_triggers_a_fetch(self) -> None:
        coordinator = _graph_coordinator({"p": {"3.0": [], "2.0": [], "1.0": []}})
        provider = Provider(coordinator, python_version="3.12.0")
        provider.fetch_versions("p")
        coordinator.request_listing.side_effect = AssertionError(
            "narrow_for_display fetched a listing"
        )
        coordinator.request_listing.reset_mock()
        constraint = SpecifierSet(">=1,<2").to_range()
        assert provider.narrow_for_display("ghost", constraint) is constraint
        provider.narrow_for_display("p", VersionRange.full(admit_arbitrary=False))
        coordinator.request_listing.assert_not_called()


class TestPackagingProviderHooks:
    def test_widen_decision_returns_none(self) -> None:
        provider = PackagingProvider({"a": {V("1.0"): {}}})
        assert provider.widen_decision("a", V("1.0")) is None

    def test_narrow_for_display_is_identity(self) -> None:
        provider = PackagingProvider({"a": {V("1.0"): {}}})
        constraint = SpecifierSet(">=1").to_range()
        assert provider.narrow_for_display("a", constraint) is constraint


class TestWideningRegressionGraphs:
    """The prerelease-admission regression graphs stay green through the
    production Provider, which is where widening is active."""

    @staticmethod
    def _resolve(
        graph: dict[str, dict[str, list[str]]],
        root_reqs: dict[str, VersionRange],
    ) -> dict[str, Version]:
        coordinator = _graph_coordinator(graph)
        provider = Provider(
            coordinator, python_version="3.12.0", root_requirements=root_reqs
        )
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        return resolver.resolve(root_reqs)

    def test_rejected_parent_does_not_leak_prerelease_admission(self) -> None:
        """The prerelease-backtrack-leak graph: a rejected parent must not
        make an unrelated pre-release beat a final."""
        pins = self._resolve(
            {
                "a": {"1.0": ["c>=1.0"], "2.0": ["c>=2.0b1"]},
                "c": {"1.0": [], "1.5a1": [], "2.0b1": ["d==2.0"]},
                "d": {"1.0": [], "2.0": []},
            },
            {
                "a": VersionRange.full(),
                "d": SpecifierSet("==1.0").to_range(),
            },
        )
        assert pins["a"] == V("1.0")
        assert pins["d"] == V("1.0")
        assert pins["c"] == V("1.0")

    def test_exclusion_fallback_still_admits_prerelease(self) -> None:
        """The intended exclusion-fallback graph: when the only final is
        unusable, the surviving pre-release is admitted."""
        pins = self._resolve(
            {
                "a": {"1.0": [], "2.0": []},
                "c": {"1.0": ["d==2.0"], "1.5a1": [], "2.0b1": ["a>=2.0"]},
                "d": {"1.0": [], "2.0": ["a>=2.0b1"]},
            },
            {
                "a": SpecifierSet("==1.0").to_range(),
                "c": SpecifierSet(">=1.0").to_range(),
            },
        )
        assert pins["a"] == V("1.0")
        assert pins["c"] == V("1.5a1")

    def test_capped_prerelease_clip_does_not_leak(self) -> None:
        """The capped-prerelease clip graph: a failed parent's capped opt-in
        must not survive into c's range and beat the final 3.5."""
        pins = self._resolve(
            {
                "c": {"2.5": [], "3.5": [], "3.6b1": []},
                "a": {"2.5": ["c==2.5"], "3.6b1": []},
                "e": {"1.0": ["c!=2.5"], "4.0": ["c==2.0b1"]},
            },
            {
                "e": SpecifierSet("!=2.5").to_range(),
                "a": SpecifierSet(">=2.5").to_range(),
            },
        )
        assert pins["c"] == V("3.5")
