"""Disk-cached PyPI Simple API client.

Wraps :class:`AsyncSimpleClient`. Consults an :class:`OnDiskCache`
before any HTTP transport call. Honors a small subset of RFC 9111:
fresh entries are served directly, stale entries are revalidated with
``If-None-Match``, and PEP 658 metadata + sdist PKG-INFO are treated
as immutable (cached forever; never revalidated).

Cache hits short-circuit before any HTTP code runs. With a fully
populated cache the client never opens a socket.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

from .cache import CacheBackend, CachePolicy, OfflineError
from .client import (
    _HTTP_NOT_FOUND,
    DEFAULT_INDEX,
    SdistFile,
    WheelFile,
    _extract_sdist_files,
    _parse_files,
)

if TYPE_CHECKING:
    from typing_extensions import Self

    from .transport import AsyncHttpTransport, HttpResponse

__all__ = [
    "CachedAsyncSimpleClient",
]


_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"
_DEFAULT_MAX_AGE = 600
_HTTP_NOT_MODIFIED = 304
_MAX_AGE_RE = re.compile(r"max-age\s*=\s*(\d+)")


def _parse_max_age(cache_control: str | None) -> int:
    if cache_control is None:
        return _DEFAULT_MAX_AGE
    match = _MAX_AGE_RE.search(cache_control)
    if match is None:
        return _DEFAULT_MAX_AGE
    return int(match.group(1))


def _header(response: HttpResponse, key: str) -> str | None:
    """Case-insensitive header lookup.

    The :class:`HttpResponse` Protocol only promises a plain
    :class:`Mapping`. Both real transports (httpx, urllib3) return
    case-insensitive header containers, but we don't rely on
    that here so a plain-dict fake also works.
    """
    headers = response.headers
    target = key.lower()
    for name, value in headers.items():
        if name.lower() == target:
            return value
    return None


class CachedAsyncSimpleClient:
    """Async PyPI Simple API client with on-disk caching.

    Distinct from :class:`AsyncSimpleClient`: the metadata methods
    take ``(package, version)`` so the cache can key by package
    coordinate rather than URL. The interface mirrors what
    :class:`FetchCoordinator` actually needs.
    """

    def __init__(
        self,
        transport: AsyncHttpTransport,
        cache: CacheBackend,
        index_url: str = DEFAULT_INDEX,
        *,
        offline: bool = False,
    ) -> None:
        """Create a cached client wrapping ``transport``."""
        self._transport = transport
        self._cache = cache
        self._index_url = index_url.rstrip("/") + "/"
        self._offline = offline

    async def aclose(self) -> None:
        """Close the underlying transport."""
        await self._transport.aclose()

    async def __aenter__(self) -> Self:
        """Enter the async context manager."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit the async context manager and close the transport."""
        await self.aclose()

    async def get_files(self, package: str) -> list[WheelFile | SdistFile]:
        """Return parsed Simple API file list for ``package``.

        Cache hit + fresh: parses cached body, no network.
        Cache hit + stale + online: conditional revalidation; on 304
        the body is reused, on 200 the body is replaced.
        Cache hit + offline: cached body is returned regardless of age.
        Cache miss + offline: raises :class:`OfflineError`.
        Cache miss + online: fetches, caches, returns.
        A 404 from the index yields an empty listing and is not cached.
        """
        cached = self._cache.get_simple(package)
        if cached is not None:
            body, policy = cached
            if policy.is_fresh() or self._offline:
                return _parse_files(json.loads(body), self._index_url, package)
            return await self._revalidate_simple(package, body, policy)

        if self._offline:
            msg = f"No cached listing for {package} (offline mode)"
            raise OfflineError(msg)
        return await self._fetch_simple(package)

    async def _revalidate_simple(
        self, package: str, body: bytes, policy: CachePolicy
    ) -> list[WheelFile | SdistFile]:
        url = f"{self._index_url}{package}/"
        headers = {"Accept": _JSON_ACCEPT}
        if policy.etag is not None:
            headers["If-None-Match"] = policy.etag
        response = await self._transport.get(url, headers=headers)
        if response.status_code == _HTTP_NOT_MODIFIED:
            new_policy = CachePolicy(
                fetched_at=int(time.time()),
                max_age=_parse_max_age(_header(response, "cache-control")),
                etag=_header(response, "etag") or policy.etag,
            )
            self._cache.refresh_simple_policy(package, new_policy)
            return _parse_files(json.loads(body), self._index_url, package)

        if response.status_code == _HTTP_NOT_FOUND:
            return []
        response.raise_for_status()
        new_body = response.content
        new_policy = CachePolicy(
            fetched_at=int(time.time()),
            max_age=_parse_max_age(_header(response, "cache-control")),
            etag=_header(response, "etag"),
        )
        self._cache.put_simple(package, new_body, new_policy)
        return _parse_files(json.loads(new_body), self._index_url, package)

    async def _fetch_simple(self, package: str) -> list[WheelFile | SdistFile]:
        url = f"{self._index_url}{package}/"
        response = await self._transport.get(url, headers={"Accept": _JSON_ACCEPT})
        if response.status_code == _HTTP_NOT_FOUND:
            return []
        response.raise_for_status()
        body = response.content
        policy = CachePolicy(
            fetched_at=int(time.time()),
            max_age=_parse_max_age(_header(response, "cache-control")),
            etag=_header(response, "etag"),
        )
        self._cache.put_simple(package, body, policy)
        return _parse_files(json.loads(body), self._index_url, package)

    async def get_metadata_text(
        self, package: str, version: str, metadata_url: str
    ) -> str:
        """Return PEP 658 metadata text for ``(package, version)``.

        Treated as immutable: cached forever, never revalidated.
        Cache miss + offline raises :class:`OfflineError`.
        """
        cached = self._cache.get_metadata(package, version)
        if cached is not None:
            return cached

        if self._offline:
            msg = f"No cached metadata for {package}=={version} (offline mode)"
            raise OfflineError(msg)

        response = await self._transport.get(metadata_url)
        response.raise_for_status()
        text = response.text
        self._cache.put_metadata(package, version, text)
        return text

    async def get_sdist_files(
        self, package: str, version: str, sdist_url: str
    ) -> tuple[str | None, str | None]:
        """Return ``(pkg_info, pyproject_toml)`` for an sdist, caching both.

        Cache miss + offline raises :class:`OfflineError`.  Either
        element may be ``None`` if the corresponding file is absent
        from the archive (or the archive cannot be parsed).  Both are
        treated as immutable: cached forever, never revalidated.
        """
        pkg_info = self._cache.get_sdist_pkginfo(package, version)
        pyproject_toml = self._cache.get_sdist_pyproject(package, version)
        if pkg_info is not None or pyproject_toml is not None:
            return (pkg_info, pyproject_toml)

        if self._offline:
            msg = f"No cached sdist PKG-INFO for {package}=={version} (offline mode)"
            raise OfflineError(msg)

        response = await self._transport.get(sdist_url)
        response.raise_for_status()
        pkg_info, pyproject_toml = _extract_sdist_files(response.content)

        if pkg_info is not None:
            self._cache.put_sdist_pkginfo(package, version, pkg_info)
        if pyproject_toml is not None:
            self._cache.put_sdist_pyproject(package, version, pyproject_toml)

        return (pkg_info, pyproject_toml)

    async def get_sdist_archive(
        self, package: str, version: str, sdist_url: str
    ) -> bytes:
        """Return the raw bytes of an sdist archive.

        Used by the ``BUILD_REMOTE`` path when a real backend invocation
        is required.  No on-disk caching is performed: archives are
        large, builds are rare, and the in-memory index already
        deduplicates within a single resolve.  Offline mode raises
        :class:`OfflineError` because there is no slot to read from.
        """
        del package, version  # offline check below is the only use
        if self._offline:
            msg = f"sdist archive fetch unavailable in offline mode ({sdist_url})"
            raise OfflineError(msg)
        response = await self._transport.get(sdist_url)
        response.raise_for_status()
        return response.content
