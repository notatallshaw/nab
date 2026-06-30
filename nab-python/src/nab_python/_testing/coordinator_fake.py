"""Shared mock-coordinator builder for the nab-python test suite.

A ``FetchCoordinator``-shaped :class:`unittest.mock.MagicMock` wrapped
around a real :class:`~nab_python.fetch.InMemoryIndex`.  The mock's
request methods write to the index and return an already-set
:class:`threading.Event`, so the synchronous provider code under test
sees fetches resolve immediately.
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


_MINIMAL_METADATA = "Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"


def _done_event() -> threading.Event:
    """Return an already-set :class:`threading.Event`."""
    ev = threading.Event()
    ev.set()
    return ev


def _pre_populate_index(
    index: InMemoryIndex,
    listings_map: Mapping[str, Sequence[WheelFile | SdistFile]],
    *,
    baseline_metadata: Mapping[str, str] | None,
    per_wheel_metadata: Mapping[str, str] | None,
    sdist_pyproject: Mapping[str, str] | None,
) -> None:
    """Load listings and pre-store validator-visible slots into ``index``."""
    for pkg_name, pkg_wheels in listings_map.items():
        index.store_listing(pkg_name, pkg_wheels)
        # Mirror production: every fetched listing records its serving index.
        index.store_listing_index(pkg_name, DEFAULT_INDEX_NAME)
        if baseline_metadata is not None and pkg_name in baseline_metadata:
            for w in pkg_wheels:
                index.store_metadata(pkg_name, w.version, baseline_metadata[pkg_name])
                break
        if per_wheel_metadata is not None:
            for w in pkg_wheels:
                if w.filename in per_wheel_metadata:
                    index.store_metadata(
                        pkg_name,
                        f"{w.version}#{w.filename}",
                        per_wheel_metadata[w.filename],
                    )
        if sdist_pyproject is not None and pkg_name in sdist_pyproject:
            for w in pkg_wheels:
                index.store_sdist_pyproject(
                    pkg_name, w.version, sdist_pyproject[pkg_name]
                )
                break


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
    auto_metadata: bool,
) -> Callable[[str, str], str | None]:
    """Return a callable that picks metadata text for ``(pkg, version)``."""

    def _resolve(pkg: str, ver: str) -> str | None:
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
    resolve_metadata: Callable[[str, str], str | None],
) -> None:
    """Attach ``request_listing``/``request_metadata``/batch side effects."""

    def _request_listing(_pkg: str) -> threading.Event:
        return _done_event()

    def _request_metadata(
        pkg: str, ver: str, _url: str, _hash: tuple[str, str] | None = None
    ) -> threading.Event:
        text = resolve_metadata(pkg, ver)
        if text is not None:
            index.store_metadata(pkg, ver, text)
        return _done_event()

    def _request_metadata_batch(
        items: list[tuple[str, str, str, tuple[str, str] | None]],
    ) -> list[tuple[str, str, threading.Event]]:
        results: list[tuple[str, str, threading.Event]] = []
        for pkg, ver, _url, _hash in items:
            text = resolve_metadata(pkg, ver)
            if text is not None:
                index.store_metadata(pkg, ver, text)
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
    sdist_pyproject_toml: str | None,
    failures: set[str],
) -> None:
    """Attach ``request_sdist`` and ``request_wheel_metadata`` side effects."""

    def _request_sdist(
        pkg: str,
        ver: str,
        _url: str,
        _hashes: tuple[tuple[str, str], ...] = (),
    ) -> threading.Event:
        # ``store_sdist_metadata`` is always called; passing ``None``
        # poisons the cache slot, matching the original test_provider
        # helper's contract for sdist-fetch failures.
        index.store_sdist_metadata(pkg, ver, sdist_pkg_info)
        if sdist_pyproject_toml is not None:
            index.store_sdist_pyproject(pkg, ver, sdist_pyproject_toml)
        return _done_event()

    def _request_wheel_metadata(
        pkg: str,
        ver: str,
        filename: str,
        _url: str,
        _hash: tuple[str, str] | None = None,
    ) -> threading.Event:
        if filename in failures:
            index.store_metadata(pkg, f"{ver}#{filename}", None)
        return _done_event()

    coordinator.request_sdist.side_effect = _request_sdist
    coordinator.request_wheel_metadata.side_effect = _request_wheel_metadata


def make_coordinator(  # noqa: PLR0913
    wheels: Sequence[WheelFile | SdistFile] | None = None,
    *,
    package: str = "pkg",
    listings: Mapping[str, Sequence[WheelFile | SdistFile]] | None = None,
    metadata_text: str | None = None,
    metadata_by_version: Mapping[str, str | None] | None = None,
    auto_metadata: bool = False,
    sdist_pkg_info: str | None = None,
    sdist_pyproject_toml: str | None = None,
    baseline_metadata: Mapping[str, str] | None = None,
    per_wheel_metadata: Mapping[str, str] | None = None,
    sdist_pyproject: Mapping[str, str] | None = None,
    fetch_failures: set[str] | None = None,
) -> MagicMock:
    """Build a mock :class:`FetchCoordinator` backed by an :class:`InMemoryIndex`.

    Listing setup (one of):

    * ``wheels`` + ``package``: pre-load ``wheels`` under ``package``.
      Passing ``None`` skips listing setup, e.g. for tests that only
      need the coordinator handle.
    * ``listings``: pre-load each ``(package, wheels)`` pair.  Overrides
      ``wheels``/``package``.

    Request side effects:

    * ``request_listing`` and ``request_wheel_metadata`` always return a
      set event.  ``request_wheel_metadata`` honours ``fetch_failures``:
      filenames in the set store ``None`` at the sentinel
      ``f"{version}#{filename}"`` key.
    * ``request_metadata`` and ``request_metadata_batch`` write
      ``metadata_text`` (or the entry from ``metadata_by_version``, or
      auto-generated minimal METADATA when ``auto_metadata`` is true).
    * ``request_sdist`` writes ``sdist_pkg_info`` and, if not ``None``,
      ``sdist_pyproject_toml``.

    Pre-stores written directly to the index before the coordinator
    fires:

    * ``baseline_metadata`` keys on package name and writes once per
      package using the first wheel's version.
    * ``per_wheel_metadata`` keys on wheel filename and writes at the
      validator's ``f"{version}#{filename}"`` sentinel.
    * ``sdist_pyproject`` keys on package name and writes the
      pyproject.toml text used by the PEP 621 fast path.

    Call sites that need request side effects beyond what this helper
    wires up (for example ``request_sdist_archive``) can reassign
    ``.side_effect`` on the returned mock; the index is exposed at
    ``coordinator.index`` for direct manipulation.  ``coordinator.indexes``
    defaults to the single default-PyPI :class:`IndexConfig` list.
    """
    index = InMemoryIndex()
    failures = fetch_failures if fetch_failures is not None else set()

    listings_map = _resolve_listings(wheels, package, listings)
    _pre_populate_index(
        index,
        listings_map,
        baseline_metadata=baseline_metadata,
        per_wheel_metadata=per_wheel_metadata,
        sdist_pyproject=sdist_pyproject,
    )

    coordinator = MagicMock()
    coordinator.index = index
    coordinator.indexes = [IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL)]

    resolve_metadata = _make_metadata_resolver(
        metadata_text=metadata_text,
        metadata_by_version=metadata_by_version,
        auto_metadata=auto_metadata,
    )
    _wire_metadata_side_effects(coordinator, index, resolve_metadata)
    _wire_sdist_side_effects(
        coordinator,
        index,
        sdist_pkg_info=sdist_pkg_info,
        sdist_pyproject_toml=sdist_pyproject_toml,
        failures=failures,
    )
    return coordinator
