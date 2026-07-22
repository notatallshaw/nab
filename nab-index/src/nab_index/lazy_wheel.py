"""HTTP range-request reader for a remote wheel's core metadata.

Recovers a wheel's ``METADATA`` by ranged reads of the remote ZIP when the
index publishes no PEP 658 sidecar. The reader drives an
:class:`~nab_index.transport.AsyncHttpTransport`, learns each host's range
capability once per run through a shared :class:`RangeCapabilityMemo`, and
navigates the ZIP with the standard library's :class:`zipfile.ZipFile` over a
sparse buffer so no EOCD scan, deflate, or CRC logic is hand-rolled.

The one exception raised outward is
:class:`~nab_index.client.MalformedSimpleResponseError`, and only for METADATA
bytes that are not valid UTF-8. Every other failure to obtain metadata returns
a result whose ``text`` is ``None``.
"""

from __future__ import annotations

import asyncio
import bisect
import enum
import io
import os
import re
import zipfile
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .client import MalformedSimpleResponseError
from .local_index import UnsupportedWheelError, wheel_metadata_member

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
# Hard ceiling on the window the growth loop will fetch before giving up.
_MAX_TAIL = 1024 * 1024

_HTTP_OK = 200
_HTTP_PARTIAL = 206
# The suffix form is unusable on these; downgrade to absolute ranges.
_SUFFIX_REJECT_STATUSES = frozenset({400, 416, 501})

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
    """A netloc's learned range behaviour, monotonic toward more restrictive."""

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

    def read(self, size: int = -1) -> bytes:
        """Return up to ``size`` bytes from the current position.

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


# A netloc that cannot serve usable ranges and volunteered no full body.
_UNSUPPORTED_NONE: tuple[RangeCapability, _AcqKind, object] = (
    RangeCapability.UNSUPPORTED,
    _AcqKind.NONE,
    None,
)


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
    return (int(match[1]), int(match[2]), int(match[3]))


async def _range_get(
    transport: AsyncHttpTransport, url: str, range_header: str
) -> HttpResponse:
    """GET ``url`` for ``range_header`` bytes, forcing identity encoding."""
    return await transport.get(
        url, headers={"Accept-Encoding": "identity", "Range": range_header}
    )


async def _suffix_attempt(
    transport: AsyncHttpTransport, url: str, tail_size: int
) -> _SuffixOutcome:
    """Try a suffix range, classifying the server's answer."""
    response = await _range_get(transport, url, f"bytes=-{tail_size}")
    status = response.status_code
    if status == _HTTP_OK and not _non_identity(response):
        return _SuffixOutcome("full", body=response.content)
    if status == _HTTP_PARTIAL and not _non_identity(response):
        body = response.content
        parsed = _parse_content_range(response.headers.get("content-range"))
        if parsed is None:
            return _SuffixOutcome("full", body=body)
        start, end, total = parsed
        if end == total - 1 and end - start + 1 == len(body):
            return _SuffixOutcome("suffix", total=total, low=start, body=body)
        return _SuffixOutcome("downgrade")
    if status not in _SUFFIX_REJECT_STATUSES and status not in (
        _HTTP_OK,
        _HTTP_PARTIAL,
    ):
        response.raise_for_status()
    return _SuffixOutcome("downgrade")


async def _absolute_attempt(
    transport: AsyncHttpTransport, url: str, tail_size: int
) -> tuple[RangeCapability, _AcqKind, object]:
    """Learn the length with ``bytes=0-0``, then read the tail absolutely."""
    probe = await _range_get(transport, url, "bytes=0-0")
    if _non_identity(probe):
        return _UNSUPPORTED_NONE
    if probe.status_code == _HTTP_OK:
        return (RangeCapability.FULL_BODY_ONLY, _AcqKind.FULL_BODY, probe.content)
    if probe.status_code != _HTTP_PARTIAL:
        probe.raise_for_status()
        return _UNSUPPORTED_NONE  # pragma: no cover
    parsed = _parse_content_range(probe.headers.get("content-range"))
    if parsed is None:
        return _UNSUPPORTED_NONE
    return await _absolute_tail(transport, url, parsed[2], tail_size)


async def _absolute_tail(
    transport: AsyncHttpTransport, url: str, total: int, tail_size: int
) -> tuple[RangeCapability, _AcqKind, object]:
    """Read the tail window with an absolute range once the length is known."""
    low = max(0, total - tail_size)
    tail = await _range_get(transport, url, f"bytes={low}-{total - 1}")
    if _non_identity(tail):
        return _UNSUPPORTED_NONE
    if tail.status_code == _HTTP_OK:
        return (RangeCapability.FULL_BODY_ONLY, _AcqKind.FULL_BODY, tail.content)
    if tail.status_code != _HTTP_PARTIAL:
        tail.raise_for_status()
        return _UNSUPPORTED_NONE  # pragma: no cover
    sparse = _SparseFile(total)
    sparse.add_span(low, tail.content)
    return (RangeCapability.ABSOLUTE_ONLY, _AcqKind.SPARSE, (sparse, low))


async def _full_body_fetch(
    transport: AsyncHttpTransport, url: str
) -> tuple[RangeCapability, _AcqKind, object]:
    """Fetch the whole wheel from a host known to ignore ranges.

    A range-ignoring host answered an earlier probe with the full body, so it
    is memoed ``FULL_BODY_ONLY``.  Later wheels on that host skip the wasted
    range probe and fetch the body directly with a plain GET, so every
    sidecar-less wheel there recovers its METADATA rather than only the first.
    A 4xx/5xx (the file went missing) raises through and drops the candidate; a
    non-identity encoding yields no usable bytes.
    """
    response = await transport.get(url, headers={"Accept-Encoding": "identity"})
    if response.status_code == _HTTP_OK and not _non_identity(response):
        return (RangeCapability.FULL_BODY_ONLY, _AcqKind.FULL_BODY, response.content)
    response.raise_for_status()
    return _UNSUPPORTED_NONE


