"""In-memory fetch port for the nab-python test suite.

:class:`FakeFetchPort` implements :class:`~nab_python.fetch_port.FetchPort`
against a real :class:`~nab_python.store.InMemoryIndex`.  Its request methods
write to that index and hand back an already-set :class:`threading.Event`, so
the synchronous provider code under test sees every fetch resolve immediately.

It is a class rather than a mock so an unserved request cannot answer: a mock
answers any attribute at any arity.  The request methods repeat the port's
signatures exactly, defaults included.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, TypeVar

from nab_provider.records import IndexConfig
from nab_python._build_remote import build_remote_sdist
from nab_python._sources import materialize_source
from nab_python._toml import parse_pyproject_table
from nab_python.fetch import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL
from nab_python.store import InMemoryIndex

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from nab_provider.records import RangeMetadataResult, SdistFile, WheelFile
    from nab_python.config import NabProjectConfig
    from nab_python.policy import SourceRequest

_MINIMAL_METADATA = "Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"

_T = TypeVar("_T")

REQUESTS = (
    "request_listing",
    "request_metadata",
    "request_metadata_batch",
    "request_range_metadata",
    "request_sdist",
    "request_sdist_archive",
    "request_direct_archive",
    "request_source_listing",
    "request_built_metadata",
)
"""The port's request methods, and the names :meth:`FakeFetchPort.calls_to`
and :meth:`FakeFetchPort.override` accept."""


def _done_event() -> threading.Event:
    """Return an already-set :class:`threading.Event`."""
    ev = threading.Event()
    ev.set()
    return ev


def _pre_populate_index(
    index: InMemoryIndex,
    listings_map: Mapping[str, Sequence[WheelFile | SdistFile]],
) -> None:
    """Load ``listings_map`` into ``index``."""
    for pkg_name, pkg_wheels in listings_map.items():
        index.store_listing(pkg_name, pkg_wheels)
        # Mirror production: every fetched listing records its serving index.
        index.store_listing_index(pkg_name, DEFAULT_INDEX_NAME)


def _resolve_listings(
    wheels: Sequence[WheelFile | SdistFile] | None,
    package: str,
    listings: Mapping[str, Sequence[WheelFile | SdistFile]] | None,
) -> Mapping[str, Sequence[WheelFile | SdistFile]]:
    """Pick the listings map: explicit ``listings`` wins over ``wheels``."""
    if listings is not None:
        return listings
    if wheels is not None:
        return {package: wheels}
    return {}


def _make_metadata_resolver(
    *,
    metadata_text: str | None,
    metadata_by_version: Mapping[str, str | None] | None,
    metadata_by_url: Mapping[str, str | None] | None,
    auto_metadata: bool,
) -> Callable[[str, str, str], str | None]:
    """Return the callable that picks metadata text for one sidecar."""

    def _resolve(pkg: str, ver: str, url: str) -> str | None:
        if metadata_by_url is not None:
            return metadata_by_url.get(url)
        if metadata_by_version is not None:
            return metadata_by_version.get(ver)
        if metadata_text is not None:
            return metadata_text
        if auto_metadata:
            return _MINIMAL_METADATA.format(name=pkg, version=ver)
        return None

    return _resolve


def _make_sdist_server(
    index: InMemoryIndex,
    *,
    sdist_pkg_info: str | None,
    sdist_pkg_info_by_version: Mapping[str, str | None] | None,
    sdist_pyproject_toml: str | None,
) -> Callable[[str, str], None]:
    """Return the callable that writes an sdist fetch's result into ``index``."""

    def _serve(pkg: str, ver: str) -> None:
        pkg_info = (
            sdist_pkg_info
            if sdist_pkg_info_by_version is None
            else sdist_pkg_info_by_version.get(ver)
        )

        # ``None`` marks the slot fetched, which is how a failed sdist reads.
        index.store_sdist_metadata(pkg, ver, pkg_info)
        if sdist_pyproject_toml is not None:
            index.store_sdist_pyproject(
                pkg, ver, parse_pyproject_table(sdist_pyproject_toml)
            )

    return _serve


