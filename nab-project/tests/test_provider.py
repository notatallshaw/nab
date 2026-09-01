"""Tests for Provider with mocked network access."""

from __future__ import annotations

import asyncio
import errno
import gc
import hashlib
import io
import json
import logging
import sys
import tarfile
import threading
import weakref
import zipfile
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, NoReturn
from unittest.mock import patch

import httpx
import pytest
import respx

from nab_index.cache import CachePolicy, OnDiskCache
from nab_index.client import (
    MalformedSimpleResponseError,
    MetadataHashMismatchError,
    SdistFile,
    SdistHashMismatchError,
    WheelFile,
    _parse_files,
)
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.lazy_wheel import RangeMetadataResult, RangeOutcome
from nab_index.local_index import LocalIndexClient, UnreadableLocalIndexError
from nab_index.multi_index import IndexConfig
from nab_index.transport import HttpError
from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_project import _build_remote
from nab_project._resolve.engine import _raise_for_source_python
from nab_project._testing.coordinator_fake import FakeFetchPort, make_coordinator
from nab_project.fetch import (
    DEFAULT_INDEX_URL,
    FetchCoordinator,
    IndexRoute,
)
from nab_project.inputs import ResolveInputs
from nab_project.resolve import build_resolver_inputs, resolve_with_coordinator
from nab_provider._provider import build_remote, metadata_resolver
from nab_provider._provider import listing as listing_mod
from nab_provider._provider import listing_diagnosis as diagnosis_mod
from nab_provider._provider.lookahead import DepRangeUnion
from nab_provider._provider.metadata_resolver import (
    TargetDepSignature,
    add_classified_dep,
    cache_deps_from_metadata,
    classify_requirement,
    pick_dist_for_metadata,
    target_dep_signature,
)
from nab_provider._vendor.packaging.markers import Marker
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.tags import Tag
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import InvalidVersion, Version
from nab_provider.errors import OverrideConflictError
from nab_provider.marker_holds import UnevaluableMarkerError, dependency_marker_holds
from nab_provider.metadata import WheelMetadata
from nab_provider.overrides import IndexOverride, PackageOverride
from nab_provider.provider import (
    BuildPolicy,
    DistPolicy,
    ExtrasMode,
    ForeignMetadataError,
    IncompatiblePythonError,
    InvalidUploadTimeError,
    LocalSource,
    MetadataError,
    MissingExtraError,
    Provider,
    ResolutionStrategy,
    SiblingMetadataDivergenceError,
    SourceNameMismatchError,
    UnsupportedSdistError,
    UnsupportedVcsError,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import pkg_override
from nab_resolver.errors import ResolutionError
from nab_resolver.resolver import Resolver
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    Term,
)

V = Version

# Excludes nothing. Used where the walk-ahead's range is not what a test checks.
_ANY_VERSION = VersionRange.full()

# The target most tests resolve against: the host machine, impersonating
# CPython 3.12.0.  Shared because it is frozen and building one walks the
# host's whole tag set.
_PY312 = ResolveTarget.for_host_python("3.12.0")

# A declared linux/3.11 target: host-independent tags for the tie tests.
_LINUX311 = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)


def short_reason(provider: Provider, package: str) -> str | None:
    """Return the one line ``package``'s diagnostic prints at default verbosity."""
    diagnostic = provider.get_no_versions_reason(package)
    return None if diagnostic is None else diagnostic.short


def rendered_reason(provider: Provider, package: str) -> str:
    """Return ``package``'s entry as one string: its line, then what ``-v`` adds."""
    diagnostic = provider.get_no_versions_reason(package)
    assert diagnostic is not None
    return "\n".join((diagnostic.short, *diagnostic.detail))


def metadata_blocks(**blocks: str) -> dict[Version, diagnosis_mod.MetadataBlock]:
    """Build the per-version metadata failures a ban records."""
    return {
        V(version): diagnosis_mod.MetadataBlock(message)
        for version, message in blocks.items()
    }


def make_wheel(
    version: str = "1.0",
    requires_python: str | None = None,
    has_metadata: bool = True,
    upload_time: str | None = None,
    local_path: Path | None = None,
    metadata_hash: tuple[str, str] | None = None,
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
        metadata_hash=metadata_hash,
    )


def make_metadata(name: str, version: str, *reqs: str) -> str:
    """Minimal METADATA text carrying the given Requires-Dist lines."""
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [f"Requires-Dist: {r}" for r in reqs]
    return "\n".join(lines) + "\n"


def make_tagged_wheel(tag: str, *, has_metadata: bool = True) -> WheelFile:
    """Build a 1.0 WheelFile carrying an explicit PEP 425 tag."""
    filename = f"pkg-1.0-{tag}.whl"
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version="1.0",
        requires_python=None,
        has_metadata=has_metadata,
        upload_time=None,
    )


def make_sdist(
    version: str = "1.0",
    requires_python: str | None = None,
    upload_time: str | None = None,
    local_path: Path | None = None,
    hashes: tuple[tuple[str, str], ...] = (),
) -> SdistFile:
    """Build a SdistFile for testing."""
    return SdistFile(
        filename=f"pkg-{version}.tar.gz",
        url=f"https://example.com/pkg-{version}.tar.gz",
        version=version,
        requires_python=requires_python,
        upload_time=upload_time,
        local_path=local_path,
        hashes=hashes,
    )


def _blocker_reason(
    *blockers: diagnosis_mod.Blocker,
) -> diagnosis_mod.NoVersionsReason:
    """A look-ahead marker, the reason tests seed when one must name a cause."""
    return diagnosis_mod.NoVersionsReason(
        diagnosis_mod.ReasonKind.BLOCKERS, blockers=blockers
    )


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


def _done_event() -> threading.Event:
    """Return an already-set Event."""
    ev = threading.Event()
    ev.set()
    return ev


def _write_local_wheel(path: Path, name: str, version: str) -> None:
    """Write a wheel at ``path`` carrying nothing but its own METADATA."""
    with zipfile.ZipFile(path, "w") as zf:
        member = f"{name}-{version}.dist-info/METADATA"
        zf.writestr(member, make_metadata(name, version))


