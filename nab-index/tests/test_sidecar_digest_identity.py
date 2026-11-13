"""Custom hash objects retain direct verification instead of shared identity."""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

import pytest
import urllib3

from nab_index.cache import NullCache, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.urllib3_async_transport import _Urllib3Response
from nab_provider.errors import MetadataHashMismatchError

if TYPE_CHECKING:
    from pathlib import Path


_INDEX = "https://example.invalid/simple/"
_FIRST = "https://example.invalid/demo-first.whl.metadata"
_SECOND = "https://example.invalid/demo-second.whl.metadata"
_BODY = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n\n"
_SHA256 = "sha256", hashlib.sha256(_BODY).hexdigest()


class TextSubclass(str):
    __slots__ = ()


class TupleSubclass(tuple[str, str]):
    __slots__ = ()


class IdentityTransport:
    """Hold the first real byte response while the other URL can complete."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _Urllib3Response:
        assert url in {_FIRST, _SECOND}
        self.requests.append(url)
        if url == _FIRST:
            self.entered.set()
            await self.release.wait()
        return _Urllib3Response(
            urllib3.HTTPResponse(body=_BODY, status=200), _BODY, url
        )

    async def aclose(self) -> None:
        pass


def _custom_hash(kind: str) -> tuple[str, str]:
    if kind == "tuple":
        return TupleSubclass(_SHA256)
    if kind == "algorithm":
        return TextSubclass(_SHA256[0]), _SHA256[1]
    if kind == "digest":
        return _SHA256[0], TextSubclass(_SHA256[1])
    assert kind == "builtin"
    return _SHA256


def _equal_text(value: str, alias: str) -> str:
    class EqualText(str):
        __slots__ = ()

        def __eq__(self, _other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return hash(alias)

    return EqualText(value)


def _wrong_hash(kind: str) -> tuple[str, str]:
    if kind == "algorithm":
        return _equal_text("sha512", "sha256"), _SHA256[1]
    if kind == "digest":
        return "sha256", _equal_text("0" * 64, _SHA256[1])

    class EqualTuple(tuple[str, str]):
        __slots__ = ()

        def __eq__(self, _other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return hash(_SHA256)

    assert kind == "tuple"
    return EqualTuple(("sha256", "0" * 64))


async def _entered_read(
    client: CachedAsyncSimpleClient,
    entered: asyncio.Event,
    digest: tuple[str, str],
) -> str:
    entered.set()
    return await client.get_metadata_text("demo", "1.0", _SECOND, digest)


@pytest.mark.parametrize("state", ["pending", "completed"])
@pytest.mark.parametrize("kind", ["builtin", "tuple", "algorithm", "digest"])
def test_only_builtin_hash_values_share_content(
    tmp_path: Path, kind: str, state: str
) -> None:
    async def read() -> None:
        cache = OnDiskCache(tmp_path, _INDEX)
        transport = IdentityTransport()
        digest = _custom_hash(kind)
        async with CachedAsyncSimpleClient(transport, cache, _INDEX) as client:
            leader = asyncio.create_task(
                client.get_metadata_text("demo", "1.0", _FIRST, digest)
            )
            await transport.entered.wait()
            if state == "completed":
                transport.release.set()
                assert await leader == _BODY.decode()
            entered = asyncio.Event()
            follower = asyncio.create_task(_entered_read(client, entered, digest))
            await entered.wait()
            transport.release.set()

            assert await asyncio.gather(leader, follower) == [_BODY.decode()] * 2
            assert transport.requests == (
                [_FIRST] if kind == "builtin" else [_FIRST, _SECOND]
            )
            assert cache.get_metadata("demo", _FIRST) == _BODY.decode()
            assert cache.get_metadata("demo", _SECOND) == _BODY.decode()
            assert not client._metadata_pending

    asyncio.run(asyncio.wait_for(read(), timeout=5))


@pytest.mark.parametrize("state", ["pending", "completed"])
@pytest.mark.parametrize("kind", ["tuple", "algorithm", "digest"])
def test_custom_equality_does_not_bypass_direct_hash_verification(
    tmp_path: Path, kind: str, state: str
) -> None:
    async def read() -> None:
        digest = _wrong_hash(kind)
        direct = IdentityTransport()
        async with CachedAsyncSimpleClient(direct, NullCache(), _INDEX) as client:
            with pytest.raises(MetadataHashMismatchError):
                await client.get_metadata_text("demo", "1.0", _SECOND, digest)
        assert direct.requests == [_SECOND]

        cache = OnDiskCache(tmp_path, _INDEX)
        transport = IdentityTransport()
        async with CachedAsyncSimpleClient(transport, cache, _INDEX) as client:
            leader = asyncio.create_task(
                client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            )
            await transport.entered.wait()
            if state == "completed":
                transport.release.set()
                assert await leader == _BODY.decode()
            entered = asyncio.Event()
            follower = asyncio.create_task(_entered_read(client, entered, digest))
            await entered.wait()
            transport.release.set()

            assert await leader == _BODY.decode()
            with pytest.raises(MetadataHashMismatchError):
                await follower
            assert transport.requests == [_FIRST, _SECOND]
            assert cache.get_metadata("demo", _FIRST) == _BODY.decode()
            assert cache.get_metadata("demo", _SECOND) is None
            assert not client._metadata_pending

    asyncio.run(asyncio.wait_for(read(), timeout=5))
