"""Provider integration with project and index fixtures."""

from __future__ import annotations

import asyncio
import errno
import io
import json
import logging
import tarfile
import threading
import zipfile
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn
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
from nab_markersets._compat import get_int_max_str_digits
from nab_project import _build_remote
from nab_project._resolve.engine import _raise_for_source_python
from nab_project.fetch import (
    DEFAULT_INDEX_URL,
    FetchCoordinator,
    IndexRoute,
)
from nab_project.inputs import ResolveInputs
from nab_project.resolve import resolve_with_coordinator
from nab_provider._provider import build_remote, metadata_resolver
from nab_provider._provider import listing as listing_mod
from nab_provider._provider import listing_diagnosis as diagnosis_mod
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import InvalidVersion, Version
from nab_provider.errors import YankAdmissionRequiredError
from nab_provider.marker_holds import UnevaluableMarkerError, dependency_marker_holds
from nab_provider.metadata import WheelMetadata
from nab_provider.overrides import IndexOverride, PackageOverride
from nab_provider.provider import (
    BuildPolicy,
    DistPolicy,
    IncompatiblePythonError,
    InvalidUploadTimeError,
    LocalSource,
    MetadataError,
    Provider,
    SiblingMetadataDivergenceError,
    SourceNameMismatchError,
    UnsupportedSdistError,
    VcsConfig,
)
from nab_provider.records import rehydrated_wheel
from nab_provider.resolver_inputs import build_resolver_inputs
from nab_provider.store import sdist_artifact_key
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    FakeFetchPort,
    make_metadata,
    make_sdist,
    make_wheel,
    pkg_override,
)
from nab_provider.yanking import mark_yanked
from nab_resolver.resolver import Resolver
from nab_resolver.types import (
    RangeProtocol,
)
from nab_test_support.coordinator import make_coordinator

requires_int_limit = pytest.mark.skipif(
    get_int_max_str_digits() == 0, reason="requires an enabled integer conversion limit"
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

_WINDOWS311 = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("windows_amd64")
)


def short_reason(
    provider: Provider, package: str, failed_range: VersionRange | None = None
) -> str | None:
    """Return the one line ``package``'s diagnostic prints at default verbosity."""
    diagnostic = provider.get_no_versions_reason(package, failed_range)
    return None if diagnostic is None else diagnostic.short


def rendered_reason(provider: Provider, package: str) -> str:
    """Return ``package``'s entry as one string: its line, then what ``-v`` adds."""
    diagnostic = provider.get_no_versions_reason(package)
    assert diagnostic is not None
    return "\n".join(diagnostic.lines(verbose=True))


def metadata_blocks(**blocks: str) -> dict[Version, diagnosis_mod.MetadataBlock]:
    """Build the per-version metadata failures a ban records."""
    return {
        V(version): diagnosis_mod.MetadataBlock(message)
        for version, message in blocks.items()
    }


def _blocker_reason(
    *blockers: diagnosis_mod.Blocker,
) -> diagnosis_mod.NoVersionsReason:
    """A look-ahead marker, the reason tests seed when one must name a cause."""
    return diagnosis_mod.NoVersionsReason(
        diagnosis_mod.ReasonKind.BLOCKERS, blockers=blockers
    )


