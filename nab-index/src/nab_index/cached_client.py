"""Disk-cached PyPI Simple API client.

Drives an :class:`~nab_index.transport.AsyncHttpTransport` and consults a
:class:`~nab_index.cache.CacheBackend` before any HTTP call. Honors a small
subset of RFC 9111: fresh entries are served directly, stale entries are
revalidated with ``If-None-Match``, and PEP 658 metadata + sdist PKG-INFO are
treated as immutable (cached forever; never revalidated).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING

from .cache import CacheBackend, CachePolicy, OfflineError
from .client import (
    _HTTP_NOT_FOUND,
    DEFAULT_INDEX,
    MalformedSimpleResponseError,
    SdistFile,
    WheelFile,
    _extract_sdist_files,
    _header,
    _listing_body,
    _parse_files,
    _select_artifact_hash,
    _verify_metadata_hash,
    holds_unreadable_format,
    verify_sdist_hash,
)
from .lazy_wheel import (
    RangeCapabilityMemo,
    RangeMetadataResult,
    RangeOutcome,
    read_wheel_metadata_over_range,
)
from .serialization import SimpleSerialization, simple_accept_header
from .transport import IDENTITY_HEADERS, raise_unless_ok

if TYPE_CHECKING:
    from packaging.utils import NormalizedName
    from typing_extensions import Self

    from .transport import AsyncHttpTransport, HttpResponse

__all__ = [
    "CachedAsyncSimpleClient",
]

logger = logging.getLogger(__name__)


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


class CachedAsyncSimpleClient:
    """Async PyPI Simple API client with on-disk caching.

    Distinct from :class:`AsyncSimpleClient`: the interface mirrors what
    :class:`FetchCoordinator` actually needs rather than the Simple API.
    """

    def __init__(
        self,
        transport: AsyncHttpTransport,
        cache: CacheBackend,
        index_url: str = DEFAULT_INDEX,
        *,
        offline: bool = False,
        range_memo: RangeCapabilityMemo | None = None,
        serialization: SimpleSerialization = SimpleSerialization.NEGOTIATE,
    ) -> None:
        """Create a cached client wrapping ``transport``.

        ``range_memo`` is the per-run range-capability memo shared across the
        indexes' clients; the coordinator owns the shared instance and injects
        it. A fresh memo is built when none is passed, so a stand-alone client
        still learns each host's range behaviour within its own lifetime.

        ``serialization`` pins which Simple-API serialization this index is
        asked for and read as.
        """
        self._transport = transport
        self._cache = cache
        self._index_url = index_url.rstrip("/") + "/"
        self._offline = offline
        self._serialization = serialization
        self._unreadable_only: set[str] = set()
        self._range_memo = (
            range_memo if range_memo is not None else RangeCapabilityMemo()
        )

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

        A positive entry beats the negative sentinel, so the sentinel is
        consulted only on a positive miss. A fresh sentinel, or any sentinel
        offline, answers an absent name empty with no transport call; a
        stale sentinel online falls through to a fetch. A 404 records a
        sentinel.

        A cached body that will not decode as JSON is a corrupt positive:
        re-fetched online, raising :class:`OfflineError` offline. The
        sentinel is not consulted then, so a corrupt body never answers the
        name absent.
        """
        cached = self._cache.get_simple(package)
        corrupt_positive = False
        if cached is not None:
            body, policy = cached
            data = self._decode_cached_listing(body, package)
            if data is not None:
                if policy.is_fresh() or self._offline:
                    return self._parse_body(data, package)
                return await self._revalidate_simple(package, body, policy)
            corrupt_positive = True

        if not corrupt_positive:
            negative = self._cache.get_negative(package)
            if negative is not None and (negative.is_fresh() or self._offline):
                return []

        if self._offline:
            msg = f"No cached listing for {package} (offline mode)"
            raise OfflineError(msg)
        return await self._fetch_simple(package)

    def _negative_policy(self, response: HttpResponse) -> CachePolicy:
        """Freshness policy for a name-level 404, clamped to the 600s cap."""
        max_age = min(
            _parse_max_age(_header(response, "cache-control")), _DEFAULT_MAX_AGE
        )
        return CachePolicy(fetched_at=int(time.time()), max_age=max_age, etag=None)

    def _decode_cached_listing(self, body: bytes, package: str) -> object | None:
        """Return the parsed JSON of a cached Simple body, or ``None``.

        A body that will not decode as JSON is logged and treated as a miss.
        A body that decodes but is the wrong shape is not caught here:
        :func:`_parse_files` raises on it, the same as on the wire path.
        """
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(
                "Corrupt cached Simple-API body for %r from %s: not valid JSON; "
                "treating as a miss and re-fetching",
                package,
                self._index_url,
            )
            return None

    def _parse_listing(self, body: bytes, package: str) -> list[WheelFile | SdistFile]:
        """Parse a Simple-API listing body.

        A body that is not valid JSON raises the same
        :class:`MalformedSimpleResponseError` as a valid-JSON body of the
        wrong shape, not a raw decode error. ``json.loads`` on non-UTF-8
        bytes raises :class:`UnicodeDecodeError`, not
        :class:`json.JSONDecodeError`, so both are caught.
        """
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = (
                f"{self._index_url} served a malformed Simple-API response for "
                f"{package!r}: body is not valid JSON"
            )
            raise MalformedSimpleResponseError(msg) from exc
        return self._parse_body(data, package)

    def _parse_body(self, data: object, package: str) -> list[WheelFile | SdistFile]:
        """Parse a decoded listing body, marking one with no readable file."""
        files = _parse_files(data, self._index_url, package)
        if not files and holds_unreadable_format(data):
            self._unreadable_only.add(package)
        return files

    def served_unreadable_only(self, package: str) -> bool:
        """Whether a listing for ``package`` held only files nab cannot read."""
        return package in self._unreadable_only

    async def _revalidate_simple(
        self, package: str, body: bytes, policy: CachePolicy
    ) -> list[WheelFile | SdistFile]:
        url = f"{self._index_url}{package}/"
        headers = {"Accept": simple_accept_header(self._serialization)}
        if policy.etag is not None:
            headers["If-None-Match"] = policy.etag
        response = await self._transport.get(url, headers=headers)
        if response.status_code == _HTTP_NOT_MODIFIED:
            # A 304 without cache-control keeps the stored max-age.
            cache_control = _header(response, "cache-control")
            new_policy = CachePolicy(
                fetched_at=int(time.time()),
                max_age=(
                    _parse_max_age(cache_control)
                    if cache_control is not None
                    else policy.max_age
                ),
                etag=_header(response, "etag") or policy.etag,
            )
            self._cache.refresh_simple_policy(package, new_policy)
            return self._parse_listing(body, package)

        if response.status_code == _HTTP_NOT_FOUND:
            self._cache.put_negative(package, self._negative_policy(response))
            return []
        raise_unless_ok(response, url)
        new_body = _listing_body(
            response, self._index_url, package, self._serialization
        )

        # Parse before caching so a bad body never poisons the cache.
        files = self._parse_listing(new_body, package)

        new_policy = CachePolicy(
            fetched_at=int(time.time()),
            max_age=_parse_max_age(_header(response, "cache-control")),
            etag=_header(response, "etag"),
        )
        self._cache.put_simple(package, new_body, new_policy)
        return files

    async def _fetch_simple(self, package: str) -> list[WheelFile | SdistFile]:
        url = f"{self._index_url}{package}/"
        accept = simple_accept_header(self._serialization)
        response = await self._transport.get(url, headers={"Accept": accept})
        if response.status_code == _HTTP_NOT_FOUND:
            self._cache.put_negative(package, self._negative_policy(response))
            return []
        raise_unless_ok(response, url)
        body = _listing_body(response, self._index_url, package, self._serialization)

        # Parse before caching so a bad body never poisons the cache.
        files = self._parse_listing(body, package)

        policy = CachePolicy(
            fetched_at=int(time.time()),
            max_age=_parse_max_age(_header(response, "cache-control")),
            etag=_header(response, "etag"),
        )
        self._cache.put_simple(package, body, policy)
        self._cache.drop_negative(package)
        return files

    async def get_metadata_text(
        self,
        package: str,
        version: str,
        metadata_url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> str:
        """Return the PEP 658 metadata text published at ``metadata_url``.

        ``version`` names the coordinate being resolved; the cache entry
        stands for the sidecar at the URL.

        Treated as immutable: cached forever, never revalidated.  A
        cache hit is returned without re-checking, since it was
        verified before being stored.  Cache miss + offline raises
        :class:`OfflineError`.  When ``metadata_hash`` is given, the
        fetched bytes are verified against it and a mismatch raises
        :class:`MetadataHashMismatchError` before anything is cached.

        Metadata is decoded from the hash-verified bytes as utf-8 rather
        than the transport's ``.text``, which a backend may decode under
        the response Content-Type charset. This keeps the parsed text
        tied to the bytes the hash covers.  A body that is not valid utf-8
        raises :class:`MalformedSimpleResponseError` (an :class:`HttpError`
        subclass) rather than a raw :class:`UnicodeDecodeError`.
        """
        cached = self._cache.get_metadata(package, metadata_url)
        if cached is not None:
            return cached

        if self._offline:
            msg = f"No cached metadata for {package}=={version} (offline mode)"
            raise OfflineError(msg)

        response = await self._transport.get(metadata_url)
        raise_unless_ok(response, metadata_url)
        content = response.content
        if metadata_hash is not None:
            _verify_metadata_hash(content, metadata_hash)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = (
                f"{metadata_url} served a malformed PEP 658 metadata sidecar "
                f"for {package}=={version}: body is not valid UTF-8"
            )
            raise MalformedSimpleResponseError(msg) from exc
        self._cache.put_metadata(package, metadata_url, text)
        return text

    async def get_range_metadata(
        self,
        package: str,
        version: str,
        wheel_url: str,
        canonical_name: NormalizedName,
        wheel_hashes: tuple[tuple[str, str], ...] = (),
    ) -> RangeMetadataResult:
        """Recover a sidecar-less wheel's METADATA by HTTP range reads.

        The recovered text is cached under the same immutable ``metadata-v1``
        store the PEP 658 sidecar uses, keyed by the wheel URL. A cache hit is
        returned without any transport call, so a warm or previously-warmed
        offline resolve never re-ranges. A cold miss in offline mode raises
        :class:`OfflineError`; otherwise the reader drives the transport with
        the shared range-capability memo. Only a successful read is cached; an
        ``UNSUPPORTED`` or ``MISSING`` result writes nothing so the caller can
        step to the sdist rung.

        When ``wheel_hashes`` carries an acceptable published digest, a
        full-body acquisition is verified against it before its METADATA is
        read, mirroring the sdist path. A mismatch raises
        :class:`WheelHashMismatchError` and nothing is cached.
        """
        cached = self._cache.get_metadata(package, wheel_url)
        if cached is not None:
            return RangeMetadataResult(cached, RangeOutcome.PARTIAL)

        if self._offline:
            msg = f"No cached range metadata for {package}=={version} (offline mode)"
            raise OfflineError(msg)

        result = await read_wheel_metadata_over_range(
            self._transport,
            wheel_url,
            canonical_name,
            self._range_memo,
            wheel_hash=_select_artifact_hash(wheel_hashes),
        )
        if result.text is not None:
            self._cache.put_metadata(package, wheel_url, result.text)
        return result

    async def get_sdist_files(
        self,
        package: str,
        version: str,
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str | None, str | None]:
        """Return ``(pkg_info, pyproject_toml)`` for an sdist, caching both.

        Cache miss + offline raises :class:`OfflineError`.  Either
        element may be ``None`` if the corresponding file is absent
        from the archive (or the archive cannot be parsed).  Both are
        treated as immutable: cached forever, never revalidated.

        When ``sdist_hashes`` carries an acceptable published digest, the
        downloaded archive is verified against it before extraction. A
        mismatch raises :class:`SdistHashMismatchError` and nothing is
        cached.
        """
        cached = self._cache.get_sdist_files(package, version)
        if cached is not None:
            return cached

        if self._offline:
            msg = f"No cached sdist PKG-INFO for {package}=={version} (offline mode)"
            raise OfflineError(msg)

        response = await self._transport.get(sdist_url, headers=IDENTITY_HEADERS)
        raise_unless_ok(response, sdist_url)
        selected = _select_artifact_hash(sdist_hashes)
        if selected is not None:
            verify_sdist_hash(response.content, selected)
        pkg_info, pyproject_toml = _extract_sdist_files(response.content)

        if pkg_info is not None or pyproject_toml is not None:
            self._cache.put_sdist_files(package, version, pkg_info, pyproject_toml)

        return (pkg_info, pyproject_toml)

    async def get_sdist_archive(
        self,
        package: str,
        version: str,
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> bytes:
        """Return the raw bytes of an sdist archive.

        Used by the ``BUILD_REMOTE`` path when a real backend invocation
        is required.  No on-disk caching is performed: archives are
        large, builds are rare, and the in-memory index already
        deduplicates within a single resolve.  Offline mode raises
        :class:`OfflineError` because there is no slot to read from.

        When ``sdist_hashes`` carries an acceptable published digest, the
        downloaded archive is verified before its bytes are returned. A
        mismatch raises :class:`SdistHashMismatchError`.
        """
        del package, version  # offline check below is the only use
        if self._offline:
            msg = f"sdist archive fetch unavailable in offline mode ({sdist_url})"
            raise OfflineError(msg)
        response = await self._transport.get(sdist_url, headers=IDENTITY_HEADERS)
        raise_unless_ok(response, sdist_url)
        selected = _select_artifact_hash(sdist_hashes)
        if selected is not None:
            verify_sdist_hash(response.content, selected)
        return response.content
