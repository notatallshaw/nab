"""Check generated PEP 691 file records through the parsed listing codec."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ..listing_roundtrip import assert_listing_roundtrip
from .settings import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

pkg_names = st.from_regex(r"[a-z][a-z0-9]{0,5}", fullmatch=True)
versions = st.sampled_from(
    ["1.0", "2.0.0", "0.1.dev1", "1!1.0", "1.0+local", "3", "10.0.0", "2.0"]
)
hex_digests = st.text(alphabet="0123456789abcdefABCDEF", min_size=0, max_size=16)


@st.composite
def _hash_table(draw: st.DrawFn) -> object:
    """Generate valid and malformed values for a file entry's hashes field."""
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
        # Non-string digest is dropped by parse_hash_table; exercise that path.
        return {"sha256": draw(st.integers())}
    return draw(st.sampled_from([[], "notadict", 5]))


@st.composite
def _metadata_field(draw: st.DrawFn) -> dict[str, object]:
    """Draw the PEP 658/714 metadata keys, including precedence collisions."""
    out: dict[str, object] = {}
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
def _file_entry(draw: st.DrawFn, package: str) -> object:
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

    entry: dict[str, object] = {"filename": filename}
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
            st.sampled_from([">=3.9", ">=3.7,<4", ">=3.9\ud800", "", 3.9, 3, None])
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


@given(data=pep691_bodies())
@PROPERTY_SETTINGS
def test_roundtrip_generated(data: tuple[str, bytes]) -> None:
    """decode(encode(_parse_files(body))) == _parse_files(body), exactly."""
    package, body = data
    assert_listing_roundtrip(package, body)
