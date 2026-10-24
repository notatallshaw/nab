"""First-request transport creation through real cache and request paths."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from nab._resolve import _make_resolve_transport
from nab_index.cache import CachePolicy, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.httpx_async_transport import _HttpxResponse

if TYPE_CHECKING:
    from pathlib import Path

    from nab_index.transport import HttpResponse

_INDEX = "https://example.invalid/simple/"
_BODY = json.dumps(
    {
        "meta": {"api-version": "1.0"},
        "files": [
            {
                "filename": "foo-1.0-py3-none-any.whl",
                "url": "https://example.invalid/foo.whl",
                "hashes": {},
            }
        ],
    }
).encode()


class RecordingTransport:
    """Return one HTTP response while recording requests and resource closure."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = _HttpxResponse(response, response.content)
        self.requests: list[tuple[str, dict[str, str] | None]] = []
        self.closes = 0

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        self.requests.append((url, headers))
        await asyncio.sleep(0)
        return self.response

    async def aclose(self) -> None:
        self.closes += 1


@pytest.mark.parametrize("cache_state", ["fresh", "stale", "missing"])
def test_cache_state_controls_client_creation(tmp_path: Path, cache_state: str) -> None:
    cache = OnDiskCache(tmp_path, _INDEX)
    if cache_state != "missing":
        cache.put_simple(
            "foo",
            _BODY,
            CachePolicy(1000, 3600 if cache_state == "fresh" else 0, '"v1"'),
        )
    response = httpx.Response(
        304 if cache_state == "stale" else 200,
        content=b"" if cache_state == "stale" else _BODY,
        headers={
            "cache-control": "max-age=3600",
            "content-type": "application/vnd.pypi.simple.v1+json",
        },
        request=httpx.Request("GET", _INDEX + "foo/"),
    )
    inner = RecordingTransport(response)

    async def read_and_close() -> None:
        client = CachedAsyncSimpleClient(
            _make_resolve_transport("urllib3"), cache, _INDEX
        )
        async with client:
            files = await client.get_files("foo")
            assert [file.filename for file in files] == ["foo-1.0-py3-none-any.whl"]

    with (
        patch("nab._resolve._make_urllib3_transport", return_value=inner) as make,
        patch("nab_index.cache.time.time", return_value=1000),
    ):
        asyncio.run(read_and_close())

    if cache_state == "fresh":
        make.assert_not_called()
        assert inner.requests == []
        assert inner.closes == 0
    else:
        make.assert_called_once_with()
        assert len(inner.requests) == 1
        assert inner.closes == 1
        if cache_state == "stale":
            assert inner.requests[0][1] is not None
            assert inner.requests[0][1]["If-None-Match"] == '"v1"'


def test_concurrent_first_requests_share_one_client() -> None:
    inner = RecordingTransport(httpx.Response(200))

    async def fetch_and_close() -> None:
        transport = _make_resolve_transport("urllib3")
        await asyncio.gather(
            *(transport.get(f"{_INDEX}{name}/") for name in ("a", "b"))
        )
        await transport.aclose()

    with patch("nab._resolve._make_urllib3_transport", return_value=inner) as make:
        asyncio.run(fetch_and_close())

    make.assert_called_once_with()

    assert len(inner.requests) == 2
    assert inner.closes == 1


def test_request_failure_still_closes_created_client() -> None:
    class FailingTransport(RecordingTransport):
        async def get(
            self, url: str, *, headers: dict[str, str] | None = None
        ) -> HttpResponse:
            raise OSError("request failed")

    inner = FailingTransport(httpx.Response(200))

    async def fetch_and_close() -> None:
        transport = _make_resolve_transport("urllib3")
        try:
            await transport.get(_INDEX)
        finally:
            await transport.aclose()

    with (
        patch("nab._resolve._make_urllib3_transport", return_value=inner),
        pytest.raises(OSError, match="request failed"),
    ):
        asyncio.run(fetch_and_close())

    assert inner.closes == 1