def _prefetched_batch_versions(coordinator: FakeFetchPort) -> list[str]:
    """Versions of the one batch metadata prefetch, in submission order."""
    batches = coordinator.calls_to("request_metadata_batch")
    assert len(batches) == 1
    return [version for _package, version, _url, _hash in batches[0][0]]


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

    @requires_int_limit
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
        oversized = ">=" + "1" * ((get_int_max_str_digits() or 4300) + 1)
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
        assert diagnostic.suggestions[0].detail == (
            "--no-offline turns offline mode off for this run wherever it was set: "
            "a nab.toml, NAB_OFFLINE, or this command line"
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

    def test_routed_yanked_page_keeps_candidates_for_admission(
        self, tmp_path: Path
    ) -> None:
        """The listing retains withdrawn candidates until admission is decided."""
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
            provider = Provider(coordinator, defer_yanked=True)
            chosen = provider.choose_version("foo", SpecifierSet("").to_range())
            assert chosen is not None
            with pytest.raises(YankAdmissionRequiredError):
                provider.get_dependencies("foo", chosen)

        assert short_reason(provider, "foo") is None

    def test_unusable_yanked_listing_keeps_its_diagnostic(self, tmp_path: Path) -> None:
        body = json.dumps(
            {
                "files": [
                    {
                        "filename": "not-a-distribution.whl",
                        "url": "https://index/not-a-distribution.whl",
                        "yanked": True,
                    }
                ]
            }
        ).encode()
        OnDiskCache(tmp_path, DEFAULT_INDEX_URL).put_simple(
            "foo",
            body,
            CachePolicy(fetched_at=0, max_age=1, etag=None),
        )
        with FetchCoordinator(
            transport=Urllib3AsyncTransport(),
            cache_dir=tmp_path,
            offline=True,
        ) as coordinator:
            provider = Provider(coordinator)
            assert provider.choose_version("foo", SpecifierSet("").to_range()) is None
            assert coordinator.index.is_all_yanked_listing("foo")
        assert "yanked" in str(short_reason(provider, "foo"))

    def test_all_yanked_listing_keeps_candidates_for_admission(
        self, tmp_path: Path
    ) -> None:
        """The listing retains withdrawn candidates until admission is decided."""
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
            provider = Provider(coordinator, defer_yanked=True)
            chosen = provider.choose_version("foo", SpecifierSet("").to_range())
            assert chosen is not None
            with pytest.raises(YankAdmissionRequiredError):
                provider.get_dependencies("foo", chosen)

        assert short_reason(provider, "foo") is None

    def test_yanked_wheel_is_retained_beside_an_unreadable_sdist(
        self, tmp_path: Path
    ) -> None:
        """The listing retains withdrawn candidates until admission is decided."""
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
            provider = Provider(coordinator, defer_yanked=True)
            chosen = provider.choose_version("foo", SpecifierSet("").to_range())
            assert chosen is not None
            with pytest.raises(YankAdmissionRequiredError):
                provider.get_dependencies("foo", chosen)

        assert short_reason(provider, "foo") is None

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
            "legacy ZIP sdists require allow-zip-sdists = true"
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
            "legacy ZIP sdists require allow-zip-sdists = true"
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
            "legacy ZIP sdists require allow-zip-sdists = true"
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
            "legacy ZIP sdists require allow-zip-sdists = true"
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

    @respx.mock
    def test_remote_html_page_of_unreachable_links_names_the_links(
        self, tmp_path: Path
    ) -> None:
        """A page served as HTML reads like the same page served as JSON.

        The wheel sits behind a URL with an unterminated IPv6 bracket, so
        the conversion to JSON has to keep the anchor for the page to read
        as unreachable rather than absent.
        """
        respx.get(f"{DEFAULT_INDEX_URL}foo/").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<a href="https://[::1/foo-1.0-py3-none-any.whl">foo-1.0</a>',
            )
        )

        with FetchCoordinator(
            transport=HttpxAsyncTransport(), cache_dir=tmp_path
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
        """A release retained under another form was not filtered.

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

        pywin32 is the motivating example: every wheel is ``win_*`` and there is no
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
            '\ntry: set packages."foo".dist-policy = "wheel-or-sdist"'
            "\n     The project-wide dist-policy setting rejected these files."
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

    def test_root_ban_outranks_a_generic_reason(self) -> None:
        """A root ban takes precedence over a generic no-match reason."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider._no_versions_reasons["foo"] = diagnosis_mod.NoVersionsReason(
            diagnosis_mod.ReasonKind.NO_MATCH
        )

        provider.record_root_ban(
            "foo",
            "bar",
            SpecifierSet("==2.0").to_range(),
            SpecifierSet("==1.0").to_range(),
            [V("5.0")],
        )

        assert short_reason(provider, "foo") == (
            "5.0 needs bar in ==2.0, but your project requires bar ==1.0"
        )

    def test_root_ban_unions_the_versions_of_every_scan(self) -> None:
        """Repeated records combine banned versions without duplicates."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        declared = SpecifierSet("==2.0").to_range()
        root_range = SpecifierSet("==1.0").to_range()

        provider.record_root_ban("foo", "bar", declared, root_range, [V("5.0")])
        provider.record_root_ban(
            "foo", "bar", declared, root_range, [V("5.0"), V("4.0")]
        )

        assert short_reason(provider, "foo") == (
            "5.0 and 4.0 need bar in ==2.0, but your project requires bar ==1.0"
        )

    def test_two_root_bans_leave_their_ranges_to_the_detail(self) -> None:
        """Multiple root constraints retain their ranges in Diagnostic.detail."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)

        provider.record_root_ban(
            "foo",
            "bar",
            SpecifierSet("==2.0").to_range(),
            SpecifierSet("==1.0").to_range(),
            [V("5.0")],
        )
        provider.record_root_ban(
            "foo",
            "baz",
            SpecifierSet(">=7.0").to_range(),
            SpecifierSet("<7.0").to_range(),
            [V("4.0")],
        )

        assert rendered_reason(provider, "foo") == (
            "some versions are blocked by bar and baz"
            "\n5.0 needs bar in ==2.0, but your project requires bar ==1.0"
            "\n4.0 needs baz in >=7.0, but your project requires baz <7.0"
        )

    def test_a_root_ban_and_a_metadata_ban_are_reported_together(self) -> None:
        """Root and metadata rejections appear in the same diagnostic."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)

        provider.record_root_ban(
            "foo",
            "bar",
            SpecifierSet("==2.0").to_range(),
            SpecifierSet("==1.0").to_range(),
            [V("5.0")],
        )
        provider.record_metadata_ban(
            "foo", metadata_blocks(**{"4.0": "No metadata for foo==4.0"})
        )

        assert rendered_reason(provider, "foo") == (
            "some versions are blocked by bar or rejected on their metadata"
            "\n5.0 needs bar in ==2.0, but your project requires bar ==1.0"
            "\nNo metadata for foo==4.0"
        )

    def test_metadata_ban_outside_the_failed_clause_is_not_its_reason(self) -> None:
        """Metadata failures outside the failed range are excluded."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider._no_versions_reasons["foo"] = diagnosis_mod.NoVersionsReason(
            diagnosis_mod.ReasonKind.NO_MATCH,
            version_range=SpecifierSet(">=2").to_range(),
        )

        provider.record_metadata_ban(
            "foo", metadata_blocks(**{"1.0": "No metadata for foo==1.0"})
        )

        assert (
            short_reason(provider, "foo", SpecifierSet(">=2").to_range())
            == "no version matches the requirement"
        )

    def test_root_ban_outside_the_failed_clause_is_not_its_reason(self) -> None:
        """Root bans are limited to versions in the failed range."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)
        provider._no_versions_reasons["foo"] = diagnosis_mod.NoVersionsReason(
            diagnosis_mod.ReasonKind.NO_MATCH,
            version_range=SpecifierSet(">=6.0").to_range(),
        )

        provider.record_root_ban(
            "foo",
            "bar",
            SpecifierSet("==2.0").to_range(),
            SpecifierSet("==1.0").to_range(),
            [V("5.0")],
        )

        assert (
            short_reason(provider, "foo", SpecifierSet(">=6.0").to_range())
            == "no version matches the requirement"
        )

    def test_a_root_ban_names_only_the_versions_the_clause_named(self) -> None:
        """A narrowed failure range restricts the reported banned versions."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator)

        provider.record_root_ban(
            "foo",
            "bar",
            SpecifierSet("==2.0").to_range(),
            SpecifierSet("==1.0").to_range(),
            [V("5.0"), V("4.0")],
        )

        assert short_reason(provider, "foo", SpecifierSet("<5.0").to_range()) == (
            "4.0 needs bar in ==2.0, but your project requires bar ==1.0"
        )

    def test_a_ban_beside_a_filtered_sdist_keeps_the_filter_it_named(self) -> None:
        """The metadata diagnosis retains the filter and its remedy."""
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

        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))

        provider.record_metadata_ban(
            "pkg",
            {
                V("1.0"): diagnosis_mod.MetadataBlock(
                    str(excinfo.value), excinfo.value.filtered_sdist_version
                )
            },
        )
        provider.record_root_ban(
            "pkg",
            "bar",
            SpecifierSet("==2.0").to_range(),
            SpecifierSet("==1.0").to_range(),
            [V("2.0")],
        )

        diagnostic = provider.get_no_versions_reason("pkg")

        assert diagnostic is not None
        assert diagnostic.short == (
            "some versions are blocked by bar or rejected on their metadata"
        )
        assert diagnostic.detail == (
            "2.0 needs bar in ==2.0, but your project requires bar ==1.0",
            "pkg 1.0 has no PEP 658 metadata on the index",
            (
                "the uploaded-prior-to cutoff 2026-01-10T00:00:00+00:00 excluded"
                " pkg-1.0.tar.gz, uploaded at 2026-01-20T00:00:00Z"
            ),
        )
        assert diagnostic.remedy == 'set packages."pkg".uploaded-prior-to = false'

        assert diagnostic.suggestions[0].action == (
            'set packages."pkg".uploaded-prior-to = false'
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
        union no specifier set expresses, so the line states the two pins one by
        one.  Neither pin names a version ``bar`` publishes, so its listing
        cannot supply a representation whether or not it is cached.
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
        """Pins covering every listed version stay on the line.

        ``bar`` publishes only the two versions ``foo`` pins. Formatting their
        union over that listing would name every version and leave no
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
        cache.put_sdist_files(
            "foo",
            sdist_artifact_key("1.0", str(_sdist_entry("1.0")["url"])),
            None,
            None,
        )
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
        cache.put_sdist_files(
            "foo",
            sdist_artifact_key("1.0", str(_sdist_entry("1.0")["url"])),
            self._PKG_INFO.format(ver="1.0"),
            None,
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
                "foo",
                sdist_artifact_key(version, str(_sdist_entry(version)["url"])),
                self._PKG_INFO.format(ver=version),
                None,
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
                pkg,
                sdist_artifact_key(ver, _url),
                SdistHashMismatchError("sdist sha256 mismatch"),
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
        # Make the underlying parse fail if reached again. The cached entry
        # must short-circuit first.
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
        """An unreadable local wheel aborts the resolve.

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
        other; it finds no candidates and backtracks.
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
            "transport_factory": None,
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
        [3.3.0, 3.4.0.dev0)" because only the local source supplied that range.
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
        # The complete override supplies the dependencies without reading the corrupt wheel.
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
    """Source Python overrides apply during admission and final-pin checks."""

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
        provider.fetch_versions("foo")
        return provider

    def test_widen_admits(self, tmp_path: Path) -> None:
        provider = self._local_provider(tmp_path, ">=3.11", ">=3.0")
        provider.get_dependencies("foo", V("1.0"))
        stamped = provider.metadata_cache[("foo", V("1.0"))].requires_python
        assert stamped == SpecifierSet(">=3.0")
        _raise_for_source_python(provider, provider.target, {"foo": V("1.0")})

    def test_narrow_rejects(self, tmp_path: Path) -> None:
        provider = self._local_provider(tmp_path, ">=3.0", ">=3.11")
        with pytest.raises(
            IncompatiblePythonError, match="foo 1.0 requires Python >=3.11"
        ):
            provider.get_dependencies("foo", V("1.0"))
        assert ("foo", V("1.0")) not in provider.deps_cache


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


class TestLadderSdistAvailability:
    """The metadata ladder tells the ways an sdist can be missing apart.

    A filtered sdist names the policy that refused it. An unusable archive
    URL cannot become a metadata source by changing the distribution policy.

    Which rung dropped a filtered sdist is a walk of the whole listing, and
    look-ahead swallows this error, so the ladder marks the version and the
    report names the rung only once the resolve has failed.
    """

    _ABSENT = "no PEP 658 metadata and no sdist available"
    _FILTERED = "no PEP 658 metadata and the listing filter excluded the sdist"

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

    @pytest.mark.parametrize("zip_version", ["1.0", "1.0.0", "2.0", "not-a-version"])
    @pytest.mark.parametrize("allowed", [False, True])
    def test_zip_diagnostics_without_usable_records_do_not_suggest_permission(
        self, zip_version: str, *, allowed: bool
    ) -> None:
        coordinator = self._with_zip_sdists(
            [make_wheel("1.0", has_metadata=False)], frozenset({zip_version})
        )
        provider = Provider(coordinator, target=_PY312, allow_zip_sdists=allowed)
        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))

        assert self._ABSENT in str(excinfo.value)
        assert "allow-zip-sdists" not in str(excinfo.value)

    def test_unusable_zip_url_does_not_suggest_permission(self) -> None:
        files = _parse_files(
            {
                "files": [
                    {
                        "filename": "pkg-1.0-py3-none-any.whl",
                        "url": "https://example.com/pkg-1.0-py3-none-any.whl",
                    },
                    {"filename": "pkg-1.0.zip", "url": "https://[broken/pkg-1.0.zip"},
                ]
            },
            "https://example.com/simple/pkg/",
            "pkg",
        )

        assert len(files) == 1

        coordinator = self._with_zip_sdists(files, frozenset({"1.0"}))
        provider = Provider(coordinator, target=_PY312)
        with pytest.raises(MetadataError) as excinfo:
            provider.get_dependencies("pkg", V("1.0"))

        assert self._ABSENT in str(excinfo.value)
        assert "allow-zip-sdists" not in str(excinfo.value)

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
        text as wheel METADATA would bypass validation and resurrect the
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
        """A sidecar with no text reads PKG-INFO and remains rejected.

        The fetcher records a sidecar that served nothing as the ``None`` of
        its own slot, so the batch read falls back to the sdist's PKG-INFO.
        Caching that as wheel METADATA would bypass the PEP 643 check.
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


PKG_INFO_DYNAMIC_DEPS = (
    "Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\nDynamic: Requires-Dist\n"
)

PKG_INFO_PRE_PEP643_DEPS = (
    "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: hidden-dep\n"
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
    """Write a Linux-only wheel without a metadata sidecar."""
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
        """Keep shared PKG-INFO subject to the sdist metadata policy."""
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
        """Use the sdist's PKG-INFO after another target cached wheel METADATA."""
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
        """Read a local wheel's METADATA after another target cached PKG-INFO."""
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
        """Fall back to PKG-INFO when the wheel's METADATA is unusable."""
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
        """Keep a target's wheel when another target cannot use the sdist."""
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
            "transport_factory": None,
        }

    def test_resolve_dynamic_sdist_reuses_cross_tuple_cache(self) -> None:
        """A second call for the same sdist returns the cached metadata.

        ``InMemoryIndex._resolved_sdist_metadata`` stops universal mode from
        augmenting or rebuilding the same sdist for every tuple. The cache key
        is the canonical name and version string.
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

        A local failure is not an HTTP error, so the hard-error branch names
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
            sdist_pkg_info_by_version={
                "2.0": PKG_INFO_DYNAMIC_DEPS.replace("Version: 1.0", "Version: 2.0"),
            },
        )

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
        # admits it, matching the listing check.
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


class TestTagRefusedWheelPayload:
    """A refused wheel keeps its record; the caller says whether it keeps more."""

    LINUX_URL = "https://example.com/foo-1.0-cp311-manylinux.whl"
    WINDOWS_URL = "https://example.com/foo-1.0-cp311-win_amd64.whl"

    def _wheels(self) -> list[WheelFile]:
        """One wheel a linux cp311 target installs and one it cannot.

        Both hold the index's tables unparsed, the form a listing's records
        take and the one the release drops.
        """
        return [
            rehydrated_wheel(
                filename="foo-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
                url=self.LINUX_URL,
                version="1.0",
                requires_python=None,
                has_metadata=True,
                upload_time="2024-01-01T00:00:00Z",
                hashes={"sha256": "ab"},
                size=None,
                metadata_hash={"sha256": "cd"},
            ),
            rehydrated_wheel(
                filename="foo-1.0-cp311-cp311-win_amd64.whl",
                url=self.WINDOWS_URL,
                version="1.0",
                requires_python=None,
                has_metadata=True,
                upload_time="2024-01-01T00:00:00Z",
                hashes={"sha256": "ef"},
                size=None,
                metadata_hash={"sha256": "01"},
            ),
        ]

    def _filtered(self, *, release_refused_wheels: bool) -> list[WheelFile]:
        """Both wheels, after a linux cp311 target has filtered the listing."""
        files = self._wheels()
        provider = Provider(
            make_coordinator(files, package="foo"),
            target=_LINUX311,
            release_refused_wheels=release_refused_wheels,
        )
        kept = [dist for _, dist in provider.fetch_versions("foo")]
        assert [w.filename for w in kept] == [files[0].filename]
        return files

    def _resolved(
        self, targets: Sequence[ResolveTarget], *, release_refused_wheels: bool
    ) -> list[WheelFile]:
        """Both wheels, after ``targets`` have resolved over one coordinator."""
        files = self._wheels()
        result = resolve_with_coordinator(
            make_coordinator(files, package="foo", auto_metadata=True),
            targets,
            [Requirement("foo")],
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            release_refused_wheels=release_refused_wheels,
        )
        assert result.success
        return files

    def test_the_only_reader_releases_the_refused_wheel(self) -> None:
        """Nothing reads a refused wheel's URL, hashes or sidecar again."""
        _, refused = self._filtered(release_refused_wheels=True)

        assert refused.url == ""
        assert refused.has_metadata is False
        assert refused.metadata_url is None
        assert refused.raw_hashes() is None
        assert refused.raw_sidecar() is None

    def test_the_only_reader_leaves_the_installable_wheel_whole(self) -> None:
        """Only the refused wheel is released; the kept one is untouched."""
        kept, _ = self._filtered(release_refused_wheels=True)

        assert kept.url == self.LINUX_URL
        assert kept.hashes == (("sha256", "ab"),)
        assert kept.metadata_hash == ("sha256", "cd")

    def test_a_shared_listing_keeps_the_refused_wheel(self) -> None:
        """Another target over the same coordinator still reads the record."""
        _, refused = self._filtered(release_refused_wheels=False)

        assert refused.url == self.WINDOWS_URL
        assert refused.hashes == (("sha256", "ef"),)
        assert refused.metadata_hash == ("sha256", "01")

    def test_a_one_target_resolve_releases_the_refused_wheel(self) -> None:
        """``resolve_with_coordinator`` passes the flag down to the provider."""
        _, refused = self._resolved([_LINUX311], release_refused_wheels=True)

        assert refused.url == ""
        assert refused.hashes == ()
        assert refused.metadata_hash is None

    def test_a_resolve_that_does_not_ask_keeps_the_refused_wheel(self) -> None:
        """One target is not enough on its own: the caller has to ask."""
        _, refused = self._resolved([_LINUX311], release_refused_wheels=False)

        assert refused.url == self.WINDOWS_URL
        assert refused.hashes == (("sha256", "ef"),)
        assert refused.metadata_hash == ("sha256", "01")

    def test_a_matrix_keeps_a_wheel_its_other_target_installs(self) -> None:
        """One target's refusal is the next target's pick, so nothing is released."""
        _, windows = self._resolved(
            [_LINUX311, _WINDOWS311], release_refused_wheels=True
        )

        assert windows.url == self.WINDOWS_URL
        assert windows.hashes == (("sha256", "ef"),)
        assert windows.metadata_hash == ("sha256", "01")


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

    def test_open_pypy_abis_do_not_hide_resident_dependency_divergence(self) -> None:
        """Native ABI siblings share preference when the target leaves its ABI open."""
        wheels = [
            _sib_wheel(f"pp311-pypy311_{abi}-win_amd64") for abi in ("pp73", "pp80")
        ]
        coordinator = make_coordinator(wheels, package="pkg")
        for wheel, dep in zip(wheels, ("alpha>=1", "beta>=1"), strict=True):
            coordinator.index.store_metadata(
                "pkg", "1.0", _sib_meta(dep), metadata_url=wheel.metadata_url
            )
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("windows_amd64"),
            implementation="pypy",
        )
        provider = Provider(coordinator, target=target)
        with pytest.raises(
            SiblingMetadataDivergenceError, match="declare different dependencies"
        ):
            provider.get_dependencies("pkg", self._V)

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

        with pytest.raises(SiblingMetadataDivergenceError):
            provider.get_dependencies("pkg", self._V)

    def test_rejected_newer_release_checks_the_prefetched_siblings(self) -> None:
        first = _sib_wheel("py2.py3-none-any")
        sibling = _sib_wheel("py3-none-any")
        coordinator = make_coordinator(
            listings={
                "pkg": [make_wheel("2", package="pkg"), first, sibling],
                "bar": [make_wheel("1", package="bar")],
            },
            metadata_by_version={"2": make_metadata("pkg", "2", "bar>=2")},
        )
        coordinator.index.store_metadata("bar", "1", make_metadata("bar", "1"))
        for wheel, text in ((first, _sib_meta("bar")), (sibling, _sib_meta())):
            coordinator.index.store_metadata(
                "pkg", "1.0", text, metadata_url=wheel.metadata_url
            )

        # The incompatible bar requirement takes the scan past the first release.
        with pytest.raises(SiblingMetadataDivergenceError):
            resolve_with_coordinator(
                coordinator, [_LINUX311], [Requirement("bar==1"), Requirement("pkg")]
            )

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

    def test_withdrawn_sibling_does_not_conflict_with_live_metadata(self) -> None:
        live = _sib_wheel("py2.py3-none-any")
        withdrawn = mark_yanked(_sib_wheel("py3-none-any"), reason="bad metadata")
        coordinator = make_coordinator([live, withdrawn], package="pkg")
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("alpha>=1"),
            metadata_url=live.metadata_url,
        )
        assert isinstance(withdrawn, WheelFile)
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            _sib_meta("beta>=1"),
            metadata_url=withdrawn.metadata_url,
        )
        provider = Provider(coordinator, target=_LINUX311)
        metadata_resolver.check_sibling_metadata_divergence(
            provider,
            [(self._V, live), (self._V, withdrawn)],
            "pkg",
            self._V,
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

    @requires_int_limit
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
        oversized = "9" * ((get_int_max_str_digits() or 4300) + 1)
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

    def test_sibling_signature_conversion_failure_skips_sibling(self) -> None:
        wheel = _sib_wheel("py3-none-any")
        coordinator = make_coordinator([wheel], package="pkg")
        coordinator.index.store_metadata(
            "pkg", "1.0", _sib_meta("beta>=1"), metadata_url=wheel.metadata_url
        )
        provider = Provider(coordinator, target=_LINUX311)

        with patch.object(
            metadata_resolver, "target_dep_signature", side_effect=ValueError
        ):
            signature = metadata_resolver._tie_sibling_signature(
                provider, coordinator.index, ("pkg", self._V), wheel
            )

        assert signature is None

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
