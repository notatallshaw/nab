"""Translate parsed listing records to and from an opaque cache blob.

The UTF-8 JSON wire form is ``[header, rows]``. The header carries format,
codec, sort-key scheme, source-body digest, served zip-sdist versions,
sparse withdrawal flags, and the response page URL.
An incompatible header, body digest or page URL is a cache miss.

Rows preserve source order and tag each flat record as a wheel or sdist. A
version cell is ``null`` where the row above carries the same version. Decode
checks every field and restores interned strings. Integrity cells retain either
the index table or parsed pairs so hash parsing stays deferred. A raw
table containing only sha256 uses its digest string instead of a JSON object.

Invalid blobs are cache misses, not caller-visible errors. The on-disk cache
treats this representation as opaque.
"""

from __future__ import annotations

__lazy_modules__ = (
    "json",
    "nab_index._json_decode",
    "nab_provider.digest",
    "nab_provider.records",
    "sys",
)

import json
import sys
from typing import TYPE_CHECKING, NamedTuple

from nab_provider.digest import is_hex_digest
from nab_provider.records import (
    SdistFile,
    WheelFile,
    rehydrated_sdist,
    rehydrated_wheel,
)

from ._json_decode import decode_json

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._json_decode import JsonValue
    from .cache import CachePolicy

__all__ = [
    "ParsedListing",
    "corruption_reason",
    "decode",
    "encode",
    "parse_body_digest",
]

# Record version, redundant with the bucket suffix but guards against a stale
# blob surfacing under the current bucket. Bump it when the header or row shape
# changes, or when the same body parses to different records: ``body_digest``
# pins only the input.
FORMAT_VERSION = 9
# Serialization variant that wrote the rows, so a future codec switch
# self-heals rather than misdecodes.
CODEC = 1
# Version sort-key scheme the rows carry; 0 == no precomputed keys. A later
# scheme bumps this and self-heals via a reparse.
KEY_SCHEME = 0

_TOP_LEN = 2
_HEADER_LEN = 7
_PAIR_LEN = 2
_TAG_WHEEL = 0
_TAG_SDIST = 1

# The first three header cells identify the build; the rest carry listing data.
_BUILD_ID = (FORMAT_VERSION, CODEC, KEY_SCHEME)
_BUILD_CELLS = len(_BUILD_ID)
_H_DIGEST = 3
_H_ZIP_SDISTS = 4
_H_YANKS = 5
_H_PAGE_URL = 6


class _InvalidRecordError(ValueError):
    """A row or header cell whose shape is not what this codec wrote."""


class ParsedListing(NamedTuple):
    """A decoded listing's records and releases offered as ``.zip`` sdists."""

    files: list[WheelFile | SdistFile]
    zip_sdists: frozenset[str]


def encode(
    files: list[WheelFile | SdistFile],
    body_digest: str,
    zip_sdists: frozenset[str] = frozenset(),
    *,
    page_url: str | None = None,
) -> bytes:
    """Encode parsed listing ``files`` into a cache blob bound to ``body_digest``.

    ``body_digest`` is the sha256 hex of the raw body the records were parsed
    from; :func:`decode` rehydrates only when it matches the policy's digest.
    ``page_url`` binds relative file URLs to the page used to resolve them.
    ``zip_sdists`` names the releases offered as a ``.zip`` sdist, including
    links that cannot become records. ``local_path`` is not stored (a
    parsed-cache entry always comes from a remote body) and ``metadata_url``
    is a derived property, so neither rides the wire.
    """
    rows: list[list[object]] = []
    yanks: dict[str, bool | str] = {}
    previous_version: str | None = None
    for record in files:
        version = record.version
        version_cell = None if version == previous_version else version
        previous_version = version

        if isinstance(record, WheelFile):
            rows.append(
                [
                    _TAG_WHEEL,
                    record.filename,
                    record.url,
                    version_cell,
                    record.requires_python,
                    record.has_metadata,
                    record.upload_time,
                    record.hashes_for_rehydration(),
                    record.size,
                    record.sidecar_for_rehydration(),
                ]
            )
        else:
            rows.append(
                [
                    _TAG_SDIST,
                    record.filename,
                    record.url,
                    version_cell,
                    record.requires_python,
                    record.upload_time,
                    record.hashes_for_rehydration(),
                    record.size,
                ]
            )
        if record.yanked:
            yanks[str(len(rows) - 1)] = record.yanked
    header = [*_BUILD_ID, body_digest, sorted(zip_sdists), yanks, page_url]

    # Escape non-ASCII: a field kept verbatim from the listing, such as
    # ``requires_python``, can hold a lone surrogate with no UTF-8 form.
    return json.dumps([header, rows], separators=(",", ":")).encode()


