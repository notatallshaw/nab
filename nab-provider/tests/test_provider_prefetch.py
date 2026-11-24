"""Speculative listing and metadata preparation by the provider."""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from packaging.ranges import VersionRange
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from nab_provider.provider import (
    DistPolicy,
    Provider,
    ResolutionStrategy,
    UnsupportedVcsError,
    VcsConfig,
    VcsPolicy,
)
from nab_provider.records import SdistFile, WheelFile
from nab_provider.testing import FakeFetchPort, make_coordinator, make_sdist, make_wheel

from .provider_fixtures import PYTHON_312 as _PY312

V = Version


# Excludes nothing. Used where the walk-ahead's range is not what a test checks.
_ANY_VERSION = VersionRange.full()


def _sidecar_hash(version: str) -> tuple[str, str]:
    """A published ``core-metadata`` digest, distinct per version."""
    return ("sha256", hashlib.sha256(version.encode()).hexdigest())


def _prefetched_batch_versions(coordinator: FakeFetchPort) -> list[str]:
    """Versions of the one batch metadata prefetch, in submission order."""
    batches = coordinator.calls_to("request_metadata_batch")
    assert len(batches) == 1
    return [version for _package, version, _url, _hash in batches[0][0]]


def _requested_digests(
    coordinator: FakeFetchPort,
) -> list[tuple[str, tuple[str, str] | None]]:
    """``(version, digest)`` for every single metadata request, in call order."""
    calls = coordinator.calls_to("request_metadata")
    return [(version, digest) for _package, version, _url, digest in calls]


def _batched_digests(
    coordinator: FakeFetchPort,
) -> list[tuple[str, tuple[str, str] | None]]:
    """``(version, digest)`` for the last metadata batch, in submission order."""
    items = coordinator.calls_to("request_metadata_batch")[-1][0]
    return [(version, digest) for _package, version, _url, digest in items]


class TestPrefetchListings:
    def test_root_requirements_prefetched(self) -> None:
        """Root requirement listings are fetched in background on init."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        root_reqs = {"foo": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        result = provider.fetch_versions("foo")
        assert len(result) == 1

    def test_prefetch_skips_already_cached(self) -> None:
        """``prefetch_new_deps`` doesn't overwrite already-cached listings."""
        coordinator = make_coordinator([make_wheel("2.0")], package="foo")
        root_reqs = {"foo": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.fetch_versions("foo")
        provider.versions_cache["foo"] = [(V("1.0"), make_wheel("1.0"))]
        provider.prefetch_new_deps({"foo": SpecifierSet(">=1.0").to_range()})
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("1.0")]

    def test_prefetch_new_deps_requests_listing_speculatively(self) -> None:
        """The prefetch cascade marks its listing requests speculative (S-CRIT)."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator, target=_PY312)
        coordinator.reset()
        provider.prefetch_new_deps({"bar": SpecifierSet(">=1.0").to_range()})
        assert coordinator.calls_to("request_listing") == [("bar", True)]


class TestSpeculativeRequestDedup:
    """A repeat speculative request is dropped, the first one is not."""

    def test_repeat_cascade_requests_each_listing_once(self) -> None:
        """A dep named by three parents gets one speculative listing request."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator, target=_PY312)
        coordinator.reset()

        deps = {"bar": SpecifierSet(">=1.0").to_range()}
        for _ in range(3):
            provider.prefetch_new_deps(deps)

        assert coordinator.calls_to("request_listing") == [("bar", True)]

    def test_listing_requests_keep_their_first_occurrence_order(self) -> None:
        """Every name still reaches the coordinator, in first-occurrence order."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator, target=_PY312)
        coordinator.reset()

        full = SpecifierSet(">=1.0").to_range()
        provider.prefetch_new_deps({"bar": full})
        provider.prefetch_new_deps({"bar": full, "baz": full})
        provider.prefetch_new_deps({"baz": full, "qux": full})

        assert coordinator.calls_to("request_listing") == [
            ("bar", True),
            ("baz", True),
            ("qux", True),
        ]

    def test_repeat_cascade_requests_transitive_metadata_once(self) -> None:
        """The best candidate's sidecar is asked for once, not once per parent."""
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")], package="foo"
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("foo")

        deps = {"foo": SpecifierSet(">=1.0").to_range()}
        for _ in range(3):
            provider.prefetch_new_deps(deps)

        calls = coordinator.calls_to("request_metadata")
        assert [version for _pkg, version, _url, _hash in calls] == ["2.0"]

    def test_new_best_pick_for_the_same_package_is_still_requested(self) -> None:
        """The candidate memo keys on the version, not on the package alone.

        ``pick_best_candidate`` is patched because within one provider it is
        constant per package: root requirements, the listing and the strategy
        are all fixed once the resolve starts.
        """
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")], package="foo"
        )
        provider = Provider(coordinator, target=_PY312)
        older = provider.fetch_versions("foo")[-1]

        deps = {"foo": SpecifierSet(">=1.0").to_range()}
        with patch.object(provider, "pick_best_candidate", return_value=older):
            provider.prefetch_new_deps(deps)
            provider.prefetch_new_deps(deps)

        calls = coordinator.calls_to("request_metadata")
        assert [version for _pkg, version, _url, _hash in calls] == ["2.0", "1.0"]

    def test_metadata_requests_keep_their_first_occurrence_order(self) -> None:
        """Every candidate still reaches the coordinator, in first-occurrence order."""
        coordinator = make_coordinator(
            listings={"foo": [make_wheel("2.0")], "bar": [make_wheel("3.0")]}
        )
        provider = Provider(coordinator, target=_PY312)
        full = SpecifierSet(">=1.0").to_range()

        provider.fetch_versions("foo")
        provider.prefetch_new_deps({"foo": full})
        provider.fetch_versions("bar")
        provider.prefetch_new_deps({"foo": full, "bar": full})

        calls = coordinator.calls_to("request_metadata")
        assert [(pkg, version) for pkg, version, _url, _hash in calls] == [
            ("foo", "2.0"),
            ("bar", "3.0"),
        ]