def _make_range_server(
    index: InMemoryIndex,
    *,
    range_result: RangeMetadataResult | None,
    range_error: BaseException | None,
    range_by_url: Mapping[str, RangeMetadataResult] | None,
) -> Callable[[str, str, str], None]:
    """Return the callable that writes a range read's result into ``index``.

    Mirrors the coordinator's ``_fetch_range_metadata`` handler.
    """

    def _serve(pkg: str, ver: str, url: str) -> None:
        if range_error is not None:
            index.store_range_error(pkg, ver, url, range_error)
            return

        result = range_by_url.get(url) if range_by_url is not None else range_result
        if result is None:
            return

        index.store_range_outcome(pkg, ver, url, result.outcome)
        if result.text is None:
            index.store_range_absent(pkg, ver, url)
        else:
            index.store_range_metadata(pkg, ver, url, result.text)

    return _serve


def _make_archive_server(
    index: InMemoryIndex,
    *,
    sdist_archive: bytes | None,
    sdist_archive_error: BaseException | None,
) -> Callable[[str, str], None]:
    """Return the callable that writes an archive fetch's result into ``index``."""

    def _serve(pkg: str, ver: str) -> None:
        if sdist_archive_error is not None:
            index.store_sdist_archive_error(pkg, ver, sdist_archive_error)
        elif sdist_archive is not None:
            index.store_sdist_archive(pkg, ver, sdist_archive)

    return _serve


