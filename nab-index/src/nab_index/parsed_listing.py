"""Codec for the parsed-listing cache: records <-> opaque cache blob.

Stores the post-:func:`nab_index.client._parse_files` records so a warm
resolve skips ``json.loads`` and wheel/sdist filename parsing. This module
owns the record<->bytes translation; :class:`~nab_index.cache.OnDiskCache`
treats the blob as opaque and knows nothing about record shapes.

Wire form (UTF-8 JSON of ``[header, rows]``):

* header ``[format, codec, key_scheme, body_digest, zip_sdists]`` is checked
  before anything is trusted. A reader on a different ``format``, ``codec``, or
  ``key_scheme`` treats the entry as a miss and rebuilds, so a cache written by
  an older build self-heals instead of misdecoding. ``body_digest`` binds the
  blob to the raw body it was parsed from; :func:`decode` rejects a blob whose
  digest does not equal the policy's, so a raw-body update invalidates the
  derived form. ``zip_sdists`` names the releases the listing offered as a
  ``.zip`` sdist, which the parse drops, so no row can carry them.
* rows hold one entry per surviving record, in the order the wire parse
  returned them, so a downstream stable-sort tie-break stays identical. Each
  row is a flat list tagged wheel or sdist by its first element. Every field is
  type-checked on the way back, and ``requires_python`` and hash-algorithm
  names are re-interned via ``sys.intern`` so the round trip reproduces the
  dedup the wire parse builds.
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
from typing import TYPE_CHECKING, NamedTuple

from nab_provider.records import (
    SdistFile,
    WheelFile,
    rehydrated_sdist,
    rehydrated_wheel,
)

from ._json_decode import decode_json

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .cache import CachePolicy

__all__ = ["ParsedListing", "corruption_reason", "decode", "encode"]

# Record version, redundant with the bucket suffix but guards against a stale
# blob surfacing under the current bucket. Bump it when the header or row shape
# changes, or when the same body parses to different records: ``body_digest``
# pins only the input.
FORMAT_VERSION = 4
# Serialization variant that wrote the rows, so a future codec switch
# self-heals rather than misdecodes.
CODEC = 1
# Version sort-key scheme the rows carry; 0 == no precomputed keys. A later
# scheme bumps this and self-heals via a reparse.
KEY_SCHEME = 0

_TOP_LEN = 2
_HEADER_LEN = 5
_PAIR_LEN = 2
_TAG_WHEEL = 0
_TAG_SDIST = 1

# The header's first cells name the build that wrote the blob; the last two
# carry its data.
_BUILD_ID = (FORMAT_VERSION, CODEC, KEY_SCHEME)
_BUILD_CELLS = len(_BUILD_ID)
_H_DIGEST = 3
_H_ZIP_SDISTS = 4


class _BadRowError(ValueError):
    """A row or header cell whose shape is not what this codec wrote."""


class ParsedListing(NamedTuple):
    """A decoded blob: a listing's records, and what its parse dropped.

    ``zip_sdists`` names the releases the listing offered as a ``.zip`` sdist,
    which leaves no record of its own.
    """

    files: list[WheelFile | SdistFile]
    zip_sdists: frozenset[str]


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


def encode(
    files: list[WheelFile | SdistFile],
    body_digest: str,
    zip_sdists: frozenset[str] = frozenset(),
) -> bytes:
    """Encode parsed listing ``files`` into a cache blob bound to ``body_digest``.

    ``body_digest`` is the sha256 hex of the raw body the records were parsed
    from; :func:`decode` rehydrates only when it matches the policy's digest.
    ``zip_sdists`` names the releases offered as a ``.zip`` sdist, which the
    parse drops, so no row can carry them. ``local_path`` is not stored (a
    parsed-cache entry always comes from a remote body) and ``metadata_url``
    is a derived property, so neither rides the wire.
    """
    rows: list[list[object]] = []
    for record in files:
        if isinstance(record, WheelFile):
            rows.append(
                [
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
            )
        else:
            rows.append(
                [
                    _TAG_SDIST,
                    record.filename,
                    record.url,
                    record.version,
                    record.requires_python,
                    record.upload_time,
                    _hashes_cell(record),
                    record.size,
                ]
            )
    header = [*_BUILD_ID, body_digest, sorted(zip_sdists)]

    # Escape non-ASCII: a field kept verbatim from the listing, such as
    # ``requires_python``, can hold a lone surrogate with no UTF-8 form.
    return json.dumps([header, rows], separators=(",", ":")).encode()


def decode(blob: bytes, policy: CachePolicy) -> ParsedListing | None:
    """Decode a cache blob back to a parsed listing, or ``None`` to force a miss.

    Returns ``None`` (treat as a cache miss and rebuild from the raw body) when
    the blob does not decode, is the wrong shape, was written by a different
    build (``format`` / ``codec`` / ``key_scheme``), or is not bound to
    ``policy``'s body (``body_digest``).  Otherwise rehydrates the records,
    re-interning ``requires_python`` and hash-algorithm names so string identity
    matches a fresh parse.
    """
    try:
        loaded = decode_json(blob)
    except ValueError:
        return None
    if not (isinstance(loaded, list) and len(loaded) == _TOP_LEN):
        return None
    header, rows = loaded
    if not _names_this_build(header):
        return None
    if policy.body_digest is None or header[_H_DIGEST] != policy.body_digest:
        return None
    # A blob whose header matches this build but whose rows or ``.zip`` cell
    # are the wrong shape must rebuild, not crash the resolve.
    try:
        return ParsedListing(
            _decode_rows(rows), _decode_zip_sdists(header[_H_ZIP_SDISTS])
        )
    except (ValueError, TypeError):
        return None


def corruption_reason(blob: bytes) -> str | None:
    """Return a reason if ``blob`` is structurally corrupt, else ``None``.

    Distinguishes genuine corruption (garbage or truncated bytes, or a
    same-build blob whose rows are the wrong shape) from a benign miss, where
    the header names a different build or binds a different body. The read path
    warns only on the former. Every check past the build cells runs only once
    the header names this exact build, since a foreign build may have written a
    shape this one never did; checking earlier would misreport version skew as
    corruption. This is a second pass used only to gate that warning;
    :func:`decode` returns ``None`` for every miss reason alike.
    """
    try:
        loaded = decode_json(blob)
    except ValueError as exc:
        return str(exc)
    if not (isinstance(loaded, list) and len(loaded) == _TOP_LEN):
        return "unexpected top-level shape"
    header, rows = loaded
    if _from_another_build(header):
        return None
    if not _names_this_build(header) or _bad_zip_sdists_cell(header):
        return "unexpected header shape"
    try:
        _decode_rows(rows)
    except (ValueError, TypeError):
        return "unexpected row shape"
    return None


def _names_this_build(header: object) -> bool:
    """Whether ``header`` has this build's length and names this build."""
    return (
        isinstance(header, list)
        and len(header) == _HEADER_LEN
        and tuple(header[:_BUILD_CELLS]) == _BUILD_ID
    )


def _from_another_build(header: object) -> bool:
    """Whether ``header`` names a build other than this one.

    Read before any shape check, since another build may write a header shape
    this one never did, and calling that corruption would warn about every
    package a cache already holds when nab is upgraded.
    """
    return (
        isinstance(header, list)
        and len(header) >= _BUILD_CELLS
        and tuple(header[:_BUILD_CELLS]) != _BUILD_ID
    )


def _bad_zip_sdists_cell(header: Sequence[object]) -> bool:
    """Whether the header's ``.zip`` cell is not a list of version strings."""
    try:
        _decode_zip_sdists(header[_H_ZIP_SDISTS])
    except ValueError:
        return True
    return False


def _decode_zip_sdists(value: object) -> frozenset[str]:
    """Rehydrate the header cell naming the releases served as a ``.zip`` sdist."""
    if not isinstance(value, list):
        raise _BadRowError
    return frozenset(map(_text, value))


def _decode_rows(rows: object) -> list[WheelFile | SdistFile]:
    """Rehydrate every row, raising :class:`_BadRowError` on the first bad one."""
    if not isinstance(rows, list):
        raise _BadRowError
    return [_decode_row(row) for row in rows]


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
