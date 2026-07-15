"""Tests for Provider with mocked network access."""

from __future__ import annotations

import asyncio
import io
import tarfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import MagicMock, patch

import pytest

from nab_index.client import (
    MetadataHashMismatchError,
    SdistFile,
    SdistHashMismatchError,
    WheelFile,
    _parse_files,
)
from nab_index.local_index import LocalIndexClient
from nab_python._provider import build_remote, metadata_resolver
from nab_python._provider.metadata_resolver import (
    add_classified_dep,
    cache_deps_from_metadata,
    classify_requirement,
    pick_dist_for_metadata,
)
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._testing.overrides import pkg_override
from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.tags import Tag
from nab_python._vendor.packaging.version import InvalidVersion, Version
from nab_python.config import (
    IndexOverride,
    NabProjectConfig,
    OverrideConflictError,
    PackageOverride,
)
from nab_python.fetch import InMemoryIndex
from nab_python.metadata import WheelMetadata
from nab_python.provider import (
    BuildPolicy,
    DistPolicy,
    ExtrasMode,
    InvalidUploadTimeError,
    LocalSource,
    MetadataError,
    MissingExtraError,
    Provider,
    ResolutionStrategy,
    UnsupportedSdistError,
    UnsupportedVcsError,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from nab_python.resolve import _build_resolver_inputs, _raise_for_source_python
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget
from nab_resolver.resolver import ResolutionError, Resolver
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

V = Version

# The target most tests resolve against: the host machine, impersonating
# CPython 3.12.0.  Shared because it is frozen and building one walks the
# host's whole tag set.
_PY312 = ResolveTarget.for_host_python("3.12.0")


def make_wheel(
    version: str = "1.0",
    requires_python: str | None = None,
    has_metadata: bool = True,
    upload_time: str | None = None,
    local_path: Path | None = None,
) -> WheelFile:
    """Build a WheelFile for testing."""
    return WheelFile(
        filename=f"pkg-{version}-py3-none-any.whl",
        url=f"https://example.com/pkg-{version}-py3-none-any.whl",
        version=version,
        requires_python=requires_python,
        has_metadata=has_metadata,
        upload_time=upload_time,
        local_path=local_path,
    )


def make_sdist(
    version: str = "1.0",
    requires_python: str | None = None,
    upload_time: str | None = None,
    local_path: Path | None = None,
) -> SdistFile:
    """Build a SdistFile for testing."""
    return SdistFile(
        filename=f"pkg-{version}.tar.gz",
        url=f"https://example.com/pkg-{version}.tar.gz",
        version=version,
        requires_python=requires_python,
        upload_time=upload_time,
        local_path=local_path,
    )


def _prefetched_batch_versions(coordinator: MagicMock) -> list[str]:
    """Versions of the one batch metadata prefetch, in submission order."""
    coordinator.request_metadata_batch.assert_called_once()
    items = coordinator.request_metadata_batch.call_args[0][0]
    return [version for _package, version, _url, _hash in items]


def _done_event() -> threading.Event:
    """Return an already-set Event."""
    ev = threading.Event()
    ev.set()
    return ev


def _make_sdist_targz() -> bytes:
    """Return .tar.gz bytes for a one-file sdist rooted at pkg-1.0."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = b"[project]\nname = 'pkg'\n"
        info = tarfile.TarInfo(name="pkg-1.0/pyproject.toml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


_SDIST_TARGZ = _make_sdist_targz()


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
        coordinator.request_metadata.assert_called_once()
        _package, version, _url, _hash = coordinator.request_metadata.call_args[0]
        assert version == "2.0"

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
        coordinator.request_metadata.assert_not_called()
        coordinator.request_metadata_batch.assert_not_called()

    def test_no_prefetch_when_already_cached(self) -> None:
        """No speculative prefetch if deps are already cached."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        provider.deps_cache[("foo", V("1.0"))] = {}
        provider.fetch_versions("foo")
        coordinator.request_metadata.assert_not_called()
        coordinator.request_metadata_batch.assert_not_called()

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
        coordinator.request_metadata_batch.assert_not_called()

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


class TestMembershipSetMarkerInMetadata:
    """A Requires-Dist marker testing extras/dependency_groups drops only the dep.

    The set variables are empty at resolve time, so the membership tests False
    and the gated dep is excluded. The candidate version itself must survive,
    matching the root-requirement handling (test_root_extras_set_marker_warns).
    """

    def test_extras_membership_marker_keeps_version(self) -> None:
        meta_text = (
            "Metadata-Version: 2.3\nName: foo\nVersion: 1.0\n"
            "Provides-Extra: docs\n"
            "Requires-Dist: realdep>=1.0\n"
            'Requires-Dist: somedep; "docs" in extras\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=meta_text, package="foo"
        )
        provider = Provider(coordinator)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "realdep" in deps
        assert "somedep" not in deps

    def test_dependency_groups_membership_marker_keeps_version(self) -> None:
        meta_text = (
            "Metadata-Version: 2.3\nName: foo\nVersion: 1.0\n"
            "Requires-Dist: realdep>=1.0\n"
            'Requires-Dist: somedep; "dev" in dependency_groups\n'
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=meta_text, package="foo"
        )
        provider = Provider(coordinator)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "realdep" in deps
        assert "somedep" not in deps


class TestFetchVersions:
    def test_caches_results(self) -> None:
        """Second call returns cached results without re-fetching."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        provider.fetch_versions("foo")
        assert provider.stats.listings_fetched == 1

    def test_skips_unparseable_version(self) -> None:
        """Wheels with invalid version strings are skipped."""
        coordinator = make_coordinator([make_wheel("not-a-version!")], package="foo")
        provider = Provider(coordinator)
        assert provider.fetch_versions("foo") == []

    def test_filters_requires_python(self) -> None:
        """Wheels that don't match python_version are excluded."""
        coordinator = make_coordinator(
            [make_wheel("1.0", requires_python=">=3.12")], package="foo"
        )
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.10.0"))
        assert provider.fetch_versions("foo") == []

    def test_keeps_matching_requires_python(self) -> None:
        """Wheels matching python_version are kept."""
        coordinator = make_coordinator(
            [make_wheel("1.0", requires_python=">=3.10")], package="foo"
        )
        provider = Provider(coordinator, target=_PY312)
        assert len(provider.fetch_versions("foo")) == 1

    def test_invalid_requires_python_keeps_package(self) -> None:
        """Invalid requires-python string doesn't exclude the wheel."""
        coordinator = make_coordinator(
            [make_wheel("1.0", requires_python=">>>invalid")], package="foo"
        )
        provider = Provider(coordinator, target=_PY312)
        assert len(provider.fetch_versions("foo")) == 1

    def test_non_string_requires_python_admitted_without_crash(self) -> None:
        """A numeric requires-python from a non-conformant index is dropped.

        PEP 691 mandates a string; a JSON number would reach SpecifierSet and
        raise an uncaught TypeError that aborts the resolve. The parser coerces
        it to None, so the wheel is admitted with no Python constraint.
        """
        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://example.com/foo/foo-1.0-py3-none-any.whl",
                    "requires-python": 3.7,
                }
            ]
        }
        files = _parse_files(data, "https://example.com/", "foo")
        assert files[0].requires_python is None
        coordinator = make_coordinator(files, package="foo")
        provider = Provider(coordinator, target=_PY312)
        assert len(provider.fetch_versions("foo")) == 1

    def test_requires_python_cache_hit_exercises_cached_branch(self) -> None:
        """Repeated calls with the same requires-python use the cache.

        The first ``_excluded_by_python`` call parses ``SpecifierSet``
        and stores the boolean result; the second call with the same
        spec string hits the cache and skips parsing.  Covers the
        cache-hit branch in :meth:`Provider._excluded_by_python`.
        """
        coordinator = make_coordinator(
            [
                make_wheel("1.0", requires_python=">=3.12"),
                make_wheel("2.0", requires_python=">=3.12"),
            ],
            package="foo",
        )
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.10.0"))
        # Two wheels, same requires-python string, both excluded.
        assert provider.fetch_versions("foo") == []
        # Cache should have one entry: the parsed exclusion verdict.
        assert provider.requires_python_cache == {">=3.12": True}

    def test_no_python_version_skips_filter(self) -> None:
        """When python_version is None, requires-python is not checked."""
        coordinator = make_coordinator(
            [make_wheel("1.0", requires_python=">=99.0")], package="foo"
        )
        provider = Provider(coordinator, target=None)
        assert len(provider.fetch_versions("foo")) == 1

    def test_requires_python_filter_honors_python_overlay(self) -> None:
        """The Requires-Python filter targets the impersonated Python.

        Under a marker-environment overlay the candidate filter must use
        the overlaid Python, not the host, so it agrees with how markers
        are evaluated.
        """
        coordinator = make_coordinator(
            [
                make_wheel("1.0", requires_python=">=3.8,<3.9"),
                make_wheel("2.0", requires_python=">=3.12"),
            ],
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312.with_marker_overrides(
                {"python_version": "3.8", "python_full_version": "3.8.0"}
            ),
            build_policy=BuildPolicy.NEVER,
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("1.0")]

    def test_flat_wheelhouse_requires_python_excludes_candidate(
        self, tmp_path: Path
    ) -> None:
        """A flat find-links wheel's METADATA Requires-Python filters candidates."""
        for version, requires_python in (("2.0", ">=3.12"), ("1.0", ">=3.8")):
            wheel = tmp_path / f"foo-{version}-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as zf:
                zf.writestr("foo/__init__.py", b"")
                zf.writestr(
                    f"foo-{version}.dist-info/METADATA",
                    f"Metadata-Version: 2.1\nName: foo\nVersion: {version}\n"
                    f"Requires-Python: {requires_python}\n",
                )
        records = asyncio.run(LocalIndexClient(tmp_path.as_uri()).get_files("foo"))
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.8.0"))
        versions = [v for v, _ in provider.filter_distributions("foo", records)]
        assert versions == [V("1.0")]

    def test_flat_wheelhouse_sdist_requires_python_excludes_candidate(
        self, tmp_path: Path
    ) -> None:
        """A flat find-links sdist's PKG-INFO Requires-Python filters candidates."""
        for version, requires_python in (("2.0", ">=3.12"), ("1.0", ">=3.8")):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                body = (
                    f"Metadata-Version: 2.2\nName: foo\nVersion: {version}\n"
                    f"Requires-Python: {requires_python}\n"
                ).encode()
                info = tarfile.TarInfo(name=f"foo-{version}/PKG-INFO")
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
            (tmp_path / f"foo-{version}.tar.gz").write_bytes(buf.getvalue())
        records = asyncio.run(LocalIndexClient(tmp_path.as_uri()).get_files("foo"))
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.8.0"))
        versions = [v for v, _ in provider.filter_distributions("foo", records)]
        assert versions == [V("1.0")]

    def test_python_version_only_overlay_syncs_full_version(self) -> None:
        """An overlay with python_version alone syncs the whole axis.

        Overlaying ``python_version = "3.8"`` moves ``python_full_version``
        off the host patch level and sets the Requires-Python filter target
        to the impersonated full release.
        """
        coordinator = make_coordinator(
            [
                make_wheel("1.0", requires_python=">=3.8,<3.9"),
                make_wheel("2.0", requires_python=">=3.12"),
            ],
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.12.3").with_marker_overrides(
                {"python_version": "3.8"}
            ),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["python_version"] == "3.8"
        assert provider.environment["python_full_version"] == "3.8.0"
        assert provider.python_version == "3.8.0"
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("1.0")]

    def test_sorted_descending(self) -> None:
        """Results are sorted newest-first."""
        wheels = [make_wheel(v) for v in ("1.0", "3.0", "2.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("3.0"), V("2.0"), V("1.0")]

    def test_normalizes_package_name(self) -> None:
        """Fetching 'Foo-Bar' and 'foo-bar' shares the cache."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo-bar")
        provider = Provider(coordinator)
        provider.fetch_versions("Foo-Bar")
        provider.fetch_versions("foo_bar")
        assert provider.stats.listings_fetched == 1


class TestChooseVersion:
    def test_picks_newest_in_range(self) -> None:
        """choose_version returns the newest version passing the filter."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0,<3.0")
        assert provider.choose_version("foo", spec.to_range()) == V("2.0")

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
        provider._no_versions_reasons["foo"] = "sentinel"
        assert not provider.has_satisfying_version(
            "foo", SpecifierSet(">=5.0").to_range()
        )
        assert provider.get_no_versions_reason("foo") == "sentinel"

    def test_preserves_prior_abort_state(self) -> None:
        """Abort markers and force-backtrack counts survive the probe."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        provider._lookahead_aborted["bar"] = ("baz", V("1.0"))
        provider._force_backtrack_counts["baz"] = 2
        provider.has_satisfying_version("foo", VersionRange.full())
        assert provider._lookahead_aborted == {"bar": ("baz", V("1.0"))}
        assert provider._force_backtrack_counts == {"baz": 2}


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
        assert (
            provider.get_no_versions_reason("foo")
            == "no version matches the requirement"
        )

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
            provider.get_no_versions_reason("foo")
            == "package not found on any configured index"
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


class TestNoVersionsReasons:
    """``_record_no_versions_reason`` captures provider-side hints."""

    def test_empty_listing_records_not_found(self) -> None:
        """An empty listing surfaces ``not found on any configured index``."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider.choose_version("foo", SpecifierSet("").to_range())
        assert (
            provider.get_no_versions_reason("foo")
            == "package not found on any configured index"
        )

    def test_no_match_in_range_records_no_match_reason(self) -> None:
        """A non-empty listing with no in-range version surfaces a no-match reason."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        provider.choose_version("foo", SpecifierSet(">=5.0").to_range())
        assert (
            provider.get_no_versions_reason("foo")
            == "no version matches the requirement"
        )

    def test_present_but_requires_python_filtered_reports_incompatible(self) -> None:
        """A requires-python-filtered package reports incompatible, not absent."""
        coordinator = make_coordinator(
            [
                make_wheel("1.0", requires_python=">=3.13"),
                make_wheel("2.0", requires_python=">=3.13"),
                make_wheel("3.0", requires_python=">=3.13"),
            ],
            package="foo",
        )
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.9.0"))
        provider.choose_version("foo", SpecifierSet("").to_range())
        assert (
            provider.get_no_versions_reason("foo")
            == "found on index but no distribution is compatible "
            "(all filtered by requires-python, dist-policy, or upload-time)"
        )

    def test_present_but_wheel_tags_incompatible_names_the_tags(self) -> None:
        """A Windows-only package on a Linux target reads like pip's message.

        pywin32 is the real case: every wheel is ``win_*`` and there is no
        sdist, so the target has nothing to install and the reason has to
        say so rather than blame requires-python.
        """
        wheels = [
            WheelFile(
                filename=f"foo-1.0-cp311-cp311-{tag}.whl",
                url=f"https://example.com/foo-1.0-cp311-cp311-{tag}.whl",
                version="1.0",
                requires_python=None,
                has_metadata=True,
                upload_time=None,
            )
            for tag in ("win_amd64", "win32")
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_declared(
                python_version="3.11", spec=PlatformSpec("linux_x86_64")
            ),
        )
        provider.choose_version("foo", SpecifierSet("").to_range())
        assert provider.get_no_versions_reason("foo") == (
            "found on index but none of the wheel's tags are compatible with"
            " the resolve target (2 wheels rejected), and no sdist is"
            " available to build from"
        )

    def test_present_but_dist_policy_filtered_reports_incompatible(self) -> None:
        """A dist-policy-filtered package reports incompatible, not absent."""
        coordinator = make_coordinator([make_sdist("1.0")], package="foo")
        provider = Provider(coordinator, dist_policy=DistPolicy.WHEEL_ONLY)
        provider.choose_version("foo", SpecifierSet("").to_range())
        assert (
            provider.get_no_versions_reason("foo")
            == "found on index but no distribution is compatible "
            "(all filtered by requires-python, dist-policy, or upload-time)"
        )

    def test_get_reason_returns_none_for_unknown_package(self) -> None:
        """Packages without a recorded reason return ``None``."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        assert provider.get_no_versions_reason("never-asked") is None

    def test_lookahead_rejection_names_the_blocker(self) -> None:
        """When every candidate is rejected by look-ahead because a
        transitive dep conflicts with a decided package, the recorded
        reason names the blocker rather than saying ``no version
        matches the requirement``.

        Scenario: ``foo`` 1.0 requires ``bar==2.0``.  The resolver
        has already decided ``bar==1.0``.  ``choose_version("foo",
        full())`` returns ``None`` because every candidate is
        blocked by the bar decision.  The recorded reason should
        mention bar.
        """
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: bar==2.0\n"
            ),
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
        )
        # Pretend the resolver decided bar==1.0 already.
        provider.solution_decisions["bar"] = V("1.0")
        result = provider.choose_version("foo", VersionRange.full())
        assert result is None
        reason = provider.get_no_versions_reason("foo")
        assert reason is not None
        assert "bar" in reason
        assert "every version in range was rejected" in reason

    def test_blocker_reason_outlives_a_later_empty_range(self) -> None:
        """A later ask over an empty range keeps the blocker reason.

        ``foo`` 1.0 requires ``bar==2.0`` and the resolver has already
        decided ``bar==1.0``, so the reason naming bar must survive a
        second ask that finds no candidate at all.
        """
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: bar==2.0\n"
            ),
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
        )
        provider.solution_decisions["bar"] = V("1.0")
        assert provider.choose_version("foo", VersionRange.full()) is None

        # Nothing falls in this range, so the second ask has no blockers.
        assert provider.choose_version("foo", SpecifierSet(">=5.0").to_range()) is None

        assert (
            provider.get_no_versions_reason("foo")
            == "every version in range was rejected: requires bar != 1.0"
        )

    def test_sdist_only_under_dynamic_local_names_build_policy(self) -> None:
        """When every candidate is rejected because the package is
        sdist-only with dynamic deps and the build policy refuses to
        build remote sdists, the recorded reason must name the build
        policy.  Regression: previously this fell through to a
        misleading "PEP 440 excludes pre-releases" message even when
        all the candidates were finals.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0"), make_sdist("2.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            package="pkg",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.BUILD_LOCAL,
            root_requirements={"pkg": VersionRange.full(admit_arbitrary=False)},
        )
        result = provider.choose_version("pkg", VersionRange.full())
        assert result is None
        reason = provider.get_no_versions_reason("pkg")
        assert reason is not None
        assert "every version in range was rejected" in reason
        assert "dynamic dependencies" in reason
        assert "build-local" in reason
        # The misleading PEP 440 narrative must NOT appear.
        assert "pre-release" not in reason
        assert "PEP 440" not in reason

    def test_lookahead_blockers_filtered_by_package(self) -> None:
        """``_capture_lookahead_blockers`` returns only blockers for the
        given candidate.  Blockers stored against another candidate
        (left over from a prior choose_version call) are skipped.
        """
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        # Plant a blocker for a different candidate in each of the
        # three pending stores so all the filter branches fire.
        provider.pending_blocks[("other", "bar", V("1.0"))].append(V("1.0"))
        provider.pending_range_blocks[
            ("other", "baz", SpecifierSet("<2.0").to_range())
        ].append(V("1.0"))
        provider.pending_root_blocks[
            (
                "other",
                "qux",
                SpecifierSet("==2.0").to_range(),
                SpecifierSet("==1.0").to_range(),
            )
        ].append(V("1.0"))
        assert provider._capture_lookahead_blockers("foo") == []

    def test_root_requirement_rejection_names_the_blocker(self) -> None:
        """When the rejection comes from the root-requirement check at
        the top of look_ahead_ok (``foo`` 1.0 requires ``bar==2.0``
        but the root requires ``bar==1.0``), the diagnostic must
        still name ``bar`` even though no decision/positive-range
        block was recorded.  The root-requirements check returns
        early; we capture the cause via
        ``pending_root_blocks``.
        """
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: bar==2.0\n"
            ),
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={
                "foo": VersionRange.full(admit_arbitrary=False),
                "bar": SpecifierSet("==1.0").to_range(),
            },
        )
        result = provider.choose_version("foo", VersionRange.full())
        assert result is None
        reason = provider.get_no_versions_reason("foo")
        assert reason is not None
        assert "bar" in reason
        assert "root has it in" in reason

    def test_range_block_rejection_names_the_blocker(self) -> None:
        """Same as above but the blocker is a positive-range constraint
        rather than a singleton decision.  ``foo`` 1.0 requires
        ``bar==2.0``; the solution's positive range for ``bar`` is
        ``<2.0`` (set by some other dependency that has not yet been
        decided).
        """
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: bar==2.0\n"
            ),
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
        )
        pos_range = SpecifierSet("<2.0").to_range()
        dep_range = SpecifierSet("==2.0").to_range()
        provider.solution_ranges["bar"] = pos_range
        result = provider.choose_version("foo", VersionRange.full())
        assert result is None
        reason = provider.get_no_versions_reason("foo")
        assert reason is not None
        # foo requires bar==2.0; the message must name that, not the solution range.
        assert (
            f"requires bar in {dep_range} but solution has it in {pos_range}" in reason
        )
        assert "disjoint with current solution range" not in reason


class TestGetDependencies:
    def test_returns_deps_from_metadata(self) -> None:
        """Parse Requires-Dist from fetched metadata."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: bar>=2.0\n"
                "Requires-Dist: baz\n"
            ),
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps
        assert "baz" in deps

    def test_duplicate_requires_dist_intersects(self) -> None:
        """Two Requires-Dist lines for one name intersect, not overwrite."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: bar>=2.0\n"
                "Requires-Dist: bar<5.0\n"
            ),
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert V("3.0") in deps["bar"]
        assert V("1.0") not in deps["bar"]
        assert V("9.0") not in deps["bar"]

    def test_caches_dependencies(self) -> None:
        """Second call returns cached deps."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text="Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        provider.get_dependencies("foo", V("1.0"))
        provider.get_dependencies("foo", V("1.0"))
        # Metadata should only be requested once (via speculative prefetch
        # or direct request), second call uses the deps cache
        assert ("foo", V("1.0")) in provider.deps_cache

    def test_metadata_hash_mismatch_aborts_no_sdist_fallback(self) -> None:
        """A recorded PEP 658 integrity failure aborts get_dependencies rather
        than degrading to the sdist's PKG-INFO deps."""
        coordinator = make_coordinator(
            [make_wheel("1.0"), make_sdist("1.0")],
            sdist_pkg_info=(
                "Metadata-Version: 2.2\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: from-sdist\n"
            ),
            package="foo",
        )
        coordinator.index.store_metadata_error(
            "foo", "1.0", MetadataHashMismatchError("metadata sha256 mismatch")
        )
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(MetadataHashMismatchError):
            provider.get_dependencies("foo", V("1.0"))
        assert ("foo", V("1.0")) not in provider.deps_cache

    def test_sdist_hash_mismatch_aborts(self) -> None:
        """A recorded sdist integrity failure raises from get_dependencies."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=(
                "Metadata-Version: 2.2\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: from-sdist\n"
            ),
            package="foo",
        )

        def _poisoned_request_sdist(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            coordinator.index.store_sdist_metadata_error(
                pkg, ver, SdistHashMismatchError("sdist sha256 mismatch")
            )
            return _done_event()

        coordinator.request_sdist.side_effect = _poisoned_request_sdist
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(SdistHashMismatchError):
            provider.get_dependencies("foo", V("1.0"))
        assert ("foo", V("1.0")) not in provider.deps_cache

    def test_unknown_version_raises(self) -> None:
        """Raise MetadataError when version doesn't exist."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        with pytest.raises(MetadataError):
            provider.get_dependencies("foo", V("9.0"))

    def test_unknown_package_raises(self) -> None:
        """Raise MetadataError for an unknown package."""
        coordinator = make_coordinator([], package="missing")
        provider = Provider(coordinator)
        with pytest.raises(MetadataError):
            provider.get_dependencies("missing", V("1.0"))

    def test_invalid_metadata_raises(self) -> None:
        """Raise MetadataError when metadata cannot be parsed."""
        bad_metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Requires-Dist: pytz (>dev)\n"
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=bad_metadata, package="foo"
        )
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(MetadataError, match="Invalid metadata"):
            provider.get_dependencies("foo", V("1.0"))

    def test_malformed_requires_python_drops_candidate(self) -> None:
        """A version whose METADATA Requires-Python is invalid is refused.

        ``!=3.3*`` is not a valid PEP 440 specifier, so the metadata is
        invalid and ``get_dependencies`` raises ``MetadataError`` instead of
        pinning the version with the Python constraint silently ignored.
        """
        bad_metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Requires-Python: >=2.7, !=3.3*\n"
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=bad_metadata, package="foo"
        )
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(MetadataError, match="invalid Requires-Python"):
            provider.get_dependencies("foo", V("1.0"))

    def test_invalid_metadata_is_negatively_cached(self) -> None:
        """Repeating a parse-failed lookup short-circuits to the cached error.

        A version whose METADATA fails to parse (malformed Requires-Dist,
        invalid specifier, etc.) is cached on the provider so subsequent
        ``get_dependencies`` calls return the same diagnostic without
        re-parsing or re-fetching.  Real-world impact: ancient boto3
        versions whose dep strings are not PEP 440 conformant are tried
        many times under ``lowest-direct`` and each previously paid the
        full parse cost.
        """
        bad_metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Requires-Dist: pytz (>dev)\n"
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=bad_metadata, package="foo"
        )
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(MetadataError, match="Invalid metadata"):
            provider.get_dependencies("foo", V("1.0"))
        # Make the underlying parse blow up if we go through it again --
        # the cached entry must short-circuit before then.
        coordinator.index._metadata[("foo", "1.0")] = "this is not METADATA"
        with pytest.raises(MetadataError, match="Invalid metadata"):
            provider.get_dependencies("foo", V("1.0"))

    def test_invalid_metadata_warning_fires_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ``Skipping ...`` warning is emitted only on the first failure.

        Surfaces the bad-metadata version once so a user reading the log
        knows that version was dropped, then stays silent on the many
        re-tries the resolver performs against the same (cached) entry.
        """
        bad_metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            "Requires-Dist: pytz (>dev)\n"
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=bad_metadata, package="foo"
        )
        provider = Provider(coordinator, target=_PY312)
        with caplog.at_level("WARNING", logger="nab_python.provider"):
            with pytest.raises(MetadataError):
                provider.get_dependencies("foo", V("1.0"))
            with pytest.raises(MetadataError):
                provider.get_dependencies("foo", V("1.0"))
            with pytest.raises(MetadataError):
                provider.get_dependencies("foo", V("1.0"))
        skipping = [r for r in caplog.records if "Skipping" in r.getMessage()]
        assert len(skipping) == 1
        assert "foo==1.0" in skipping[0].getMessage()

    def test_no_metadata_raises(self) -> None:
        """Raise MetadataError when no PEP 658 metadata and sdists disabled."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False)], package="foo"
        )
        provider = Provider(coordinator)
        with pytest.raises(MetadataError):
            provider.get_dependencies("foo", V("1.0"))

    def test_local_wheel_without_sidecar_reads_metadata_from_zip(
        self, tmp_path: Path
    ) -> None:
        """A local wheel with no sidecar resolves by reading METADATA from the .whl."""
        wheel_path = tmp_path / "foo-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as zf:
            zf.writestr(
                "foo-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: bar>=2\n",
            )
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False, local_path=wheel_path)],
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps
        assert V("2.0") in deps["bar"]
        assert V("1.0") not in deps["bar"]

    def test_local_wheel_with_mismatched_dist_info_rejected(
        self, tmp_path: Path
    ) -> None:
        """A local wheel whose .dist-info names another distribution is rejected."""
        wheel_path = tmp_path / "foo-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as zf:
            zf.writestr(
                "bar-2.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: bar\nVersion: 2.0\nRequires-Dist: baz\n",
            )
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False, local_path=wheel_path)],
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(MetadataError):
            provider.get_dependencies("foo", V("1.0"))

    def test_filters_deps_by_marker(self) -> None:
        """Dependencies with non-matching markers are excluded."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                'Requires-Dist: bar; sys_platform == "fakeos"\n'
                "Requires-Dist: baz\n"
            ),
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" not in deps
        assert "baz" in deps

    def test_marker_uses_scenario_python_version(self) -> None:
        """python_version markers evaluate against the scenario's Python,
        not the host's. ``audioop-lts; python_version >= '3.13'`` must
        not be activated when the scenario asks for 3.11.
        """
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                'Requires-Dist: only313; python_version >= "3.13"\n'
                'Requires-Dist: only311; python_version == "3.11"\n'
            ),
            package="foo",
        )
        provider = Provider(coordinator, target=ResolveTarget.for_host_python("3.11"))
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "only313" not in deps
        assert "only311" in deps

    def test_invalid_python_version_raises(self) -> None:
        """A malformed ``python_version`` raises instead of silently
        evaluating markers against the host environment.
        """
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        with pytest.raises(InvalidVersion, match="'not-a-version'"):
            Provider(coordinator, target=ResolveTarget.for_host_python("not-a-version"))

    def test_arbitrary_equality_dep_is_literal_range(self) -> None:
        """``===`` deps round-trip as a literal-only range.

        ``packaging.ranges.VersionRange`` represents arbitrary-string
        equality as a literal that matches the original string but no
        PEP 440 ``Version``.  The resolver consumes that range like any
        other; it simply finds no candidates and backtracks naturally.
        """
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: bar===custom-build\n"
                "Requires-Dist: baz>=1.0\n"
            ),
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps
        assert "custom-build" in deps["bar"]
        assert V("1.0") not in deps["bar"]
        assert "baz" in deps


class TestTransitiveDirectUrlDep:
    """A direct-URL ``Requires-Dist`` is refused, not silently substituted.

    The root and constraint inputs and the universal per-tuple path already
    call ``admit_vcs_url`` then raise. Without the same check here a fetched
    package's ``bar @ https://...`` dep was recorded as a bare ``bar`` and
    resolved from the index, pinning the wrong artifact silently.
    """

    @staticmethod
    def _provider_for(url: str, vcs_config: VcsConfig | None = None) -> Provider:
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                f"Requires-Dist: bar @ {url}\n"
            ),
            package="foo",
        )
        return Provider(coordinator, target=_PY312, vcs_config=vcs_config)

    def test_plain_url_dep_refused(self) -> None:
        """A non-VCS direct-URL dep raises rather than pinning from the index."""
        provider = self._provider_for("https://example.com/bar-9.9-py3-none-any.whl")
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            provider.get_dependencies("foo", V("1.0"))

    def test_vcs_url_dep_blocked_by_default(self) -> None:
        """A VCS dep is refused under the default BLOCK policy."""
        provider = self._provider_for("git+https://example.com/bar.git@" + "a" * 40)
        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            provider.get_dependencies("foo", V("1.0"))

    def test_admitted_vcs_url_dep_raises_not_implemented(self) -> None:
        """An admitted VCS dep raises NotImplementedError; support is deferred."""
        provider = self._provider_for(
            "git+https://example.com/bar.git@" + "a" * 40,
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
            ),
        )
        with pytest.raises(NotImplementedError, match="not implemented"):
            provider.get_dependencies("foo", V("1.0"))

    def test_marker_gated_url_dep_not_applying_is_skipped(self) -> None:
        """A url dep gated by a marker that does not apply is skipped, not refused."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: bar @ https://example.com/bar.whl ;"
                ' python_version < "3.0"\n'
                "Requires-Dist: baz>=1.0\n"
            ),
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" not in deps
        assert "baz" in deps

    @staticmethod
    def _provider_with_extra_url(
        url: str,
        *,
        root_extras: set[tuple[str, str]] | None = None,
        vcs_config: VcsConfig | None = None,
    ) -> Provider:
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Provides-Extra: uvloop\n"
                f'Requires-Dist: bar @ {url} ; extra == "uvloop"\n'
                "Requires-Dist: baz>=1.0\n"
            ),
            package="foo",
        )
        return Provider(
            coordinator,
            target=_PY312,
            root_extras=root_extras,
            vcs_config=vcs_config,
        )

    def test_unrequested_extra_url_dep_does_not_abort_base(self) -> None:
        """A url dep gated on a provided-but-unrequested extra is skipped at base."""
        provider = self._provider_with_extra_url("https://example.com/bar.whl")
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" not in deps
        assert "baz" in deps

    def test_unrequested_extra_vcs_url_dep_does_not_abort_base(self) -> None:
        """A VCS url under an unrequested extra is also skipped at base."""
        provider = self._provider_with_extra_url(
            "git+https://example.com/bar.git@" + "a" * 40
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" not in deps
        assert "baz" in deps

    def test_selected_extra_url_dep_is_refused(self) -> None:
        """Selecting the extra fires the deferred direct-URL refusal."""
        provider = self._provider_with_extra_url("https://example.com/bar.whl")
        provider.get_dependencies("foo", V("1.0"))
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            provider.get_dependencies("foo[uvloop]", V("1.0"))

    def test_selected_extra_vcs_url_dep_is_refused(self) -> None:
        """Selecting the extra fires the deferred VCS refusal under BLOCK."""
        provider = self._provider_with_extra_url(
            "git+https://example.com/bar.git@" + "a" * 40
        )
        provider.get_dependencies("foo", V("1.0"))
        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            provider.get_dependencies("foo[uvloop]", V("1.0"))

    def test_selected_extra_admitted_vcs_url_raises_not_implemented(self) -> None:
        """An admitted VCS url under a selected extra raises NotImplementedError."""
        provider = self._provider_with_extra_url(
            "git+https://example.com/bar.git@" + "a" * 40,
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
            ),
        )
        provider.get_dependencies("foo", V("1.0"))
        with pytest.raises(NotImplementedError, match="not implemented"):
            provider.get_dependencies("foo[uvloop]", V("1.0"))

    def test_root_requested_extra_url_dep_refused_eagerly(self) -> None:
        """When the extra is a root extra, the url dep is refused at base parse."""
        provider = self._provider_with_extra_url(
            "https://example.com/bar.whl",
            root_extras={("foo", "uvloop")},
        )
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            provider.get_dependencies("foo", V("1.0"))