class FakeFetchPort:
    """An in-memory :class:`~nab_python.fetch_port.FetchPort`.

    Build one with :func:`make_coordinator` rather than directly: it assembles
    the four servers out of the keywords a test sets.
    """

    def __init__(
        self,
        index: InMemoryIndex,
        *,
        serve_metadata: Callable[[str, str, str], str | None],
        serve_sdist: Callable[[str, str], None],
        serve_range: Callable[[str, str, str], None],
        serve_archive: Callable[[str, str], None],
        build_config: NabProjectConfig | None = None,
    ) -> None:
        """Wire the port to ``index`` and to one server per fetch kind."""
        self.index = index
        # Not on the port: the engine reads it off the coordinator.
        self.indexes = [IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL)]
        self.offline = False
        self.build_config = build_config

        self._serve_metadata = serve_metadata
        self._serve_sdist = serve_sdist
        self._serve_range = serve_range
        self._serve_archive = serve_archive

        self._calls: dict[str, list[tuple[object, ...]]] = {n: [] for n in REQUESTS}
        self._overrides: dict[str, Callable[..., Any]] = {}

    def calls_to(self, name: str) -> list[tuple[object, ...]]:
        """Return the arguments of each call to request ``name``, oldest first."""
        return list(self._calls[self._checked(name)])

    def override(self, name: str, handler: Callable[..., Any]) -> None:
        """Replace request ``name``'s behaviour; calls are still recorded."""
        self._overrides[self._checked(name)] = handler

    def reset(self) -> None:
        """Forget every recorded call, keeping the overrides in place."""
        for calls in self._calls.values():
            calls.clear()

    def _checked(self, name: str) -> str:
        """Return ``name`` if it is one of the port's requests, else raise."""
        if name not in self._calls:
            msg = f"{name!r} is not a fetch request; expected one of {REQUESTS}"
            raise KeyError(msg)
        return name

    def _handle(
        self, name: str, args: tuple[object, ...], default: Callable[..., _T]
    ) -> _T:
        """Record the call to ``name``, then run its override or ``default``."""
        self._calls[name].append(args)
        handler = self._overrides.get(name, default)
        return handler(*args)

    def request_listing(
        self, package: str, *, speculative: bool = False
    ) -> threading.Event:
        """Return a set event: listings are pre-loaded into the index."""
        return self._handle("request_listing", (package, speculative), self._listing)

    def request_metadata(
        self,
        package: str,
        version: str,
        url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> threading.Event:
        """Write the sidecar text configured for ``url`` and return a set event."""
        return self._handle(
            "request_metadata", (package, version, url, metadata_hash), self._metadata
        )

    def request_metadata_batch(
        self, items: list[tuple[str, str, str, tuple[str, str] | None]]
    ) -> list[tuple[str, str, threading.Event]]:
        """Serve every item in ``items`` as :meth:`request_metadata` does."""
        return self._handle("request_metadata_batch", (items,), self._metadata_batch)

    def request_range_metadata(
        self,
        package: str,
        version: str,
        wheel_url: str,
        wheel_hashes: tuple[tuple[str, str], ...] = (),
    ) -> threading.Event:
        """Write the configured range read for ``wheel_url`` and return a set event."""
        return self._handle(
            "request_range_metadata",
            (package, version, wheel_url, wheel_hashes),
            self._range_metadata,
        )

    def request_sdist(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> threading.Event:
        """Write the configured sdist PKG-INFO and return a set event."""
        return self._handle(
            "request_sdist", (package, version, url, sdist_hashes), self._sdist
        )

    def request_sdist_archive(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> threading.Event:
        """Write the configured archive bytes and return a set event."""
        return self._handle(
            "request_sdist_archive",
            (package, version, url, sdist_hashes),
            self._archive,
        )

    def request_direct_archive(
        self, package: str, version: str, url: str
    ) -> threading.Event:
        """Write the configured archive bytes and return a set event.

        The provider keys a declared archive by its digest, so ``version`` is
        that digest rather than a release version.
        """
        return self._handle(
            "request_direct_archive", (package, version, url), self._direct_archive
        )

    def request_source_listing(self, request: SourceRequest) -> threading.Event:
        """Materialise the declared source for real, as the coordinator does."""
        return self._handle(
            "request_source_listing", (request,), self._materialize_source
        )

    def request_built_metadata(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...],
    ) -> threading.Event:
        """Build the sdist for real, over whatever archive bytes the store has."""
        return self._handle(
            "request_built_metadata",
            (package, version, url, sdist_hashes),
            self._store_built_metadata,
        )

    def _listing(self, package: str, speculative: bool) -> threading.Event:  # noqa: FBT001 - _handle calls this positionally
        """Serve a listing request: the index already holds what was pre-loaded."""
        del package, speculative
        return _done_event()

    def _fetch_metadata(self, package: str, version: str, url: str) -> None:
        """Write one sidecar slot, as the fetcher's metadata handler does."""
        # The fetcher skips a sidecar the index already answers for.
        if self.index.has_metadata(package, version, url):
            return

        # Storing ``None`` still marks the slot fetched.
        self.index.store_metadata(
            package,
            version,
            self._serve_metadata(package, version, url),
            metadata_url=url,
        )

    def _metadata(
        self,
        package: str,
        version: str,
        url: str,
        metadata_hash: tuple[str, str] | None,
    ) -> threading.Event:
        """Serve one sidecar request."""
        del metadata_hash
        self._fetch_metadata(package, version, url)
        return _done_event()

    def _metadata_batch(
        self, items: list[tuple[str, str, str, tuple[str, str] | None]]
    ) -> list[tuple[str, str, threading.Event]]:
        """Serve a batch of sidecar requests, one result per item."""
        results: list[tuple[str, str, threading.Event]] = []
        for package, version, url, _hash in items:
            self._fetch_metadata(package, version, url)
            results.append((package, version, _done_event()))
        return results

    def _range_metadata(
        self,
        package: str,
        version: str,
        wheel_url: str,
        wheel_hashes: tuple[tuple[str, str], ...],
    ) -> threading.Event:
        """Serve one range read."""
        del wheel_hashes
        self._serve_range(package, version, wheel_url)
        return _done_event()

    def _sdist(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...],
    ) -> threading.Event:
        """Serve one sdist PKG-INFO request."""
        del url, sdist_hashes
        self._serve_sdist(package, version)
        return _done_event()

    def _archive(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...],
    ) -> threading.Event:
        """Serve one sdist-archive request."""
        del url, sdist_hashes
        self._serve_archive(package, version)
        return _done_event()

    def _direct_archive(self, package: str, version: str, url: str) -> threading.Event:
        """Serve one declared-archive request."""
        del url
        self._serve_archive(package, version)
        return _done_event()

    def _materialize_source(self, request: SourceRequest) -> threading.Event:
        """Materialise one declared source, as the coordinator does."""
        self.index.store_source(
            request.package, materialize_source(self, request, self.build_config)
        )
        return _done_event()

    def _store_built_metadata(
        self, pkg: str, ver: str, url: str, hashes: tuple[tuple[str, str], ...]
    ) -> threading.Event:
        """Build one sdist, as the coordinator does."""
        self.index.store_built_metadata(
            pkg, ver, build_remote_sdist(self, pkg, ver, url, hashes, self.build_config)
        )
        return _done_event()


