"""Tests for the async HTTP transports and FetchCoordinator transport injection."""

from __future__ import annotations

import asyncio
import io
import json
import ssl
import tarfile
from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx
import truststore
import urllib3

from nab_index.client import AsyncSimpleClient, _extract_sdist_files
from nab_index.httpx_async_transport import HttpxAsyncTransport, _HttpxResponse
from nab_index.transport import HttpError
from nab_index.urllib3_async_transport import (
    Urllib3AsyncTransport,
    _SSLContext,
    _Urllib3Response,
)
from nab_python.fetch import FetchCoordinator

LISTING_JSON = {
    "meta": {"api-version": "1.0"},
    "name": "pkg",
    "files": [
        {
            "filename": "pkg-1.0-py3-none-any.whl",
            "url": "https://files.example.com/pkg-1.0-py3-none-any.whl",
            "data-dist-info-metadata": {"sha256": "abc"},
        },
    ],
}


class TestFetchCoordinatorTransport:
    @respx.mock
    def test_explicit_transport(self) -> None:
        """Coordinator routes fetches through whatever transport is passed in."""
        respx.get("https://pypi.org/simple/pkg/").mock(
            return_value=httpx.Response(200, json=LISTING_JSON)
        )
        with FetchCoordinator(transport=HttpxAsyncTransport()) as coord:
            event = coord.request_listing("pkg")
            event.wait(timeout=5)
            assert coord.index.get_listing("pkg") is not None


