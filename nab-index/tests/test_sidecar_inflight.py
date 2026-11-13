"""Exercise digest sharing while an actual response boundary is held open."""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

import pytest
import urllib3

from nab_index.cache import NullCache, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.urllib3_async_transport import _Urllib3Response
from nab_provider.errors import (
    HttpError,
    MalformedSimpleResponseError,
    MetadataHashMismatchError,
)

if TYPE_CHECKING:
    from pathlib import Path

_INDEX = "https://example.invalid/simple/"
_FIRST = "https://example.invalid/demo-1.0-cp312-cp312-linux_x86_64.whl.metadata"
_SECOND = "https://example.invalid/demo-1.0-cp313-cp313-linux_x86_64.whl.metadata"
_THIRD = "https://example.invalid/demo-1.0-cp314-cp314-linux_x86_64.whl.metadata"
_BODY = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n\n"
_SHA256 = "sha256", hashlib.sha256(_BODY).hexdigest()


class HeldSidecarTransport:
    """Hold the first URL until the test releases its real byte response."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.statuses: dict[str, int] = {}
        self.requests: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _Urllib3Response:
        self.requests.append(url)
        if url == _FIRST:
            self.entered.set()
            await self.release.wait()
        body = self.bodies[url]
        status = self.statuses.get(url, 200)
        return _Urllib3Response(
            urllib3.HTTPResponse(body=body, status=status), body, url
        )

    async def aclose(self) -> None:
        self.closed = True


class FailingWriteCache(OnDiskCache):
    """Fail one selected URL write before allowing ordinary disk persistence."""

    def __init__(self, root: Path, *, fail_url: str = _FIRST) -> None:
        super().__init__(root, _INDEX)
        self.fail_first = True
        self.fail_url = fail_url

    def put_metadata(self, package: str, url: str, text: str) -> None:
        if self.fail_first and url == self.fail_url:
            self.fail_first = False
            raise OSError("sidecar write failed")
        super().put_metadata(package, url, text)


async def entered_read(
    client: CachedAsyncSimpleClient,
    entered: asyncio.Event,
    url: str,
    digest: tuple[str, str] | None = _SHA256,
    *,
    package: str = "demo",
    version: str = "1.0",
) -> str:
    """Signal before the read reaches its first await, so callers can hold peers."""
    entered.set()
    return await client.get_metadata_text(package, version, url, digest)


@pytest.mark.parametrize("backend", ["disk", "disabled"])
@pytest.mark.parametrize("body", [_BODY, b""])
@pytest.mark.parametrize("algorithm", ["sha256", "sha384", "sha512"])
def test_distinct_urls_share_only_one_held_fetch(
    tmp_path: Path, backend: str, body: bytes, algorithm: str
) -> None:
    async def read() -> None:
        cache = OnDiskCache(tmp_path, _INDEX) if backend == "disk" else NullCache()
        transport = HeldSidecarTransport({_FIRST: body, _SECOND: body})
        digest = algorithm, hashlib.new(algorithm, body).hexdigest()
        async with CachedAsyncSimpleClient(transport, cache, _INDEX) as client:
            leader = asyncio.create_task(
                client.get_metadata_text("demo", "1.0", _FIRST, digest)
            )
            await transport.entered.wait()
            entered = asyncio.Event()
            follower = asyncio.create_task(
                entered_read(client, entered, _SECOND, digest)
            )
            await entered.wait()
            assert not leader.done()
            assert transport.requests == [_FIRST]
            assert not follower.done()

            transport.release.set()
            assert await asyncio.gather(leader, follower) == [
                body.decode(),
                body.decode(),
            ]
            assert not client._metadata_pending
            if backend == "disk":
                assert cache.get_metadata("demo", _FIRST) == body.decode()
                assert cache.get_metadata("demo", _SECOND) == body.decode()

        assert transport.requests == [_FIRST]

    asyncio.run(asyncio.wait_for(read(), timeout=5))


@pytest.mark.parametrize(
    "difference",
    ["package", "version", "digest", "client"],
)
def test_unrelated_requests_do_not_wait_for_the_held_fetch(difference: str) -> None:
    async def read() -> None:
        other = b"other body"
        transport = HeldSidecarTransport({_FIRST: _BODY, _SECOND: other})
        async with CachedAsyncSimpleClient(transport, NullCache(), _INDEX) as client:
            leader = asyncio.create_task(
                client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            )
            await transport.entered.wait()
            package = "other" if difference == "package" else "demo"
            version = "2.0" if difference == "version" else "1.0"
            body = other if difference == "digest" else _BODY
            transport.bodies[_SECOND] = body
            digest = "sha256", hashlib.sha256(body).hexdigest()
            peer_client = client
            other_client = CachedAsyncSimpleClient(transport, NullCache(), _INDEX)
            if difference == "client":
                peer_client = other_client
            try:
                assert (
                    await peer_client.get_metadata_text(
                        package, version, _SECOND, digest
                    )
                    == body.decode()
                )
                assert not leader.done()
                assert transport.requests == [_FIRST, _SECOND]
            finally:
                transport.release.set()
                await leader
                await other_client.aclose()

    asyncio.run(asyncio.wait_for(read(), timeout=5))


@pytest.mark.parametrize("failure", ["hash", "http", "utf8", "store"])
def test_leader_failure_retries_the_followers_own_url(
    tmp_path: Path, failure: str
) -> None:
    async def read() -> None:
        body = b"\xff" if failure == "utf8" else _BODY
        digest = "sha256", hashlib.sha256(body).hexdigest()
        leader_body = b"different" if failure == "hash" else body
        transport = HeldSidecarTransport({_FIRST: leader_body, _SECOND: _BODY})
        if failure == "http":
            transport.statuses[_FIRST] = 500
        cache = (
            FailingWriteCache(tmp_path)
            if failure == "store"
            else OnDiskCache(tmp_path, _INDEX)
        )
        expected_error = {
            "hash": MetadataHashMismatchError,
            "http": HttpError,
            "utf8": MalformedSimpleResponseError,
            "store": OSError,
        }[failure]
        async with CachedAsyncSimpleClient(transport, cache, _INDEX) as client:
            leader = asyncio.create_task(
                client.get_metadata_text("demo", "1.0", _FIRST, digest)
            )
            await transport.entered.wait()
            entered = asyncio.Event()
            follower = asyncio.create_task(
                entered_read(client, entered, _SECOND, digest)
            )
            await entered.wait()
            transport.release.set()
            with pytest.raises(expected_error):
                await leader
            if failure == "utf8":
                # Valid UTF-8 bytes cannot match the invalid body's advertised digest.
                with pytest.raises(MetadataHashMismatchError):
                    await follower
                assert cache.get_metadata("demo", _SECOND) is None
                assert (
                    await client.get_metadata_text("demo", "1.0", _SECOND, _SHA256)
                    == _BODY.decode()
                )
            else:
                assert await follower == _BODY.decode()
                assert cache.get_metadata("demo", _SECOND) == _BODY.decode()
            assert cache.get_metadata("demo", _FIRST) is None
            assert not client._metadata_pending
            assert transport.requests[:2] == [_FIRST, _SECOND]

    asyncio.run(asyncio.wait_for(read(), timeout=5))


def test_cancelled_owner_does_not_cancel_the_followers_own_fetch(
    tmp_path: Path,
) -> None:
    async def read() -> None:
        cache = OnDiskCache(tmp_path, _INDEX)
        transport = HeldSidecarTransport({_FIRST: _BODY, _SECOND: _BODY})
        async with CachedAsyncSimpleClient(transport, cache, _INDEX) as client:
            leader = asyncio.create_task(
                client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            )
            await transport.entered.wait()
            entered = asyncio.Event()
            follower = asyncio.create_task(entered_read(client, entered, _SECOND))
            await entered.wait()
            leader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await leader
            assert await follower == _BODY.decode()
            assert transport.requests == [_FIRST, _SECOND]
            assert cache.get_metadata("demo", _FIRST) is None
            assert cache.get_metadata("demo", _SECOND) == _BODY.decode()
            assert not client._metadata_pending

    asyncio.run(asyncio.wait_for(read(), timeout=5))


def test_cancelled_waiter_does_not_cancel_other_reads(tmp_path: Path) -> None:
    async def read() -> None:
        cache = OnDiskCache(tmp_path, _INDEX)
        transport = HeldSidecarTransport({_FIRST: _BODY})
        async with CachedAsyncSimpleClient(transport, cache, _INDEX) as client:
            leader = asyncio.create_task(
                client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            )
            await transport.entered.wait()
            entered = asyncio.Event()
            cancelled = asyncio.create_task(entered_read(client, entered, _SECOND))
            await entered.wait()
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            entered_peer = asyncio.Event()
            useful = asyncio.create_task(entered_read(client, entered_peer, _THIRD))
            await entered_peer.wait()
            assert not leader.done()
            assert not useful.done()

            transport.release.set()
            assert await asyncio.gather(leader, useful) == [
                _BODY.decode(),
                _BODY.decode(),
            ]
            assert not client._metadata_pending
            assert cache.get_metadata("demo", _FIRST) == _BODY.decode()
            assert cache.get_metadata("demo", _THIRD) == _BODY.decode()
            assert cache.get_metadata("demo", _SECOND) is None
            assert (
                await client.get_metadata_text("demo", "1.0", _SECOND, _SHA256)
                == _BODY.decode()
            )
            assert transport.requests == [_FIRST]

    asyncio.run(asyncio.wait_for(read(), timeout=5))


def test_failure_then_a_new_owner_can_succeed() -> None:
    async def read() -> None:
        transport = HeldSidecarTransport({_FIRST: b"different", _SECOND: _BODY})
        async with CachedAsyncSimpleClient(transport, NullCache(), _INDEX) as client:
            transport.release.set()
            with pytest.raises(MetadataHashMismatchError):
                await client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            assert not client._metadata_pending
            transport.bodies[_FIRST] = _BODY
            assert (
                await client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
                == _BODY.decode()
            )
            assert (
                await client.get_metadata_text("demo", "1.0", _SECOND, _SHA256)
                == _BODY.decode()
            )
            assert transport.requests == [_FIRST, _FIRST]
            assert not client._metadata_pending

    asyncio.run(asyncio.wait_for(read(), timeout=5))


def test_close_releases_waiters_and_does_not_retain_a_late_body() -> None:
    async def read() -> None:
        transport = HeldSidecarTransport({_FIRST: _BODY})
        client = CachedAsyncSimpleClient(transport, NullCache(), _INDEX)
        leader = asyncio.create_task(
            client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
        )
        await transport.entered.wait()
        entered = asyncio.Event()
        follower = asyncio.create_task(entered_read(client, entered, _SECOND))
        await entered.wait()
        finished = asyncio.Event()
        follower.add_done_callback(lambda _task: finished.set())
        await client.aclose()
        await finished.wait()
        assert transport.closed
        with pytest.raises(asyncio.CancelledError):
            await follower
        assert not client._metadata_pending

        transport.release.set()
        await leader
        assert not client._metadata_pending
        assert not client._verified_sidecars

    asyncio.run(asyncio.wait_for(read(), timeout=5))


def test_follower_store_failure_does_not_poison_the_verified_owner(
    tmp_path: Path,
) -> None:
    async def read() -> None:
        cache = FailingWriteCache(tmp_path, fail_url=_SECOND)
        transport = HeldSidecarTransport({_FIRST: _BODY})
        async with CachedAsyncSimpleClient(transport, cache, _INDEX) as client:
            leader = asyncio.create_task(
                client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            )
            await transport.entered.wait()
            entered = asyncio.Event()
            follower = asyncio.create_task(entered_read(client, entered, _SECOND))
            await entered.wait()
            transport.release.set()
            assert await leader == _BODY.decode()
            with pytest.raises(OSError, match="sidecar write failed"):
                await follower
            assert cache.get_metadata("demo", _FIRST) == _BODY.decode()
            assert cache.get_metadata("demo", _SECOND) is None
            assert not client._metadata_pending

            assert (
                await client.get_metadata_text("demo", "1.0", _THIRD, _SHA256)
                == _BODY.decode()
            )
            assert (
                await client.get_metadata_text("demo", "1.0", _SECOND, _SHA256)
                == _BODY.decode()
            )
            assert cache.get_metadata("demo", _SECOND) == _BODY.decode()
            assert transport.requests == [_FIRST]

    asyncio.run(asyncio.wait_for(read(), timeout=5))


def test_closed_client_does_not_create_new_shared_waiters(tmp_path: Path) -> None:
    async def read() -> None:
        cache = OnDiskCache(tmp_path, _INDEX)
        transport = HeldSidecarTransport({_FIRST: _BODY, _SECOND: _BODY})
        client = CachedAsyncSimpleClient(transport, cache, _INDEX)
        await client.aclose()
        leader = asyncio.create_task(
            client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
        )
        await transport.entered.wait()
        try:
            assert (
                await client.get_metadata_text("demo", "1.0", _SECOND, _SHA256)
                == _BODY.decode()
            )
            assert not leader.done()
            assert not client._metadata_pending
        finally:
            transport.release.set()
            await leader
        assert transport.requests == [_FIRST, _SECOND]
        assert cache.get_metadata("demo", _FIRST) == _BODY.decode()
        assert cache.get_metadata("demo", _SECOND) == _BODY.decode()
        assert not client._verified_sidecars

    asyncio.run(asyncio.wait_for(read(), timeout=5))


@pytest.mark.parametrize("algorithm", [None, "md5", "sha1"])
def test_unverified_peers_each_fetch_while_one_url_is_held(
    algorithm: str | None,
) -> None:
    async def read() -> None:
        transport = HeldSidecarTransport({_FIRST: _BODY, _SECOND: _BODY})
        digest = (
            None
            if algorithm is None
            else (algorithm, hashlib.new(algorithm, _BODY).hexdigest())
        )
        async with CachedAsyncSimpleClient(transport, NullCache(), _INDEX) as client:
            leader = asyncio.create_task(
                client.get_metadata_text("demo", "1.0", _FIRST, digest)
            )
            await transport.entered.wait()
            try:
                assert (
                    await client.get_metadata_text("demo", "1.0", _SECOND, digest)
                    == _BODY.decode()
                )
                assert not leader.done()
                assert transport.requests == [_FIRST, _SECOND]
            finally:
                transport.release.set()
                await leader
            assert not client._metadata_pending

    asyncio.run(asyncio.wait_for(read(), timeout=5))


def test_digest_algorithm_is_part_of_the_pending_identity() -> None:
    async def read() -> None:
        transport = HeldSidecarTransport({_FIRST: _BODY, _SECOND: _BODY})
        async with CachedAsyncSimpleClient(transport, NullCache(), _INDEX) as client:
            leader = asyncio.create_task(
                client.get_metadata_text("demo", "1.0", _FIRST, _SHA256)
            )
            await transport.entered.wait()
            try:
                with pytest.raises(MetadataHashMismatchError):
                    await client.get_metadata_text(
                        "demo", "1.0", _SECOND, ("sha512", _SHA256[1])
                    )
                assert not leader.done()
                assert transport.requests == [_FIRST, _SECOND]
            finally:
                transport.release.set()
                await leader
            assert not client._metadata_pending

    asyncio.run(asyncio.wait_for(read(), timeout=5))