def _write_corrupt_bz2_wheel(path: Path, name: str, version: str) -> None:
    """Write a wheel whose bz2-compressed METADATA holds a corrupt stream.

    The central directory stays intact, so the archive opens and lists its
    members; only the member's bz2 magic is broken, which bz2 reports as a
    plain OSError rather than an error type of its own.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_BZIP2) as zf:
        member = f"{name}-{version}.dist-info/METADATA"
        zf.writestr(member, make_metadata(name, version))
    path.write_bytes(path.read_bytes().replace(b"BZh", b"BZx", 1))


def _deny_zip_open(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Make opening ``target`` as a zip fail with EACCES.

    A real chmod would not do: root ignores the mode bits and Windows has none.
    """
    original = zipfile.ZipFile

    def denied(file: Any, *args: Any, **kwargs: Any) -> zipfile.ZipFile:
        if file == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return original(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", denied)


def _make_sdist_targz(body: bytes = b"[project]\nname = 'pkg'\n") -> bytes:
    """Return .tar.gz bytes for a one-file sdist rooted at pkg-1.0."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="pkg-1.0/pyproject.toml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


_SDIST_TARGZ = _make_sdist_targz()

# Dynamic deps take the PEP 517 build path, and the build requirement gives
# the build env something to fetch.
_DYNAMIC_SDIST_TARGZ = _make_sdist_targz(
    b'[project]\nname = "pkg"\nversion = "1.0"\ndynamic = ["dependencies"]\n\n'
    b'[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
)

# Extraction requires the tar data filter (PEP 706), so skip the paths that
# actually extract on a Python that lacks it (before 3.10.12 / 3.11.4 / 3.12).
requires_data_filter = pytest.mark.skipif(
    not hasattr(tarfile, "data_filter"),
    reason="sdist extraction requires the tar data filter (PEP 706)",
)


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

    def test_skips_unparseable_version_under_overrides(self) -> None:
        """An override does not rescue a version that does not parse.

        The override is what selects the version-scoped pass, which still
        drops an unparseable version before consulting anything.
        """
        coordinator = make_coordinator([make_wheel("not-a-version!")], package="foo")
        provider = Provider(
            coordinator,
            package_overrides=(pkg_override("foo", provides_extra=("e",)),),
        )
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

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param(_PY312, id="minor-interval"),
            pytest.param(ResolveTarget.for_host_python("3.12.4"), id="whole-target"),
        ],
    )
    def test_oversized_requires_python_admitted_without_crash(
        self, target: ResolveTarget
    ) -> None:
        """An oversized requires-python admits the wheel instead of crashing.

        The entry is a string, so it passes the parser's isinstance guard and
        SpecifierSet accepts it; the ValueError only lands when the target
        compares against it, where the listing catches it and keeps the wheel.
        """
        oversized = ">=" + "1" * (sys.get_int_max_str_digits() + 1)
        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://example.com/foo/foo-1.0-py3-none-any.whl",
                    "requires-python": oversized,
                }
            ]
        }

        files = _parse_files(data, "https://example.com/", "foo")
        coordinator = make_coordinator(files, package="foo")
        provider = Provider(coordinator, target=target)

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


class _CountingListing(list[Version]):
    """A version listing that counts how many of its entries are read."""

    def __init__(self, versions: list[Version]) -> None:
        super().__init__(versions)
        self.reads = 0

    def __getitem__(self, index: Any) -> Any:
        self.reads += 1
        return super().__getitem__(index)


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

        # filter's sortedness check reads both ends; the pick is the third read.
        assert listing.reads == 3

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


class TestNoVersionsReasons:
    """``_record_no_versions_reason`` captures provider-side hints."""

    def test_empty_listing_records_not_found(self) -> None:
        """An empty listing surfaces ``not found on any configured index``."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider.choose_version("foo", SpecifierSet("").to_range())
        assert (
            short_reason(provider, "foo") == "package not found on any configured index"
        )

    @pytest.mark.parametrize(
        ("indexes", "routes"),
        [
            pytest.param(None, None, id="single-index"),
            pytest.param(
                [
                    IndexConfig("pypi", DEFAULT_INDEX_URL),
                    IndexConfig("extra", "https://extra.example/simple/"),
                ],
                None,
                id="two-indexes",
            ),
            pytest.param(None, [IndexRoute("other", "pypi")], id="one-index-one-route"),
        ],
    )
    def test_offline_cold_cache_miss_reports_offline_not_absence(
        self,
        tmp_path: Path,
        indexes: list[IndexConfig] | None,
        routes: list[IndexRoute] | None,
    ) -> None:
        """An offline miss is reported as offline rather than absence.

        The parameters cover each client shape: a second index or any route
        swaps the lone client for the multi-index router.
        """
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
            indexes=indexes,
            index_routes=routes,
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())
        diagnostic = provider.get_no_versions_reason("foo")
        assert diagnostic is not None
        assert diagnostic.short == (
            "offline mode skipped an index with no cached listing"
        )
        # offline is layered, so the instruction is the one that wins over
        # every layer rather than the flag this run may not have passed.
        assert diagnostic.remedy == "re-run with --no-offline"
        assert diagnostic.detail == (
            (
                "note: --no-offline turns offline mode off for this run"
                " wherever it was set: a nab.toml, NAB_OFFLINE, or this"
                " command line"
            ),
        )

    def test_offline_with_cached_absence_still_reports_not_found(
        self, tmp_path: Path
    ) -> None:
        """Offline over a cached 404 still names absence: an index did answer."""
        OnDiskCache(tmp_path, DEFAULT_INDEX_URL).put_negative(
            "foo", CachePolicy(fetched_at=0, max_age=1, etag=None)
        )
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())
        assert (
            short_reason(provider, "foo") == "package not found on any configured index"
        )

    def test_route_to_an_index_without_the_package_names_that_index(
        self, tmp_path: Path
    ) -> None:
        """A pinned package missing from its index is not missing everywhere.

        The route is why the index holding ``foo`` was never asked, so the
        line has to name the one index that was.
        """
        public = tmp_path / "pypi"
        public.mkdir()
        (public / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        internal = tmp_path / "internal"
        internal.mkdir()

        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            indexes=[
                IndexConfig("pypi", public.as_uri()),
                IndexConfig("internal", internal.as_uri()),
            ],
            index_routes=[IndexRoute("foo", "internal")],
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())

        assert rendered_reason(provider, "foo") == (
            "not found on index 'internal', the only index this package is routed to"
        )

    def test_route_over_a_lone_index_still_names_absence(self, tmp_path: Path) -> None:
        """A route that holds no index back leaves the absence line alone."""
        internal = tmp_path / "internal"
        internal.mkdir()
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            indexes=[IndexConfig("internal", internal.as_uri())],
            index_routes=[IndexRoute("foo", "internal")],
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())

        assert (
            short_reason(provider, "foo") == "package not found on any configured index"
        )

    def test_yanked_page_on_the_routed_index_still_names_the_yank(
        self, tmp_path: Path
    ) -> None:
        """The route is the weakest answer: a page that says more wins."""
        body = json.dumps(
            {
                "files": [
                    {
                        "filename": "foo-1.0-py3-none-any.whl",
                        "url": "https://internal.example/foo-1.0-py3-none-any.whl",
                        "yanked": True,
                    }
                ]
            }
        ).encode()
        stale = CachePolicy(fetched_at=0, max_age=1, etag=None)
        OnDiskCache(tmp_path, "https://internal.example/simple/").put_simple(
            "foo", body, stale
        )
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
            indexes=[
                IndexConfig("pypi", DEFAULT_INDEX_URL),
                IndexConfig("internal", "https://internal.example/simple/"),
            ],
            index_routes=[IndexRoute("foo", "internal")],
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())

        assert short_reason(provider, "foo") == (
            "the index lists this package but every file is yanked"
        )

    def test_all_yanked_listing_reports_the_yank_not_absence(
        self, tmp_path: Path
    ) -> None:
        """A page whose every file is yanked is not a package no index carries.

        :pep:`592` yanks never reach the listing, so the filter sees the same
        empty list an absent package gives it, and the two have to read
        differently: nothing the user can configure brings a yanked file back.
        """
        body = json.dumps(
            {
                "files": [
                    {
                        "filename": "foo-1.0-py3-none-any.whl",
                        "url": "https://pypi.example/foo-1.0-py3-none-any.whl",
                        "yanked": True,
                    },
                    {
                        "filename": "foo-2.0-py3-none-any.whl",
                        "url": "https://pypi.example/foo-2.0-py3-none-any.whl",
                        "yanked": "withdrawn",
                    },
                ]
            }
        ).encode()
        OnDiskCache(tmp_path, DEFAULT_INDEX_URL).put_simple(
            "foo", body, CachePolicy(fetched_at=0, max_age=1, etag=None)
        )
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())

        assert rendered_reason(provider, "foo") == (
            "the index lists this package but every file is yanked"
        )

    def test_a_yanked_file_beside_a_readable_one_is_not_an_all_yanked_page(
        self, tmp_path: Path
    ) -> None:
        """One admitted file leaves the page ordinary, whatever else was yanked.

        The listing here is emptied by the ``.zip``, not by the yank, so the
        line has to name the format rather than the withdrawal.
        """
        body = json.dumps(
            {
                "files": [
                    {
                        "filename": "foo-1.0-py3-none-any.whl",
                        "url": "https://pypi.example/foo-1.0-py3-none-any.whl",
                        "yanked": True,
                    },
                    {
                        "filename": "foo-2.0.zip",
                        "url": "https://pypi.example/foo-2.0.zip",
                    },
                ]
            }
        ).encode()
        OnDiskCache(tmp_path, DEFAULT_INDEX_URL).put_simple(
            "foo", body, CachePolicy(fetched_at=0, max_age=1, etag=None)
        )
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())

        assert short_reason(provider, "foo") == (
            "no file the index served is one nab can read"
        )

    def test_unreadable_formats_only_reports_format_not_absence(
        self, tmp_path: Path
    ) -> None:
        """A page of formats nab does not read is not reported as absence."""
        body = json.dumps(
            {
                "files": [
                    {
                        "filename": "foo-1.0.zip",
                        "url": "https://pypi.example/foo-1.0.zip",
                    },
                    {
                        "filename": "foo-1.0.win32.exe",
                        "url": "https://pypi.example/foo-1.0.win32.exe",
                    },
                ]
            }
        ).encode()
        OnDiskCache(tmp_path, DEFAULT_INDEX_URL).put_simple(
            "foo", body, CachePolicy(fetched_at=0, max_age=1, etag=None)
        )
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())
        assert short_reason(provider, "foo") == (
            "no file the index served is one nab can read"
        )

    def test_unreadable_formats_on_second_index_reports_format(
        self, tmp_path: Path
    ) -> None:
        """The router names the format when any walked index served one."""
        body = json.dumps(
            {"files": [{"filename": "foo-1.0.zip", "url": "https://e.example/f.zip"}]}
        ).encode()
        stale = CachePolicy(fetched_at=0, max_age=1, etag=None)
        OnDiskCache(tmp_path, DEFAULT_INDEX_URL).put_negative("foo", stale)
        OnDiskCache(tmp_path, "https://extra.example/simple/").put_simple(
            "foo", body, stale
        )
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
            indexes=[
                IndexConfig("pypi", DEFAULT_INDEX_URL),
                IndexConfig("extra", "https://extra.example/simple/"),
            ],
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())
        assert short_reason(provider, "foo") == (
            "no file the index served is one nab can read"
        )

    def test_local_wheelhouse_zip_only_reports_format_not_absence(
        self, tmp_path: Path
    ) -> None:
        """A find-links directory holding only a ``.zip`` sdist names the format."""
        (tmp_path / "foo-1.0.zip").write_bytes(b"")
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            indexes=[IndexConfig("local", tmp_path.as_uri())],
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())
        assert short_reason(provider, "foo") == (
            "no file the index served is one nab can read"
        )

    def test_local_wheelhouse_other_package_zip_still_reports_absence(
        self, tmp_path: Path
    ) -> None:
        """Another package's ``.zip`` in the directory does not name the format."""
        (tmp_path / "bar-1.0.zip").write_bytes(b"")
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            indexes=[IndexConfig("local", tmp_path.as_uri())],
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())
        assert (
            short_reason(provider, "foo") == "package not found on any configured index"
        )

    def test_local_pep503_page_zip_only_reports_format_not_absence(
        self, tmp_path: Path
    ) -> None:
        """A local PEP 503 page listing only a ``.zip`` names the format."""
        package_dir = tmp_path / "foo"
        package_dir.mkdir()
        (package_dir / "index.html").write_text(
            '<a href="foo-1.0.zip">foo-1.0.zip</a>', encoding="utf-8"
        )
        (package_dir / "foo-1.0.zip").write_bytes(b"")
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            indexes=[IndexConfig("local", tmp_path.as_uri())],
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())
        assert short_reason(provider, "foo") == (
            "no file the index served is one nab can read"
        )

    def test_local_pep503_page_of_unreachable_links_names_the_links(
        self, tmp_path: Path
    ) -> None:
        """The line names the links, not a missing package or a bad filename.

        The index carries a page for ``foo`` naming a wheel, so sending the
        reader to check the package name would be wrong.
        """
        package_dir = tmp_path / "foo"
        package_dir.mkdir()
        (package_dir / "index.html").write_text(
            '<a href="ftp://mirror.example/foo-1.0-py3-none-any.whl">foo-1.0</a>',
            encoding="utf-8",
        )
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            indexes=[IndexConfig("local", tmp_path.as_uri())],
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())
        assert rendered_reason(provider, "foo") == (
            "the index lists this package but nab cannot reach any of its links"
        )

    def test_remote_page_of_unreachable_links_names_the_links(
        self, tmp_path: Path
    ) -> None:
        """A remote page reads like the local one it mirrors, not like absence.

        The wheel sits behind a URL with an unterminated IPv6 bracket, so
        the entry is dropped and the page parses to nothing.
        """
        body = json.dumps(
            {
                "files": [
                    {
                        "filename": "foo-1.0-py3-none-any.whl",
                        "url": "https://[::1/foo-1.0-py3-none-any.whl",
                    }
                ]
            }
        ).encode()
        OnDiskCache(tmp_path, DEFAULT_INDEX_URL).put_simple(
            "foo", body, CachePolicy(fetched_at=0, max_age=1, etag=None)
        )

        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("foo", SpecifierSet("").to_range())

        assert rendered_reason(provider, "foo") == (
            "the index lists this package but nab cannot reach any of its links"
        )

    def test_remote_page_naming_another_project_is_not_absence(
        self, tmp_path: Path
    ) -> None:
        """A filename packaging parses to another project is not a missing package.

        ``cffi-1.0.2-2.tar.gz`` parses as project ``cffi-1-0-2`` at version
        ``2``, so the file is dropped while the index still lists the
        project.
        """
        body = json.dumps(
            {
                "files": [
                    {
                        "filename": "cffi-1.0.2-2.tar.gz",
                        "url": "https://pypi.example/cffi-1.0.2-2.tar.gz",
                    }
                ]
            }
        ).encode()
        OnDiskCache(tmp_path, DEFAULT_INDEX_URL).put_simple(
            "cffi", body, CachePolicy(fetched_at=0, max_age=1, etag=None)
        )

        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("cffi", SpecifierSet("").to_range())

        assert rendered_reason(provider, "cffi") == (
            "the index lists this package but nab can use none of the files it names"
        )

    def test_local_pep503_page_naming_another_project_is_not_absence(
        self, tmp_path: Path
    ) -> None:
        """The local scanner drops the same filename, so it reports the same line."""
        package_dir = tmp_path / "cffi"
        package_dir.mkdir()
        (package_dir / "index.html").write_text(
            '<a href="cffi-1.0.2-2.tar.gz">cffi-1.0.2-2.tar.gz</a>', encoding="utf-8"
        )
        (package_dir / "cffi-1.0.2-2.tar.gz").write_bytes(b"")

        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            indexes=[IndexConfig("local", tmp_path.as_uri())],
        ) as coordinator:
            provider = Provider(coordinator)
            provider.choose_version("cffi", SpecifierSet("").to_range())

        assert rendered_reason(provider, "cffi") == (
            "the index lists this package but nab can use none of the files it names"
        )

    def test_no_match_in_range_records_no_match_reason(self) -> None:
        """A non-empty listing with no in-range version surfaces a no-match reason."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        provider.choose_version("foo", SpecifierSet(">=5.0").to_range())
        assert short_reason(provider, "foo") == "no version matches the requirement"

    @pytest.mark.parametrize(
        ("listing", "build", "line"),
        [
            pytest.param(
                [make_wheel("2.0", requires_python=">=3.13"), make_wheel("1.0")],
                lambda coordinator: Provider(
                    coordinator, target=ResolveTarget.for_host_python("3.9.0")
                ),
                "no version in range supports Python 3.9",
                id="requires-python",
            ),
            pytest.param(
                [
                    WheelFile(
                        filename="pkg-2.0-cp311-cp311-win_amd64.whl",
                        url="https://example.com/pkg-2.0-cp311-cp311-win_amd64.whl",
                        version="2.0",
                        requires_python=None,
                        has_metadata=True,
                        upload_time=None,
                    ),
                    make_wheel("1.0"),
                ],
                lambda coordinator: Provider(coordinator, target=_LINUX311),
                "no wheel in range matches this platform or Python",
                id="wheel-tags",
            ),
            pytest.param(
                [make_sdist("2.0"), make_wheel("1.0")],
                lambda coordinator: Provider(
                    coordinator, dist_policy=DistPolicy.WHEEL_ONLY
                ),
                'dist-policy = "wheel-only" excluded every version in range',
                id="dist-policy",
            ),
            pytest.param(
                [
                    make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
                    make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
                ],
                lambda coordinator: Provider(
                    coordinator,
                    uploaded_prior_to=datetime(2024, 3, 1, tzinfo=timezone.utc),
                ),
                "uploaded-prior-to excluded every version in range",
                id="upload-time",
            ),
        ],
    )
    def test_filtered_in_range_release_reports_the_filter(
        self,
        listing: list[WheelFile | SdistFile],
        build: Callable[[FakeFetchPort], Provider],
        line: str,
    ) -> None:
        """An in-range release a filter dropped names the filter that dropped it.

        Each listing keeps an out-of-range 1.0, so the package still has a
        surviving version and only the 2.0 the requirement asks for was
        filtered away.  Four listings, four different lines: the two rungs a
        config key turns on name that key, and the two that answer to no key
        name the target they judged against.
        """
        coordinator = make_coordinator(listing, package="foo")
        provider = build(coordinator)
        assert provider.choose_version("foo", SpecifierSet(">=2").to_range()) is None
        assert short_reason(provider, "foo") == line

    def test_unparseable_listing_version_is_not_read_as_a_filtered_match(self) -> None:
        """A version nab cannot parse cannot be the in-range release."""
        unparseable = WheelFile(
            filename="pkg-not-a-version-py3-none-any.whl",
            url="https://example.com/pkg-not-a-version-py3-none-any.whl",
            version="not-a-version",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )
        coordinator = make_coordinator([unparseable, make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        assert provider.choose_version("foo", SpecifierSet(">=2").to_range()) is None
        assert short_reason(provider, "foo") == "no version matches the requirement"

    def test_another_spelling_of_a_kept_version_is_not_a_filtered_match(self) -> None:
        """A release that survived under another spelling was not filtered.

        The wheel's ``1.0`` and the sdist's ``1.0.0`` are one release, which
        the listing collapses onto the ``1.0`` representative.  ``===``
        compares the string, so ``1.0.0`` falls in range while the kept
        representative does not, and no filter dropped anything.
        """
        coordinator = make_coordinator(
            [make_wheel("1.0"), make_sdist("1.0.0")], package="foo"
        )
        provider = Provider(coordinator)
        assert (
            provider.choose_version("foo", SpecifierSet("===1.0.0").to_range()) is None
        )
        assert short_reason(provider, "foo") == "no version matches the requirement"

    def test_local_source_out_of_range_reports_no_match(self, tmp_path: Path) -> None:
        """A local source has no index listing to have filtered anything."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.2.3"\n', encoding="utf-8"
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator, local_sources=[LocalSource("foo", str(tmp_path))]
        )
        assert provider.choose_version("foo", SpecifierSet(">=5").to_range()) is None
        assert short_reason(provider, "foo") == "no version matches the requirement"

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
        assert rendered_reason(provider, "foo") == (
            "no file supports Python 3.9"
            "\nrequires-python excluded 3 files (newest: 3.0 requires >=3.13,"
            " the resolve targets Python 3.9)"
            "\nthe files nab read hold no sdist to build from"
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
        assert rendered_reason(provider, "foo") == (
            "no wheel matches this platform or Python"
            "\nno wheel's tags are compatible with the resolve target"
            " (2 wheels rejected)"
            "\nthe files nab read hold no sdist to build from"
        )

    def test_tag_rejected_wheel_does_not_hijack_requires_python_reason(self) -> None:
        """A tag-rejected wheel must not mask a requires-python exclusion.

        ``foo`` 2.0 ships only a Windows wheel the Linux target refuses,
        and ``foo`` 1.0 ships a ``py3-none-any`` wheel the target could
        install but for its ``requires-python``.  The wheel-tag tally is
        package-global, so the 2.0 rejection must not make the reason
        blame wheel tags and claim no sdist when 1.0 was really dropped
        by requires-python.
        """
        listing = [
            WheelFile(
                filename="foo-2.0-cp311-cp311-win_amd64.whl",
                url="https://example.com/foo-2.0-cp311-cp311-win_amd64.whl",
                version="2.0",
                requires_python=None,
                has_metadata=True,
                upload_time=None,
            ),
            make_wheel("1.0", requires_python=">=3.13"),
        ]
        coordinator = make_coordinator(listing, package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_declared(
                python_version="3.11", spec=PlatformSpec("linux_x86_64")
            ),
        )
        provider.choose_version("foo", SpecifierSet("").to_range())
        assert rendered_reason(provider, "foo") == (
            "requires-python and wheel tags excluded every file"
            "\nrequires-python excluded 1 file (1.0 requires >=3.13, the resolve"
            " targets Python 3.11)"
            "\nno wheel's tags are compatible with the resolve target"
            " (1 wheel rejected)"
            "\nthe files nab read hold no sdist to build from"
        )

    def test_tag_rejected_wheel_does_not_deny_a_filtered_sdist(self) -> None:
        """A tag-rejected wheel must not deny an sdist that was filtered.

        ``foo`` 2.0 ships only a Windows wheel the Linux target refuses,
        and ``foo`` 1.0 ships an sdist dropped by its ``requires-python``.
        The reason must not claim "no sdist is available" when one is on
        the index: it names the rejected tags and the filtered sdist.
        """
        listing = [
            WheelFile(
                filename="foo-2.0-cp311-cp311-win_amd64.whl",
                url="https://example.com/foo-2.0-cp311-cp311-win_amd64.whl",
                version="2.0",
                requires_python=None,
                has_metadata=True,
                upload_time=None,
            ),
            make_sdist("1.0", requires_python=">=3.13"),
        ]
        coordinator = make_coordinator(listing, package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_declared(
                python_version="3.11", spec=PlatformSpec("linux_x86_64")
            ),
        )
        provider.choose_version("foo", SpecifierSet("").to_range())
        assert rendered_reason(provider, "foo") == (
            "requires-python and wheel tags excluded every file"
            "\nrequires-python excluded 1 file (1.0 requires >=3.13, the resolve"
            " targets Python 3.11)"
            "\nno wheel's tags are compatible with the resolve target"
            " (1 wheel rejected)"
        )

    def test_present_but_dist_policy_filtered_reports_incompatible(self) -> None:
        """A dist-policy-filtered package reports incompatible, not absent."""
        coordinator = make_coordinator([make_sdist("1.0")], package="foo")
        provider = Provider(coordinator, dist_policy=DistPolicy.WHEEL_ONLY)
        provider.choose_version("foo", SpecifierSet("").to_range())
        assert rendered_reason(provider, "foo") == (
            'dist-policy = "wheel-only" excluded every file'
            '\ndist-policy = "wheel-only" excluded 1 sdist (1.0)'
            "\nnote: the project-level dist-policy set that policy; setting"
            ' packages."foo".dist-policy = "wheel-or-sdist" admits both formats'
            " for this package"
        )

    def test_tag_excluded_wheel_with_filtered_sdist_keeps_the_sdist(self) -> None:
        """A tag-excluded wheel beside a filtered sdist must not deny the sdist.

        The sdist is on the index, dropped only by requires-python, so the
        reason may not claim ``the files nab read hold no sdist to build from``: it has
        to read present-but-filtered like the no-tag-excluded branch does.
        """
        wheel = WheelFile(
            filename="foo-1.0-cp311-cp311-win_amd64.whl",
            url="https://example.com/foo-1.0-cp311-cp311-win_amd64.whl",
            version="1.0",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )
        sdist = SdistFile(
            filename="foo-1.0.tar.gz",
            url="https://example.com/foo-1.0.tar.gz",
            version="1.0",
            requires_python=">=3.13",
            upload_time=None,
        )
        coordinator = make_coordinator([wheel, sdist], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_declared(
                python_version="3.11", spec=PlatformSpec("linux_x86_64")
            ),
        )
        provider.choose_version("foo", SpecifierSet("").to_range())
        reason = rendered_reason(provider, "foo")
        assert "no sdist is available" not in reason
        assert reason == (
            "requires-python and wheel tags excluded every file"
            "\nrequires-python excluded 1 file (1.0 requires >=3.13, the resolve"
            " targets Python 3.11)"
            "\nno wheel's tags are compatible with the resolve target"
            " (1 wheel rejected)"
        )

    def test_get_reason_returns_none_for_unknown_package(self) -> None:
        """Packages without a recorded reason return ``None``."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        assert short_reason(provider, "never-asked") is None

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
        reason = rendered_reason(provider, "foo")
        assert "bar" in reason
        assert reason.startswith("every version in range needs bar in ==2.0")

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

        assert rendered_reason(provider, "foo") == (
            "every version in range needs bar in ==2.0, but the resolve chose bar 1.0"
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
            sdist_pkg_info_by_version={
                "1.0": PKG_INFO_DYNAMIC_DEPS,
                "2.0": PKG_INFO_DYNAMIC_DEPS.replace("Version: 1.0", "Version: 2.0"),
            },
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
        reason = rendered_reason(provider, "pkg")
        assert reason.startswith("every version in range was rejected on its metadata")
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
        # four pending stores so all the filter branches fire.
        provider.pending_blocks[("other", "bar", V("1.0"))].append(V("1.0"))
        provider.pending_range_blocks[
            ("other", "baz", SpecifierSet("<2.0").to_range())
        ].append(V("1.0"))
        provider.pending_self_blocks[
            (
                "other",
                SpecifierSet("==5.0").to_range(),
                SpecifierSet(">=6.0").to_range(),
            )
        ].append(V("1.0"))
        provider.pending_root_blocks[
            (
                "other",
                "qux",
                SpecifierSet("==2.0").to_range(),
                SpecifierSet("==1.0").to_range(),
            )
        ].append(V("1.0"))
        assert provider._capture_lookahead_blockers("foo") == ([], ())

    def test_metadata_ban_loses_to_a_reason_that_names_a_cause(self) -> None:
        """A blocker line names a cause, so it outranks the metadata summary."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider._no_versions_reasons["foo"] = _blocker_reason(
            diagnosis_mod.Blocker(
                diagnosis_mod.BlockerKind.HELD,
                "bar",
                (SpecifierSet("==2.0").to_range(),),
                SpecifierSet("<2.0").to_range(),
            )
        )

        provider.record_metadata_ban(
            "foo", metadata_blocks(**{"1.0": "No metadata for foo==1.0"})
        )

        assert short_reason(provider, "foo") == (
            "every version in range needs bar in ==2.0, but the resolve holds bar in <2.0"
        )

    @pytest.mark.parametrize(
        "recorded",
        [
            pytest.param(
                diagnosis_mod.NoVersionsReason(diagnosis_mod.ReasonKind.NO_MATCH),
                id="no-match",
            ),
            pytest.param(
                diagnosis_mod.NoVersionsReason(
                    diagnosis_mod.ReasonKind.NO_MATCH,
                    version_range=SpecifierSet(">=2").to_range(),
                ),
                id="filtered-in-range",
            ),
        ],
    )
    def test_metadata_ban_outranks_a_generic_reason(
        self, recorded: diagnosis_mod.NoVersionsReason
    ) -> None:
        """The ban wins over the lines that say only that nothing matched.

        A sibling requirement over a range the ban emptied records one of
        these first, so arrival order alone would bury the metadata cause.
        """
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider._no_versions_reasons["foo"] = recorded

        provider.record_metadata_ban(
            "foo", metadata_blocks(**{"1.0": "No metadata for foo==1.0"})
        )

        assert short_reason(provider, "foo") == (
            "every version in range was rejected on its metadata"
        )

    def test_metadata_ban_unions_the_blocks_of_every_scan(self) -> None:
        """Versions banned by a later scan are counted with the earlier ones."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)

        provider.record_metadata_ban(
            "foo", metadata_blocks(**{"5.0": "No metadata for foo==5.0"})
        )
        provider.record_metadata_ban(
            "foo", metadata_blocks(**{"3.0": "No metadata for foo==3.0"})
        )

        assert rendered_reason(provider, "foo") == (
            "every version in range was rejected on its metadata"
            "\nNo metadata for foo==5.0"
            "\nNo metadata for foo==3.0"
        )

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
        reason = rendered_reason(provider, "foo")
        assert "<VersionRange" not in reason
        assert "needs bar in ==2.0, but your project requires bar ==1.0" in reason

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
        provider.solution_ranges["bar"] = pos_range
        result = provider.choose_version("foo", VersionRange.full())
        assert result is None
        reason = rendered_reason(provider, "foo")
        # foo requires bar==2.0; the message must name that, not the solution range.
        assert "<VersionRange" not in reason
        assert "AFTER_LOCALS" not in reason
        assert "needs bar in ==2.0, but the resolve holds bar in <2.0" in reason
        assert "disjoint with current solution range" not in reason

    def test_a_rejected_scan_spells_no_range_until_the_report(self) -> None:
        """A capture keeps the ranges; the report is where they get spelled.

        An ask that returns nothing is ordinary backtracking, so a resolve
        that goes on to succeed reads none of what the scan recorded.  The
        three ``foo`` versions are rejected down the three capture paths, so
        the count covers all of them.
        """

        class CountsSpelledRanges(Provider):
            spelled = 0

            def format_range(self, constraint: RangeProtocol[Version]) -> str:
                self.spelled += 1
                return super().format_range(constraint)

        coordinator = make_coordinator(
            listings={"foo": [make_wheel(v) for v in ("1.0", "2.0", "3.0")]},
            metadata_by_version={
                "1.0": make_metadata("foo", "1.0", "qux==2.0"),
                "2.0": make_metadata("foo", "2.0", "baz==2.0"),
                "3.0": make_metadata("foo", "3.0", "bar==2.0"),
            },
        )
        provider = CountsSpelledRanges(
            coordinator,
            target=_PY312,
            root_requirements={
                "foo": VersionRange.full(admit_arbitrary=False),
                "bar": SpecifierSet("==1.0").to_range(),
            },
        )
        provider.solution_decisions["baz"] = V("1.0")
        provider.solution_ranges["qux"] = SpecifierSet("<2.0").to_range()

        assert provider.choose_version("foo", VersionRange.full()) is None
        assert provider.spelled == 0

        reason = rendered_reason(provider, "foo")
        assert "needs bar in ==2.0, but your project requires bar ==1.0" in reason
        assert "needs baz in ==2.0, but the resolve chose baz 1.0" in reason
        assert "needs qux in ==2.0, but the resolve holds qux in <2.0" in reason

        # One declared range each, plus a held range for the two blockers
        # standing in one; the decided blocker's side is a version.
        assert provider.spelled == 5

    @pytest.mark.parametrize("cache_listing", [True, False])
    def test_range_block_rejection_spells_each_declared_range(
        self, cache_listing: bool
    ) -> None:
        """Candidates that disagree on the blocker still read as requirements.

        ``foo`` 2.0 requires ``bar==3.0`` and 1.0 requires ``bar==1.0``, a
        union no specifier set spells, so the line states the two pins one by
        one.  Neither pin names a version ``bar`` publishes, so its listing
        cannot supply a spelling whether or not it is cached.
        """
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("1.0"), make_wheel("2.0")],
                "bar": [make_wheel(version) for version in ("5.0", "6.0")],
            },
            metadata_by_version={
                "1.0": make_metadata("foo", "1.0", "bar==1.0"),
                "2.0": make_metadata("foo", "2.0", "bar==3.0"),
            },
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
        )
        if cache_listing:
            provider.fetch_versions("bar")
        provider.solution_ranges["bar"] = SpecifierSet("==5.0").to_range()

        assert provider.choose_version("foo", VersionRange.full()) is None

        reason = rendered_reason(provider, "foo")
        assert "<VersionRange" not in reason
        assert "AFTER_LOCALS" not in reason
        assert (
            "needs bar in ==3.0 or ==1.0, but the resolve holds bar in ==5.0" in reason
        )

    def test_declared_pins_covering_the_listing_keep_their_spelling(self) -> None:
        """Pins that between them cover every listed version stay on the line.

        ``bar`` publishes only the two versions ``foo`` pins, so spelling
        their union over that listing would name every version and leave no
        requirement in the sentence.
        """
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("1.0"), make_wheel("2.0")],
                "bar": [make_wheel("1.0"), make_wheel("3.0")],
            },
            metadata_by_version={
                "1.0": make_metadata("foo", "1.0", "bar==1.0"),
                "2.0": make_metadata("foo", "2.0", "bar==3.0"),
            },
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
        )
        provider.fetch_versions("bar")
        provider.solution_ranges["bar"] = SpecifierSet("==2.0").to_range()

        assert provider.choose_version("foo", VersionRange.full()) is None

        reason = rendered_reason(provider, "foo")
        assert "needs bar in , but" not in reason
        assert (
            "needs bar in ==3.0 or ==1.0, but the resolve holds bar in ==2.0" in reason
        )

    def test_root_blocker_names_an_unconstrained_root_range(self) -> None:
        """The root side reads as "any version" rather than as nothing.

        ``foo`` 1.0 requires ``bar>=2,<1``, which no version satisfies, so it
        is disjoint with the root's unconstrained ``bar``.  An unconstrained
        range spells as nothing, which would end the line on a dangling
        ``in``.
        """
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=make_metadata("foo", "1.0", "bar>=2,<1"),
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={
                "foo": VersionRange.full(admit_arbitrary=False),
                "bar": VersionRange.full(admit_arbitrary=False),
            },
        )

        assert provider.choose_version("foo", VersionRange.full()) is None

        reason = rendered_reason(provider, "foo")
        assert (
            "needs bar in no version, but your project requires bar any version"
            in reason
        )

    def test_post_release_pin_blocker_spells_both_sides_as_specifiers(self) -> None:
        """``bar>2.0`` excludes 2.0's post releases, so it is disjoint with
        ``==2.0.post1``.  Both sides read as the specifiers a user would write,
        so the reader can see why they do not overlap.
        """
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=(
                "Metadata-Version: 2.1\n"
                "Name: foo\n"
                "Version: 1.0\n"
                "Requires-Dist: bar>2.0\n"
            ),
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
        )
        provider.solution_ranges["bar"] = SpecifierSet("==2.0.post1").to_range()
        result = provider.choose_version("foo", VersionRange.full())
        assert result is None
        reason = rendered_reason(provider, "foo")
        assert "needs bar in >2.0, but the resolve holds bar in ==2.0.post1" in reason

    def test_decision_block_rejection_names_the_requirement(self) -> None:
        """The decision-block diagnostic names the candidate's real
        requirement, mirroring the range-block path.  ``foo`` 1.0 requires
        ``bar==2.0`` and the resolver has already decided ``bar==1.0``, so
        every ``foo`` candidate is rejected.  The message must name the
        ``bar==2.0`` foo needs, not ``requires bar != 1.0`` (which wrongly
        implies any bar other than 1.0 would satisfy foo).

        The ranges are spelled out rather than interpolated, so the
        assertion fails if the message ever prints the debug repr again.
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
        result = provider.choose_version("foo", VersionRange.full())
        assert result is None
        reason = rendered_reason(provider, "foo")
        assert "<VersionRange" not in reason
        assert "AFTER_LOCALS" not in reason
        assert "needs bar in ==2.0, but the resolve chose bar 1.0" in reason
        assert "requires bar != 1.0" not in reason


def _sdist_entry(version: str) -> dict[str, object]:
    """A Simple-API file record for ``foo``'s sdist at ``version``."""
    return {
        "filename": f"foo-{version}.tar.gz",
        "url": f"https://pypi.example/foo-{version}.tar.gz",
    }


def _wheel_entry(version: str, *, sidecar: bool) -> dict[str, object]:
    """A Simple-API file record for ``foo``'s wheel at ``version``."""
    return {
        "filename": f"foo-{version}-py3-none-any.whl",
        "url": f"https://pypi.example/foo-{version}-py3-none-any.whl",
        "core-metadata": sidecar,
    }


def _skipped_release_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The per-release offline-skip lines logged for ``foo``."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and r.getMessage().startswith("Skipping foo==")
    ]


def _seed_listing(cache_dir: Path, files: list[dict[str, object]]) -> OnDiskCache:
    """Write a cached Simple listing for ``foo`` and return the cache."""
    cache = OnDiskCache(cache_dir, DEFAULT_INDEX_URL)
    cache.put_simple(
        "foo",
        json.dumps({"files": files}).encode(),
        CachePolicy(fetched_at=0, max_age=1, etag=None),
    )
    return cache


