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
from datetime import timezone
from email.utils import parsedate_to_datetime
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
# RFC 9111 5.2: directive names are case-insensitive and the argument may be quoted.
_MAX_AGE_RE = re.compile(r'max-age\s*=\s*"?(\d+)', re.IGNORECASE)
_AGE_RE = re.compile(r"\A\s*(\d+)\s*\Z")
_SECONDS_CEILING = 2**31
_SECONDS_CEILING_DIGITS = len(str(_SECONDS_CEILING))


def _parse_seconds(digits: str) -> int:
    """Return a ``delta-seconds`` digit run as an int.

    RFC 9110 5.6.7 allows leading zeros, sets no length limit on the run, and
    caps a value too large to represent at 2**31. ``int`` refuses a string past
    4300 digits, so the padding is stripped before the run is measured.
    """
    trimmed = digits.lstrip("0")

    if len(trimmed) > _SECONDS_CEILING_DIGITS:
        return _SECONDS_CEILING
    return min(int(trimmed or "0"), _SECONDS_CEILING)


def _max_age_directive(cache_control: str | None) -> int | None:
    """Return the ``max-age`` a Cache-Control field carries, or ``None``."""
    if cache_control is None:
        return None
    match = _MAX_AGE_RE.search(cache_control)
    if match is None:
        return None
    return _parse_seconds(match.group(1))


def _parse_age(age: str | None) -> int:
    """Return the seconds a response spent in caches before nab received it.

    An absent header, or one that is not a bare run of digits, reads as 0.
    """
    if age is None:
        return 0
    match = _AGE_RE.match(age)
    if match is None:
        return 0
    return _parse_seconds(match.group(1))


def _freshness_start(response: HttpResponse) -> int:
    """Return when ``response`` opened its freshness window.

    A shared cache reports as Age how long ago the origin generated the
    representation, so a relayed response arrives that far into its window.
    Only the Age term of RFC 9111 4.2.3 is applied; the apparent age computed
    from the Date header is not.
    """
    return int(time.time()) - _parse_age(_header(response, "age"))


def _http_date_seconds(value: str | None) -> int | None:
    """Return an HTTP-date as Unix seconds, or ``None`` if it is not a date.

    RFC 9110 5.6.7 writes every HTTP-date in GMT, so the zone-less obsolete
    asctime form reads as GMT rather than as local time. A year, day, hour or
    zone offset too large for a C int raises :class:`OverflowError` rather than
    :class:`ValueError`; neither is a date.
    """
    if value is None:
        return None

    try:
        parsed = parsedate_to_datetime(value)
    except (ValueError, OverflowError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _expires_lifetime(expires: str, date: str | None) -> int:
    """Return the freshness lifetime an Expires field grants.

    RFC 9111 5.3: an Expires that is not an HTTP-date, ``0`` included, means
    already expired. Date is the point the expiry is measured from; a response
    that omits it is measured from its arrival.
    """
    expires_at = _http_date_seconds(expires)
    if expires_at is None:
        return 0

    generated_at = _http_date_seconds(date)
    if generated_at is None:
        generated_at = int(time.time())

    return min(max(expires_at - generated_at, 0), _SECONDS_CEILING)


def _states_freshness(response: HttpResponse) -> bool:
    """Whether ``response`` carries a freshness field of its own."""
    return (
        _header(response, "cache-control") is not None
        or _header(response, "expires") is not None
    )


def _freshness_lifetime(response: HttpResponse) -> int:
    """Return how long ``response`` may be served without revalidation.

    RFC 9111 4.2.1 for a private cache: an explicit ``max-age``, else Expires
    measured from Date, else the heuristic default. 4.2.2 reserves that default
    for a response stating no expiry at all.
    """
    max_age = _max_age_directive(_header(response, "cache-control"))
    if max_age is not None:
        return max_age

    expires = _header(response, "expires")
    if expires is None:
        return _DEFAULT_MAX_AGE
    return _expires_lifetime(expires, _header(response, "date"))


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
        min_fresh_seconds: int | None = None,
    ) -> None:
        """Create a cached client wrapping ``transport``.

        ``range_memo`` is the per-run range-capability memo shared across the
        indexes' clients; the coordinator owns the shared instance and injects
        it. A fresh memo is built when none is passed, so a stand-alone client
        still learns each host's range behaviour within its own lifetime.

        ``serialization`` pins which Simple-API serialization this index is
        asked for and read as.

        ``min_fresh_seconds`` is a read-time freshness floor for this index's
        Simple listing. A stale positive listing or negative sentinel within
        the floor is served without revalidation; it only extends freshness and
        rewrites nothing on disk.
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
        self._min_fresh_seconds = min_fresh_seconds

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
                if self._floor_keeps_fresh(policy, package, "listing"):
                    return self._parse_body(data, package)
                return await self._revalidate_simple(package, body, policy)
            corrupt_positive = True

        if not corrupt_positive:
            negative = self._cache.get_negative(package)
            if negative is not None and (
                negative.is_fresh()
                or self._offline
                or self._floor_keeps_fresh(negative, package, "absent-name sentinel")
            ):
                return []

        if self._offline:
            msg = f"No cached listing for {package} (offline mode)"
            raise OfflineError(msg)
        return await self._fetch_simple(package)

    def _floor_keeps_fresh(self, policy: CachePolicy, package: str, kind: str) -> bool:
        """Whether the read-time freshness floor still covers a stale entry.

        Consulted only after the stored policy has said stale and offline is
        false.
        """
        if self._min_fresh_seconds is None:
            return False
        age = int(time.time()) - policy.fetched_at
        if age >= self._min_fresh_seconds:
            return False
        logger.debug(
            "assume-fresh-seconds=%d: %s for %r from %s kept fresh at age %ds "
            "(server freshness %ds); skipping revalidation",
            self._min_fresh_seconds,
            kind,
            package,
            self._index_url,
            age,
            policy.max_age,
        )
        return True

    def _negative_policy(self, response: HttpResponse) -> CachePolicy:
        """Freshness policy for a name-level 404, clamped to the 600s cap."""
        max_age = min(_freshness_lifetime(response), _DEFAULT_MAX_AGE)
        return CachePolicy(
            fetched_at=_freshness_start(response), max_age=max_age, etag=None
        )

    def _decode_cached_listing(self, body: bytes, package: str) -> object | None:
        """Return the parsed JSON of a cached Simple body, or ``None``.

        A body that will not decode as JSON is logged and treated as a miss.
        A body that decodes but is the wrong shape is not caught here:
        :func:`_parse_files` raises on it, the same as on the wire path.
        """
        try:
            return json.loads(body)
        except ValueError:
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
        wrong shape, not a raw decode error. ``json.loads`` raises a
        :class:`ValueError` for every body it rejects, including non-UTF-8
        bytes and an integer literal past CPython's conversion limit.
        """
        try:
            data = json.loads(body)
        except ValueError as exc:
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
            # A 304 stating no freshness of its own keeps the stored max-age.
            new_policy = CachePolicy(
                fetched_at=_freshness_start(response),
                max_age=(
                    _freshness_lifetime(response)
                    if _states_freshness(response)
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
            fetched_at=_freshness_start(response),
            max_age=_freshness_lifetime(response),
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
            fetched_at=_freshness_start(response),
            max_age=_freshness_lifetime(response),
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
