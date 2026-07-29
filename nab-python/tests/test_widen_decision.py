"""Tests for the ``Provider`` widening hooks and ``narrow_for_display``.

The widening universe is the post-filter listing for the normalized base
package: ascending, including pre-release, dev, post, and local versions.
``widen_decision`` spans base packages across adjacent versions with equal
cached deps before widening to the surrounding gap; under a lowest
preference the upward half of that span is capped and keeps the plain
neighbor gap. Extras proxies keep the pure neighbor gap, and
``widen_decision_gap`` keeps it for every package. Local, VCS, and
archive sources (synthesized single-version listings) and packages with
no cached listing are never widened, and display narrowing reads caches
only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

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
    ResolutionStrategy,
    VcsSource,
)
from nab_python.target import ResolveTarget
from nab_resolver.resolver import ResolutionError, Resolver
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
    provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.12.0"))
    provider.fetch_versions(package)
    return provider


def _deps_provider(
    graph: dict[str, dict[str, list[str]]],
    package: str,
    fetched: list[str],
    *,
    strategy: ResolutionStrategy = ResolutionStrategy.HIGHEST,
    direct: frozenset[str] | None = None,
) -> Provider:
    """Provider with ``package``'s listing and ``fetched`` versions' deps cached."""
    coordinator = _graph_coordinator(graph)
    provider = Provider(
        coordinator,
        target=ResolveTarget.for_host_python("3.12.0"),
        resolution_strategy=strategy,
        direct_packages=direct,
    )
    provider.fetch_versions(package)
    for version in fetched:
        provider.get_dependencies(package, V(version))
    return provider


class TestWidenDecision:
    def test_none_before_listing_is_cached(self) -> None:
        coordinator = _graph_coordinator({"p": {"1.0": []}})
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.12.0"))
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
        assert proxy is not None
        assert proxy == base
        assert V("2.0") in proxy
        assert V("2.5") in proxy
        assert V("1.0") not in proxy
        assert V("3.0") not in proxy

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
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.12.0"))
        provider.vcs_sources["bar"] = VcsSource(
            "bar", "git+https://example.com/bar.git@abc123"
        )
        provider.versions_cache["bar"] = [(V("1.0"), _wheel("bar", "1.0"))]
        assert provider.widen_decision("bar", V("1.0")) is None

    def test_none_for_archive_source(self) -> None:
        coordinator = make_coordinator([], package="baz")
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.12.0"))
        provider.archive_sources["baz"] = ArchiveSource(
            "baz", "https://example.com/baz-1.0.tar.gz#sha256=00"
        )
        provider.versions_cache["baz"] = [(V("1.0"), _wheel("baz", "1.0"))]
        assert provider.widen_decision("baz", V("1.0")) is None


_RUN_GRAPH = {
    "p": {
        "4.0": [],
        "3.0": ["d>=1"],
        "2.0": ["d>=1"],
        "1.0": ["d>=1"],
        "0.5": [],
    },
    "d": {"1.0": []},
}


class TestSameDepsSpan:
    """Base packages span adjacent versions with equal cached dependency
    dicts before widening to the open gap around the span.  These fixtures
    run the default HIGHEST strategy, which spans both ways."""

    def test_spans_cached_neighbors_with_equal_deps(self) -> None:
        provider = _deps_provider(_RUN_GRAPH, "p", ["0.5", "1.0", "2.0", "3.0", "4.0"])
        widened = provider.widen_decision("p", V("2.0"))
        assert widened is not None
        assert V("1.0") in widened
        assert V("2.0") in widened
        assert V("3.0") in widened
        assert V("0.7") in widened
        assert V("3.5") in widened
        assert V("0.5") not in widened
        assert V("4.0") not in widened

    def test_differing_deps_neighbor_fences(self) -> None:
        graph = {
            "p": {"3.0": ["d>=2"], "2.0": ["d>=1"], "1.0": ["d>=1"]},
            "d": {"2.0": [], "1.0": []},
        }
        provider = _deps_provider(graph, "p", ["1.0", "2.0", "3.0"])
        widened = provider.widen_decision("p", V("2.0"))
        assert widened is not None
        assert V("1.0") in widened
        assert V("2.0") in widened
        assert V("0.1") in widened
        assert V("2.5") in widened
        assert V("3.0") not in widened

    def test_uncached_neighbor_fences(self) -> None:
        graph = {
            "p": {"3.0": ["d>=1"], "2.0": ["d>=1"], "1.0": ["d>=1"]},
            "d": {"1.0": []},
        }
        provider = _deps_provider(graph, "p", ["1.0", "2.0"])
        widened = provider.widen_decision("p", V("2.0"))
        assert widened is not None
        assert V("1.0") in widened
        assert V("2.0") in widened
        assert V("3.0") not in widened

    def test_uncached_decided_version_keeps_gap(self) -> None:
        graph = {
            "p": {"3.0": ["d>=1"], "2.0": ["d>=1"], "1.0": ["d>=1"]},
            "d": {"1.0": []},
        }
        provider = _deps_provider(graph, "p", ["1.0", "3.0"])
        widened = provider.widen_decision("p", V("2.0"))
        assert widened is not None
        assert V("2.0") in widened
        assert V("1.0") not in widened
        assert V("3.0") not in widened

    def test_identical_run_covering_whole_listing_spans_fully(self) -> None:
        graph = {
            "p": {"3.0": ["d>=1"], "2.0": ["d>=1"], "1.0": ["d>=1"]},
            "d": {"1.0": []},
        }
        provider = _deps_provider(graph, "p", ["1.0", "2.0", "3.0"])
        widened = provider.widen_decision("p", V("2.0"))
        assert widened is not None
        assert V("0.1") in widened
        assert V("99") in widened

    def test_extras_proxy_keeps_gap_widening(self) -> None:
        graph = {
            "foo": {"3.0": ["d>=1"], "2.0": ["d>=1"], "1.0": ["d>=1"]},
            "d": {"1.0": []},
        }
        provider = _deps_provider(graph, "foo", ["1.0", "2.0", "3.0"])
        base = provider.widen_decision("foo", V("2.0"))
        proxy = provider.widen_decision("foo[bar]", V("2.0"))
        assert base is not None
        assert proxy is not None
        assert V("1.0") in base
        assert V("3.0") in base
        assert V("2.0") in proxy
        assert V("2.5") in proxy
        assert V("1.0") not in proxy
        assert V("3.0") not in proxy
        assert provider.widen_decision("foo[bar]", V("2.0")) is proxy

    def test_span_memo_returns_identical_object(self) -> None:
        provider = _deps_provider(_RUN_GRAPH, "p", ["0.5", "1.0", "2.0", "3.0", "4.0"])
        first = provider.widen_decision("p", V("2.0"))
        second = provider.widen_decision("p", V("2.0"))
        assert first is not None
        assert second is first

    def test_span_is_fixed_at_first_computation(self) -> None:
        graph = {
            "p": {"3.0": ["d>=1"], "2.0": ["d>=1"], "1.0": ["d>=1"]},
            "d": {"1.0": []},
        }
        provider = _deps_provider(graph, "p", ["1.0", "2.0"])
        first = provider.widen_decision("p", V("2.0"))
        assert first is not None
        assert V("3.0") not in first
        provider.get_dependencies("p", V("3.0"))
        second = provider.widen_decision("p", V("2.0"))
        assert second is not None
        assert second is first
        assert V("3.0") not in second

    def test_span_never_fetches(self) -> None:
        provider = _deps_provider(_RUN_GRAPH, "p", ["0.5", "1.0", "2.0", "3.0", "4.0"])
        coordinator = cast("MagicMock", provider.coordinator)
        for method in (
            coordinator.request_listing,
            coordinator.request_metadata,
            coordinator.request_metadata_batch,
        ):
            method.side_effect = AssertionError("widen_decision fetched")
        assert provider.widen_decision("p", V("2.0")) is not None


_SPLIT_GRAPH = {
    "p": {"4.0": [], "3.0": ["d>=1"], "2.0": ["d>=1"], "1.0": ["d>=1"], "0.5": []},
    "q": {"4.0": [], "3.0": ["d>=1"], "2.0": ["d>=1"], "1.0": ["d>=1"], "0.5": []},
    "d": {"1.0": []},
}


class TestSpanDirection:
    """A lowest preference caps the upward half of the span; every other
    package spans both ways.  ``wants_lowest`` is the same per-package
    answer ``choose_version`` orders candidates by."""

    def test_lowest_caps_the_upward_half(self) -> None:
        provider = _deps_provider(
            _RUN_GRAPH,
            "p",
            ["0.5", "1.0", "2.0", "3.0", "4.0"],
            strategy=ResolutionStrategy.LOWEST,
        )
        widened = provider.widen_decision("p", V("2.0"))
        assert widened is not None
        assert V("1.0") in widened
        assert V("2.0") in widened
        assert V("2.5") in widened
        assert V("3.0") not in widened
        assert V("0.5") not in widened

    def test_highest_spans_both_ways(self) -> None:
        provider = _deps_provider(
            _RUN_GRAPH,
            "p",
            ["0.5", "1.0", "2.0", "3.0", "4.0"],
            strategy=ResolutionStrategy.HIGHEST,
        )
        widened = provider.widen_decision("p", V("2.0"))
        assert widened is not None
        assert V("1.0") in widened
        assert V("2.0") in widened
        assert V("3.0") in widened
        assert V("0.5") not in widened
        assert V("4.0") not in widened

    def test_lowest_span_reaching_the_floor_drops_the_lower_bound(self) -> None:
        graph = {
            "p": {"3.0": [], "2.0": ["d>=1"], "1.0": ["d>=1"]},
            "d": {"1.0": []},
        }
        provider = _deps_provider(
            graph, "p", ["1.0", "2.0", "3.0"], strategy=ResolutionStrategy.LOWEST
        )
        widened = provider.widen_decision("p", V("2.0"))
        assert widened is not None
        assert V("0.1") in widened
        assert V("1.0") in widened
        assert V("3.0") not in widened

    def test_lowest_direct_caps_direct_packages_only(self) -> None:
        provider = _deps_provider(
            _SPLIT_GRAPH,
            "p",
            ["0.5", "1.0", "2.0", "3.0", "4.0"],
            strategy=ResolutionStrategy.LOWEST_DIRECT,
            direct=frozenset({"p"}),
        )
        provider.fetch_versions("q")
        for version in ("0.5", "1.0", "2.0", "3.0", "4.0"):
            provider.get_dependencies("q", V(version))

        direct = provider.widen_decision("p", V("2.0"))
        transitive = provider.widen_decision("q", V("2.0"))
        assert direct is not None
        assert transitive is not None
        assert V("1.0") in direct
        assert V("3.0") not in direct
        assert V("1.0") in transitive
        assert V("3.0") in transitive

    def test_memo_holds_one_shape_per_provider(self) -> None:
        fetched = ["0.5", "1.0", "2.0", "3.0", "4.0"]
        lowest = _deps_provider(
            _RUN_GRAPH, "p", fetched, strategy=ResolutionStrategy.LOWEST
        )
        highest = _deps_provider(_RUN_GRAPH, "p", fetched)
        low = lowest.widen_decision("p", V("2.0"))
        high = highest.widen_decision("p", V("2.0"))
        assert low is not None
        assert high is not None
        assert low != high
        assert lowest.widen_decision("p", V("2.0")) is low
        assert highest.widen_decision("p", V("2.0")) is high

    def test_gap_path_is_direction_free(self) -> None:
        fetched = ["0.5", "1.0", "2.0", "3.0", "4.0"]
        lowest = _deps_provider(
            _RUN_GRAPH, "p", fetched, strategy=ResolutionStrategy.LOWEST
        )
        highest = _deps_provider(_RUN_GRAPH, "p", fetched)
        gap = lowest.widen_decision_gap("p", V("2.0"))
        assert gap is not None
        assert gap == highest.widen_decision_gap("p", V("2.0"))
        assert V("2.0") in gap
        assert V("1.0") not in gap
        assert V("3.0") not in gap


class TestGapSpanComposition:
    """``widen_decision`` spans equal-deps runs for decided parent terms
    while look-ahead version terms keep pure gaps via
    ``widen_decision_gap``."""

    def test_run_member_spans_parent_but_lookahead_keeps_gaps(self) -> None:
        graph = {
            "foo": {
                "3.0": ["bar>=5.0"],
                "2.0": ["bar>=5.0"],
                "1.0": ["bar>=5.0"],
                "0.5": ["bar>=5.0"],
            },
            "bar": {"5.0": [], "3.0": [], "1.0": []},
        }
        coordinator = _graph_coordinator(graph)
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.12.0"))
        provider.fetch_versions("foo")
        provider.fetch_versions("bar")
        for version in ("0.5", "1.0", "2.0", "3.0"):
            provider.get_dependencies("foo", V(version))
        for version in ("1.0", "3.0", "5.0"):
            provider.get_dependencies("bar", V(version))
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        assert provider.choose_version("foo", SpecifierSet(">=1.0").to_range()) is None
        span = provider.widen_decision("foo", V("1.0"))
        assert span is not None
        assert V("0.5") in span
        assert V("3.0") in span
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        candidate, blocker = clauses[0].terms
        assert candidate.package == "foo"
        assert V("1.0") in candidate.constraint
        assert V("2.0") in candidate.constraint
        assert V("3.0") in candidate.constraint
        assert V("0.5") not in candidate.constraint
        assert blocker.package == "bar"
        assert V("3.0") in blocker.constraint
        # Every scanned foo version requires bar>=5.0, so the widening reaches
        # past the gap to bar 1.0, which blocks them all; bar 5.0 satisfies
        # them and stays out.
        assert V("1.0") in blocker.constraint
        assert V("5.0") not in blocker.constraint

    def test_gap_path_ignores_spans_for_base_packages(self) -> None:
        provider = _deps_provider(_RUN_GRAPH, "p", ["0.5", "1.0", "2.0", "3.0", "4.0"])
        span = provider.widen_decision("p", V("2.0"))
        gap = provider.widen_decision_gap("p", V("2.0"))
        assert span is not None
        assert gap is not None
        assert V("1.0") in span
        assert V("3.0") in span
        assert V("2.0") in gap
        assert V("1.0") not in gap
        assert V("3.0") not in gap

    def test_extras_proxy_gets_gaps_in_both_paths(self) -> None:
        provider = _deps_provider(_RUN_GRAPH, "p", ["0.5", "1.0", "2.0", "3.0", "4.0"])
        proxy = provider.widen_decision("p[x]", V("2.0"))
        gap = provider.widen_decision_gap("p[x]", V("2.0"))
        assert proxy is not None
        assert gap is proxy
        assert V("2.0") in proxy
        assert V("1.0") not in proxy
        assert V("3.0") not in proxy
        assert provider.widen_decision_gap("p", V("2.0")) is proxy

    def test_gap_path_none_before_listing_is_cached(self) -> None:
        coordinator = _graph_coordinator({"p": {"1.0": []}})
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.12.0"))
        assert provider.widen_decision_gap("p", V("1.0")) is None


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

    def test_full_coverage_promotes_to_full_range(self) -> None:
        provider = _listing_provider("p", ["3.0", "2.0", "1.0"])
        constraint = SpecifierSet(">=0.5,<9").to_range()
        assert provider.narrow_for_display("p", constraint) == VersionRange.full(
            admit_arbitrary=False
        )

    def test_multi_segment_full_coverage_promotes(self) -> None:
        """A hole between listed versions still covers the whole listing."""
        provider = _listing_provider("p", ["3.0", "2.0", "1.0"])
        constraint = SpecifierSet(">=0.5,<9,!=2.5").to_range()
        assert provider.narrow_for_display("p", constraint) == VersionRange.full(
            admit_arbitrary=False
        )

    def test_excluded_middle_version_does_not_promote(self) -> None:
        provider = _listing_provider("p", ["3.0", "2.0", "1.0"])
        constraint = SpecifierSet(">=0.5,<9,!=2.0").to_range()
        narrowed = provider.narrow_for_display("p", constraint)
        assert V("1.0") in narrowed
        assert V("2.0") not in narrowed
        assert V("3.0") in narrowed
        assert V("9.5") not in narrowed

    def test_excluded_top_version_does_not_promote(self) -> None:
        provider = _listing_provider("p", ["3.0", "2.0", "1.0"])
        narrowed = provider.narrow_for_display("p", SpecifierSet("<3.0").to_range())
        assert V("2.0") in narrowed
        assert V("3.0") not in narrowed

    def test_excluded_bottom_version_does_not_promote(self) -> None:
        provider = _listing_provider("p", ["3.0", "2.0", "1.0"])
        narrowed = provider.narrow_for_display("p", SpecifierSet(">1.0").to_range())
        assert V("1.0") not in narrowed
        assert V("3.0") in narrowed

    def test_empty_universe_does_not_promote(self) -> None:
        provider = _listing_provider("p", ["1.0"])
        provider.versions_cache["empty"] = []
        constraint = SpecifierSet(">=1,<2").to_range()
        assert provider.narrow_for_display("empty", constraint) == constraint

    def test_availability_line_keeps_its_range(self) -> None:
        """``a`` is rejected only against pinned ``c``, so the line keeps its range."""
        coordinator = _graph_coordinator(
            {
                "a": {"2.0": ["c==1.0"], "1.0": ["c==1.0"]},
                "c": {"5.0": [], "1.0": []},
            }
        )
        root_reqs = {
            "a": SpecifierSet(">=1,<9").to_range(),
            "c": SpecifierSet("==5.0").to_range(),
        }
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.12.0"),
            root_requirements=root_reqs,
        )
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(dict(root_reqs))

        expected = f"because no versions of a {root_reqs['a']} are available"
        assert expected in str(exc_info.value).splitlines()

    def test_promoted_parent_reads_as_all_versions(self) -> None:
        """A promoted depending side takes the plural prose and verb."""
        coordinator = _graph_coordinator(
            {
                "a": {"2.0": ["c>=2"], "1.0": ["c>=2"]},
                "c": {"1.0": []},
            }
        )
        root_reqs = {"a": SpecifierSet(">=1,<9").to_range()}
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.12.0"),
            root_requirements=root_reqs,
        )
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(dict(root_reqs))

        lines = str(exc_info.value).splitlines()
        assert lines[0].startswith("because all versions of a depend on c ")
        assert "so all versions of a" in lines

    def test_never_triggers_a_fetch(self) -> None:
        coordinator = _graph_coordinator({"p": {"3.0": [], "2.0": [], "1.0": []}})
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.12.0"))
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
            coordinator,
            target=ResolveTarget.for_host_python("3.12.0"),
            root_requirements=root_reqs,
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
