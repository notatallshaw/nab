"""Deterministic branch coverage for the ``parsed_listing`` codec.

The exact-equivalence contract is proved by the Hypothesis harness in
``nab-project/tests/property_python/test_parsed_listing_equiv.py``; that suite
is opt-in (``-m property``) and does not run under the coverage gate, so these
plain unit tests drive every branch of the codec: the wheel/sdist tag split,
present and absent ``requires_python``, present and absent ``metadata_hash``,
the integrity cells in both their raw and their parsed form, the preamble and
checksum gates, each header / digest gate that turns a stale or foreign blob
into a decode-to-``None`` miss, and the field checks that keep a hand-written
row from reaching a record.
"""

from __future__ import annotations

import dataclasses
import marshal
import sys

import pytest

from nab_index.cache import CachePolicy
from nab_index.client import SdistFile, WheelFile
from nab_index.parsed_listing import (
    _HEX_LEN,
    _LEN_AT,
    _MARSHAL_VERSION,
    _PAYLOAD_AT,
    _TAG_SDIST,
    _TAG_WHEEL,
    _WIRE_FORM,
    CODEC,
    FORMAT_VERSION,
    KEY_SCHEME,
    _frame,
    corruption_reason,
    decode,
    encode,
)
from nab_provider.records import defer_hashes, defer_sidecar_hash

DIGEST = "a" * 64

# A preamble naming another interpreter. It carries no checksum, since the
# prefix check rejects it before anything reads one.
_FOREIGN_PREAMBLE = b"nab-parsed-listing m4.some-other-interpreter "


def _blob(document: object) -> bytes:
    """A blob this build will read, carrying ``document`` whatever its shape."""
    return _frame(marshal.dumps(document, _MARSHAL_VERSION))


def _document(blob: bytes) -> object:
    """The document ``blob`` carries, read the way :func:`decode` reads it."""
    return marshal.loads(blob[_PAYLOAD_AT:])  # noqa: S302


def _flip_bit(blob: bytes, offset: int) -> bytes:
    """``blob`` with the low bit of the byte at ``offset`` inverted."""
    return blob[:offset] + bytes([blob[offset] ^ 1]) + blob[offset + 1 :]


SHA256 = sys.intern("sha256")
SHA512 = sys.intern("sha512")
RP = sys.intern(">=3.8")

# Header positions, matching the wire form the module documents.
_H_FORMAT = 0
_H_CODEC = 1
_H_KEY_SCHEME = 2
_H_ZIP_SDISTS = 4

# An sdist row is tag plus seven fields; the unknown-tag case relies on keeping
# that arity so the tag check is what rejects it, not the unpack.
_SDIST_ROW_LEN = 8

# Wheel row positions after the tag, for the field-check cases below.
_W_FILENAME = 1
_W_URL = 2
_W_VERSION = 3
_W_REQUIRES_PYTHON = 4
_W_HAS_METADATA = 5
_W_UPLOAD_TIME = 6
_W_HASHES = 7
_W_SIZE = 8
_W_METADATA_HASH = 9

# The same for an sdist row, which carries neither flag nor sidecar hash.
_S_FILENAME = 1
_S_URL = 2
_S_VERSION = 3
_S_REQUIRES_PYTHON = 4
_S_UPLOAD_TIME = 5
_S_SIZE = 7


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
    return decoded.files


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


def test_lone_surrogate_in_field_round_trips() -> None:
    """A field with no UTF-8 form still round-trips.

    A listing can put an unpaired surrogate in ``requires-python``, which
    ``_parse_files`` keeps verbatim.
    """
    record = dataclasses.replace(SDIST_FULL, requires_python=f"{RP}\ud800")

    assert _roundtrip([record]) == [record]


def test_blob_names_its_wire_form_and_the_build_inside() -> None:
    """The preamble pins the wire form; the header names the build."""
    blob = encode(SAMPLE, DIGEST)
    assert blob.startswith(_WIRE_FORM)

    header, rows = _document(blob)
    assert header == [FORMAT_VERSION, CODEC, KEY_SCHEME, DIGEST, []]
    assert [row[0] for row in rows] == [_TAG_WHEEL, _TAG_SDIST, _TAG_WHEEL, _TAG_SDIST]


def _tamper_header(index: int, value: object) -> bytes:
    header, rows = _document(encode(SAMPLE, DIGEST))
    header[index] = value
    return _blob([header, rows])


def _blob_with_rows(rows: object) -> bytes:
    """A blob whose header names this exact build, over caller-supplied rows."""
    header, _rows = _document(encode(SAMPLE, DIGEST))
    return _blob([header, rows])


def _wheel_row_with(index: int, value: object) -> bytes:
    """A blob holding one wheel row with a single field replaced."""
    _header, rows = _document(encode([WHEEL_FULL], DIGEST))
    rows[0][index] = value
    return _blob_with_rows(rows)


