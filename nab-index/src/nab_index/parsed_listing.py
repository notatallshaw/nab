"""Codec for the parsed-listing cache: records <-> opaque cache blob.

Stores the post-:func:`nab_index.client._parse_files` records so a warm
resolve skips ``json.loads`` and wheel/sdist filename parsing. This module
owns the record<->bytes translation; :class:`~nab_index.cache.OnDiskCache`
treats the blob as opaque and knows nothing about record shapes.

Wire form (UTF-8 JSON, one document per line):

* the first line is the header
  ``[format, codec, key_scheme, body_digest, rows]``, checked before anything
  is trusted. A reader on a different ``format``, ``codec``, or ``key_scheme``
  treats the entry as a miss and rebuilds, so a cache written by an older build
  self-heals instead of misdecoding. ``body_digest`` binds the blob to the raw
  body it was parsed from; :func:`decode` rejects a blob whose digest does not
  equal the policy's, so a raw-body update invalidates the derived form.
  ``rows`` is how many rows were written, and a reader that decodes a different
  number rejects the blob, so bytes lost at a line boundary are a miss rather
  than a silently short listing.
* every later line is a batch of up to ``_ROWS_PER_LINE`` rows, one entry per
  surviving record, in the order the wire parse returned them, so a downstream
  stable-sort tie-break stays identical. Each row is a flat list tagged wheel
  or sdist by its first element. Every field is type-checked on the way back,
  and ``requires_python`` and hash-algorithm names are re-interned via
  ``sys.intern`` so the round trip reproduces the dedup the wire parse builds.
* batching lets :func:`decode` read straight from the cache file, one line at
  a time, rather than holding the whole document and the copy ``json.loads``
  makes of it.
* the two integrity cells carry the index's own table, as a JSON object, when
  the record was built from one, and the parsed pairs otherwise. A rehydrated
  record defers the same way, so a listing pays the integrity parse only for
  the files a resolve reads.

The blob is portable: one entry serves every interpreter that shares the cache,
and a body this module will not parse is a miss, never an exception reaching
the caller.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from nab_provider.records import (
    SdistFile,
    WheelFile,
    rehydrated_sdist,
    rehydrated_wheel,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from .cache import CachePolicy

__all__ = ["corruption_reason", "decode", "encode"]

# Record version, redundant with the bucket suffix but guards against a stale
# blob surfacing under the current bucket. Bump it when the row shape changes
# or when the same body parses to different records: ``body_digest`` pins only
# the input.
FORMAT_VERSION = 3
# Serialization variant that wrote the rows, so a future codec switch
# self-heals rather than misdecodes.
CODEC = 2
# Version sort-key scheme the rows carry; 0 == no precomputed keys. A later
# scheme bumps this and self-heals via a reparse.
KEY_SCHEME = 0

_HEADER_LEN = 5
# Rows per wire line. A smaller batch holds fewer decoded rows at once and costs
# more ``json.loads`` calls.
_ROWS_PER_LINE = 1024
_PAIR_LEN = 2
_TAG_WHEEL = 0
_TAG_SDIST = 1


class _BadRowError(ValueError):
    """A row whose tag, arity, or field types are not what this codec wrote."""


def _hashes_cell(record: WheelFile | SdistFile) -> object:
    """Return the row cell for ``hashes``: the raw table, or the parsed pairs.

    A record built from the index's own table writes that table, so encoding a
    fresh listing does not force the parse it deferred.
    """
    raw = record.raw_hashes()
    return record.hashes if raw is None else raw


def _sidecar_cell(wheel: WheelFile) -> object:
    """Return the row cell for ``metadata_hash``, raw table or parsed pair."""
    raw = wheel.raw_sidecar()
    return wheel.metadata_hash if raw is None else raw


def encode(files: list[WheelFile | SdistFile], body_digest: str) -> bytes:
    """Encode parsed listing ``files`` into a cache blob bound to ``body_digest``.

    ``body_digest`` is the sha256 hex of the raw body the records were parsed
    from; :func:`decode` rehydrates only when it matches the policy's digest.
    ``local_path`` is not stored (a parsed-cache entry always comes from a
    remote body) and ``metadata_url`` is a derived property, so neither rides
    the wire.
    """
    header = [FORMAT_VERSION, CODEC, KEY_SCHEME, body_digest, len(files)]
    blob = bytearray(_line(header))
    batch: list[list[object]] = []
    for record in files:
        if isinstance(record, WheelFile):
            row: list[object] = [
                _TAG_WHEEL,
                record.filename,
                record.url,
                record.version,
                record.requires_python,
                record.has_metadata,
                record.upload_time,
                _hashes_cell(record),
                record.size,
                _sidecar_cell(record),
            ]
        else:
            row = [
                _TAG_SDIST,
                record.filename,
                record.url,
                record.version,
                record.requires_python,
                record.upload_time,
                _hashes_cell(record),
                record.size,
            ]

        batch.append(row)
        if len(batch) == _ROWS_PER_LINE:
            blob += _line(batch)
            batch.clear()

    if batch:
        blob += _line(batch)
    return bytes(blob)


def _line(document: object) -> bytes:
    """Serialize one wire document as its own line.

    ``json.dumps`` escapes control characters, so a serialized document carries
    no newline of its own for the line split to trip over. Non-ASCII is escaped
    as well, because a field kept verbatim from the listing, such as
    ``requires_python``, can hold a lone surrogate with no UTF-8 form.
    """
    return json.dumps(document, separators=(",", ":")).encode() + b"\n"


def decode(
    lines: Iterable[bytes], policy: CachePolicy
) -> list[WheelFile | SdistFile] | None:
    """Decode a cache blob's ``lines`` to parsed records, or ``None`` for a miss.

    ``lines`` is the blob read line by line, so an open cache file can be
    passed straight in and the document is never held whole.

    Returns ``None`` (treat as a cache miss and rebuild from the raw body) when
    the blob does not decode, cannot be read, is the wrong shape, holds a
    different number of rows than its header declares, was written by a
    different build (``format`` / ``codec`` / ``key_scheme``), or is not bound
    to ``policy``'s body (``body_digest``). Otherwise rehydrates the records,
    re-interning ``requires_python`` and hash-algorithm names so string identity
    matches a fresh parse.
    """
    stream = iter(lines)
    # Every failure is a miss, a read error included: this must not raise.
    try:
        header = _read_header(stream)
        if isinstance(header, str):
            return None

        format_, codec, key_scheme, body_digest, count = header
        if (
            format_ != FORMAT_VERSION
            or codec != CODEC
            or key_scheme != KEY_SCHEME
            or policy.body_digest is None
            or body_digest != policy.body_digest
        ):
            return None

        records: list[WheelFile | SdistFile] = []
        for batch in stream:
            # A list rather than a generator into ``extend``: measured cheaper.
            records.extend([_decode_row(row) for row in json.loads(batch)])
    except (OSError, ValueError, TypeError):
        return None
    return records if len(records) == count else None


def corruption_reason(lines: Iterable[bytes]) -> str | None:
    """Return a reason if a blob's ``lines`` are structurally corrupt, else ``None``.

    Distinguishes genuine corruption (garbage or truncated bytes, or a
    same-build blob whose rows are the wrong shape or the wrong number) from a
    benign miss, where the header names a different build or binds a different
    body. The read path warns only on the former. The row checks run only once
    the header names this exact build, since a foreign build may have written a
    shape this one never did; checking earlier would misreport version skew as
    corruption. This second pass only gates that warning; :func:`decode`
    returns ``None`` for every miss reason alike.
    """
    stream = iter(lines)
    seen = 0
    try:
        header = _read_header(stream)
        if isinstance(header, str):
            return header

        format_, codec, key_scheme, _body_digest, count = header
        if format_ != FORMAT_VERSION or codec != CODEC or key_scheme != KEY_SCHEME:
            # Version skew: another build may write a row shape this one never did.
            return None

        for batch in stream:
            for row in json.loads(batch):
                _decode_row(row)
                seen += 1
    except (ValueError, TypeError):
        return "unexpected row shape"
    except OSError:
        # A blob the filesystem will not hand over is unreadable, not corrupt.
        return None
    return None if seen == count else "unexpected row count"


def _read_header(lines: Iterator[bytes]) -> list[object] | str:
    """Consume the first of ``lines``, returning the header or a reason.

    A ``str`` is the corruption reason for a first line that is missing, is not
    JSON, or is not this codec's header shape. Shared so the read path and
    :func:`corruption_reason` cannot disagree on what a header is.
    """
    line = next(lines, None)
    if line is None:
        return "not valid JSON"
    try:
        header = json.loads(line)
    except ValueError:
        return "not valid JSON"
    if not (isinstance(header, list) and len(header) == _HEADER_LEN):
        return "unexpected header shape"
    return header


def _decode_row(row: object) -> WheelFile | SdistFile:
    """Rehydrate one row, dispatching on the tag its first element carries."""
    if not isinstance(row, list) or not row:
        raise _BadRowError
    tag = row[0]
    # bool is a subclass of int, so ``True == _TAG_SDIST`` without this.
    if type(tag) is not int:
        raise _BadRowError
    if tag == _TAG_WHEEL:
        return _decode_wheel(row)
    if tag == _TAG_SDIST:
        return _decode_sdist(row)
    raise _BadRowError


def _decode_wheel(row: Sequence[object]) -> WheelFile:
    """Rehydrate a wheel row.

    An integrity cell holding the index's own table passes through unparsed,
    for the record to parse on first read; any other form is parsed here.
    """
    (
        _,
        filename,
        url,
        version,
        requires_python,
        has_metadata,
        upload_time,
        hashes,
        size,
        metadata_hash,
    ) = row

    # ``type() is`` rather than ``isinstance``: bool is a subclass of int, so
    # ``True`` would pass as a size.
    if (
        type(filename) is not str
        or type(url) is not str
        or type(version) is not str
        or type(has_metadata) is not bool
        or (requires_python is not None and type(requires_python) is not str)
        or (upload_time is not None and type(upload_time) is not str)
        or (size is not None and type(size) is not int)
    ):
        raise _BadRowError

    return rehydrated_wheel(
        filename,
        url,
        version,
        None if requires_python is None else sys.intern(requires_python),
        has_metadata,
        upload_time,
        hashes if isinstance(hashes, dict) else _hashes(hashes),
        size,
        metadata_hash
        if isinstance(metadata_hash, dict)
        else _pair_or_none(metadata_hash),
    )


def _decode_sdist(row: Sequence[object]) -> SdistFile:
    """Rehydrate a source-distribution row; see :func:`_decode_wheel`."""
    _, filename, url, version, requires_python, upload_time, hashes, size = row

    if (
        type(filename) is not str
        or type(url) is not str
        or type(version) is not str
        or (requires_python is not None and type(requires_python) is not str)
        or (upload_time is not None and type(upload_time) is not str)
        or (size is not None and type(size) is not int)
    ):
        raise _BadRowError

    return rehydrated_sdist(
        filename,
        url,
        version,
        None if requires_python is None else sys.intern(requires_python),
        upload_time,
        hashes if isinstance(hashes, dict) else _hashes(hashes),
        size,
    )


# JSON hands back only its own types, so an exact type check is enough to keep a
# hand-written or corrupt blob from reaching a record's fields.
def _text(value: object) -> str:
    if type(value) is not str:
        raise _BadRowError
    return value


def _pair(value: object) -> tuple[str, str]:
    if not isinstance(value, list) or len(value) != _PAIR_LEN:
        raise _BadRowError
    return (_text(value[0]), _text(value[1]))


def _pair_or_none(value: object) -> tuple[str, str] | None:
    return None if value is None else _pair(value)


def _hashes(value: object) -> tuple[tuple[str, str], ...]:
    """Return the hash pairs with algorithm names interned."""
    if not isinstance(value, list):
        raise _BadRowError
    return tuple((sys.intern(algo), digest) for algo, digest in map(_pair, value))
