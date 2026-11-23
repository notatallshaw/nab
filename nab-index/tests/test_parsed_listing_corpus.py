"""Round-trip recorded package listings and reject unusable cache records."""

import hashlib
import json
from pathlib import Path

import pytest

from nab_index.cache import CachePolicy
from nab_index.client import MalformedSimpleResponseError, _parse_files
from nab_index.parsed_listing import decode, encode

from .listing_roundtrip import INDEX, assert_listing_roundtrip

CORPUS_DIR = Path(__file__).parent / "corpus_parsed_listing"


@pytest.mark.parametrize("package", ["boto3", "botocore", "cryptography", "torch"])
def test_roundtrip_corpus(package: str) -> None:
    """The trimmed real corpus round-trips element-for-element."""
    body = (CORPUS_DIR / f"{package}.json").read_bytes()
    assert_listing_roundtrip(package, body)


def test_empty_listing_roundtrips() -> None:
    """An empty file list encodes and decodes to an empty list."""
    body = json.dumps({"name": "nothing", "files": []}).encode()
    assert_listing_roundtrip("nothing", body)


def _sample_blob() -> tuple[bytes, CachePolicy]:
    body = (CORPUS_DIR / "boto3.json").read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    parsed = _parse_files(json.loads(body), INDEX, "boto3")
    policy = CachePolicy(fetched_at=0, max_age=0, etag=None, body_digest=digest)
    return encode(parsed, digest), policy


def _retamper(header_index: int, value: object) -> tuple[bytes, CachePolicy]:
    """Re-encode a valid blob with one header field replaced."""
    blob, policy = _sample_blob()
    header, rows = json.loads(blob)
    header[header_index] = value
    return json.dumps([header, rows]).encode(), policy


def test_digest_mismatch_decodes_to_none() -> None:
    """A parsed blob whose header digest differs from the policy is a miss."""
    blob, _ = _sample_blob()
    other = CachePolicy(fetched_at=0, max_age=0, etag=None, body_digest="0" * 64)
    assert decode(blob, other) is None


def test_policy_without_digest_decodes_to_none() -> None:
    """An older policy carrying no digest can never bind a parsed blob."""
    blob, _ = _sample_blob()
    policy = CachePolicy(fetched_at=0, max_age=0, etag=None, body_digest=None)
    assert decode(blob, policy) is None


def test_format_mismatch_decodes_to_none() -> None:
    blob, policy = _retamper(0, 99)
    assert decode(blob, policy) is None


def test_codec_mismatch_decodes_to_none() -> None:
    blob, policy = _retamper(1, 99)
    assert decode(blob, policy) is None


def test_key_scheme_mismatch_decodes_to_none() -> None:
    blob, policy = _retamper(2, 7)
    assert decode(blob, policy) is None


def test_blob_carries_no_interpreter_tag() -> None:
    """The wire form is portable, so a blob written anywhere decodes here."""
    blob, policy = _sample_blob()
    header, _rows = json.loads(blob)
    *build, digest, zip_sdists, yanks, page_url = header

    assert all(isinstance(cell, int) for cell in build)
    assert isinstance(digest, str)
    assert zip_sdists == []
    assert yanks == {}
    assert page_url is None
    assert decode(blob, policy) is not None


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"not-json-bytes",
        b"\xff\xfe not utf-8",
        json.dumps(42).encode(),
        json.dumps([1, 2, 3]).encode(),
        json.dumps(["shorthdr", "rows"]).encode(),
    ],
)
def test_garbage_blob_decodes_to_none(blob: bytes) -> None:
    """Truncated, non-JSON, or wrong-shaped blobs are misses, not crashes."""
    _, policy = _sample_blob()
    assert decode(blob, policy) is None


def test_truncated_blob_decodes_to_none() -> None:
    """A valid blob cut short raises inside json and is treated as a miss."""
    blob, policy = _sample_blob()
    assert decode(blob[: len(blob) // 2], policy) is None


def test_malformed_body_never_reaches_encode() -> None:
    """A malformed Simple body raises at parse, so no parsed blob is written."""
    for body in (b'{"files": 5}', b"[]", b'"scalar"'):
        with pytest.raises(MalformedSimpleResponseError):
            _parse_files(json.loads(body), INDEX, "pkg")