class TestOfflineMetadataMiss:
    """A version whose metadata is uncached is named as an offline miss.

    Offline still degrades by skipping the version, but what the user is told
    must not name an artifact nab never requested.
    """

    _PKG_INFO = "Metadata-Version: 2.2\nName: foo\nVersion: {ver}\n\n"

    @pytest.mark.parametrize(
        "files",
        [
            pytest.param([_sdist_entry("1.0")], id="sdist-only"),
            pytest.param(
                [_wheel_entry("1.0", sidecar=True), _sdist_entry("1.0")],
                id="sidecar-and-sdist",
            ),
            pytest.param([_wheel_entry("1.0", sidecar=False)], id="bare-wheel"),
        ],
    )
    def test_cold_metadata_names_offline_not_the_artifact(
        self, tmp_path: Path, files: list[dict[str, object]]
    ) -> None:
        """Each metadata rung reports the offline miss it actually hit."""
        _seed_listing(tmp_path, files)
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
        ) as coordinator:
            provider = Provider(coordinator, target=_PY312)
            with pytest.raises(MetadataError) as excinfo:
                provider.get_dependencies("foo", V("1.0"))

        message = str(excinfo.value)
        assert "offline mode" in message
        assert "no cached metadata" in message
        assert "PKG-INFO" not in message
        assert "no sdist available" not in message
        assert "no PEP 658 metadata" not in message

    def test_a_read_sdist_is_not_reported_as_an_offline_miss(
        self, tmp_path: Path
    ) -> None:
        """An earlier rung skipped does not speak for the sdist nab did read."""
        cache = _seed_listing(
            tmp_path, [_wheel_entry("1.0", sidecar=False), _sdist_entry("1.0")]
        )
        cache.put_sdist_files("foo", "1.0", None, None)
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
        ) as coordinator:
            provider = Provider(coordinator, target=_PY312)
            with pytest.raises(MetadataError) as excinfo:
                provider.get_dependencies("foo", V("1.0"))

        assert "the sdist has no readable PKG-INFO" in str(excinfo.value)
        assert "offline mode" not in str(excinfo.value)

    def test_one_warning_covers_the_package_across_targets(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One warning covers a package's skipped releases across every target.

        A cold cache skips as many releases as the listing holds, and each
        target rescans them with its own provider, so a warning per release
        would bury the one fact worth reading.
        """
        versions = ("1.0", "2.0", "3.0")
        _seed_listing(tmp_path, [_sdist_entry(v) for v in versions])
        with (
            caplog.at_level(logging.INFO),
            FetchCoordinator(
                transport=Urllib3AsyncTransport(),
                cache_dir=tmp_path,
                offline=True,
            ) as coordinator,
        ):
            for target in (_PY312, _LINUX311):
                provider = Provider(coordinator, target=target)
                for version in versions:
                    with pytest.raises(MetadataError):
                        provider.get_dependencies("foo", V(version))

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "Skipping releases of foo" in warnings[0].getMessage()

        skipped = _skipped_release_messages(caplog)
        assert len(skipped) == len(versions) * 2
        assert {m.split(":")[0] for m in skipped} == {
            f"Skipping foo=={v}" for v in versions
        }

    def test_a_dropped_version_is_reported_when_an_older_one_pins(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A newer version dropped for a cold slot is not dropped in silence."""
        cache = _seed_listing(tmp_path, [_sdist_entry("1.0"), _sdist_entry("2.0")])
        cache.put_sdist_files("foo", "1.0", self._PKG_INFO.format(ver="1.0"), None)
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        with (
            caplog.at_level(logging.INFO),
            FetchCoordinator(
                transport=Urllib3AsyncTransport(),
                cache_dir=tmp_path,
                offline=True,
            ) as coordinator,
        ):
            provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
            resolver = Resolver(provider, range_type=VersionRange, root_version="0")
            pins = resolver.resolve(root_reqs)

        assert pins["foo"] == V("1.0")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "Skipping releases of foo" in warnings[0].getMessage()
        assert "offline mode" in warnings[0].getMessage()

        skipped = _skipped_release_messages(caplog)
        assert len(skipped) == 1
        assert "foo==2.0" in skipped[0]

    def test_warm_metadata_offline_pins_the_newest_and_stays_quiet(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The control: with both slots warm, offline pins 2.0 and logs nothing."""
        cache = _seed_listing(tmp_path, [_sdist_entry("1.0"), _sdist_entry("2.0")])
        for version in ("1.0", "2.0"):
            cache.put_sdist_files(
                "foo", version, self._PKG_INFO.format(ver=version), None
            )
        root_reqs = {"foo": VersionRange.full(admit_arbitrary=False)}
        with (
            caplog.at_level(logging.INFO),
            FetchCoordinator(
                transport=Urllib3AsyncTransport(),
                cache_dir=tmp_path,
                offline=True,
            ) as coordinator,
        ):
            provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
            resolver = Resolver(provider, range_type=VersionRange, root_version="0")
            pins = resolver.resolve(root_reqs)

        assert pins["foo"] == V("2.0")
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
        assert _skipped_release_messages(caplog) == []


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
            _hashes: tuple[tuple[str, str], ...],
        ) -> threading.Event:
            coordinator.index.store_sdist_metadata_error(
                pkg, ver, SdistHashMismatchError("sdist sha256 mismatch")
            )
            return _done_event()

        coordinator.override("request_sdist", _poisoned_request_sdist)
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

    def test_unevaluable_marker_in_metadata_drops_the_version(self) -> None:
        """Index metadata with an undecidable marker loses only that version.

        A declared source or an override stops the run instead: there the
        marker is the user's to fix, while an index can serve another version.
        """
        bad_metadata = make_metadata("foo", "1.0", 'dep-a; sys_platform ~= "linux"')
        coordinator = make_coordinator(
            [make_wheel("1.0")], metadata_text=bad_metadata, package="foo"
        )
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(MetadataError, match="cannot be evaluated"):
            provider.get_dependencies("foo", V("1.0"))

        assert provider.has_invalid_metadata("foo", V("1.0"))

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
        with caplog.at_level("WARNING", logger="nab_provider.provider"):
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

    def test_unreadable_local_wheel_fails_instead_of_pinning_older(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable local wheel fails the resolve, not just the version.

        Look-ahead treats a ``MetadataError`` as a rejection, so folding the
        read failure into one would drop 1.0 and answer with 0.9.
        """
        readable = tmp_path / "foo-0.9-py3-none-any.whl"
        _write_local_wheel(readable, "foo", "0.9")
        refused = tmp_path / "foo-1.0-py3-none-any.whl"
        _write_local_wheel(refused, "foo", "1.0")
        _deny_zip_open(monkeypatch, refused)

        coordinator = make_coordinator(
            [
                make_wheel("0.9", has_metadata=False, local_path=readable),
                make_wheel("1.0", has_metadata=False, local_path=refused),
            ],
            package="foo",
        )
        root_reqs = {"foo": SpecifierSet(">=0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        with pytest.raises(UnreadableLocalIndexError, match="Permission denied"):
            provider.choose_version("foo", root_reqs["foo"])

    def test_corrupt_local_wheel_pins_the_older_version(self, tmp_path: Path) -> None:
        """A wheel whose METADATA will not decompress rejects only that version.

        The archive opens and lists its members, so the failure is the wheel's
        own content rather than a filesystem fault: 1.0 drops and 0.9 answers.
        """
        readable = tmp_path / "foo-0.9-py3-none-any.whl"
        _write_local_wheel(readable, "foo", "0.9")
        corrupt = tmp_path / "foo-1.0-py3-none-any.whl"
        _write_corrupt_bz2_wheel(corrupt, "foo", "1.0")

        coordinator = make_coordinator(
            [
                make_wheel("0.9", has_metadata=False, local_path=readable),
                make_wheel("1.0", has_metadata=False, local_path=corrupt),
            ],
            package="foo",
        )
        root_reqs = {"foo": SpecifierSet(">=0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        assert provider.choose_version("foo", root_reqs["foo"]) == V("0.9")

    def test_unreadable_local_wheel_falls_back_to_sdist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The version's own sdist still answers for an unreadable local wheel."""
        wheel_path = tmp_path / "foo-1.0-py3-none-any.whl"
        _write_local_wheel(wheel_path, "foo", "1.0")
        _deny_zip_open(monkeypatch, wheel_path)

        coordinator = make_coordinator(
            [
                make_wheel("1.0", has_metadata=False, local_path=wheel_path),
                make_sdist("1.0"),
            ],
            package="foo",
            sdist_pkg_info=(
                "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: baz>=3\n"
            ),
        )
        provider = Provider(coordinator, target=_PY312)

        deps = provider.get_dependencies("foo", V("1.0"))
        assert "baz" in deps

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


_RANGE_METADATA = (
    "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=2\n"
)


class TestRangeMetadataRung:
    """Rung 4: a sidecar-less remote wheel recovers METADATA over an HTTP range."""

    def test_bare_remote_wheel_resolves_via_range(self) -> None:
        """A bare remote wheel with no sidecar resolves from a range read."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False)],
            package="foo",
            range_result=RangeMetadataResult(_RANGE_METADATA, RangeOutcome.PARTIAL),
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps
        assert V("2.0") in deps["bar"]
        assert V("1.0") not in deps["bar"]
        assert provider.stats.wheel_metadata_range_fetched == 1

    def test_range_miss_falls_to_sdist(self) -> None:
        """A range read that yields no METADATA steps to the sdist rung."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False), make_sdist("1.0")],
            package="foo",
            range_result=RangeMetadataResult(None, RangeOutcome.MISSING),
            sdist_pkg_info=(
                "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: baz>=3\n"
            ),
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "baz" in deps
        assert provider.stats.wheel_metadata_range_missing == 1
        assert provider.stats.sdist_pkg_info_fetched == 1

    def test_range_utf8_error_fails_resolve(self) -> None:
        """Malformed-UTF-8 METADATA from a range read fails the resolve."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False), make_sdist("1.0")],
            package="foo",
            range_error=MalformedSimpleResponseError("bad utf-8"),
            sdist_pkg_info=(
                "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: baz>=3\n"
            ),
        )
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(MalformedSimpleResponseError):
            provider.get_dependencies("foo", V("1.0"))

    @pytest.mark.parametrize(
        ("outcome", "text", "stat_name"),
        [
            (RangeOutcome.PARTIAL, _RANGE_METADATA, "wheel_metadata_range_fetched"),
            (RangeOutcome.FULL_BODY, _RANGE_METADATA, "wheel_metadata_range_full_body"),
            (RangeOutcome.UNSUPPORTED, None, "wheel_metadata_range_unsupported"),
            (RangeOutcome.MISSING, None, "wheel_metadata_range_missing"),
        ],
    )
    def test_each_outcome_increments_its_counter(
        self, outcome: RangeOutcome, text: str | None, stat_name: str
    ) -> None:
        """Each range outcome bumps exactly its own ProviderStats counter."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False), make_sdist("1.0")],
            package="foo",
            range_result=RangeMetadataResult(text, outcome),
            sdist_pkg_info=(
                "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: baz>=3\n"
            ),
        )
        provider = Provider(coordinator, target=_PY312)
        provider.get_dependencies("foo", V("1.0"))
        counters = {
            "wheel_metadata_range_fetched",
            "wheel_metadata_range_full_body",
            "wheel_metadata_range_unsupported",
            "wheel_metadata_range_missing",
        }
        assert getattr(provider.stats, stat_name) == 1
        for other in counters - {stat_name}:
            assert getattr(provider.stats, other) == 0

    def test_no_outcome_recorded_leaves_counters_zero(self) -> None:
        """A refused or offline-missed range read records no outcome counter."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False), make_sdist("1.0")],
            package="foo",
            sdist_pkg_info=(
                "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n"
                "Requires-Dist: baz>=3\n"
            ),
        )
        provider = Provider(coordinator, target=_PY312)
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "baz" in deps
        assert provider.stats.wheel_metadata_range_fetched == 0
        assert provider.stats.wheel_metadata_range_full_body == 0
        assert provider.stats.wheel_metadata_range_unsupported == 0
        assert provider.stats.wheel_metadata_range_missing == 0

    def test_sibling_bare_wheels_isolated_across_targets(self) -> None:
        """Two sibling sidecar-less wheels of one version keep separate deps.

        In a matrix resolve two targets share one coordinator and pick different
        bare wheels of the same ``(package, version)`` from their own
        tag-filtered listings.  Each must read the deps of the wheel it picked,
        not whichever sibling ranged first.
        """
        url_a = "https://example.com/foo-1.0-cp312-manylinux.whl"
        url_b = "https://example.com/foo-1.0-cp312-macosx.whl"
        wheel_a = WheelFile(
            filename="foo-1.0-cp312-manylinux.whl",
            url=url_a,
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
        )
        wheel_b = WheelFile(
            filename="foo-1.0-cp312-macosx.whl",
            url=url_b,
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
        )
        meta_a = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: linux-dep\n"
        )
        meta_b = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: macos-dep\n"
        )
        coordinator = make_coordinator(
            [wheel_a, wheel_b],
            package="foo",
            range_by_url={
                url_a: RangeMetadataResult(meta_a, RangeOutcome.PARTIAL),
                url_b: RangeMetadataResult(meta_b, RangeOutcome.PARTIAL),
            },
        )
        provider = Provider(coordinator, target=_PY312)
        text_a, from_a = metadata_resolver.resolve_metadata(
            provider, [(V("1.0"), wheel_a)], "foo", V("1.0")
        )
        text_b, from_b = metadata_resolver.resolve_metadata(
            provider, [(V("1.0"), wheel_b)], "foo", V("1.0")
        )
        assert text_a == meta_a
        assert text_b == meta_b
        assert not from_a
        assert not from_b

    def test_wheel_only_bare_wheel_resolves_over_range(self) -> None:
        """Under WHEEL_ONLY a sidecar-less wheel now resolves instead of skipping."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False)],
            package="foo",
            range_result=RangeMetadataResult(_RANGE_METADATA, RangeOutcome.PARTIAL),
        )
        provider = Provider(
            coordinator, target=_PY312, dist_policy=DistPolicy.WHEEL_ONLY
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps
        assert provider.stats.wheel_metadata_range_fetched == 1


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
        ranges: dict[str, VersionRange] = {}
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {}
        add_classified_dep(Requirement("bar>=2.0"), set(), base, extra_map, ranges)
        add_classified_dep(Requirement("bar<5.0"), set(), base, extra_map, ranges)

        assert V("3.0") in base["bar"]
        assert V("1.0") not in base["bar"]
        assert V("9.0") not in base["bar"]

        # The memoized range is shared, so folding must leave it as it was.
        assert V("9.0") in ranges[">=2.0"]

    def test_extra_deps_intersect(self) -> None:
        """A name seen twice under one extra folds to the intersection."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {"x": {}}
        add_classified_dep(Requirement("bar>=2.0"), {"x"}, base, extra_map, {})
        add_classified_dep(Requirement("bar<5.0"), {"x"}, base, extra_map, {})
        assert V("3.0") in extra_map["x"]["bar"]
        assert V("1.0") not in extra_map["x"]["bar"]
        assert V("9.0") not in extra_map["x"]["bar"]

    def test_repeated_specifier_builds_one_range(self) -> None:
        """Two deps spelling one specifier share the range built for it."""
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
        assert V("2.0") in base["bar"]
        assert V("0.5") not in base["bar"]
        assert base["bar[a]"] == VersionRange.full(admit_arbitrary=False)
        assert base["bar[b]"] == VersionRange.full(admit_arbitrary=False)

    def test_extra_gated_multi_extra_splits_into_one_proxy_each(self) -> None:
        """``bar[a,b]`` under extra ``x`` records both proxies in that bucket."""
        base: dict[str, VersionRange] = {}
        extra_map: dict[str, dict[str, VersionRange]] = {"x": {}}
        add_classified_dep(Requirement("bar[a,b]>=1.0"), {"x"}, base, extra_map, {})
        assert V("2.0") in extra_map["x"]["bar"]
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
        """Extra-gated proxy keys come out sorted regardless of extras set order."""
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
        return Provider(coordinator, target=_PY312)

    def test_matches_a_single_pass_split(self) -> None:
        """Each built map equals the one-pass split, key order included."""
        provider = self._provider()
        for version in ("1.0", "2.0"):
            cache_key = ("pkg", V(version))
            provider.get_dependencies("pkg", V(version))

            built = provider.extra_deps_map[cache_key]
            expected = _one_pass_extra_deps(provider, cache_key)

            assert built == expected
            assert list(built) == list(expected)
            for extra, deps in built.items():
                assert list(deps) == list(expected[extra])

    def test_extras_hold_their_own_deps(self) -> None:
        """The split lands each dep under the extras whose markers select it."""
        provider = self._provider()
        provider.get_dependencies("pkg", V("1.0"))

        built = provider.extra_deps_map[("pkg", V("1.0"))]

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

        assert V("3.9") in aiohttp
        assert V("3.0") not in aiohttp
        assert V("4.0") not in aiohttp

    def test_base_deps_do_not_wait_for_the_split(self) -> None:
        """The deps the base environment admits come straight from the parse."""
        provider = self._provider()

        base = provider.get_dependencies("pkg", V("1.0"))

        assert set(base) == {"attrs", "legacy"}

    def test_key_absent_until_the_release_is_parsed(self) -> None:
        """A key missing means unparsed metadata, not a release without extras."""
        provider = self._provider()
        cache_key = ("pkg", V("1.0"))

        assert cache_key not in provider.extra_deps_map
        assert provider.extra_deps_map.get(cache_key, {}) == {}

        provider.get_dependencies("pkg", V("1.0"))

        assert cache_key in provider.extra_deps_map
        assert provider.extra_deps_map.get(cache_key, {}) != {}

    def test_no_marker_is_evaluated_against_an_extra_until_a_read(self) -> None:
        """Parsing leaves the split undone: no marker is bound to an extra yet."""
        provider = self._provider()
        provider.get_dependencies("pkg", V("1.0"))

        assert provider.marker_extra_cache == {}

        assert provider.extra_deps_map[("pkg", V("1.0"))]

        assert provider.marker_extra_cache != {}

    def test_membership_leaves_the_split_undone(self) -> None:
        """Asking whether a release is parsed must not build its split."""
        provider = self._provider()
        provider.get_dependencies("pkg", V("1.0"))

        assert ("pkg", V("1.0")) in provider.extra_deps_map

        assert provider.marker_extra_cache == {}

    def test_parsed_releases_enumerate_in_parse_order(self) -> None:
        """The mapping reports the releases parsed, newest parse last."""
        provider = self._provider()
        provider.get_dependencies("pkg", V("2.0"))
        provider.get_dependencies("pkg", V("1.0"))

        assert list(provider.extra_deps_map) == [("pkg", V("2.0")), ("pkg", V("1.0"))]
        assert len(provider.extra_deps_map) == 2

    def test_a_built_map_is_kept(self) -> None:
        """The split runs once per release however often the map is read."""
        provider = self._provider()
        provider.get_dependencies("pkg", V("1.0"))

        first = provider.extra_deps_map[("pkg", V("1.0"))]

        assert provider.extra_deps_map[("pkg", V("1.0"))] is first

    def test_base_parse_consults_every_marker(self) -> None:
        """Parsing the release evaluates each marker, extra-gated ones included."""
        provider = self._provider()
        provider.get_dependencies("pkg", V("1.0"))

        metadata = provider.metadata_cache[("pkg", V("1.0"))]
        markers = {r.marker for r in metadata.requires_dist if r.marker is not None}

        assert provider.consulted_markers == markers

    def test_building_a_map_consults_no_further_markers(self) -> None:
        """Building the split records no marker the parse had not already."""
        provider = self._provider()
        provider.get_dependencies("pkg", V("1.0"))
        consulted = set(provider.consulted_markers)

        assert provider.extra_deps_map[("pkg", V("1.0"))]
        assert provider.consulted_markers == consulted

    def test_reading_the_map_does_not_keep_the_provider_alive(self) -> None:
        """A finished provider dies on its refcount, without waiting for a sweep."""
        provider = self._provider()
        provider.get_dependencies("pkg", V("1.0"))
        assert provider.extra_deps_map[("pkg", V("1.0"))]
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
        provider = Provider(coordinator, target=_PY312)
        provider.get_dependencies("pkg", V("1.0"))

        assert set(provider.extra_deps_map[("pkg", V("1.0"))]["uvloop"]) == {"baz"}
        assert provider.deferred_url_extras[("pkg", V("1.0"))]["uvloop"]


class TestTargetDepSignature:
    """``target_dep_signature`` projects metadata to a comparable signature."""

    def _provider(self) -> Provider:
        coordinator = make_coordinator([make_wheel("1.0")], package="pkg")
        return Provider(coordinator, target=_PY312)

    @staticmethod
    def _md(
        requires_dist: list[str],
        *,
        provides_extra: tuple[str, ...] = (),
        requires_python: str | None = None,
    ) -> WheelMetadata:
        return WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=(
                SpecifierSet(requires_python) if requires_python is not None else None
            ),
            requires_dist=[Requirement(r) for r in requires_dist],
            provides_extra=list(provides_extra),
        )

    def _sig(self, provider: Provider, metadata: WheelMetadata) -> TargetDepSignature:
        """Project ``metadata`` under the release the wheel was served as."""
        return target_dep_signature(provider, ("pkg", V("1.0")), metadata)

    def test_permuted_lines_equal(self) -> None:
        """Line order does not change the signature."""
        provider = self._provider()
        a = self._md(["foo>=1", "bar<2"])
        b = self._md(["bar<2", "foo>=1"])
        assert self._sig(provider, a) == self._sig(provider, b)

    def test_specifier_spelling_equal(self) -> None:
        """Whitespace and specifier spelling normalize equal."""
        provider = self._provider()
        a = self._md(["foo>=1,<2"])
        b = self._md(["foo >= 1, < 2"])
        assert self._sig(provider, a) == self._sig(provider, b)

    def test_target_equal_marker_equal(self) -> None:
        """Markers that evaluate the same for the target project equal."""
        provider = self._provider()
        a = self._md(['foo; python_version >= "3.0"'])
        b = self._md(['foo; python_version >= "3.5"'])
        assert self._sig(provider, a) == self._sig(provider, b)

    def test_genuine_dep_difference_unequal(self) -> None:
        """A real base-dep range difference is unequal."""
        provider = self._provider()
        a = self._md(["foo>=1"])
        b = self._md(["foo>=2"])
        assert self._sig(provider, a) != self._sig(provider, b)

    def test_extra_only_difference_unequal(self) -> None:
        """A difference confined to an extra-gated dep is unequal."""
        provider = self._provider()
        a = self._md(['foo; extra == "e"'], provides_extra=("e",))
        b = self._md(['foo>=2; extra == "e"'], provides_extra=("e",))
        assert self._sig(provider, a) != self._sig(provider, b)

    def test_url_dep_only_difference_unequal(self) -> None:
        """A difference confined to a URL dep is unequal."""
        provider = self._provider()
        a = self._md(["foo @ https://example.com/a.whl"])
        b = self._md(["foo @ https://example.com/b.whl"])
        assert self._sig(provider, a) != self._sig(provider, b)

    def test_requires_python_difference_folds(self) -> None:
        """A Requires-Python difference alone does not change the signature.

        Requires-Python gates admission, not the dependency edges a lock
        records, so two wheels with the same deps but different Python floors
        project equal.
        """
        provider = self._provider()
        a = self._md(["foo"], requires_python=">=3.8")
        b = self._md(["foo"], requires_python=">=3.9")
        assert self._sig(provider, a) == self._sig(provider, b)

    def test_marker_dropped_dep_absent(self) -> None:
        """A dep whose marker fails for the target is not projected."""
        provider = self._provider()
        base_deps = self._sig(provider, self._md(['foo; python_version < "3.0"']))[0]
        assert base_deps == {}

    def test_duplicate_name_intersected(self) -> None:
        """A name on two base lines folds to the range intersection."""
        provider = self._provider()
        base_deps = self._sig(provider, self._md(["foo>=1", "foo<5"]))[0]
        assert V("3") in base_deps["foo"]
        assert V("6") not in base_deps["foo"]

    def test_extra_gated_url_dep_bucketed_by_extra(self) -> None:
        """An extra-gated URL dep buckets under its extra name."""
        provider = self._provider()
        url_buckets = self._sig(
            provider,
            self._md(
                ['foo @ https://example.com/a.whl ; extra == "e"'],
                provides_extra=("e",),
            ),
        )[2]
        assert "e" in url_buckets

    def test_base_url_dep_bucketed(self) -> None:
        """A base URL dep buckets under the base key, not an extra."""
        provider = self._provider()
        url_buckets = self._sig(
            provider, self._md(["foo @ https://example.com/a.whl"])
        )[2]
        assert list(url_buckets) == [None]

    def test_per_extra_dep_projected(self) -> None:
        """An extra-gated non-URL dep lands in the per-extra map."""
        provider = self._provider()
        extra_map = self._sig(
            provider, self._md(['foo>=2; extra == "e"'], provides_extra=("e",))
        )[1]
        assert V("3") in extra_map["e"]["foo"]
        assert V("1") not in extra_map["e"]["foo"]

    def test_extra_marker_matches_only_named_extra(self) -> None:
        """An extra-gated dep lands only under the extras its marker matches."""
        provider = self._provider()
        extra_map = self._sig(
            provider, self._md(['foo; extra == "e"'], provides_extra=("e", "f"))
        )[1]
        assert "foo" in extra_map["e"]
        assert extra_map["f"] == {}

    def test_provides_extra_override_applied(self) -> None:
        """A provides-extra override reshapes the projection before comparison.

        Two wheels declaring different raw extras project apart on their raw
        extras, but a provides-extra override normalizes both to the same
        declared set, so the overridden view they resolve from agrees.
        """
        coordinator = make_coordinator([make_wheel("1.0")], package="pkg")
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(pkg_override("pkg", provides_extra=("e",)),),
        )
        a = self._md(['foo; extra == "e"'], provides_extra=("e",))
        b = self._md(['foo; extra == "e"'], provides_extra=("e", "f"))
        assert self._sig(provider, a) == self._sig(provider, b)


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

    def test_unevaluable_marker_names_the_marker(self, tmp_path: Path) -> None:
        """A declared tree's undecidable marker stops the run, naming the marker."""

        self._write_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.2.3"\n'
            "dependencies = [\"dep-a; sys_platform ~= 'linux'\"]\n",
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        with pytest.raises(UnevaluableMarkerError, match='sys_platform ~= "linux"'):
            provider.get_dependencies("foo", V("1.2.3"))

    def test_local_source_root_requirement_skips_listing_request(
        self, tmp_path: Path
    ) -> None:
        """Constructor must not request a PyPI listing for a workspace member."""

        self._write_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.2.3"\n',
        )
        coordinator = make_coordinator([], package="foo")
        coordinator.reset()
        Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            root_requirements={"foo": SpecifierSet("").to_range()},
            build_policy=BuildPolicy.NEVER,
        )
        assert not coordinator.calls_to("request_listing")

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
        coordinator.reset()
        provider.prefetch_new_deps({"foo": SpecifierSet("").to_range()})
        assert not coordinator.calls_to("request_listing")

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
        appears, NEVER cannot satisfy the request and listing the source
        raises.
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
        with pytest.raises(UnsupportedSdistError) as excinfo:
            provider.fetch_versions("foo")
        msg = str(excinfo.value)
        assert "building requires build-policy 'build-local'" in msg
        assert "effective policy is 'never'" in msg
        assert "BuildPolicy." not in msg

    def test_non_utf8_pyproject_raises_unsupported(self, tmp_path: Path) -> None:
        """A local source with a non-UTF-8 ``pyproject.toml`` is unbuildable.

        Neither the static read nor the backend can read the file, so it is
        reported as an unsupported source, not as a decode traceback.
        """
        (tmp_path / "pyproject.toml").write_bytes(
            b'[project]\nname = "foo"\nversion = "1.0"\ndescription = "\xe9"\n'
        )
        coordinator = make_coordinator([], package="foo", build_config=ResolveInputs())
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.BUILD_LOCAL,
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

    def test_local_source_name_mismatch_refused(self, tmp_path: Path) -> None:
        """A source declared for one name backed by a different project is refused.

        The directory's ``[project].name`` (``bar``) does not canonicalise to the
        declared source name (``foo``), so materialising it would pin ``foo`` with
        ``bar``'s version and dependencies.
        """
        self._write_local(
            tmp_path,
            '[project]\nname = "bar"\nversion = "9.9.9"\n'
            'dependencies = ["unrelated-dep==6.6.6"]\n',
        )
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        with pytest.raises(SourceNameMismatchError, match="bar") as excinfo:
            provider.fetch_versions("foo")
        assert "foo" in str(excinfo.value)

    @pytest.mark.parametrize(
        "body",
        [
            '[build-system]\nrequires = []\nbuild-backend = "stub"\n',
            '[project]\nname = "bar"\ndynamic = ["version"]\n',
        ],
        ids=["no-project-table", "dynamic-version"],
    )
    def test_built_source_name_mismatch_points_at_the_project(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        body: str,
    ) -> None:
        """The mismatch message points at the built project rather than a key.

        The name comes back from the backend either way, and a tree with no
        ``[project]`` table has no key to blame.
        """
        self._write_local(tmp_path, body)

        built = WheelMetadata(
            name="bar",
            version=V("2.3.0"),
            requires_python=None,
            requires_dist=[],
            provides_extra=[],
        )
        monkeypatch.setattr(
            "nab_project.build_backend.extract_metadata",
            lambda _path, **_kwargs: built,
        )

        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.BUILD_LOCAL,
        )

        with pytest.raises(SourceNameMismatchError) as excinfo:
            provider.fetch_versions("foo")

        msg = str(excinfo.value)
        assert "[project].name" not in msg
        assert (
            "local source 'foo' declares package 'foo' but the project at"
            f" {tmp_path} is named 'bar'" in msg
        )

    def test_build_local_source_failure_propagates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A backend failure under BUILD_LOCAL surfaces as UnsupportedSdistError."""
        from nab_project import (
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

        monkeypatch.setattr("nab_project.build_backend.extract_metadata", boom)
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

        def fake_build(path: Path, **kwargs: object) -> WheelMetadata:
            captured["path"] = path
            captured.update(kwargs)
            return built

        monkeypatch.setattr("nab_project.build_backend.extract_metadata", fake_build)
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.BUILD_LOCAL,
        )
        assert len(provider.fetch_versions("foo")) == 1
        assert captured == {
            "path": tmp_path,
            "config": coordinator.build_config,
            "offline": False,
        }

    def test_local_source_build_refused_under_an_offline_coordinator(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The build env's own fetches obey ``--offline`` too."""
        self._write_local(
            tmp_path,
            """
            [project]
            name = "foo"
            version = "1.0"
            dynamic = ["dependencies"]

            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"
            """,
        )

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("offline must not reach the network")

        monkeypatch.setattr("nab_project.resolve.resolve_for_targets", _boom)
        coordinator = make_coordinator(
            [], package="foo", build_config=ResolveInputs(), offline=True
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.BUILD_LOCAL,
        )
        with pytest.raises(UnsupportedSdistError, match="offline mode"):
            provider.fetch_versions("foo")

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
        from nab_provider._vendor.packaging.specifiers import SpecifierSet

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

    def test_root_rejection_queues_its_own_clause(self) -> None:
        """A root-requirement rejection bans just the versions it rejected.

        Look-ahead rejects the candidate before it is decided, so the
        dependency clause that would explain the rejection is never added.
        """
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={
                "2.0": (
                    "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\n"
                    "Requires-Dist: bar>=5.0\n"
                ),
                "1.0": "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            },
            package="foo",
        )
        root_range = SpecifierSet("<2.0").to_range()
        provider = Provider(
            coordinator, target=_PY312, root_requirements={"bar": root_range}
        )
        assert provider.choose_version("foo", SpecifierSet("").to_range()) == V("1.0")

        (clause,) = provider.consume_pending_clauses()
        assert clause.cause is IncompatibilityCause.NO_VERSIONS
        (term,) = clause.terms
        assert term.package == "foo"
        assert term.is_positive()
        assert V("2.0") in term.constraint
        assert V("1.0") not in term.constraint

    def test_metadata_rejection_queues_its_own_clause(self) -> None:
        """A version whose metadata will not read is banned the same way."""
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={
                "2.0": "Metadata-Version: 2.1\nName: foo\nVersion: nope\n",
                "1.0": "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            },
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
        )
        assert provider.choose_version("foo", SpecifierSet("").to_range()) == V("1.0")

        (clause,) = provider.consume_pending_clauses()
        assert clause.cause is IncompatibilityCause.NO_VERSIONS
        (term,) = clause.terms
        assert term.package == "foo"
        assert V("2.0") in term.constraint
        assert V("1.0") not in term.constraint

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

        coordinator = make_coordinator(wheels, package="foo")
        index = coordinator.index

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

        coordinator.override("request_metadata", _request_metadata)

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

        coordinator.override("request_metadata_batch", _request_metadata_batch)

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

    def test_self_dependency_rejection_merges_into_one_term(self) -> None:
        """A candidate excluded by its own requirement gets one merged term.

        ``foo`` 1.0 requires ``foo==5.0`` while the solution holds ``foo<2.0``.
        Both terms would name ``foo``, and a clause holds at most one term per
        package, so they merge to the versions the requirement rules out and
        the clause carries the declared range.
        """
        wheels = [make_wheel("1.0")]
        meta = make_metadata("foo", "1.0", "foo==5.0")
        coordinator = make_coordinator(wheels, metadata_text=meta, package="foo")
        provider = Provider(coordinator, target=_PY312)
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("<2.0").to_range()}, {}
        )

        assert provider._look_ahead_ok("foo", V("1.0")) is False
        provider._flush_pending_blocks()

        (clause,) = provider.consume_pending_clauses()
        (term,) = clause.terms
        assert term.package == "foo"
        assert term.is_positive()

        assert V("1.0") in term.constraint
        assert V("5.0") not in term.constraint
        assert clause.dependency_range == SpecifierSet("==5.0").to_range()

    def test_self_dependency_report_names_the_edge(self) -> None:
        """The report states the self-dependency rather than a tautology.

        ``proj`` 3.0 requires ``proj==2.0`` while the solution holds ``>2.0``
        from ``app``, so look-ahead rejects it.  Two positive terms for
        ``proj`` would print as ``proj`` being incompatible with itself.
        """
        coordinator = make_coordinator(
            listings={
                "app": [make_wheel("1.0")],
                "proj": [make_wheel("3.0"), make_wheel("2.0")],
            },
            metadata_by_version={
                "1.0": make_metadata("app", "1.0", "proj>2.0"),
                "3.0": make_metadata("proj", "3.0", "proj==2.0"),
                "2.0": make_metadata("proj", "2.0", "proj==2.0"),
            },
        )
        root_reqs = {"app": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
            format_range=provider.format_range,
        )
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(dict(root_reqs))

        lines = str(exc_info.value).splitlines()
        assert "because proj >=3.0 depends on proj ==2.0" in lines
        assert not any(
            "incompatible with" in line and line.count("proj") > 1 for line in lines
        )

        assert short_reason(provider, "proj") == (
            "every version in range needs proj in ==2.0, but the resolve holds proj in >2.0"
        )

    def test_flush_widens_terms_onto_listed_gaps(self) -> None:
        """Consecutive rejected candidates coalesce into one segment and the
        blocker covers every version the scanned candidates exclude."""
        foo_wheels = [make_wheel(v) for v in ("3.0", "2.0", "1.0", "0.5")]
        bar_wheels = [make_wheel(v) for v in ("5.0", "3.0", "1.0")]
        meta_template = "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar>=5.0\n"
        coordinator = make_coordinator(
            listings={"foo": foo_wheels, "bar": bar_wheels},
            metadata_by_version={
                v: meta_template.format(ver=v) for v in ("3.0", "2.0", "1.0", "0.5")
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        assert provider.choose_version("foo", SpecifierSet(">=1.0").to_range()) is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        candidate, blocker = clauses[0].terms
        assert candidate.package == "foo"
        assert isinstance(candidate.constraint, VersionRange)
        assert len(candidate.constraint._bounds) == 1
        assert V("1.0") in candidate.constraint
        assert V("2.0") in candidate.constraint
        assert V("3.0") in candidate.constraint
        assert V("0.5") not in candidate.constraint
        assert blocker.package == "bar"
        assert V("3.0") in blocker.constraint
        assert V("4.0") in blocker.constraint
        # Every scanned candidate requires bar>=5.0, so bar 1.0 blocks them
        # all just as bar 3.0 does; bar 5.0 satisfies them and stays out.
        assert V("1.0") in blocker.constraint
        assert V("5.0") not in blocker.constraint

    def test_flush_widening_keeps_a_live_hole_version_out(self) -> None:
        """A still-selectable version inside a hole of the asked range stays
        out of the widened union, so the candidates keep separate segments."""
        foo_wheels = [make_wheel(v) for v in ("3.0", "2.0", "1.0", "0.5")]
        bar_wheels = [make_wheel(v) for v in ("5.0", "3.0", "1.0")]
        meta_template = "Metadata-Version: 2.1\nName: foo\nVersion: {ver}\nRequires-Dist: bar>=5.0\n"
        coordinator = make_coordinator(
            listings={"foo": foo_wheels, "bar": bar_wheels},
            metadata_by_version={
                v: meta_template.format(ver=v) for v in ("3.0", "2.0", "1.0", "0.5")
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("3.0")})
        asked = SpecifierSet(">=1.0,!=2.0").to_range()
        assert provider.choose_version("foo", asked) is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        candidate = clauses[0].terms[0]
        assert candidate.package == "foo"
        assert isinstance(candidate.constraint, VersionRange)
        assert len(candidate.constraint._bounds) == 2
        assert V("1.0") in candidate.constraint
        assert V("3.0") in candidate.constraint
        assert V("2.0") not in candidate.constraint
        assert V("0.5") not in candidate.constraint

    def test_flush_widens_range_block_candidates_only(self) -> None:
        """Range-keyed flush widens the candidate union; the blocker term
        keeps the captured positive-range object."""
        wheels = [make_wheel(v) for v in ("3.0", "2.0", "1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("foo")
        blocker_range = SpecifierSet("<2.0").to_range()
        provider.pending_range_blocks[("foo", "bar", blocker_range)].extend(
            [V("2.0"), V("3.0")]
        )
        provider._flush_pending_blocks()
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        candidate, blocker = clauses[0].terms
        assert isinstance(candidate.constraint, VersionRange)
        assert len(candidate.constraint._bounds) == 1
        assert V("2.0") in candidate.constraint
        assert V("3.0") in candidate.constraint
        assert V("1.0") not in candidate.constraint
        assert blocker.constraint is blocker_range

    def test_flush_keeps_singletons_when_widening_unavailable(self) -> None:
        """No cached listing: the flushed terms are exact singletons."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider.pending_blocks[("foo", "bar", V("2.0"))].extend([V("1.0"), V("3.0")])
        provider._flush_pending_blocks()
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        candidate, blocker = clauses[0].terms
        assert candidate.constraint == (
            VersionRange.singleton(V("1.0")) | VersionRange.singleton(V("3.0"))
        )
        assert blocker.constraint == VersionRange.singleton(V("2.0"))

    def test_blocker_widens_to_the_recorded_dep_range_complement(self) -> None:
        """The blocker term becomes the complement of the scanned candidates'
        dependency ranges, which reaches past the decided version's gap."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel(v) for v in ("11.0", "10.0")],
                "bar": [make_wheel(v) for v in ("9.0", "4.0", "2.0", "0.1")],
            },
            metadata_by_version={
                "10.0": make_metadata("foo", "10.0", "bar>=4.0"),
                "11.0": make_metadata("foo", "11.0", "bar>=9.0"),
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("2.0")})
        assert provider.choose_version("foo", SpecifierSet(">=1.0").to_range()) is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        blocker = clauses[0].terms[1]
        assert blocker.package == "bar"
        # The decided version, by construction outside every recorded range.
        assert V("2.0") in blocker.constraint
        # A listed version no candidate would have accepted either, and one
        # the decided version's neighbor gap leaves out.
        assert V("0.1") in blocker.constraint
        gap = provider.widen_decision("bar", V("2.0"))
        assert gap is not None
        assert V("0.1") not in gap
        # Versions some candidate accepts stay out.
        assert V("4.0") not in blocker.constraint
        assert V("9.0") not in blocker.constraint

    def test_blocker_membership_keeps_out_versions_a_candidate_accepts(self) -> None:
        """Candidates rejecting the blocker from opposite sides leave a bounded
        blocker term: everything between their ranges, and nothing inside."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel(v) for v in ("11.0", "10.0")],
                "bar": [make_wheel(v) for v in ("9.0", "4.0", "2.0", "0.1")],
            },
            metadata_by_version={
                "10.0": make_metadata("foo", "10.0", "bar<1.0"),
                "11.0": make_metadata("foo", "11.0", "bar>=9.0"),
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("2.0")})
        assert provider.choose_version("foo", SpecifierSet(">=1.0").to_range()) is None
        clauses = provider.consume_pending_clauses()
        blocker = clauses[0].terms[1]
        assert isinstance(blocker.constraint, VersionRange)
        assert len(blocker.constraint._bounds) == 1
        assert V("2.0") in blocker.constraint
        assert V("4.0") in blocker.constraint
        assert V("0.1") not in blocker.constraint
        assert V("9.0") not in blocker.constraint

    def test_blocker_membership_opens_no_admission_surface(self) -> None:
        """The complement inherits no arbitrary-string admission and drops the
        recorded ranges' pre-release opt-in region."""
        assert SpecifierSet(">=9.0rc1").to_range()._pre_region != ()
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel(v) for v in ("11.0", "10.0")],
                "bar": [make_wheel(v) for v in ("9.0", "2.0", "0.1")],
            },
            metadata_by_version={
                "10.0": make_metadata("foo", "10.0", "bar>=9.0rc1"),
                "11.0": make_metadata("foo", "11.0", "bar>=9.0rc1"),
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("2.0")})
        assert provider.choose_version("foo", SpecifierSet(">=1.0").to_range()) is None
        clauses = provider.consume_pending_clauses()
        blocker = clauses[0].terms[1]
        assert isinstance(blocker.constraint, VersionRange)
        assert V("2.0") in blocker.constraint
        assert blocker.constraint._pre_region == ()
        assert blocker.constraint._admit_arbitrary is False
        assert "wat" not in blocker.constraint

    def test_blocker_keeps_the_gap_without_recorded_dep_ranges(self) -> None:
        """A producer that queues versions without dependency ranges (the
        extras block path) leaves the blocker on its neighbor gap."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("10.0")],
                "bar": [make_wheel(v) for v in ("4.0", "2.0", "0.1")],
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("foo")
        provider.fetch_versions("bar")
        provider.pending_blocks[("foo", "bar", V("2.0"))].append(V("10.0"))
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert V("2.0") in blocker.constraint
        assert V("2.5") in blocker.constraint
        assert V("0.1") not in blocker.constraint
        assert V("4.0") not in blocker.constraint

    def test_blocker_keeps_the_gap_when_one_rejection_lacks_a_range(self) -> None:
        """Membership widening needs the whole group: one queued version
        without a recorded range drops the group back to the gap."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel(v) for v in ("11.0", "10.0")],
                "bar": [make_wheel(v) for v in ("4.0", "2.0", "0.1")],
            },
            metadata_by_version={"10.0": make_metadata("foo", "10.0", "bar>=4.0")},
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("bar")
        provider.receive_partial_solution_hint({}, {"bar": V("2.0")})
        assert provider._look_ahead_ok("foo", V("10.0")) is False
        provider.pending_blocks[("foo", "bar", V("2.0"))].append(V("11.0"))
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert V("2.0") in blocker.constraint
        assert V("0.1") not in blocker.constraint
        assert V("4.0") not in blocker.constraint

    def test_blocker_keeps_the_gap_when_the_union_swallows_the_decision(self) -> None:
        """The containment fence: a recorded union that contains the blocker
        version cannot have produced the rejections, so the gap stands."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("10.0")],
                "bar": [make_wheel(v) for v in ("4.0", "2.0", "0.1")],
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("foo")
        provider.fetch_versions("bar")
        key = ("foo", "bar", V("2.0"))
        provider.pending_blocks[key].append(V("10.0"))
        provider.pending_decision_dep_ranges[key] = DepRangeUnion(
            1, SpecifierSet(">=0.1").to_range()
        )
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert V("2.0") in blocker.constraint
        assert V("0.1") not in blocker.constraint
        assert V("4.0") not in blocker.constraint

    def test_decision_blocker_term_is_satisfied_by_the_decision(self) -> None:
        """The blocker term has to be satisfied by the decision's singleton;
        containing the decided version is not enough.

        A ``===`` dependency range separates the two: its complement rejects
        the literal ``1.0.0`` by string, so ``V("1.0")`` reads as contained
        while the singleton is not a subset.  Such a term leaves the clause
        undetermined, which suppresses NO_VERSIONS and livelocks the resolve,
        so the fence declines it and the gap stands.
        """
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("10.0")],
                "bar": [make_wheel(v) for v in ("2.0", "1.0")],
            },
        )
        provider = Provider(coordinator, target=_PY312)
        provider.fetch_versions("foo")
        provider.fetch_versions("bar")
        key = ("foo", "bar", V("1.0"))
        provider.pending_blocks[key].append(V("10.0"))
        provider.pending_decision_dep_ranges[key] = DepRangeUnion(
            1, SpecifierSet("===1.0.0").to_range()
        )
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert blocker.satisfies(VersionRange.singleton(V("1.0")))

    def test_range_block_widens_to_the_membership_complement(self) -> None:
        """The range-keyed blocker term widens the same way, past the captured
        positive range it started from."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=make_metadata("foo", "1.0", "bar>=5.0"),
            package="foo",
        )
        root_reqs = {"baz": SpecifierSet(">=1.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        pos_range = SpecifierSet("<2.0").to_range()
        provider.receive_partial_solution_hint({"bar": pos_range}, {})
        assert provider._look_ahead_ok("foo", V("1.0")) is False
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert blocker.constraint is not pos_range
        assert V("1.0") in blocker.constraint
        assert V("3.0") in blocker.constraint
        assert V("5.0") not in blocker.constraint

    def test_range_block_keeps_its_range_when_membership_misses_it(self) -> None:
        """The containment fence on the range family: a recorded union that
        overlaps the positive range leaves the captured object in place."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator, target=_PY312)
        blocker_range = SpecifierSet("<2.0").to_range()
        key = ("foo", "bar", blocker_range)
        provider.pending_range_blocks[key].append(V("1.0"))
        provider.pending_range_dep_ranges[key] = DepRangeUnion(
            1, SpecifierSet(">=1.0").to_range()
        )
        provider._flush_pending_blocks()
        blocker = provider.consume_pending_clauses()[0].terms[1]
        assert blocker.constraint is blocker_range

    def test_lookahead_clause_prunes_the_other_blocker_versions(self) -> None:
        """End to end: the widened clause rules out every bar version at once,
        so the scan of foo runs once instead of once per bar decision."""
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel(v) for v in ("12.0", "11.0", "10.0")],
                "bar": [make_wheel(v) for v in ("2.0", "1.0")],
            },
            metadata_by_version={
                "10.0": make_metadata("foo", "10.0", "bar>=9.0"),
                "11.0": make_metadata("foo", "11.0", "bar>=9.0"),
                "12.0": make_metadata("foo", "12.0", "bar>=9.0"),
                "1.0": make_metadata("bar", "1.0"),
                "2.0": make_metadata("bar", "2.0"),
            },
        )
        root_reqs = {
            "bar": VersionRange.full(admit_arbitrary=False),
            "foo": VersionRange.full(admit_arbitrary=False),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        with pytest.raises(ResolutionError):
            resolver.resolve(dict(root_reqs))
        # One scan of foo's three versions. A gap-widened blocker names only
        # bar 2.0, so bar 1.0 gets decided next and foo is scanned again.
        assert provider.stats.look_ahead_rejections == 3

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

        coordinator = make_coordinator(
            listings={
                "foo": [named_wheel("foo", "1.0")],
                "app": [named_wheel("app", "3.0")],
                "lib": [named_wheel("lib", "5.0"), named_wheel("lib", "9.0")],
            },
            metadata_by_version={
                "1.0": make_metadata("foo", "1.0", "lib==9.0"),
                "3.0": make_metadata("app", "3.0", "lib==5.0"),
                "5.0": make_metadata("lib", "5.0"),
                "9.0": make_metadata("lib", "9.0"),
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

    def test_full_resolve_terminates_on_an_arbitrary_equality_blocker(self) -> None:
        """An unsatisfiable ``===`` dependency reports cleanly, not by
        exhausting the iteration cap.

        Both foo versions pin ``bar===1.0.0`` while root holds bar below 2.0,
        so bar decides at 1.0 and the scan rejects both.  The widened blocker
        term has to stay satisfiable by that decision, or the clause never
        asserts and the resolver re-picks foo forever.
        """
        coordinator = make_coordinator(
            listings={
                "foo": [make_wheel("10.0"), make_wheel("9.0")],
                "bar": [make_wheel("2.0"), make_wheel("1.0")],
            },
            metadata_by_version={
                "10.0": make_metadata("foo", "10.0", "bar===1.0.0"),
                "9.0": make_metadata("foo", "9.0", "bar===1.0.0"),
                "2.0": make_metadata("bar", "2.0"),
                "1.0": make_metadata("bar", "1.0"),
            },
        )
        root_reqs = {
            "foo": VersionRange.full(admit_arbitrary=False),
            "bar": SpecifierSet("<2.0").to_range(),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(dict(root_reqs))

        assert "exceeded" not in str(exc_info.value)

    def test_full_resolve_report_spells_ranges_as_requirements(self) -> None:
        """A real failure report spells a dependency the way a user wrote it.

        The resolver runs with ``range_type=VersionRange``, which has no
        ``__str__``, so without the hook a ``==V`` dependency shows the debug
        repr and its internal ``AFTER_LOCALS`` boundary sentinel, and an
        unconstrained root requirement shows ``(-inf, +inf)``.
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

        coordinator = make_coordinator(
            listings={
                "foo": [named_wheel("foo", "1.0")],
                "app": [named_wheel("app", "3.0")],
                "lib": [named_wheel("lib", "5.0"), named_wheel("lib", "9.0")],
            },
            metadata_by_version={
                "1.0": make_metadata("foo", "1.0", "lib==9.0"),
                "3.0": make_metadata("app", "3.0", "lib==5.0"),
                "5.0": make_metadata("lib", "5.0"),
                "9.0": make_metadata("lib", "9.0"),
            },
        )
        root_reqs = {
            "foo": VersionRange.full(admit_arbitrary=False),
            "app": VersionRange.full(admit_arbitrary=False),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(
            provider,
            range_type=VersionRange,
            root_version="0",
            format_range=provider.format_range,
        )
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(dict(root_reqs))

        lines = str(exc_info.value).splitlines()
        assert "because all versions of foo depend on lib ==9.0" in lines
        assert "because your project depends on foo" in lines


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

    def test_should_abort_none_when_self_block_present(self) -> None:
        provider = self._provider()
        provider.pending_blocks[("foo", "bar", V("1.0"))].append(V("0.9"))
        provider.pending_self_blocks[
            (
                "foo",
                SpecifierSet("==5.0").to_range(),
                SpecifierSet(">=6.0").to_range(),
            )
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
        provider.pending_metadata_blocks["foo"][V("0.8")] = "metadata failure"
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
        provider.pending_metadata_blocks["other"][V("0.5")] = "x"
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
        After the cap the abort still fires but does not re-queue the
        force-backtrack target.
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

        # Fourth abort blaming the same blocker is past the cap; no re-queue.
        provider.choose_version("foo", VersionRange.full())
        assert provider.consume_force_backtrack_targets() == []


class TestConstraintNotBlamedWhenProbeAborts:
    """A transitive-conflict failure must not be blamed on a user constraint
    just because the causation probe's un-narrowed range is large enough to
    trip the look-ahead abort.

    ``record_no_versions`` labels a clause CONSTRAINT only when
    ``has_satisfying_version`` finds a version over the un-narrowed range.  When
    that range holds enough candidates that all conflict with one decided
    dependency, the monolithic-rejection abort returns an optimistic pick; the
    probe must not read that as "the constraint hid a usable version".
    """

    @staticmethod
    def _wheel(name: str, version: str) -> WheelFile:
        return WheelFile(
            filename=f"{name}-{version}-py3-none-any.whl",
            url=f"https://example.com/{name}-{version}-py3-none-any.whl",
            version=version,
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )

    def _resolve(self, constraint: str, foo_deps: dict[str, str]) -> str:
        listings = {
            "foo": [self._wheel("foo", v) for v in foo_deps],
            "bar": [self._wheel("bar", "90.0"), self._wheel("bar", "50.0")],
            "baz": [self._wheel("baz", "3.0")],
        }
        metadata = {
            v: (
                f"Metadata-Version: 2.1\nName: foo\nVersion: {v}\n"
                f"Requires-Dist: {dep}\n\n"
            )
            for v, dep in foo_deps.items()
        }
        metadata["90.0"] = "Metadata-Version: 2.1\nName: bar\nVersion: 90.0\n\n"
        metadata["50.0"] = "Metadata-Version: 2.1\nName: bar\nVersion: 50.0\n\n"
        metadata["3.0"] = (
            "Metadata-Version: 2.1\nName: baz\nVersion: 3.0\n"
            "Requires-Dist: bar==50.0\n\n"
        )
        coordinator = make_coordinator(listings=listings, metadata_by_version=metadata)
        root_reqs = {
            "foo": VersionRange.full(admit_arbitrary=False),
            "baz": SpecifierSet("==3.0").to_range(),
        }
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(
                root_reqs, constraints={"foo": SpecifierSet(constraint).to_range()}
            )
        return str(exc_info.value)

    def test_transitive_conflict_not_blamed_on_constraint(self) -> None:
        """Twelve foo versions leave eight in the probe range once the top four
        are excluded, so the abort fires inside the probe.

        Every foo needs ``bar==90.0`` but ``baz`` pins ``bar`` at ``50.0``, so
        the resolve fails on that transitive conflict with or without the
        constraint.  ``foo>=108.0`` clips only the top four versions, which also
        fail, so it is not causal and must not be named.
        """
        message = self._resolve(
            ">=108.0", {f"{100 + i}.0": "bar==90.0" for i in range(12)}
        )
        assert "the user constrained" not in message
        assert "no versions of foo" in message

    def test_constraint_that_hides_a_usable_version_is_still_blamed(self) -> None:
        """The suppression must not hide a usable version.

        ``foo==100.0`` needs ``bar==50.0`` (the pinned version), so it resolves
        without the constraint.  ``foo>=101.0`` excludes it; the probe scans
        past the eight blocked newer versions and still finds it, so the
        constraint keeps the blame.
        """
        foo_deps = {
            f"{100 + i}.0": ("bar==50.0" if i == 0 else "bar==90.0") for i in range(12)
        }
        message = self._resolve(">=101.0", foo_deps)
        assert "the user constrained foo" in message


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

    def test_date_only_upload_time_reaches_the_rewriting_parser(self) -> None:
        """A date-only ``Z`` time still parses, through the fallback, and is naive."""
        wheels = [
            make_wheel("1.0", upload_time="2024-01-01Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        provider = Provider(coordinator, uploaded_prior_to=cutoff)
        with pytest.raises(InvalidUploadTimeError, match="2024-01-01Z"):
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


class TestRequiresPythonMetadataGate:
    """The wheel METADATA Requires-Python gates a candidate the listing admits.

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

    def test_admits_equivalent_spelling(self) -> None:
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

    def test_unevaluable_marker_names_the_marker(self) -> None:
        """An override's undecidable marker stops the run, naming the marker."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(
                pkg_override(
                    "foo",
                    dependencies=(Requirement('dep-a; sys_platform ~= "linux"'),),
                ),
            ),
        )
        with pytest.raises(UnevaluableMarkerError, match='sys_platform ~= "linux"'):
            provider.get_dependencies("foo", V("1.0"))

    def test_prefetches_override_dependencies(self) -> None:
        # The override introduces dep-a, so its listing is background-fetched
        # speculatively even though foo itself skips the metadata fetch.
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            package_overrides=(
                pkg_override("foo", dependencies=(Requirement("dep-a>=1"),)),
            ),
        )
        provider.get_dependencies("foo", V("1.0"))
        assert ("dep-a", True) in coordinator.calls_to("request_listing")

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
        # A complete override short-circuits batch submission for the corrupt
        # wheel, so the scan reaches get_dependencies (skip-fetch) and resolves
        # from the override's deps rather than the corrupt metadata.
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
        assert not coordinator.calls_to("request_metadata")
        assert not coordinator.calls_to("request_metadata_batch")

    def test_prefetch_transitive_best_skips_pick_for_cached_deps(self) -> None:
        # The deps_cache guard runs before the artifact pick, so a candidate
        # with cached deps never reaches pick_dist_for_metadata.
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")], package="pkg"
        )
        provider = Provider(coordinator, target=_PY312)
        provider.deps_cache[("pkg", V("2.0"))] = {}

        with patch.object(
            listing_mod,
            "pick_dist_for_metadata",
            wraps=listing_mod.pick_dist_for_metadata,
        ) as pick:
            provider.fetch_versions("pkg")

        assert not pick.called
        assert not coordinator.calls_to("request_metadata")

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
        coordinator.reset()
        provider._prefetch_walk_ahead("pkg", _ANY_VERSION)
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

    def test_overrides_doc_names_every_source_kind(self) -> None:
        # The bare-name-only override rule covers local, VCS, and archive
        # sources alike, so the doc must name every kind.
        build_policy_doc = (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "reference"
            / "build-policy.md"
        )
        text = build_policy_doc.read_text()
        section = text[text.index("## Overrides") :].split("\n## ", 1)[0]
        paragraph = next(
            block for block in section.split("\n\n") if "bare name" in block
        ).lower()

        assert "local" in paragraph
        assert "vcs" in paragraph
        assert "archive" in paragraph


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
            wheels,
            metadata_by_version={
                "2.0": EXTRA_METADATA.replace("Version: 1.0", "Version: 2.0"),
                "1.0": EXTRA_METADATA,
            },
            package="foo",
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

    def test_choose_extra_version_blocks_a_prerelease_excluded_by_the_base(
        self,
    ) -> None:
        """The pre-release enumeration reads the same descending listing.

        The proxy range's bounds are wider than its opt-in region, so the
        default policy buffers 2.0a1 behind 2.0 and drops it; only the
        ``prereleases=True`` pass reaches the block recorder with it.
        """
        wheels = [make_wheel(v) for v in ("2.0", "2.0a1", "1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator)
        all_versions = provider.versions_only("foo", provider.fetch_versions("foo"))
        assert all_versions == sorted(all_versions, reverse=True)
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("<1.0").to_range()},
            {},
        )
        result = provider.choose_version(
            "foo[security]", SpecifierSet(">=1.0,<3.0").to_range()
        )
        assert result is None
        clauses = provider.consume_pending_clauses()
        proxy_terms = [
            t for c in clauses for t in c.terms if t.package == "foo[security]"
        ]
        assert any(V("2.0a1") in t.constraint for t in proxy_terms)

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

    @staticmethod
    def _user_extra_provider(
        extras_mode: ExtrasMode,
        root_spec: str = "",
        extra: str = "security",
    ) -> Provider:
        """A provider whose ``foo`` declares ``security`` only at 2.0.

        ``root_spec`` is the specifier the root requirement put on ``foo``.
        """
        wheels = [make_wheel(v) for v in ("3.0", "2.1", "2.0", "1.0")]
        coordinator = make_coordinator(
            wheels,
            package="foo",
            metadata_by_version={
                "3.0": make_metadata("foo", "3.0"),
                "2.1": make_metadata("foo", "2.1"),
                "2.0": make_metadata("foo", "2.0") + "Provides-Extra: security\n",
                "1.0": make_metadata("foo", "1.0"),
            },
        )
        return Provider(
            coordinator,
            extras_mode=extras_mode,
            root_extras={("foo", extra)},
            root_requirements={"foo": SpecifierSet(root_spec).to_range()},
        )

    @pytest.mark.parametrize(
        "extras_mode", [ExtrasMode.ERROR_USER, ExtrasMode.BACKTRACK]
    )
    def test_user_extra_reports_no_version_when_range_drops_the_provider(
        self, extras_mode: ExtrasMode
    ) -> None:
        """A narrowed range holding no provider of a user extra has no version.

        Pinning one would commit the resolver to a version the extra
        cannot be expanded from, aborting the resolve instead of
        backjumping off whatever narrowed the range.
        """
        provider = self._user_extra_provider(extras_mode)
        spec = SpecifierSet(">=2.1")
        assert provider.choose_version("foo[security]", spec.to_range()) is None

    def test_user_extra_keeps_the_first_version_over_an_older_provider(self) -> None:
        """A user extra pins the first in-range version, not an older provider."""
        provider = self._user_extra_provider(ExtrasMode.ERROR_USER)
        spec = SpecifierSet(">=2.0")
        assert provider.choose_version("foo[security]", spec.to_range()) == V("3.0")

    def test_user_extra_keeps_the_first_version_when_the_root_excludes_it(self) -> None:
        """A root range with no provider of its own stays the user's error.

        Pinning the first version reports the miss against it, rather
        than reporting no version and losing the error naming the extra.
        """
        provider = self._user_extra_provider(ExtrasMode.ERROR_USER, root_spec=">=2.1")
        spec = SpecifierSet(">=2.1")
        assert provider.choose_version("foo[security]", spec.to_range()) == V("3.0")

    def test_user_extra_keeps_the_first_version_when_no_version_declares_it(
        self,
    ) -> None:
        """An extra no version of the package declares stays the user's error."""
        provider = self._user_extra_provider(ExtrasMode.ERROR_USER, extra="nonexistent")
        spec = SpecifierSet(">=2.1")
        assert provider.choose_version("foo[nonexistent]", spec.to_range()) == V("3.0")

    def test_warn_mode_user_extra_keeps_the_first_version(self) -> None:
        """WARN mode pins the first in-range version whatever the range."""
        provider = self._user_extra_provider(ExtrasMode.WARN)
        spec = SpecifierSet(">=2.1")
        assert provider.choose_version("foo[security]", spec.to_range()) == V("3.0")

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

    def _coordinator_for_c(self) -> FakeFetchPort:
        listings = {"c": [make_wheel("2.0.0a1"), make_wheel("1.0.0")]}
        metadata = {v: self.C_META.format(v=v) for v in ("1.0.0", "2.0.0a1")}
        return make_coordinator(listings=listings, metadata_by_version=metadata)

    def _resolve(self, requirements: list[str]) -> dict[str, Version]:
        _, root_reqs, root_extras = build_resolver_inputs(
            [Requirement(r) for r in requirements],
            VcsConfig(),
            environment={},
            marker_holds=dependency_marker_holds,
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

    def _s2_coordinator(self, *, with_escape: bool) -> FakeFetchPort:
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
        self, coordinator: FakeFetchPort
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

    def test_prefer_wheel_wheels_before_sdists_under_overrides(self) -> None:
        """An override that leaves the policy alone keeps PREFER_WHEEL's order."""
        coordinator = make_coordinator(
            [make_sdist("1.0"), make_wheel("1.0")],
            metadata_text="Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n",
        )
        provider = Provider(
            coordinator,
            dist_policy=DistPolicy.PREFER_WHEEL,
            package_overrides=(pkg_override("pkg", provides_extra=("e",)),),
        )
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

    def test_sdist_install_drops_wheel_only_version(self) -> None:
        """A wheel-only version is not a candidate under SDIST_INSTALL.

        The newest release (2.0) publishes only a wheel, so it has no
        source to install and is dropped before the resolver sees it.
        The older 1.0 ships a wheel and an sdist, so it stays: the
        wheel as the cheap metadata source, sorted before the sdist.
        """
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0"), make_sdist("1.0")],
            metadata_text="Metadata-Version: 2.1\nName: pkg\nVersion: 2.0\n",
        )
        provider = Provider(coordinator, dist_policy=DistPolicy.SDIST_INSTALL)
        versions = provider.fetch_versions("pkg")
        assert [str(v) for v, _ in versions] == ["1.0", "1.0"]
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


class TestLadderSdistAvailability:
    """The metadata ladder tells the ways an sdist can be missing apart.

    A filtered sdist comes back by loosening the cutoff or the policy that
    dropped it, a ``.zip`` is a format nab declines to read, and a release
    that published none has nothing to loosen.  So the three must not read
    alike.

    Which rung dropped a filtered sdist is a walk of the whole listing, and
    look-ahead swallows this error, so the ladder marks the version and the
    report names the rung only once the resolve has failed.
    """

    _ABSENT = "no PEP 658 metadata and no sdist available"
    _FILTERED = "no PEP 658 metadata and the listing filter excluded the sdist"
    _ZIP = "no PEP 658 metadata and the sdist is a .zip, a format nab drops"

    def _named_line(self, provider: Provider, version: Version) -> str | None:
        """The line the report builds for a marked version, or ``None``."""
        diagnostic = provider.filtered_sdist_diagnostic("pkg", version)
        return None if diagnostic is None else diagnostic.short

    def _marker(self, provider: Provider) -> Version | None:
        """Resolve ``pkg`` 1.0's metadata and return the marker the ladder left."""
        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))
        assert self._FILTERED in str(excinfo.value)
        return excinfo.value.filtered_sdist_version

    @staticmethod
    def _with_zip_sdists(
        files: list[WheelFile | SdistFile], versions: frozenset[str]
    ) -> FakeFetchPort:
        """Serve ``files`` for pkg, with ``versions`` also published as ``.zip``."""
        coordinator = make_coordinator()
        coordinator.index.store_listing("pkg", files, zip_sdists=versions)
        return coordinator

    def test_upload_cutoff_filtered_sdist_names_the_filter(self) -> None:
        """A cutoff keeping the wheel and dropping the sdist names the filter."""
        coordinator = make_coordinator(
            [
                make_wheel(
                    "1.0", has_metadata=False, upload_time="2026-01-01T00:00:00Z"
                ),
                make_sdist("1.0", upload_time="2026-01-20T00:00:00Z"),
            ]
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            uploaded_prior_to=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )

        assert self._marker(provider) == V("1.0")
        assert self._named_line(provider, V("1.0")) == (
            "uploaded-prior-to excluded the sdist nab needed for metadata"
        )

    def test_requires_python_filtered_sdist_names_the_filter(self) -> None:
        """An sdist whose own Requires-Python excludes the target still exists."""
        coordinator = make_coordinator(
            [
                make_wheel("1.0", has_metadata=False),
                make_sdist("1.0", requires_python=">=3.13"),
            ]
        )
        provider = Provider(coordinator, target=_PY312)

        assert self._marker(provider) == V("1.0")
        assert self._named_line(provider, V("1.0")) == (
            "requires-python excluded the sdist nab needed for metadata"
        )

    def test_wheel_only_policy_filtered_sdist_names_the_filter(self) -> None:
        """The sdist a wheel-only policy dropped is not reported as absent."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False), make_sdist("1.0")]
        )
        provider = Provider(
            coordinator, target=_PY312, dist_policy=DistPolicy.WHEEL_ONLY
        )

        assert self._marker(provider) == V("1.0")
        assert self._named_line(provider, V("1.0")) == (
            'dist-policy = "wheel-only" excluded the sdist nab needed for metadata'
        )

    def test_a_host_side_drop_leaves_the_untargeted_sentence(self) -> None:
        """A drop no rung models names no filter rather than guessing at one.

        The host removes the sdist after the filter ran, so the index did
        publish one and the walk still refused nothing: there is no key to
        point the user at.
        """

        class DropsTheSdist(Provider):
            def filter_distributions(
                self,
                normalized: str,
                files: Sequence[WheelFile | SdistFile],
            ) -> list[tuple[Version, WheelFile | SdistFile]]:
                kept = super().filter_distributions(normalized, files)
                return [pair for pair in kept if not isinstance(pair[1], SdistFile)]

        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False), make_sdist("1.0")]
        )
        provider = DropsTheSdist(coordinator, target=_PY312)

        assert self._marker(provider) == V("1.0")
        assert self._named_line(provider, V("1.0")) is None

    def test_sdist_of_another_release_is_reported_as_absent(self) -> None:
        """Only this release's own sdist counts as one the filter dropped."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False), make_sdist("2.0")]
        )
        provider = Provider(coordinator, target=_PY312)

        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))

        assert self._ABSENT in str(excinfo.value)

    def test_unparseable_sdist_version_is_reported_as_absent(self) -> None:
        """A version nab cannot parse cannot be this release's sdist."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False), make_sdist("not-a-version")]
        )
        provider = Provider(coordinator, target=_PY312)

        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))

        assert self._ABSENT in str(excinfo.value)

    def test_zip_sdist_names_the_format_instead_of_denying_the_sdist(self) -> None:
        """A ``.zip`` sdist leaves no record, and must not read as absent.

        The repair differs: no sdist sends the user to another release or
        another index, while a ``.zip`` is a file nab declines to read.
        """
        coordinator = self._with_zip_sdists(
            [make_wheel("1.0", has_metadata=False)], frozenset({"1.0"})
        )
        provider = Provider(coordinator, target=_PY312)

        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))

        assert self._ZIP in str(excinfo.value)

    def test_zip_sdist_of_another_release_is_reported_as_absent(self) -> None:
        """Only this release's own ``.zip`` says anything about this release."""
        coordinator = self._with_zip_sdists(
            [make_wheel("1.0", has_metadata=False)], frozenset({"2.0"})
        )
        provider = Provider(coordinator, target=_PY312)

        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))

        assert self._ABSENT in str(excinfo.value)

    def test_zip_version_matches_on_release_not_on_spelling(self) -> None:
        """``1.0`` and ``1.0.0`` are one release, so the ``.zip`` still counts."""
        coordinator = self._with_zip_sdists(
            [make_wheel("1.0.0", has_metadata=False)], frozenset({"1.0"})
        )
        provider = Provider(coordinator, target=_PY312)

        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0.0"))

        assert self._ZIP in str(excinfo.value)

    def test_unparseable_zip_version_is_reported_as_absent(self) -> None:
        """A blob can carry any string, so a version that will not parse is skipped."""
        coordinator = self._with_zip_sdists(
            [make_wheel("1.0", has_metadata=False)], frozenset({"not-a-version"})
        )
        provider = Provider(coordinator, target=_PY312)

        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))

        assert self._ABSENT in str(excinfo.value)

    def test_a_re_stored_listing_replaces_the_zip_releases(self) -> None:
        """A second store speaks for the package, so an empty set clears it.

        A retry against another index can serve the same package without the
        ``.zip``, and the first store's releases must not outlive it.
        """
        files = [make_wheel("1.0", has_metadata=False)]
        coordinator = self._with_zip_sdists(files, frozenset({"1.0"}))
        coordinator.index.store_listing("pkg", files)
        provider = Provider(coordinator, target=_PY312)

        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))

        assert self._ABSENT in str(excinfo.value)

    def test_filtered_tar_gz_beside_a_zip_names_the_filter(self) -> None:
        """A filtered ``.tar.gz`` is the one the user can bring back."""
        coordinator = self._with_zip_sdists(
            [
                make_wheel("1.0", has_metadata=False),
                make_sdist("1.0", requires_python=">=3.13"),
            ],
            frozenset({"1.0"}),
        )
        provider = Provider(coordinator, target=_PY312)

        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))

        assert self._FILTERED in str(excinfo.value)


