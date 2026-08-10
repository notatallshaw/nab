"""Exact-equivalence harness for the ``parsed_listing`` codec.

The parsed-listing cache stores the post-``_parse_files`` records so a warm
resolve skips ``json.loads`` + filename parsing.  The only correctness
contract is that materialising the stored form yields the *exact* list
``_parse_files`` returned: same records in the same order, every field equal
in value and type, and string identity preserved for the interned
``requires_python`` and hash-algorithm values (the dedup the wire parse
builds must survive the round trip).

This harness discharges that contract two ways:

* a Hypothesis strategy generating PEP 691 bodies across the parse surface
  (wheels, sdists, name-mismatch phantoms, yanked bool/string, non-string
  ``requires-python``, negative/bool/string ``size``, PEP 714 key
  precedence, empty and multi hash, relative and absolute URLs), and
* a checked-in trimmed real corpus (boto3, botocore, cryptography, and one
  large AI-stack body, torch).

Plus the miss semantics: a body-digest mismatch, a header field mismatch
(format / codec / key_scheme), and a truncated or garbage blob all decode to
``None`` so the read path rebuilds from the raw body.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index.cache import CachePolicy
from nab_index.client import (
    MalformedSimpleResponseError,
    SdistFile,
    WheelFile,
    _parse_files,
)
from nab_index.parsed_listing import decode, encode

from .strategies import PROPERTY_SETTINGS

if TYPE_CHECKING:
    from collections.abc import Iterable

pytestmark = pytest.mark.property

INDEX = "https://pypi.org/simple/"
CORPUS_DIR = Path(__file__).parent / "corpus_parsed_listing"

pkg_names = st.from_regex(r"[a-z][a-z0-9]{0,5}", fullmatch=True)
versions = st.sampled_from(
    ["1.0", "2.0.0", "0.1.dev1", "1!1.0", "1.0+local", "3", "10.0.0", "2.0"]
)
hex_digests = st.text(alphabet="0123456789abcdefABCDEF", min_size=0, max_size=16)


def _digest_of(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _policy_for(body: bytes) -> CachePolicy:
    """A policy whose ``body_digest`` binds the parsed blob to ``body``."""
    return CachePolicy(fetched_at=0, max_age=0, etag=None, body_digest=_digest_of(body))


@st.composite
def _hash_table(draw: st.DrawFn) -> Any:
    """A ``hashes`` value: absent-shaped, empty, single, multi, or non-dict."""
    kind = draw(st.integers(min_value=0, max_value=4))
    if kind == 0:
        return {}
    if kind == 1:
        return {"sha256": draw(hex_digests)}
    if kind == 2:
        return {
            "sha256": draw(hex_digests),
            "sha384": draw(hex_digests),
            "md5": draw(hex_digests),
        }
    if kind == 3:
        # Non-string digest is dropped by _parse_hashes; exercise that path.
        return {"sha256": draw(st.integers())}
    return draw(st.sampled_from([[], "notadict", 5]))


@st.composite
def _metadata_field(draw: st.DrawFn) -> dict[str, Any]:
    """Draw the PEP 658/714 metadata keys, including precedence collisions."""
    out: dict[str, Any] = {}
    core = draw(
        st.sampled_from(
            [None, True, False, {"sha256": draw(hex_digests)}, {"sha512": "ab"}, {}]
        )
    )
    legacy = draw(st.sampled_from([None, True, False, {"sha256": draw(hex_digests)}]))
    if core is not None:
        out["core-metadata"] = core
    if legacy is not None:
        out["dist-info-metadata"] = legacy
    return out


@st.composite
def _file_entry(draw: st.DrawFn, package: str) -> Any:
    """One PEP 691 file entry, or a non-dict junk entry to be skipped."""
    if draw(st.integers(min_value=0, max_value=12)) == 0:
        return draw(st.sampled_from([None, "junk", 42, []]))

    kind = draw(st.integers(min_value=0, max_value=5))
    version = draw(versions)
    # kinds 4/5 use a different name to exercise the name-mismatch drop,
    # including the legacy embedded-build-tag sdist phantom.
    name = package if kind < 4 else draw(pkg_names)
    if kind in (0, 4):
        filename = f"{name}-{version}-py3-none-any.whl"
    elif kind in (1, 5):
        filename = f"{name}-{version}.tar.gz"
    elif kind == 2:
        filename = f"{name}-{version}-2.tar.gz"
    else:
        filename = draw(st.text(max_size=16)) + draw(
            st.sampled_from([".whl", ".tar.gz", ".zip", ".txt"])
        )

    entry: dict[str, Any] = {"filename": filename}
    url_choice = draw(st.integers(min_value=0, max_value=3))
    if url_choice == 0:
        entry["url"] = f"https://files.example/{filename}"
    elif url_choice == 1:
        entry["url"] = f"http://files.example/{filename}"
    elif url_choice == 2:
        entry["url"] = f"../pool/{filename}"
    else:
        entry["url"] = filename

    if draw(st.booleans()):
        entry["yanked"] = draw(st.sampled_from([True, False, "reason", ""]))
    if draw(st.booleans()):
        entry["hashes"] = draw(_hash_table())
    if draw(st.booleans()):
        entry["requires-python"] = draw(
            st.sampled_from([">=3.9", ">=3.7,<4", "", 3.9, 3, None])
        )
    if draw(st.booleans()):
        entry["upload-time"] = draw(
            st.sampled_from(["2023-01-01T00:00:00Z", "not-a-time", 1234, None])
        )
    if draw(st.booleans()):
        entry["size"] = draw(st.sampled_from([0, 1024, -5, True, "big", 2**40]))
    entry.update(draw(_metadata_field()))
    # Occasionally drop the required url to hit the missing-url skip.
    if draw(st.integers(min_value=0, max_value=10)) == 0:
        del entry["url"]
    return entry


@st.composite
def pep691_bodies(draw: st.DrawFn) -> tuple[str, bytes]:
    """A package name plus a valid-JSON PEP 691 project-page body for it."""
    package = draw(pkg_names)
    entries = draw(st.lists(_file_entry(package), max_size=8))
    body = json.dumps({"name": package, "files": entries}).encode()
    return package, body


def _assert_record_equal(got: object, want: object) -> None:
    """Every field equal in value and type; interned fields equal by identity."""
    assert type(got) is type(want)
    assert isinstance(got, (WheelFile, SdistFile))
    assert isinstance(want, (WheelFile, SdistFile))
    for field in dataclasses.fields(got):
        g = getattr(got, field.name)
        w = getattr(want, field.name)
        assert g == w, field.name
        assert type(g) is type(w), field.name
    # local_path is never carried through the parsed form.
    assert got.local_path is None
    # requires_python is interned on both paths, so identity must hold.
    if want.requires_python is not None:
        assert got.requires_python is want.requires_python
    # Hash algorithm names are interned; digests are not.
    for (g_algo, _g_dig), (w_algo, _w_dig) in zip(got.hashes, want.hashes, strict=True):
        assert g_algo is w_algo
    if isinstance(got, WheelFile):
        assert isinstance(want, WheelFile)
        # metadata_url is a derived property; it must still match.
        assert got.metadata_url == want.metadata_url


def _assert_roundtrip(package: str, body: bytes) -> None:
    digest = _digest_of(body)
    parsed = _parse_files(json.loads(body), INDEX, package)
    policy = CachePolicy(fetched_at=0, max_age=0, etag=None, body_digest=digest)
    blob = encode(parsed, digest)
    decoded = decode(blob, policy)
    assert decoded is not None
    assert len(decoded) == len(parsed)
    for got, want in zip(decoded, parsed, strict=True):
        _assert_record_equal(got, want)


@given(data=pep691_bodies())
@PROPERTY_SETTINGS
def test_roundtrip_generated(data: tuple[str, bytes]) -> None:
    """decode(encode(_parse_files(body))) == _parse_files(body), exactly."""
    package, body = data
    _assert_roundtrip(package, body)


@pytest.mark.parametrize("package", ["boto3", "botocore", "cryptography", "torch"])
def test_roundtrip_corpus(package: str) -> None:
    """The trimmed real corpus round-trips element-for-element."""
    body = (CORPUS_DIR / f"{package}.json").read_bytes()
    _assert_roundtrip(package, body)


def test_empty_listing_roundtrips() -> None:
    """An empty file list encodes and decodes to an empty list."""
    body = json.dumps({"name": "nothing", "files": []}).encode()
    _assert_roundtrip("nothing", body)


def _sample_blob() -> tuple[bytes, CachePolicy]:
    body = (CORPUS_DIR / "boto3.json").read_bytes()
    digest = _digest_of(body)
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
    assert not any(isinstance(field, list) for field in header)
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


def _iter_corpus() -> Iterable[Path]:
    return sorted(CORPUS_DIR.glob("*.json"))


def test_corpus_present() -> None:
    """The checked-in corpus is non-empty (guards an accidental deletion)."""
    assert list(_iter_corpus())
