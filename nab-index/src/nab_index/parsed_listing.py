"""Codec for the parsed-listing cache: records <-> opaque cache blob.

Stores the post-:func:`nab_index.client._parse_files` records so a warm
resolve skips ``json.loads`` and wheel/sdist filename parsing. This module
owns the record<->bytes translation; :class:`~nab_index.cache.OnDiskCache`
treats the blob as opaque and knows nothing about record shapes.

Wire form (UTF-8 JSON of ``[header, rows]``):

* header ``[format, codec, key_scheme, body_digest]`` is checked before
  anything is trusted. A reader on a different ``format``, ``codec``, or
  ``key_scheme`` treats the entry as a miss and rebuilds, so a cache written by
  an older build self-heals instead of misdecoding. ``body_digest`` binds the
  blob to the raw body it was parsed from; :func:`decode` rejects a blob whose
  digest does not equal the policy's, so a raw-body update invalidates the
  derived form.
* rows hold one entry per surviving record, in the order the wire parse
  returned them, so a downstream stable-sort tie-break stays identical. Each
  row is a flat list tagged wheel or sdist by its first element. Every field is
  type-checked on the way back, and ``requires_python`` and hash-algorithm
  names are re-interned via ``sys.intern`` so the round trip reproduces the
  dedup the wire parse builds.

The blob is portable: one entry serves every interpreter that shares the cache,
and a body this module will not parse is a miss, never an exception reaching
the caller.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from .client import SdistFile, WheelFile

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .cache import CachePolicy

__all__ = ["corruption_reason", "decode", "encode"]

# Record version, redundant with the bucket suffix but guards against a stale
# blob surfacing under the current bucket. Bump it when the row shape changes
# or when the same body parses to different records: ``body_digest`` pins only
# the input.
FORMAT_VERSION = 1
# Serialization variant that wrote the rows, so a future codec switch
# self-heals rather than misdecodes.
CODEC = 1
# Version sort-key scheme the rows carry; 0 == no precomputed keys. A later
# scheme bumps this and self-heals via a reparse.
KEY_SCHEME = 0

_TOP_LEN = 2
_HEADER_LEN = 4
_PAIR_LEN = 2
_TAG_WHEEL = 0
_TAG_SDIST = 1


class _BadRowError(ValueError):
    """A row whose tag, arity, or field types are not what this codec wrote."""


def encode(files: list[WheelFile | SdistFile], body_digest: str) -> bytes:
    """Encode parsed listing ``files`` into a cache blob bound to ``body_digest``.

    ``body_digest`` is the sha256 hex of the raw body the records were parsed
    from; :func:`decode` rehydrates only when it matches the policy's digest.
    ``local_path`` is not stored (a parsed-cache entry always comes from a
    remote body) and ``metadata_url`` is a derived property, so neither rides
    the wire.
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
                    record.hashes,
                    record.size,
                    record.metadata_hash,
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
                    record.hashes,
                    record.size,
                ]
            )
    header = [FORMAT_VERSION, CODEC, KEY_SCHEME, body_digest]

    # Escape non-ASCII: a field kept verbatim from the listing, such as
    # ``requires_python``, can hold a lone surrogate with no UTF-8 form.
    return json.dumps([header, rows], separators=(",", ":")).encode()


def decode(blob: bytes, policy: CachePolicy) -> list[WheelFile | SdistFile] | None:
    """Decode a cache blob back to parsed records, or ``None`` to force a miss.

    Returns ``None`` (treat as a cache miss and rebuild from the raw body) when
    the blob does not decode, is the wrong shape, was written by a different
    build (``format`` / ``codec`` / ``key_scheme``), or is not bound to
    ``policy``'s body (``body_digest``).  Otherwise rehydrates the records,
    re-interning ``requires_python`` and hash-algorithm names so string identity
    matches a fresh parse.
    """
    try:
        loaded = json.loads(blob)
    except ValueError:
        return None
    if not (isinstance(loaded, list) and len(loaded) == _TOP_LEN):
        return None
    header, rows = loaded
    if not (isinstance(header, list) and len(header) == _HEADER_LEN):
        return None
    format_, codec, key_scheme, body_digest = header
    if (
        format_ != FORMAT_VERSION
        or codec != CODEC
        or key_scheme != KEY_SCHEME
        or policy.body_digest is None
        or body_digest != policy.body_digest
    ):
        return None
    # A blob whose header matches this build but whose rows are the wrong shape
    # must rebuild, not crash the resolve; the rows are untrusted too.
    try:
        return _decode_rows(rows)
    except (ValueError, TypeError):
        return None


def corruption_reason(blob: bytes) -> str | None:
    """Return a reason if ``blob`` is structurally corrupt, else ``None``.

    Distinguishes genuine corruption (garbage or truncated bytes, or a
    same-build blob whose rows are the wrong shape) from a benign miss, where
    the header names a different build or binds a different body. The read path
    warns only on the former. The row check runs only once the header names this
    exact build, since a foreign build may have written a shape this one never
    did; checking it earlier would misreport version skew as corruption. This is
    a second pass used only to gate that warning; :func:`decode` returns ``None``
    for every miss reason alike.
    """
    try:
        loaded = json.loads(blob)
    except ValueError:
        return "not valid JSON"
    if not (isinstance(loaded, list) and len(loaded) == _TOP_LEN):
        return "unexpected top-level shape"
    header, rows = loaded
    if not (isinstance(header, list) and len(header) == _HEADER_LEN):
        return "unexpected header shape"
    format_, codec, key_scheme, _body_digest = header
    if format_ != FORMAT_VERSION or codec != CODEC or key_scheme != KEY_SCHEME:
        # A different-build header is benign version skew, not corruption; its
        # rows may be a shape this build never wrote. A same-build body_digest
        # mismatch decodes cleanly below and returns None silently.
        return None
    try:
        _decode_rows(rows)
    except (ValueError, TypeError):
        return "unexpected row shape"
    return None


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
    return WheelFile(
        filename=_text(filename),
        url=_text(url),
        version=_text(version),
        requires_python=_interned_or_none(requires_python),
        has_metadata=_flag(has_metadata),
        upload_time=_text_or_none(upload_time),
        hashes=_hashes(hashes),
        size=_count_or_none(size),
        metadata_hash=_pair_or_none(metadata_hash),
    )


def _decode_sdist(row: Sequence[object]) -> SdistFile:
    _, filename, url, version, requires_python, upload_time, hashes, size = row
    return SdistFile(
        filename=_text(filename),
        url=_text(url),
        version=_text(version),
        requires_python=_interned_or_none(requires_python),
        upload_time=_text_or_none(upload_time),
        hashes=_hashes(hashes),
        size=_count_or_none(size),
    )


# JSON hands back only its own types, so an exact type check is enough to keep a
# hand-written or corrupt blob from reaching a record's fields. ``type() is``
# rather than ``isinstance`` because ``bool`` is a subclass of ``int`` and the
# two are not interchangeable in a record.
def _text(value: object) -> str:
    if type(value) is not str:
        raise _BadRowError
    return value


def _text_or_none(value: object) -> str | None:
    return None if value is None else _text(value)


def _interned_or_none(value: object) -> str | None:
    # JSON builds a fresh string per occurrence, so interning here is what
    # reproduces the dedup the wire parse leaves behind.
    return None if value is None else sys.intern(_text(value))


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise _BadRowError
    return value


def _count_or_none(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
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