class TestSpeculativePrefetch:
    def test_metadata_prefetched_after_listing(self) -> None:
        """After listing fetch, metadata for best candidate is in flight."""
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")],
            metadata_text="Metadata-Version: 2.1\nName: foo\nVersion: 2.0\n",
            package="foo",
        )
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        assert len(coordinator.calls_to("request_metadata")) == 1
        _package, version, _url, _hash = coordinator.calls_to("request_metadata")[-1]
        assert version == "2.0"

    @pytest.mark.parametrize(
        "strategy",
        [ResolutionStrategy.LOWEST, ResolutionStrategy.LOWEST_DIRECT],
    )
    def test_transitive_prefetch_is_oldest_under_lowest(
        self, strategy: ResolutionStrategy
    ) -> None:
        """Under a lowest strategy the warmed version is the one the scan starts on."""
        wheels = [make_wheel(f"{i}.0") for i in range(20, 0, -1)]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            resolution_strategy=strategy,
            direct_packages=frozenset({"foo"}),
        )
        provider.fetch_versions("foo")
        _package, version, _url, _hash = coordinator.calls_to("request_metadata")[-1]
        assert version == "1.0"

    @pytest.mark.parametrize(
        "strategy",
        [ResolutionStrategy.LOWEST, ResolutionStrategy.LOWEST_DIRECT],
    )
    def test_unconstrained_root_prefetch_is_oldest_under_lowest(
        self, strategy: ResolutionStrategy
    ) -> None:
        """A root requirement that constrains nothing takes the same oldest-first pick.

        ``speculative_prefetch`` routes a full root range to the single-candidate
        path, so ``pick_best_candidate`` is what reads the strategy here.
        """
        wheels = [make_wheel(f"{i}.0") for i in range(20, 0, -1)]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full()},
            resolution_strategy=strategy,
            direct_packages=frozenset({"foo"}),
        )
        provider.fetch_versions("foo")
        assert not coordinator.calls_to("request_metadata_batch")
        _package, version, _url, _hash = coordinator.calls_to("request_metadata")[-1]
        assert version == "1.0"

    def test_get_dependencies_uses_speculative_future(self) -> None:
        """get_dependencies picks up the speculatively prefetched metadata."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: bar>=1.0\n"
            ),
            package="foo",
        )
        provider = Provider(coordinator)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps

    def test_root_requirement_prefetches_within_range(self) -> None:
        """The root batch holds the newest in-range versions, PREFETCH_BATCH of them."""
        wheels = [make_wheel(f"{n}.0") for n in range(20, 0, -1)]
        coordinator = make_coordinator(wheels, package="foo")
        root_reqs = {"foo": SpecifierSet("<15.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.fetch_versions("foo")
        newest_first = [f"{n}.0" for n in range(14, 0, -1)]
        assert (
            _prefetched_batch_versions(coordinator)
            == newest_first[: Provider.PREFETCH_BATCH]
        )

    @pytest.mark.parametrize(
        "strategy",
        [ResolutionStrategy.LOWEST, ResolutionStrategy.LOWEST_DIRECT],
    )
    def test_root_batch_is_oldest_first_under_lowest(
        self, strategy: ResolutionStrategy
    ) -> None:
        """Under a lowest strategy the batch follows choose_version: oldest first."""
        wheels = [make_wheel(f"{n}.0") for n in range(20, 0, -1)]
        coordinator = make_coordinator(wheels, package="foo")
        root_reqs = {"foo": SpecifierSet("<15.0").to_range()}
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements=root_reqs,
            resolution_strategy=strategy,
            direct_packages=frozenset({"foo"}),
        )
        provider.fetch_versions("foo")
        oldest_first = [f"{n}.0" for n in range(1, 15)]
        assert (
            _prefetched_batch_versions(coordinator)
            == oldest_first[: Provider.PREFETCH_BATCH]
        )

    def test_no_prefetch_when_no_versions(self) -> None:
        """No speculative prefetch for empty package listings."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        assert not coordinator.calls_to("request_metadata")
        assert not coordinator.calls_to("request_metadata_batch")

    def test_no_prefetch_when_already_cached(self) -> None:
        """No speculative prefetch if deps are already cached."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        provider.deps_cache[("foo", V("1.0"))] = {}
        provider.fetch_versions("foo")
        assert not coordinator.calls_to("request_metadata")
        assert not coordinator.calls_to("request_metadata_batch")

    def test_early_metadata_consumed_by_get_deps(self) -> None:
        """If metadata is already in the index, get_dependencies uses it."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            package="foo",
        )
        # Pre-store metadata in the index
        coordinator.index.store_metadata(
            "foo", "1.0", "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
        )
        provider = Provider(coordinator)
        # Pre-cache versions to avoid fetch_versions triggering prefetch
        provider.versions_cache["foo"] = [(V("1.0"), make_wheel("1.0"))]
        deps = provider.get_dependencies("foo", V("1.0"))
        assert deps == {}  # empty metadata

    def test_no_prefetch_for_unsatisfiable_root(self) -> None:
        """No prefetch when root range excludes all versions."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        root_reqs = {"foo": SpecifierSet(">=5.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.fetch_versions("foo")
        assert not coordinator.calls_to("request_metadata_batch")

    def test_non_best_version_fetches_synchronously(self) -> None:
        """Requesting a non-best version falls back to sync fetch."""
        meta_text = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=1.0\n"
        )
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={
                "2.0": "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\n",
                "1.0": meta_text,
            },
            package="foo",
        )
        provider = Provider(coordinator)
        # v2.0 was prefetched, but we ask for v1.0
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps


class TestSidecarDigestForwarding:
    """Each metadata request carries the sidecar digest its listing published.

    A request that drops the listing's ``core-metadata`` value is fetched
    without a hash check and reports nothing, so there is one test per request
    site the provider owns.
    """

    def _coordinator(self, *versions: str) -> FakeFetchPort:
        """A ``foo`` listing whose wheels each publish their own digest."""
        return make_coordinator(
            [make_wheel(v, metadata_hash=_sidecar_hash(v)) for v in versions],
            auto_metadata=True,
            package="foo",
        )

    def test_transitive_best_prefetch_forwards_the_digest(self) -> None:
        """The single-candidate prefetch carries the picked wheel's digest."""
        coordinator = self._coordinator("2.0", "1.0")
        provider = Provider(coordinator, target=_PY312)

        provider.fetch_versions("foo")

        assert _requested_digests(coordinator) == [("2.0", _sidecar_hash("2.0"))]

    def test_root_batch_prefetch_forwards_the_digest(self) -> None:
        """The root-range batch carries a digest per item, not one for the batch."""
        coordinator = self._coordinator("2.0", "1.0")
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": SpecifierSet(">=1.0").to_range()},
        )

        provider.fetch_versions("foo")

        assert _batched_digests(coordinator) == [
            ("2.0", _sidecar_hash("2.0")),
            ("1.0", _sidecar_hash("1.0")),
        ]

    def test_walk_ahead_prefetch_forwards_the_digest(self) -> None:
        """The deep walk-ahead batch carries the digest of each version it warms."""
        coordinator = self._coordinator("2.0", "1.0")
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("foo")
        coordinator.reset()

        provider._prefetch_walk_ahead("foo", _ANY_VERSION)

        assert _batched_digests(coordinator) == [("1.0", _sidecar_hash("1.0"))]

    def test_pipelined_batch_forwards_the_digest(self) -> None:
        """The scan's own batch carries the digest of the version it submits."""
        coordinator = self._coordinator("2.0", "1.0")
        provider = Provider(coordinator, target=_PY312)
        versions = provider.fetch_versions("foo")
        coordinator.reset()

        provider._prefetch_batch(
            "foo", [V("1.0")], provider._wheel_by_version("foo", versions)
        )

        assert _batched_digests(coordinator) == [("1.0", _sidecar_hash("1.0"))]

    def test_synchronous_read_forwards_the_digest(self) -> None:
        """The blocking read of a version no prefetch warmed carries its digest."""
        coordinator = self._coordinator("2.0", "1.0")
        provider = Provider(coordinator, target=_PY312)

        provider.get_dependencies("foo", V("1.0"))

        assert _requested_digests(coordinator)[-1] == ("1.0", _sidecar_hash("1.0"))