class TestFetchVersionsNotInIndex:
    def test_listing_not_in_index_blocks_on_request(self) -> None:
        """When listing is not in the index, request_listing + wait is used."""
        wheels = [make_wheel("1.0")]
        # The listing is absent until request_listing populates the index.
        coordinator = make_coordinator()
        index = coordinator.index

        def _request_listing(pkg: str, _speculative: bool) -> threading.Event:
            # Simulate the coordinator populating the index after request.
            index.store_listing(pkg, wheels)
            return _done_event()

        coordinator.override("request_listing", _request_listing)
        coordinator.override("request_metadata", lambda p, v, u, h=None: _done_event())
        coordinator.override(
            "request_metadata_batch",
            lambda items: [(p, v, _done_event()) for p, v, u, h in items],
        )

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
            provider, "_prefetch_walk_ahead", wraps=provider._prefetch_walk_ahead
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
        assert not coordinator.calls_to("request_metadata")


# The sidecar of ``make_wheel("1.0")``: the batch path reads the metadata
# back by the artifact it submitted.
_SIDECAR_URL = "https://example.com/pkg-1.0-py3-none-any.whl.metadata"


class TestAwaitMetadataBatchEdgeCases:
    def test_skips_versions_already_in_deps_cache(self) -> None:
        """Submitted entries already in ``deps_cache`` short-circuit the wait.

        Pre-empting the walk-ahead deep prefetch can land metadata for a
        version that the next pipelined batch also submits; by the time
        ``_await_metadata_batch`` runs, that version's deps are cached
        already and queuing it for another parse is wasted work.
        """
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        provider.deps_cache[("foo", V("1.0"))] = {"bar": VersionRange.full()}
        provider._await_metadata_batch(
            "foo",
            [(V("1.0"), "1.0", _SIDECAR_URL, _done_event())],
        )

        assert ("foo", V("1.0")) not in provider.pending_metadata_parses
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

    def test_batch_constants_only_marker_refuses_version(self) -> None:
        """A marker comparing two literals in the batch path refuses the version.

        A quoted left operand makes packaging read the right one as an
        environment key, so the clause fails only when the decode evaluates
        it. The batch leaves v2.0 un-cached and look-ahead's get_dependencies
        refuses it instead of the failure aborting the resolve, so the scan
        falls through to v1.0.
        """
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={
                "3.0": "Metadata-Version: 2.1\nName: foo\nVersion: 3.0\nRequires-Dist: bar>=5.0\n",
                "2.0": 'Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: pytz>=1; "extra" == "gpu"\n',
                "1.0": "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            },
            package="foo",
        )
        root_reqs = {"bar": SpecifierSet("<2.0").to_range()}
        provider = Provider(coordinator, target=_PY312, root_requirements=root_reqs)

        assert provider.choose_version("foo", SpecifierSet("").to_range()) == V("1.0")
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

    def test_batch_metadata_hash_mismatch_defers_to_get_dependencies(self) -> None:
        """A recorded integrity failure in the batch path leaves the version
        un-cached without aborting; get_dependencies re-raises it when the
        resolver actually selects this version."""
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        coordinator.index.store_metadata_error(
            "foo",
            "1.0",
            MetadataHashMismatchError("metadata sha256 mismatch"),
            _SIDECAR_URL,
        )
        provider = Provider(coordinator, target=_PY312)
        provider._await_metadata_batch(
            "foo",
            [(V("1.0"), "1.0", _SIDECAR_URL, _done_event())],
        )
        listing_mod.parse_prefetched_metadata(provider, ("foo", V("1.0")))

        assert ("foo", V("1.0")) not in provider.deps_cache
        with pytest.raises(MetadataHashMismatchError):
            provider.get_dependencies("foo", V("1.0"))

    def test_batch_hash_mismatch_on_non_selected_candidate_resolves(self) -> None:
        """A corrupt sidecar on a candidate the scan never selects must not abort.

        v3.0's deps conflict with a decision, so the scan prefetches the
        [v2.0, v1.0] batch.  v2.0 is clean and selected first; v1.0's sidecar
        is corrupt but never examined.  Integrity-checking it in the batch
        would crash the resolve over a version not in the solution.
        """
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={
                "3.0": "Metadata-Version: 2.1\nName: pkg\nVersion: 3.0\nRequires-Dist: bar==2.0\n",
                "2.0": "Metadata-Version: 2.1\nName: pkg\nVersion: 2.0\n",
            },
            package="pkg",
        )
        corrupt = make_wheel("1.0")
        assert corrupt.metadata_url is not None
        coordinator.index.store_metadata_error(
            "pkg",
            "1.0",
            MetadataHashMismatchError("metadata sha256 mismatch"),
            corrupt.metadata_url,
        )
        provider = Provider(coordinator, target=_PY312)
        provider.solution_decisions["bar"] = V("1.0")
        assert provider.choose_version("pkg", VersionRange.full()) == V("2.0")
        assert provider.deps_cache[("pkg", V("2.0"))] == {}
        assert ("pkg", V("1.0")) not in provider.deps_cache

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
        listing_mod.parse_prefetched_metadata(provider, ("pkg", V("1.0")))

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
        coordinator.index.store_metadata(
            "pkg", "1.0", None, metadata_url=wheel.metadata_url
        )

        version_list = provider.fetch_versions("pkg")
        wheel_map = provider._wheel_by_version("pkg", version_list)
        submitted = provider._prefetch_batch("pkg", [V("1.0")], wheel_map)
        provider._await_metadata_batch("pkg", submitted)
        listing_mod.parse_prefetched_metadata(provider, ("pkg", V("1.0")))

        assert ("pkg", V("1.0")) not in provider.deps_cache


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
        """The in-flight placeholder is not cached; the next scan recomputes."""
        coordinator = make_coordinator(None, package="foo")
        provider = Provider(coordinator)
        rng = SpecifierSet(">=1.0").to_range()
        provider.begin_decision_scan()
        assert provider.prioritize("foo", rng, {}) == (
            Provider.TIER_NORMAL,
            1000,
            True,
        )
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        # Store directly into the index, as the fetcher thread would.
        coordinator.index.store_listing("foo", wheels)
        provider.begin_decision_scan()
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

    def test_priority_cache_hit_skips_the_name_split(self) -> None:
        """A hit answers from the entry's own name, without re-splitting."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        intervals = SpecifierSet(">=1.0").to_range()
        first = provider.prioritize("foo", intervals, {}, {})

        provider._package_parts.clear()

        assert provider.prioritize("foo", intervals, {}, {}) == first
        assert provider._package_parts == {}

    def test_priority_cache_hit_yields_to_force_backtrack(self) -> None:
        """A force-backtrack recorded after the entry was cached still demotes."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        intervals = SpecifierSet(">=1.0").to_range()
        assert provider.prioritize("foo", intervals, {}, {})[0] == Provider.TIER_NORMAL

        provider._force_backtrack_counts["foo"] = 1
        demoted = provider.prioritize("foo", intervals, {}, {})

        assert demoted[0] == Provider.TIER_CULPRIT

    def test_priority_cache_hit_yields_to_dominant_culprit(self) -> None:
        """A package that becomes the dominant culprit after caching still demotes."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        intervals = SpecifierSet(">=1.0").to_range()
        assert provider.prioritize("foo", intervals, {}, {})[0] == Provider.TIER_NORMAL

        demoted = provider.prioritize("foo", intervals, {}, {"foo": 10})

        assert demoted[0] == Provider.TIER_CULPRIT

    def test_priority_cache_hit_yields_to_new_conflict_count(self) -> None:
        """A conflict count that reaches the threshold after caching still promotes."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        intervals = SpecifierSet(">=1.0").to_range()
        assert provider.prioritize("foo", intervals, {}, {})[0] == Provider.TIER_NORMAL

        promoted = provider.prioritize("foo", intervals, {"foo": 5}, {})

        assert promoted[0] == Provider.TIER_AFFECTED

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

    def test_matching_counts_one_entry_per_file(self) -> None:
        """versions_cache holds a row per file, so a dual-artifact release counts twice."""
        files = [make_wheel("2.0"), make_sdist("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(files, package="foo")
        provider = Provider(coordinator)
        version_range = SpecifierSet(">=2.0").to_range()
        result = provider.prioritize("foo", version_range, {})
        cached = provider.versions_cache["foo"]
        assert result[1] == sum(1 for v, _ in cached if v in version_range)
        assert result[1] == 2

    def test_matching_counts_an_in_bounds_prerelease(self) -> None:
        """The count admits an in-bounds pre-release the default policy would drop.

        The range's bounds are wider than its pre-release opt-in region, so the
        default policy buffers 2.0a1 behind the matching final release and drops
        it; only the admitting policy counts all three.
        """
        wheels = [make_wheel(v) for v in ("2.0", "2.0a1", "1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        version_range = SpecifierSet(">=1.0,<3.0").to_range()
        result = provider.prioritize("foo", version_range, {})
        assert V("2.0a1") in version_range
        assert result[1] == 3

    def test_versions_only_cache_hit(self) -> None:
        """Calling versions_only twice returns the same cached list."""
        wheels = [make_wheel(v) for v in ("1.0", "2.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        version_list = provider.fetch_versions("foo")
        first = provider.versions_only("foo", version_list)
        second = provider.versions_only("foo", version_list)
        assert first is second

    def test_versions_only_is_descending_whatever_the_index_order(self) -> None:
        """The listing view is newest-first, which choose_version's filter asserts.

        ``filter(assume_sorted="descending")`` bisects the view rather than
        testing every entry, so an index listing files in any other order must
        still reach the provider sorted.
        """
        wheels = [make_wheel(v) for v in ("1.0", "3.0", "1.0a1", "2.0", "0.9")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        versions = provider.versions_only("foo", provider.fetch_versions("foo"))
        assert versions == sorted(versions, reverse=True)
        assert versions[0] == V("3.0")

    def test_choose_version_candidates_match_the_entry_wise_filter(self) -> None:
        """The bisected candidate list equals what the plain filter yields."""
        wheels = [make_wheel(v) for v in ("0.9", "1.0a1", "1.0", "1.5", "2.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        version_list = provider.fetch_versions("foo")
        all_versions = provider.versions_only("foo", version_list)
        for spec in ("", ">=1.0", ">=1.0,<2.0", "!=1.0", ">=1.0a1", "===1.0"):
            version_range = SpecifierSet(spec).to_range()
            bisected = version_range.filter(all_versions, assume_sorted="descending")
            assert list(bisected) == list(version_range.filter(all_versions))

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


_MATRIX_ORDERS = [
    ("windows_amd64", "linux_x86_64"),
    ("linux_x86_64", "windows_amd64"),
]


def _declared_target(platform: str) -> ResolveTarget:
    """A declared 3.12 target for one platform."""
    return ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec(platform)
    )


def _wheelhouse_wheel(
    directory: Path, *reqs: str, dist_info: str = "pkg-1.0"
) -> WheelFile:
    """Write a linux-only 1.0 wheel to disk, listed as a flat wheelhouse would.

    A flat wheelhouse serves no PEP 658 sidecar, so ``has_metadata`` is false
    and the METADATA lives only inside the ``.whl``.  A ``dist_info`` naming
    another distribution makes that METADATA unreadable.
    """
    filename = "pkg-1.0-py3-none-manylinux_2_17_x86_64.whl"
    path = directory / filename
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            f"{dist_info}.dist-info/METADATA", make_metadata("pkg", "1.0", *reqs)
        )

    return WheelFile(
        filename=filename,
        url=path.as_uri(),
        version="1.0",
        requires_python=None,
        has_metadata=False,
        upload_time=None,
        local_path=path,
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

    @pytest.mark.parametrize("order", _MATRIX_ORDERS)
    def test_a_wheelhouse_wheel_reads_its_own_metadata(
        self, tmp_path: Path, order: tuple[str, str]
    ) -> None:
        """A find-links wheel is not served a sibling target's PKG-INFO.

        A flat wheelhouse publishes no sidecar, and the windows target, whose
        only artifact is the sdist, fills the version-level slot the matrix
        shares.  Either order leaves linux its own wheel's dependencies.
        """
        wheel = _wheelhouse_wheel(tmp_path, "wheel-dep")
        pkg_info = (
            "Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\nRequires-Dist: sdist-dep\n"
        )
        coordinator = make_coordinator(
            [wheel, make_sdist("1.0")], sdist_pkg_info=pkg_info
        )

        deps = {
            platform: Provider(
                coordinator, target=_declared_target(platform)
            ).get_dependencies("pkg", V("1.0"))
            for platform in order
        }

        assert "sdist-dep" in deps["windows_amd64"]
        assert "wheel-dep" in deps["linux_x86_64"]
        assert "sdist-dep" not in deps["linux_x86_64"]

    @pytest.mark.parametrize("order", _MATRIX_ORDERS)
    def test_an_unreadable_wheelhouse_wheel_falls_back_to_pkg_info(
        self, tmp_path: Path, order: tuple[str, str]
    ) -> None:
        """A wheel with no usable METADATA of its own still reads the sdist's.

        Its ``.dist-info`` names another distribution, so nothing answers for
        the wheel and the version-level PKG-INFO stands for the version.
        """
        wheel = _wheelhouse_wheel(tmp_path, "wheel-dep", dist_info="other-9.9")
        pkg_info = (
            "Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\nRequires-Dist: sdist-dep\n"
        )
        coordinator = make_coordinator(
            [wheel, make_sdist("1.0")], sdist_pkg_info=pkg_info
        )

        deps = {
            platform: Provider(
                coordinator, target=_declared_target(platform)
            ).get_dependencies("pkg", V("1.0"))
            for platform in order
        }

        assert "sdist-dep" in deps["linux_x86_64"]
        assert "wheel-dep" not in deps["linux_x86_64"]

    @pytest.mark.parametrize("order", _MATRIX_ORDERS)
    def test_a_wheelhouse_wheel_survives_an_unbuildable_sdist(
        self, tmp_path: Path, order: tuple[str, str]
    ) -> None:
        """A target holding an installable wheel is not refused with the sdist.

        The sdist's pre-PEP-643 PKG-INFO has to go through the dynamic-deps
        path, which the default build policy refuses.  That refusal belongs to
        the target with no wheel; the target that installs the wheelhouse wheel
        reads its METADATA off disk and keeps the version.
        """
        wheel = _wheelhouse_wheel(tmp_path, "wheel-dep")
        coordinator = make_coordinator(
            [wheel, make_sdist("1.0")], sdist_pkg_info=PKG_INFO_PRE_PEP643_DEPS
        )

        for platform in order:
            provider = Provider(coordinator, target=_declared_target(platform))
            if platform == "windows_amd64":
                with pytest.raises(UnsupportedSdistError):
                    provider.get_dependencies("pkg", V("1.0"))
            else:
                assert "wheel-dep" in provider.get_dependencies("pkg", V("1.0"))


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
        :attr:`~nab_provider.provider.DistPolicy.SDIST_INSTALL`: those
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

    def test_prefers_the_wheel_the_tags_rank_most_specific(self) -> None:
        """The tags rank the siblings; listing order does not.

        An abi3 wheel published beside a full-ABI one is a routine
        layout, and the target installs the full-ABI one.
        """
        tags = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_x86_64")
        ).tags
        abi3 = make_tagged_wheel("cp312-abi3-manylinux_2_17_x86_64")
        full_abi = make_tagged_wheel("cp312-cp312-manylinux_2_17_x86_64")
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
        no_sidecar = make_tagged_wheel("py2.py3-none-any", has_metadata=False)
        sidecar = make_tagged_wheel("py3-none-any")
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

    @pytest.mark.parametrize("offline", [False, True])
    def test_dynamic_sdist_path_under_build_remote_invokes_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
        offline: bool,
    ) -> None:
        """``BUILD_REMOTE`` routes a dynamic-deps sdist through the build path.

        Under ``BUILD_LOCAL`` (or no override at NEVER) the path raises
        :class:`UnsupportedSdistError`; under the ``BUILD_REMOTE``
        override the archive is fetched, extracted, and handed to the
        build backend (mocked here) along with the run's ``--offline``
        flag.
        """
        from nab_provider._vendor.packaging.version import Version as _Version

        archive_bytes = b"sdist-archive-bytes"
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            package="pkg",
            sdist_archive=archive_bytes,
            offline=offline,
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

        monkeypatch.setattr(_build_remote, "extract_sdist_archive", fake_extract)
        monkeypatch.setattr("nab_project.build_backend.extract_metadata", fake_build)

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
        assert captured["kwargs"] == {
            "config": coordinator.build_config,
            "offline": offline,
        }

    def test_resolve_dynamic_sdist_reuses_cross_tuple_cache(self) -> None:
        """A second call for the same sdist returns the cached metadata.

        The ``InMemoryIndex._resolved_sdist_metadata`` cache is the
        backstop that stops universal mode from re-augmenting (or, more
        importantly, re-building) the same sdist for every tuple.  The
        cache key is the canonical name + version string.
        """
        from nab_provider._vendor.packaging.version import Version as _Version

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

    def test_dynamic_sdist_diagnostic_names_config_build_policy(self) -> None:
        """The build-policy hint names the config token, not the enum symbol."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
        )
        with pytest.raises(UnsupportedSdistError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))
        msg = str(excinfo.value)
        assert "build-policy 'build-remote'" in msg
        assert "effective policy is 'build-local'" in msg
        assert "BuildPolicy." not in msg

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
        """Optional-dependencies in dynamic blocks even a static dependencies list."""
        pyproject = (
            "[project]\n"
            'name = "pkg"\n'
            'version = "1.0"\n'
            "dependencies = ['dep-a>=1.0']\n"
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
            sdist_archive=b"sdist-archive",
        )

        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=None,
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        monkeypatch.setattr(
            _build_remote, "extract_sdist_archive", lambda _data, target: target
        )
        monkeypatch.setattr(
            "nab_project.build_backend.extract_metadata", lambda *_a, **_k: built
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
            sdist_archive_error=SdistHashMismatchError("sdist sha256 mismatch"),
        )

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

    def test_build_remote_archive_http_error_aborts_get_dependencies(
        self,
    ) -> None:
        """A build-remote archive HTTP error aborts get_dependencies, not skips."""
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_archive_error=HttpError("503 Server Error fetching sdist archive"),
        )

        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.BUILD_REMOTE,
        )
        with pytest.raises(HttpError):
            provider.get_dependencies("pkg", V("1.0"))
        assert ("pkg", V("1.0")) not in provider.deps_cache
        assert ("pkg", V("1.0")) not in provider._invalid_metadata

    def test_build_remote_archive_local_index_error_aborts_get_dependencies(
        self,
    ) -> None:
        """A local index that cannot serve the archive aborts, not skips.

        A local failure is not an HTTP error, so the hard-error arm has to name
        the family both backends share; otherwise a wheelhouse that goes
        unreadable mid-resolve is cached as a bad-metadata skip.
        """
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_archive_error=UnreadableLocalIndexError(
                "cannot read local sdist /wheelhouse/pkg-1.0.tar.gz:"
                " [Errno 13] Permission denied"
            ),
        )

        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.BUILD_REMOTE,
        )
        with pytest.raises(UnreadableLocalIndexError):
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
            sdist_archive=b"sdist-archive-bytes",
        )

        def _naive_build(_path: object, **_kwargs: object) -> WheelMetadata:
            raise InvalidUploadTimeError(
                "setuptools 68.0.0 has a timezone-naive upload time"
                " '2025-06-01T00:00:00'; the Simple API requires"
                " timezone-aware (UTC) upload times"
            )

        monkeypatch.setattr(
            _build_remote, "extract_sdist_archive", lambda data, target: target
        )
        monkeypatch.setattr("nab_project.build_backend.extract_metadata", _naive_build)

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
            hashes: tuple[tuple[str, str], ...],
        ) -> threading.Event:
            coordinator.index.store_sdist_metadata(
                pkg,
                ver,
                PKG_INFO_DYNAMIC_DEPS.replace("Version: 1.0", f"Version: {ver}"),
            )
            coordinator.index.store_sdist_pyproject(pkg, ver, None)
            return _done_event()

        coordinator.override("request_sdist", _request_sdist)

        root_reqs = {"pkg": SpecifierSet(">=0").to_range()}
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            root_requirements=root_reqs,
        )
        chosen = provider.choose_version("pkg", root_reqs["pkg"])
        assert chosen == V("1.0")


class _CleanupErrorTemporaryDirectory:
    """Raise ``PermissionError`` unless cleanup errors are ignored."""

    def __init__(self, *, prefix: str, ignore_cleanup_errors: bool = False) -> None:
        self.name = f"/tmp/{prefix}in-use"
        self._ignore_cleanup_errors = ignore_cleanup_errors

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *_args: object) -> None:
        if not self._ignore_cleanup_errors:
            raise PermissionError("build source is still in use")


class TestBuildRemoteFailureModes:
    """Failure paths in :func:`nab_provider._provider.build_remote.build_remote_sdist`.

    A skippable failure surfaces as :class:`UnsupportedSdistError`, which
    look-ahead either skips or folds into the eventual no-versions
    diagnostic. An integrity failure propagates instead.
    """

    def _provider(
        self,
        *,
        with_sdist: bool,
        overrides: tuple[PackageOverride, ...] = (),
        target: ResolveTarget = _PY312,
        build_config: ResolveInputs | None = None,
        sdist_archive: bytes | None = None,
        sdist_archive_error: BaseException | None = None,
        offline: bool = False,
    ) -> Provider:
        """A ``BUILD_REMOTE`` provider whose port serves the archive it is given.

        With neither archive keyword the port writes nothing.
        """
        files = [make_sdist("1.0")] if with_sdist else [make_wheel("1.0")]
        coordinator = make_coordinator(
            files,
            package="pkg",
            sdist_archive=sdist_archive,
            sdist_archive_error=sdist_archive_error,
            build_config=build_config,
            offline=offline,
        )
        return Provider(
            coordinator,
            target=target,
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
        """A fetch that produced no bytes raises ``UnsupportedSdistError``."""
        provider = self._provider(with_sdist=True)
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        with pytest.raises(UnsupportedSdistError, match="archive.*fetch.*failed"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_archive_hash_mismatch_aborts(self) -> None:
        """A tampered archive raises the integrity error from the build.

        The error must propagate, not degrade to ``UnsupportedSdistError``,
        which the resolve treats as a skippable version.
        """
        provider = self._provider(
            with_sdist=True,
            sdist_archive_error=SdistHashMismatchError("sdist sha256 mismatch"),
        )
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        with pytest.raises(SdistHashMismatchError):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    @respx.mock
    def test_archive_hash_checked_over_real_fetch(self) -> None:
        """A tampered archive is refused against the listing's published sha256.

        Fetches for real, so the refusal only happens if the provider
        forwards the listing's hashes.
        """
        sdist = make_sdist("1.0", hashes=(("sha256", "0" * 64),))
        respx.get(sdist.url).mock(
            return_value=httpx.Response(200, content=b"tampered bytes")
        )

        with FetchCoordinator(transport=HttpxAsyncTransport()) as coordinator:
            provider = Provider(
                coordinator,
                target=_PY312,
                dist_policy=DistPolicy.WHEEL_OR_SDIST,
                build_policy=BuildPolicy.BUILD_REMOTE,
            )
            provider.versions_cache["pkg"] = [(V("1.0"), sdist)]
            with pytest.raises(SdistHashMismatchError, match="0" * 64):
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
        provider = self._provider(with_sdist=True, sdist_archive=data)
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        with pytest.raises(UnsupportedSdistError, match="could not be extracted"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_unwritable_temp_filesystem_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A temp directory nab cannot create raises ``UnsupportedSdistError``."""
        provider = self._provider(with_sdist=True, sdist_archive=b"data")
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        real = _build_remote.tempfile.TemporaryDirectory

        def _no_space(*args: Any, **kwargs: Any) -> object:
            if kwargs.get("prefix") == "nab-build-remote-":
                raise OSError(28, "No space left on device")
            return real(*args, **kwargs)

        monkeypatch.setattr(_build_remote.tempfile, "TemporaryDirectory", _no_space)

        with pytest.raises(
            UnsupportedSdistError,
            match="could not create a temporary build directory",
        ):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_build_backend_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nab_project import build_backend

        provider = self._provider(with_sdist=True, sdist_archive=b"data")
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        monkeypatch.setattr(
            _build_remote, "extract_sdist_archive", lambda _d, target: target
        )

        def _boom(*_a: object, **_k: object) -> None:
            raise build_backend.BuildBackendError("backend explosion")

        monkeypatch.setattr("nab_project.build_backend.extract_metadata", _boom)
        with pytest.raises(UnsupportedSdistError, match="backend explosion"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    @requires_data_filter
    def test_offline_coordinator_refuses_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An offline run refuses the build instead of fetching build requirements."""

        def _no_network(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("offline must not reach the network")

        monkeypatch.setattr("nab_project.resolve.resolve_for_targets", _no_network)

        provider = self._provider(
            with_sdist=True,
            build_config=ResolveInputs(),
            sdist_archive=_DYNAMIC_SDIST_TARGZ,
            offline=True,
        )
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        with pytest.raises(UnsupportedSdistError, match="offline mode"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def test_find_sdist_skips_non_matching_versions(self) -> None:
        provider = self._provider(with_sdist=True, sdist_archive=b"data")
        # Two versions, only 2.0 has an sdist.
        provider.versions_cache["pkg"] = [
            (V("1.0"), make_wheel("1.0")),
            (V("2.0"), make_sdist("2.0")),
        ]

        # Asking for 1.0 (wheel-only) raises.
        with pytest.raises(UnsupportedSdistError, match="no sdist is available"):
            build_remote.build_remote_sdist(provider, "pkg", V("1.0"))

    def _build_into(
        self,
        monkeypatch: pytest.MonkeyPatch,
        built: object,
        *,
        overrides: tuple[PackageOverride, ...] = (),
        target: ResolveTarget = _PY312,
    ) -> Provider:
        provider = self._provider(
            with_sdist=True,
            overrides=overrides,
            target=target,
            sdist_archive=b"data",
        )
        provider.versions_cache["pkg"] = [(V("1.0"), make_sdist("1.0"))]

        monkeypatch.setattr(
            _build_remote, "extract_sdist_archive", lambda _d, target: target
        )
        monkeypatch.setattr(
            "nab_project.build_backend.extract_metadata", lambda *_a, **_k: built
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

    @pytest.mark.parametrize("cancel", [False, True], ids=["success", "cancel"])
    def test_cleanup_error_does_not_replace_result_or_cancellation(
        self, monkeypatch: pytest.MonkeyPatch, *, cancel: bool
    ) -> None:
        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=None,
            requires_dist=[],
            provides_extra=[],
        )
        provider = self._build_into(monkeypatch, built)
        monkeypatch.setattr(
            _build_remote.tempfile,
            "TemporaryDirectory",
            _CleanupErrorTemporaryDirectory,
        )

        if cancel:

            def interrupt_extraction(*args: object, **kwargs: object) -> NoReturn:
                raise KeyboardInterrupt

            monkeypatch.setattr(
                "nab_project.build_backend.extract_metadata",
                interrupt_extraction,
            )

            with pytest.raises(KeyboardInterrupt):
                build_remote.build_remote_sdist(provider, "pkg", V("1.0"))
        else:
            assert build_remote.build_remote_sdist(provider, "pkg", V("1.0")) is built

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
        provider.target = None
        result = build_remote.build_remote_sdist(provider, "pkg", V("1.0"))
        assert result is built

    def test_prerelease_target_admits_matching_requires_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=SpecifierSet(">=3.14"),
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        provider = self._build_into(
            monkeypatch, built, target=ResolveTarget.for_host_python("3.14.0rc1")
        )
        result = build_remote.build_remote_sdist(provider, "pkg", V("1.0"))
        assert result is built

    def test_bare_minor_target_admits_micro_floor_requires_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=SpecifierSet(">=3.9.2"),
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        provider = self._build_into(
            monkeypatch, built, target=ResolveTarget.for_host_python("3.9")
        )
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

    def test_cached_rejection_repeats_original_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coordinator = make_coordinator(
            [make_sdist("1.0")],
            sdist_pkg_info=PKG_INFO_DYNAMIC_DEPS,
            sdist_archive=b"data",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.BUILD_REMOTE,
        )

        monkeypatch.setattr(
            _build_remote, "extract_sdist_archive", lambda _d, target: target
        )

        built = WheelMetadata(
            name="pkg",
            version=V("1.0"),
            requires_python=SpecifierSet(">=3.13"),
            requires_dist=[Requirement("dep-a>=1")],
            provides_extra=[],
        )
        monkeypatch.setattr(
            "nab_project.build_backend.extract_metadata", lambda *_a, **_k: built
        )

        with pytest.raises(UnsupportedSdistError) as first:
            provider.get_dependencies("pkg", V("1.0"))
        with pytest.raises(UnsupportedSdistError) as second:
            provider.get_dependencies("pkg", V("1.0"))

        assert "built sdist requires Python >=3.13" in str(first.value)
        assert str(second.value) == str(first.value)


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
        from nab_provider._provider.priority import (
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
            sdist_pkg_info=PRE_22_SDIST_PKG_INFO.replace(
                "Name: pkg\nVersion: 1.0", "Name: foo\nVersion: 2.0"
            ),
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
    def _coordinator(requires_dist: str) -> FakeFetchPort:
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
        # _PY312 pins python_version to 3.12, so this marker holds on every
        # host the suite runs on, unlike a platform marker like sys_platform.
        provider = Provider(
            self._coordinator('bar ; python_version == "3.12"'), target=_PY312
        )
        deps = provider.get_dependencies("foo", V("1.0"))
        assert "bar" in deps
        assert provider.consulted_markers == {Marker('python_version == "3.12"')}

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


def _sib_wheel(
    tag: str, *, has_metadata: bool = True, local_path: Path | None = None
) -> WheelFile:
    """A pkg 1.0 wheel carrying an explicit tag, one url per filename."""
    filename = f"pkg-1.0-{tag}.whl"
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version="1.0",
        requires_python=None,
        has_metadata=has_metadata,
        upload_time=None,
        local_path=local_path,
    )


def _sib_meta(*requires_dist: str, provides_extra: tuple[str, ...] = ()) -> str:
    """A METADATA blob for pkg 1.0 with the given Requires-Dist lines."""
    head = "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
    extras = "".join(f"Provides-Extra: {name}\n" for name in provides_extra)
    body = "".join(f"Requires-Dist: {line}\n" for line in requires_dist)
    return f"{head}{extras}{body}\n"


class TestSiblingMetadataDivergence:
    """A version whose tie-ranked wheels declare different target deps crashes.

    nab reads one wheel's deps per version and treats it as authoritative.
    Two wheels an installer's own rules cannot rank against each other can
    still declare different dependencies, so pinning from one silently
    disagrees with an install of the other.  The check compares only siblings
    already resident in the shared index and never fetches.
    """

    _V = V("1.0")

    def test_divergent_tie_siblings_crash(self) -> None:
        """Two resident tie siblings with different deps crash naming both."""
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("common>=1", "alpha>=1"),
            metadata_url=wheel_a.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("common>=1", "beta>=1"),
            metadata_url=wheel_b.metadata_url,
        )
        provider = Provider(coordinator, target=_LINUX311)
        with pytest.raises(SiblingMetadataDivergenceError) as exc:
            provider.get_dependencies("pkg", self._V)
        message = str(exc.value)
        assert "pkg-1.0-py2.py3-none-any.whl" in message
        assert "pkg-1.0-py3-none-any.whl" in message
        assert "alpha" in message
        assert "beta" in message
        # A hard crash, not an invalid-metadata candidate drop.
        assert ("pkg", self._V) not in provider._invalid_metadata

    def test_check_issues_no_fetch(self) -> None:
        """The check reads stored text only; it never requests a fetch."""
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("beta>=1"), metadata_url=wheel_b.metadata_url
        )
        provider = Provider(coordinator, target=_LINUX311)
        with pytest.raises(SiblingMetadataDivergenceError):
            metadata_resolver.check_sibling_metadata_divergence(
                provider,
                [(self._V, wheel_a), (self._V, wheel_b)],
                "pkg",
                self._V,
            )
        assert not coordinator.calls_to("request_metadata")
        assert not coordinator.calls_to("request_range_metadata")

    def test_agreeing_tie_siblings_no_crash(self) -> None:
        """Reordered, whitespace-different siblings project equal, so no crash.

        Three tie wheels (no tag axis) exercise the pick-signature reuse across
        more than one resident sibling.
        """
        wheel_a = _sib_wheel("py3-none-any")
        wheel_b = _sib_wheel("py2.py3-none-any")
        wheel_c = _sib_wheel("cp311-cp311-manylinux_2_17_x86_64")
        coordinator = make_coordinator([wheel_a, wheel_b, wheel_c], package="pkg")
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("common>=1", "alpha>=1"),
            metadata_url=wheel_a.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("alpha>=1", "common>=1"),
            metadata_url=wheel_b.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("common >= 1", "alpha >= 1"),
            metadata_url=wheel_c.metadata_url,
        )
        provider = Provider(coordinator, target=None)
        deps = provider.get_dependencies("pkg", self._V)
        assert "alpha" in deps
        assert "common" in deps

    def test_benign_marker_variation_no_crash(self) -> None:
        """Markers that both evaluate the same for the target do not diverge."""
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta('alpha; python_version >= "3"'),
            metadata_url=wheel_a.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta('alpha; python_version >= "2"'),
            metadata_url=wheel_b.metadata_url,
        )
        provider = Provider(coordinator, target=_LINUX311)
        deps = provider.get_dependencies("pkg", self._V)
        assert "alpha" in deps

    def test_non_tie_multiwheel_no_crash(self) -> None:
        """A specific wheel outranks the generic sibling, so divergence is fine."""
        specific = _sib_wheel("cp311-cp311-manylinux_2_17_x86_64")
        generic = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([specific, generic], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("mldep>=1"), metadata_url=specific.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("gendep>=1"), metadata_url=generic.metadata_url
        )
        provider = Provider(coordinator, target=_LINUX311)
        deps = provider.get_dependencies("pkg", self._V)
        assert "mldep" in deps
        assert "gendep" not in deps

    def test_one_wheel_version_returns_early(self) -> None:
        """A version with a single wheel never reaches the sibling compare."""
        wheel = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=wheel.metadata_url
        )
        provider = Provider(coordinator, target=_LINUX311)
        deps = provider.get_dependencies("pkg", self._V)
        assert "alpha" in deps

    def test_sdist_pick_returns_early(self) -> None:
        """An sdist pick is not a wheel, so the check returns without reading."""
        provider = Provider(make_coordinator(package="pkg"), target=_LINUX311)
        metadata_resolver.check_sibling_metadata_divergence(
            provider, [(self._V, make_sdist("1.0"))], "pkg", self._V
        )

    def test_local_wheel_pick_returns_early(self, tmp_path: Path) -> None:
        """A local wheel pick is skipped, so its divergent tie sibling is ignored.

        Only text the check treats as resident is compared, and a local
        wheel's METADATA is not, so the resolve answers with the local
        wheel's own deps.
        """
        wheel_path = tmp_path / "pkg-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as zf:
            zf.writestr("pkg-1.0.dist-info/METADATA", _sib_meta("alpha>=1"))

        local = _sib_wheel("py3-none-any", has_metadata=False, local_path=wheel_path)
        sibling = _sib_wheel("py2.py3-none-any", has_metadata=False)

        coordinator = make_coordinator([local, sibling], package="pkg")
        # A sidecar-less wheel's resident text sits under the wheel's own url.
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("beta>=1"), metadata_url=sibling.url
        )
        provider = Provider(coordinator, target=_LINUX311)

        deps = provider.get_dependencies("pkg", self._V)

        assert "alpha" in deps
        assert "beta" not in deps

    def test_no_tag_axis_divergent_siblings_crash(self) -> None:
        """With no tag axis every resident sibling wheel is a real ambiguity."""
        wheel_a = _sib_wheel("cp311-cp311-manylinux_2_17_x86_64")
        wheel_b = _sib_wheel("cp311-cp311-macosx_11_0_arm64")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("beta>=1"), metadata_url=wheel_b.metadata_url
        )
        provider = Provider(coordinator, target=None)
        with pytest.raises(SiblingMetadataDivergenceError) as exc:
            provider.get_dependencies("pkg", self._V)
        message = str(exc.value)
        assert "alpha" in message
        assert "beta" in message

    def test_foreign_named_sibling_skipped(self) -> None:
        """A tie sibling declaring another project is not compared at all.

        Its deps differ, but it is not a candidate for pkg 1.0, so the pick's
        deps stand instead of aborting the resolve.
        """
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("beta>=1").replace("Name: pkg", "Name: other"),
            metadata_url=wheel_b.metadata_url,
        )
        provider = Provider(coordinator, target=_LINUX311)
        deps = provider.get_dependencies("pkg", self._V)
        assert "alpha" in deps
        assert "beta" not in deps

    def test_absent_sibling_text_skipped(self) -> None:
        """A tie sibling with no resident text is skipped, not fetched."""
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=wheel_a.metadata_url
        )
        provider = Provider(coordinator, target=_LINUX311)
        metadata_resolver.check_sibling_metadata_divergence(
            provider, [(self._V, wheel_a), (self._V, wheel_b)], "pkg", self._V
        )

    def test_sibling_sdist_origin_skipped(self) -> None:
        """Sdist PKG-INFO never stands in for a tie sibling wheel's own text."""
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", None, metadata_url=wheel_b.metadata_url
        )
        coordinator.index.store_sdist_metadata("pkg", "1.0", _sib_meta("sdistdep>=1"))
        provider = Provider(coordinator, target=_LINUX311)
        metadata_resolver.check_sibling_metadata_divergence(
            provider, [(self._V, wheel_a), (self._V, wheel_b)], "pkg", self._V
        )

    def test_incompatible_tag_sibling_skipped(self) -> None:
        """A sibling the target cannot install has no rank, so it never ties."""
        pick = _sib_wheel("py3-none-any")
        incompatible = _sib_wheel("cp311-cp311-win_amd64")
        coordinator = make_coordinator([pick, incompatible], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=pick.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("beta>=1"), metadata_url=incompatible.metadata_url
        )
        provider = Provider(coordinator, target=_LINUX311)
        metadata_resolver.check_sibling_metadata_divergence(
            provider, [(self._V, pick), (self._V, incompatible)], "pkg", self._V
        )

    def test_python_incompatible_sibling_skipped(self) -> None:
        """A sibling the target's Python excludes never ties, so no false crash.

        Two pure-python wheels tie on tags, but the py3 sibling's own METADATA
        Requires-Python excludes the 3.9 target, so an installer would reject
        it.  Comparing it would crash on a version an installer resolves fine.
        """
        target = ResolveTarget.for_declared(
            python_version="3.9", spec=PlatformSpec("linux_x86_64")
        )
        pick = _sib_wheel("py2.py3-none-any")
        incompatible = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([pick, incompatible], package="pkg")
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            "Requires-Python: >=3.8\nRequires-Dist: alpha>=1\n\n",
            metadata_url=pick.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            "Requires-Python: >=3.11\nRequires-Dist: alpha>=1\n"
            "Requires-Dist: beta>=1\n\n",
            metadata_url=incompatible.metadata_url,
        )
        provider = Provider(coordinator, target=target)
        deps = provider.get_dependencies("pkg", self._V)
        assert "alpha" in deps
        assert "beta" not in deps

    def test_agreeing_siblings_record_no_markers(self) -> None:
        """Projecting tie siblings touches no provider marker state."""
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        md = _sib_meta(
            'alpha; python_version >= "3"',
            'gamma; extra == "x"',
            provides_extra=("x",),
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", md, metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", md, metadata_url=wheel_b.metadata_url
        )
        provider = Provider(coordinator, target=_LINUX311)
        metadata_resolver.check_sibling_metadata_divergence(
            provider, [(self._V, wheel_a), (self._V, wheel_b)], "pkg", self._V
        )
        assert provider.consulted_markers == set()
        assert provider.marker_base_cache == {}
        assert provider.marker_extra_cache == {}

    def test_sidecar_vs_range_recovered_sibling_crash(self) -> None:
        """A sidecar pick and a bare rung-4 sibling are compared as equals."""
        sidecar = _sib_wheel("py2.py3-none-any")
        bare = _sib_wheel("py3-none-any", has_metadata=False)
        coordinator = make_coordinator([sidecar, bare], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=sidecar.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("beta>=1"), metadata_url=bare.url
        )
        provider = Provider(coordinator, target=_LINUX311)
        with pytest.raises(SiblingMetadataDivergenceError) as exc:
            metadata_resolver.check_sibling_metadata_divergence(
                provider, [(self._V, sidecar), (self._V, bare)], "pkg", self._V
            )
        message = str(exc.value)
        assert "alpha" in message
        assert "beta" in message

    def test_extra_dep_divergence_crash(self) -> None:
        """Divergence confined to an extra's deps still crashes."""
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta('alpha; extra == "x"', provides_extra=("x",)),
            metadata_url=wheel_a.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta('beta; extra == "x"', provides_extra=("x",)),
            metadata_url=wheel_b.metadata_url,
        )
        provider = Provider(coordinator, target=_LINUX311)
        with pytest.raises(SiblingMetadataDivergenceError) as exc:
            metadata_resolver.check_sibling_metadata_divergence(
                provider, [(self._V, wheel_a), (self._V, wheel_b)], "pkg", self._V
            )
        assert "extra:x" in str(exc.value)

    def test_url_dep_divergence_crash(self) -> None:
        """A sibling's direct-URL dep is a divergence, bucketed not refused."""
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("dep>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("dep @ https://example.com/dep-1.0-py3-none-any.whl"),
            metadata_url=wheel_b.metadata_url,
        )
        provider = Provider(coordinator, target=_LINUX311)
        with pytest.raises(SiblingMetadataDivergenceError) as exc:
            metadata_resolver.check_sibling_metadata_divergence(
                provider, [(self._V, wheel_a), (self._V, wheel_b)], "pkg", self._V
            )
        assert "dep" in str(exc.value)

    def test_requires_python_floor_variation_no_crash(self) -> None:
        """Siblings differing only on the Python floor do not diverge.

        A universal py2.py3 wheel and a py3 wheel routinely ship different
        Requires-Python floors while imposing the same dependencies.  Both
        install identically on the target, so the version stays resolvable.
        """
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            "Requires-Python: >=2.7\nRequires-Dist: common>=1\n\n",
            metadata_url=wheel_a.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            "Requires-Python: >=3.6\nRequires-Dist: common>=1\n\n",
            metadata_url=wheel_b.metadata_url,
        )
        provider = Provider(coordinator, target=_LINUX311)
        deps = provider.get_dependencies("pkg", self._V)
        assert "common" in deps

    def test_unparseable_sibling_skipped(self) -> None:
        """A tie sibling whose own METADATA does not parse is dropped, not raised.

        A malformed sibling is an invalid candidate, so it is skipped like any
        other; it must not escape as an uncaught parse error, nor stand in as a
        divergence.
        """
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\n\n",
            metadata_url=wheel_b.metadata_url,
        )
        provider = Provider(coordinator, target=_LINUX311)
        deps = provider.get_dependencies("pkg", self._V)
        assert "alpha" in deps

    @pytest.mark.parametrize(
        "sibling_fields",
        [
            pytest.param(
                "Requires-Python: >=3.{oversized}\nRequires-Dist: beta>=1\n",
                id="requires-python",
            ),
            pytest.param("Requires-Dist: beta>=1.{oversized}\n", id="requires-dist"),
            pytest.param(
                'Requires-Dist: beta>=1; python_full_version >= "3.{oversized}"\n',
                id="marker",
            ),
        ],
    )
    def test_oversized_sibling_clause_version_skipped(
        self, sibling_fields: str
    ) -> None:
        """A tie sibling whose clause version will not convert is skipped too.

        A digit run past the int-from-string limit gets past the specifier and
        marker constructors alike and raises only when something compares it,
        which for a marker is during the sibling's projection.  The sibling is
        dropped like any other invalid candidate instead of aborting the
        resolve.
        """
        oversized = "9" * (sys.get_int_max_str_digits() + 1)
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")

        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            + sibling_fields.format(oversized=oversized)
            + "\n",
            metadata_url=wheel_b.metadata_url,
        )

        provider = Provider(coordinator, target=_LINUX311)
        assert sorted(provider.get_dependencies("pkg", self._V)) == ["alpha"]

    def test_unevaluable_sibling_marker_stops_the_run(self) -> None:
        """A tie sibling with an undecidable marker stops the run.

        A quoted left operand makes packaging read the right one as an
        environment key, so the clause fails only when the sibling is
        projected.  Skipping the sibling there would pin the pick's
        ``common`` and lose the ``gamma`` the sibling also declares.
        """
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")

        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("common>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("common>=1", "gamma>=1", 'beta>=1; "extra" == "gpu"'),
            metadata_url=wheel_b.metadata_url,
        )

        provider = Provider(coordinator, target=_LINUX311)
        with pytest.raises(UnevaluableMarkerError, match='"extra" == "gpu"'):
            provider.get_dependencies("pkg", self._V)

    def test_provides_extra_override_folds_divergence(self) -> None:
        """A provides-extra override is applied to both siblings before compare.

        Two tie wheels share a base dep but declare different Provides-Extra.
        Raw, the extra maps differ and the version crashes; an override that
        normalizes both to the same declared extras reconciles them, matching
        the overridden view the resolver actually pins from.
        """
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("alpha>=1", provides_extra=("x",)),
            metadata_url=wheel_a.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("alpha>=1", provides_extra=("y",)),
            metadata_url=wheel_b.metadata_url,
        )
        provider = Provider(
            coordinator,
            target=_LINUX311,
            package_overrides=(pkg_override("pkg", provides_extra=()),),
        )
        deps = provider.get_dependencies("pkg", self._V)
        assert "alpha" in deps

    def test_requires_python_override_admits_sibling(self) -> None:
        """The sibling admission check uses the effective Requires-Python.

        A sibling whose parsed Requires-Python excludes the target would be
        skipped, missing its divergence; a Requires-Python override that admits
        the target puts it back in play, so the divergence crashes.
        """
        target = ResolveTarget.for_declared(
            python_version="3.9", spec=PlatformSpec("linux_x86_64")
        )
        pick = _sib_wheel("py2.py3-none-any")
        sibling = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([pick, sibling], package="pkg")
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            "Requires-Python: >=3.8\nRequires-Dist: alpha>=1\n\n",
            metadata_url=pick.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            "Requires-Python: >=3.11\nRequires-Dist: alpha>=1\n"
            "Requires-Dist: beta>=1\n\n",
            metadata_url=sibling.metadata_url,
        )
        provider = Provider(
            coordinator,
            target=target,
            package_overrides=(pkg_override("pkg", requires_python=">=3.8"),),
        )
        with pytest.raises(SiblingMetadataDivergenceError) as exc:
            provider.get_dependencies("pkg", self._V)
        assert "beta" in str(exc.value)

    def test_resolve_aborts_on_lone_divergent_version(self) -> None:
        """A full resolve over a diverging version aborts, never silently fails.

        The divergence must not read as a MetadataError candidate drop: that
        would turn the version into a soft "no versions available" rejection
        rather than the loud crash the ambiguity demands.
        """
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel_a, wheel_b], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("beta>=1"), metadata_url=wheel_b.metadata_url
        )
        with pytest.raises(SiblingMetadataDivergenceError):
            resolve_with_coordinator(
                coordinator,
                [_LINUX311],
                [Requirement("pkg")],
                inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            )

    def test_resolve_aborts_not_downgrades_past_divergent(self) -> None:
        """A diverging version aborts rather than silently pinning a clean one."""
        wheel_a = _sib_wheel("py2.py3-none-any")
        wheel_b = _sib_wheel("py3-none-any")
        clean = WheelFile(
            filename="pkg-0.9-py3-none-any.whl",
            url="https://example.com/pkg-0.9-py3-none-any.whl",
            version="0.9",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
            local_path=None,
        )
        coordinator = make_coordinator([wheel_a, wheel_b, clean], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("alpha>=1"), metadata_url=wheel_a.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("beta>=1"), metadata_url=wheel_b.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "0.9", _sib_meta(), metadata_url=clean.metadata_url
        )
        with pytest.raises(SiblingMetadataDivergenceError):
            resolve_with_coordinator(
                coordinator,
                [_LINUX311],
                [Requirement("pkg")],
                inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            )


