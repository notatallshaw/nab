"""HTTP range-request reader for a remote wheel's core metadata.

Recovers a wheel's ``METADATA`` by ranged reads of the remote ZIP when the
index publishes no PEP 658 sidecar. The reader drives an
:class:`~nab_index.transport.AsyncHttpTransport`, learns each host's range
capability once per run through a shared :class:`RangeCapabilityMemo`, and
navigates the ZIP with the standard library's :class:`zipfile.ZipFile` over a
sparse buffer so no EOCD scan, deflate, or CRC logic is hand-rolled.

Two kinds of exception travel outward: a
:class:`~nab_index.client.MalformedSimpleResponseError` for METADATA bytes
that are not valid UTF-8, and the transport's error when the wheel URL itself
cannot be served (a 404 on the file, a failing plain GET).  A server that
merely refuses the Range mechanism is stepped down instead, from a suffix
range to absolute ranges to a plain full-body GET.  Every other failure to
obtain metadata returns a result whose ``text`` is ``None``.
"""

from __future__ import annotations

import asyncio
import bisect
import enum
import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .client import MalformedSimpleResponseError, WheelHashMismatchError
from .local_index import UnsupportedWheelError, wheel_metadata_member
from .transport import IDENTITY_HEADERS

if TYPE_CHECKING:
    from packaging.utils import NormalizedName

    from .transport import AsyncHttpTransport, HttpResponse

__all__ = [
    "RangeCapability",
    "RangeCapabilityMemo",
    "RangeMetadataResult",
    "RangeOutcome",
    "read_wheel_metadata_over_range",
]

# Initial suffix window. pip and poetry read about 10 KiB; the growth loop
# makes correctness independent of the exact value.
_DEFAULT_TAIL = 10240

_HTTP_OK = 200
_HTTP_PARTIAL = 206
_HTTP_RANGE_NOT_SATISFIABLE = 416
# Statuses a server uses to refuse the Range mechanism itself (416 for an
# unsatisfiable range, 501 for an unimplemented one, 400 from strict parsers,
# 403 from request filters).  None of them proves the file is unserveable, so
# a refusal steps down to the next request shape rather than failing the read.
_RANGE_REJECT_STATUSES = frozenset({400, 403, _HTTP_RANGE_NOT_SATISFIABLE, 501})

_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)")


class RangeOutcome(enum.Enum):
    """How rung 4 obtained (or failed to obtain) a wheel's METADATA."""

    PARTIAL = "partial"
    FULL_BODY = "full-body"
    UNSUPPORTED = "unsupported"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class RangeMetadataResult:
    """The recovered METADATA text and the outcome that produced it."""

    text: str | None
    outcome: RangeOutcome


class RangeCapability(enum.Enum):
    """The request shape to lead with on a netloc, monotonic toward more restrictive.

    ``SUFFIX_OK`` is optimistic: a host that volunteers a 200 full body for a
    suffix range stays ``SUFFIX_OK``, since leading with the suffix form again
    costs the same single request while a proxy that only ignores ranges while
    filling its cache gets to serve cheap partial reads again.
    ``FULL_BODY_ONLY`` is for hosts that, once the suffix form was already
    unusable, refused or ignored ranges or honoured one without reporting a
    complete-length; their wheels are fetched whole.  An answer that speaks
    for the one file never latches it: a 416 or a zero complete-length
    describes an empty or shrunk artifact, not the netloc.
    """

    UNKNOWN = "unknown"
    SUFFIX_OK = "suffix-ok"
    ABSOLUTE_ONLY = "absolute-only"
    FULL_BODY_ONLY = "full-body-only"
    UNSUPPORTED = "unsupported"


