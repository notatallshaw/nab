"""Candidate selection, release ordering and artifact lookup by the provider."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from packaging.ranges import VersionRange
from packaging.specifiers import SpecifierSet
from packaging.tags import Tag
from packaging.version import Version

from nab_provider._provider import listing as listing_mod
from nab_provider._provider import listing_diagnosis as diagnosis_mod
from nab_provider._provider import metadata_resolver
from nab_provider._provider.metadata_resolver import (
    dists_at_version,
    pick_dist_for_metadata,
)
from nab_provider.provider import (
    BuildPolicy,
    LocalSource,
    Provider,
    ResolutionStrategy,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from nab_provider.records import DistFile, SdistFile, WheelFile
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator, make_metadata, make_sdist, make_wheel
from nab_resolver.resolver import Resolver

from .provider_fixtures import PYTHON_312 as _PY312
from .provider_fixtures import short_reason

V = Version


class _CountingVersion(Version):
    """Count comparisons, including reflected comparisons from listing entries."""

    def __init__(self, version: str) -> None:
        super().__init__(version)
        self.comparisons = 0

    def __lt__(self, other: Version) -> bool:
        self.comparisons += 1
        return super().__lt__(other)

    def __le__(self, other: Version) -> bool:
        self.comparisons += 1
        return super().__le__(other)

    def __eq__(self, other: object) -> bool:
        self.comparisons += 1
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        self.comparisons += 1
        return super().__ne__(other)

    def __gt__(self, other: Version) -> bool:
        self.comparisons += 1
        return super().__gt__(other)

    def __ge__(self, other: Version) -> bool:
        self.comparisons += 1
        return super().__ge__(other)

    # Defining ``__eq__`` would otherwise drop the inherited hash.
    __hash__ = Version.__hash__


def _one_wheel_releases(count: int) -> list[tuple[Version, DistFile]]:
    """A newest-first listing of ``count`` releases, one wheel each."""
    return [(V(f"{n}.0"), make_wheel(f"{n}.0")) for n in range(count, 0, -1)]


def _wheel_and_sdist_releases(count: int) -> list[tuple[Version, DistFile]]:
    """A newest-first listing of ``count`` releases, a wheel then an sdist each."""
    listing: list[tuple[Version, DistFile]] = []
    for n in range(count, 0, -1):
        listing.append((V(f"{n}.0"), make_wheel(f"{n}.0")))
        listing.append((V(f"{n}.0"), make_sdist(f"{n}.0")))
    return listing


def _blocker_reason(
    *blockers: diagnosis_mod.Blocker,
) -> diagnosis_mod.NoVersionsReason:
    """A look-ahead marker, the reason tests seed when one must name a cause."""
    return diagnosis_mod.NoVersionsReason(
        diagnosis_mod.ReasonKind.BLOCKERS, blockers=blockers
    )


class _CountingListing(list[Version]):
    """A version listing that counts how many of its entries are read."""

    def __init__(self, versions: list[Version]) -> None:
        super().__init__(versions)
        self.reads = 0

    def __getitem__(self, index: Any) -> Any:
        self.reads += 1
        return super().__getitem__(index)

    def __iter__(self) -> Iterator[Version]:
        for version in super().__iter__():
            self.reads += 1
            yield version


class TestChooseVersion:
    def test_picks_newest_in_range(self) -> None:
        """choose_version returns the newest version passing the filter."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0,<3.0")
        assert provider.choose_version("foo", spec.to_range()) == V("2.0")

    def test_accepted_pick_leaves_the_listing_tail_unread(self) -> None:
        """An accepted look-ahead pick returns without reading the tail."""
        versions = [f"1.{minor}" for minor in range(60)]
        coordinator = make_coordinator(
            [make_wheel(v) for v in versions],
            metadata_by_version={
                v: make_metadata("foo", v, "bar>=1.0") for v in versions
            },
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        # The decided bar==3.0 satisfies bar>=1.0, so look-ahead accepts the pick.
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})

        listing = _CountingListing(
            provider.versions_only("foo", provider.fetch_versions("foo"))
        )
        provider.versions_only_cache["foo"] = listing

        assert provider.choose_version("foo", VersionRange.full()) == V("1.59")

        assert listing.reads == 1

    def test_returns_none_when_no_match(self) -> None:
        """choose_version returns None when nothing matches."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=5.0")
        assert provider.choose_version("foo", spec.to_range()) is None

    def test_returns_none_for_empty_index(self) -> None:
        """choose_version returns None for a package with no versions."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) is None

    _PREFERENCE_META = "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n\n"

    def _preference_provider(
        self, preferred: str, versions: tuple[str, ...] = ("1.0", "2.0", "3.0")
    ) -> Provider:
        coordinator = make_coordinator(
            [make_wheel(v) for v in versions],
            metadata_by_version={
                v: self._PREFERENCE_META.format(ver=v) for v in versions
            },
            package="foo",
        )
        return Provider(coordinator, target=_PY312, preferences={"foo": V(preferred)})

    def test_a_preference_inside_the_range_wins(self) -> None:
        """The membership walk stops at the preference wherever it sits."""
        provider = self._preference_provider("1.0")
        spec = SpecifierSet(">=1.0")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_a_preference_outside_the_range_is_dropped(self) -> None:
        """A preference the range excludes falls through to the strategy."""
        provider = self._preference_provider("1.0")
        spec = SpecifierSet(">=2.0")
        assert provider.choose_version("foo", spec.to_range()) == V("3.0")

    def test_a_preference_the_index_never_listed_is_dropped(self) -> None:
        """A preference absent from the listing exhausts the walk and loses."""
        provider = self._preference_provider("9.9")
        spec = SpecifierSet(">=1.0")
        assert provider.choose_version("foo", spec.to_range()) == V("3.0")

    def test_a_preference_buffered_out_by_the_default_policy_is_dropped(self) -> None:
        """The membership question honours the filter's pre-release policy."""
        provider = self._preference_provider("2.0a1", ("1.0", "2.0a1", "2.0"))
        spec = SpecifierSet(">=1.0")
        assert provider.choose_version("foo", spec.to_range()) == V("2.0")