# One Requires-Python spanning every interpreter a release built for, the
# torch 1.4.0 shape: it admits every sibling wheel, so the Requires-Python
# guard on the tie set never fires and the tags have to do the narrowing.
_SPLIT_RP = ">=2.7, !=3.0.*, !=3.1.*, !=3.2.*, !=3.3.*, !=3.4.*"


def _split_wheel(tag: str) -> WheelFile:
    """A pkg 1.0 wheel for one interpreter, carrying the release's Requires-Python."""
    filename = f"pkg-1.0-{tag}.whl"
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version="1.0",
        requires_python=_SPLIT_RP,
        has_metadata=True,
        upload_time=None,
    )


def _split_meta(*requires_dist: str) -> str:
    """A METADATA blob carrying the split release's Requires-Python."""
    return (
        f"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
        f"Requires-Python: {_SPLIT_RP}\n"
        + "".join(f"Requires-Dist: {line}\n" for line in requires_dist)
        + "\n"
    )


def _overlay_target() -> ResolveTarget:
    """A declared python 3.7 linux target moved off its tags by a marker overlay."""
    target = ResolveTarget.for_declared(
        python_version="3.7", spec=PlatformSpec("linux_x86_64")
    ).with_marker_overrides({"platform_system": "Windows"})
    assert not target.tags_faithful
    return target