class RangeCapabilityMemo:
    """Per-netloc range capability, single-flight per host within one run.

    Touched only on the fetcher event loop's single thread, so its state
    needs no lock. Discovery of an ``UNKNOWN`` host is kept to one probe
    under a burst by an :class:`asyncio.Event` per netloc: the first task
    probes while later tasks await the event, then read the settled state.
    The owner releases the probe as soon as the capability settles, so
    waiters overlap with its remaining growth and member reads.  An owner
    whose acquisition fails settles nothing; its released waiters then probe
    for themselves.
    """

    def __init__(self) -> None:
        """Start with every netloc ``UNKNOWN`` and no probe in flight."""
        self._states: dict[str, RangeCapability] = {}
        self._inflight: dict[str, asyncio.Event] = {}

    def capability(self, netloc: str) -> RangeCapability:
        """Return the learned capability for ``netloc``."""
        return self._states.get(netloc, RangeCapability.UNKNOWN)

    def record(self, netloc: str, capability: RangeCapability) -> None:
        """Store the capability discovered for ``netloc``."""
        self._states[netloc] = capability

    async def await_probe(self, netloc: str) -> bool:
        """Coordinate single-flight discovery for ``netloc``.

        Returns ``True`` when the caller owns the probe and must discover
        the capability. Returns ``False`` when another task is already
        probing; the caller awaits it and then re-reads
        :meth:`capability`.
        """
        event = self._inflight.get(netloc)
        if event is not None:
            await event.wait()
            return False
        self._inflight[netloc] = asyncio.Event()
        return True

    def finish_probe(self, netloc: str) -> None:
        """Wake tasks waiting on ``netloc``'s probe and clear it."""
        self._inflight.pop(netloc).set()


class _SparseFile:
    """A read-only file-like over sparse byte spans of a remote file.

    Implements the subset :class:`zipfile.ZipFile` drives: ``read``,
    ``seek``, ``tell``, ``seekable``. Populated spans are kept sorted and
    non-overlapping; a read across an unpopulated gap returns only the bytes
    up to the gap (zero bytes when the start is unpopulated), which makes
    ZipFile raise :class:`zipfile.BadZipFile`, the signal to fetch more or
    give up.
    """

    def __init__(self, length: int) -> None:
        """Create a file of ``length`` bytes with no spans populated."""
        self._length = length
        self._pos = 0
        self._starts: list[int] = []
        self._spans: list[bytes] = []

    def add_span(self, start: int, data: bytes) -> None:
        """Populate ``[start, start + len(data))`` with ``data``."""
        if not data:
            return
        index = bisect.bisect_left(self._starts, start)
        if index < len(self._starts) and self._starts[index] == start:
            return
        self._starts.insert(index, start)
        self._spans.insert(index, data)

    def populated(self, start: int, end: int) -> bool:
        """Return whether ``[start, end)`` is fully populated."""
        saved = self._pos
        self._pos = start
        data = self.read(end - start)
        self._pos = saved
        return len(data) == end - start

    def seekable(self) -> bool:
        """Report the file as seekable."""
        return True

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """Move the read position and return it."""
        if whence == os.SEEK_SET:
            self._pos = offset
        elif whence == os.SEEK_CUR:
            self._pos += offset
        elif whence == os.SEEK_END:
            self._pos = self._length + offset
        else:
            msg = f"unsupported whence: {whence}"
            raise ValueError(msg)
        return self._pos

    def tell(self) -> int:
        """Return the current read position."""
        return self._pos

    def read(self, size: int | None = -1) -> bytes:
        """Return up to ``size`` bytes from the current position.

        ``None`` or a negative ``size`` reads to the end, as on a file object.
        Reads across contiguous spans and stops at the first gap, so a
        short return is the caller's cue that more bytes are needed.
        """
        remaining = self._length - self._pos if size is None or size < 0 else size
        result = bytearray()
        pos = self._pos
        while remaining > 0:
            index = bisect.bisect_right(self._starts, pos) - 1
            if index < 0:
                break
            start = self._starts[index]
            data = self._spans[index]
            if pos >= start + len(data):
                break
            offset = pos - start
            take = min(len(data) - offset, remaining)
            result += data[offset : offset + take]
            pos += take
            remaining -= take
        self._pos = pos
        return bytes(result)


@dataclass(frozen=True, slots=True)
class _SuffixOutcome:
    """The result of one suffix-range attempt."""

    kind: str  # "suffix", "full", or "downgrade"
    total: int = 0
    low: int = 0
    body: bytes = b""


class _AcqKind(enum.Enum):
    """Which shape of bytes an acquisition handed back."""

    SPARSE = "sparse"
    FULL_BODY = "full-body"
    NONE = "none"


# The bytes an acquisition carries, read according to its _AcqKind: a whole
# body, a tail window plus the offset it starts at, or nothing.
_AcqPayload = bytes | tuple[_SparseFile, int] | None

# A netloc that cannot serve usable ranges and volunteered no full body.
_UNSUPPORTED_NONE: tuple[RangeCapability, _AcqKind, _AcqPayload] = (
    RangeCapability.UNSUPPORTED,
    _AcqKind.NONE,
    None,
)


