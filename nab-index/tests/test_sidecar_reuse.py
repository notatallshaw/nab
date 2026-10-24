"""Reuse only metadata already verified for the same coordinate and digest."""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

import pytest
import urllib3

from nab_index.cache import NullCache, OfflineError, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.urllib3_async_transport import _Urllib3Response
from nab_provider.errors import MetadataHashMismatchError

if TYPE_CHECKING:
    from pathlib import Path

_INDEX = "https://example.invalid/simple/"
_FIRST = "https://example.invalid/demo-1.0-cp312-cp312-linux_x86_64.whl.metadata"
_SECOND = "https://example.invalid/demo-1.0-cp313-cp313-linux_x86_64.whl.metadata"
_BODY = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n\n"
_SHA256 = ("sha256", hashlib.sha256(_BODY).hexdigest())


class SidecarTransport:
    """Serve known byte payloads while recording actual sidecar requests."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.requests: list[str] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _Urllib3Response:
        self.requests.append(url)
        body = self.bodies[url]
        return _Urllib3Response(urllib3.HTTPResponse(body=body, status=200), body, url)

    async def aclose(self) -> None:
        pass


@pytest.mark.parametrize("backend", ["disk", "disabled"])
@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512"])
def test_equal_digest_avoids_a_second_request(
    tmp_path: Path, backend: str, algorithm: str
) -> None:
    cache = OnDiskCache(tmp_path, _INDEX) if backend == "disk" else NullCache()
    transport = SidecarTransport({_FIRST: _BODY})
    client = CachedAsyncSimpleClient(transport, cache, _INDEX)
    digest = algorithm, hashlib.new(algorithm, _BODY).hexdigest()

    async def read() -> None:
        async with client:
            for url in (_FIRST, _SECOND):
                assert (
                    await client.get_metadata_text("demo", "1.0", url, digest)
                    == _BODY.decode()
                )

    asyncio.run(read())

    assert transport.requests == [_FIRST]

    if backend == "disk":
        assert cache.get_metadata("demo", _SECOND) == _BODY.decode()


@pytest.mark.parametrize("coordinate", [("other", "1.0"), ("demo", "2.0")])
def test_different_coordinates_fetch_separately(coordinate: tuple[str, str]) -> None:
    transport = SidecarTransport({_FIRST: _BODY, _SECOND: _BODY})
    client = CachedAsyncSimpleClient(transport, NullCache(), _INDEX)

    async def read() -> None:
        async with client:
            await client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            await client.get_metadata_text(*coordinate, _SECOND, _SHA256)

    asyncio.run(read())

    assert transport.requests == [_FIRST, _SECOND]


def test_different_metadata_digests_do_not_share() -> None:
    other = _BODY.replace(b"\n\n", b"\nRequires-Dist: dependency\n\n")
    transport = SidecarTransport({_FIRST: _BODY, _SECOND: other})
    client = CachedAsyncSimpleClient(transport, NullCache(), _INDEX)

    async def read() -> None:
        async with client:
            await client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            result = await client.get_metadata_text(
                "demo", "1.0", _SECOND, ("sha256", hashlib.sha256(other).hexdigest())
            )
            assert result == other.decode()

    asyncio.run(read())

    assert transport.requests == [_FIRST, _SECOND]


@pytest.mark.parametrize("algorithm", [None, "md5", "sha1"])
def test_missing_or_weak_hashes_fetch_each_url(algorithm: str | None) -> None:
    transport = SidecarTransport({_FIRST: _BODY, _SECOND: _BODY})
    client = CachedAsyncSimpleClient(transport, NullCache(), _INDEX)
    digest = (
        (algorithm, hashlib.new(algorithm, _BODY).hexdigest()) if algorithm else None
    )

    async def read() -> None:
        async with client:
            for url in (_FIRST, _SECOND):
                assert (
                    await client.get_metadata_text("demo", "1.0", url, digest)
                    == _BODY.decode()
                )

    asyncio.run(read())

    assert transport.requests == [_FIRST, _SECOND]


def test_failed_verification_cannot_supply_another_url() -> None:
    transport = SidecarTransport({_FIRST: b"different", _SECOND: _BODY})
    client = CachedAsyncSimpleClient(transport, NullCache(), _INDEX)

    async def read() -> None:
        async with client:
            with pytest.raises(MetadataHashMismatchError):
                await client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            assert (
                await client.get_metadata_text("demo", "1.0", _SECOND, _SHA256)
                == _BODY.decode()
            )

    asyncio.run(read())

    assert transport.requests == [_FIRST, _SECOND]


def test_unverified_cached_text_does_not_leak_to_another_url(tmp_path: Path) -> None:
    cache = OnDiskCache(tmp_path, _INDEX)
    cache.put_metadata("demo", _FIRST, "older cached bytes")
    transport = SidecarTransport({_SECOND: _BODY})
    client = CachedAsyncSimpleClient(transport, cache, _INDEX)

    async def read() -> None:
        async with client:
            assert (
                await client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
                == "older cached bytes"
            )
            assert (
                await client.get_metadata_text("demo", "1.0", _SECOND, _SHA256)
                == _BODY.decode()
            )

    asyncio.run(read())

    assert transport.requests == [_SECOND]


def test_an_offline_missing_url_remains_a_miss(tmp_path: Path) -> None:
    cache = OnDiskCache(tmp_path, _INDEX)
    cache.put_metadata("demo", _FIRST, _BODY.decode())
    transport = SidecarTransport({})
    client = CachedAsyncSimpleClient(transport, cache, _INDEX, offline=True)

    async def read() -> None:
        async with client:
            with pytest.raises(OfflineError):
                await client.get_metadata_text("demo", "1.0", _SECOND, _SHA256)

    asyncio.run(read())

    assert transport.requests == []


def test_reuse_does_not_escape_the_client_lifetime() -> None:
    transport = SidecarTransport({_FIRST: _BODY, _SECOND: _BODY})

    async def read() -> None:
        for url in (_FIRST, _SECOND):
            async with CachedAsyncSimpleClient(
                transport, NullCache(), _INDEX
            ) as client:
                await client.get_metadata_text("demo", "1.0", url, _SHA256)

    asyncio.run(read())

    assert transport.requests == [_FIRST, _SECOND]


def test_variants_of_the_same_version_remain_reusable() -> None:
    third = "https://example.invalid/demo-1.0-cp314-cp314-linux_x86_64.whl.metadata"
    other = _BODY.replace(b"\n\n", b"\nRequires-Dist: dependency\n\n")
    transport = SidecarTransport({_FIRST: _BODY, _SECOND: other})
    client = CachedAsyncSimpleClient(transport, NullCache(), _INDEX)

    async def read() -> None:
        async with client:
            for url, body in ((_FIRST, _BODY), (_SECOND, other), (third, _BODY)):
                digest = "sha256", hashlib.sha256(body).hexdigest()
                assert (
                    await client.get_metadata_text("demo", "1.0", url, digest)
                    == body.decode()
                )

    asyncio.run(read())

    assert transport.requests == [_FIRST, _SECOND]


def test_close_releases_verified_bodies() -> None:
    client = CachedAsyncSimpleClient(
        SidecarTransport({_FIRST: _BODY}), NullCache(), _INDEX
    )

    async def read() -> None:
        async with client:
            await client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            assert client._verified_sidecars

    asyncio.run(read())

    assert not client._verified_sidecars


def test_a_new_version_replaces_previous_version_bodies() -> None:
    third = "https://example.invalid/demo-1.0-cp314-cp314-linux_x86_64.whl.metadata"
    other = _BODY.replace(b"Version: 1.0", b"Version: 2.0")
    transport = SidecarTransport({_FIRST: _BODY, _SECOND: other, third: _BODY})
    client = CachedAsyncSimpleClient(transport, NullCache(), _INDEX)

    async def read() -> None:
        async with client:
            for url, version, body in (
                (_FIRST, "1.0", _BODY),
                (_SECOND, "2.0", other),
                (third, "1.0", _BODY),
            ):
                digest = "sha256", hashlib.sha256(body).hexdigest()
                assert (
                    await client.get_metadata_text("demo", version, url, digest)
                    == body.decode()
                )

    asyncio.run(read())

    assert transport.requests == [_FIRST, _SECOND, third]


def test_concurrent_variants_merge_into_the_current_version() -> None:
    class YieldingTransport(SidecarTransport):
        async def get(
            self, url: str, *, headers: dict[str, str] | None = None
        ) -> _Urllib3Response:
            await asyncio.sleep(0)
            return await super().get(url, headers=headers)

    third = "https://example.invalid/demo-1.0-cp314-cp314-linux_x86_64.whl.metadata"
    other = _BODY.replace(b"\n\n", b"\nRequires-Dist: dependency\n\n")
    transport = YieldingTransport({_FIRST: _BODY, _SECOND: other})
    client = CachedAsyncSimpleClient(transport, NullCache(), _INDEX)

    async def read() -> None:
        async with client:
            await asyncio.gather(
                client.get_metadata_text("demo", "1.0", _FIRST, _SHA256),
                client.get_metadata_text(
                    "demo",
                    "1.0",
                    _SECOND,
                    ("sha256", hashlib.sha256(other).hexdigest()),
                ),
            )
            assert (
                await client.get_metadata_text("demo", "1.0", third, _SHA256)
                == _BODY.decode()
            )

    asyncio.run(read())

    assert transport.requests == [_FIRST, _SECOND]