async def _acquire(
    transport: AsyncHttpTransport,
    url: str,
    capability: RangeCapability,
    tail_size: int,
) -> tuple[RangeCapability, _AcqKind, object]:
    """Fetch bytes per the netloc's known capability, learning it if unknown."""
    if capability is RangeCapability.UNSUPPORTED:
        return _UNSUPPORTED_NONE
    if capability is RangeCapability.FULL_BODY_ONLY:
        return await _full_body_fetch(transport, url)
    if capability in (RangeCapability.UNKNOWN, RangeCapability.SUFFIX_OK):
        suffix = await _suffix_attempt(transport, url, tail_size)
        if suffix.kind == "suffix":
            sparse = _SparseFile(suffix.total)
            sparse.add_span(suffix.low, suffix.body)
            return (RangeCapability.SUFFIX_OK, _AcqKind.SPARSE, (sparse, suffix.low))
        if suffix.kind == "full":
            return (RangeCapability.FULL_BODY_ONLY, _AcqKind.FULL_BODY, suffix.body)
    return await _absolute_attempt(transport, url, tail_size)


def _absorb_range(
    response: HttpResponse, sparse: _SparseFile, partial_low: int
) -> int | None:
    """Absorb a growth or member range response into ``sparse``.

    Returns the low offset at which bytes were populated, or ``None`` when the
    response yielded no usable bytes.  A volunteered 200 full body populates the
    whole file from offset 0 and is used rather than discarded.  A 4xx/5xx on
    the wheel URL raises through so the candidate drops, matching the first
    round trip; a non-identity encoding yields no usable bytes.
    """
    if response.status_code == _HTTP_OK and not _non_identity(response):
        sparse.add_span(0, response.content)
        return 0
    if response.status_code != _HTTP_PARTIAL or _non_identity(response):
        response.raise_for_status()
        return None
    sparse.add_span(partial_low, response.content)
    return partial_low


def _try_open(sparse: _SparseFile) -> zipfile.ZipFile | None:
    """Open a ZipFile over the current spans, or ``None`` if more bytes are needed."""
    try:
        return zipfile.ZipFile(sparse)
    except zipfile.BadZipFile:
        return None


async def _open_zip(
    transport: AsyncHttpTransport, url: str, sparse: _SparseFile, tail_low: int
) -> zipfile.ZipFile | None:
    """Open a ZipFile over the sparse buffer, growing the window on demand."""
    total = sparse._length  # noqa: SLF001
    while True:
        opened = _try_open(sparse)
        if opened is not None:
            return opened
        have = total - tail_low
        if have >= total or have >= _MAX_TAIL:
            return None
        new_size = min(have * 2, total, _MAX_TAIL)
        new_low = total - new_size
        response = await _range_get(transport, url, f"bytes={new_low}-{tail_low - 1}")
        low = _absorb_range(response, sparse, new_low)
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
) -> bytes | None:
    """Read the METADATA member out of the sparse buffer, or ``None``."""
    zip_file = await _open_zip(transport, url, sparse, tail_low)
    if zip_file is None:
        return None
    try:
        member = wheel_metadata_member(zip_file.namelist(), canonical_name)
    except UnsupportedWheelError:
        return None
    if member is None:
        return None
    start, end = _member_span(zip_file, member)
    if not sparse.populated(start, end):
        response = await _range_get(transport, url, f"bytes={start}-{end - 1}")
        if _absorb_range(response, sparse, start) is None:
            return None
    try:
        return zip_file.read(member)
    except (zipfile.BadZipFile, zlib.error, OSError):
        return None


def _read_full_body(body: bytes, canonical_name: NormalizedName) -> bytes | None:
    """Read the METADATA member out of a complete in-memory wheel, or ``None``."""
    try:
        zip_file = zipfile.ZipFile(io.BytesIO(body))
        member = wheel_metadata_member(zip_file.namelist(), canonical_name)
        if member is None:
            return None
        return zip_file.read(member)
    except (zipfile.BadZipFile, zlib.error, OSError, UnsupportedWheelError):
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
    tail_size: int = _DEFAULT_TAIL,
) -> RangeMetadataResult:
    """Recover a remote wheel's METADATA text by ranged reads of its ZIP.

    Raises :class:`~nab_index.client.MalformedSimpleResponseError` only when
    the recovered METADATA is not valid UTF-8, and re-raises a transport
    error for a 4xx/5xx on the wheel URL. Every other failure to obtain
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
        memo.record(netloc, new_capability)
        if kind is _AcqKind.NONE:
            return RangeMetadataResult(None, RangeOutcome.UNSUPPORTED)
        if kind is _AcqKind.FULL_BODY:
            assert isinstance(payload, bytes)
            raw = _read_full_body(payload, canonical_name)
            outcome = RangeOutcome.FULL_BODY
        else:
            assert isinstance(payload, tuple)
            sparse, tail_low = payload
            raw = await _read_sparse(
                transport, wheel_url, sparse, tail_low, canonical_name
            )
            outcome = RangeOutcome.PARTIAL
        if raw is None:
            return RangeMetadataResult(None, RangeOutcome.MISSING)
        return RangeMetadataResult(_decode(raw), outcome)
    finally:
        if owns_probe:
            memo.finish_probe(netloc)
