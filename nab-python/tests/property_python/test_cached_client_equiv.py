"""Observational equivalence: ``CachedAsyncSimpleClient`` vs ``AsyncSimpleClient``.

Over identical scripted responses, the cached client must return the same
listings as the uncached client; a warm cache must serve identical results
without any transport call; stale entries revalidated via 304 must equal
the cached parse, via 200 must equal the new body's parse.  PEP 658
metadata is immutable once verified; a hash mismatch must raise and must
not poison the cache.

Also fuzzes ``_parse_files`` over random PEP 691 JSON: drops yanked and
name-mismatched files, lowercases hex digests, never keeps a negative size.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, TypeVar

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index.cache import CachePolicy
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.client import (
    AsyncSimpleClient,
    MetadataHashMismatchError,
    _parse_files,
)

from .strategies import PROPERTY_SETTINGS

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

pytestmark = pytest.mark.property

_T = TypeVar("_T")

INDEX = "https://idx.example/simple/"


class FakeResponse:
    """Minimal ``HttpResponse`` stand-in over canned bytes."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status
        self.headers = headers
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"HTTP {self.status_code}"
            raise RuntimeError(msg)


class FakeTransport:
    """URL -> list of scripted (status, headers, body); replays the last forever."""

    def __init__(
        self, script: dict[str, list[tuple[int, dict[str, str], bytes]]]
    ) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> FakeResponse:
        self.calls.append((url, headers))
        responses = self.script[url]
        entry = responses.pop(0) if len(responses) > 1 else responses[0]
        return FakeResponse(*entry)

    async def aclose(self) -> None:
        pass


class DictCache:
    """In-memory ``CacheBackend`` whose state the tests can inspect."""

    def __init__(self) -> None:
        self.simple: dict[str, tuple[bytes, CachePolicy]] = {}
        self.metadata: dict[tuple[str, str], str] = {}
        self.sdist: dict[tuple[str, str], tuple[str | None, str | None]] = {}
        self.negative: dict[str, CachePolicy] = {}

    def get_simple(self, package: str) -> tuple[bytes, CachePolicy] | None:
        return self.simple.get(package)

    def put_simple(self, package: str, body: bytes, policy: CachePolicy) -> None:
        self.simple[package] = (body, policy)

    def refresh_simple_policy(self, package: str, policy: CachePolicy) -> None:
        body, _ = self.simple[package]
        self.simple[package] = (body, policy)

    def get_metadata(self, package: str, metadata_url: str) -> str | None:
        return self.metadata.get((package, metadata_url))

    def put_metadata(self, package: str, metadata_url: str, text: str) -> None:
        self.metadata[(package, metadata_url)] = text

    def get_sdist_files(
        self, package: str, version: str
    ) -> tuple[str | None, str | None] | None:
        return self.sdist.get((package, version))

    def put_sdist_files(
        self,
        package: str,
        version: str,
        pkg_info: str | None,
        pyproject_toml: str | None,
    ) -> None:
        self.sdist[(package, version)] = (pkg_info, pyproject_toml)

    def get_negative(self, package: str) -> CachePolicy | None:
        return self.negative.get(package)

    def put_negative(self, package: str, policy: CachePolicy) -> None:
        self.negative[package] = policy

    def drop_negative(self, package: str) -> None:
        self.negative.pop(package, None)


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    """Synchronously run an awaitable inside a property test."""
    return asyncio.run(coro)


pkg_names = st.from_regex(r"[a-z][a-z0-9]{0,5}", fullmatch=True)
versions = st.sampled_from(["1.0", "2.0.0", "0.1.dev1", "1!1.0", "1.0+local", "3"])
hex_chars = "0123456789abcdefABCDEF"


@st.composite
def file_entries(draw: st.DrawFn, package: str) -> dict[str, Any]:
    """One PEP 691 file entry: wheel, sdist, or junk, with optional fields."""
    kind = draw(st.integers(min_value=0, max_value=4))
    version = draw(versions)
    name = package if kind < 3 else draw(pkg_names)
    if kind in (0, 3):
        filename = f"{name}-{version}-py3-none-any.whl"
    elif kind in (1, 4):
        filename = f"{name}-{version}.tar.gz"
    else:
        filename = draw(st.text(max_size=20)) + draw(
            st.sampled_from([".whl", ".tar.gz", ".txt"])
        )
    entry: dict[str, Any] = {
        "filename": filename,
        "url": draw(
            st.sampled_from(
                [f"https://files.example/{filename}", filename, f"../pool/{filename}"]
            )
        ),
    }
    if draw(st.booleans()):
        entry["yanked"] = draw(st.sampled_from([True, False, "reason", ""]))
    if draw(st.booleans()):
        entry["hashes"] = {
            "sha256": draw(st.text(alphabet=hex_chars, min_size=4, max_size=16))
        }
    if draw(st.booleans()):
        entry["requires-python"] = ">=3.9"
    if draw(st.booleans()):
        entry["core-metadata"] = draw(st.sampled_from([True, False, {"sha256": "ab"}]))
    if draw(st.booleans()):
        entry["size"] = draw(st.integers(min_value=-5, max_value=10**6))
    return entry


@st.composite
def listings(draw: st.DrawFn) -> tuple[str, bytes]:
    """A package name plus a PEP 691 project-page body for it."""
    package = draw(pkg_names)
    entries = draw(st.lists(file_entries(package), max_size=6))
    body = json.dumps({"name": package, "files": entries}).encode()
    return package, body