def decode(blob: bytes, policy: CachePolicy) -> ParsedListing | None:
    """Restore records when the build, response digest and page URL match.

    Return ``None`` for a stale or malformed entry so the caller can rebuild
    it from the cached Simple API response.
    """
    try:
        loaded = decode_json(blob)
    except ValueError:
        return None
    if not (isinstance(loaded, list) and len(loaded) == _TOP_LEN):
        return None
    raw_header, rows = loaded
    try:
        header = _current_header(raw_header)
    except _InvalidRecordError:
        return None
    if (
        header is None
        or policy.body_digest is None
        or header[_H_DIGEST] != policy.body_digest
        or header[_H_PAGE_URL] != policy.page_url
    ):
        return None
    # A blob whose header matches this build but whose rows or ``.zip`` cell
    # are the wrong shape must rebuild, not crash the resolve.
    try:
        return ParsedListing(
            _restore_yanks(_decode_rows(rows), header[_H_YANKS]),
            _decode_zip_sdists(header[_H_ZIP_SDISTS]),
        )
    except (ValueError, TypeError):
        return None


def corruption_reason(blob: bytes) -> str | None:
    """Explain a malformed cache entry, or return ``None`` when it is valid.

    A different build may have written different fields after its build ID.
    Check those fields and the rows only for this build. The cache reader
    separately compares the digest and page URL with its cache policy.
    """
    try:
        loaded = decode_json(blob)
    except ValueError as exc:
        return str(exc)
    if not (isinstance(loaded, list) and len(loaded) == _TOP_LEN):
        return "unexpected top-level shape"
    raw_header, rows = loaded
    try:
        header = _current_header(raw_header)
        if header is None:
            return None
        _decode_zip_sdists(header[_H_ZIP_SDISTS])
    except _InvalidRecordError:
        return "unexpected header shape"
    try:
        _restore_yanks(_decode_rows(rows), header[_H_YANKS])
    except (ValueError, TypeError):
        return "unexpected row shape"
    return None


def _current_header(value: JsonValue) -> list[JsonValue] | None:
    """Read this build's header. A different build can have different fields."""
    if not isinstance(value, list) or len(value) < _BUILD_CELLS:
        raise _InvalidRecordError
    version, codec, key_scheme = value[:_BUILD_CELLS]
    if (
        type(version) is not int
        or type(codec) is not int
        or type(key_scheme) is not int
    ):
        raise _InvalidRecordError
    if (version, codec, key_scheme) != _BUILD_ID:
        return None

    if len(value) != _HEADER_LEN or parse_body_digest(value[_H_DIGEST]) is None:
        raise _InvalidRecordError
    page_url = value[_H_PAGE_URL]
    if page_url is not None and not isinstance(page_url, str):
        raise _InvalidRecordError
    return value


_SHA256_HEX_LEN = 64


def parse_body_digest(value: JsonValue) -> str | None:
    """Return a stored Simple API response's SHA-256 digest when it is valid."""
    if (
        isinstance(value, str)
        and len(value) == _SHA256_HEX_LEN
        and is_hex_digest(value)
    ):
        return value
    return None


def _decode_zip_sdists(value: JsonValue) -> frozenset[str]:
    """Rehydrate the header cell naming the releases served as a ``.zip`` sdist."""
    if not isinstance(value, list):
        raise _InvalidRecordError
    return frozenset(map(_text, value))


