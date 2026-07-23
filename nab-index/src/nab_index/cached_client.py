"""Disk-cached PyPI Simple API client.

Drives an :class:`~nab_index.transport.AsyncHttpTransport` and consults a
:class:`~nab_index.cache.CacheBackend` before any HTTP call. Honors a small
subset of RFC 9111: fresh entries are served directly, stale entries are
revalidated with ``If-None-Match``, and PEP 658 metadata + sdist PKG-INFO are
treated as immutable (cached forever; never revalidated).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .cache import CacheBackend, CachePolicy, OfflineError
from .client import (
    _HTTP_NOT_FOUND,
    DEFAULT_INDEX,
    MalformedSimpleResponseError,
    SdistFile,
    WheelFile,
    _extract_sdist_files,
    _parse_files,
    _select_artifact_hash,
    _verify_metadata_hash,
    _verify_sdist_hash,
)
from .lazy_wheel import (
    RangeCapabilityMemo,
    RangeMetadataResult,
    RangeOutcome,
    read_wheel_metadata_over_range,
)
from .parsed_listing import (
    corruption_reason as _parsed_corruption,
)
from .parsed_listing import (
    decode as _decode_parsed,
)
from .parsed_listing import (
    encode as _encode_parsed,
)
from .transport import IDENTITY_HEADERS

if TYPE_CHECKING:
    from packaging.utils import NormalizedName
    from typing_extensions import Self

    from .transport import AsyncHttpTransport, HttpResponse

__all__ = [
    "CachedAsyncSimpleClient",
    "ParsedCacheStats",
    "read_fresh_parsed_listing",
]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ParsedCacheStats:
    """Mutable counters for parsed-listing cache outcomes on the fetcher thread.

    One instance is shared across every per-index client in a run and read once
    after it, so a benchmark can confirm a warm resolve serves parsed blobs
    rather than reparsing.

    Each fresh-or-offline consult of ``get_files`` bumps exactly one counter:
    ``hit`` when a present blob binds the policy's body and is served without
    reading the raw body; ``miss`` when no blob was present; ``rebuild`` when a
    blob was present but did not bind (a stale digest, a different build, or
    corruption) and was rebuilt from the raw body.
    """

    hit: int = 0
    miss: int = 0
    rebuild: int = 0


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


def read_fresh_parsed_listing(
    cache: CacheBackend, package: str, *, offline: bool
) -> list[WheelFile | SdistFile] | None:
    """Read a clean fresh (or offline) parsed listing for ``package``, or ``None``.

    The synchronous, write-free subset of :meth:`CachedAsyncSimpleClient.get_files`'
    fresh-hit branch: it consults the same ``get_simple_policy``, the same
    ``policy.is_fresh() or offline`` freshness expression, the same
    ``get_simple_parsed`` blob, and the same :func:`parsed_listing.decode`, so on a
    hit it returns the identical records ``get_files`` would. It declines to
    ``None`` on every other reason (no policy, stale-online, no blob, a
    non-binding digest, or a corrupt blob) and, unlike ``get_files``, never reads
    the raw body, revalidates, rebuilds, writes, or logs: a decline routes the
    caller to the unchanged async path, which owns every write and self-heal.

    Total by construction (never raises): each cache read swallows ``OSError`` to
    ``None``, ``is_fresh`` is pure arithmetic, and ``decode`` returns ``None`` on
    any marshal/shape/build/digest failure.
    """
    policy = cache.get_simple_policy(package)
    if policy is None:
        return None
    if not (policy.is_fresh() or offline):
        return None
    blob = cache.get_simple_parsed(package)
    if blob is None:
        return None
    return _decode_parsed(blob, policy)


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
        parsed_stats: ParsedCacheStats | None = None,
    ) -> None:
        """Create a cached client wrapping ``transport``.

        ``range_memo`` is the per-run range-capability memo shared across the
        indexes' clients; the coordinator owns the shared instance and injects
        it. A fresh memo is built when none is passed, so a stand-alone client
        still learns each host's range behaviour within its own lifetime.

        ``parsed_stats`` is the per-run parsed-listing cache sink, shared the
        same way so hit/miss/rebuild counts total across every index client. A
        private sink is created when none is passed, so a stand-alone client
        still counts its own outcomes.
        """
        self._transport = transport
        self._cache = cache
        self._index_url = index_url.rstrip("/") + "/"
        self._offline = offline
        self._range_memo = (
            range_memo if range_memo is not None else RangeCapabilityMemo()
        )
        self._parsed_stats = (
            parsed_stats if parsed_stats is not None else ParsedCacheStats()
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

        Cache hit + fresh (or offline): the parsed-listing blob is
        rehydrated without reading the large raw body; on a blob miss,
        build/digest mismatch, or corruption the raw body is reparsed and the
        blob rebuilt (a WARNING self-heal on genuine corruption).
        Cache hit + stale + online: conditional revalidation; on 304 the body
        (and its parsed blob) are reused, on 200 both are replaced.
        Cache miss + offline: raises :class:`OfflineError`.
        Cache miss + online: fetches, caches, returns.

        The policy sidecar is read first: it carries the freshness window and
        the ``body_digest`` that gates the parsed blob, without the raw body. An
        absent policy is a full miss.

        A positive entry beats the negative sentinel, so the sentinel is
        consulted only on a positive miss. A fresh sentinel, or any sentinel
        offline, answers an absent name empty with no transport call; a
        stale sentinel online falls through to a fetch. A 404 records a
        sentinel.

        A cached body that will not decode as JSON is a corrupt positive:
        re-fetched online, raising :class:`OfflineError` offline. It is reached
        only after a parsed-blob miss reads the body, so the sentinel is not
        consulted then and a corrupt body never answers the name absent.
        """
        policy = self._cache.get_simple_policy(package)
        corrupt_positive = False
        if policy is not None:
            fresh_or_offline = policy.is_fresh() or self._offline
            if fresh_or_offline:
                hit = self._parsed_hit(package, policy)
                if hit is not None:
                    return hit
            # A parsed miss (fresh/offline) or a stale-online revalidation both
            # need the raw body now, so read it once here.
            cached = self._cache.get_simple(package)
            if cached is not None:
                body, policy = cached
                data = self._decode_cached_listing(body, package)
                if data is not None:
                    files = _parse_files(data, self._index_url, package)
                    if fresh_or_offline:
                        self._rebuild_parsed(package, body, policy, files)
                        return files
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

    def _parsed_hit(
        self, package: str, policy: CachePolicy
    ) -> list[WheelFile | SdistFile] | None:
        """Rehydrate the parsed blob for ``package``, or ``None`` on any miss.

        Returns the records only when a present blob decodes and its header
        binds ``policy``'s body. An absent blob, a build/digest mismatch, or a
        corrupt blob all return ``None`` so the caller reparses the raw body;
        genuine corruption (garbage/truncated bytes) is logged at WARNING as a
        self-heal, while a build/digest mismatch is a silent rebuild.

        This is the one place that reads the blob, so it records the outcome: a
        served blob counts a ``hit``, an absent one a ``miss``, and a
        present-but-unusable one a ``rebuild``.
        """
        blob = self._cache.get_simple_parsed(package)
        if blob is None:
            self._parsed_stats.miss += 1
            return None
        records = _decode_parsed(blob, policy)
        if records is not None:
            self._parsed_stats.hit += 1
            return records
        self._parsed_stats.rebuild += 1
        reason = _parsed_corruption(blob)
        if reason is not None:
            logger.warning(
                "Corrupt parsed-listing cache blob for %r from %s: %s; "
                "rebuilding from the raw body",
                package,
                self._index_url,
                reason,
            )
        return None

    def _rebuild_parsed(
        self,
        package: str,
        body: bytes,
        policy: CachePolicy,
        files: list[WheelFile | SdistFile],
    ) -> None:
        """Write a parsed blob for ``files`` bound to the on-disk ``body``.

        An older policy carries no ``body_digest``; the blob would never bind,
        so the digest is computed here and stamped into the refreshed policy so
        the next read hits. A policy that already carries a digest reuses it,
        sparing the body a rehash.
        """
        digest = policy.body_digest
        if digest is None:
            digest = hashlib.sha256(body).hexdigest()
            self._cache.put_simple_parsed(package, _encode_parsed(files, digest))
            self._cache.refresh_simple_policy(
                package, replace(policy, body_digest=digest)
            )
        else:
            self._cache.put_simple_parsed(package, _encode_parsed(files, digest))

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
        return _parse_files(data, self._index_url, package)

    async def _revalidate_simple(
        self, package: str, body: bytes, policy: CachePolicy
    ) -> list[WheelFile | SdistFile]:
        url = f"{self._index_url}{package}/"
        headers = {"Accept": _JSON_ACCEPT}
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
                # Body unchanged, so its digest carries forward and any parsed
                # blob written for the old policy still binds; no reparse.
                body_digest=policy.body_digest,
            )
            self._cache.refresh_simple_policy(package, new_policy)
            return self._parse_listing(body, package)

        if response.status_code == _HTTP_NOT_FOUND:
            self._cache.put_negative(package, self._negative_policy(response))
            return []
        response.raise_for_status()
        new_body = response.content

        # Parse before caching so a bad body never poisons the cache.
        files = self._parse_listing(new_body, package)

        new_policy = CachePolicy(
            fetched_at=int(time.time()),
            max_age=_parse_max_age(_header(response, "cache-control")),
            etag=_header(response, "etag"),
        )
        digest = self._cache.put_simple(package, new_body, new_policy)
        self._cache.put_simple_parsed(package, _encode_parsed(files, digest))
        return files

    async def _fetch_simple(self, package: str) -> list[WheelFile | SdistFile]:
        url = f"{self._index_url}{package}/"
        response = await self._transport.get(url, headers={"Accept": _JSON_ACCEPT})
        if response.status_code == _HTTP_NOT_FOUND:
            self._cache.put_negative(package, self._negative_policy(response))
            return []
        response.raise_for_status()
        body = response.content

        # Parse before caching so a bad body never poisons the cache.
        files = self._parse_listing(body, package)

        policy = CachePolicy(
            fetched_at=int(time.time()),
            max_age=_parse_max_age(_header(response, "cache-control")),
            etag=_header(response, "etag"),
        )
        digest = self._cache.put_simple(package, body, policy)
        self._cache.put_simple_parsed(package, _encode_parsed(files, digest))
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
        response.raise_for_status()
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
        response.raise_for_status()
        selected = _select_artifact_hash(sdist_hashes)
        if selected is not None:
            _verify_sdist_hash(response.content, selected)
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
        response.raise_for_status()
        selected = _select_artifact_hash(sdist_hashes)
        if selected is not None:
            _verify_sdist_hash(response.content, selected)
        return response.content
