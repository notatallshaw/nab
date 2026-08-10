"""Deterministic branch coverage for the ``parsed_listing`` codec.

The exact-equivalence contract is proved by the Hypothesis harness in
``nab-python/tests/property_python/test_parsed_listing_equiv.py``; that suite
is opt-in (``-m property``) and does not run under the coverage gate, so these
plain unit tests drive every branch of the codec: the wheel/sdist tag split,
present and absent ``requires_python``, present and absent ``metadata_hash``,
each header / digest gate that turns a stale or foreign blob into a
decode-to-``None`` miss, and the field checks that keep a hand-written row from
reaching a record.
"""

from __future__ import annotations

import json
import sys

import pytest

from nab_index.cache import CachePolicy
from nab_index.client import SdistFile, WheelFile
from nab_index.parsed_listing import (
    _TAG_SDIST,
    _TAG_WHEEL,
    CODEC,
    FORMAT_VERSION,
    KEY_SCHEME,
    corruption_reason,
    decode,
    encode,
)

DIGEST = "a" * 64

SHA256 = sys.intern("sha256")
SHA512 = sys.intern("sha512")
RP = sys.intern(">=3.8")

# Header positions, matching the wire form the module documents.
_H_FORMAT = 0
_H_CODEC = 1
_H_KEY_SCHEME = 2

# An sdist row is tag plus seven fields; the unknown-tag case relies on keeping
# that arity so the tag check is what rejects it, not the unpack.
_SDIST_ROW_LEN = 8

# Wheel row positions after the tag, for the field-check cases below.
_W_FILENAME = 1
_W_URL = 2
_W_REQUIRES_PYTHON = 4
_W_HAS_METADATA = 5
_W_HASHES = 7
_W_SIZE = 8
_W_METADATA_HASH = 9


def _policy(digest: str | None = DIGEST) -> CachePolicy:
    return CachePolicy(fetched_at=0, max_age=0, etag=None, body_digest=digest)


WHEEL_FULL = WheelFile(
    filename="pkg-1.0-py3-none-any.whl",
    url="https://files.example/pkg-1.0-py3-none-any.whl",
    version="1.0",
    requires_python=RP,
    has_metadata=True,
    upload_time="2023-01-01T00:00:00Z",
    hashes=((SHA256, "beef"),),
    size=1024,
    metadata_hash=("sha256", "dead"),
)
WHEEL_BARE = WheelFile(
    filename="pkg-1.0-py3-none-any.whl",
    url="https://files.example/pkg-1.0-py3-none-any.whl",
    version="1.0",
    requires_python=None,
    has_metadata=False,
    upload_time=None,
    hashes=(),
    size=None,
    metadata_hash=None,
)
SDIST_FULL = SdistFile(
    filename="pkg-1.0.tar.gz",
    url="https://files.example/pkg-1.0.tar.gz",
    version="1.0",
    requires_python=RP,
    upload_time="2023-01-01T00:00:00Z",
    hashes=((SHA256, "aaaa"), (SHA512, "bbbb")),
    size=10,
)
SDIST_BARE = SdistFile(
    filename="pkg-2.0.tar.gz",
    url="https://files.example/pkg-2.0.tar.gz",
    version="2.0",
    requires_python=None,
    upload_time=None,
    hashes=(),
    size=None,
)

# Interleaved so the tag split and every present/absent field are all hit.
SAMPLE: list[WheelFile | SdistFile] = [WHEEL_FULL, SDIST_FULL, WHEEL_BARE, SDIST_BARE]


def _roundtrip(files: list[WheelFile | SdistFile]) -> list[WheelFile | SdistFile]:
    decoded = decode(encode(files, DIGEST), _policy())
    assert decoded is not None
    return decoded


def test_roundtrip_preserves_records_and_order() -> None:
    decoded = _roundtrip(SAMPLE)
    assert decoded == SAMPLE
    for got, want in zip(decoded, SAMPLE, strict=True):
        assert type(got) is type(want)
        assert got.local_path is None


def test_requires_python_and_algo_reinterned_by_identity() -> None:
    decoded = _roundtrip(SAMPLE)
    wheel, sdist = decoded[0], decoded[1]
    assert wheel.requires_python is RP
    assert sdist.requires_python is RP
    assert wheel.hashes[0][0] is SHA256
    assert sdist.hashes[0][0] is SHA256
    assert sdist.hashes[1][0] is SHA512
    # An absent requires-python round-trips as None, not the sentinel.
    assert decoded[2].requires_python is None


def test_empty_listing_roundtrips() -> None:
    assert _roundtrip([]) == []


def test_metadata_hash_present_and_absent() -> None:
    decoded = _roundtrip([WHEEL_FULL, WHEEL_BARE])
    full, bare = decoded[0], decoded[1]
    assert isinstance(full, WheelFile)
    assert isinstance(bare, WheelFile)
    assert full.metadata_hash == ("sha256", "dead")
    assert bare.metadata_hash is None


def test_blob_is_portable_json() -> None:
    """The wire form carries no interpreter tag, so any reader can decode it."""
    header, rows = json.loads(encode(SAMPLE, DIGEST))
    assert header == [FORMAT_VERSION, CODEC, KEY_SCHEME, DIGEST]
    assert [row[0] for row in rows] == [_TAG_WHEEL, _TAG_SDIST, _TAG_WHEEL, _TAG_SDIST]


def _tamper_header(index: int, value: object) -> bytes:
    header, rows = json.loads(encode(SAMPLE, DIGEST))
    header[index] = value
    return json.dumps([header, rows]).encode()