class TestHasSatisfyingVersion:
    def test_true_when_a_version_is_in_range(self) -> None:
        """A candidate inside the range reports True."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        assert provider.has_satisfying_version("foo", SpecifierSet(">=1.0").to_range())

    def test_false_when_no_version_in_range(self) -> None:
        """No candidate inside the range reports False."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        assert not provider.has_satisfying_version(
            "foo", SpecifierSet(">=5.0").to_range()
        )

    def test_false_for_empty_index(self) -> None:
        """A package with no versions reports False."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        assert not provider.has_satisfying_version("foo", VersionRange.full())

    def test_restores_recorded_no_versions_reason(self) -> None:
        """The probe reverts the no-versions reason its own scan records.

        A miss over ``>=5.0`` would otherwise overwrite the stored reason for
        ``foo``; the restore keeps the pre-probe value.
        """
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        provider._no_versions_reasons["foo"] = _blocker_reason(
            diagnosis_mod.Blocker(
                diagnosis_mod.BlockerKind.DECIDED,
                "bar",
                (SpecifierSet("==2.0").to_range(),),
                V("1.0"),
            )
        )
        assert not provider.has_satisfying_version(
            "foo", SpecifierSet(">=5.0").to_range()
        )
        assert short_reason(provider, "foo") == (
            "every version in range needs bar in ==2.0, but the resolve chose bar 1.0"
        )

    def test_false_when_every_candidate_hits_the_abort_blocker(self) -> None:
        """The look-ahead abort's optimistic pick is not a satisfying version.

        Every candidate in range is rejected by one decided blocker, tripping
        ``choose_version``'s monolithic-rejection abort, which returns the first
        candidate for the resolver to decide and back-jump.  The probe must
        report that no usable version exists.
        """
        versions = [f"{n}.0" for n in range(8, 0, -1)]
        wheels = [make_wheel(v) for v in versions]
        meta_template = "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar==9.0\n"
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("5.0")})
        # choose_version takes the abort shortcut and returns the first pick.
        assert provider.choose_version("foo", VersionRange.full()) == V("8.0")

        assert not provider.has_satisfying_version("foo", VersionRange.full())

    def test_true_when_a_usable_version_sits_past_the_abort_threshold(self) -> None:
        """Suppressing the abort still finds a usable version deep in the scan.

        The newest eight candidates are rejected by the decided blocker and the
        trigger is pinned to that count, so a fired abort would stop before the
        ninth, usable one.  The probe keeps scanning and reports True.
        """
        usable, blocked = "1.0", [f"{n}.0" for n in range(9, 1, -1)]
        wheels = [make_wheel(v) for v in [*blocked, usable]]
        metadata = {
            v: (
                "Metadata-Version: 2.1\nName: foo\nVersion: "
                f"{v}\nRequires-Dist: bar=={'5.0' if v == usable else '9.0'}\n"
            )
            for v in [*blocked, usable]
        }
        coordinator = make_coordinator(
            wheels, metadata_by_version=metadata, package="foo"
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("5.0")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = len(blocked)  # type: ignore[misc]
        assert provider.has_satisfying_version("foo", VersionRange.full())

    def test_false_when_conflicts_exceed_the_reject_cap(self) -> None:
        """The reject-cap fallback is not a second route to a false positive.

        Suppressing the abort lets the probe scan every candidate.  With more
        than ``_BROAD_LA_REJECT_CAP`` (64) versions all rejected by one decided
        blocker, decision-checking would otherwise flip off past the cap and let
        a later candidate pass unchecked.  The probe must pin decision-checking
        for the whole scan and report that no usable version exists.
        """
        versions = [f"{n}.0" for n in range(70, 0, -1)]
        wheels = [make_wheel(v) for v in versions]
        meta_template = "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar==9.0\n"
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("5.0")})
        assert not provider.has_satisfying_version("foo", VersionRange.full())


class TestResolutionStrategy:
    """``choose_version`` honours ``ResolutionStrategy``."""

    def test_lowest_picks_minimum(self) -> None:
        """``LOWEST`` returns the smallest candidate in range."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, resolution_strategy=ResolutionStrategy.LOWEST)
        assert provider.choose_version("foo", VersionRange.full()) == V("1.0")

    def test_lowest_respects_range(self) -> None:
        """``LOWEST`` picks the smallest *in-range* candidate, not the smallest overall."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, resolution_strategy=ResolutionStrategy.LOWEST)
        chosen = provider.choose_version("foo", SpecifierSet(">=2.0").to_range())
        assert chosen == V("2.0")

    def test_lowest_returns_none_for_empty_range(self) -> None:
        """``LOWEST`` returns None when nothing matches and records a reason."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, resolution_strategy=ResolutionStrategy.LOWEST)
        chosen = provider.choose_version("foo", SpecifierSet(">=5.0").to_range())
        assert chosen is None
        assert short_reason(provider, "foo") == "no version matches the requirement"

    def test_lowest_direct_picks_min_for_direct(self) -> None:
        """``LOWEST_DIRECT`` returns minimum when the package is in the direct set."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(
            coordinator,
            resolution_strategy=ResolutionStrategy.LOWEST_DIRECT,
            direct_packages=frozenset({"foo"}),
        )
        assert provider.choose_version("foo", VersionRange.full()) == V("1.0")

    def test_lowest_direct_picks_max_for_transitive(self) -> None:
        """``LOWEST_DIRECT`` returns highest when the package is not direct."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(
            coordinator,
            resolution_strategy=ResolutionStrategy.LOWEST_DIRECT,
            direct_packages=frozenset(),
        )
        assert provider.choose_version("foo", VersionRange.full()) == V("3.0")

    def test_highest_is_default(self) -> None:
        """No strategy kwarg keeps the historical highest-first behaviour."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        assert provider.choose_version("foo", VersionRange.full()) == V("3.0")

    def test_lowest_records_no_versions_reason_for_empty_index(self) -> None:
        """``LOWEST`` matches HIGHEST's reason recording for an empty listing."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator, resolution_strategy=ResolutionStrategy.LOWEST)
        provider.choose_version("foo", VersionRange.full())
        assert (
            short_reason(provider, "foo") == "package not found on any configured index"
        )

    def test_lowest_skips_version_conflicting_with_root_req(self) -> None:
        """``LOWEST`` walks min->max and skips candidates rejected by look-ahead.

        Mirrors the highest-path's skip-on-conflict behaviour but
        starting from the oldest version.  Without this the lowest path
        would pick the absolute minimum and leak metadata errors out
        of ``get_dependencies``.
        """
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        meta_v3 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 3.0\nRequires-Dist: bar>=1.0\n"
        )
        meta_v2 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar>=1.0\n"
        )
        meta_v1 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=5.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"3.0": meta_v3, "2.0": meta_v2, "1.0": meta_v1},
            package="foo",
        )

        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements=root_reqs,
            resolution_strategy=ResolutionStrategy.LOWEST,
        )
        # 1.0's declared bar>=5.0 conflicts with root bar<2.0 so the
        # lowest path must skip it; 2.0 satisfies and is the lowest
        # acceptable version.
        assert provider.choose_version("foo", VersionRange.full()) == V("2.0")