@given(data=listings())
@PROPERTY_SETTINGS
def test_cached_equals_uncached_first_call(data: tuple[str, bytes]) -> None:
    """A cold cached client returns exactly what the plain client returns."""
    package, body = data
    url = f"{INDEX}{package}/"
    headers = {"Cache-Control": "max-age=10000", "ETag": '"e1"'}

    plain = AsyncSimpleClient(FakeTransport({url: [(200, headers, body)]}), INDEX)
    transport = FakeTransport({url: [(200, headers, body)]})
    cached = CachedAsyncSimpleClient(transport, DictCache(), INDEX)

    assert run(cached.get_files(package)) == run(plain.get_files(package))


@given(data=listings())
@PROPERTY_SETTINGS
def test_warm_fresh_cache_serves_identically_without_network(
    data: tuple[str, bytes],
) -> None:
    """A fresh cache entry is served as-is with no transport call."""
    package, body = data
    url = f"{INDEX}{package}/"
    transport = FakeTransport({url: [(200, {"Cache-Control": "max-age=10000"}, body)]})
    cached = CachedAsyncSimpleClient(transport, DictCache(), INDEX)
    first = run(cached.get_files(package))
    n_calls = len(transport.calls)
    second = run(cached.get_files(package))
    assert second == first
    assert len(transport.calls) == n_calls


@given(data=listings())
@PROPERTY_SETTINGS
def test_stale_304_revalidation_equals_cached_parse(data: tuple[str, bytes]) -> None:
    """A 304 revalidation reuses the cached body and refreshes the policy."""
    package, body = data
    url = f"{INDEX}{package}/"
    transport = FakeTransport(
        {
            url: [
                (200, {"Cache-Control": "max-age=0", "ETag": '"e1"'}, body),
                (304, {"Cache-Control": "max-age=10000"}, b""),
            ]
        }
    )
    cached = CachedAsyncSimpleClient(transport, DictCache(), INDEX)
    first = run(cached.get_files(package))
    second = run(cached.get_files(package))
    assert second == first
    # Conditional request carried the stored ETag.
    assert transport.calls[-1][1] is not None
    assert transport.calls[-1][1].get("If-None-Match") == '"e1"'
    # 304 refreshed the policy: a third call is served from cache.
    n_calls = len(transport.calls)
    third = run(cached.get_files(package))
    assert third == first
    assert len(transport.calls) == n_calls


@given(data=listings(), data2=listings())
@PROPERTY_SETTINGS
def test_stale_200_revalidation_serves_new_body(
    data: tuple[str, bytes], data2: tuple[str, bytes]
) -> None:
    """A 200 revalidation replaces the cached body and re-caches as fresh."""
    package, body = data
    _, body2 = data2
    body2 = json.dumps({**json.loads(body2), "name": package}).encode()
    url = f"{INDEX}{package}/"
    transport = FakeTransport(
        {
            url: [
                (200, {"Cache-Control": "max-age=0"}, body),
                (200, {"Cache-Control": "max-age=10000"}, body2),
            ]
        }
    )
    cached = CachedAsyncSimpleClient(transport, DictCache(), INDEX)
    run(cached.get_files(package))
    second = run(cached.get_files(package))
    assert second == _parse_files(json.loads(body2), INDEX, package)
    # New body cached as fresh: third call hits cache and equals it.
    n_calls = len(transport.calls)
    assert run(cached.get_files(package)) == second
    assert len(transport.calls) == n_calls


@given(
    package=pkg_names,
    version=versions,
    text=st.text(max_size=50),
    digest_ok=st.booleans(),
)
@PROPERTY_SETTINGS
def test_metadata_hash_gate_and_immutability(
    package: str, version: str, text: str, digest_ok: bool
) -> None:
    """A verified sidecar is cached forever; a mismatch raises and caches nothing."""
    body = text.encode()
    good = hashlib.sha256(body).hexdigest()
    bad = ("0" * 64) if good[0] != "0" else ("1" * 64)
    murl = "https://files.example/m.metadata"
    transport = FakeTransport({murl: [(200, {}, body)]})
    cache = DictCache()
    client = CachedAsyncSimpleClient(transport, cache, INDEX)

    if digest_ok:
        out = run(client.get_metadata_text(package, version, murl, ("sha256", good)))
        assert out == text
        # Immutable: second call with a now-different server is cache-served.
        transport.script[murl] = [(200, {}, b"CHANGED")]
        assert run(client.get_metadata_text(package, version, murl, None)) == text
    else:
        with pytest.raises(MetadataHashMismatchError):
            run(client.get_metadata_text(package, version, murl, ("sha256", bad)))
        # Mismatch must not poison the cache.
        assert cache.get_metadata(package, murl) is None
        out = run(client.get_metadata_text(package, version, murl, ("sha256", good)))
        assert out == text


@given(data=listings())
@PROPERTY_SETTINGS
def test_parse_files_postconditions(data: tuple[str, bytes]) -> None:
    """Yanked and name-mismatched files are dropped; digests and sizes sane."""
    package, body = data
    payload = json.loads(body)
    files = _parse_files(payload, INDEX, package)
    for f in files:
        candidates = [e for e in payload["files"] if e["filename"] == f.filename]
        assert any(not e.get("yanked") for e in candidates), "yanked file leaked"
        # Name always matches the queried package (pkg_names are canonical).
        assert f.filename.startswith((package, package.replace("-", "_")))
        for _algo, digest in f.hashes:
            assert digest == digest.lower()
        if f.size is not None:
            assert f.size >= 0
