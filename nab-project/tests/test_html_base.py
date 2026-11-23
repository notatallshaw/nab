"""HTML base URLs retain their meaning through clients and cached listings."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import respx
from packaging.version import Version

import nab_index.cache as cache_mod
from nab_index._pep503 import read_page
from nab_index.cache import CachePolicy, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.client import AsyncSimpleClient, WheelFile
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator

_INDEX = "https://index.example/simple/"
_PAGE_URL = f"{_INDEX}demo/"
_WHEEL = "demo-1.0-py3-none-any.whl"
_WHEEL_URL = f"https://files.example/{_WHEEL}"
_METADATA_URL = f"{_WHEEL_URL}.metadata"
_METADATA = (
    "Metadata-Version: 2.3\nName: demo\nVersion: 1.0\nRequires-Dist: leaf>=2\n\n"
)


class _InlinePolicyCache(OnDiskCache):
    """Persist revalidation policies through the custom-backend sync path."""


def _page(base: str) -> str:
    """Return a Simple page with a relative wheel and metadata sidecar."""
    return (
        f'<base href="{base}"><a href="{_WHEEL}" data-core-metadata="true">{_WHEEL}</a>'
    )


@pytest.mark.parametrize("padding", [" ", "\t\n\f\r "])
@pytest.mark.parametrize("cached", [False, True], ids=["direct", "cached"])
@respx.mock
def test_padded_base_urls_survive_client_reads(
    tmp_path: Path, padding: str, cached: bool
) -> None:
    listing = respx.get(_PAGE_URL).respond(
        200,
        text=_page(f"{padding}https://files.example{padding}"),
        headers={"content-type": "text/html", "cache-control": "max-age=3600"},
    )
    cache = OnDiskCache(tmp_path, _INDEX)
    transport = HttpxAsyncTransport()
    client = (
        CachedAsyncSimpleClient(transport, cache, _INDEX)
        if cached
        else AsyncSimpleClient(transport, _INDEX)
    )

    async def go() -> None:
        async with client:
            (wheel,) = await client.get_files("demo")
            assert isinstance(wheel, WheelFile)
            assert wheel.url == _WHEEL_URL
            assert wheel.metadata_url == _METADATA_URL

        if cached:
            for offline in (False, True):
                async with CachedAsyncSimpleClient(
                    HttpxAsyncTransport(), cache, _INDEX, offline=offline
                ) as warm:
                    assert await warm.get_files("demo") == [wheel]

    asyncio.run(go())
    assert listing.call_count == 1


@pytest.mark.parametrize(
    ("first", "expected"),
    [
        (" \thttps://files.example\f ", "https://files.example"),
        (" \t\n\f\r ", ""),
        (" \u00a0https://files.example\u00a0 ", "\u00a0https://files.example\u00a0"),
    ],
    ids=["padded", "empty", "non-ascii"],
)
def test_first_base_trims_only_html_whitespace(first: str, expected: str) -> None:
    _, base = read_page(
        f'<base target="_blank"><base href><base href="{first}">'
        '<base href="https://other.example/">'
    )
    assert base == expected


@respx.mock
def test_provider_reads_dependencies_from_padded_base(tmp_path: Path) -> None:
    respx.get(_PAGE_URL).respond(
        200,
        text=_page(" \t\n\f\r https://files.example \t\n\f\r "),
        headers={"content-type": "text/html"},
    )

    def serve_metadata(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _METADATA_URL:
            return httpx.Response(200, text=_METADATA)
        return httpx.Response(404)

    metadata = respx.route().mock(side_effect=serve_metadata)

    async def go() -> None:
        async with CachedAsyncSimpleClient(
            HttpxAsyncTransport(), OnDiskCache(tmp_path, _INDEX), _INDEX
        ) as client:
            files = await client.get_files("demo")
            (wheel,) = files
            assert isinstance(wheel, WheelFile)
            metadata_url = wheel.metadata_url
            assert metadata_url is not None
            text = await client.get_metadata_text("demo", "1.0", metadata_url)

        coordinator = make_coordinator(
            files, package="demo", metadata_by_url={metadata_url: text}
        )
        dependencies = Provider(coordinator).get_dependencies("demo", Version("1.0"))

        assert set(dependencies) == {"leaf"}
        assert Version("2.0") in dependencies["leaf"]
        assert Version("1.0") not in dependencies["leaf"]

    asyncio.run(go())
    assert metadata.call_count == 1


@pytest.mark.parametrize("fresh", [False, True], ids=["stale", "fresh"])
@respx.mock
def test_cached_padded_base_rendering_is_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh: bool
) -> None:
    obsolete_body = json.dumps(
        {
            "files": [
                {
                    "filename": _WHEEL,
                    "url": f"https://files.example   /{_WHEEL}",
                    "core-metadata": True,
                }
            ]
        }
    ).encode()
    with monkeypatch.context() as previous:
        previous.setattr(cache_mod, "CACHE_VERSION_SIMPLE", "v2")
        old_cache = OnDiskCache(tmp_path, _INDEX)
        old_cache.put_simple(
            "demo",
            obsolete_body,
            CachePolicy(
                fetched_at=time.time() if fresh else 0,
                max_age=3600 if fresh else 0,
                etag='"unchanged"',
                page_url=_PAGE_URL,
            ),
        )

    def serve(request: httpx.Request) -> httpx.Response:
        if "if-none-match" in request.headers:
            return httpx.Response(304, headers={"etag": '"unchanged"'})
        return httpx.Response(
            200,
            text=_page("  https://files.example   "),
            headers={"content-type": "text/html", "etag": '"unchanged"'},
        )

    listing = respx.get(_PAGE_URL).mock(side_effect=serve)

    async def go() -> None:
        async with CachedAsyncSimpleClient(
            HttpxAsyncTransport(), _InlinePolicyCache(tmp_path, _INDEX), _INDEX
        ) as client:
            (wheel,) = await client.get_files("demo")
            assert isinstance(wheel, WheelFile)
            assert wheel.url == _WHEEL_URL
            assert wheel.metadata_url == _METADATA_URL

    asyncio.run(go())
    assert listing.call_count == 1
    assert "if-none-match" not in listing.calls[0].request.headers
    old_listing = old_cache.get_simple("demo")
    assert old_listing is not None
    assert old_listing[0] == obsolete_body