class _RangeRefusedError(Exception):
    """A growth or member range was refused after the tail had been honoured."""

    def __init__(self, status: int) -> None:
        super().__init__(f"range refused with HTTP {status}")
        self.status = status


class _UnreadableWheelError(Exception):
    """The zip machinery cannot read the fetched bytes as an archive at all."""


def _non_identity(response: HttpResponse) -> bool:
    """Return whether the response carries a non-identity Content-Encoding."""
    encoding = response.headers.get("content-encoding")
    return encoding is not None and encoding.lower() != "identity"


def _parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    """Parse ``bytes start-end/total`` into a tuple, or ``None``."""
    if value is None:
        return None
    match = _CONTENT_RANGE_RE.match(value.strip())
    if match is None:
        return None

    try:
        return (int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        # \d+ still matches a run past CPython's int-from-string limit.
        return None


async def _range_get(
    transport: AsyncHttpTransport, url: str, range_header: str
) -> HttpResponse:
    """GET ``url`` for ``range_header`` bytes, forcing identity encoding."""
    return await transport.get(url, headers={**IDENTITY_HEADERS, "Range": range_header})


async def _suffix_attempt(
    transport: AsyncHttpTransport, url: str, tail_size: int
) -> _SuffixOutcome:
    """Try a suffix range, classifying the server's answer.

    A 206 whose Content-Range is absent, unparseable, or of unknown length
    (``bytes a-b/*``) gives no offset to anchor the returned bytes, so it
    steps down to absolute ranges rather than guessing what the body is.
    """
    response = await _range_get(transport, url, f"bytes=-{tail_size}")
    status = response.status_code
    if status == _HTTP_OK and not _non_identity(response):
        return _SuffixOutcome("full", body=response.content)
    if status == _HTTP_PARTIAL and not _non_identity(response):
        body = response.content
        parsed = _parse_content_range(response.headers.get("content-range"))
        if parsed is None:
            return _SuffixOutcome("downgrade")
        start, end, total = parsed
        if end == total - 1 and end - start + 1 == len(body):
            return _SuffixOutcome("suffix", total=total, low=start, body=body)
        return _SuffixOutcome("downgrade")
    if status not in _RANGE_REJECT_STATUSES and status not in (
        _HTTP_OK,
        _HTTP_PARTIAL,
    ):
        response.raise_for_status()
    return _SuffixOutcome("downgrade")


async def _absolute_attempt(
    transport: AsyncHttpTransport, url: str, tail_size: int
) -> tuple[RangeCapability, _AcqKind, _AcqPayload]:
    """Learn the length with ``bytes=0-0``, then read the tail absolutely.

    A probe that is refused, or that reports no usable total, steps down to
    the plain GET: neither answer says the file is unserveable, so only the
    plain GET is authoritative for whether the wheel can be served at all.
    Any other 4xx/5xx raises through, whatever its Content-Encoding.
    """
    probe = await _range_get(transport, url, "bytes=0-0")
    if probe.status_code in _RANGE_REJECT_STATUSES:
        return await _fallback_full_body(
            transport, url, latch=probe.status_code != _HTTP_RANGE_NOT_SATISFIABLE
        )
    if probe.status_code == _HTTP_OK and not _non_identity(probe):
        return (RangeCapability.FULL_BODY_ONLY, _AcqKind.FULL_BODY, probe.content)
    if probe.status_code != _HTTP_PARTIAL or _non_identity(probe):
        probe.raise_for_status()
        return _UNSUPPORTED_NONE
    parsed = _parse_content_range(probe.headers.get("content-range"))
    if parsed is None or parsed[2] == 0:
        # No total to range against.  An absent, unparseable, or "*" length
        # (RFC 9110 section 14.4) leaves ranges unusable on this host; a zero
        # speaks for this artifact, as a 416 does.
        return await _fallback_full_body(transport, url, latch=parsed is None)
    return await _absolute_tail(transport, url, parsed[2], tail_size)


async def _absolute_tail(
    transport: AsyncHttpTransport, url: str, total: int, tail_size: int
) -> tuple[RangeCapability, _AcqKind, _AcqPayload]:
    """Read the tail window with an absolute range once the length is known.

    A tail refused after an honoured probe still steps down to the plain GET,
    since a shrunk file or a flaky proxy can 416 a range the probe implied.
    Any other 4xx/5xx raises through, whatever its Content-Encoding.
    """
    low = max(0, total - tail_size)
    tail = await _range_get(transport, url, f"bytes={low}-{total - 1}")
    if tail.status_code in _RANGE_REJECT_STATUSES:
        return await _fallback_full_body(
            transport, url, latch=tail.status_code != _HTTP_RANGE_NOT_SATISFIABLE
        )
    if tail.status_code == _HTTP_OK and not _non_identity(tail):
        return (RangeCapability.FULL_BODY_ONLY, _AcqKind.FULL_BODY, tail.content)
    if tail.status_code != _HTTP_PARTIAL or _non_identity(tail):
        tail.raise_for_status()
        return _UNSUPPORTED_NONE
    sparse = _SparseFile(total)
    sparse.add_span(low, tail.content)
    return (RangeCapability.ABSOLUTE_ONLY, _AcqKind.SPARSE, (sparse, low))


async def _full_body_fetch(transport: AsyncHttpTransport, url: str) -> bytes | None:
    """Fetch the whole wheel with a plain GET, leaving the Range mechanism out.

    Reached for a netloc memoed ``FULL_BODY_ONLY`` and as the last step down
    after a refused range: the file may still be served without the Range
    header, and this GET is the authoritative check.  A 4xx/5xx here raises
    through and fails the resolve, since the listing advertised a file the
    index cannot serve; a non-identity encoding returns ``None``, no usable
    bytes.
    """
    response = await transport.get(url, headers=IDENTITY_HEADERS)
    if response.status_code == _HTTP_OK and not _non_identity(response):
        return response.content
    response.raise_for_status()
    return None


async def _fallback_full_body(
    transport: AsyncHttpTransport, url: str, *, latch: bool
) -> tuple[RangeCapability, _AcqKind, _AcqPayload]:
    """Step an unusable range down to the plain GET, choosing what it teaches.

    ``latch`` records the netloc ``FULL_BODY_ONLY``, so later wheels skip the
    wasted probes; it is for an answer that leaves the Range mechanism
    unusable on this host.  An answer that speaks for the one file (a 416, or
    a zero complete-length) teaches the memo nothing, and later wheels probe
    ranges again.
    """
    body = await _full_body_fetch(transport, url)
    if body is None:
        return _UNSUPPORTED_NONE
    if latch:
        return (RangeCapability.FULL_BODY_ONLY, _AcqKind.FULL_BODY, body)
    return (RangeCapability.UNKNOWN, _AcqKind.FULL_BODY, body)


async def _acquire(
    transport: AsyncHttpTransport,
    url: str,
    capability: RangeCapability,
    tail_size: int,
) -> tuple[RangeCapability, _AcqKind, _AcqPayload]:
    """Fetch bytes per the netloc's known capability, learning it if unknown."""
    if capability is RangeCapability.UNSUPPORTED:
        return _UNSUPPORTED_NONE
    if capability is RangeCapability.FULL_BODY_ONLY:
        body = await _full_body_fetch(transport, url)
        if body is None:
            return _UNSUPPORTED_NONE
        return (RangeCapability.FULL_BODY_ONLY, _AcqKind.FULL_BODY, body)
    if capability in (RangeCapability.UNKNOWN, RangeCapability.SUFFIX_OK):
        suffix = await _suffix_attempt(transport, url, tail_size)
        if suffix.kind == "suffix":
            sparse = _SparseFile(suffix.total)
            sparse.add_span(suffix.low, suffix.body)
            return (RangeCapability.SUFFIX_OK, _AcqKind.SPARSE, (sparse, suffix.low))
        if suffix.kind == "full":
            # Not latched to FULL_BODY_ONLY: see RangeCapability.
            return (RangeCapability.SUFFIX_OK, _AcqKind.FULL_BODY, suffix.body)
    return await _absolute_attempt(transport, url, tail_size)


def _absorb_range(
    response: HttpResponse,
    sparse: _SparseFile,
    partial_low: int,
    wheel_hash: tuple[str, str] | None,
) -> int | None:
    """Absorb a growth or member range response into ``sparse``.

    Returns the low offset at which bytes were populated, or ``None`` when the
    response yielded no usable bytes.  A volunteered 200 full body is checked
    against ``wheel_hash`` (raising :class:`WheelHashMismatchError` on a
    mismatch) and then populates the whole file from offset 0, used rather than
    discarded.  A range refused mid-read raises :class:`_RangeRefusedError` so
    the reader can step down to the plain GET.  Any other 4xx/5xx on the wheel
    URL raises through and fails the resolve, matching the first round trip; a
    non-identity encoding yields no usable bytes.
    """
    if response.status_code == _HTTP_OK and not _non_identity(response):
        _verify_full_body(response.content, wheel_hash)
        sparse.add_span(0, response.content)
        return 0
    if response.status_code in _RANGE_REJECT_STATUSES:
        raise _RangeRefusedError(response.status_code)
    if response.status_code != _HTTP_PARTIAL or _non_identity(response):
        response.raise_for_status()
        return None
    sparse.add_span(partial_low, response.content)
    return partial_low


def _try_open(sparse: _SparseFile) -> zipfile.ZipFile | None:
    """Open a ZipFile over the current spans, or ``None`` if more bytes are needed.

    A window with a gap always reads as a truncated directory, which is
    :class:`zipfile.BadZipFile`, the grow signal.  Anything else the zip
    machinery raises comes from the archive's own bytes (an undecodable
    member name, offsets outside the file), so it marks the wheel unreadable
    rather than the window short.
    """
    try:
        return zipfile.ZipFile(sparse)
    except zipfile.BadZipFile:
        return None
    except Exception as exc:
        raise _UnreadableWheelError from exc


async def _open_zip(
    transport: AsyncHttpTransport,
    url: str,
    sparse: _SparseFile,
    tail_low: int,
    wheel_hash: tuple[str, str] | None,
) -> zipfile.ZipFile | None:
    """Open a ZipFile over the sparse buffer, growing the window on demand."""
    total = sparse._length  # noqa: SLF001
    while True:
        opened = _try_open(sparse)
        if opened is not None:
            return opened
        have = total - tail_low
        if have >= total:
            return None
        new_size = min(have * 2, total)
        new_low = total - new_size
        response = await _range_get(transport, url, f"bytes={new_low}-{tail_low - 1}")
        low = _absorb_range(response, sparse, new_low, wheel_hash)
        if low is None:
            return None
        tail_low = low


def _member_span(zip_file: zipfile.ZipFile, member: str) -> tuple[int, int]:
    """Return the ``[header_offset, next)`` byte span of ``member``.

    ``next`` is the smallest other entry offset greater than this one, or
    the central-directory start when this entry is the last by offset. This
    avoids parsing the local file header, whose extra-field length can
    differ from the central directory's.
    """
    start = zip_file.getinfo(member).header_offset
    laters = [
        info.header_offset for info in zip_file.infolist() if info.header_offset > start
    ]
    end = min(laters) if laters else zip_file.start_dir
    return (start, end)


async def _read_sparse(
    transport: AsyncHttpTransport,
    url: str,
    sparse: _SparseFile,
    tail_low: int,
    canonical_name: NormalizedName,
    wheel_hash: tuple[str, str] | None,
) -> bytes | None:
    """Read the METADATA member out of the sparse buffer, or ``None``.

    Only :class:`_RangeRefusedError`, :class:`WheelHashMismatchError`, and the
    transport's errors travel out of the growth and member requests; every way
    the zip machinery can choke on the fetched bytes reads back as ``None``.
    """
    try:
        zip_file = await _open_zip(transport, url, sparse, tail_low, wheel_hash)
        member = (
            None
            if zip_file is None
            else wheel_metadata_member(zip_file.namelist(), canonical_name)
        )
    except (_UnreadableWheelError, UnsupportedWheelError):
        return None
    if zip_file is None or member is None:
        return None
    start, end = _member_span(zip_file, member)
    if not sparse.populated(start, end):
        response = await _range_get(transport, url, f"bytes={start}-{end - 1}")
        if _absorb_range(response, sparse, start, wheel_hash) is None:
            return None
    try:
        return zip_file.read(member)
    except Exception:  # noqa: BLE001 - untrusted archive bytes
        # Encrypted members, unknown compression methods, and corrupt streams
        # surface as several exception types; all mean the member is
        # unrecoverable from this artifact, never that the resolver is broken.
        return None


def _verify_full_body(body: bytes, wheel_hash: tuple[str, str] | None) -> None:
    """Raise :class:`WheelHashMismatchError` if a full body fails its published hash.

    ``wheel_hash`` is ``None`` when the listing published no accepted digest, so
    no check runs. Only a full body can be verified this way; a partial read
    holds just a slice of the wheel.
    """
    if wheel_hash is None:
        return

    algo, expected = wheel_hash
    actual = hashlib.new(algo, body).hexdigest()
    if actual != expected:
        msg = f"wheel {algo} mismatch: expected {expected}, got {actual}"
        raise WheelHashMismatchError(msg)


def _read_full_body(body: bytes, canonical_name: NormalizedName) -> bytes | None:
    """Read the METADATA member out of a complete in-memory wheel, or ``None``."""
    try:
        zip_file = zipfile.ZipFile(io.BytesIO(body))
        member = wheel_metadata_member(zip_file.namelist(), canonical_name)
        if member is None:
            return None
        return zip_file.read(member)
    except Exception:  # noqa: BLE001 - untrusted archive bytes
        # The zip machinery raises several exception types over bytes that are
        # not a readable wheel (including the resolver's own
        # UnsupportedWheelError); all of them mean this artifact has no
        # recoverable METADATA.
        return None


def _decode(raw: bytes) -> str:
    """Decode METADATA bytes as UTF-8, raising on invalid input."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"range-read METADATA is not valid UTF-8: {exc}"
        raise MalformedSimpleResponseError(msg) from exc


async def read_wheel_metadata_over_range(
    transport: AsyncHttpTransport,
    wheel_url: str,
    canonical_name: NormalizedName,
    memo: RangeCapabilityMemo,
    *,
    wheel_hash: tuple[str, str] | None = None,
    tail_size: int = _DEFAULT_TAIL,
) -> RangeMetadataResult:
    """Recover a remote wheel's METADATA text by ranged reads of its ZIP.

    ``wheel_hash`` is the wheel's published ``(algorithm, hex_digest)`` from the
    Simple-API listing, or ``None`` when none was published.  Whenever the whole
    wheel comes back as one body, whether from a host that ignores or refuses
    ranges or from a 200 volunteered while a partial read is growing its window,
    the bytes are checked against ``wheel_hash`` before their METADATA is read,
    so bytes that disagree with the published digest never drive the resolve.  A
    ranged read is left unverified, even one whose window grew to cover the
    whole file.

    Raises :class:`~nab_index.client.WheelHashMismatchError` when a full-body
    wheel fails ``wheel_hash``, and
    :class:`~nab_index.client.MalformedSimpleResponseError` when the recovered
    METADATA is not valid UTF-8.  Re-raises a transport error when the wheel URL
    itself cannot be served (a 404, a failing plain GET).  A refused Range
    mechanism is stepped down, not raised.  Every other failure to obtain
    metadata returns a result with ``text=None``.
    """
    netloc = urlsplit(wheel_url).netloc
    owns_probe = False
    if memo.capability(netloc) is RangeCapability.UNKNOWN:
        owns_probe = await memo.await_probe(netloc)
    try:
        capability = memo.capability(netloc)
        new_capability, kind, payload = await _acquire(
            transport, wheel_url, capability, tail_size
        )
        if new_capability is not RangeCapability.UNKNOWN:
            # A fallback that learned nothing about the netloc returns UNKNOWN
            # and must not erase a learned capability.
            memo.record(netloc, new_capability)
    finally:
        # Settled (or failed): waiters re-read the memo, so the growth and
        # member reads below need not keep them parked.
        if owns_probe:
            memo.finish_probe(netloc)
    if kind is _AcqKind.NONE:
        return RangeMetadataResult(None, RangeOutcome.UNSUPPORTED)
    if kind is _AcqKind.FULL_BODY:
        assert isinstance(payload, bytes)
        _verify_full_body(payload, wheel_hash)
        raw = _read_full_body(payload, canonical_name)
        outcome = RangeOutcome.FULL_BODY
    else:
        assert isinstance(payload, tuple)
        sparse, tail_low = payload
        try:
            raw = await _read_sparse(
                transport, wheel_url, sparse, tail_low, canonical_name, wheel_hash
            )
            outcome = RangeOutcome.PARTIAL
        except _RangeRefusedError as refusal:
            # A growth or member range refused after an honoured tail: step
            # down to the plain GET like the acquisition path, teaching the
            # memo only when the refusal was aimed at the mechanism.
            body = await _full_body_fetch(transport, wheel_url)
            if body is None:
                return RangeMetadataResult(None, RangeOutcome.MISSING)
            _verify_full_body(body, wheel_hash)
            if refusal.status != _HTTP_RANGE_NOT_SATISFIABLE:
                memo.record(netloc, RangeCapability.FULL_BODY_ONLY)
            raw = _read_full_body(body, canonical_name)
            outcome = RangeOutcome.FULL_BODY
    if raw is None:
        return RangeMetadataResult(None, RangeOutcome.MISSING)
    return RangeMetadataResult(_decode(raw), outcome)