class TestAddClassifiedDep:
    """``add_classified_dep`` intersects duplicate dependency names."""

    def test_base_deps_intersect(self) -> None:
        """A name seen twice as a base dep folds to the intersection."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {}
        add_classified_dep(Requirement("bar>=2.0"), set(), base, extra_map)
        add_classified_dep(Requirement("bar<5.0"), set(), base, extra_map)
        assert V("3.0") in base["bar"]
        assert V("1.0") not in base["bar"]
        assert V("9.0") not in base["bar"]

    def test_extra_deps_intersect(self) -> None:
        """A name seen twice under one extra folds to the intersection."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {"x": {}}
        add_classified_dep(Requirement("bar>=2.0"), {"x"}, base, extra_map)
        add_classified_dep(Requirement("bar<5.0"), {"x"}, base, extra_map)
        assert V("3.0") in extra_map["x"]["bar"]
        assert V("1.0") not in extra_map["x"]["bar"]
        assert V("9.0") not in extra_map["x"]["bar"]

    def test_base_multi_extra_splits_into_one_proxy_each(self) -> None:
        """``bar[a,b]`` as a base dep records ``bar`` plus a proxy per extra."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {}
        add_classified_dep(Requirement("bar[a,b]>=1.0"), set(), base, extra_map)
        assert V("2.0") in base["bar"]
        assert V("0.5") not in base["bar"]
        assert base["bar[a]"] == VersionRange.full(admit_arbitrary=False)
        assert base["bar[b]"] == VersionRange.full(admit_arbitrary=False)

    def test_extra_gated_multi_extra_splits_into_one_proxy_each(self) -> None:
        """``bar[a,b]`` under extra ``x`` records both proxies in that bucket."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {"x": {}}
        add_classified_dep(Requirement("bar[a,b]>=1.0"), {"x"}, base, extra_map)
        assert V("2.0") in extra_map["x"]["bar"]
        assert extra_map["x"]["bar[a]"] == VersionRange.full(admit_arbitrary=False)
        assert extra_map["x"]["bar[b]"] == VersionRange.full(admit_arbitrary=False)
        assert "bar" not in base

    def test_multi_extra_proxy_names_normalized(self) -> None:
        """Proxy keys for the dep's extras are PEP 685 normalized."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {}
        add_classified_dep(Requirement("bar[A_x,B.y]"), set(), base, extra_map)
        assert "bar[a-x]" in base
        assert "bar[b-y]" in base


class TestLocalSources:
    def _write_local(
        self,
        tmp_path: Path,
        body: str,
    ) -> Path:
        (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
        return tmp_path

    def test_local_source_short_circuits_listing(self, tmp_path: Path) -> None:
        """A registered local source replaces the PyPI listing."""

        self._write_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.2.3"\n',
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        versions = provider.fetch_versions("foo")
        assert len(versions) == 1
        version, dist = versions[0]
        assert str(version) == "1.2.3"
        assert dist.url.startswith("file://")

    def test_local_source_root_requirement_skips_listing_request(
        self, tmp_path: Path
    ) -> None:
        """Constructor must not request a PyPI listing for a workspace member."""

        self._write_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.2.3"\n',
        )
        coordinator = make_coordinator([], package="foo")
        coordinator.request_listing.reset_mock()
        Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            root_requirements={"foo": SpecifierSet("").to_range()},
            build_policy=BuildPolicy.NEVER,
        )
        coordinator.request_listing.assert_not_called()

    def test_prefetch_new_deps_skips_local_source(self, tmp_path: Path) -> None:
        """A transitive-dep prefetch must not hit PyPI for a local source."""

        self._write_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.2.3"\n',
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        coordinator.request_listing.reset_mock()
        provider.prefetch_new_deps({"foo": SpecifierSet("").to_range()})
        coordinator.request_listing.assert_not_called()

    def test_local_source_reads_from_subdirectory(self, tmp_path: Path) -> None:
        """A local source with a subdirectory resolves the package there."""
        sub = tmp_path / "packages" / "foo"
        sub.mkdir(parents=True)
        (sub / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "4.5.6"\n', encoding="utf-8"
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[
                LocalSource("foo", str(tmp_path), subdirectory="packages/foo")
            ],
            build_policy=BuildPolicy.NEVER,
        )
        versions = provider.fetch_versions("foo")
        assert len(versions) == 1
        assert str(versions[0][0]) == "4.5.6"

    def test_local_source_dependencies(self, tmp_path: Path) -> None:
        """Local source deps round-trip through ``get_dependencies``."""

        self._write_local(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dependencies = ["requests>=2.0", "click<9"]
            """,
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "requests" in deps
        assert "click" in deps

    def test_dynamic_deps_under_never_raises(self, tmp_path: Path) -> None:
        """Dynamic LocalSource deps under NEVER raise UnsupportedSdistError.

        Static-only reads are fine; once ``dynamic = ["dependencies"]``
        appears, NEVER cannot satisfy the request and the version is
        skipped (or, on a forced fetch, surfaces as an error).
        """

        self._write_local(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["dependencies"]
            """,
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        with pytest.raises(UnsupportedSdistError, match="BUILD_LOCAL"):
            provider.fetch_versions("foo")

    def test_non_utf8_pyproject_raises_unsupported(self, tmp_path: Path) -> None:
        """A local source with a non-UTF-8 ``pyproject.toml`` is unbuildable.

        Neither the static read nor the backend can read the file, so it is
        reported as an unsupported source, not as a decode traceback.
        """
        (tmp_path / "pyproject.toml").write_bytes(
            b'[project]\nname = "foo"\nversion = "1.0"\ndescription = "\xe9"\n'
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.BUILD_LOCAL,
            build_config=NabProjectConfig(),
        )
        with pytest.raises(UnsupportedSdistError, match="could not read pyproject"):
            provider.fetch_versions("foo")

    def test_local_source_under_never_reads_statically(self, tmp_path: Path) -> None:
        """NEVER admits ``LocalSource`` declarations and reads them statically.

        The pre-three-level lockdown that refused any declaration up-front
        is gone; only dynamic metadata requires a backend.
        """
        self._write_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.2.3"\n',
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        versions = provider.fetch_versions("foo")
        assert len(versions) == 1
        assert str(versions[0][0]) == "1.2.3"

    def test_duplicate_local_source_raises(self, tmp_path: Path) -> None:
        """Two LocalSource entries for the same name are rejected."""

        coordinator = make_coordinator([], package="foo")
        with pytest.raises(ValueError, match="duplicate local source"):
            Provider(
                coordinator,
                local_sources=[
                    LocalSource("foo", str(tmp_path)),
                    LocalSource("Foo", str(tmp_path / "alt")),
                ],
                build_policy=BuildPolicy.NEVER,
            )

    def test_local_source_canonicalises_name(self, tmp_path: Path) -> None:
        """LocalSource uses canonical lookup so Foo_Bar matches foo-bar."""

        self._write_local(
            tmp_path,
            '[project]\nname = "foo-bar"\nversion = "1.0"\n',
        )
        coordinator = make_coordinator([], package="foo-bar")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("Foo_Bar", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        versions = provider.fetch_versions("Foo.Bar")
        assert len(versions) == 1

    def test_build_local_source_failure_propagates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A backend failure under BUILD_LOCAL surfaces as UnsupportedSdistError."""
        from nab_python import (
            build_backend,
        )

        self._write_local(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["dependencies"]
            """,
        )

        def boom(_path: Path, **_kwargs: object) -> None:
            raise build_backend.BuildBackendError("backend exploded")

        monkeypatch.setattr("nab_python.build_backend.extract_metadata", boom)
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.BUILD_LOCAL,
        )
        with pytest.raises(UnsupportedSdistError, match="backend exploded"):
            provider.fetch_versions("foo")

    def test_local_source_build_is_not_retargeted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``--python`` retarget does not reach the build env.

        The backend runs in a venv made from the host interpreter, so
        a target Python would pick wheels for the wrong ABI and drop
        build requirements the host needs.
        """
        self._write_local(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["dependencies"]
            """,
        )
        built = WheelMetadata(
            name="foo",
            version=V("1.0"),
            requires_python=None,
            requires_dist=[],
            provides_extra=[],
        )
        captured: dict[str, object] = {}

        def fake_build(_path: Path, **kwargs: object) -> WheelMetadata:
            captured.update(kwargs)
            return built

        monkeypatch.setattr("nab_python.build_backend.extract_metadata", fake_build)
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.BUILD_LOCAL,
        )
        assert len(provider.fetch_versions("foo")) == 1
        assert captured == {"config": provider.build_config}

    def test_priority_does_not_shadow_local_source_with_pypi_listing(
        self,
        tmp_path: Path,
    ) -> None:
        """Regression: ``compute_matching`` populated ``versions_cache``
        from the coordinator's PyPI listing without checking
        ``local_sources`` first.  When ``foo`` was both registered as
        a local source AND present in the coordinator's index (because
        the resolver had pre-fetched it as a root requirement), the
        cache held PyPI versions and ``fetch_versions`` returned PyPI
        instead of the local source.

        The visible failure was an ``--extras all`` lock on a workspace
        member: the resolver reported "no versions of <member> in
        [3.3.0, 3.4.0.dev0)" because the pre-cached PyPI listing did
        not have that range, even though the local source did.
        """
        from nab_python._vendor.packaging.specifiers import SpecifierSet

        self._write_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "3.3.0"\n',
        )
        # Coordinator carries a stale PyPI listing missing 3.3.0.
        coordinator = make_coordinator(
            [make_wheel("3.2.1"), make_wheel("3.0.6")],
            package="foo",
        )
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        # Trigger the priority path the way the resolver does on
        # any package with both root pressure and a listing in
        # flight.  Before the fix this populates versions_cache
        # with PyPI versions.
        provider.prioritize("foo", SpecifierSet(">=3.3.0").to_range(), {})
        # The PyPI versions must NOT have leaked into the cache.
        cached = provider.versions_cache.get("foo")
        if cached is not None:
            assert [str(v) for v, _ in cached] == ["3.3.0"], (
                f"local source shadowed by PyPI listing: {[str(v) for v, _ in cached]}"
            )
        # Either way, fetch_versions must return the local source.
        versions = provider.fetch_versions("foo")
        assert [str(v) for v, _ in versions] == ["3.3.0"]


class TestMarkerEnvironment:
    def test_overlay_overrides_host(self) -> None:
        """Keys in the target's marker overrides overlay the host environment."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host().with_marker_overrides(
                {
                    "platform_system": "Windows",
                    "sys_platform": "win32",
                    "platform_machine": "AMD64",
                }
            ),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["platform_system"] == "Windows"
        assert provider.environment["sys_platform"] == "win32"
        assert provider.environment["platform_machine"] == "AMD64"

    def test_overlay_does_not_replace_unspecified_keys(self) -> None:
        """Keys not in the overlay keep the host value (``os_name`` etc)."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host().with_marker_overrides(
                {"platform_system": "Darwin"}
            ),
            build_policy=BuildPolicy.NEVER,
        )
        assert "os_name" in provider.environment
        assert provider.environment["platform_system"] == "Darwin"

    def test_marker_evaluates_against_overlay(self) -> None:
        """A ``sys_platform == 'win32'`` dep activates under impersonation."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                'Requires-Dist: pywin32 ; sys_platform == "win32"\n'
                'Requires-Dist: only-linux ; sys_platform == "linux"\n'
            ),
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.12").with_marker_overrides(
                {"sys_platform": "win32"}
            ),
            build_policy=BuildPolicy.NEVER,
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "pywin32" in deps
        assert "only-linux" not in deps

    def test_python_version_overlay_runs_after_python_version_arg(self) -> None:
        """The marker overrides apply after the target Python."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.11.5").with_marker_overrides(
                {"python_version": "3.13"}
            ),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["python_version"] == "3.13"

    def test_two_part_python_version_expands_full_version(self) -> None:
        """A 2-part python_version yields a 3-part python_full_version."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.10"),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["python_version"] == "3.10"
        assert provider.environment["python_full_version"] == "3.10.0"

    def test_full_python_version_preserved(self) -> None:
        """A 3-part python_version keeps its patch component."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.11.5"),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["python_full_version"] == "3.11.5"


class TestTargetEnvironment:
    def test_target_python_moves_implementation_version(self) -> None:
        """A target Python moves implementation_version too."""
        provider = Provider(
            make_coordinator(), target=ResolveTarget.for_host_python("3.9.0")
        )
        assert provider.environment["implementation_version"] == "3.9.0"
        assert (
            provider.environment["implementation_version"]
            == provider.environment["python_full_version"]
        )

    def test_marker_env_patch_python_version_normalizes(self) -> None:
        """A marker-override python_version like 3.10.5 normalizes to major.minor."""
        provider = Provider(
            make_coordinator(),
            target=ResolveTarget.for_host_python("3.12.3").with_marker_overrides(
                {"python_version": "3.10.5"}
            ),
            build_policy=BuildPolicy.NEVER,
        )
        assert provider.environment["python_version"] == "3.10"
        assert provider.environment["python_full_version"] == "3.10.5"


class TestLookAhead:
    def test_skips_version_conflicting_with_root(self) -> None:
        """choose_version skips candidates whose deps conflict with root reqs."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        meta_v2 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar>=5.0\n"
        )
        meta_v1 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=1.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": meta_v2, "1.0": meta_v1},
            package="foo",
        )

        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_no_root_requirements_skips_nothing(self) -> None:
        """Without root_requirements, look-ahead is inactive."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_unrelated_deps_pass_look_ahead(self) -> None:
        """Deps on packages not in root requirements pass look-ahead."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: unknown-pkg>=5.0\n"
            ),
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_all_versions_filtered_returns_none(self) -> None:
        """Returns None when every candidate conflicts with root reqs."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: bar>=5.0\n"
            ),
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) is None

    def test_cached_deps_used_for_look_ahead(self) -> None:
        """Look-ahead uses cached deps without re-fetching."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: bar>=5.0\n"
            ),
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.get_dependencies("foo", V("1.0"))
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) is None

    def test_batch_with_no_metadata_url(self) -> None:
        """Versions without metadata URLs are skipped by look-ahead.

        Look-ahead catches :class:`MetadataError` so the resolver can
        record "no versions" and backjump rather than fail mid-resolve.
        """
        wheels = [
            make_wheel("2.0"),
            make_wheel("1.0", has_metadata=False),
        ]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={
                "2.0": "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar>=5.0\n",
            },
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        # v2.0 conflicts with root, v1.0 has no metadata_url and is
        # skipped; choose_version returns ``None`` instead of raising.
        assert provider.choose_version("foo", spec.to_range()) is None

    def test_batch_uses_cached_deps(self) -> None:
        """Batch prefetch skips already-cached versions."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar>=5.0\n"
            ),
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.deps_cache[("foo", V("1.0"))] = {}
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_batch_skips_version_cached_during_fetch(self) -> None:
        """If a version gets cached between submit and result, skip it."""
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]

        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}

        index = InMemoryIndex()
        index.store_listing("foo", wheels)

        coordinator = MagicMock()
        coordinator.index = index
        coordinator.request_listing.side_effect = lambda pkg: _done_event()

        call_count = [0]

        def _request_metadata(
            pkg: str, ver: str, url: str, metadata_hash: tuple[str, str] | None = None
        ) -> threading.Event:
            call_count[0] += 1
            text = (
                f"Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n"
                "Requires-Dist: bar>=5.0\n"
            )
            if ver == "1.0":
                text = "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            index.store_metadata(pkg, ver, text)
            return _done_event()

        coordinator.request_metadata.side_effect = _request_metadata

        def _request_metadata_batch(
            items: list[tuple[str, str, str, tuple[str, str] | None]],
        ) -> list[tuple[str, str, threading.Event]]:
            results: list[tuple[str, str, threading.Event]] = []
            for pkg, ver, url, _hash in items:
                call_count[0] += 1
                # On second batch call, inject cache for v1.0
                if call_count[0] == 2:
                    provider.deps_cache[("foo", V("1.0"))] = {}
                text = (
                    f"Metadata-Version: 2.1\nName: foo\nVersion: {ver}\n"
                    "Requires-Dist: bar>=5.0\n"
                )
                if ver == "1.0":
                    text = "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                index.store_metadata(pkg, ver, text)
                results.append((pkg, ver, _done_event()))
            return results

        coordinator.request_metadata_batch.side_effect = _request_metadata_batch

        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")

    def test_batch_skips_multiple_bad_versions(self) -> None:
        """Batch processes multiple chunks to find a compatible version."""
        wheels = [make_wheel(f"{v}.0") for v in range(15, 0, -1)]
        metadata_by_version = {}
        for v in range(15, 0, -1):
            ver = f"{v}.0"
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
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")


class TestDecisionLookAhead:
    """First-candidate decision check with grouped binary clauses."""

    def test_hint_stores_decisions_subset(self) -> None:
        """receive_partial_solution_hint records the decisions snapshot."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        ranges = {"bar": SpecifierSet(">=1.0").to_range()}
        decisions = {"bar": V("1.0")}
        provider.receive_partial_solution_hint(ranges, decisions)
        assert provider.solution_decisions == decisions
        assert provider.solution_ranges == ranges

    def test_hint_stores_snapshots_without_copying(self) -> None:
        """The caller hands over owned snapshots, so they are stored as-is."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        ranges = {"bar": SpecifierSet(">=1.0").to_range()}
        decisions = {"bar": V("1.0")}
        provider.receive_partial_solution_hint(ranges, decisions)
        assert provider.solution_ranges is ranges
        assert provider.solution_decisions is decisions

    def test_first_candidate_blocked_by_decision_records_clause(self) -> None:
        """When the newest candidate's deps disagree with a decision, a binary
        clause is queued for the resolver to absorb via consume_pending_clauses."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        meta_v2 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar>=5.0\n"
        )
        meta_v1 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=1.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": meta_v2, "1.0": meta_v1},
            package="foo",
        )
        # Root requirements are unused for the rejection itself, but they
        # have to be non-empty so that look-ahead is enabled.
        root_reqs = {"baz": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        spec = SpecifierSet("")
        # foo==2.0 conflicts with bar==3.0 (needs >=5.0); foo==1.0 is compatible.
        assert provider.choose_version("foo", spec.to_range()) == V("1.0")
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        # Each rejected (candidate_pkg, blocker_pkg, blocker_version) group
        # produces one clause: positive(candidate range), positive(blocker==w).
        terms = clauses[0].terms
        assert len(terms) == 2
        packages = {t.package for t in terms}
        assert packages == {"foo", "bar"}

    def test_consume_pending_clauses_drains(self) -> None:
        """consume_pending_clauses returns and clears queued clauses."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        # Manually populate the queue, then drain.
        clause: Incompatibility[str, Version] = Incompatibility(
            [
                Term("foo", VersionRange.singleton(V("1.0")), positive=True),
                Term("bar", VersionRange.singleton(V("2.0")), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        provider.pending_clauses.append(clause)
        drained = provider.consume_pending_clauses()
        assert drained == [clause]
        assert provider.consume_pending_clauses() == []

    def test_decision_check_disabled_skips_clause(self) -> None:
        """check_decisions=False suppresses the decision-driven rejection path."""
        wheels = [make_wheel("1.0")]
        meta = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=5.0\n"
        )
        coordinator = make_coordinator(wheels, metadata_text=meta, package="foo")
        root_reqs = {"baz": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        # check_decisions=False ignores the bar==3.0 conflict.
        assert provider._look_ahead_ok("foo", V("1.0"), check_decisions=False)
        assert provider.consume_pending_clauses() == []

    def test_all_first_candidates_blocked_returns_none(self) -> None:
        """When the only candidate is blocked by a decision, returns None
        and queues the binary clause for the resolver."""
        wheels = [make_wheel("1.0")]
        meta = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=5.0\n"
        )
        coordinator = make_coordinator(wheels, metadata_text=meta, package="foo")
        root_reqs = {"baz": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        spec = SpecifierSet("")
        assert provider.choose_version("foo", spec.to_range()) is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1

    def test_undecided_positive_range_records_range_block(self) -> None:
        """When the dep has a positive range but no decision, look-ahead
        records a sound range-block clause and rejects the candidate.
        """
        wheels = [make_wheel("1.0")]
        meta = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=5.0\n"
        )
        coordinator = make_coordinator(wheels, metadata_text=meta, package="foo")
        root_reqs = {"baz": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        # bar has a positive range that doesn't intersect bar>=5.0 (the dep)
        # but is not decided yet.
        provider.receive_partial_solution_hint(
            {"bar": SpecifierSet("<2.0").to_range()}, {}
        )
        assert provider._look_ahead_ok("foo", V("1.0")) is False
        # _flush_pending_blocks turns the range block into a clause.
        provider._flush_pending_blocks()
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        terms = clauses[0].terms
        assert {t.package for t in terms} == {"foo", "bar"}

    def test_full_resolve_reports_lookahead_clause_as_incompatible(self) -> None:
        """The look-ahead grouped clause renders as an incompatibility.

        Root needs foo and app; foo 1.0 requires lib==9.0 while app 3.0
        requires lib==5.0.  The resolve fails, and the grouped clause that
        rejects app against the decided lib==9.0 carries the blocker term
        positive.  It must render as app being incompatible with lib 9.0, not
        as app depending on it; app's real dependency is lib 5.0.
        """

        def named_wheel(pkg: str, version: str) -> WheelFile:
            return WheelFile(
                filename=f"{pkg}-{version}-py3-none-any.whl",
                url=f"https://example.com/{pkg}-{version}.whl",
                version=version,
                requires_python=None,
                has_metadata=True,
                upload_time=None,
                local_path=None,
            )

        def meta(name: str, version: str, *reqs: str) -> str:
            lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
            lines += [f"Requires-Dist: {r}" for r in reqs]
            return "\n".join(lines) + "\n"

        coordinator = make_coordinator(
            listings={
                "foo": [named_wheel("foo", "1.0")],
                "app": [named_wheel("app", "3.0")],
                "lib": [named_wheel("lib", "5.0"), named_wheel("lib", "9.0")],
            },
            metadata_by_version={
                "1.0": meta("foo", "1.0", "lib==9.0"),
                "3.0": meta("app", "3.0", "lib==5.0"),
                "5.0": meta("lib", "5.0"),
                "9.0": meta("lib", "9.0"),
            },
        )
        root_reqs = {
            "foo": VersionRange.full(admit_arbitrary=False),
            "app": VersionRange.full(admit_arbitrary=False),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(dict(root_reqs))

        lines = str(exc_info.value).splitlines()
        assert not [
            ln for ln in lines if "app" in ln and "lib" in ln and "depends on" in ln
        ]
        assert any(
            "app" in ln and "lib" in ln and "incompatible with" in ln for ln in lines
        )


class TestLookAheadAbort:
    """Look-ahead abort path: when the scan rejects ``_LOOKAHEAD_ABORT_THRESHOLD``
    candidates all blamed on the same ``(blocker_pkg, blocker_version)``, the
    scan returns the first candidate without emitting any clauses so the
    resolver can decide it tentatively and learn the real dep-range clause
    via ``get_dependencies``.
    """

    def _provider(self, root_reqs: dict[str, VersionRange] | None = None) -> Provider:
        coordinator = make_coordinator([], package="foo")
        return Provider(coordinator, root_requirements=root_reqs)

    def test_should_abort_returns_blocker_for_single_decision_blocker(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].extend(
            [V("0.9"), V("0.8"), V("0.7")]
        )
        assert provider._should_abort_lookahead("foo") == ("bar", V("1.0"))

    def test_should_abort_none_when_no_blocks(self) -> None:
        provider = self._provider()
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_two_distinct_blockers(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_blocks[("foo", "baz", V("2.0"))].append(V("0.8"))
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_two_blocker_versions(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_blocks[("foo", "bar", V("2.0"))].append(V("0.8"))
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_range_block_present(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_range_blocks[
            ("foo", "baz", SpecifierSet("<2.0").to_range())
        ].append(V("0.9"))
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_root_block_present(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_root_blocks[
            (
                "foo",
                "qux",
                SpecifierSet("==2.0").to_range(),
                SpecifierSet("==1.0").to_range(),
            )
        ].append(V("0.9"))
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_none_when_metadata_block_present(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_metadata_blocks["foo"].append((V("0.8"), "metadata failure"))
        assert provider._should_abort_lookahead("foo") is None

    def test_should_abort_ignores_blocks_owned_by_other_packages(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        # "other" has range/root/metadata blocks but they belong to a
        # different candidate and must not influence foo's decision.
        provider.pending_blocks[("other", "baz", V("2.0"))].append(V("0.8"))
        provider.pending_range_blocks[
            ("other", "qux", SpecifierSet("<2.0").to_range())
        ].append(V("0.8"))
        provider.pending_metadata_blocks["other"].append((V("0.5"), "x"))
        assert provider._should_abort_lookahead("foo") == ("bar", V("1.0"))

    def test_discard_drops_only_target_candidate_blocks(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_blocks[("other", "baz", V("2.0"))].append(V("0.8"))
        provider._discard_pending_decision_blocks("foo")
        assert ("foo", "bar", V("1.0")) not in provider.pending_blocks
        assert provider.pending_blocks[("other", "baz", V("2.0"))] == [V("0.8")]

    def test_abort_returns_first_candidate_and_emits_no_clauses(self) -> None:
        """End-to-end: a scan that hits the abort threshold returns the
        first candidate, drops its singleton-blocker pending blocks, and
        emits *no* clauses.  The resolver then decides the candidate
        and ``get_dependencies`` produces the actual dep-range clause.
        """
        # Threshold 2 keeps the fixture small; foo has 3 versions all
        # rejected because bar=3.0 conflicts with foo's bar<2.0 dep.
        versions = ["3.0", "2.0", "1.0"]
        wheels = [make_wheel(v) for v in versions]
        meta_template = (
            "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar<2.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = 2  # type: ignore[misc]
        chosen = provider.choose_version("foo", VersionRange.full())
        assert chosen == V("3.0")
        # No clauses: the abort path discarded the singleton-blocker
        # pending blocks rather than flushing them into the resolver.
        assert provider.consume_pending_clauses() == []

    def test_no_abort_under_threshold(self) -> None:
        """Below the threshold, look-ahead emits the usual singleton-blocker
        clauses and returns ``None`` if no candidate passes.
        """
        versions = ["1.0", "0.9"]
        wheels = [make_wheel(v) for v in versions]
        meta_template = (
            "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar<0.5\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = 64  # type: ignore[misc]
        # Only 2 candidates so we never cross 64; the scan returns None.
        assert provider.choose_version("foo", VersionRange.full()) is None
        # The 2 rejections still emit a clause via the normal flush path.
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1

    def test_abort_records_state_for_per_package_skip(self) -> None:
        """When the abort fires, ``_lookahead_aborted`` records the blocker so
        the next ``choose_version`` for this package can short-circuit
        look-ahead while the blocker decision is unchanged.
        """
        versions = ["3.0", "2.0", "1.0"]
        wheels = [make_wheel(v) for v in versions]
        meta_template = (
            "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar<2.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = 2  # type: ignore[misc]
        assert provider.choose_version("foo", VersionRange.full()) == V("3.0")
        assert provider._lookahead_aborted == {"foo": ("bar", V("3.0"))}

    def test_abort_queues_force_backtrack_target(self) -> None:
        """The abort path queues the blocker package on
        ``_force_backtrack_targets`` so the resolver can pick it up via
        ``consume_force_backtrack_targets`` and target-back-jump
        immediately instead of paying a natural-path conflict cycle.
        """
        versions = ["3.0", "2.0", "1.0"]
        wheels = [make_wheel(v) for v in versions]
        meta_template = (
            "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar<2.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = 2  # type: ignore[misc]
        provider.choose_version("foo", VersionRange.full())
        assert provider.consume_force_backtrack_targets() == ["bar"]
        # Drained on consume.
        assert provider.consume_force_backtrack_targets() == []

    def test_force_backtrack_refires_up_to_cap(self) -> None:
        """A blocker can drive at most ``_MAX_FORCE_BACKTRACKS_PER_PKG``
        force-backtracks, mirroring uv's repeated ConflictTracker fires.
        After the cap the abort path still records the skip but does not
        re-queue the force-backtrack target.
        """
        versions = ["3.0", "2.0", "1.0"]
        wheels = [make_wheel(v) for v in versions]
        meta_template = (
            "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar<2.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={v: meta_template.format(ver=v) for v in versions},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        provider._LOOKAHEAD_ABORT_THRESHOLD = 2  # type: ignore[misc]
        provider._MAX_FORCE_BACKTRACKS_PER_PKG = 3  # type: ignore[misc]
        for _ in range(3):
            provider.choose_version("foo", VersionRange.full())
            assert provider.consume_force_backtrack_targets() == ["bar"]
            provider._lookahead_aborted.pop("foo", None)
        # Fourth abort blaming the same blocker is past the cap; no re-queue.
        provider.choose_version("foo", VersionRange.full())
        assert provider.consume_force_backtrack_targets() == []

    def test_per_package_skip_short_circuits_while_blocker_decided(self) -> None:
        """A recorded abort makes ``choose_version`` skip the full scan
        when the blocker decision is unchanged.  If the first candidate's
        metadata is already cached (warm path), it is returned without
        running look-ahead at all.  If not cached (cold path), a non
        -decision look-ahead gate runs to guard against unreadable
        wheels.
        """
        meta = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar<2.0\n"
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=meta, package="foo"
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        # bar=3.0 conflicts with foo's bar<2.0 dep, the same
        # state that would have triggered the abort in the first place.
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        # Pre-record the abort state as if a prior scan had populated it.
        provider._lookahead_aborted["foo"] = ("bar", V("3.0"))
        # Pre-warm the deps_cache so the skip path takes the cache fast
        # path (no look-ahead invocation).
        provider.get_dependencies("foo", V("1.0"))
        before_rejections = provider.stats.look_ahead_rejections
        chosen = provider.choose_version("foo", VersionRange.full())
        assert chosen == V("1.0")
        # Cache hit, no extra look-ahead rejections recorded.
        assert provider.stats.look_ahead_rejections == before_rejections

    def test_per_package_skip_cold_runs_safety_check(self) -> None:
        """Cold cache: the skip path runs ``_look_ahead_ok`` with
        ``check_decisions=False`` to catch unreadable wheels before the
        resolver decides them.
        """
        meta = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar<2.0\n"
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=meta, package="foo"
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        provider._lookahead_aborted["foo"] = ("bar", V("3.0"))
        # No pre-warm; cold cache. The safety look-ahead fetches the
        # metadata and the skip path returns the candidate.
        chosen = provider.choose_version("foo", VersionRange.full())
        assert chosen == V("1.0")

    def test_per_package_skip_falls_through_on_metadata_error(self) -> None:
        """If the first candidate has unreadable metadata, the safety
        check rejects it and the normal scan path runs.  Prevents the
        resolver from deciding a broken candidate and crashing with
        ``MetadataError``.
        """
        wheels = [make_wheel("1.0"), make_wheel("0.9")]
        meta_v1_broken = "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar(invalid\n"
        meta_v09 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 0.9\nRequires-Dist: bar>=1.0\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"1.0": meta_v1_broken, "0.9": meta_v09},
            package="foo",
        )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        # Skip is recorded, but foo==1.0 has bad metadata so the
        # safety check rejects and the scan proceeds.
        provider._lookahead_aborted["foo"] = ("bar", V("3.0"))
        chosen = provider.choose_version("foo", VersionRange.full())
        # The broken candidate is rejected via the metadata-block path;
        # the scan continues to ``0.9`` which does *not* conflict with
        # bar=3.0 (its dep is bar>=1.0).  Returning ``0.9`` shows the
        # safety check successfully kept the resolver from crashing.
        assert chosen == V("0.9")

    def test_per_package_skip_invalidated_when_blocker_changes(self) -> None:
        """A recorded abort is dropped once the blocker decision changes,
        and the normal look-ahead path runs again.
        """
        wheels = [make_wheel("1.0")]
        meta = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=5.0\n"
        )
        coordinator = make_coordinator(wheels, metadata_text=meta, package="foo")
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        # Recorded state names bar==3.0, but the current decisions have
        # bar==2.0, so the recorded state is stale.
        provider.receive_partial_solution_hint({}, {"bar": V("2.0")})
        provider._lookahead_aborted["foo"] = ("bar", V("3.0"))
        chosen = provider.choose_version("foo", VersionRange.full())
        # Look-ahead ran normally: foo==1.0 rejects because bar==2.0
        # doesn't match its >=5.0 dep.
        assert chosen is None
        # Stale record was dropped.
        assert "foo" not in provider._lookahead_aborted


class TestPrioritize:
    def test_counts_matching_versions(self) -> None:
        """prioritize returns tuple (tier, matching_count, is_base)."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=2.0")
        assert provider.prioritize("foo", spec.to_range(), {}) == (
            Provider.TIER_NORMAL,
            2,
            True,
        )

    def test_conflict_promoted_gets_higher_priority(self) -> None:
        """Packages with 5+ affected conflicts sort before others."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0")
        normal = provider.prioritize("foo", spec.to_range(), {})
        promoted = provider.prioritize("foo", spec.to_range(), {"foo": 5})
        assert promoted < normal

    def test_culprit_counts_demote(self) -> None:
        """Persistent culprits are demoted to a tier below normal.

        Mirrors uv's "Package X has too many conflicts (culprit),
        deprioritizing" heuristic so the culprit decides AFTER its
        dependents narrow it.  Affected packages are still promoted
        above normal and culprits are below normal.
        """
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0")
        normal = provider.prioritize("foo", spec.to_range(), {}, {})
        culprit = provider.prioritize("foo", spec.to_range(), {}, {"foo": 10})
        affected = provider.prioritize("foo", spec.to_range(), {"foo": 5}, {})
        assert affected < normal
        assert normal < culprit

    def test_culprit_below_threshold_not_demoted(self) -> None:
        """A culprit below the threshold stays in the normal tier."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0")
        normal = provider.prioritize("foo", spec.to_range(), {}, {})
        below = provider.prioritize("foo", spec.to_range(), {}, {"foo": 2})
        assert below == normal

    def test_tied_culprits_not_demoted(self) -> None:
        """When the top-two culprits are near-tied (gap < threshold), neither demotes."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0")
        normal = provider.prioritize("foo", spec.to_range(), {}, {})
        # foo's 6 vs bar's 8 vs baz's 5: bar dominates over foo, foo
        # itself stays in normal because foo - max(bar, baz) = -2 < 5.
        not_dominant = provider.prioritize(
            "foo", spec.to_range(), {}, {"foo": 6, "bar": 8, "baz": 5}
        )
        assert not_dominant == normal


class TestUploadedPriorTo:
    def test_filters_newer_wheels(self) -> None:
        """Wheels uploaded after the cutoff are excluded."""
        wheels = [
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("1.0")]

    def test_keeps_older_wheels(self) -> None:
        """Wheels uploaded before the cutoff are kept."""
        wheels = [
            make_wheel("2.0", upload_time="2024-01-15T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("2.0"), V("1.0")]

    def test_excludes_wheels_without_upload_time(self) -> None:
        """Wheels missing upload-time are excluded when cutoff is set."""
        wheels = [
            make_wheel("2.0", upload_time=None),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("1.0")]

    def test_keeps_local_artifact_without_upload_time(self) -> None:
        """A local file:// artifact carries no upload time and is kept."""
        wheels = [
            make_wheel(
                "1.0",
                upload_time=None,
                local_path=Path("/wheelhouse/pkg-1.0-py3-none-any.whl"),
            ),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("1.0")]

    def test_excludes_remote_but_keeps_local_without_upload_time(self) -> None:
        """The cutoff drops a remote no-upload-time wheel but keeps a local one."""
        wheels = [
            make_wheel("2.0", upload_time=None),
            make_wheel(
                "1.0",
                upload_time=None,
                local_path=Path("/wheelhouse/pkg-1.0-py3-none-any.whl"),
            ),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("1.0")]

    def test_no_cutoff_ignores_upload_time(self) -> None:
        """Without uploaded_prior_to, upload_time is irrelevant."""
        wheels = [
            make_wheel("2.0", upload_time=None),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("2.0"), V("1.0")]

    def test_exact_cutoff_excludes(self) -> None:
        """A wheel uploaded at exactly the cutoff time is excluded."""
        wheels = [
            make_wheel("1.0", upload_time="2024-03-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        assert provider.fetch_versions("foo") == []

    @pytest.mark.parametrize(
        "fraction", ["", ".1", ".12", ".123", ".1234", ".12345", ".123456"]
    )
    def test_fractional_seconds(self, fraction: str) -> None:
        """PEP 700 permits 0 through 6 fractional digits, and all of them parse."""
        wheels = [
            make_wheel("1.0", upload_time=f"2024-01-15T12:30:45{fraction}Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("1.0")]
        assert provider.stats.excluded_by_time == 0

    def test_invalid_upload_time_excluded(self) -> None:
        """Wheels with unparseable upload-time are excluded."""
        wheels = [
            make_wheel("1.0", upload_time="not-a-date"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        assert provider.fetch_versions("foo") == []

    def test_malformed_fraction_excluded_not_fatal(self) -> None:
        """An empty fraction is malformed, so the wheel is skipped, not fatal."""
        wheels = [make_wheel("1.0", upload_time="2024-01-15T12:30:45.")]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        assert provider.fetch_versions("foo") == []
        assert provider.stats.excluded_by_time == 1

    def test_naive_upload_time_raises_when_cutoff_active(self) -> None:
        """A timezone-naive upload-time violates PEP 700, so it is a hard error."""
        wheels = [
            make_wheel("1.0", upload_time="2024-01-01T00:00:00"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        with pytest.raises(InvalidUploadTimeError, match="2024-01-01T00:00:00"):
            provider.fetch_versions("foo")

    def test_naive_upload_time_not_metadata_error(self) -> None:
        """The error is not a MetadataError, so look-ahead cannot swallow it."""
        assert not issubclass(InvalidUploadTimeError, MetadataError)

    def test_naive_upload_time_ignored_without_cutoff(self) -> None:
        """With no cutoff active the naive upload-time is never inspected."""
        wheels = [
            make_wheel("1.0", upload_time="2024-01-01T00:00:00"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("1.0")]


class TestUploadedPriorToOverrides:
    """Per-package / per-index overrides bypass or replace the global cutoff."""

    def test_override_disables_global_cutoff(self) -> None:
        """An override with the ``false`` form keeps every wheel even when
        the global cutoff would exclude them."""
        wheels = [
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        global_cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(
            coordinator,
            uploaded_prior_to=global_cutoff,
            package_overrides=(pkg_override("foo", uploaded_prior_to_disabled=True),),
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("2.0"), V("1.0")]

    def test_override_applies_per_package_cutoff(self) -> None:
        """A per-package datetime override replaces the global cutoff
        for that package only."""
        wheels = [
            make_wheel("3.0", upload_time="2024-08-01T00:00:00Z"),
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        global_cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        package_cutoff = datetime(2024, 7, 1, tzinfo=timezone.utc)
        provider = Provider(
            coordinator,
            uploaded_prior_to=global_cutoff,
            package_overrides=(pkg_override("foo", uploaded_prior_to=package_cutoff),),
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        # 3.0 (Aug) excluded by package cutoff (Jul); 2.0 (Jun) kept
        # because the package override allows up to Jul; 1.0 (Jan) kept.
        assert versions == [V("2.0"), V("1.0")]

    def test_version_scoped_cutoff(self) -> None:
        """A version-scoped override applies only inside its range."""
        wheels = [
            make_wheel("3.0", upload_time="2024-08-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-08-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        global_cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(
            coordinator,
            uploaded_prior_to=global_cutoff,
            package_overrides=(
                pkg_override("foo <= 2", uploaded_prior_to_disabled=True),
            ),
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        # 1.0 is inside the override range (cutoff disabled) so it stays;
        # 3.0 is outside it and the global cutoff (Mar) drops its Aug upload.
        assert versions == [V("1.0")]

    def test_override_only_applies_to_named_package(self) -> None:
        """Other packages keep using the global cutoff."""
        wheels = [
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="bar")
        global_cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(
            coordinator,
            uploaded_prior_to=global_cutoff,
            package_overrides=(pkg_override("foo", uploaded_prior_to_disabled=True),),
        )
        versions = [v for v, _ in provider.fetch_versions("bar")]
        # ``bar`` has no override, so the global cutoff still applies.
        assert versions == [V("1.0")]

    def test_per_index_cutoff_takes_effect(self) -> None:
        """A per-index override changes behaviour vs the global default."""
        wheels = [
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        coordinator.index.store_listing_index("foo", "internal")
        package_cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(
            coordinator,
            index_overrides={
                "internal": IndexOverride(uploaded_prior_to=package_cutoff)
            },
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        # The per-index cutoff (Mar) drops 2.0 (Jun) even though the global
        # cutoff is unset.
        assert versions == [V("1.0")]

    def test_per_index_applies_to_every_package(self) -> None:
        """A per-index override governs every package served from it."""
        package_cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        index = IndexOverride(uploaded_prior_to=package_cutoff)
        for pkg in ("foo", "bar"):
            wheels = [
                make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
                make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
            ]
            coordinator = make_coordinator(wheels, package=pkg)
            coordinator.index.store_listing_index(pkg, "internal")
            provider = Provider(coordinator, index_overrides={"internal": index})
            versions = [v for v, _ in provider.fetch_versions(pkg)]
            assert versions == [V("1.0")]

    def test_override_with_no_global_cutoff(self) -> None:
        """A per-package cutoff with no global cutoff still filters."""
        wheels = [
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        package_cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(
            coordinator,
            uploaded_prior_to=None,
            package_overrides=(pkg_override("foo", uploaded_prior_to=package_cutoff),),
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [V("1.0")]


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


class TestSkipFetch:
    """A complete ``dependencies`` override skips the metadata fetch/build."""

    def test_fires_when_dependencies_present(self) -> None:
        # No metadata is available, so a real fetch would raise; the
        # override resolves the version from declared deps alone.
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(
                pkg_override("foo", dependencies=(Requirement("dep-a>=1"),)),
            ),
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "dep-a" in deps

    def test_fires_with_empty_dependencies(self) -> None:
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(pkg_override("foo", dependencies=()),),
        )
        assert provider.get_dependencies("foo", V("1.0")) == {}

    def test_prefetches_override_dependencies(self) -> None:
        # The override introduces dep-a, so its listing is background-fetched
        # even though foo itself skips the metadata fetch.
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(
                pkg_override("foo", dependencies=(Requirement("dep-a>=1"),)),
            ),
        )
        provider.get_dependencies("foo", V("1.0"))
        coordinator.request_listing.assert_any_call("dep-a")

    def test_does_not_fire_for_requires_python_only(self) -> None:
        # A partial override (only requires-python) still needs the artifact
        # for deps, so with no metadata available it raises.
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(pkg_override("foo", requires_python=">=3.0"),),
        )
        with pytest.raises(MetadataError):
            provider.get_dependencies("foo", V("1.0"))

    def test_sdist_only_under_never_resolves_via_scan(self) -> None:
        # Strict PEP 643 x BuildPolicy.NEVER: dynamic-deps sdists are
        # unresolvable without a build (see the no-override companion in
        # TestNoVersionsReasons).  A complete override rescues them, reached
        # through the look-ahead scan in choose_version.
        coordinator = make_coordinator(
            [make_sdist("1.0"), make_sdist("2.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            package="pkg",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            root_requirements={"pkg": VersionRange.full(admit_arbitrary=False)},
            package_overrides=(
                pkg_override("pkg", dependencies=(Requirement("dep-a>=1"),)),
            ),
        )
        assert provider.choose_version("pkg", VersionRange.full()) == V("2.0")
        assert "dep-a" in provider.deps_cache[("pkg", V("2.0"))]

    def test_corrupt_wheel_resolves_via_scan(self) -> None:
        # A corrupt-metadata wheel would make await_metadata_batch raise in
        # the prefetch path.  A complete override short-circuits submission,
        # so the scan reaches get_dependencies (skip-fetch) and resolves.
        coordinator = make_coordinator(
            [make_wheel("3.0"), make_wheel("2.0")],
            metadata_by_version={
                "3.0": (
                    "Metadata-Version: 2.1\nName: pkg\nVersion: 3.0\n"
                    "Requires-Dist: bar==2.0\n"
                ),
            },
            package="pkg",
        )
        coordinator.index.store_metadata_error(
            "pkg", "2.0", MetadataHashMismatchError("metadata sha256 mismatch")
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"pkg": VersionRange.full(admit_arbitrary=False)},
            package_overrides=(
                pkg_override("pkg == 2.0", dependencies=(Requirement("dep-a>=1"),)),
            ),
        )
        # 3.0's real deps conflict with this decision, forcing the scan past
        # 3.0 into the prefetch batch that would fetch the corrupt 2.0 wheel.
        provider.solution_decisions["bar"] = V("1.0")
        assert provider.choose_version("pkg", VersionRange.full()) == V("2.0")
        assert "dep-a" in provider.deps_cache[("pkg", V("2.0"))]

    def test_prefetch_batch_skips_complete_override(self) -> None:
        # Direct check: prefetch_batch submits nothing for a version with a
        # complete override, even though its wheel advertises PEP 658.
        coordinator = make_coordinator(
            [make_wheel("1.0"), make_wheel("2.0")], package="pkg"
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(
                pkg_override("pkg", dependencies=(Requirement("dep-a"),)),
            ),
        )
        version_list = provider.fetch_versions("pkg")
        wheel_by_version = provider._wheel_by_version("pkg", version_list)
        submitted = provider._prefetch_batch(
            "pkg", [V("2.0"), V("1.0")], wheel_by_version
        )
        assert submitted == []

    def test_prefetch_root_batch_skips_complete_override(self) -> None:
        # The root-range batch prefetch skips a version whose complete
        # override replaces its metadata, but still submits the sibling.
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")], package="pkg"
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"pkg": SpecifierSet(">=1.0").to_range()},
            package_overrides=(
                pkg_override("pkg == 2.0", dependencies=(Requirement("dep-a"),)),
            ),
        )
        provider.fetch_versions("pkg")
        assert _prefetched_batch_versions(coordinator) == ["1.0"]

    def test_prefetch_transitive_best_skips_complete_override(self) -> None:
        # The single-best transitive prefetch submits nothing when the best
        # candidate's metadata is replaced by a complete override.
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")], package="pkg"
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(
                pkg_override("pkg", dependencies=(Requirement("dep-a"),)),
            ),
        )
        provider.fetch_versions("pkg")
        coordinator.request_metadata.assert_not_called()
        coordinator.request_metadata_batch.assert_not_called()

    def test_prefetch_walk_ahead_skips_complete_override(self) -> None:
        # The walk-ahead batch excludes a version whose complete override
        # replaces its metadata, but keeps the un-overridden sibling.
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")], package="pkg"
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(
                pkg_override("pkg == 2.0", dependencies=(Requirement("dep-a"),)),
            ),
        )
        provider.fetch_versions("pkg")
        coordinator.reset_mock()
        provider.prefetch_walk_ahead("pkg")
        assert _prefetched_batch_versions(coordinator) == ["1.0"]


class TestLocalVcsPythonGuardOverride:
    """The local/VCS python guard reads the overridden requires-python."""

    def _local_provider(
        self, tmp_path: Path, pyproject_rp: str, override_rp: str
    ) -> Provider:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "foo"\n'
            'version = "1.0"\n'
            f'requires-python = "{pyproject_rp}"\n'
            'dependencies = ["dep-a>=1"]\n',
            encoding="utf-8",
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python("3.9.0"),
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
            package_overrides=(pkg_override("foo", requires_python=override_rp),),
        )
        provider.get_dependencies("foo", V("1.0"))
        return provider

    def test_widen_admits(self, tmp_path: Path) -> None:
        # pyproject demands >=3.11 (would reject 3.9); the bare-name override
        # widens to >=3.0, so the guard admits the 3.9 target.
        provider = self._local_provider(tmp_path, ">=3.11", ">=3.0")
        stamped = provider.metadata_cache[("foo", V("1.0"))].requires_python
        assert stamped == SpecifierSet(">=3.0")
        _raise_for_source_python(provider, provider.target, {"foo": V("1.0")})

    def test_narrow_rejects(self, tmp_path: Path) -> None:
        # pyproject allows >=3.0; the override narrows to >=3.11, so the
        # guard rejects the 3.9 target.
        provider = self._local_provider(tmp_path, ">=3.0", ">=3.11")
        stamped = provider.metadata_cache[("foo", V("1.0"))].requires_python
        assert stamped == SpecifierSet(">=3.11")
        with pytest.raises(ResolutionError):
            _raise_for_source_python(provider, provider.target, {"foo": V("1.0")})


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


EXTRA_METADATA = (
    "Metadata-Version: 2.1\n"
    "Name: foo\n"
    "Version: 1.0\n"
    "Provides-Extra: security\n"
    "Requires-Dist: bar>=1.0\n"
    'Requires-Dist: cryptography>=2.0; extra == "security"\n'
)

NO_EXTRA_METADATA = (
    "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=1.0\n"
)

WHITESPACE_EXTRA_METADATA = (
    "Metadata-Version: 2.1\n"
    "Name: foo\n"
    "Version: 1.0\n"
    "Provides-Extra: security \n"
    'Requires-Dist: cryptography>=2.0; extra == "security"\n'
)


class TestExtras:
    def test_choose_version_delegates_to_base(self) -> None:
        """choose_version for extras returns the base's best version."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator)
        spec = SpecifierSet("")
        version = provider.choose_version("foo[security]", spec.to_range())
        assert version == V("2.0")

    def test_choose_extra_version_honours_lowest_strategy(self) -> None:
        """``LOWEST`` flips the extras-proxy decision to the smallest version.

        The strategy is keyed off the *base* canonical name; an extras
        proxy never gets a different answer than its underlying package.
        """
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator, resolution_strategy=ResolutionStrategy.LOWEST)
        chosen = provider.choose_version("foo[security]", VersionRange.full())
        assert chosen == V("1.0")

    def test_get_dependencies_returns_extra_deps(self) -> None:
        """get_dependencies for extras returns extra-gated deps plus base dep."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo[security]", V("1.0"))
        assert "cryptography" in deps
        assert "foo" in deps
        assert "bar" not in deps

    def test_get_dependencies_whitespace_provides_extra_keeps_deps(self) -> None:
        """A trailing space on Provides-Extra must not drop the extra's deps."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=WHITESPACE_EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo[security]", V("1.0"))
        assert "cryptography" in deps

    def test_choose_extra_version_filters_by_base_range(self) -> None:
        """An extras proxy whose base==V is excluded by the base's positive
        range must not be returned (the proxy at version V depends on
        base==V).
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator)
        # Base "foo" has a positive range that excludes 2.0.
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("<2.0").to_range()}, {}
        )
        spec = SpecifierSet("")
        # Highest viable version for foo[security] is 1.0 because 2.0 is
        # not in the base's positive range.
        assert provider.choose_version("foo[security]", spec.to_range()) == V("1.0")

    def test_choose_extra_version_records_block_when_base_filter_empties(
        self,
    ) -> None:
        """When the base's decision excludes every proxy candidate in
        ``version_range``, the provider must push a binary clause linking
        the proxy's emptied range to the base's decision.  Without it, the
        resolver only sees a single-term NO_VERSIONS clause and cannot
        learn to revisit the base.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator)
        # Base decided to 1.0; proxy version range excludes 1.0.  Both
        # 2.0 and 1.0 are filtered out (1.0 by version_range, 2.0 by
        # base's positive range), so the proxy has no viable candidate.
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("==1.0").to_range()},
            {"foo": V("1.0")},
        )
        result = provider.choose_version(
            "foo[security]", SpecifierSet(">1.0").to_range()
        )
        assert result is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        terms = clauses[0].terms
        proxy_terms = [t for t in terms if t.package == "foo[security]"]
        base_terms = [t for t in terms if t.package == "foo"]
        assert len(proxy_terms) == 1
        assert len(base_terms) == 1
        # Proxy term names the excluded version 2.0; base term pins 1.0.
        assert proxy_terms[0].is_positive()
        assert V("2.0") in proxy_terms[0].constraint
        assert V("1.0") in base_terms[0].constraint

    def test_choose_extra_version_records_range_block_when_base_undecided(
        self,
    ) -> None:
        """Range-block path: base has a positive range but no decision yet.

        The clause must use the base's range (not a singleton) so it
        stays sound across backjumps that widen the range.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator)
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("<2.0").to_range()},
            {},  # base is constrained but not yet decided
        )
        # version_range demands 2.0+ but base_range forbids it.
        result = provider.choose_version(
            "foo[security]", SpecifierSet(">=2.0").to_range()
        )
        assert result is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        base_terms = [t for t in clauses[0].terms if t.package == "foo"]
        assert len(base_terms) == 1
        # Base term carries the range, not a singleton.
        assert V("1.0") in base_terms[0].constraint
        assert V("2.0") not in base_terms[0].constraint

    def test_choose_extra_version_records_block_in_backtrack_mode(self) -> None:
        """BACKTRACK mode also pushes block clauses when base filter empties.

        The fall-through path in ``choose_extra_version`` (after the
        per-version metadata check loop) must record the same block
        clauses as the non-BACKTRACK path so the resolver can revisit
        the base regardless of the configured extras handling.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator, extras_mode=ExtrasMode.BACKTRACK)
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("==1.0").to_range()},
            {"foo": V("1.0")},
        )
        result = provider.choose_version(
            "foo[security]", SpecifierSet(">1.0").to_range()
        )
        assert result is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        terms = clauses[0].terms
        assert any(t.package == "foo[security]" for t in terms)
        assert any(t.package == "foo" for t in terms)

    def test_base_deps_not_duplicated(self) -> None:
        """Extra deps don't include deps already in the base package."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        base_deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in base_deps
        extra_deps = provider.get_dependencies("foo[security]", V("1.0"))
        assert "bar" not in extra_deps

    def test_extra_deps_cached(self) -> None:
        """Second call returns cached extra deps."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312)
        provider.get_dependencies("foo[security]", V("1.0"))
        provider.get_dependencies("foo[security]", V("1.0"))
        assert ("foo[security]", V("1.0")) in provider.deps_cache

    def test_prioritize_extras_before_base(self) -> None:
        """Extras proxies sort before their base so they pin base versions."""
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet("")
        base = provider.prioritize("foo", spec.to_range(), {})
        extra = provider.prioritize("foo[security]", spec.to_range(), {})
        assert extra < base

    def test_marker_cache_hits_on_repeat(self) -> None:
        """Reclassifying the same Requirement hits the marker caches."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator, target=_PY312)
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
        """``extra``-gated dep is dropped when no provided extra matches the marker."""
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
        provider = Provider(coordinator, target=_PY312)
        base_deps = provider.get_dependencies("foo", V("1.0"))
        sec_deps = provider.get_dependencies("foo[security]", V("1.0"))
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
        provider = Provider(coordinator, target=_PY312)
        sec_deps = provider.get_dependencies("foo[security]", V("1.0"))
        socks_deps = provider.get_dependencies("foo[socks]", V("1.0"))
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
        provider = Provider(coordinator, target=_PY312)
        base_deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in base_deps
        extra_deps = provider.get_dependencies("foo[dev]", V("1.0"))
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
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps
        assert "bar[baz]" in deps

    def test_transitive_extras_in_extra_deps(self) -> None:
        """Extra-gated deps with extras create proxy packages."""
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
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo[all]", V("1.0"))
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
        provider = Provider(coordinator, target=_PY312)
        base_deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar[http]" in base_deps
        extra_deps = provider.get_dependencies("foo[all]", V("1.0"))
        assert "bar[http]" in extra_deps

    def test_extra_tighter_constraint_than_base(self) -> None:
        """A stricter extra-gated bound is recorded even when the name is in base."""
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
        provider = Provider(coordinator, target=_PY312)
        extra_deps = provider.get_dependencies("foo[tight]", V("1.0"))
        assert "bar" in extra_deps
        assert V("1.5") not in extra_deps["bar"]
        assert V("2.5") in extra_deps["bar"]

    def test_extra_self_constraint_intersects_proxy_pin(self) -> None:
        """An extra-gated bound on the base intersects with the proxy pin.

        ``foo>=2; extra == "bar"`` in foo 1.0's metadata makes
        foo[bar]==1.0 unsatisfiable, so the recorded range for foo must
        be empty, not the bare pin.
        """
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
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo[bar]", V("1.0"))
        assert deps["foo"].is_empty

    def test_extra_self_constraint_compatible_keeps_pin(self) -> None:
        """A satisfiable extra-gated self bound leaves the proxy pin intact."""
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
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo[bar]", V("1.0"))
        assert deps["foo"] == VersionRange.singleton(V("1.0"))

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
        provider = Provider(coordinator, target=_PY312)
        base_deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in base_deps
        assert "bar[http]" not in base_deps  # base doesn't have the extra
        extra_deps = provider.get_dependencies("foo[all]", V("1.0"))
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
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps
        assert "bar[http]" in deps
        assert "bar[socks]" in deps

    def test_extra_gated_multi_extra_dep_creates_all_proxies(self) -> None:
        """An extra-gated ``bar[http,socks]`` dep yields both proxies."""
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
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo[all]", V("1.0"))
        assert "bar" in deps
        assert "bar[http]" in deps
        assert "bar[socks]" in deps
        assert "foo" in deps

    def test_warn_mode_missing_extra(self) -> None:
        """WARN mode logs and returns only the base pin for missing extra."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=NO_EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312, extras_mode=ExtrasMode.WARN)
        deps = provider.get_dependencies("foo[nonexistent]", V("1.0"))
        assert deps == {"foo": VersionRange.singleton(V("1.0"))}

    def test_error_user_mode_raises_for_root_extra(self) -> None:
        """ERROR_USER raises for user-provided missing extra."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=NO_EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            extras_mode=ExtrasMode.ERROR_USER,
            root_extras={("foo", "nonexistent")},
        )
        with pytest.raises(MissingExtraError):
            provider.get_dependencies("foo[nonexistent]", V("1.0"))

    def test_error_user_mode_warns_for_transitive(self) -> None:
        """ERROR_USER warns (not errors) for transitive missing extra."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=NO_EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            extras_mode=ExtrasMode.ERROR_USER,
        )
        deps = provider.get_dependencies("foo[nonexistent]", V("1.0"))
        assert deps == {"foo": VersionRange.singleton(V("1.0"))}

    def test_missing_extra_proxy_and_base_converge(self) -> None:
        """A warn-and-drop extra still keeps proxy and base in lockstep.

        alpha 2.0 provides ext-one, whose dependency beta has no
        versions; alpha 1.0 does not provide it.  The proxy backtracks
        to 1.0 and must drag the base along, otherwise the resolution
        pins alpha 2.0 while silently dropping ext-one's dependencies.
        """
        listings = {
            "alpha": [make_wheel("2.0"), make_wheel("1.0")],
            "beta": [],
            "gamma": [make_wheel("1.0")],
        }
        coordinator = make_coordinator(listings=listings)
        coordinator.index.store_metadata(
            "alpha",
            "2.0",
            "Metadata-Version: 2.1\nName: alpha\nVersion: 2.0\n"
            "Provides-Extra: ext-one\n"
            'Requires-Dist: beta; extra == "ext-one"\n',
        )
        coordinator.index.store_metadata(
            "alpha", "1.0", "Metadata-Version: 2.1\nName: alpha\nVersion: 1.0\n"
        )
        coordinator.index.store_metadata(
            "gamma",
            "1.0",
            "Metadata-Version: 2.1\nName: gamma\nVersion: 1.0\n"
            "Requires-Dist: alpha[ext-one]\n",
        )
        roots = {"gamma": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=roots)
        resolver: Resolver[str, Version] = Resolver(
            provider, range_type=VersionRange, root_version="0"
        )
        result = resolver.resolve(roots)
        assert result["alpha[ext-one]"] == result["alpha"] == V("1.0")
        assert "beta" not in result

    def test_backtrack_mode_raises_for_root_extra(self) -> None:
        """BACKTRACK raises for user-provided missing extra."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=NO_EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            extras_mode=ExtrasMode.BACKTRACK,
            root_extras={("foo", "nonexistent")},
        )
        with pytest.raises(MissingExtraError):
            provider.get_dependencies("foo[nonexistent]", V("1.0"))

    def test_backtrack_mode_skips_version_without_extra(self) -> None:
        """BACKTRACK skips versions where the extra is missing."""
        meta_v2 = "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar\n"
        meta_v1 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            "Provides-Extra: security\n"
            'Requires-Dist: cryptography; extra == "security"\n'
        )
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": meta_v2, "1.0": meta_v1},
            package="foo",
        )

        provider = Provider(
            coordinator,
            target=_PY312,
            extras_mode=ExtrasMode.BACKTRACK,
        )
        spec = SpecifierSet("")
        version = provider.choose_version("foo[security]", spec.to_range())
        assert version == V("1.0")

    def test_no_metadata_raises(self) -> None:
        """Extras on a package with no metadata raises MetadataError."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False)], package="foo"
        )
        provider = Provider(coordinator)
        with pytest.raises(MetadataError):
            provider.get_dependencies("foo[security]", V("1.0"))

    def test_look_ahead_skipped_for_extras(self) -> None:
        """Look-ahead always passes for extras packages."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        assert provider._look_ahead_ok("foo[security]", V("1.0")) is True

    def test_warn_mode_choose_version_empty(self) -> None:
        """WARN mode choose_version returns None for empty candidates."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator, extras_mode=ExtrasMode.WARN)
        spec = SpecifierSet(">=99.0")
        assert provider.choose_version("foo[security]", spec.to_range()) is None

    def test_backtrack_mode_user_extra_returns_first(self) -> None:
        """BACKTRACK mode returns first candidate for user-provided extras.

        A user extra skips the ``Provides-Extra`` gating that transitive
        extras get in BACKTRACK mode, so 2.0 is returned even though its
        minimal metadata declares no extras.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo", auto_metadata=True)
        provider = Provider(
            coordinator,
            extras_mode=ExtrasMode.BACKTRACK,
            root_extras={("foo", "security")},
        )
        spec = SpecifierSet("")
        version = provider.choose_version("foo[security]", spec.to_range())
        assert version == V("2.0")

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
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo[dev]", V("1.0"))
        assert "good" in deps
        assert "broken" in deps
        assert "custom" in deps["broken"]
        assert V("1.0") not in deps["broken"]

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
        provider = Provider(coordinator, target=_PY312)
        provider.versions_cache["cryptography"] = [(V("1.0"), make_wheel("1.0"))]
        deps = provider.get_dependencies("foo[security]", V("1.0"))
        assert "cryptography" in deps

    def test_backtrack_all_versions_rejected(self) -> None:
        """BACKTRACK returns None when no version provides the extra."""
        metadata = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar\n"
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            extras_mode=ExtrasMode.BACKTRACK,
        )
        spec = SpecifierSet("")
        assert provider.choose_version("foo[missing]", spec.to_range()) is None