class TestNoTagAxisPythonNarrowing:
    """A disowned tag axis still narrows siblings by the target's Python.

    A marker overlay cannot rebuild the platform tags, so under one the
    provider filters no wheel by tag.  The target still names a Python,
    whichever ``python_version`` and ``implementation_name`` the overlay leaves
    in force, so a wheel built for another interpreter is still a wheel this
    target can never install, and neither the tie set nor the pick may treat it
    as a candidate.  Where the Python axis admits no wheel of a version it
    decides nothing, and both fall back to the whole listing.
    """

    _V = V("1.0")

    def test_other_interpreter_siblings_never_tie(self) -> None:
        """A release split across interpreters resolves under an overlay.

        The divergent wheel is built for another interpreter, so it is no
        ambiguity for this target and the version stays resolvable.
        """
        cp27 = _split_wheel("cp27-cp27m-manylinux1_x86_64")
        cp35 = _split_wheel("cp35-cp35m-manylinux1_x86_64")
        cp37 = _split_wheel("cp37-cp37m-manylinux1_x86_64")
        coordinator = make_coordinator([cp27, cp35, cp37], package="pkg")
        for wheel in (cp27, cp37):
            coordinator.index.store_metadata(
                "pkg", "1.0", _split_meta("common>=1"), metadata_url=wheel.metadata_url
            )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _split_meta("common>=1", "numpy"),
            metadata_url=cp35.metadata_url,
        )
        provider = Provider(coordinator, target=_overlay_target())
        deps = provider.get_dependencies("pkg", self._V)
        assert "common" in deps
        assert "numpy" not in deps

    def test_same_interpreter_divergence_still_crashes(self) -> None:
        """Two wheels for the target's own interpreter remain a real ambiguity.

        Only the platform separates them, and the platform is the axis the
        overlay disowned, so nothing can rank them and the crash must stand.
        """
        linux = _split_wheel("cp37-cp37m-manylinux1_x86_64")
        macos = _split_wheel("cp37-none-macosx_10_9_x86_64")
        coordinator = make_coordinator([linux, macos], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _split_meta("common>=1"), metadata_url=linux.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _split_meta("common>=1", "numpy"),
            metadata_url=macos.metadata_url,
        )
        provider = Provider(coordinator, target=_overlay_target())
        with pytest.raises(SiblingMetadataDivergenceError) as exc:
            provider.get_dependencies("pkg", self._V)
        assert "numpy" in str(exc.value)

    def test_the_pick_reads_a_wheel_the_target_could_install(self) -> None:
        """Listing order does not decide which wheel a version's deps come from.

        The interpreter wheels are listed oldest first, as an index serves
        them, so an order-driven pick would read this target's dependencies
        out of a wheel built for another Python.
        """
        cp27 = _split_wheel("cp27-cp27m-manylinux1_x86_64")
        cp35 = _split_wheel("cp35-cp35m-manylinux1_x86_64")
        cp37 = _split_wheel("cp37-cp37m-manylinux1_x86_64")
        coordinator = make_coordinator([cp27, cp35, cp37], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _split_meta("py27dep>=1"), metadata_url=cp27.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", _split_meta("py35dep>=1"), metadata_url=cp35.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg", "1.0", _split_meta("py37dep>=1"), metadata_url=cp37.metadata_url
        )
        provider = Provider(coordinator, target=_overlay_target())
        assert (
            metadata_resolver.pick_dist_for_metadata(
                [(self._V, cp27), (self._V, cp35), (self._V, cp37)],
                self._V,
                provider.wheel_tags,
                provider.target,
            )
            is cp37
        )
        assert "py37dep" in provider.get_dependencies("pkg", self._V)

    def test_a_split_release_resolves_end_to_end_under_an_overlay(self) -> None:
        """The whole resolve completes, rather than aborting on the ambiguity.

        Every wheel built for another interpreter declares a dependency the
        pick does not, and the index does not carry it, so treating any of
        them as this target's wheel fails the resolve just as loudly as the
        crash did.
        """
        cp27 = _split_wheel("cp27-cp27m-manylinux1_x86_64")
        cp35 = _split_wheel("cp35-cp35m-manylinux1_x86_64")
        cp37 = _split_wheel("cp37-cp37m-manylinux1_x86_64")
        coordinator = make_coordinator([cp27, cp35, cp37], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _split_meta(), metadata_url=cp37.metadata_url
        )
        for wheel in (cp27, cp35):
            coordinator.index.store_metadata(
                "pkg", "1.0", _split_meta("numpy"), metadata_url=wheel.metadata_url
            )
        result = resolve_with_coordinator(
            coordinator,
            [_overlay_target()],
            [Requirement("pkg")],
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
        )
        assert result.success
        assert result.target_results[0].pins == {"pkg": self._V}

    def test_a_version_built_for_no_admitted_interpreter_keeps_listing_order(
        self,
    ) -> None:
        """Narrowing to no wheel at all decides nothing, so listing order stands."""
        cp27 = _split_wheel("cp27-cp27m-manylinux1_x86_64")
        cp35 = _split_wheel("cp35-cp35m-manylinux1_x86_64")
        assert (
            metadata_resolver.pick_dist_for_metadata(
                [(self._V, cp27), (self._V, cp35)],
                self._V,
                None,
                _overlay_target(),
            )
            is cp27
        )

    def test_a_version_admitting_no_wheel_still_crashes_on_divergence(self) -> None:
        """A pick the target cannot install either leaves every sibling a tie.

        Narrowing to nothing decides nothing, so the pick falls back to listing
        order and the divergence it would read past has to stay loud.
        """
        cp27 = _split_wheel("cp27-cp27m-manylinux1_x86_64")
        cp35 = _split_wheel("cp35-cp35m-manylinux1_x86_64")
        coordinator = make_coordinator([cp27, cp35], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _split_meta("common>=1"), metadata_url=cp27.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _split_meta("common>=1", "numpy"),
            metadata_url=cp35.metadata_url,
        )
        provider = Provider(coordinator, target=_overlay_target())
        with pytest.raises(SiblingMetadataDivergenceError) as exc:
            provider.get_dependencies("pkg", self._V)
        assert "numpy" in str(exc.value)

    def test_abi3_sibling_of_an_older_interpreter_still_ties(self) -> None:
        """An abi3 wheel below the target's interpreter is installable, so it ties."""
        pick = _split_wheel("cp37-cp37m-manylinux1_x86_64")
        abi3 = _split_wheel("cp35-abi3-manylinux1_x86_64")
        coordinator = make_coordinator([pick, abi3], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _split_meta("common>=1"), metadata_url=pick.metadata_url
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _split_meta("common>=1", "numpy"),
            metadata_url=abi3.metadata_url,
        )
        provider = Provider(coordinator, target=_overlay_target())
        with pytest.raises(SiblingMetadataDivergenceError) as exc:
            provider.get_dependencies("pkg", self._V)
        assert "numpy" in str(exc.value)