def _restore_yanks(
    files: list[WheelFile | SdistFile], value: JsonValue
) -> list[WheelFile | SdistFile]:
    """Reattach sparse withdrawal metadata after validating its row references."""
    if not isinstance(value, dict):
        raise _InvalidRecordError
    if not value:
        return files
    from nab_provider.yanking import (  # noqa: PLC0415 - load yank-only records on demand
        YankedListing,
        mark_yanked,
    )

    for key, reason in value.items():
        if not key.isascii() or not key.isdigit():
            raise _InvalidRecordError
        index = int(key)
        if str(index) != key or index >= len(files):
            raise _InvalidRecordError
        if reason is not True and (not isinstance(reason, str) or not reason):
            raise _InvalidRecordError
        files[index] = mark_yanked(files[index], reason=reason)
    return YankedListing(
        files, withdrawn_versions=(files[int(key)].version for key in value)
    )


def _decode_rows(rows: JsonValue) -> list[WheelFile | SdistFile]:
    """Restore the wheel and sdist rows in listing order."""
    if not isinstance(rows, list):
        raise _InvalidRecordError
    decoded: list[WheelFile | SdistFile] = []
    previous_version: str | None = None
    record: WheelFile | SdistFile
    for row in rows:
        if not isinstance(row, list) or not row:
            raise _InvalidRecordError
        tag = row[0]
        # bool is a subclass of int, so ``True == _TAG_SDIST`` without this.
        if type(tag) is not int:
            raise _InvalidRecordError
        if tag == _TAG_WHEEL:
            record = _decode_wheel(row, previous_version)
        elif tag == _TAG_SDIST:
            record = _decode_sdist(row, previous_version)
        else:
            raise _InvalidRecordError
        previous_version = record.version
        decoded.append(record)
    return decoded


def _decode_wheel(row: Sequence[JsonValue], previous_version: str | None) -> WheelFile:
    """Rehydrate a wheel row, whose ``null`` version means ``previous_version``.

    A ``null`` on the first row leaves the version ``None``, which the field
    check rejects.

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

    if version is None:
        version = previous_version

    # ``type() is`` rather than ``isinstance``: bool is a subclass of int, so
    # ``True`` would pass as a size.
    if (
        type(filename) is not str
        or type(url) is not str
        or type(version) is not str
        or type(has_metadata) is not bool
        or (requires_python is not None and type(requires_python) is not str)
        or (upload_time is not None and type(upload_time) is not str)
        or (size is not None and (type(size) is not int or size < 0))
    ):
        raise _InvalidRecordError

    return rehydrated_wheel(
        filename,
        url,
        version,
        None if requires_python is None else sys.intern(requires_python),
        has_metadata,
        upload_time,
        hashes if isinstance(hashes, (dict, str)) else _hashes(hashes),
        size,
        metadata_hash
        if isinstance(metadata_hash, (dict, str))
        else _pair_or_none(metadata_hash),
    )


def _decode_sdist(row: Sequence[JsonValue], previous_version: str | None) -> SdistFile:
    """Rehydrate a source-distribution row; see :func:`_decode_wheel`."""
    _, filename, url, version, requires_python, upload_time, hashes, size = row

    if version is None:
        version = previous_version

    if (
        type(filename) is not str
        or type(url) is not str
        or type(version) is not str
        or (requires_python is not None and type(requires_python) is not str)
        or (upload_time is not None and type(upload_time) is not str)
        or (size is not None and (type(size) is not int or size < 0))
    ):
        raise _InvalidRecordError

    return rehydrated_sdist(
        filename,
        url,
        version,
        None if requires_python is None else sys.intern(requires_python),
        upload_time,
        hashes if isinstance(hashes, (dict, str)) else _hashes(hashes),
        size,
    )


# JSON hands back only its own types, so an exact type check is enough to keep a
# hand-written or corrupt blob from reaching a record's fields.
def _text(value: JsonValue) -> str:
    if type(value) is not str:
        raise _InvalidRecordError
    return value


def _pair(value: JsonValue) -> tuple[str, str]:
    if not isinstance(value, list) or len(value) != _PAIR_LEN:
        raise _InvalidRecordError
    return (_text(value[0]), _text(value[1]))


def _pair_or_none(value: JsonValue) -> tuple[str, str] | None:
    return None if value is None else _pair(value)


def _hashes(value: JsonValue) -> tuple[tuple[str, str], ...]:
    """Return the hash pairs with algorithm names interned."""
    if not isinstance(value, list):
        raise _InvalidRecordError
    return tuple((sys.intern(algo), digest) for algo, digest in map(_pair, value))