def make_coordinator(  # noqa: PLR0913 - one keyword per index slot a test pre-loads
    wheels: Sequence[WheelFile | SdistFile] | None = None,
    *,
    package: str = "pkg",
    listings: Mapping[str, Sequence[WheelFile | SdistFile]] | None = None,
    metadata_text: str | None = None,
    metadata_by_version: Mapping[str, str | None] | None = None,
    metadata_by_url: Mapping[str, str | None] | None = None,
    auto_metadata: bool = False,
    sdist_pkg_info: str | None = None,
    sdist_pkg_info_by_version: Mapping[str, str | None] | None = None,
    sdist_pyproject_toml: str | None = None,
    range_result: RangeMetadataResult | None = None,
    range_error: BaseException | None = None,
    range_by_url: Mapping[str, RangeMetadataResult] | None = None,
    sdist_archive: bytes | None = None,
    sdist_archive_error: BaseException | None = None,
    build_config: NabProjectConfig | None = None,
) -> FakeFetchPort:
    """Build a :class:`FakeFetchPort` backed by an :class:`InMemoryIndex`.

    Listing setup (one of):

    * ``wheels`` + ``package``: pre-load ``wheels`` under ``package``.
      Passing ``None`` skips listing setup.
    * ``listings``: pre-load each ``(package, wheels)`` pair.  Overrides
      ``wheels``/``package``.

    What each request then serves:

    * ``request_listing`` always returns a set event.
    * ``request_metadata`` and ``request_metadata_batch`` write the sidecar
      text under the requested URL, as the fetcher does.  The first of
      ``metadata_by_url``, ``metadata_by_version`` and ``metadata_text`` that
      is set answers, even where its mapping holds no entry, and
      ``auto_metadata`` fills in minimal METADATA when none is set.  A miss
      lands ``None`` in the sidecar slot, marking it fetched and empty.
    * ``request_sdist`` writes the entry from ``sdist_pkg_info_by_version``
      when that is set, else ``sdist_pkg_info``, and ``sdist_pyproject_toml``
      when it is not ``None``.
    * ``request_range_metadata`` records the recovered METADATA (or an absent
      read when its text is ``None``), or lands ``range_error`` as a per-wheel
      metadata error.  ``range_by_url`` gives sibling sidecar-less wheels of
      one version different dependencies; ``range_result`` answers every URL.
      With none it writes nothing, so rung 4 finds nothing.
    * ``request_sdist_archive`` and ``request_direct_archive`` write
      ``sdist_archive_error``, or ``sdist_archive`` as the fetched bytes.  With
      neither they write nothing, which leaves whatever the test stored in the
      index itself.
    * ``request_source_listing`` and ``request_built_metadata`` run the real
      materialiser and the real remote build under ``build_config``, so a
      declared source or a BUILD_REMOTE candidate behaves as it does in
      production over whatever bytes the store holds.

    For a setup these keywords cannot express, replace one request with
    :meth:`FakeFetchPort.override`, and read back the calls with
    :meth:`FakeFetchPort.calls_to`.
    """
    index = InMemoryIndex()

    _pre_populate_index(index, _resolve_listings(wheels, package, listings))

    return FakeFetchPort(
        index,
        serve_metadata=_make_metadata_resolver(
            metadata_text=metadata_text,
            metadata_by_version=metadata_by_version,
            metadata_by_url=metadata_by_url,
            auto_metadata=auto_metadata,
        ),
        serve_sdist=_make_sdist_server(
            index,
            sdist_pkg_info=sdist_pkg_info,
            sdist_pkg_info_by_version=sdist_pkg_info_by_version,
            sdist_pyproject_toml=sdist_pyproject_toml,
        ),
        serve_range=_make_range_server(
            index,
            range_result=range_result,
            range_error=range_error,
            range_by_url=range_by_url,
        ),
        serve_archive=_make_archive_server(
            index,
            sdist_archive=sdist_archive,
            sdist_archive_error=sdist_archive_error,
        ),
        build_config=build_config,
    )
