"""Public Simple clients normalize project URLs and resolve file references."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from nab_index import cache as cache_module
from nab_index.cache import CachePolicy, OfflineError, OnDiskCache
from nab_index.cached_client import CachedAsyncSimpleClient
from nab_index.client import AsyncSimpleClient, SdistFile, WheelFile
from nab_index.multi_index import MultiIndexClient
from nab_index.parsed_listing import decode, encode
from nab_index.transport import HttpError
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator, make_wheel
from nab_provider.value import replace

_INDEX = "https://primary.example/simple/"
_PROJECT_URL = f"{_INDEX}foo-bar/"
_NAMES = ["Foo.Bar", "Foo-Bar", "foo_bar", "FOO__..BAR", "foo-bar"]
_ARTIFACT = "https://files.example/foo_bar-1.0-py3-none-any.whl"
_METADATA = (
    b"Metadata-Version: 2.3\nName: foo-bar\nVersion: 1.0\nRequires-Dist: leaf>=2\n\n"
)


def _query_listing(reference: str, *, html: bool) -> bytes:
    """Build a listing with one file reference and a metadata-sidecar declaration."""
    if html:
        return (
            f'<a href="{reference}" data-core-metadata="true">'
            "foo_bar-1.0-py3-none-any.whl</a>"
        ).encode()

    return json.dumps(
        {
            "files": [
                {
                    "filename": "foo_bar-1.0-py3-none-any.whl",
                    "url": reference,
                    "hashes": {},
                    "core-metadata": True,
                }
            ]
        }
    ).encode()


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
    ("reference", "suffix"),
    [
        pytest.param("?", "", id="empty-query"),
        pytest.param("?#download", "#download", id="empty-query-fragment"),
        pytest.param("?\t\r\n#download", "#download", id="stripped-query-controls"),
        pytest.param("?download=1", "?download=1", id="explicit-query"),
        pytest.param("?download=?", "?download=?", id="query-ending-in-question-mark"),
        pytest.param("", "?view=index", id="absent-query"),
        pytest.param("#download", "?view=index#download", id="fragment-only"),
    ],
)
@pytest.mark.parametrize("html", [False, True], ids=["json", "html"])
@pytest.mark.parametrize("cached", [False, True], ids=["plain", "cached"])
def test_file_references_resolve_queries(
    tmp_path: Path, reference: str, suffix: str, html: bool, cached: bool
) -> None:
    expected_url = _ARTIFACT + suffix
    if html:
        expected_url = expected_url.partition("#")[0]
    path_query = expected_url.partition("#")[0]
    path, separator, query = path_query.partition("?")
    metadata_url = path + ".metadata" + separator + query

    transport = _RoutedTransport(
        {
            _PROJECT_URL: (200, _query_listing(reference, html=html)),
            metadata_url: (200, _METADATA),
        },
        response_url=_ARTIFACT + "?view=index",
        html=html,
    )
    cache = OnDiskCache(tmp_path, _INDEX)

    async def check(client: AsyncSimpleClient | CachedAsyncSimpleClient) -> None:
        async with client:
            files = await client.get_files("foo-bar")
            assert [file.url for file in files] == [expected_url]
            assert files[0].metadata_url == metadata_url

            text = (
                await client.get_metadata_text("foo-bar", "1.0", metadata_url)
                if isinstance(client, CachedAsyncSimpleClient)
                else await client.get_metadata_text(metadata_url)
            )
            assert text == _METADATA.decode()

            provider = Provider(
                make_coordinator(
                    files,
                    package="foo-bar",
                    metadata_by_url={metadata_url: text},
                )
            )
            assert set(provider.get_dependencies("foo-bar", Version("1.0"))) == {"leaf"}

    if cached:
        asyncio.run(check(CachedAsyncSimpleClient(transport, cache, _INDEX)))
        asyncio.run(check(CachedAsyncSimpleClient(transport, cache, _INDEX)))

        calls = len(transport.calls)
        asyncio.run(
            check(CachedAsyncSimpleClient(transport, cache, _INDEX, offline=True))
        )
        assert len(transport.calls) == calls
    else:
        asyncio.run(check(AsyncSimpleClient(transport, _INDEX)))


def test_html_base_reference_clears_query() -> None:
    body = b'<base href="?">' + _query_listing("#download", html=True)
    transport = _RoutedTransport(
        {_PROJECT_URL: (200, body)},
        response_url=_ARTIFACT + "?view=index",
        html=True,
    )
    files = asyncio.run(AsyncSimpleClient(transport, _INDEX).get_files("foo-bar"))

    assert [file.url for file in files] == [_ARTIFACT]


def test_cached_html_with_inherited_query_is_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _query_listing(_ARTIFACT + "?view=index", html=False)
    policy = CachePolicy(
        fetched_at=time.time(), max_age=600, etag='"old"', page_url=_ARTIFACT
    )
    with monkeypatch.context() as old:
        old.setattr(cache_module, "CACHE_VERSION_SIMPLE", "v3")
        OnDiskCache(tmp_path, _INDEX).put_simple("foo-bar", body, policy)

    cache = OnDiskCache(tmp_path, _INDEX)
    transport = _RoutedTransport(
        {_PROJECT_URL: (200, _query_listing("?", html=True))},
        response_url=_ARTIFACT + "?view=index",
        html=True,
    )

    async def read(*, offline: bool) -> list[WheelFile | SdistFile]:
        async with CachedAsyncSimpleClient(
            transport, cache, _INDEX, offline=offline
        ) as client:
            return await client.get_files("foo-bar")

    with pytest.raises(OfflineError):
        asyncio.run(read(offline=True))
    assert transport.calls == []

    assert [file.url for file in asyncio.run(read(offline=False))] == [_ARTIFACT]
    assert [file.url for file in asyncio.run(read(offline=True))] == [_ARTIFACT]
    assert [url for url, _ in transport.calls] == [_PROJECT_URL]


def test_cached_json_with_inherited_query_is_reparsed(tmp_path: Path) -> None:
    cache = OnDiskCache(tmp_path, _INDEX)
    digest = cache.put_simple(
        "foo-bar",
        _query_listing("?", html=False),
        CachePolicy(
            fetched_at=time.time(),
            max_age=600,
            etag=None,
            page_url=_ARTIFACT + "?view=index",
        ),
    )
    assert digest is not None

    wheel = make_wheel(package="foo_bar", url=_ARTIFACT + "?view=index")
    blob = json.loads(encode([wheel], digest))
    # Emulate a stale parsed record without changing the current encoder.
    blob[0][0] = 7
    cache.put_simple_parsed("foo-bar", json.dumps(blob).encode())

    transport = _RoutedTransport({})

    async def read() -> list[WheelFile | SdistFile]:
        async with CachedAsyncSimpleClient(
            transport, cache, _INDEX, offline=True
        ) as client:
            return await client.get_files("foo-bar")

    assert [file.url for file in asyncio.run(read())] == [_ARTIFACT]
    assert [file.url for file in asyncio.run(read())] == [_ARTIFACT]
    assert transport.calls == []


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