class TestExtrasProxyDiagnostics:
    """An extras proxy left with no version says why, in its own name.

    ``choose_version`` hands ``foo[bar]`` to the extras chooser before either
    listing-level record is made, so without a record of its own the tree
    names a package the ``Diagnostics:`` section cannot.
    """

    def _backtracking(self, metadata_by_version: dict[str, str]) -> Provider:
        """A BACKTRACK-mode provider over the versions ``metadata`` describes."""
        coordinator = make_coordinator(
            [make_wheel(version) for version in metadata_by_version],
            metadata_by_version=metadata_by_version,
            package="foo",
        )
        return Provider(coordinator, target=_PY312, extras_mode=ExtrasMode.BACKTRACK)

    def test_no_version_declares_the_extra(self) -> None:
        """The base publishes versions, and none of them provides the extra."""
        provider = self._backtracking(
            {
                "2.0": make_metadata("foo", "2.0", "bar>=1.0"),
                "1.0": make_metadata("foo", "1.0", "bar>=1.0"),
            }
        )
        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        diagnostic = provider.get_no_versions_reason("foo[security]")
        assert diagnostic is not None
        assert diagnostic.short == "no version of foo declares this extra"
        assert diagnostic.detail[0] == (
            "no version of foo in the range the resolve considered declares"
            " Provides-Extra: security"
        )
        assert diagnostic.detail[1].startswith("the range considered: ")

    def test_every_declaring_version_has_unreadable_metadata(self) -> None:
        """Metadata nothing could read is told apart from an absent extra."""
        provider = self._backtracking({"2.0": "not: metadata\n"})
        with pytest.raises(MetadataError):
            provider.get_dependencies("foo", V("2.0"))

        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        diagnostic = provider.get_no_versions_reason("foo[security]")
        assert diagnostic is not None
        assert diagnostic.short == (
            "every version in range was rejected on its metadata"
        )
        assert "foo==2.0" in diagnostic.detail[0]

    def test_a_metadata_error_raised_by_the_chooser_is_recorded(self) -> None:
        """The chooser's own failed read is the record, with no earlier ask."""
        provider = self._backtracking({"2.0": "not: metadata\n"})
        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        diagnostic = provider.get_no_versions_reason("foo[security]")
        assert diagnostic is not None
        assert diagnostic.short == (
            "every version in range was rejected on its metadata"
        )

    def test_the_base_was_narrowed_off_every_declaring_version(self) -> None:
        """A root extra whose declaring versions the search narrowed away.

        The detail names the version that declares the extra rather than
        the range the search was left with: that range is the solver's own
        and does not always spell as a specifier.
        """
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={
                "2.0": make_metadata("foo", "2.0", "bar>=1.0"),
                "1.0": EXTRA_METADATA,
            },
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=_PY312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
            root_extras={("foo", "security")},
        )
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet(">=2").to_range()}, {}
        )
        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        diagnostic = provider.get_no_versions_reason("foo[security]")
        assert diagnostic is not None
        assert diagnostic.short == (
            "another requirement holds foo where this extra is undeclared"
        )
        assert diagnostic.detail == (
            "foo 1.0 declares the extra, and the resolve cannot choose that version",
        )

    def test_a_filter_that_emptied_the_base_listing_is_named(self) -> None:
        """A proxy whose base has no version at all reports the base's listing.

        The extra never comes into it: the cutoff took every file before
        anything read a ``Provides-Extra``, so the entry is the one the
        base package would have printed, and its remedy names the base's
        own configuration entry rather than the proxy.
        """
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=_PY312,
            uploaded_prior_to=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        diagnostic = provider.get_no_versions_reason("foo[security]")
        assert diagnostic is not None
        assert (
            diagnostic.short == "uploaded-prior-to excluded every file; none is dated"
        )
        assert diagnostic.remedy == 'set packages."foo".uploaded-prior-to = false'

    def test_a_base_no_index_serves_is_reported_absent(self) -> None:
        """The listing-level answer reaches the proxy whatever it says."""
        provider = Provider(make_coordinator([], package="foo"), target=_PY312)
        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        assert (
            short_reason(provider, "foo[security]")
            == "package not found on any configured index"
        )

    def test_a_base_whose_versions_are_all_out_of_range_records_nothing(self) -> None:
        """No filter refused anything here, so there is nothing to name.

        The base published 1.0 and the ask was for ``>=9``, so the range
        that emptied the proxy is the resolve's own rather than the
        listing's.
        """
        provider = self._backtracking({"1.0": EXTRA_METADATA})
        assert (
            provider.choose_version("foo[security]", SpecifierSet(">=9").to_range())
            is None
        )
        assert provider.get_no_versions_reason("foo[security]") is None