def _blob_with_rows(rows: object) -> bytes:
    """A blob whose header names this exact build, over caller-supplied rows."""
    header, _rows = json.loads(encode(SAMPLE, DIGEST))
    return json.dumps([header, rows]).encode()


def _wheel_row_with(index: int, value: object) -> bytes:
    """A blob holding one wheel row with a single field replaced."""
    _header, rows = json.loads(encode([WHEEL_FULL], DIGEST))
    rows[0][index] = value
    return _blob_with_rows(rows)


def test_valid_blob_decodes() -> None:
    assert decode(encode(SAMPLE, DIGEST), _policy()) is not None


def test_format_mismatch_is_miss() -> None:
    assert decode(_tamper_header(_H_FORMAT, FORMAT_VERSION + 1), _policy()) is None


def test_codec_mismatch_is_miss() -> None:
    assert decode(_tamper_header(_H_CODEC, CODEC + 1), _policy()) is None


def test_key_scheme_mismatch_is_miss() -> None:
    assert decode(_tamper_header(_H_KEY_SCHEME, KEY_SCHEME + 1), _policy()) is None


def test_digest_mismatch_is_miss() -> None:
    assert decode(encode(SAMPLE, DIGEST), _policy("b" * 64)) is None


def test_policy_without_digest_is_miss() -> None:
    assert decode(encode(SAMPLE, DIGEST), _policy(None)) is None


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"not json",
        b"\xff\xfe not utf-8",
        json.dumps(7).encode(),
        json.dumps([1, 2, 3]).encode(),
        json.dumps(["bad-header", []]).encode(),
        json.dumps([[0, 1, 0], []]).encode(),
    ],
)
def test_malformed_blob_is_miss(blob: bytes) -> None:
    assert decode(blob, _policy()) is None


def test_truncated_blob_is_miss() -> None:
    blob = encode(SAMPLE, DIGEST)
    assert decode(blob[: len(blob) // 2], _policy()) is None


@pytest.mark.parametrize(
    "rows",
    [
        "not-a-list",
        ["not-a-row"],
        [[]],
        [[7]],
        [[_TAG_WHEEL]],
        [[_TAG_SDIST]],
        [[None, "short"]],
    ],
)
def test_rows_corrupt_but_header_valid_is_miss(rows: object) -> None:
    """A same-build header over wrong-shape rows self-heals to a miss, not a crash."""
    assert decode(_blob_with_rows(rows), _policy()) is None


@pytest.mark.parametrize("tag", [7, -1, "0", None, True])
def test_unknown_tag_on_a_well_formed_row_is_miss(tag: object) -> None:
    """An unrecognised tag is rejected on its own, not decoded as the other kind.

    The row keeps sdist arity, so a codec that fell through to the sdist branch
    instead of rejecting would decode it happily. That is what this pins.
    """
    _header, rows = json.loads(encode([SDIST_FULL], DIGEST))
    assert len(rows[0]) == _SDIST_ROW_LEN
    rows[0][0] = tag
    assert decode(_blob_with_rows(rows), _policy()) is None


@pytest.mark.parametrize(
    ("index", "value"),
    [
        (_W_FILENAME, 12345),
        (_W_FILENAME, None),
        (_W_URL, None),
        (_W_REQUIRES_PYTHON, 3),
        (_W_HAS_METADATA, "yes"),
        # bool is a subclass of int, so an int must not pass as a flag.
        (_W_HAS_METADATA, 1),
        (_W_SIZE, "1024"),
        # ... nor a bool as a count.
        (_W_SIZE, True),
        (_W_HASHES, "sha256"),
        (_W_HASHES, [["sha256"]]),
        (_W_HASHES, [["sha256", 5]]),
        (_W_HASHES, ["sha256"]),
        (_W_METADATA_HASH, "sha256"),
        (_W_METADATA_HASH, ["sha256", "dead", "extra"]),
    ],
)
def test_wrong_field_type_is_miss(index: int, value: object) -> None:
    """A field JSON allows but the record does not never reaches a record."""
    assert decode(_wheel_row_with(index, value), _policy()) is None


def test_absent_optional_fields_decode_as_none() -> None:
    blob = _wheel_row_with(_W_METADATA_HASH, None)
    decoded = decode(blob, _policy())
    assert decoded is not None
    wheel = decoded[0]
    assert isinstance(wheel, WheelFile)
    assert wheel.metadata_hash is None


@pytest.mark.parametrize(
    ("blob", "reason"),
    [
        (b"not json", "not valid JSON"),
        (json.dumps(7).encode(), "unexpected top-level shape"),
        (json.dumps(["bad-header", []]).encode(), "unexpected header shape"),
    ],
)
def test_corruption_reason_names_the_structural_fault(blob: bytes, reason: str) -> None:
    assert corruption_reason(blob) == reason


def test_corruption_reason_flags_same_build_bad_rows() -> None:
    assert corruption_reason(_blob_with_rows([[7]])) == "unexpected row shape"


def test_corruption_reason_passes_a_valid_blob() -> None:
    assert corruption_reason(encode(SAMPLE, DIGEST)) is None


def test_foreign_build_header_is_not_corruption() -> None:
    """Version skew is a benign miss, so the read path rebuilds without warning."""
    assert corruption_reason(_tamper_header(_H_CODEC, CODEC + 1)) is None


def test_digest_mismatch_is_not_corruption() -> None:
    assert corruption_reason(encode(SAMPLE, "b" * 64)) is None
