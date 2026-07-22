"""Deterministic branch coverage for the ``parsed_listing`` codec.

The exact-equivalence contract is proved by the Hypothesis harness in
``nab-python/tests/property_python/test_parsed_listing_equiv.py``; that suite
is opt-in (``-m property``) and does not run under the coverage gate, so these
plain unit tests drive every branch of the codec: the wheel/sdist split and
order vector, the deduplicated tables with present and absent
``requires_python``, present and absent ``metadata_hash``, and each header /
digest gate that turns a stale or foreign blob into a decode-to-``None`` miss.
"""

from __future__ import annotations

import marshal
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
    decode,
    encode,
)

DIGEST = "a" * 64

SHA256 = sys.intern("sha256")
SHA512 = sys.intern("sha512")
RP = sys.intern(">=3.8")


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

# Interleaved so the order vector, the wheel/sdist split, the version-table
# repeat (1.0 appears three times), and every present/absent field are all hit.
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


def _tamper(index: int, value: object) -> bytes:
    header, body = marshal.loads(encode(SAMPLE, DIGEST))  # noqa: S302
    header = list(header)
    header[index] = value
    return marshal.dumps((tuple(header), body))


def _tamper_body(value: object) -> bytes:
    header, _body = marshal.loads(encode(SAMPLE, DIGEST))  # noqa: S302
    return marshal.dumps((header, value))


def test_valid_blob_decodes() -> None:
    assert decode(encode(SAMPLE, DIGEST), _policy()) is not None


def test_format_mismatch_is_miss() -> None:
    assert decode(_tamper(0, FORMAT_VERSION + 1), _policy()) is None


def test_codec_mismatch_is_miss() -> None:
    assert decode(_tamper(1, CODEC + 1), _policy()) is None


def test_interp_mismatch_is_miss() -> None:
    other = (sys.version_info[0], sys.version_info[1] + 1)
    assert decode(_tamper(2, other), _policy()) is None


def test_key_scheme_mismatch_is_miss() -> None:
    assert decode(_tamper(3, KEY_SCHEME + 1), _policy()) is None


def test_digest_mismatch_is_miss() -> None:
    assert decode(encode(SAMPLE, DIGEST), _policy("b" * 64)) is None


def test_policy_without_digest_is_miss() -> None:
    assert decode(encode(SAMPLE, DIGEST), _policy(None)) is None


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"not marshal",
        marshal.dumps(7),
        marshal.dumps((1, 2, 3)),
        marshal.dumps(("bad-header", "body")),
    ],
)
def test_malformed_blob_is_miss(blob: bytes) -> None:
    assert decode(blob, _policy()) is None


def test_truncated_blob_is_miss() -> None:
    blob = encode(SAMPLE, DIGEST)
    assert decode(blob[: len(blob) // 2], _policy()) is None


@pytest.mark.parametrize(
    "body",
    [
        (),
        ([], []),
        ([], [], [], []),
        "not-a-tuple",
        ([], [], [_TAG_WHEEL]),
        ([("short", "row")], [], [_TAG_WHEEL]),
        ([], [], [_TAG_SDIST]),
    ],
)
def test_body_corrupt_but_header_valid_is_miss(body: object) -> None:
    # A blob that survives marshal.loads and matches the current build/digest
    # header but carries a wrong-shape body must self-heal to a miss, not crash.
    assert decode(_tamper_body(body), _policy()) is None