def _sdist_row_with(index: int, value: object) -> bytes:
    """A blob holding one sdist row with a single field replaced."""
    _header, rows = _document(encode([SDIST_FULL], DIGEST))
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
        b"no preamble",
        b"\xff\xfe not utf-8",
        _frame(b"\xff\xfe not a payload"),
        _FOREIGN_PREAMBLE + b"whatever that build wrote",
        _blob(7),
        _blob([1, 2, 3]),
        _blob(["bad-header", []]),
        _blob([[0, 1, 0], []]),
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
    _header, rows = _document(encode([SDIST_FULL], DIGEST))
    assert len(rows[0]) == _SDIST_ROW_LEN
    rows[0][0] = tag
    assert decode(_blob_with_rows(rows), _policy()) is None


@pytest.mark.parametrize(
    ("index", "value"),
    [
        (_W_FILENAME, 12345),
        (_W_FILENAME, None),
        (_W_URL, None),
        (_W_VERSION, 1.0),
        (_W_REQUIRES_PYTHON, 3),
        (_W_HAS_METADATA, "yes"),
        # bool is a subclass of int, so an int must not pass as a flag.
        (_W_HAS_METADATA, 1),
        (_W_UPLOAD_TIME, 20230101),
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
    """A field the wire form allows but the record does not never reaches one."""
    assert decode(_wheel_row_with(index, value), _policy()) is None


@pytest.mark.parametrize(
    ("index", "value"),
    [
        (_S_FILENAME, 12345),
        (_S_URL, None),
        (_S_VERSION, None),
        (_S_REQUIRES_PYTHON, 3),
        (_S_UPLOAD_TIME, 20230101),
        (_S_SIZE, "10"),
        # bool is a subclass of int here too.
        (_S_SIZE, True),
        (_S_SIZE, 1.5),
    ],
)
def test_wrong_sdist_field_type_is_miss(index: int, value: object) -> None:
    """An sdist row is checked on its own fields, not through the wheel's."""
    assert decode(_sdist_row_with(index, value), _policy()) is None


def test_absent_optional_fields_decode_as_none() -> None:
    blob = _wheel_row_with(_W_METADATA_HASH, None)
    decoded = decode(blob, _policy())
    assert decoded is not None
    wheel = decoded.files[0]
    assert isinstance(wheel, WheelFile)
    assert wheel.metadata_hash is None


@pytest.mark.parametrize(
    ("blob", "reason"),
    [
        (b"no preamble", "not this codec's wire form"),
        (_frame(b"\xff\xfe not a payload"), "unreadable payload"),
        (_blob(7), "unexpected top-level shape"),
        (_blob(["bad-header", []]), "unexpected header shape"),
    ],
)
def test_corruption_reason_names_the_structural_fault(blob: bytes, reason: str) -> None:
    assert corruption_reason(blob) == reason


def test_zip_sdists_round_trip_sorted() -> None:
    """The dropped releases ride the header, sorted rather than in set order."""
    blob = encode(SAMPLE, DIGEST, frozenset({"2.0", "1.0"}))
    header = _document(blob)[0]
    decoded = decode(blob, _policy())

    assert header[_H_ZIP_SDISTS] == ["1.0", "2.0"]
    assert decoded is not None
    assert decoded.zip_sdists == frozenset({"1.0", "2.0"})


@pytest.mark.parametrize("cell", ["1.0", {"1.0": True}, [1.0], [None]])
def test_bad_zip_sdists_cell_is_miss(cell: object) -> None:
    """A cell the wire form allows but the header does not rebuilds, not crashes."""
    assert decode(_tamper_header(_H_ZIP_SDISTS, cell), _policy()) is None


def test_corruption_reason_flags_a_bad_zip_sdists_cell() -> None:
    assert (
        corruption_reason(_tamper_header(_H_ZIP_SDISTS, "1.0"))
        == "unexpected header shape"
    )


def test_corruption_reason_flags_same_build_bad_rows() -> None:
    assert corruption_reason(_blob_with_rows([[7]])) == "unexpected row shape"


def test_corruption_reason_passes_a_valid_blob() -> None:
    assert corruption_reason(encode(SAMPLE, DIGEST)) is None


def test_foreign_build_header_is_not_corruption() -> None:
    """Version skew is a benign miss, so the read path rebuilds without warning."""
    assert corruption_reason(_tamper_header(_H_CODEC, CODEC + 1)) is None


def test_foreign_preamble_is_a_miss_and_not_corruption() -> None:
    """A blob another interpreter wrote is never handed to ``marshal``."""
    blob = _FOREIGN_PREAMBLE + b"whatever that build wrote"

    assert decode(blob, _policy()) is None
    assert corruption_reason(blob) is None


def test_a_flipped_length_field_is_a_miss() -> None:
    """A length that no longer matches the payload stops the decode.

    ``marshal`` reads a container's element count straight out of the buffer
    and allocates from it, so a payload nothing has vouched for can ask for a
    billion-element list rather than raise.
    """
    blob = _flip_bit(encode(SAMPLE, DIGEST), _LEN_AT)

    assert decode(blob, _policy()) is None
    assert corruption_reason(blob) == "unreadable payload"


def test_a_flipped_payload_byte_is_a_miss_not_altered_records() -> None:
    """A flip inside a filename still unmarshals, to a document that is wrong."""
    blob = encode(SAMPLE, DIGEST)
    flipped = _flip_bit(blob, blob.index(b"pkg-1.0-py3-none-any.whl"))
    assert _document(flipped) != _document(blob)

    assert decode(flipped, _policy()) is None
    assert corruption_reason(flipped) == "unreadable payload"


def test_a_non_hex_length_field_is_a_miss() -> None:
    """A preamble whose length field is not a number reads as corruption."""
    blob = encode(SAMPLE, DIGEST)
    blob = blob[:_LEN_AT] + b"." * _HEX_LEN + blob[_LEN_AT + _HEX_LEN :]

    assert decode(blob, _policy()) is None
    assert corruption_reason(blob) == "unreadable payload"


def test_digest_mismatch_is_not_corruption() -> None:
    assert corruption_reason(encode(SAMPLE, "b" * 64)) is None


def _deferred_wheel(hashes: object, *, sidecar: object) -> WheelFile:
    """A wheel holding ``hashes`` and ``sidecar`` as the index served them."""
    wheel = WheelFile(
        filename="pkg-1.0-py3-none-any.whl",
        url="https://files.example/pkg-1.0-py3-none-any.whl",
        version="1.0",
        requires_python=None,
        has_metadata=True,
        upload_time=None,
    )
    defer_hashes(wheel, hashes)
    defer_sidecar_hash(wheel, sidecar)
    return wheel


def test_unparsed_tables_ride_the_wire_as_the_index_served_them() -> None:
    wheel = _deferred_wheel({"SHA256": DIGEST.upper()}, sidecar={"sha256": DIGEST})

    rows = _document(encode([wheel], DIGEST))[1]

    assert rows[0][_W_HASHES] == {"SHA256": DIGEST.upper()}
    assert rows[0][_W_METADATA_HASH] == {"sha256": DIGEST}


def test_a_value_that_is_not_an_object_rides_as_its_parse() -> None:
    """Only a table defers, so any other value writes what the record parsed."""
    wheel = _deferred_wheel(["sha256", DIGEST], sidecar=True)

    rows = _document(encode([wheel], DIGEST))[1]

    assert rows[0][_W_HASHES] == []
    assert rows[0][_W_METADATA_HASH] is None


def test_a_many_algorithm_table_defers_as_the_index_served_it() -> None:
    wheel = _deferred_wheel({"sha256": DIGEST, "sha512": "f" * 128}, sidecar=True)

    rows = _document(encode([wheel], DIGEST))[1]

    assert rows[0][_W_HASHES] == {"sha256": DIGEST, "sha512": "f" * 128}
    assert wheel.hashes == ((SHA256, DIGEST), (SHA512, "f" * 128))


def test_a_rehydrated_record_defers_the_same_parse() -> None:
    (wheel,) = _roundtrip(
        [_deferred_wheel({"SHA256": DIGEST.upper()}, sidecar={"sha256": DIGEST})]
    )

    assert wheel.raw_hashes() == {"SHA256": DIGEST.upper()}
    assert wheel.raw_sidecar() == {"sha256": DIGEST}
    assert wheel.hashes == ((SHA256, DIGEST),)
    assert wheel.metadata_hash == (SHA256, DIGEST)


def test_a_rehydrated_sdist_defers_its_hashes() -> None:
    sdist = SdistFile(
        filename="pkg-1.0.tar.gz",
        url="https://files.example/pkg-1.0.tar.gz",
        version="1.0",
        requires_python=None,
        upload_time=None,
    )
    defer_hashes(sdist, {"SHA256": DIGEST.upper()})

    (decoded,) = _roundtrip([sdist])

    assert decoded.raw_hashes() == {"SHA256": DIGEST.upper()}
    assert decoded.hashes == ((SHA256, DIGEST),)


def test_a_row_is_the_same_whether_or_not_the_record_was_read() -> None:
    """A read record keeps its table, so encoding does not depend on read order."""
    unread = _deferred_wheel({"sha256": DIGEST}, sidecar={"sha256": DIGEST})
    read = _deferred_wheel({"sha256": DIGEST}, sidecar={"sha256": DIGEST})
    assert read.hashes == ((SHA256, DIGEST),)
    assert read.metadata_hash == (SHA256, DIGEST)

    assert encode([read], DIGEST) == encode([unread], DIGEST)