class TestBatchPrefetchUrlDepRefusal:
    """A refused direct-URL dep on a non-selected candidate must not abort the scan.

    The speculative batch prefetch parses metadata for every candidate in a
    look-ahead batch. A base direct-URL/VCS ``Requires-Dist`` is refused only at
    selection time, so a refusal raised while prefetching a candidate the scan
    never picks must be swallowed, not propagated out of ``choose_version``.
    """

    def _provider(
        self, url_line: str, *, vcs_config: VcsConfig | None = None
    ) -> Provider:
        meta = {
            # 3.0 has no Name, so it is look-ahead-rejected and the scan enters
            # the pipelined batch over 2.0 (the clean winner) and 1.0.
            "3.0": "Metadata-Version: 2.1\nVersion: 3.0\n\n",
            "2.0": "Metadata-Version: 2.1\nName: pkg\nVersion: 2.0\n\n",
            "1.0": (
                "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
                f"Requires-Dist: {url_line}\n\n"
            ),
        }
        coordinator = make_coordinator(
            [make_wheel(v) for v in ("3.0", "2.0", "1.0")],
            metadata_by_version=meta,
            package="pkg",
        )
        return Provider(
            coordinator,
            target=_PY312,
            root_requirements={"pkg": VersionRange.full(admit_arbitrary=False)},
            vcs_config=vcs_config,
        )

    def test_plain_url_dep_on_lower_candidate(self) -> None:
        """A plain https archive URL dep refusal on 1.0 leaves 2.0 selectable."""
        provider = self._provider("foo @ https://example.com/foo-1.0.whl")
        assert provider.choose_version("pkg", VersionRange.full()) == V("2.0")
        assert ("pkg", V("1.0")) not in provider.deps_cache

    def test_blocked_vcs_dep_on_lower_candidate(self) -> None:
        """A git+https URL dep refused under the default block policy."""
        provider = self._provider("foo @ git+https://example.com/foo.git")
        assert provider.choose_version("pkg", VersionRange.full()) == V("2.0")

    def test_admitted_vcs_dep_on_lower_candidate(self) -> None:
        """An admitted git URL raises NotImplementedError, still not an abort."""
        provider = self._provider(
            "foo @ git+https://example.com/foo.git",
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/foo",),
                require_pin=False,
            ),
        )
        assert provider.choose_version("pkg", VersionRange.full()) == V("2.0")

    def test_selecting_the_url_dep_candidate_still_refuses(self) -> None:
        """Pinning to 1.0 re-raises the refusal the prefetch swallowed."""
        provider = self._provider("foo @ https://example.com/foo-1.0.whl")
        with pytest.raises(UnsupportedVcsError):
            provider.get_dependencies("pkg", V("1.0"))