class TestHttpxAsyncTransport:
    @respx.mock
    def test_get_returns_response_adapter(self) -> None:
        respx.get("https://example.com/").mock(
            return_value=httpx.Response(200, json={"a": 1}, headers={"etag": "abc"})
        )

        async def go() -> _HttpxResponse:
            transport = HttpxAsyncTransport(http2=False)
            try:
                resp = await transport.get("https://example.com/")
                resp.raise_for_status()
                return resp
            finally:
                await transport.aclose()

        resp = asyncio.run(go())
        assert isinstance(resp, _HttpxResponse)
        assert resp.status_code == 200
        assert resp.headers["etag"] == "abc"
        assert resp.json() == {"a": 1}
        assert resp.content == b'{"a":1}'
        assert resp.text == '{"a":1}'

    @respx.mock
    def test_raise_for_status_converts_status_error(self) -> None:
        respx.get("https://example.com/missing").mock(return_value=httpx.Response(404))

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)
            try:
                resp = await transport.get("https://example.com/missing")
                resp.raise_for_status()
            finally:
                await transport.aclose()

        with pytest.raises(HttpError):
            asyncio.run(go())

    @respx.mock
    def test_get_wraps_connection_error(self) -> None:
        respx.get("https://example.com/").mock(side_effect=httpx.ConnectError("boom"))

        async def go() -> None:
            transport = HttpxAsyncTransport(http2=False)
            try:
                await transport.get("https://example.com/")
            finally:
                await transport.aclose()

        with pytest.raises(HttpError, match="GET https://example.com/ failed"):
            asyncio.run(go())

    def test_uses_truststore_ssl_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AsyncClient gets a truststore SSLContext via verify=."""
        cls = MagicMock()
        monkeypatch.setattr("nab_index.httpx_async_transport.httpx.AsyncClient", cls)
        HttpxAsyncTransport()
        verify = cls.call_args.kwargs["verify"]
        assert isinstance(verify, truststore.SSLContext)


class TestUrllib3AsyncTransport:
    def _fake_pool(self, body: bytes, status: int = 200) -> MagicMock:
        fake_response = MagicMock(spec=urllib3.BaseHTTPResponse)
        fake_response.data = body
        fake_response.status = status
        fake_response.geturl.return_value = "https://example.com/"
        pool = MagicMock(spec=urllib3.PoolManager)
        pool.request.return_value = fake_response
        return pool

    def test_get_returns_response_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = self._fake_pool(b"world")
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> tuple[bytes, str]:
            transport = Urllib3AsyncTransport()
            try:
                resp = await transport.get("https://example.com/", headers={"k": "v"})
                resp.raise_for_status()
                return resp.content, resp.text
            finally:
                await transport.aclose()

        content, text = asyncio.run(go())
        assert content == b"world"
        assert text == "world"
        pool.request.assert_called_once_with(
            "GET", "https://example.com/", headers={"Accept-Encoding": "gzip", "k": "v"}
        )
        pool.clear.assert_called_once()

    def test_get_wraps_connection_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A urllib3 transport error surfaces as the transport-contract HttpError."""
        pool = MagicMock(spec=urllib3.PoolManager)
        pool.request.side_effect = urllib3.exceptions.MaxRetryError(pool, "https://x/")
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> None:
            transport = Urllib3AsyncTransport()
            try:
                await transport.get("https://x/")
            finally:
                await transport.aclose()

        with pytest.raises(HttpError, match="GET https://x/ failed"):
            asyncio.run(go())

    def test_get_requests_gzip_without_caller_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get() advertises gzip even when the caller passes no headers.

        urllib3 sits on stdlib http.client, which emits
        ``Accept-Encoding: identity`` when no Accept-Encoding header is
        supplied, telling the server not to compress.
        """
        pool = self._fake_pool(b"{}")
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> None:
            transport = Urllib3AsyncTransport()
            try:
                await transport.get("https://example.com/")
            finally:
                await transport.aclose()

        asyncio.run(go())
        pool.request.assert_called_once_with(
            "GET", "https://example.com/", headers={"Accept-Encoding": "gzip"}
        )

    def test_get_lets_caller_override_accept_encoding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller-supplied Accept-Encoding overrides the gzip default."""
        pool = self._fake_pool(b"{}")
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: pool,
        )

        async def go() -> None:
            transport = Urllib3AsyncTransport()
            try:
                await transport.get(
                    "https://example.com/", headers={"Accept-Encoding": "identity"}
                )
            finally:
                await transport.aclose()

        asyncio.run(go())
        pool.request.assert_called_once_with(
            "GET", "https://example.com/", headers={"Accept-Encoding": "identity"}
        )

    def test_response_json(self) -> None:
        fake = MagicMock(spec=urllib3.BaseHTTPResponse)
        fake.data = json.dumps({"a": 1}).encode()
        fake.status = 200
        assert _Urllib3Response(fake).json() == {"a": 1}

    def test_response_raise_for_status(self) -> None:
        fake = MagicMock(spec=urllib3.BaseHTTPResponse)
        fake.data = b""
        fake.status = 404
        fake.geturl.return_value = "https://example.com/missing"
        with pytest.raises(HttpError, match="404"):
            _Urllib3Response(fake).raise_for_status()

    def test_response_raise_for_status_no_url(self) -> None:
        """raise_for_status falls back when geturl returns None."""
        fake = MagicMock(spec=urllib3.BaseHTTPResponse)
        fake.data = b""
        fake.status = 500
        fake.geturl.return_value = None
        with pytest.raises(HttpError, match="<unknown>"):
            _Urllib3Response(fake).raise_for_status()

    def test_response_raise_for_status_ok(self) -> None:
        fake = MagicMock(spec=urllib3.BaseHTTPResponse)
        fake.data = b""
        fake.status = 200
        _Urllib3Response(fake).raise_for_status()  # no exception

    def test_response_status_code_and_headers(self) -> None:
        fake = MagicMock(spec=urllib3.BaseHTTPResponse)
        fake.data = b""
        fake.status = 304
        fake.headers = {"etag": "abc"}
        adapter = _Urllib3Response(fake)
        assert adapter.status_code == 304
        assert adapter.headers["etag"] == "abc"

    def test_uses_truststore_ssl_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each per-thread PoolManager gets a truststore SSLContext."""
        captured: dict[str, Any] = {}

        def fake_pool_manager(**kw: Any) -> MagicMock:
            captured.update(kw)
            return MagicMock(spec=urllib3.PoolManager)

        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            fake_pool_manager,
        )
        # Pools are created lazily per worker thread, so build one.
        Urllib3AsyncTransport()._pool()
        assert isinstance(captured["ssl_context"], truststore.SSLContext)

    def test_pool_is_per_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each thread gets its own PoolManager so the truststore context is unshared."""
        monkeypatch.setattr(
            "nab_index.urllib3_async_transport.urllib3.PoolManager",
            lambda **kw: MagicMock(spec=urllib3.PoolManager),
        )
        transport = Urllib3AsyncTransport()
        pools = [
            asyncio.run(asyncio.to_thread(transport._pool)),
            asyncio.run(asyncio.to_thread(transport._pool)),
        ]
        # Distinct worker threads -> distinct pools; all tracked for aclose.
        assert pools[0] is not pools[1]
        assert set(map(id, pools)) <= set(map(id, transport._pools))

    def test_ssl_context_satisfies_urllib3_cert_check(self) -> None:
        """``_SSLContext`` returns a non-empty CA count for urllib3-future.

        urllib3-future calls ``cert_store_stats()`` to decide whether to
        load default certs; truststore raises ``NotImplementedError``
        there, so the subclass returns a non-zero ``x509_ca`` count.
        """
        ctx = _SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        stats = ctx.cert_store_stats()
        assert stats["x509_ca"] >= 1