class TestExtrasPrereleaseAdmission:
    """An extras proxy must not drop the base range's pre-release admission.

    The proxy delegates to the base's version list, so the admission a
    specifier grants must survive the proxy hop: ``c[extra]>=0.5a1`` and
    ``c>=0.5a1`` select the same base version.
    """

    C_META = "Metadata-Version: 2.1\nName: c\nVersion: {v}\nProvides-Extra: extra\n\n"

    def _coordinator_for_c(self) -> MagicMock:
        listings = {"c": [make_wheel("2.0.0a1"), make_wheel("1.0.0")]}
        metadata = {v: self.C_META.format(v=v) for v in ("1.0.0", "2.0.0a1")}
        return make_coordinator(listings=listings, metadata_by_version=metadata)

    def _resolve(self, requirements: list[str]) -> dict[str, Version]:
        root_reqs, root_extras = _build_resolver_inputs(
            [Requirement(r) for r in requirements],
            NabProjectConfig(),
            environment={},
        )
        provider = Provider(
            self._coordinator_for_c(),
            target=_PY312,
            root_requirements=root_reqs,
            root_extras=root_extras,
        )
        return Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            root_reqs
        )

    def test_extra_requirement_prerelease_admits(self) -> None:
        """``c[extra]>=0.5a1`` admits the pre-release exactly like ``c>=0.5a1``."""
        pins = self._resolve(["c[extra]>=0.5a1"])
        assert pins["c"] == V("2.0.0a1")

    @pytest.mark.parametrize(
        "requirements",
        [
            ["c[extra]>=0.5a1", "c>=1.0"],
            ["c>=1.0", "c[extra]>=0.5a1"],
            ["c>=0.5a1", "c[extra]>=1.0"],
        ],
    )
    def test_base_and_extra_requirements_share_admission(
        self, requirements: list[str]
    ) -> None:
        """Plain and extra requirements share the admission, from either side."""
        pins = self._resolve(requirements)
        assert pins["c"] == V("2.0.0a1")

    def test_extra_admission_survives_backtrack(self) -> None:
        """A dep-declared ``c[extra]>=0.5a1`` admits after its parent is repicked.

        b has three versions to a's two, so a is decided first. a 2.0 needs
        e==6.0, which conflicts with b's e==5.0 only once b is decided,
        forcing a real backjump to a 1.5. That a 1.5 names the extra, and its
        admission must reach c.
        """
        listings = {
            "a": [make_wheel("2.0"), make_wheel("1.5")],
            "b": [make_wheel("30.0"), make_wheel("20.0"), make_wheel("10.0")],
            "c": [make_wheel("2.0.0a1"), make_wheel("1.0.0")],
            "e": [make_wheel("6.0"), make_wheel("5.0")],
        }
        b_meta = (
            "Metadata-Version: 2.1\nName: b\nVersion: {v}\nRequires-Dist: e==5.0\n\n"
        )
        metadata = {
            "2.0": (
                "Metadata-Version: 2.1\nName: a\nVersion: 2.0\n"
                "Requires-Dist: c>=1.0\nRequires-Dist: e==6.0\n\n"
            ),
            "1.5": (
                "Metadata-Version: 2.1\nName: a\nVersion: 1.5\n"
                "Requires-Dist: c>=1.0\nRequires-Dist: c[extra]>=0.5a1\n\n"
            ),
            "30.0": b_meta.format(v="30.0"),
            "20.0": b_meta.format(v="20.0"),
            "10.0": b_meta.format(v="10.0"),
            "1.0.0": self.C_META.format(v="1.0.0"),
            "2.0.0a1": self.C_META.format(v="2.0.0a1"),
            "5.0": "Metadata-Version: 2.1\nName: e\nVersion: 5.0\n\n",
            "6.0": "Metadata-Version: 2.1\nName: e\nVersion: 6.0\n\n",
        }
        coordinator = make_coordinator(listings=listings, metadata_by_version=metadata)
        root_reqs = {
            "a": VersionRange.full(admit_arbitrary=False),
            "b": VersionRange.full(admit_arbitrary=False),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        pins = resolver.resolve(root_reqs)
        assert resolver.stats.backjumps > 0
        assert pins["a"] == V("1.5")
        assert pins["e"] == V("5.0")
        assert pins["c"] == V("2.0.0a1")


class TestExtrasPrereleaseBaseRangeBlocks:
    """Bounds-excluded pre-releases must be recorded as base-range blocks.

    The proxy's own range is built full, and PEP 440 default filtering
    buffers a pre-release whenever a final in range matches.  A pre-release
    that only the base's bounds exclude therefore never reached the block
    recompute, so the resolver baked a permanent NO_VERSIONS clause over
    the proxy that outlived the backjump lifting the base decision.
    """

    C_META_WITH_X = (
        "Metadata-Version: 2.1\nName: c\nVersion: {v}\nProvides-Extra: x\n\n"
    )
    C_META_NO_X = "Metadata-Version: 2.1\nName: c\nVersion: {v}\n\n"

    def _s2_coordinator(self, *, with_escape: bool) -> MagicMock:
        r_wheels = [make_wheel("2.0"), make_wheel("1.0")]
        if with_escape:
            r_wheels.append(make_wheel("0.5"))
        listings = {
            "r": r_wheels,
            "c": [make_wheel("2.0.0a1"), make_wheel("1.0.0")],
        }
        metadata = {
            "2.0": (
                "Metadata-Version: 2.1\nName: r\nVersion: 2.0\n"
                "Requires-Dist: c[x]>=1.0,<1.5\n\n"
            ),
            "1.0": (
                "Metadata-Version: 2.1\nName: r\nVersion: 1.0\n"
                "Requires-Dist: c[x]>=1.5a0\n\n"
            ),
            "0.5": "Metadata-Version: 2.1\nName: r\nVersion: 0.5\n\n",
            "1.0.0": self.C_META_NO_X.format(v="1.0.0"),
            "2.0.0a1": self.C_META_WITH_X.format(v="2.0.0a1"),
        }
        return make_coordinator(listings=listings, metadata_by_version=metadata)

    def _resolve_r(
        self, coordinator: MagicMock
    ) -> tuple[dict[str, Version], Resolver[str, Version]]:
        root_reqs = {"r": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(
            coordinator,
            target=_PY312,
            extras_mode=ExtrasMode.BACKTRACK,
            root_requirements=root_reqs,
        )
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        return resolver.resolve(root_reqs), resolver

    def test_bounds_excluded_prerelease_repins_over_escape_hatch(self) -> None:
        """r=2.0 needs c[x]<1.5 (final only), r=1.0 needs the c pre-release.

        The bounds-excluded pre-release c 2.0.0a1 must be recorded as a
        conditional base-range block, not a permanent proxy NO_VERSIONS
        clause, so the resolver backs off r=2.0 to r=1.0 instead of
        settling on the depless r=0.5 escape hatch.
        """
        pins, resolver = self._resolve_r(self._s2_coordinator(with_escape=True))
        assert pins["r"] == V("1.0")
        assert pins["c"] == V("2.0.0a1")
        assert pins["c[x]"] == V("2.0.0a1")
        assert resolver.stats.backjumps > 0

    def test_bounds_excluded_prerelease_resolves_without_escape_hatch(self) -> None:
        """Same shape without r=0.5 still resolves to r=1.0.

        A permanent proxy clause would make the resolve spuriously
        impossible; the conditional block lets it settle.
        """
        pins, _ = self._resolve_r(self._s2_coordinator(with_escape=False))
        assert pins["r"] == V("1.0")
        assert pins["c"] == V("2.0.0a1")

    def test_recorded_block_includes_bounds_excluded_prerelease(self) -> None:
        """A recorded block names the bounds-excluded pre-release.

        Default filtering buffers and drops it once the final 1.0.0
        matches, so the block naming c 2.0.0a1 appears only when the
        recompute admits pre-releases.
        """
        listings = {"c": [make_wheel("2.0.0a1"), make_wheel("1.0.0")]}
        metadata = {
            "2.0.0a1": self.C_META_WITH_X.format(v="2.0.0a1"),
            "1.0.0": self.C_META_NO_X.format(v="1.0.0"),
        }
        coordinator = make_coordinator(listings=listings, metadata_by_version=metadata)
        provider = Provider(
            coordinator, target=_PY312, extras_mode=ExtrasMode.BACKTRACK
        )
        # Base decided to the final 1.0.0, which does not provide x, so the
        # proxy has no candidate; the pre-release 2.0.0a1 is excluded only
        # by the base's bounds.
        provider.receive_partial_solution_hint(
            {"c": SpecifierSet("==1.0.0").to_range()},
            {"c": V("1.0.0")},
        )
        result = provider.choose_version("c[x]", VersionRange.full())
        assert result is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        terms = clauses[0].terms
        proxy_terms = [t for t in terms if t.package == "c[x]"]
        base_terms = [t for t in terms if t.package == "c"]
        assert len(proxy_terms) == 1
        assert len(base_terms) == 1
        assert proxy_terms[0].is_positive()
        assert V("2.0.0a1") in proxy_terms[0].constraint
        assert V("1.0.0") in base_terms[0].constraint


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
        provider = Provider(coordinator, dist_policy=DistPolicy.PREFER_WHEEL)
        versions = provider.fetch_versions("pkg")
        assert len(versions) == 2
        assert isinstance(versions[0][1], WheelFile)
        assert isinstance(versions[1][1], SdistFile)

    def test_sdist_install_keeps_wheels_and_sorts_wheels_first(self) -> None:
        """SDIST_INSTALL behaves like WHEEL_OR_SDIST for the listing.

        Wheels stay in the listing (the resolver reads metadata from
        them), but they sort before the sdist at the same version so
        the metadata picker hits the cheapest source first.  The
        lockfile pin is what filters wheels out, not this stage.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0"), make_wheel("1.0")],
            metadata_text="Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n",
        )
        provider = Provider(coordinator, dist_policy=DistPolicy.SDIST_INSTALL)
        versions = provider.fetch_versions("pkg")
        assert len(versions) == 2
        assert isinstance(versions[0][1], WheelFile)
        assert isinstance(versions[1][1], SdistFile)

    def test_sdist_install_resolves_from_wheel_metadata(self) -> None:
        """A dynamic-deps sdist is not built when a wheel publishes the deps.

        The package ships both a wheel (with PEP 658 ``Requires-Dist:
        dep-a``) and an sdist whose PKG-INFO marks dependencies as
        Dynamic.  Under SDIST_INSTALL the resolver should pick the
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
            dist_policy=DistPolicy.SDIST_INSTALL,
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
        """Raise MetadataError when PKG-INFO cannot be extracted."""
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
        # Two versions: v2 has metadata, v1 doesn't.
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
        """Listing with both wheels and sdists is stored correctly."""
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


class TestFetchVersionsNotInIndex:
    def test_listing_not_in_index_blocks_on_request(self) -> None:
        """When listing is not in the index, request_listing + wait is used."""
        wheels = [make_wheel("1.0")]
        # Build a coordinator where get_listing returns None initially
        # but request_listing populates the index.
        index = InMemoryIndex()

        coordinator = MagicMock()
        coordinator.index = index

        def _request_listing(pkg: str) -> threading.Event:
            # Simulate the coordinator populating the index after request.
            index.store_listing(pkg, wheels)
            return _done_event()

        coordinator.request_listing.side_effect = _request_listing
        coordinator.request_metadata.side_effect = lambda p, v, u, h=None: _done_event()
        coordinator.request_metadata_batch.side_effect = lambda items: [
            (p, v, _done_event()) for p, v, u, h in items
        ]

        provider = Provider(coordinator)
        result = provider.fetch_versions("pkg")
        assert len(result) == 1
        assert result[0][0] == V("1.0")
        coordinator.request_listing.assert_called_with("pkg")

    def test_listing_fetch_error_reraised(self) -> None:
        """A recorded listing fetch error surfaces, not an empty listing."""
        coordinator = make_coordinator(package="bad")
        coordinator.index.store_listing_error("bad", RuntimeError("index 500"))
        provider = Provider(coordinator)
        with pytest.raises(RuntimeError, match="index 500"):
            provider.fetch_versions("bad")


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
        call_args = coordinator.request_metadata_batch.call_args
        assert call_args is not None
        items = call_args[0][0]
        assert all(ver == "1.0" for _, ver, _, _ in items)


class TestPrefetchWalkAhead:
    """``prefetch_walk_ahead`` covers the abort-skip walk after the scan.

    Fired from ``_scan_candidates_pipelined``; submits up to
    ``DEEP_PREFETCH_COUNT`` wheel metadata requests for the front of
    ``versions_cache[normalized]``.  Fire-and-forget; correctness only
    depends on it being a superset of what the resolver later asks for.
    """

    def test_no_op_when_listing_missing(self) -> None:
        """Bare provider with no cached listing makes no batch call."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider.prefetch_walk_ahead("foo")
        coordinator.request_metadata_batch.assert_not_called()

    def test_caps_at_deep_prefetch_count(self) -> None:
        """No more than ``DEEP_PREFETCH_COUNT`` distinct versions submitted."""
        wheels = [make_wheel(f"{i}.0") for i in range(100, 0, -1)]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset_mock()
        provider.prefetch_walk_ahead("foo")
        items = coordinator.request_metadata_batch.call_args[0][0]
        # fetch_versions already prefetched the newest version; it fills a
        # window slot but is skipped as already-held.
        assert len(items) == provider.DEEP_PREFETCH_COUNT - 1

    def test_skips_versions_with_cached_deps(self) -> None:
        """Versions already in ``deps_cache`` are excluded from the batch."""
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        provider.deps_cache[("foo", V("2.0"))] = {}
        coordinator.reset_mock()
        provider.prefetch_walk_ahead("foo")
        items = coordinator.request_metadata_batch.call_args[0][0]
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
        coordinator.reset_mock()
        provider.prefetch_walk_ahead("foo")
        items = coordinator.request_metadata_batch.call_args[0][0]
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
        coordinator.request_metadata("foo", "2.0", two.metadata_url)
        coordinator.reset_mock()
        provider.prefetch_walk_ahead("foo")
        items = coordinator.request_metadata_batch.call_args[0][0]
        versions = [ver for _, ver, _, _ in items]
        assert "2.0" not in versions
        assert "1.0" in versions

    def test_picks_wheel_when_both_present(self) -> None:
        """A version with both a wheel and a sdist still prefetches the wheel."""
        wheels: list[WheelFile | SdistFile] = [
            make_sdist("1.0"),
            make_wheel("1.0"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset_mock()
        provider.prefetch_walk_ahead("foo")
        items = coordinator.request_metadata_batch.call_args[0][0]
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
        coordinator.reset_mock()
        provider.prefetch_walk_ahead("foo")
        items = coordinator.request_metadata_batch.call_args[0][0]
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
        coordinator.reset_mock()
        provider.prefetch_walk_ahead("foo")
        items = coordinator.request_metadata_batch.call_args[0][0]
        versions = [ver for _, ver, _, _ in items]
        assert versions == ["1.0"]

    def test_no_batch_when_all_filtered(self) -> None:
        """Empty items list (everything cached or sdist-only) skips the call."""
        wheels = [make_sdist("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.fetch_versions("foo")
        coordinator.reset_mock()
        provider.prefetch_walk_ahead("foo")
        coordinator.request_metadata_batch.assert_not_called()

    def test_fires_when_pipelined_scan_engages(self) -> None:
        """Trip the look-ahead scan: walk_ahead fires before the 8-batch starts."""
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
        with patch.object(
            provider, "prefetch_walk_ahead", wraps=provider.prefetch_walk_ahead
        ) as spy:
            provider.choose_version("foo", VersionRange.full())
        spy.assert_called_with("foo")

    def test_does_not_fire_when_first_candidate_accepted(self) -> None:
        """No rejection means no pipelined scan and no deep prefetch."""
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_text="Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            package="foo",
        )
        root_reqs = {"foo": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        with patch.object(
            provider, "prefetch_walk_ahead", wraps=provider.prefetch_walk_ahead
        ) as spy:
            provider.choose_version("foo", SpecifierSet(">=1.0").to_range())
        spy.assert_not_called()


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
        coordinator.request_metadata.assert_not_called()


# The sidecar of ``make_wheel("1.0")``: the batch path reads the metadata
# back by the artifact it submitted.
_SIDECAR_URL = "https://example.com/pkg-1.0-py3-none-any.whl.metadata"


class TestAwaitMetadataBatchEdgeCases:
    def test_skips_versions_already_in_deps_cache(self) -> None:
        """Submitted entries already in ``deps_cache`` short-circuit the wait.

        Pre-empting the walk-ahead deep prefetch can land metadata for a
        version that the next pipelined batch also submits; by the time
        ``_await_metadata_batch`` runs, that version's deps are cached
        already and re-parsing is wasted work.
        """
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.deps_cache[("foo", V("1.0"))] = {"bar": VersionRange.full()}
        provider._await_metadata_batch(
            "foo",
            [(V("1.0"), "1.0", _SIDECAR_URL, _done_event())],
        )
        assert provider.deps_cache[("foo", V("1.0"))] == {"bar": VersionRange.full()}

    def test_batch_invalid_metadata_refuses_version(self) -> None:
        """Malformed metadata in the batch path refuses the version.

        v2.0 really depends on pytz but its metadata is unparseable.
        Caching it as dependency-free would pin an under-constrained
        lock, so the batch path leaves it un-cached and look-ahead's
        get_dependencies refuses it. The scan falls through to v1.0.
        """
        # v3.0: valid metadata, conflicts with root (look-ahead fails).
        # v2.0: garbage metadata, hit via batch processing.
        # v1.0: valid, clean, passes look-ahead.
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={
                "3.0": "Metadata-Version: 2.1\nName: foo\nVersion: 3.0\nRequires-Dist: bar>=5.0\n",
                "2.0": "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: pytz (>dev)\n",
                "1.0": "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            },
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        result = provider.choose_version("foo", spec.to_range())
        assert result == V("1.0")
        assert ("foo", V("2.0")) not in provider.deps_cache
        assert provider.has_invalid_metadata("foo", V("2.0"))

    def test_batch_none_metadata_refuses_version(self) -> None:
        """Missing metadata text in the batch path refuses the version.

        A failed PEP 658 fetch (text is None) must not pin the version as
        dependency-free; the batch leaves it un-cached and get_dependencies
        refuses it after the sdist fallback finds nothing.
        """
        # v3.0: valid, conflicts with root (look-ahead fails).
        # v2.0: metadata not provided (None), hit via batch processing.
        # v1.0: valid, clean.
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={
                "3.0": "Metadata-Version: 2.1\nName: foo\nVersion: 3.0\nRequires-Dist: bar>=5.0\n",
                # v2.0 intentionally omitted: get_metadata returns None
                "1.0": "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            },
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        spec = SpecifierSet("")
        result = provider.choose_version("foo", spec.to_range())
        assert result == V("1.0")
        assert ("foo", V("2.0")) not in provider.deps_cache

    def test_batch_metadata_hash_mismatch_aborts(self) -> None:
        """A recorded integrity failure in the batch path aborts the wait
        rather than leaving the version un-cached for an sdist fallback."""
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        coordinator.index.store_metadata_error(
            "foo",
            "1.0",
            MetadataHashMismatchError("metadata sha256 mismatch"),
            _SIDECAR_URL,
        )
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(MetadataHashMismatchError):
            provider._await_metadata_batch(
                "foo",
                [(V("1.0"), "1.0", _SIDECAR_URL, _done_event())],
            )
        assert ("foo", V("1.0")) not in provider.deps_cache

    def test_batch_does_not_parse_sdist_pkg_info_as_wheel_metadata(self) -> None:
        """Sdist PKG-INFO in the shared slot is not cached by the batch path.

        When a wheel's PEP 658 fetch yields nothing, get_dependencies
        falls back to the sdist, stores its PKG-INFO in the shared
        metadata slot, and rejects the version under the strict PEP 643
        default. A later batch await reads the same slot; caching that
        text as wheel METADATA would bypass the gate and resurrect the
        rejected version with unverified deps.
        """
        dists = [make_sdist("1.0"), make_wheel("1.0")]
        coordinator = make_coordinator(dists, sdist_pkg_info=PKG_INFO_PRE_PEP643_DEPS)
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

        version_list = provider.fetch_versions("pkg")
        wheel_map = provider._wheel_by_version("pkg", version_list)
        submitted = provider._prefetch_batch("pkg", [V("1.0")], wheel_map)
        provider._await_metadata_batch("pkg", submitted)

        assert ("pkg", V("1.0")) not in provider.deps_cache
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_batch_leaves_an_empty_sidecar_on_the_sdists_terms(self) -> None:
        """A sidecar that served no text reads PKG-INFO, and stays gated.

        The fetcher records a sidecar that served nothing as the ``None`` of
        its own slot, so the batch read falls back to the sdist's PKG-INFO.
        Caching that as wheel METADATA would bypass the PEP 643 gate.
        """
        wheel = make_wheel("1.0")
        assert wheel.metadata_url is not None
        dists = [make_sdist("1.0"), wheel]
        coordinator = make_coordinator(dists, sdist_pkg_info=PKG_INFO_PRE_PEP643_DEPS)
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))
        coordinator.index.store_metadata("pkg", "1.0", None, wheel.metadata_url)

        version_list = provider.fetch_versions("pkg")
        wheel_map = provider._wheel_by_version("pkg", version_list)
        submitted = provider._prefetch_batch("pkg", [V("1.0")], wheel_map)
        provider._await_metadata_batch("pkg", submitted)

        assert ("pkg", V("1.0")) not in provider.deps_cache


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
        # Batch should only contain the wheel v1.0, not the sdist v2.0.
        if coordinator.request_metadata_batch.called:
            items = coordinator.request_metadata_batch.call_args[0][0]
            for _, ver, _, _ in items:
                assert ver != "2.0"


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


class TestPrioritizeMatchingFromIndex:
    def test_matching_count_from_index(self) -> None:
        """prioritize computes matching count from index listing."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        # Don't pre-fill versions_cache; let prioritize pull from index.
        spec = SpecifierSet(">=2.0")
        result = provider.prioritize("foo", spec.to_range(), {})
        assert result == (Provider.TIER_NORMAL, 2, True)

    def test_matching_count_defaults_to_1000(self) -> None:
        """prioritize returns 1000 when listing is not available."""
        coordinator = make_coordinator(None, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=1.0")
        result = provider.prioritize("foo", spec.to_range(), {})
        assert result == (Provider.TIER_NORMAL, 1000, True)

    def test_matching_recomputed_after_listing_arrives(self) -> None:
        """The in-flight placeholder is not cached; arrival recomputes."""
        coordinator = make_coordinator(None, package="foo")
        provider = Provider(coordinator)
        rng = SpecifierSet(">=1.0").to_range()
        assert provider.prioritize("foo", rng, {}) == (
            Provider.TIER_NORMAL,
            1000,
            True,
        )
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        # Store directly into the index, as the fetcher thread would.
        coordinator.index.store_listing("foo", wheels)
        assert provider.prioritize("foo", rng, {}) == (
            Provider.TIER_NORMAL,
            3,
            True,
        )
        assert "foo" in provider.versions_cache

    def test_prioritize_uses_versions_cache(self) -> None:
        """prioritize skips index check when versions cache is populated."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        # Pre-populate versions cache directly.
        provider.versions_cache["foo"] = [
            (V("3.0"), make_wheel("3.0")),
            (V("2.0"), make_wheel("2.0")),
            (V("1.0"), make_wheel("1.0")),
        ]
        spec = SpecifierSet(">=2.0")
        result = provider.prioritize("foo", spec.to_range(), {})
        assert result == (Provider.TIER_NORMAL, 2, True)

    def test_prioritize_priority_cache_hit(self) -> None:
        """Repeat prioritize with the same Range object returns the cached tuple."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet(">=2.0")
        intervals = spec.to_range()
        first = provider.prioritize("foo", intervals, {})
        second = provider.prioritize("foo", intervals, {})
        assert first == second
        assert ("foo", intervals) in [
            (k, v[0]) for k, v in provider.priority_cache.items()
        ]

    def test_matching_cache_inner_dict_reused_for_new_range(self) -> None:
        """Two distinct ranges for the same package share the inner dict."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec_a = SpecifierSet(">=1.0").to_range()
        spec_b = SpecifierSet(">=2.0").to_range()
        provider.prioritize("foo", spec_a, {})
        # Second call with a different range should populate the same
        # inner dict instead of allocating a new one.
        provider.prioritize("foo", spec_b, {})
        per_pkg = provider.matching_cache["foo"]
        assert spec_a in per_pkg
        assert spec_b in per_pkg

    def test_versions_only_cache_hit(self) -> None:
        """Calling versions_only twice returns the same cached list."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        version_list = provider.fetch_versions("foo")
        first = provider.versions_only("foo", version_list)
        second = provider.versions_only("foo", version_list)
        assert first is second

    def test_wheel_by_version_cache_hit(self) -> None:
        """Calling _wheel_by_version twice returns the same cached dict."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        version_list = provider.fetch_versions("foo")
        first = provider._wheel_by_version("foo", version_list)
        second = provider._wheel_by_version("foo", version_list)
        assert first is second


PKG_INFO_DYNAMIC_DEPS = (
    "Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\nDynamic: Requires-Dist\n"
)

PKG_INFO_PRE_PEP643_DEPS = (
    "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: hidden-dep\n"
)

PKG_INFO_DYNAMIC_PROVIDES_EXTRA = (
    "Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\n"
    "Requires-Dist: dep-a\nDynamic: Provides-Extra\n"
)


class TestSharedSlotProvenance:
    def test_pkg_info_stays_gated_for_provider_with_wheel_in_view(self) -> None:
        """PKG-INFO stored by one provider stays sdist-gated for another.

        Universal mode shares one coordinator index across tuple
        providers. A tuple whose filtered view is sdist-only stores
        PKG-INFO in the shared slot; a second tuple with the wheel in
        view must not relabel that text as wheel METADATA and trust
        the pre-PEP-643 deps.
        """
        coordinator = make_coordinator(None, sdist_pkg_info=PKG_INFO_PRE_PEP643_DEPS)
        first = Provider(coordinator, target=_PY312)
        first.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]
        with pytest.raises(UnsupportedSdistError):
            first.get_dependencies("pkg", V("1.0"))

        second = Provider(coordinator, target=_PY312)
        second.versions_cache["pkg"] = [
            (V("1.0"), make_wheel("1.0")),
            (V("1.0"), make_sdist("1.0")),
        ]
        with pytest.raises(UnsupportedSdistError):
            second.get_dependencies("pkg", V("1.0"))

    def test_an_sdist_only_view_reads_the_sdists_own_pkg_info(self) -> None:
        """A wheel's METADATA does not answer for a tuple that installs the sdist.

        The reverse ordering: a provider with the wheel in view stores its
        METADATA, then a provider whose view is sdist-only reads.  The two
        artifacts can declare different dependencies, and the origin travels
        with the text, so the sdist's PEP 643 static PKG-INFO is trusted on
        its own terms rather than replaced by the wheel's deps.
        """
        metadata = (
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: dep-a\n"
        )
        pkg_info = (
            "Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\nRequires-Dist: dep-b\n"
        )
        coordinator = make_coordinator(
            None, metadata_text=metadata, sdist_pkg_info=pkg_info
        )
        first = Provider(coordinator, target=_PY312)
        first.versions_cache["pkg"] = [(V("1.0"), make_wheel("1.0"))]
        assert "dep-a" in first.get_dependencies("pkg", V("1.0"))

        second = Provider(coordinator, target=_PY312)
        second.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]
        assert "dep-b" in second.get_dependencies("pkg", V("1.0"))


class TestPickDistForMetadata:
    def test_prefers_wheel_with_metadata_when_sdist_first(self) -> None:
        """A wheel-with-meta wins over an earlier sdist at the same version."""
        sdist = make_sdist("1.0")
        wheel = make_wheel("1.0")
        versions = [(V("1.0"), sdist), (V("1.0"), wheel)]
        assert pick_dist_for_metadata(versions, V("1.0")) is wheel

    def test_prefers_any_wheel_over_sdist_when_no_pep658(self) -> None:
        """Without PEP 658, a plain wheel still wins over an sdist.

        Range-fetching ``METADATA`` from a wheel is cheaper than
        building an sdist, so the picker only falls back to the sdist
        when no wheel exists at the version.  This matters for
        :attr:`~nab_python.provider.DistPolicy.SDIST_INSTALL`: those
        listings keep wheels purely so the resolver can read their
        metadata, and ``pick_dist_for_metadata`` must honour that.
        """
        sdist = make_sdist("1.0")
        wheel_no_meta = make_wheel("1.0", has_metadata=False)
        versions = [(V("1.0"), sdist), (V("1.0"), wheel_no_meta)]
        assert pick_dist_for_metadata(versions, V("1.0")) is wheel_no_meta

    def test_falls_back_to_sdist_when_no_wheel_at_version(self) -> None:
        """When no wheel exists at the version, the sdist is returned."""
        sdist = make_sdist("1.0")
        versions = [(V("1.0"), sdist)]
        assert pick_dist_for_metadata(versions, V("1.0")) is sdist

    def test_returns_none_for_unknown_version(self) -> None:
        """No matching version yields ``None``."""
        versions = [(V("2.0"), make_wheel("2.0"))]
        assert pick_dist_for_metadata(versions, V("1.0")) is None

    def test_first_match_wins_for_each_dist_kind(self) -> None:
        """Repeated entries at the same version do not displace the first.

        The picker keeps the *first* candidate in each preference tier
        (wheel-with-meta, wheel-without-meta, sdist) it sees, so a
        later candidate of the same kind never silently overrides an
        earlier one.  Covers the ``is None`` guards on each tier.
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
        assert pick_dist_for_metadata(versions, V("1.0")) is first_meta
        # Strip PEP 658 wheels: the plain wheel still wins, first one
        # encountered wins within the tier.
        versions_no_meta = [
            (V("1.0"), first_sdist),
            (V("1.0"), second_sdist),
            (V("1.0"), first_plain),
            (V("1.0"), second_plain),
        ]
        assert pick_dist_for_metadata(versions_no_meta, V("1.0")) is first_plain
        # And with sdists only, first sdist wins.
        versions_sdist_only = [
            (V("1.0"), first_sdist),
            (V("1.0"), second_sdist),
        ]
        assert pick_dist_for_metadata(versions_sdist_only, V("1.0")) is first_sdist


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


class TestEffectiveBuildPolicy:
    """Overrides replace the global build policy per package/index."""

    def test_no_overrides_falls_back_to_global(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator, build_policy=BuildPolicy.BUILD_LOCAL)
        assert (
            provider.effective_build_policy("anything", V("1.0"))
            is BuildPolicy.BUILD_LOCAL
        )

    def test_override_replaces_global_for_named_package(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            build_policy=BuildPolicy.BUILD_LOCAL,
            package_overrides=(
                pkg_override("pyspark-client", build_policy=BuildPolicy.BUILD_REMOTE),
            ),
        )
        assert (
            provider.effective_build_policy("pyspark-client", V("1.0"))
            is BuildPolicy.BUILD_REMOTE
        )
        assert (
            provider.effective_build_policy("other-pkg", V("1.0"))
            is BuildPolicy.BUILD_LOCAL
        )

    def test_override_can_be_more_restrictive(self) -> None:
        """An override at NEVER while the global is permissive blocks the
        named package only.
        """
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            build_policy=BuildPolicy.BUILD_REMOTE,
            package_overrides=(
                pkg_override("quarantined", build_policy=BuildPolicy.NEVER),
            ),
        )
        assert (
            provider.effective_build_policy("quarantined", V("1.0"))
            is BuildPolicy.NEVER
        )
        assert (
            provider.effective_build_policy("anything-else", V("1.0"))
            is BuildPolicy.BUILD_REMOTE
        )

    def test_version_scoped_build_policy(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            build_policy=BuildPolicy.BUILD_LOCAL,
            package_overrides=(
                pkg_override("foo <= 2", build_policy=BuildPolicy.NEVER),
            ),
        )
        assert provider.effective_build_policy("foo", V("1.0")) is BuildPolicy.NEVER
        assert (
            provider.effective_build_policy("foo", V("5.0")) is BuildPolicy.BUILD_LOCAL
        )

    def test_per_index_build_policy(self) -> None:
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            build_policy=BuildPolicy.BUILD_LOCAL,
            index_overrides={
                "internal": IndexOverride(build_policy=BuildPolicy.BUILD_REMOTE)
            },
        )
        assert (
            provider.effective_build_policy("foo", V("1.0"), "internal")
            is BuildPolicy.BUILD_REMOTE
        )
        assert (
            provider.effective_build_policy("foo", V("1.0"), "pypi")
            is BuildPolicy.BUILD_LOCAL
        )

    def test_dynamic_sdist_path_under_build_remote_invokes_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``BUILD_REMOTE`` routes a dynamic-deps sdist through the build path.

        Under ``BUILD_LOCAL`` (or no override at NEVER) the path raises
        :class:`UnsupportedSdistError`; under the ``BUILD_REMOTE``
        override the archive is fetched, extracted, and handed to the
        build backend (mocked here).  The previous silent-passthrough
        behaviour (return dynamic metadata as-is) is gone.
        """
        from nab_python._vendor.packaging.version import Version as _Version

        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            package="pkg",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.BUILD_LOCAL,
            package_overrides=(
                pkg_override("pkg", build_policy=BuildPolicy.BUILD_REMOTE),
            ),
        )

        provider.versions_cache["pkg"] = [(_Version("1.0"), make_sdist("1.0"))]
        archive_bytes = b"sdist-archive-bytes"

        def _request_archive(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            coordinator.index.store_sdist_archive(pkg, ver, archive_bytes)
            return _done_event()

        coordinator.request_sdist_archive.side_effect = _request_archive

        captured: dict[str, object] = {}

        def fake_extract(data: bytes, target: object) -> object:
            captured["bytes"] = data
            return target

        built_meta = WheelMetadata(
            name="pkg",
            version=_Version("1.0"),
            requires_python=None,
            requires_dist=[],
            provides_extra=[],
        )

        def fake_build(_path: object, **kwargs: object) -> WheelMetadata:
            captured["kwargs"] = kwargs
            return built_meta

        monkeypatch.setattr(build_remote, "extract_sdist_archive", fake_extract)
        monkeypatch.setattr("nab_python.build_backend.extract_metadata", fake_build)

        starting = WheelMetadata(
            name="pkg",
            version=_Version("1.0"),
            requires_python=None,
            requires_dist=[],
            provides_extra=[],
            dynamic=frozenset({"Requires-Dist"}),
        )
        out = metadata_resolver.resolve_dynamic_sdist(
            provider, ("pkg", _Version("1.0")), starting
        )
        assert out is built_meta
        assert captured["bytes"] == archive_bytes
        # The backend runs on the host interpreter, so the resolve
        # target's Python must not reach the build env.
        assert captured["kwargs"] == {"config": provider.build_config}

    def test_resolve_dynamic_sdist_reuses_cross_tuple_cache(self) -> None:
        """A second call for the same sdist returns the cached metadata.

        The ``InMemoryIndex._resolved_sdist_metadata`` cache is the
        backstop that stops universal mode from re-augmenting (or, more
        importantly, re-building) the same sdist for every tuple.  The
        cache key is the canonical name + version string.
        """
        from nab_python._vendor.packaging.version import Version as _Version

        coordinator = make_coordinator([make_sdist("1.0")], package="pkg")
        provider = Provider(coordinator, target=_PY312)
        cached_meta = WheelMetadata(
            name="pkg",
            version=_Version("1.0"),
            requires_python=None,
            requires_dist=[],
            provides_extra=[],
        )
        coordinator.index.store_resolved_sdist_metadata("pkg", "1.0", cached_meta)

        starting = WheelMetadata(
            name="pkg",
            version=_Version("1.0"),
            requires_python=None,
            requires_dist=[],
            provides_extra=[],
            dynamic=frozenset({"Requires-Dist"}),
        )
        out = metadata_resolver.resolve_dynamic_sdist(
            provider, ("pkg", _Version("1.0")), starting
        )
        assert out is cached_meta


class TestStaticSdistMetadata:
    def test_dynamic_pkg_info_no_pyproject_raises(self) -> None:
        """Dynamic Requires-Dist without pyproject fallback raises under NEVER."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))
        assert provider.stats.excluded_by_build_policy == 1

    def test_unsupported_sdist_is_negatively_cached(self) -> None:
        """Re-asking for a rejected sdist hits the cache, not the parser."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))
        # excluded_by_build_policy is incremented inside _resolve_dynamic_sdist;
        # the cache hit re-raises without going through that path.
        assert provider.stats.excluded_by_build_policy == 1

    def test_dynamic_pkg_info_with_static_pyproject(self) -> None:
        """Static pyproject.toml replaces dynamic Requires-Dist."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dependencies = ['dep-a>=1.0', 'dep-b']\n"
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps
        assert "dep-b" in deps
        assert provider.stats.sdist_pyproject_fallbacks == 1

    def test_pyproject_marks_dependencies_dynamic(self) -> None:
        """Pyproject with dependencies in dynamic falls through to NEVER."""
        pyproject = (
            '[project]\nname = "pkg"\nversion = "1.0"\ndynamic = [\'dependencies\']\n'
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_marks_optional_dependencies_dynamic(self) -> None:
        """Pyproject with optional-dependencies in dynamic also falls through."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dynamic = ['optional-dependencies']\n"
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_with_optional_deps_populates_extras(self) -> None:
        """``optional-dependencies`` is mirrored as Provides-Extra + markers."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dependencies = ['dep-a>=1.0']\n"
            "[project.optional-dependencies]\n"
            "foo = ['dep-b>=1.0']\n"
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps
        # The extras dep is gated by the foo extra, so not in base deps.
        assert "dep-b" not in deps
        metadata = provider.metadata_cache[("pkg", V("1.0"))]
        assert "foo" in metadata.provides_extra

    def test_pyproject_malformed_toml(self) -> None:
        """A pyproject that doesn't parse falls through to UnsupportedSdistError."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml="not = valid = toml = [",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_no_project_table(self) -> None:
        """A pyproject without ``[project]`` is treated as no fallback."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml="[build-system]\nrequires = ['setuptools']\n",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_project_not_a_dict(self) -> None:
        """``[project]`` declared as something other than a table aborts."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml='project = "scalar"\n',
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_dynamic_wrong_type(self) -> None:
        """A non-list ``dynamic`` is treated as empty (no dynamic markers)."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            'dynamic = "scalar"\n'
            "dependencies = ['dep-a']\n"
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps

    def test_pyproject_dependencies_wrong_type(self) -> None:
        """``dependencies`` declared as non-list rejects the version."""
        pyproject = '[project]\nname = "pkg"\nversion = "1.0"\ndependencies = "dep-a"\n'
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(MetadataError, match="must be an array of strings"):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_optional_dependencies_wrong_type(self) -> None:
        """``optional-dependencies`` declared as non-table rejects the version."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dependencies = []\n"
            'optional-dependencies = "foo"\n'
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(MetadataError, match="must be a table"):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_skips_non_string_dynamic_entries(self) -> None:
        """Non-string ``dynamic`` entries are filtered before the check."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dynamic = [42, 'readme']\n"
            "dependencies = ['dep-a']\n"
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps

    def test_pyproject_extra_value_not_list_rejects(self) -> None:
        """An extras value that isn't a list rejects the version."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dependencies = ['dep-a']\n"
            "[project.optional-dependencies]\n"
            'bad = "not-a-list"\n'
            "foo = ['dep-b']\n"
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(MetadataError, match="extra 'bad' must be an array"):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_non_string_dep_entry_rejects(self) -> None:
        """A non-string dependency entry rejects the version."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dependencies = ['dep-a', 42, 'dep-b']\n"
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(MetadataError, match="must be an array of strings"):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_invalid_requirement_rejects(self) -> None:
        """A malformed requirement string rejects the version."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dependencies = ['dep-a', '!!not a valid req!!']\n"
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(MetadataError, match="invalid requirement"):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_invalid_extra_dep_rejects(self) -> None:
        """A malformed extras entry rejects the version."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            "foo = ['!!bad!!', 'dep-b']\n"
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(MetadataError, match="invalid requirement"):
            provider.get_dependencies("pkg", V("1.0"))

    def test_pyproject_non_string_extra_dep_rejects(self) -> None:
        """A non-string entry inside an extras list rejects the version."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            "foo = [42, 'dep-b']\n"
        )
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_pyproject_toml=pyproject,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(MetadataError, match="extra 'foo' must be an array"):
            provider.get_dependencies("pkg", V("1.0"))

    def test_build_remote_invokes_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BUILD_REMOTE`` fetches the sdist, extracts, and builds it."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
        )
        archive_bytes = b"sdist-archive"

        def _request_archive(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            coordinator.index.store_sdist_archive(pkg, ver, archive_bytes)
            return _done_event()

        coordinator.request_sdist_archive.side_effect = _request_archive

        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=None,
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        monkeypatch.setattr(
            build_remote, "extract_sdist_archive", lambda _data, target: target
        )
        monkeypatch.setattr(
            "nab_python.build_backend.extract_metadata", lambda *_a, **_k: built
        )

        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.BUILD_REMOTE,
        )
        deps = provider.get_dependencies("pkg", V("1.0"))
        assert "dep-a" in deps
        assert provider.stats.excluded_by_build_policy == 0

    def test_build_remote_archive_hash_mismatch_aborts_get_dependencies(
        self,
    ) -> None:
        """A tampered build-remote archive aborts get_dependencies."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
        )

        def _tampered_archive(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            coordinator.index.store_sdist_archive_error(
                pkg, ver, SdistHashMismatchError("sdist sha256 mismatch")
            )
            return _done_event()

        coordinator.request_sdist_archive.side_effect = _tampered_archive

        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.BUILD_REMOTE,
        )
        with pytest.raises(SdistHashMismatchError):
            provider.get_dependencies("pkg", V("1.0"))
        assert ("pkg", V("1.0")) not in provider.deps_cache
        assert ("pkg", V("1.0")) not in provider._invalid_metadata

    def test_build_naive_upload_time_aborts_get_dependencies(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A naive upload-time hit while building an sdist aborts, not skips."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
        )

        def _request_archive(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            coordinator.index.store_sdist_archive(pkg, ver, b"sdist-archive-bytes")
            return _done_event()

        coordinator.request_sdist_archive.side_effect = _request_archive

        def _naive_build(_path: object, *, config: object) -> WheelMetadata:
            raise InvalidUploadTimeError(
                "setuptools 68.0.0 has a timezone-naive upload time"
                " '2025-06-01T00:00:00'; the Simple API requires"
                " timezone-aware (UTC) upload times"
            )

        monkeypatch.setattr(
            build_remote, "extract_sdist_archive", lambda data, target: target
        )
        monkeypatch.setattr("nab_python.build_backend.extract_metadata", _naive_build)

        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.BUILD_REMOTE,
        )
        with pytest.raises(InvalidUploadTimeError, match="timezone-naive"):
            provider.get_dependencies("pkg", V("1.0"))
        assert ("pkg", V("1.0")) not in provider.deps_cache
        assert ("pkg", V("1.0")) not in provider._invalid_metadata

    def test_look_ahead_skips_unsupported_sdist(self) -> None:
        """A dynamic sdist is rejected by look-ahead, not raised."""
        # Two versions: 2.0 has a dynamic sdist, 1.0 has a usable wheel.
        wheels = [
            make_wheel("1.0"),
            make_sdist("2.0"),
        ]
        meta_v1 = (
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: dep-a\n"
        )
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"1.0": meta_v1, "2.0": None},
        )

        def _request_sdist(
            pkg: str,
            ver: str,
            url: str,
            hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            coordinator.index.store_sdist_metadata(pkg, ver, PKG_INFO_DYNAMIC_DEPS)
            coordinator.index.store_sdist_pyproject(pkg, ver, None)
            return _done_event()

        coordinator.request_sdist.side_effect = _request_sdist

        root_reqs = {"pkg": SpecifierSet(">=0").to_range()}
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            root_requirements=root_reqs,
        )
        chosen = provider.choose_version("pkg", root_reqs["pkg"])
        assert chosen == V("1.0")


