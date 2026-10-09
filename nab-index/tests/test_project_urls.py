"""Public Simple clients request normalized project URLs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from nab_index.cache import CachePolicy, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.client import AsyncSimpleClient, SdistFile, WheelFile
from nab_index.multi_index import MultiIndexClient
from nab_index.parsed_listing import decode
from nab_index.transport import HttpError

_INDEX = "https://primary.example/simple/"
_PROJECT_URL = f"{_INDEX}foo-bar/"
_NAMES = ["Foo.Bar", "Foo-Bar", "foo_bar", "FOO__..BAR", "foo-bar"]


def _listing(version: str, *, html: bool = False) -> bytes:
    """Return a project listing with a relative wheel URL."""
    if html:
        filename = f"foo_bar-{version}-py3-none-any.whl"
        return f'<!doctype html><a href="{filename}">{filename}</a>'.encode()

    return json.dumps(
        {
            "meta": {"api-version": "1.0"},
            "name": "foo-bar",
            "files": [
                {
                    "filename": f"foo_bar-{version}-py3-none-any.whl",
                    "url": f"foo_bar-{version}-py3-none-any.whl",
                    "hashes": {},
                },
            ],
        }
    ).encode()


@dataclass
class _Response:
    """Carry one routed HTTP response."""

    url: str
    status_code: int
    content: bytes
    headers: dict[str, str] = field(
        default_factory=lambda: {
            "content-type": "application/vnd.pypi.simple.v1+json",
            "cache-control": "max-age=600",
            "etag": '"new"',
        }
    )

    @property
    def text(self) -> str:
        return self.content.decode()

    def json(self) -> object:
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            message = f"status {self.status_code}"
            raise HttpError(message)


class _RoutedTransport:
    """Serve mapped URLs and return 404 for every other URL."""

    def __init__(
        self,
        routes: dict[str, tuple[int, bytes]],
        *,
        response_url: str | None = None,
        html: bool = False,
    ) -> None:
        self._routes = routes
        self._response_url = response_url
        self._content_type = (
            "text/html" if html else "application/vnd.pypi.simple.v1+json"
        )
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _Response:
        self.calls.append((url, dict(headers or {})))
        status, body = self._routes.get(url, (404, b""))
        response = _Response(self._response_url or url, status, body)
        response.headers["content-type"] = self._content_type
        return response

    async def aclose(self) -> None:
        return None


class _InlinePolicyCache(OnDiskCache):
    """Use the custom-backend path to write revalidation policies inline."""


@pytest.mark.parametrize(
    ("package", "html"),
    [
        *(pytest.param(package, False, id=package) for package in _NAMES),
        pytest.param("Foo.Bar", True, id="html"),
    ],
)
@pytest.mark.parametrize("cached", [False, True], ids=["plain", "cached"])
def test_clients_request_normalized_project_url(
    tmp_path: Path, package: str, html: bool, cached: bool
) -> None:
    transport = _RoutedTransport(
        {_PROJECT_URL: (200, _listing("1.0", html=html))}, html=html
    )
    cache = OnDiskCache(tmp_path, _INDEX)
    client = (
        CachedAsyncSimpleClient(transport, cache, _INDEX)
        if cached
        else AsyncSimpleClient(transport, _INDEX)
    )

    async def go() -> list[WheelFile | SdistFile]:
        async with client:
            files = await client.get_files(package)
            if cached:
                assert await client.get_files(package) == files
            return files

    files = asyncio.run(go())
    assert [url for url, _ in transport.calls] == [_PROJECT_URL]
    assert [file.version for file in files] == ["1.0"]
    assert files[0].url == f"{_PROJECT_URL}foo_bar-1.0-py3-none-any.whl"
    if cached:
        assert cache.get_simple(package) is not None


@pytest.mark.parametrize("package", ["Foo.Bar", "foo-bar"])
@pytest.mark.parametrize("status", [200, 304], ids=["replacement", "unmodified"])
def test_stale_cache_requests_normalized_project_url(
    tmp_path: Path, package: str, status: int
) -> None:
    cache = _InlinePolicyCache(tmp_path, _INDEX)
    cache.put_simple(
        package,
        _listing("1.0"),
        CachePolicy(fetched_at=0, max_age=0, etag='"old"', page_url=_PROJECT_URL),
    )
    body = _listing("2.0") if status == 200 else b""
    transport = _RoutedTransport({_PROJECT_URL: (status, body)})

    async def go() -> list[WheelFile | SdistFile]:
        async with CachedAsyncSimpleClient(transport, cache, _INDEX) as client:
            files = await client.get_files(package)
            assert await client.get_files(package) == files
            return files

    files = asyncio.run(go())
    assert [url for url, _ in transport.calls] == [_PROJECT_URL]
    assert transport.calls[0][1]["If-None-Match"] == '"old"'
    version = "2.0" if status == 200 else "1.0"
    assert [file.version for file in files] == [version]
    assert files[0].url == f"{_PROJECT_URL}foo_bar-{version}-py3-none-any.whl"
    entry = cache.get_simple(package)
    assert entry is not None
    assert entry[1].page_url == _PROJECT_URL


@pytest.mark.parametrize("cached", [False, True], ids=["plain", "cached"])
def test_missing_project_requests_normalized_url(tmp_path: Path, cached: bool) -> None:
    transport = _RoutedTransport({_PROJECT_URL: (200, _listing("1.0"))})
    cache = OnDiskCache(tmp_path, _INDEX)
    client = (
        CachedAsyncSimpleClient(transport, cache, _INDEX)
        if cached
        else AsyncSimpleClient(transport, _INDEX)
    )

    async def go() -> None:
        async with client:
            assert await client.get_files("Missing.Project") == []
            if cached:
                assert await client.get_files("Missing.Project") == []

    asyncio.run(go())
    assert [url for url, _ in transport.calls] == [f"{_INDEX}missing-project/"]


@pytest.mark.parametrize("package", ["Foo.Bar", "foo-bar"])
def test_stale_project_missing_at_normalized_url(tmp_path: Path, package: str) -> None:
    cache = OnDiskCache(tmp_path, _INDEX)
    cache.put_simple(
        package,
        _listing("1.0"),
        CachePolicy(fetched_at=0, max_age=0, etag='"old"', page_url=_PROJECT_URL),
    )
    transport = _RoutedTransport({})

    async def go() -> None:
        async with CachedAsyncSimpleClient(transport, cache, _INDEX) as client:
            assert await client.get_files(package) == []

    asyncio.run(go())
    assert [url for url, _ in transport.calls] == [_PROJECT_URL]
    assert transport.calls[0][1]["If-None-Match"] == '"old"'
    assert cache.get_negative(package) is not None


def test_first_index_keeps_aliased_project(tmp_path: Path) -> None:
    package = "Foo.Bar"
    primary = _RoutedTransport({_PROJECT_URL: (200, _listing("1.0"))})
    secondary_index = "https://secondary.example/simple/"
    secondary = _RoutedTransport(
        {
            f"{secondary_index}foo-bar/": (200, _listing("2.0")),
            f"{secondary_index}{package}/": (200, _listing("2.0")),
        }
    )
    client = MultiIndexClient(
        {
            "primary": CachedAsyncSimpleClient(
                primary, OnDiskCache(tmp_path / "primary", _INDEX), _INDEX
            ),
            "secondary": CachedAsyncSimpleClient(
                secondary,
                OnDiskCache(tmp_path / "secondary", secondary_index),
                secondary_index,
            ),
        },
        ["primary", "secondary"],
        {},
    )

    async def go() -> list[WheelFile | SdistFile]:
        async with client:
            return await client.get_files(package)

    files = asyncio.run(go())
    assert [file.version for file in files] == ["1.0"]
    assert [url for url, _ in primary.calls] == [_PROJECT_URL]
    assert secondary.calls == []


def test_304_from_normalized_url_replaces_old_parsed_file_urls(tmp_path: Path) -> None:
    package = "Foo.Bar"
    old_url = f"{_INDEX}{package}/"
    cache = OnDiskCache(tmp_path, _INDEX)
    initial = _RoutedTransport(
        {_PROJECT_URL: (200, _listing("1.0"))}, response_url=old_url
    )

    async def fetch(transport: _RoutedTransport) -> list[WheelFile | SdistFile]:
        """Fetch one listing and finish its pending cache-policy writes."""
        async with CachedAsyncSimpleClient(transport, cache, _INDEX) as client:
            return await client.get_files(package)

    old_files = asyncio.run(fetch(initial))
    assert old_files[0].url == f"{old_url}foo_bar-1.0-py3-none-any.whl"
    entry = cache.get_simple(package)
    assert entry is not None
    body, policy = entry
    old_blob = cache.get_simple_parsed(package)
    assert old_blob is not None
    old_parsed = decode(old_blob, policy)
    assert old_parsed is not None
    assert old_parsed.files == old_files
    cache.refresh_simple_policy(package, replace(policy, fetched_at=0, max_age=0))

    transport = _RoutedTransport({_PROJECT_URL: (304, b"")})
    files = asyncio.run(fetch(transport))
    assert [url for url, _ in transport.calls] == [_PROJECT_URL]
    assert transport.calls[0][1]["If-None-Match"] == policy.etag
    assert [file.version for file in files] == ["1.0"]
    assert files[0].url == f"{_PROJECT_URL}foo_bar-1.0-py3-none-any.whl"

    entry = cache.get_simple(package)
    assert entry is not None
    assert entry[0] == body
    assert entry[1].page_url == _PROJECT_URL
    assert decode(old_blob, entry[1]) is None

    assert asyncio.run(fetch(transport)) == files
    assert len(transport.calls) == 1
    entry = cache.get_simple(package)
    assert entry is not None
    new_blob = cache.get_simple_parsed(package)
    assert new_blob is not None
    new_parsed = decode(new_blob, entry[1])
    assert new_parsed is not None
    assert new_parsed.files == files