class TestSpeculativePrefetchBatchLimit:
    def test_batch_limit_stops_at_prefetch_batch(self) -> None:
        """Prefetch stops collecting items once PREFETCH_BATCH is reached."""
        # Create more wheels than PREFETCH_BATCH
        wheels = [make_wheel(f"{i}.0") for i in range(40, 0, -1)]
        coordinator = make_coordinator(
            wheels,
            metadata_text="Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            package="foo",
        )
        root_reqs = {"foo": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.fetch_versions("foo")
        newest_first = [f"{i}.0" for i in range(40, 0, -1)]
        assert (
            _prefetched_batch_versions(coordinator)
            == newest_first[: Provider.PREFETCH_BATCH]
        )

    def test_batch_skips_already_cached_deps(self) -> None:
        """Prefetch skips versions with deps already in cache."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_text="Metadata-Version: 2.1\nName: foo\nVersion: 2.0\n",
            package="foo",
        )
        root_reqs = {"foo": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        # Pre-cache deps for v2.0 so it gets skipped during prefetch.
        provider.deps_cache[("foo", V("2.0"))] = {}
        provider.fetch_versions("foo")
        # Only v1.0 should be in the batch.
        batches = coordinator.calls_to("request_metadata_batch")
        assert batches
        items = batches[-1][0]
        assert all(ver == "1.0" for _, ver, _, _ in items)


class TestPrefetchWalkAhead:
    """``_prefetch_walk_ahead`` covers the walk past the first batch.

    Fired from ``_scan_candidates_pipelined``; submits up to
    ``DEEP_PREFETCH_COUNT`` wheel metadata requests in scan order over
    ``versions_cache[normalized]``, skipping the versions the scan's range
    excludes.  Fire-and-forget: a version it skips is fetched on demand
    when the resolver asks for it.
    """

    def test_no_op_when_listing_missing(self) -> None:
        """Bare provider with no cached listing makes no batch call."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider._prefetch_walk_ahead("foo", _ANY_VERSION)
        assert not coordinator.calls_to("request_metadata_batch")

    def test_caps_at_deep_prefetch_count(self) -> None:
        """No more than ``DEEP_PREFETCH_COUNT`` distinct versions submitted."""
        wheels = [make_wheel(f"{i}.0") for i in range(100, 0, -1)]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", _ANY_VERSION)
        items = coordinator.calls_to("request_metadata_batch")[-1][0]
        # fetch_versions already prefetched the newest version; it fills a
        # window slot but is skipped as already-held.
        assert len(items) == provider.DEEP_PREFETCH_COUNT - 1

    def test_skips_versions_outside_the_range(self) -> None:
        """A version the scan's range excludes is not requested."""
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", SpecifierSet(">=2.0").to_range())
        items = coordinator.calls_to("request_metadata_batch")[-1][0]
        versions = [ver for _, ver, _, _ in items]
        # fetch_versions already prefetched the newest version.
        assert versions == ["2.0"]

    def test_out_of_range_version_still_fills_its_window_slot(self) -> None:
        """The window stays the first ``DEEP_PREFETCH_COUNT`` listing versions.

        Excluding a version frees no slot for a deeper one, so the walk
        never reaches further than it does with no range at all.
        """
        wheels = [make_wheel(f"{i}.0") for i in range(100, 0, -1)]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", SpecifierSet("<=90.0").to_range())
        items = coordinator.calls_to("request_metadata_batch")[-1][0]
        versions = [ver for _, ver, _, _ in items]
        # The window runs 100.0 down to 37.0; 100.0 through 91.0 are out of
        # range and 37.0 is the last slot.
        assert versions == [f"{i}.0" for i in range(90, 36, -1)]

    @pytest.mark.parametrize(
        "strategy",
        [ResolutionStrategy.LOWEST, ResolutionStrategy.LOWEST_DIRECT],
    )
    def test_walk_ahead_is_oldest_first_under_lowest(
        self, strategy: ResolutionStrategy
    ) -> None:
        """Under a lowest strategy the batch follows the scan: oldest first."""
        wheels = [make_wheel(f"{i}.0") for i in range(100, 0, -1)]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(
            coordinator,
            resolution_strategy=strategy,
            direct_packages=frozenset({"foo"}),
        )
        provider.fetch_versions("foo")
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", _ANY_VERSION)
        items = coordinator.calls_to("request_metadata_batch")[-1][0]
        versions = [ver for _, ver, _, _ in items]
        # fetch_versions already prefetched 1.0, the oldest; it fills a window
        # slot but is skipped as already-held.
        assert versions == [
            f"{i}.0" for i in range(2, provider.DEEP_PREFETCH_COUNT + 1)
        ]

    def test_skips_versions_with_cached_deps(self) -> None:
        """Versions already in ``deps_cache`` are excluded from the batch."""
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        provider.deps_cache[("foo", V("2.0"))] = {}
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", _ANY_VERSION)
        items = coordinator.calls_to("request_metadata_batch")[-1][0]
        versions = [ver for _, ver, _, _ in items]
        assert "2.0" not in versions
        # fetch_versions already prefetched the newest version.
        assert "3.0" not in versions
        assert "1.0" in versions

    def test_dedupes_repeated_versions(self) -> None:
        """A wheel and a sdist for the same version count as one slot."""
        # 3.0 is the newest, so fetch_versions prefetches and skips it; the
        # wheel and sdist for 2.0 collapse to the single 2.0 slot.
        wheels: list[WheelFile | SdistFile] = [
            make_wheel("3.0"),
            make_wheel("2.0"),
            make_sdist("2.0"),
            make_wheel("1.0"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", _ANY_VERSION)
        items = coordinator.calls_to("request_metadata_batch")[-1][0]
        versions = [ver for _, ver, _, _ in items]
        assert versions == ["2.0", "1.0"]

    def test_skips_version_whose_empty_fetch_the_coordinator_holds(self) -> None:
        """An already-fetched sidecar, even an empty one, is not re-requested."""
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        # An empty fetch (no sidecar served) still marks the slot fetched.
        two = make_wheel("2.0")
        assert two.metadata_url is not None
        coordinator.request_metadata("foo", "2.0", two.metadata_url, None)
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", _ANY_VERSION)
        items = coordinator.calls_to("request_metadata_batch")[-1][0]
        versions = [ver for _, ver, _, _ in items]
        assert "2.0" not in versions
        assert "1.0" in versions

    def test_picks_wheel_when_both_present(self) -> None:
        """A version with both a wheel and a sdist still prefetches the wheel.

        2.0 is the newest, so the listing prefetch takes it and 1.0 is the
        version left for the walk-ahead batch.
        """
        wheels: list[WheelFile | SdistFile] = [
            make_wheel("2.0"),
            make_sdist("1.0"),
            make_wheel("1.0"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", _ANY_VERSION)
        items = coordinator.calls_to("request_metadata_batch")[-1][0]
        assert any(
            ver == "1.0" and url.endswith(".whl.metadata") for _, ver, url, _ in items
        )

    def test_skips_sdist_only_versions(self) -> None:
        """Sdist-only versions never appear in the batch but consume a slot."""
        wheels: list[WheelFile | SdistFile] = [
            make_sdist("3.0"),
            make_wheel("2.0"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", _ANY_VERSION)
        items = coordinator.calls_to("request_metadata_batch")[-1][0]
        versions = [ver for _, ver, _, _ in items]
        assert versions == ["2.0"]

    def test_skips_wheels_without_metadata_url(self) -> None:
        """Wheels lacking a PEP 658/714 metadata pointer are skipped."""
        wheels = [
            make_wheel("2.0", has_metadata=False),
            make_wheel("1.0"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", _ANY_VERSION)
        items = coordinator.calls_to("request_metadata_batch")[-1][0]
        versions = [ver for _, ver, _, _ in items]
        assert versions == ["1.0"]

    def test_no_batch_when_all_filtered(self) -> None:
        """Empty items list (everything cached or sdist-only) skips the call."""
        wheels = [make_sdist("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset()
        provider._prefetch_walk_ahead("foo", _ANY_VERSION)
        assert not coordinator.calls_to("request_metadata_batch")

    def test_fires_when_pipelined_scan_engages(self) -> None:
        """Trip the look-ahead scan: walk_ahead fires with the range being scanned."""
        # foo's first candidate (3.0) requires bar<2.0 but the resolver has
        # already decided bar==3.0; that rejection drives _run_full_scan into
        # _scan_candidates_pipelined, which fires the deep prefetch.
        wheels = [make_wheel(v) for v in ("3.0", "2.0", "1.0")]
        meta = (
            "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar<2.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta.format(ver=v) for v in ("3.0", "2.0", "1.0")},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})

        version_range = SpecifierSet(">=2.0").to_range()
        with patch.object(
            provider, "_prefetch_walk_ahead", wraps=provider._prefetch_walk_ahead
        ) as spy:
            provider.choose_version("foo", version_range)
        spy.assert_called_with("foo", version_range)

    def test_does_not_fire_when_first_candidate_accepted(self) -> None:
        """Accepting the first candidate skips pipelined scans and deep prefetch."""
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_text="Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            package="foo",
        )
        root_reqs = {"foo": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        with patch.object(
            provider, "_prefetch_walk_ahead", wraps=provider._prefetch_walk_ahead
        ) as spy:
            provider.choose_version("foo", SpecifierSet(">=1.0").to_range())
        spy.assert_not_called()


class TestPipelinedScanDefersUnreadMetadata:
    """The scan decodes what it reads and leaves the rest of the batch queued."""

    @staticmethod
    def _provider_over_twelve_versions() -> Provider:
        """A provider whose scan rejects foo>=10.0 and settles on 9.0.

        ``bar`` is decided at 1.0, which the top three versions exclude.  With
        twelve versions the first prefetch batch reaches below 9.0, so it holds
        candidates no read ever asks about.
        """
        versions = [f"{n}.0" for n in range(1, 13)]
        metadata = {
            v: (
                "Metadata-Version: 2.1\nName: foo\n"
                f"Version: {v}\n"
                f"Requires-Dist: bar{'>=2' if V(v) >= V('10.0') else '<2'}\n"
            )
            for v in versions
        }
        coordinator = make_coordinator(
            [make_wheel(v) for v in versions],
            package="foo",
            metadata_by_version=metadata,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
        )
        provider.solution_decisions["bar"] = V("1.0")
        return provider

    def test_batch_below_the_pick_stays_undecoded(self) -> None:
        """Versions the scan never reaches are queued, not parsed."""
        provider = self._provider_over_twelve_versions()

        assert provider.choose_version("foo", VersionRange.full()) == V("9.0")

        assert ("foo", V("5.0")) in provider.pending_metadata_parses
        assert ("foo", V("5.0")) not in provider.deps_cache
        assert ("foo", V("9.0")) in provider.deps_cache

    def test_later_read_serves_the_queued_metadata(self) -> None:
        """A read of a queued version decodes it without a metadata fetch."""
        provider = self._provider_over_twelve_versions()
        provider.choose_version("foo", VersionRange.full())
        fetched = provider.stats.metadata_fetched

        deps = provider.get_dependencies("foo", V("5.0"))

        assert deps == {"bar": SpecifierSet("<2").to_range()}
        assert provider.stats.metadata_fetched == fetched
        assert ("foo", V("5.0")) not in provider.pending_metadata_parses


class TestIsReady:
    def test_extras_cached_base(self) -> None:
        """is_ready for extras returns True when base is cached."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        provider.versions_cache["foo"] = [(V("1.0"), make_wheel("1.0"))]
        assert provider.is_ready("foo[security]") is True

    def test_extras_not_cached(self) -> None:
        """is_ready for extras returns False when base is not cached."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        assert provider.is_ready("foo[security]") is False

    def test_base_cached(self) -> None:
        """is_ready returns True when package is in versions cache."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        provider.versions_cache["foo"] = [(V("1.0"), make_wheel("1.0"))]
        assert provider.is_ready("foo") is True

    def test_base_in_index_not_cache(self) -> None:
        """is_ready returns True when listing is in index but not cache."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        # Listing is in the index (set by make_coordinator) but not
        # in versions_cache.
        assert provider.is_ready("foo") is True

    def test_base_not_in_index(self) -> None:
        """is_ready returns False when listing is not in index."""
        coordinator = make_coordinator(None, package="foo")
        provider = Provider(coordinator)
        assert provider.is_ready("foo") is False


class TestSpeculativePrefetchSkipsNonWheelDists:
    def test_skips_sdists_in_root_batch(self) -> None:
        """Prefetch skips sdists (no metadata_url) in root batch."""
        wheels = [make_sdist("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_text="Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            package="foo",
        )
        root_reqs = {"foo": SpecifierSet(">=1.0").to_range()}
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements=root_reqs,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        provider.fetch_versions("foo")
        assert _prefetched_batch_versions(coordinator) == ["1.0"]


class TestChooseVersionBatchLoop:
    def test_batch_loop_iterates_multiple_chunks(self) -> None:
        """choose_version processes multiple batch chunks."""
        # Create enough versions to need more than one PREFETCH_BATCH
        # cycle in the remaining candidates loop.
        n = 35
        wheels = [make_wheel(f"{v}.0") for v in range(n, 0, -1)]
        metadata_by_version = {}
        for v in range(n, 0, -1):
            ver = f"{v}.0"
            # All versions conflict with root except v1.0.
            bar_req = "bar>=0.1" if ver == "1.0" else f"bar>={ver}"
            metadata_by_version[ver] = (
                f"Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n"
                f"Requires-Dist: {bar_req}\n"
            )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version=metadata_by_version,
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements=root_reqs,
        )
        # Use a small batch size to force multiple iterations.
        provider.PREFETCH_BATCH = 5
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_broad_la_reject_cap_falls_back_to_root_only(self) -> None:
        """Once broad_rejections crosses the cap, look-ahead falls
        back to root-only checking and stops counting rejections.

        Lower the cap to 1 so the second rejection trips the cap, the
        third+ rejections take the ``check_decisions=False`` branch,
        and the fall-back path eventually accepts a candidate via
        root-only forward checking.
        """
        n = 5
        wheels = [make_wheel(f"{v}.0") for v in range(n, 0, -1)]
        metadata_by_version = {}
        for v in range(n, 0, -1):
            ver = f"{v}.0"
            # Every version requires bar>=ver; root constrains
            # bar<2.0, so only 1.0 is compatible by root forward
            # checking.  All higher versions get rejected.
            bar_req = "bar>=0.1" if ver == "1.0" else f"bar>={ver}"
            metadata_by_version[ver] = (
                f"Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n"
                f"Requires-Dist: {bar_req}\n"
            )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version=metadata_by_version,
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements=root_reqs,
        )
        provider._BROAD_LA_REJECT_CAP = 1
        provider.PREFETCH_BATCH = 5
        spec = SpecifierSet("")
        # 5.0..2.0 fail root forward check; 1.0 passes.  After the
        # cap is exhausted the loop keeps iterating with
        # ``check_decisions=False``.
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")
        # We exhausted the cap then continued; total rejections
        # should be roughly the number of failing candidates.
        assert provider.stats.look_ahead_rejections >= n - 1


class TestScanBatchNoFirstCandidate:
    """Cover the `first_candidate is None` skip in _scan_batch."""

    def test_no_abort_when_first_candidate_missing(self) -> None:
        """A scan started without first_candidate cannot trigger an abort."""
        wheels = [make_wheel(str(i)) for i in range(20, 0, -1)]
        meta_by_version = {
            str(i): "Metadata-Version: 2.1\nName: foo\nVersion: "
            + str(i)
            + "\nRequires-Dist: bar==99\n"
            for i in range(20, 0, -1)
        }
        coordinator = make_coordinator(
            wheels, metadata_by_version=meta_by_version, package="foo"
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("1")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = 2  # type: ignore[misc]
        wheel_by = {V(str(i)): wheels[20 - i] for i in range(20, 0, -1)}
        outcome, _ = provider._scan_batch(
            "foo",
            [V(str(i)) for i in range(20, 0, -1)],
            broad_rejections=0,
            first_candidate=None,
        )
        del wheel_by
        # No abort can fire without a first_candidate; the scan returns None.
        assert outcome is None
