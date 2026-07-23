"""Shared mock-coordinator builder for the nab-python test suite.

A ``FetchCoordinator``-shaped :class:`unittest.mock.MagicMock` wrapped
around a real :class:`~nab_python.fetch.InMemoryIndex`.  The mock's
request methods write to the index and return an already-set
:class:`threading.Event`, so the synchronous provider code under test
sees fetches resolve immediately.

Unlike the real coordinator, the request methods take the published hashes
as required arguments, so a caller that stops forwarding them fails.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from nab_index.multi_index import IndexConfig
from nab_python.fetch import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, InMemoryIndex

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from nab_index.client import SdistFile, WheelFile
    from nab_index.lazy_wheel import RangeMetadataResult


_MINIMAL_METADATA = "Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"


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
    """Return a callable that picks metadata text for one sidecar."""

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


def _wire_metadata_side_effects(
    coordinator: MagicMock,
    index: InMemoryIndex,
    resolve_metadata: Callable[[str, str, str], str | None],
) -> None:
    """Attach ``request_listing``/``request_metadata``/batch side effects."""

    def _request_listing(
        _pkg: str,
        *,
        speculative: bool = False,  # noqa: ARG001 (mirrors the real keyword)
    ) -> threading.Event:
        return _done_event()

    def _fetch_metadata(pkg: str, ver: str, url: str) -> None:
        # The fetcher skips a sidecar the index already answers for.
        if index.has_metadata(pkg, ver, url):
            return
        # Store even when the sidecar returns nothing: an empty fetch still
        # marks the slot fetched, as real _fetch_metadata does.
        text = resolve_metadata(pkg, ver, url)
        index.store_metadata(pkg, ver, text, url)

    def _request_metadata(
        pkg: str, ver: str, url: str, _hash: tuple[str, str] | None
    ) -> threading.Event:
        _fetch_metadata(pkg, ver, url)
        return _done_event()

    def _request_metadata_batch(
        items: list[tuple[str, str, str, tuple[str, str] | None]],
    ) -> list[tuple[str, str, threading.Event]]:
        results: list[tuple[str, str, threading.Event]] = []
        for pkg, ver, url, _hash in items:
            _fetch_metadata(pkg, ver, url)
            results.append((pkg, ver, _done_event()))
        return results

    coordinator.request_listing.side_effect = _request_listing
    coordinator.request_metadata.side_effect = _request_metadata
    coordinator.request_metadata_batch.side_effect = _request_metadata_batch


def _wire_sdist_side_effects(
    coordinator: MagicMock,
    index: InMemoryIndex,
    *,
    sdist_pkg_info: str | None,
    sdist_pkg_info_by_version: Mapping[str, str | None] | None,
    sdist_pyproject_toml: str | None,
) -> None:
    """Attach the ``request_sdist`` side effect."""

    def _request_sdist(
        pkg: str,
        ver: str,
        _url: str,
        _hashes: tuple[tuple[str, str], ...],
    ) -> threading.Event:
        pkg_info = (
            sdist_pkg_info
            if sdist_pkg_info_by_version is None
            else sdist_pkg_info_by_version.get(ver)
        )

        # ``store_sdist_metadata`` is always called; passing ``None``
        # poisons the cache slot, matching the original test_provider
        # helper's contract for sdist-fetch failures.
        index.store_sdist_metadata(pkg, ver, pkg_info)
        if sdist_pyproject_toml is not None:
            index.store_sdist_pyproject(pkg, ver, sdist_pyproject_toml)
        return _done_event()

    coordinator.request_sdist.side_effect = _request_sdist


def _wire_range_side_effects(
    coordinator: MagicMock,
    index: InMemoryIndex,
    *,
    range_result: RangeMetadataResult | None,
    range_error: BaseException | None,
    range_by_url: Mapping[str, RangeMetadataResult] | None,
) -> None:
    """Attach the ``request_range_metadata`` side effect.

    Mirrors the coordinator's ``_fetch_range_metadata`` handler: a recorded
    ``range_error`` lands a per-wheel metadata error (the malformed-UTF-8
    blob, the unserveable wheel URL), otherwise the read stores the recovered
    METADATA or marks the read absent.  ``range_by_url`` selects a result per
    wheel URL, which is how sibling sidecar-less wheels of one version are
    given different dependencies; ``range_result`` is the single-result
    shortcut.  With none set the request is a no-op that still returns a done
    event, so a rung-4 read finds nothing and the ladder steps to the sdist
    rung.
    """

    def _request_range_metadata(
        pkg: str,
        ver: str,
        url: str,
        _hashes: tuple[tuple[str, str], ...] = (),
    ) -> threading.Event:
        if range_error is not None:
            index.store_range_error(pkg, ver, url, range_error)
            return _done_event()
        result = range_by_url.get(url) if range_by_url is not None else range_result
        if result is not None:
            index.store_range_outcome(pkg, ver, url, result.outcome)
            if result.text is None:
                index.store_range_absent(pkg, ver, url)
            else:
                index.store_range_metadata(pkg, ver, url, result.text)
        return _done_event()

    coordinator.request_range_metadata.side_effect = _request_range_metadata


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
) -> MagicMock:
    """Build a mock :class:`FetchCoordinator` backed by an :class:`InMemoryIndex`.

    Listing setup (one of):

    * ``wheels`` + ``package``: pre-load ``wheels`` under ``package``.
      Passing ``None`` skips listing setup, e.g. for tests that only
      need the coordinator handle.
    * ``listings``: pre-load each ``(package, wheels)`` pair.  Overrides
      ``wheels``/``package``.

    Request side effects:

    * ``request_listing`` always returns a set event.
    * ``request_metadata`` and ``request_metadata_batch`` write
      ``metadata_text`` (or the entry from ``metadata_by_url``, or from
      ``metadata_by_version``, or auto-generated minimal METADATA when
      ``auto_metadata`` is true) under the requested sidecar URL, as the
      fetcher does.  When nothing resolves, ``None`` lands in the sidecar
      slot, so the fetched-but-empty slot reads back the way it would in
      production.  ``metadata_by_url`` is how sibling wheels of one
      version are given different dependencies.
    * ``request_sdist`` writes ``sdist_pkg_info``, or the entry from
      ``sdist_pkg_info_by_version`` when sdists of several versions each need
      their own PKG-INFO, and, if not ``None``, ``sdist_pyproject_toml``.
    * ``request_range_metadata`` records the recovered METADATA (or an absent
      read when its text is ``None``), or lands ``range_error`` as a per-wheel
      metadata error.  ``range_by_url`` picks a result per wheel URL, so sibling
      sidecar-less wheels of one version get different dependencies;
      ``range_result`` is the single-result shortcut.  With none it is a no-op,
      so rung 4 finds nothing.

    Call sites that need request side effects beyond what this helper
    wires up (for example ``request_sdist_archive``) can reassign
    ``.side_effect`` on the returned mock; the index is exposed at
    ``coordinator.index`` for direct manipulation.  ``coordinator.indexes``
    defaults to the single default-PyPI :class:`IndexConfig` list, and
    ``coordinator.offline`` to False.
    """
    index = InMemoryIndex()

    _pre_populate_index(index, _resolve_listings(wheels, package, listings))

    coordinator = MagicMock()
    coordinator.index = index
    coordinator.indexes = [IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL)]
    coordinator.offline = False

    resolve_metadata = _make_metadata_resolver(
        metadata_text=metadata_text,
        metadata_by_version=metadata_by_version,
        metadata_by_url=metadata_by_url,
        auto_metadata=auto_metadata,
    )
    _wire_metadata_side_effects(coordinator, index, resolve_metadata)
    _wire_sdist_side_effects(
        coordinator,
        index,
        sdist_pkg_info=sdist_pkg_info,
        sdist_pkg_info_by_version=sdist_pkg_info_by_version,
        sdist_pyproject_toml=sdist_pyproject_toml,
    )
    _wire_range_side_effects(
        coordinator,
        index,
        range_result=range_result,
        range_error=range_error,
        range_by_url=range_by_url,
    )
    return coordinator