class TestBuildRemoteFailureModes:
    """Failure paths in :func:`nab_python._provider.build_remote.build_remote_sdist`.

    Every failure must surface as :class:`UnsupportedSdistError` so the
    resolver's look-ahead can either skip the version or fold the
    message into the eventual no-versions diagnostic.
    """

    def _provider(
        self,
        *,
        with_sdist: bool,
        overrides: tuple[PackageOverride, ...] = (),
    ) -> Provider:
        files = [make_sdist("1.0")] if with_sdist else [make_wheel("1.0")]
        coordinator = make_coordinator(files, package="pkg")
        return Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.BUILD_REMOTE,
            package_overrides=overrides,
        )

    def test_missing_sdist_in_listing_raises(self) -> None:
        provider = self._provider(with_sdist=False)
        # Listing only has a wheel; build_remote_sdist needs an sdist.
        provider.versions_cache["pkg"] = [(V("1.0"), make_wheel("1.0"))]
        with pytest.raises(UnsupportedSdistError, match="no sdist is available"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_archive_fetch_failure_raises(self) -> None:
        provider = self._provider(with_sdist=True)
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        def _failed_fetch(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            provider.coordinator.index.store_sdist_archive(pkg, ver, None)
            return _done_event()

        cast(
            "MagicMock", provider.coordinator
        ).request_sdist_archive.side_effect = _failed_fetch
        with pytest.raises(UnsupportedSdistError, match="archive.*fetch.*failed"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_archive_hash_mismatch_aborts(self) -> None:
        """A tampered archive raises the integrity error from the build.

        The error must propagate, not degrade to ``UnsupportedSdistError``,
        which the resolve treats as a skippable version.
        """
        provider = self._provider(with_sdist=True)
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        def _tampered_fetch(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            provider.coordinator.index.store_sdist_archive_error(
                pkg, ver, SdistHashMismatchError("sdist sha256 mismatch")
            )
            return _done_event()

        cast(
            "MagicMock", provider.coordinator
        ).request_sdist_archive.side_effect = _tampered_fetch
        with pytest.raises(SdistHashMismatchError):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"not a gzip stream",
            _SDIST_TARGZ[: len(_SDIST_TARGZ) // 2],
        ],
        ids=["empty", "not-gzip", "truncated"],
    )
    def test_unreadable_archive_raises(self, data: bytes) -> None:
        """An unreadable archive raises the skippable ``UnsupportedSdistError``."""
        provider = self._provider(with_sdist=True)
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        def _fetch(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            provider.coordinator.index.store_sdist_archive(pkg, ver, data)
            return _done_event()

        cast(
            "MagicMock", provider.coordinator
        ).request_sdist_archive.side_effect = _fetch
        with pytest.raises(UnsupportedSdistError, match="could not be extracted"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_build_backend_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nab_python import build_backend

        provider = self._provider(with_sdist=True)
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        def _ok_fetch(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            provider.coordinator.index.store_sdist_archive(pkg, ver, b"data")
            return _done_event()

        cast(
            "MagicMock", provider.coordinator
        ).request_sdist_archive.side_effect = _ok_fetch
        monkeypatch.setattr(
            build_remote, "extract_sdist_archive", lambda _d, target: target
        )

        def _boom(*_a: object, **_k: object) -> None:
            raise build_backend.BuildBackendError("backend explosion")

        monkeypatch.setattr("nab_python.build_backend.extract_metadata", _boom)
        with pytest.raises(UnsupportedSdistError, match="backend explosion"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_find_sdist_skips_non_matching_versions(self) -> None:
        provider = self._provider(with_sdist=True)
        # Two versions, only 2.0 has an sdist.
        provider.versions_cache["pkg"] = [
            (V("1.0"), make_wheel("1.0")),
            (V("2.0"), make_sdist("2.0")),
        ]

        def _ok_fetch(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            provider.coordinator.index.store_sdist_archive(pkg, ver, b"data")
            return _done_event()

        cast(
            "MagicMock", provider.coordinator
        ).request_sdist_archive.side_effect = _ok_fetch
        # Asking for 1.0 (wheel-only) raises.
        with pytest.raises(UnsupportedSdistError, match="no sdist is available"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def _build_into(
        self,
        monkeypatch: pytest.MonkeyPatch,
        built: object,
        *,
        overrides: tuple[PackageOverride, ...] = (),
    ) -> Provider:
        provider = self._provider(with_sdist=True, overrides=overrides)
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        def _ok_fetch(
            pkg: str,
            ver: str,
            _url: str,
            _hashes: tuple[tuple[str, str], ...] = (),
        ) -> threading.Event:
            provider.coordinator.index.store_sdist_archive(pkg, ver, b"data")
            return _done_event()

        cast(
            "MagicMock", provider.coordinator
        ).request_sdist_archive.side_effect = _ok_fetch
        monkeypatch.setattr(
            build_remote, "extract_sdist_archive", lambda _d, target: target
        )
        monkeypatch.setattr(
            "nab_python.build_backend.extract_metadata", lambda *_a, **_k: built
        )
        return provider

    def test_built_requires_python_excludes_target_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=SpecifierSet(">=3.13"),
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        provider = self._build_into(monkeypatch, built)
        with pytest.raises(UnsupportedSdistError, match="requires Python"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_built_requires_python_compatible_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=SpecifierSet(">=3.10"),
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        provider = self._build_into(monkeypatch, built)
        result = build_remote.build_remote_sdist(provider, "pkg", V("1.0"))
        assert result is built

    def test_built_requires_python_no_target_skips_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=SpecifierSet(">=3.13"),
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        provider = self._build_into(monkeypatch, built)
        provider.python_version = None
        result = build_remote.build_remote_sdist(provider, "pkg", V("1.0"))
        assert result is built

    def test_override_widens_requires_python_accepts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The built value excludes the 3.12 target; a widening override
        # admits it, matching what the listing gate already accepted.
        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=SpecifierSet(">=3.13"),
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        provider = self._build_into(
            monkeypatch,
            built,
            overrides=(pkg_override("pkg", requires_python=">=3.9"),),
        )
        result = build_remote.build_remote_sdist(provider, "pkg", V("1.0"))
        assert result is built

    def test_override_narrows_requires_python_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The built value admits the 3.12 target; a narrowing override
        # excludes it, so the build is rejected.
        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=SpecifierSet(">=3.10"),
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        provider = self._build_into(
            monkeypatch,
            built,
            overrides=(pkg_override("pkg", requires_python=">=3.13"),),
        )
        with pytest.raises(UnsupportedSdistError, match="requires Python"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_built_name_mismatch_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wrong = WheelMetadata(
            name="other-pkg",
            version=V("1.0"),
            requires_python=None,
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        provider = self._build_into(monkeypatch, wrong)
        with pytest.raises(UnsupportedSdistError, match="does not match"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_built_version_mismatch_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wrong = WheelMetadata(
            name="pkg",
            version=V("2.0"),
            requires_python=None,
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        provider = self._build_into(monkeypatch, wrong)
        with pytest.raises(UnsupportedSdistError, match="does not match"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))


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


class TestComputeTier:
    """Cover the tier-decision branches."""

    def test_force_backtracked_returns_culprit(self) -> None:
        from nab_python._provider.priority import (
            TIER_CULPRIT,
            compute_tier,
        )

        tier = compute_tier(
            "foo",
            affected_count=0,
            culprit_count=0,
            culprit_counts=None,
            force_backtracked=True,
        )
        assert tier == TIER_CULPRIT


class TestExtrasInvalidMetadata:
    """Cover the invalid-metadata skip paths in extras pick mode."""

    BAD_META = (
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar (>=1.*)\n"
    )

    def test_skip_when_invalid_metadata_cached(self) -> None:
        """Cached invalid metadata short-circuits the candidate loop."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": EXTRA_METADATA, "1.0": EXTRA_METADATA},
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312, extras_mode=ExtrasMode.WARN)
        provider._invalid_metadata[("foo", V("2.0"))] = "stub"
        version = provider.choose_version("foo[security]", VersionRange.full())
        assert version == V("1.0")

    def test_skip_when_get_dependencies_marks_invalid(self) -> None:
        """Parseable but rejected metadata fetched mid-loop skips the candidate."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": self.BAD_META, "1.0": EXTRA_METADATA},
            package="foo",
        )
        provider = Provider(coordinator, target=_PY312, extras_mode=ExtrasMode.WARN)
        version = provider.choose_version("foo[security]", VersionRange.full())
        assert version == V("1.0")

    def test_user_extra_skips_invalid_metadata(self) -> None:
        """A user-requested extra must not pick a known-invalid version.

        get_dependencies raises MetadataError for a version in
        _invalid_metadata, so returning 2.0 here crashes when the proxy
        later fetches its dependencies.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": EXTRA_METADATA, "1.0": EXTRA_METADATA},
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            extras_mode=ExtrasMode.WARN,
            root_extras={("foo", "security")},
        )
        provider._invalid_metadata[("foo", V("2.0"))] = "stub"
        version = provider.choose_version("foo[security]", VersionRange.full())
        assert version == V("1.0")

    def test_user_extra_skips_unsupported_sdist(self) -> None:
        """A user-requested extra must not pick an unbuildable sdist version.

        2.0 is sdist-only with a pre-2.2 PKG-INFO, so its metadata
        cannot be extracted under the default BUILD_LOCAL policy.
        The base package's look-ahead skips it; the user-extra path
        must skip it too instead of letting the resolver's later
        dependency fetch raise UnsupportedSdistError.
        """
        dists = [make_sdist("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            dists,
            metadata_by_version={"1.0": EXTRA_METADATA},
            sdist_pkg_info=PRE_22_SDIST_PKG_INFO,
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_extras={("foo", "security")},
        )
        version = provider.choose_version("foo[security]", VersionRange.full())
        assert version == V("1.0")

    def test_user_extra_skips_when_get_dependencies_marks_invalid(self) -> None:
        """A user-requested extra skips metadata rejected mid-loop.

        The invalid metadata for 2.0 is not cached before the pick, so
        only the fetch inside the candidate loop can reveal it.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": self.BAD_META, "1.0": EXTRA_METADATA},
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            extras_mode=ExtrasMode.WARN,
            root_extras={("foo", "security")},
        )
        version = provider.choose_version("foo[security]", VersionRange.full())
        assert version == V("1.0")

    def test_user_extra_skips_version_with_no_metadata_source(self) -> None:
        """A user extra skips a version with no PEP 658 metadata and no sdist.

        resolve_metadata raises a generic MetadataError before
        get_dependencies records the version, so has_invalid_metadata stays
        False. The pick must still skip 2.0 and choose 1.0.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"1.0": EXTRA_METADATA},
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_extras={("foo", "security")},
        )
        version = provider.choose_version("foo[security]", VersionRange.full())
        assert version == V("1.0")

    def test_user_extra_no_metadata_resolves_end_to_end(self) -> None:
        """A user extra whose top version lacks metadata still resolves.

        The extras proxy sorts before its base, so pkg[feature] is decided
        first. With 2.0 unreadable, the proxy must fall to 1.0.
        """
        metadata = (
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nProvides-Extra: feature\n"
        )
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={"1.0": metadata},
            package="pkg",
        )
        root_reqs = {
            "pkg": VersionRange.full(admit_arbitrary=False),
            "pkg[feature]": VersionRange.full(admit_arbitrary=False),
        }
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements=root_reqs,
            root_extras={("pkg", "feature")},
        )
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        pins = resolver.resolve(root_reqs)
        assert pins["pkg"] == V("1.0")


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


class TestConsultedMarkers:
    """The lock's ``environments`` declaration is built from the dependency
    markers the resolve read, so the provider has to record every marker it
    evaluates.
    """

    @staticmethod
    def _coordinator(requires_dist: str) -> MagicMock:
        return make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                f"Requires-Dist: {requires_dist}\n"
            ),
            package="foo",
        )

    def test_an_evaluated_dependency_marker_is_recorded(self) -> None:
        provider = Provider(
            self._coordinator('colorama ; platform_system == "Windows"'),
            target=_PY312,
        )
        provider.get_dependencies("foo", V("1.0"))
        assert provider.consulted_markers == {Marker('platform_system == "Windows"')}

    def test_a_marker_that_holds_is_recorded_too(self) -> None:
        """A True marker keeps its dep, so it still gated the resolve."""
        provider = Provider(
            self._coordinator('bar ; sys_platform != "win32"'), target=_PY312
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps
        assert provider.consulted_markers == {Marker('sys_platform != "win32"')}

    def test_an_unmarked_dependency_records_nothing(self) -> None:
        provider = Provider(self._coordinator("bar"), target=_PY312)
        provider.get_dependencies("foo", V("1.0"))
        assert provider.consulted_markers == set()


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
