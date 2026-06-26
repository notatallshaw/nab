"""Routing across an ordered list of named indexes for nab-index.

Each index in the list is named and addressable; the order alone is
semantically significant (no "primary vs extras" split).  Per package,
the router walks the list left-to-right and stops at the first index
whose listing for the package is non-empty (presence-based first-index,
matching uv's ``--index-strategy first-index``).

Per-package overrides route a package to a *named* index regardless
of order; when an override matches, *only* that index is consulted
(strict pin), matching uv's ``[tool.uv.sources]`` semantics.

Marker evaluation for overrides happens upstream; this layer just sees
a ``canonical_package_name -> index_name`` mapping that has already
been resolved against the active environment.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ._naming import canonical as _normalise_name

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from typing_extensions import Self

    from .client import SdistFile, WheelFile

__all__ = [
    "IndexClient",
    "IndexConfig",
    "MultiIndexClient",
]


class IndexClient(Protocol):
    """Structural protocol satisfied by Simple-API clients.

    The concrete implementations are
    :class:`~nab_index.cached_client.CachedAsyncSimpleClient` and
    :class:`~nab_index.local_index.LocalIndexClient`; tests can also
    supply a duck-typed stand-in.
    """

    async def get_files(self, package: str) -> list[WheelFile | SdistFile]:
        """Return the listing for ``package``."""
        ...

    async def get_metadata_text(
        self,
        package: str,
        version: str,
        metadata_url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> str:
        """Return the metadata text for a wheel."""
        ...

    async def get_sdist_files(
        self,
        package: str,
        version: str,
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str | None, str | None]:
        """Return ``(pkg_info_text, pyproject_text)`` for the sdist."""
        ...

    async def get_sdist_archive(
        self, package: str, version: str, sdist_url: str
    ) -> bytes:
        """Return the raw sdist archive bytes."""
        ...

    async def aclose(self) -> None:
        """Close any underlying transport."""
        ...


@dataclass(frozen=True, slots=True)
class IndexConfig:
    """Declares one index in the ordered list of indexes.

    ``name`` is the index identifier used by overrides and lockfile
    output.  ``url`` is the Simple API root (HTTPS or ``file://``).
    Order is significant: callers walk the list left-to-right and
    presence-based first-index applies.
    """

    name: str
    url: str


class MultiIndexClient:
    """Routes Simple API calls across an ordered list of named indexes.

    ``clients_by_name`` maps each index name to its client (HTTPS via
    :class:`CachedAsyncSimpleClient` or ``file://`` via
    :class:`LocalIndexClient`).  ``order`` is the ordered list of
    index names; the router walks it left-to-right when no override
    applies.  ``override_map`` is keyed by canonical (PEP 503)
    package name and maps to the chosen index name.

    All client instances are owned by the router and closed by
    :meth:`aclose`.  When the same client appears multiple times in
    ``clients_by_name`` (e.g. an alias), :meth:`aclose` closes it
    once.
    """

    def __init__(
        self,
        clients_by_name: Mapping[str, IndexClient],
        order: Sequence[str],
        override_map: Mapping[str, str],
    ) -> None:
        """Wire the router.  See module docstring for routing rules."""
        if not order:
            msg = "order must contain at least one index name"
            raise ValueError(msg)

        missing_order = [n for n in order if n not in clients_by_name]
        if missing_order:
            msg = f"order references unknown index names: {missing_order}"
            raise ValueError(msg)

        bad_overrides = [
            (pkg, idx)
            for pkg, idx in override_map.items()
            if idx not in clients_by_name
        ]
        if bad_overrides:
            msg = f"override targets unknown index names: {bad_overrides}"
            raise ValueError(msg)

        self._clients = dict(clients_by_name)
        self._order = list(order)
        self._override_map = {
            _normalise_name(name): index_name
            for name, index_name in override_map.items()
        }
        self._route_cache: dict[str, str] = {}
        self._route_lock = threading.Lock()

    async def aclose(self) -> None:
        """Close every owned client exactly once."""
        seen: set[int] = set()
        unique: list[IndexClient] = []
        for client in self._clients.values():
            if id(client) in seen:
                continue
            seen.add(id(client))
            unique.append(client)
        await asyncio.gather(
            *(c.aclose() for c in unique),
            return_exceptions=True,
        )

    async def __aenter__(self) -> Self:
        """Return self."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Close all clients on exit."""
        await self.aclose()

    def _override_index(self, package: str) -> str | None:
        return self._override_map.get(_normalise_name(package))

    async def get_files(self, package: str) -> list[WheelFile | SdistFile]:
        """Return the chosen index's listing for ``package``.

        See module docstring for routing.  The chosen index name is
        cached so subsequent metadata / sdist calls hit the same
        client.
        """
        override = self._override_index(package)
        if override is not None:
            client = self._clients[override]
            files = await client.get_files(package)
            self._record_route(package, override)
            return files

        cached = self._cached_route_name(package)
        if cached is not None:
            return await self._clients[cached].get_files(package)

        for index_name in self._order:
            client = self._clients[index_name]
            files = await client.get_files(package)
            if files:
                self._record_route(package, index_name)
                return files

        # Nothing found anywhere: route to the first index so
        # subsequent metadata calls (which will also miss) hit a
        # stable client.
        self._record_route(package, self._order[0])
        return []

    async def get_metadata_text(
        self,
        package: str,
        version: str,
        metadata_url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> str:
        """Forward to the routed client; presupposes ``get_files`` was called."""
        return await self._client_for(package).get_metadata_text(
            package, version, metadata_url, metadata_hash
        )

    async def get_sdist_files(
        self,
        package: str,
        version: str,
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str | None, str | None]:
        """Forward to the routed client; presupposes ``get_files`` was called."""
        return await self._client_for(package).get_sdist_files(
            package, version, sdist_url, sdist_hashes
        )

    async def get_sdist_archive(
        self,
        package: str,
        version: str,
        sdist_url: str,
    ) -> bytes:
        """Forward to the routed client; presupposes ``get_files`` was called."""
        return await self._client_for(package).get_sdist_archive(
            package, version, sdist_url
        )

    def _client_for(self, package: str) -> IndexClient:
        cached = self._cached_route_name(package)
        if cached is not None:
            return self._clients[cached]
        return self._clients[self._order[0]]

    def _cached_route_name(self, package: str) -> str | None:
        canonical = _normalise_name(package)
        with self._route_lock:
            return self._route_cache.get(canonical)

    def route_for(self, package: str) -> str | None:
        """Return the index name that served ``package``, or ``None``.

        ``None`` indicates the router has not seen the package yet:
        no listing call has resolved its location.  Callers must
        treat the return value as best-effort and fall back to a
        configured default when ``None`` comes back.
        """
        return self._cached_route_name(package)

    def _record_route(self, package: str, index_name: str) -> None:
        canonical = _normalise_name(package)
        with self._route_lock:
            self._route_cache[canonical] = index_name