class TestAsyncSimpleClient:
    """Tests for nab-index's AsyncSimpleClient via a faked transport."""

    class _FakeResponse:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self._content = body
            self._status = status

        @property
        def status_code(self) -> int:
            return self._status

        @property
        def headers(self) -> Mapping[str, str]:
            return {}

        @property
        def content(self) -> bytes:
            return self._content

        @property
        def text(self) -> str:
            return self._content.decode()

        def json(self) -> object:
            return json.loads(self.text)

        def raise_for_status(self) -> None:
            if self._status >= 400:
                msg = f"status {self._status}"
                raise RuntimeError(msg)

    class _FakeTransport:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self._body = body
            self._status = status
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        async def get(
            self, url: str, *, headers: dict[str, str] | None = None
        ) -> TestAsyncSimpleClient._FakeResponse:
            self.calls.append((url, headers))
            return TestAsyncSimpleClient._FakeResponse(self._body, self._status)

        async def aclose(self) -> None:
            return None

    def test_get_files(self) -> None:
        body = json.dumps(LISTING_JSON).encode()
        transport = self._FakeTransport(body)

        async def go() -> list:
            async with AsyncSimpleClient(transport, "https://pypi.org/simple/") as c:
                return await c.get_files("pkg")

        files = asyncio.run(go())
        assert len(files) == 1
        assert transport.calls[0][0] == "https://pypi.org/simple/pkg/"
        assert transport.calls[0][1] == {
            "Accept": "application/vnd.pypi.simple.v1+json"
        }

    def test_get_files_404_returns_empty(self) -> None:
        transport = self._FakeTransport(b"not found", status=404)

        async def go() -> list:
            async with AsyncSimpleClient(transport, "https://pypi.org/simple/") as c:
                return await c.get_files("absent")

        assert asyncio.run(go()) == []

    def test_get_metadata_text(self) -> None:
        transport = self._FakeTransport(b"Metadata-Version: 2.1\n")

        async def go() -> str:
            async with AsyncSimpleClient(transport) as c:
                return await c.get_metadata_text("https://example.com/pkg.metadata")

        assert asyncio.run(go()) == "Metadata-Version: 2.1\n"


def _build_tarball(members: list[tuple[str, bytes | None]]) -> bytes:
    """Build a tar.gz with the given (name, data-or-None) members.

    ``data is None`` produces a directory entry rather than a file.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestExtractSdistFiles:
    """Tests for ``_extract_sdist_files``: PKG-INFO + pyproject.toml extraction."""

    def test_returns_pkg_info_text(self) -> None:
        body = _build_tarball(
            [("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: pkg\n")]
        )
        pkg_info, pyproject = _extract_sdist_files(body)
        assert pkg_info is not None
        assert "Name: pkg" in pkg_info
        assert pyproject is None

    def test_iterates_past_non_pkg_info_members(self) -> None:
        body = _build_tarball(
            [
                ("pkg-1.0/setup.py", b"# setup"),
                ("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\n"),
            ]
        )
        pkg_info, _ = _extract_sdist_files(body)
        assert pkg_info == "Metadata-Version: 2.1\n"

    def test_skips_directory_named_pkg_info(self) -> None:
        """A depth-1 directory named PKG-INFO yields no metadata text."""
        body = _build_tarball(
            [
                ("pkg-1.0/PKG-INFO", None),
                ("pkg-1.0/setup.py", b"# something"),
            ]
        )
        pkg_info, _ = _extract_sdist_files(body)
        assert pkg_info is None

    def test_ignores_pkg_info_below_top_level(self) -> None:
        """A PKG-INFO buried below the conventional ``<name>-<version>/`` is ignored."""
        body = _build_tarball(
            [("pkg-1.0/sub/PKG-INFO", b"Metadata-Version: 2.1\nName: deep\n")]
        )
        pkg_info, _ = _extract_sdist_files(body)
        assert pkg_info is None

    def test_ignores_pyproject_below_top_level(self) -> None:
        """A pyproject.toml buried below the top level is ignored."""
        body = _build_tarball(
            [("pkg-1.0/sub/pyproject.toml", b"[project]\nname = 'deep'\n")]
        )
        _, pyproject = _extract_sdist_files(body)
        assert pyproject is None

    def test_returns_none_on_tar_error(self) -> None:
        assert _extract_sdist_files(b"not-a-tarball") == (None, None)

    def test_returns_none_when_pkg_info_missing(self) -> None:
        body = _build_tarball([("pkg-1.0/setup.py", b"# nothing")])
        pkg_info, pyproject = _extract_sdist_files(body)
        assert pkg_info is None
        assert pyproject is None
