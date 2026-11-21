"""Compare parsed listing records with the files returned by the codec."""

import hashlib
import json

from nab_index.cache import CachePolicy
from nab_index.client import SdistFile, WheelFile, _parse_files
from nab_index.parsed_listing import decode, encode
from nab_provider.value import field_names

INDEX = "https://pypi.org/simple/"


def _assert_record_equal(got: object, want: object) -> None:
    """Every field equal in value and type; interned fields equal by identity."""
    assert type(got) is type(want)
    assert isinstance(got, (WheelFile, SdistFile))
    assert isinstance(want, (WheelFile, SdistFile))
    for field in field_names(got):
        g = getattr(got, field)
        w = getattr(want, field)
        assert g == w, field
        assert type(g) is type(w), field

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


def assert_listing_roundtrip(package: str, body: bytes) -> None:
    """Require the codec to preserve each parsed file and its metadata."""
    digest = hashlib.sha256(body).hexdigest()
    parsed = _parse_files(json.loads(body), INDEX, package)
    policy = CachePolicy(fetched_at=0, max_age=0, etag=None, body_digest=digest)
    blob = encode(parsed, digest)
    decoded = decode(blob, policy)
    assert decoded is not None
    assert len(decoded.files) == len(parsed)
    for got, want in zip(decoded.files, parsed, strict=True):
        _assert_record_equal(got, want)