class TestEqualVersionDifferentStrings:
    """One logical release published with mismatched filename version strings.

    A wheel filename reading ``1.0`` and an sdist filename reading ``1.0.0``
    are the same version (``Version("1.0") == Version("1.0.0")``) with
    distinct ``str()`` forms.  The listing collapses them to one version,
    keeps the wheel as the metadata source, and pins the same string under
    every resolution strategy.
    """

    @staticmethod
    def _files() -> list[WheelFile | SdistFile]:
        return [make_wheel("1.0"), make_sdist("1.0.0")]

    def test_listing_collapses_to_one_logical_version(self) -> None:
        coordinator = make_coordinator(self._files(), package="pkg")
        provider = Provider(coordinator, target=_PY312)
        version_list = provider.fetch_versions("pkg")
        versions = provider.versions_only("pkg", version_list)
        assert versions == [V("1.0")]
        assert {str(v) for v in versions} == {"1.0"}

    def test_wheel_not_evicted_by_same_version_sdist(self) -> None:
        coordinator = make_coordinator(self._files(), package="pkg")
        provider = Provider(coordinator, target=_PY312)
        version_list = provider.fetch_versions("pkg")
        mapping = provider._wheel_by_version("pkg", version_list)
        assert len(mapping) == 1
        dist = mapping[V("1.0")]
        assert isinstance(dist, WheelFile)
        assert dist.metadata_url is not None

    def test_pin_string_is_strategy_independent(self) -> None:
        chosen: dict[ResolutionStrategy, Version | None] = {}
        for strategy in (ResolutionStrategy.HIGHEST, ResolutionStrategy.LOWEST):
            coordinator = make_coordinator(self._files(), package="pkg")
            provider = Provider(
                coordinator,
                target=_PY312,
                resolution_strategy=strategy,
            )
            chosen[strategy] = provider.choose_version(
                "pkg", SpecifierSet(">=1.0").to_range()
            )
        highest = chosen[ResolutionStrategy.HIGHEST]
        lowest = chosen[ResolutionStrategy.LOWEST]
        assert highest is not None
        assert lowest is not None
        assert str(highest) == str(lowest)

    def test_pin_string_independent_of_file_order(self) -> None:
        forward = make_coordinator(self._files(), package="pkg")
        reverse_files = list(reversed(self._files()))
        backward = make_coordinator(reverse_files, package="pkg")
        p_forward = Provider(forward, target=_PY312)
        p_backward = Provider(backward, target=_PY312)
        v_forward = p_forward.choose_version("pkg", SpecifierSet(">=1.0").to_range())
        v_backward = p_backward.choose_version("pkg", SpecifierSet(">=1.0").to_range())
        assert v_forward is not None
        assert v_backward is not None
        assert str(v_forward) == str(v_backward)


