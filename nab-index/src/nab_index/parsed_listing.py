"""Codec for the parsed-listing cache: records <-> opaque cache blob.

Stores the post-:func:`nab_index.client._parse_files` records so a warm
resolve skips ``json.loads`` and wheel/sdist filename parsing. This module
owns the record<->bytes translation; :class:`~nab_index.cache.OnDiskCache`
treats the blob as opaque and knows nothing about record shapes.

Wire form (``marshal.dumps`` of ``(header, body)``):

* header ``(format, codec, interp, key_scheme, body_digest)`` is checked
  before anything is trusted. A reader on a different ``format``, ``codec``,
  interpreter minor (``interp``), or ``key_scheme`` treats the entry as a miss
  and rebuilds, so a cache written by an older build self-heals instead of
  misdecoding. ``body_digest`` binds the blob to the raw body it was parsed
  from; :func:`decode` rejects a blob whose digest does not equal the policy's,
  so a raw-body update invalidates the derived form.
* body splits the surviving wheel and sdist records into two lists plus an
  ``order`` vector of small ints that reproduces the interleaved survivor order,
  keeping a downstream stable-sort tie-break identical. Each record is a flat
  tuple of its fields; ``requires_python`` and hash-algorithm names are
  re-interned on decode via ``sys.intern`` so the round trip reproduces the
  dedup the wire parse builds.

``marshal`` builds only builtins, so a garbage blob raises on load rather than
running anything, and :func:`decode` reports a miss. The blob is
interpreter-specific, which the ``interp`` header tag guards.
"""

from __future__ import annotations

import marshal
import sys
from typing import TYPE_CHECKING

from .client import SdistFile, WheelFile

if TYPE_CHECKING:
    from .cache import CachePolicy

__all__ = ["decode", "encode"]

# Record-shape version, redundant with the bucket suffix but cheap insurance
# against a stale-shape blob surfacing under the current bucket.
FORMAT_VERSION = 0
# Serialization variant that wrote the body, so a future codec switch
# self-heals rather than misdecodes: 1 == M1 (flat per-record tuples, shipped),
# 2 reserved for M2 (string-deduplicated table).
CODEC_M1 = 1
CODEC_M2 = 2
CODEC = CODEC_M1
# Version sort-key scheme the rows carry. 0 == no precomputed keys. A later V1
# scalar-key scheme bumps this and self-heals via a reparse with no format
# break: higher schemes append their columns (a sort_key, an upload_epoch) to
# the record tuples, and scheme 0 pays nothing because it appends nothing.
KEY_SCHEME_NONE = 0
KEY_SCHEME = KEY_SCHEME_NONE

# marshal is interpreter-specific; a reader on a different minor version misses.
_INTERP = sys.version_info[:2]

_TOP_LEN = 2
_HEADER_LEN = 5
_TAG_WHEEL = 0
_TAG_SDIST = 1


def encode(files: list[WheelFile | SdistFile], body_digest: str) -> bytes:
    """Encode parsed listing ``files`` into a cache blob bound to ``body_digest``.

    ``body_digest`` is the sha256 hex of the raw body the records were parsed
    from; :func:`decode` rehydrates only when it matches the policy's digest.
    ``local_path`` is not stored (a parsed-cache entry always comes from a
    remote body) and ``metadata_url`` is a derived property, so neither rides
    the wire.
    """
    wheel_rows: list[tuple[object, ...]] = []
    sdist_rows: list[tuple[object, ...]] = []
    order: list[int] = []
    for record in files:
        if isinstance(record, WheelFile):
            order.append(_TAG_WHEEL)
            wheel_rows.append(
                (
                    record.filename,
                    record.url,
                    record.version,
                    record.requires_python,
                    record.has_metadata,
                    record.upload_time,
                    record.hashes,
                    record.size,
                    record.metadata_hash,
                )
            )
        else:
            order.append(_TAG_SDIST)
            sdist_rows.append(
                (
                    record.filename,
                    record.url,
                    record.version,
                    record.requires_python,
                    record.upload_time,
                    record.hashes,
                    record.size,
                )
            )
    header = (FORMAT_VERSION, CODEC, _INTERP, KEY_SCHEME, body_digest)
    return marshal.dumps((header, (wheel_rows, sdist_rows, order)))


def decode(blob: bytes, policy: CachePolicy) -> list[WheelFile | SdistFile] | None:
    """Decode a cache blob back to parsed records, or ``None`` to force a miss.

    Returns ``None`` (treat as a cache miss and rebuild from the raw body) when
    the blob does not decode, is the wrong shape, was written by a different
    build (``format`` / ``codec`` / ``interp`` / ``key_scheme``), or is not
    bound to ``policy``'s body (``body_digest``).  Otherwise rehydrates the
    records, re-interning ``requires_python`` and hash-algorithm names so
    string identity matches a fresh parse.
    """
    try:
        loaded = marshal.loads(blob)  # noqa: S302
    except (ValueError, EOFError, TypeError):
        return None
    if not (isinstance(loaded, tuple) and len(loaded) == _TOP_LEN):
        return None
    header, body = loaded
    if not (isinstance(header, tuple) and len(header) == _HEADER_LEN):
        return None
    format_, codec, interp, key_scheme, body_digest = header
    if (
        format_ != FORMAT_VERSION
        or codec != CODEC
        or interp != _INTERP
        or key_scheme != KEY_SCHEME
        or policy.body_digest is None
        or body_digest != policy.body_digest
    ):
        return None
    return _decode_body(body)


def _decode_body(
    body: tuple[list, list, list],
) -> list[WheelFile | SdistFile]:
    wheel_rows, sdist_rows, order = body
    wheels = iter(wheel_rows)
    sdists = iter(sdist_rows)
    out: list[WheelFile | SdistFile] = []
    for tag in order:
        if tag == _TAG_WHEEL:
            out.append(_decode_wheel(next(wheels)))
        else:
            out.append(_decode_sdist(next(sdists)))
    return out


def _intern_hashes(
    hashes: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    # Algo names are a tiny fixed vocabulary the wire parse interns; re-intern
    # on decode so repeated ("sha256", ...) share one object, digests do not.
    return tuple((sys.intern(algo), digest) for algo, digest in hashes)


def _intern_optional(value: str | None) -> str | None:
    # requires_python is interned on the wire path; re-intern so the dedup
    # survives the round trip, leaving None untouched.
    return None if value is None else sys.intern(value)


def _decode_wheel(row: tuple[object, ...]) -> WheelFile:
    (
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
        filename=filename,
        url=url,
        version=version,
        requires_python=_intern_optional(requires_python),
        has_metadata=has_metadata,
        upload_time=upload_time,
        hashes=_intern_hashes(hashes),
        size=size,
        metadata_hash=metadata_hash,
    )


def _decode_sdist(row: tuple[object, ...]) -> SdistFile:
    filename, url, version, requires_python, upload_time, hashes, size = row
    return SdistFile(
        filename=filename,
        url=url,
        version=version,
        requires_python=_intern_optional(requires_python),
        upload_time=upload_time,
        hashes=_intern_hashes(hashes),
        size=size,
    )