class TestCanonicalizeEqualVersions:
    """Only a listing that holds an equal group is rebuilt."""

    def test_distinct_versions_keep_the_same_list(self) -> None:
        pairs = [(V("2.0"), make_wheel("2.0")), (V("1.0"), make_sdist("1.0"))]
        assert listing_mod._canonicalize_equal_versions(pairs) is pairs

    def test_one_version_across_two_artifacts_keeps_the_same_list(self) -> None:
        """The listing interns its versions, so a repeat is the same object."""
        version = V("1.0")
        pairs = [(version, make_wheel("1.0")), (version, make_sdist("1.0"))]
        assert listing_mod._canonicalize_equal_versions(pairs) is pairs

    @pytest.mark.parametrize("first", ["1.0", "1.0.0"])
    def test_equal_group_collapses_to_the_shortest_string(self, first: str) -> None:
        second = "1.0.0" if first == "1.0" else "1.0"
        pairs = [(V(first), make_wheel(first)), (V(second), make_sdist(second))]
        result = listing_mod._canonicalize_equal_versions(pairs)
        assert [str(version) for version, _ in result] == ["1.0", "1.0"]


class TestPrereleaseAdmission:
    """The real Provider applies PEP 440 pre-release admission end to end.

    The toy ``PackagingProvider`` used by the resolver tests picks by plain
    membership, so it cannot catch a regression in the real provider's
    ``version_range.filter`` buffering. These exercise that path directly.
    """

    @staticmethod
    def _provider(
        versions: list[str],
        strategy: ResolutionStrategy = ResolutionStrategy.HIGHEST,
    ) -> Provider:
        wheels = [make_wheel(v) for v in versions]
        coordinator = make_coordinator(wheels, package="foo")
        return Provider(coordinator, resolution_strategy=strategy)

    def test_final_preferred_over_newer_prerelease(self) -> None:
        """A bare requirement buffers a newer pre-release behind the final."""
        provider = self._provider(["1.0", "2.0rc1"])
        assert provider.choose_version("foo", VersionRange.full()) == V("1.0")

    def test_only_prerelease_admitted(self) -> None:
        """With no final in range the buffered pre-release is admitted."""
        provider = self._provider(["2.0rc1"])
        assert provider.choose_version("foo", VersionRange.full()) == V("2.0rc1")

    def test_dev_release_buffered_then_admitted(self) -> None:
        """Developmental releases follow the same buffer-unless-only rule."""
        assert self._provider(["1.0.dev1", "1.0"]).choose_version(
            "foo", VersionRange.full()
        ) == V("1.0")
        assert self._provider(["1.0.dev1"]).choose_version(
            "foo", VersionRange.full()
        ) == V("1.0.dev1")

    def test_explicit_prerelease_spec_admits(self) -> None:
        """A spec naming a pre-release admits it (auto-detected policy)."""
        provider = self._provider(["1.0", "2.0rc1"])
        chosen = provider.choose_version("foo", SpecifierSet(">=2.0rc1").to_range())
        assert chosen == V("2.0rc1")

    def test_prerelease_in_range_but_final_wins(self) -> None:
        """A higher in-range pre-release stays buffered while finals exist."""
        provider = self._provider(["1.0", "1.5", "2.0rc1"])
        chosen = provider.choose_version("foo", SpecifierSet(">=1.0").to_range())
        assert chosen == V("1.5")

    def test_exact_prerelease_pin(self) -> None:
        """An exact ``==`` pin to a pre-release selects it."""
        provider = self._provider(["1.0", "2.0rc1"])
        chosen = provider.choose_version("foo", SpecifierSet("==2.0rc1").to_range())
        assert chosen == V("2.0rc1")

    def test_lowest_skips_lower_prerelease(self) -> None:
        """LOWEST picks the lowest final, not a lower pre-release."""
        provider = self._provider(["1.0rc1", "1.0", "2.0"], ResolutionStrategy.LOWEST)
        assert provider.choose_version("foo", VersionRange.full()) == V("1.0")

    def test_lowest_all_prerelease(self) -> None:
        """With only pre-releases LOWEST picks the lowest pre-release."""
        provider = self._provider(["1.0rc1", "1.0rc2"], ResolutionStrategy.LOWEST)
        assert provider.choose_version("foo", VersionRange.full()) == V("1.0rc1")

    def test_lowest_picks_flushed_prerelease_over_admitted(self) -> None:
        """With no final in range LOWEST reverses the newest-first walk.

        ``>=1.0a1`` admits ``1.0a1`` in place while ``1.95a1`` arrives only
        in the end-of-listing flush, so both walk directions end on
        ``1.95a1`` and only the reversal puts it first.
        """
        provider = self._provider(["1.0a1", "1.95a1"], ResolutionStrategy.LOWEST)
        version_range = (
            SpecifierSet(">=1.0a1,<1.1").to_range()
            | SpecifierSet(">1.9,<2.0").to_range()
        )
        assert provider.choose_version("foo", version_range) == V("1.95a1")

    def test_dependency_prerelease_admits_via_intersection(self) -> None:
        """A dep naming a pre-release propagates admission through ``&``."""
        provider = self._provider(["1.0", "2.0rc1"])
        accumulated = VersionRange.full() & SpecifierSet(">=2.0rc1").to_range()
        assert provider.choose_version("foo", accumulated) == V("2.0rc1")

    def test_plain_dependency_constraint_keeps_final(self) -> None:
        """A plain dep constraint leaves the final-preferring default intact."""
        provider = self._provider(["1.0", "2.0rc1"])
        accumulated = VersionRange.full() & SpecifierSet(">=1.0").to_range()
        assert provider.choose_version("foo", accumulated) == V("1.0")

    def test_only_candidate_in_derived_range_is_prerelease(self) -> None:
        """A derived range leaving only a pre-release admits it (PEP 440)."""
        provider = self._provider(["1.0", "2.5rc1"])
        accumulated = VersionRange.full() & SpecifierSet(">=2.0").to_range()
        assert provider.choose_version("foo", accumulated) == V("2.5rc1")

    def test_full_resolve_propagates_dependency_prerelease(self) -> None:
        """End to end: a transitive dep naming a pre-release pins it."""
        listings = {
            "foo": [make_wheel("1.0"), make_wheel("2.0rc1")],
            "bar": [make_wheel("5.0")],
        }
        metadata = {
            "1.0": "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n\n",
            "2.0rc1": "Metadata-Version: 2.1\nName: foo\nVersion: 2.0rc1\n\n",
            "5.0": (
                "Metadata-Version: 2.1\nName: bar\nVersion: 5.0\n"
                "Requires-Dist: foo>=2.0rc1\n\n"
            ),
        }
        coordinator = make_coordinator(listings=listings, metadata_by_version=metadata)
        root_reqs = {
            "foo": VersionRange.full(admit_arbitrary=False),
            "bar": SpecifierSet("==5.0").to_range(),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        pins = resolver.resolve(root_reqs)
        assert pins["foo"] == V("2.0rc1")


class TestFetchVersionsNotInIndex:
    def test_listing_not_in_index_blocks_on_request(self) -> None:
        """When listing is not in the index, request_listing + wait is used."""
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(pending_listings={"pkg": wheels})

        provider = Provider(coordinator)
        result = provider.fetch_versions("pkg")
        assert len(result) == 1
        assert result[0][0] == V("1.0")
        assert coordinator.calls_to("request_listing")[-1] == ("pkg", False)

    def test_listing_fetch_error_reraised(self) -> None:
        """A recorded listing fetch error surfaces, not an empty listing."""
        coordinator = make_coordinator(package="bad")
        coordinator.index.store_listing_error("bad", RuntimeError("index 500"))
        provider = Provider(coordinator)
        with pytest.raises(RuntimeError, match="index 500"):
            provider.fetch_versions("bad")


class TestPickBestCandidateNone:
    def test_returns_none_when_root_range_excludes_all(self) -> None:
        """pick_best_candidate returns None if no version is in root range."""
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        root_reqs = {"foo": SpecifierSet(">=5.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        versions = provider.fetch_versions("foo")
        result = provider.pick_best_candidate("foo", versions)
        assert result is None

    def test_returns_none_on_empty_versions(self) -> None:
        """pick_best_candidate returns None when versions list is empty."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator, target=_PY312)
        assert provider.pick_best_candidate("foo", []) is None

    def test_returns_none_on_empty_versions_with_root_range(self) -> None:
        """Empty versions list returns None even when name is a root requirement."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        root_reqs = {"foo": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        assert provider.pick_best_candidate("foo", []) is None

    def test_root_requirement_returns_first_match(self) -> None:
        """pick_best_candidate returns the first version in root range."""
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        root_reqs = {"foo": SpecifierSet("<3.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        versions = provider.fetch_versions("foo")
        result = provider.pick_best_candidate("foo", versions)
        assert result is not None
        assert result[0] == V("2.0")

    def test_speculative_prefetch_returns_early_when_no_best(self) -> None:
        """speculative_prefetch returns early when best candidate is None."""
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        with patch.object(provider, "pick_best_candidate", return_value=None):
            provider.speculative_prefetch("foo", [(V("1.0"), wheels[0])])
        assert not coordinator.calls_to("request_metadata")


class TestDistsAtVersion:
    @pytest.mark.parametrize(("version", "start"), [("3.0", 0), ("2.0", 2), ("1.0", 4)])
    def test_yields_the_release_run_in_listing_order(
        self, version: str, start: int
    ) -> None:
        """Preserve distribution order at any listed version."""
        listing = _wheel_and_sdist_releases(3)
        expected = [dist for _, dist in listing[start : start + 2]]
        assert dists_at_version(listing, V(version)) == expected

    @pytest.mark.parametrize("version", ["4.0", "2.5", "0.5"])
    def test_an_unlisted_version_yields_nothing(self, version: str) -> None:
        """Return nothing for a version outside or between releases."""
        assert dists_at_version(_wheel_and_sdist_releases(3), V(version)) == []

    def test_an_empty_listing_yields_nothing(self) -> None:
        assert dists_at_version([], V("1.0")) == []


class TestPickDistForMetadata:
    def test_prefers_wheel_with_metadata_when_sdist_first(self) -> None:
        """A wheel-with-meta wins over an earlier sdist at the same version."""
        sdist = make_sdist("1.0")
        wheel = make_wheel("1.0")
        versions = [(V("1.0"), sdist), (V("1.0"), wheel)]
        assert pick_dist_for_metadata(versions, V("1.0"), None) is wheel

    def test_prefers_any_wheel_over_sdist_when_no_pep658(self) -> None:
        """Without PEP 658, a plain wheel still wins over an sdist.

        Range-fetching ``METADATA`` from a wheel is cheaper than
        building an sdist, so the picker only falls back to the sdist
        when no wheel exists at the version.  This matters for
        ``sdist-only`` with ``metadata-policy = "prefer-wheel"``: those
        listings keep wheels purely so the resolver can read their
        metadata, and ``pick_dist_for_metadata`` must honour that.
        """
        sdist = make_sdist("1.0")
        wheel_no_meta = make_wheel("1.0", has_metadata=False)
        versions = [(V("1.0"), sdist), (V("1.0"), wheel_no_meta)]
        assert pick_dist_for_metadata(versions, V("1.0"), None) is wheel_no_meta

    def test_falls_back_to_sdist_when_no_wheel_at_version(self) -> None:
        """When no wheel exists at the version, the sdist is returned."""
        sdist = make_sdist("1.0")
        versions = [(V("1.0"), sdist)]
        assert pick_dist_for_metadata(versions, V("1.0"), None) is sdist

    def test_returns_none_for_unknown_version(self) -> None:
        """No matching version yields ``None``."""
        versions = [(V("2.0"), make_wheel("2.0"))]
        assert pick_dist_for_metadata(versions, V("1.0"), None) is None

    def test_bisects_the_newest_first_listing(self) -> None:
        """Find one of 256 entries in fewer than 20 comparisons."""
        listing = _one_wheel_releases(256)
        mid_version, mid_dist = listing[127]
        version = _CountingVersion(str(mid_version))

        assert pick_dist_for_metadata(listing, version, None) is mid_dist
        assert version.comparisons < 20

    def test_prefers_the_wheel_the_tags_rank_most_specific(self) -> None:
        """The tags rank the siblings; listing order does not.

        An abi3 wheel published beside a full-ABI one is a routine
        layout, and the target installs the full-ABI one.
        """
        tags = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_x86_64")
        ).tags
        abi3 = make_wheel(tag="cp312-abi3-manylinux_2_17_x86_64")
        full_abi = make_wheel(tag="cp312-cp312-manylinux_2_17_x86_64")
        versions = [(V("1.0"), abi3), (V("1.0"), full_abi)]
        assert pick_dist_for_metadata(versions, V("1.0"), tags) is full_abi

    def test_prefers_a_wheel_over_an_sdist_under_a_tag_set(self) -> None:
        """The tag pick runs over the wheels; an sdist stays the last resort."""
        tags = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_x86_64")
        ).tags
        sdist = make_sdist("1.0")
        wheel = make_wheel("1.0")
        versions = [(V("1.0"), sdist), (V("1.0"), wheel)]
        assert pick_dist_for_metadata(versions, V("1.0"), tags) is wheel

    def test_breaks_a_tag_tie_with_the_cheaper_metadata_source(self) -> None:
        """Two wheels the tags rank equally fall back to the cheaper source."""
        tags = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_x86_64")
        ).tags
        no_sidecar = make_wheel(tag="py2.py3-none-any", has_metadata=False)
        sidecar = make_wheel(tag="py3-none-any")
        versions = [(V("1.0"), no_sidecar), (V("1.0"), sidecar)]
        assert pick_dist_for_metadata(versions, V("1.0"), tags) is sidecar

    def test_first_match_wins_for_each_dist_kind(self) -> None:
        """Repeated entries at the same version do not displace the first.

        With nothing to rank the wheels by, the picker keeps the *first*
        candidate in each preference tier (wheel-with-meta,
        wheel-without-meta, sdist) it sees, so a later candidate of the
        same kind never silently overrides an earlier one.
        """
        first_meta = make_wheel("1.0")
        second_meta = WheelFile(
            filename="pkg-1.0-cp310-cp310-linux_x86_64.whl",
            url="https://example.com/pkg-1.0-cp310-cp310-linux_x86_64.whl",
            version="1.0",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )
        first_plain = make_wheel("1.0", has_metadata=False)
        second_plain = WheelFile(
            filename="pkg-1.0-cp311-cp311-linux_x86_64.whl",
            url="https://example.com/pkg-1.0-cp311-cp311-linux_x86_64.whl",
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
        )
        first_sdist = make_sdist("1.0")
        second_sdist = SdistFile(
            filename="pkg-1.0.zip",
            url="https://example.com/pkg-1.0.zip",
            version="1.0",
            requires_python=None,
            upload_time=None,
        )
        versions = [
            (V("1.0"), first_sdist),
            (V("1.0"), second_sdist),
            (V("1.0"), first_plain),
            (V("1.0"), second_plain),
            (V("1.0"), first_meta),
            (V("1.0"), second_meta),
        ]
        assert pick_dist_for_metadata(versions, V("1.0"), None) is first_meta
        # Strip PEP 658 wheels: the plain wheel still wins, first one
        # encountered wins within the tier.
        versions_no_meta = [
            (V("1.0"), first_sdist),
            (V("1.0"), second_sdist),
            (V("1.0"), first_plain),
            (V("1.0"), second_plain),
        ]
        assert pick_dist_for_metadata(versions_no_meta, V("1.0"), None) is first_plain
        # And with sdists only, first sdist wins.
        versions_sdist_only = [
            (V("1.0"), first_sdist),
            (V("1.0"), second_sdist),
        ]
        assert (
            pick_dist_for_metadata(versions_sdist_only, V("1.0"), None) is first_sdist
        )


class TestPublicAccessors:
    """Public read accessors used by lockfile / download tooling."""

    def test_local_source_lookup_returns_registered(self) -> None:
        coordinator = make_coordinator(package="foo")
        local = LocalSource(name="My-Lib", path="/tmp/my-lib")
        provider = Provider(
            coordinator,
            target=_PY312,
            local_sources=[local],
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.local_source_for("my-lib") is local
        assert provider.local_source_for("My_Lib") is local
        assert provider.local_source_for("absent") is None

    def test_vcs_source_lookup_returns_registered(self) -> None:
        coordinator = make_coordinator(package="foo")
        vcs = VcsSource(
            name="My-Lib",
            url="git+https://example.com/r.git@" + "a" * 40,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            vcs_sources=[vcs],
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
            ),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.vcs_source_for("my-lib") is vcs
        assert provider.vcs_source_for("absent") is None

    def test_dist_files_for_returns_listing_subset(self) -> None:
        wheels = [make_wheel("1.0"), make_wheel("2.0"), make_sdist("1.0")]
        coordinator = make_coordinator(wheels, package="pkg")
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"pkg": SpecifierSet(">=0").to_range()},
        )
        # Force the listing into the cache by asking for a version
        provider.choose_version("pkg", SpecifierSet(">=0").to_range())
        files = provider.dist_files_for("pkg", V("1.0"))
        assert len(files) == 2  # one wheel + one sdist
        assert provider.dist_files_for("pkg", V("3.0")) == []

    def test_dist_files_for_unlisted_returns_empty(self) -> None:
        coordinator = make_coordinator(package="pkg")
        provider = Provider(coordinator, target=_PY312)
        assert provider.dist_files_for("unknown", V("1.0")) == []

    def test_dist_files_for_bisects_the_listing(self) -> None:
        """Find one of 256 entries in fewer than 20 comparisons."""
        provider = Provider(make_coordinator(package="pkg"), target=_PY312)
        listing = _one_wheel_releases(256)
        provider.versions_cache["pkg"] = listing

        mid_version, mid_dist = listing[127]
        version = _CountingVersion(str(mid_version))

        assert provider.dist_files_for("pkg", version) == [mid_dist]
        assert version.comparisons < 20


class TestPrereleaseHostTarget:
    """A prerelease interpreter reports ``3.15.0rc1`` as its full version.

    ``ResolveTarget.for_host`` takes that string verbatim, so the tag
    filter has to key off the release the interpreter is (cp315), not the
    version string, and Requires-Python still has to admit the machine the
    resolve is running on.
    """

    _RC_ENV: ClassVar[dict[str, str]] = {
        "implementation_name": "cpython",
        "implementation_version": "3.15.0rc1",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_python_implementation": "CPython",
        "platform_release": "6.8.0",
        "platform_system": "Linux",
        "platform_version": "#1 SMP",
        "python_full_version": "3.15.0rc1",
        "python_version": "3.15",
        "sys_platform": "linux",
    }
    _RC_TAGS = (
        Tag("cp315", "cp315", "manylinux_2_39_x86_64"),
        Tag("py3", "none", "any"),
    )

    def _target(self) -> ResolveTarget:
        return ResolveTarget.for_host(
            env_source=lambda: dict(self._RC_ENV),
            tags_source=lambda: self._RC_TAGS,
        )

    @staticmethod
    def _wheel(tag: str, requires_python: str | None = None) -> WheelFile:
        filename = f"foo-1.0-{tag}.whl"
        return WheelFile(
            filename=filename,
            url=f"https://example.com/{filename}",
            version="1.0",
            requires_python=requires_python,
            has_metadata=True,
            upload_time=None,
        )

    def test_rc_host_installs_its_own_release_wheels(self) -> None:
        """cp315 wheels are kept; a wheel for the previous release is not."""
        files = [
            self._wheel("cp315-cp315-manylinux_2_39_x86_64"),
            self._wheel("cp314-cp314-manylinux_2_39_x86_64"),
        ]
        provider = Provider(
            make_coordinator(files, package="foo"), target=self._target()
        )
        kept = {dist.filename for _, dist in provider.fetch_versions("foo")}
        assert kept == {"foo-1.0-cp315-cp315-manylinux_2_39_x86_64.whl"}
        assert provider.stats.excluded_by_wheel_tags == 1

    def test_rc_host_is_admitted_by_a_requires_python_floor(self) -> None:
        """PEP 440 admits a prerelease when the range holds no final release."""
        files = [self._wheel("py3-none-any", requires_python=">=3.9")]
        provider = Provider(
            make_coordinator(files, package="foo"), target=self._target()
        )
        assert provider.python_version == "3.15.0rc1"
        assert [v for v, _ in provider.fetch_versions("foo")] == [V("1.0")]


class TestLazyVersionDists:
    """The provider's own listing is indexed one version at a time."""

    @staticmethod
    def _provider() -> tuple[Provider, list[tuple[Version, WheelFile | SdistFile]]]:
        files = [
            make_wheel("3.0"),
            make_wheel("2.0"),
            make_sdist("2.0"),
            make_wheel("1.0"),
        ]
        provider = Provider(make_coordinator(files, package="foo"))
        return provider, provider.fetch_versions("foo")

    def test_picks_only_the_versions_asked_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider, version_list = self._provider()
        picked_versions: list[str] = []
        real_pick = metadata_resolver.pick_dist

        def counting_pick(dists: Sequence[Any], *args: Any, **kwargs: Any) -> Any:
            picked_versions.append(dists[0].version)
            return real_pick(dists, *args, **kwargs)

        monkeypatch.setattr(metadata_resolver, "pick_dist", counting_pick)
        mapping = provider._wheel_by_version("foo", version_list)
        assert picked_versions == []

        assert mapping[V("2.0")].version == "2.0"
        assert picked_versions == ["2.0"]
        # Memoised: asking again picks nothing.
        assert mapping[V("2.0")] is mapping[V("2.0")]
        assert picked_versions == ["2.0"]
        # Size and order come from the listing without picking.
        assert len(mapping) == 3
        assert list(mapping) == [V("3.0"), V("2.0"), V("1.0")]
        assert picked_versions == ["2.0"]

    def test_an_unlisted_version_is_absent(self) -> None:
        provider, version_list = self._provider()
        mapping = provider._wheel_by_version("foo", version_list)
        assert V("9.9") not in mapping
        assert mapping.get(V("9.9")) is None
        with pytest.raises(KeyError):
            mapping[V("9.9")]
        assert "2.0" not in mapping
        assert V("2.0") in mapping

    def test_matches_the_eager_index_of_another_listing(self) -> None:
        provider, version_list = self._provider()
        lazy = metadata_resolver.version_dists(provider, "foo", version_list)
        # A copy is not the provider's cached listing, so it is indexed whole.
        eager = metadata_resolver.version_dists(provider, "foo", list(version_list))
        assert isinstance(eager.picked, dict)
        assert dict(lazy.picked) == eager.picked
        assert dict(lazy.sibling_wheels) == eager.sibling_wheels

    def test_sibling_wheels_hold_only_tied_versions(self) -> None:
        first = make_wheel("1.0")
        second = WheelFile(
            filename="pkg-1.0-py2.py3-none-any.whl",
            url="https://example.com/pkg-1.0-py2.py3-none-any.whl",
            version="1.0",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
            hashes=(),
            size=None,
            metadata_hash=None,
        )
        provider = Provider(
            make_coordinator([make_wheel("2.0"), first, second], package="foo")
        )
        version_list = provider.fetch_versions("foo")
        indexed = metadata_resolver.version_dists(provider, "foo", version_list)

        assert indexed.sibling_wheels.get(V("2.0")) is None
        assert V("2.0") not in indexed.sibling_wheels
        assert indexed.sibling_wheels[V("1.0")] == [first, second]
        assert indexed.sibling_wheels.get(V("9.9")) is None
        assert list(indexed.sibling_wheels) == [V("1.0")]
        assert len(indexed.sibling_wheels) == 1
