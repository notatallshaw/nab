"""Tests for nab_index.cached_client.CachedAsyncSimpleClient."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nab_index.cache import CachePolicy, OfflineError, OnDiskCache
from nab_index.cached_client import (
    CachedAsyncSimpleClient,
    _header,
    _parse_max_age,
)
from nab_index.client import SdistFile, WheelFile, _parse_files, _parse_sdist_filename

LISTING = {
    "meta": {"api-version": "1.0"},
    "name": "pkg",
    "files": [
        {
            "filename": "pkg-1.0-py3-none-any.whl",
            "url": "https://files.example.com/pkg-1.0-py3-none-any.whl",
            "core-metadata": {"sha256": "abc"},
        },
    ],
}
LISTING_BYTES = json.dumps(LISTING).encode()


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.content = body
        self.status_code = status
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.content.decode()

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"status {self.status_code}"
            raise RuntimeError(msg)


class _FakeTransport:
    def __init__(self, responses: list[_FakeResponse] | None = None) -> None:
        self._responses = responses or []
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self.calls.append((url, headers))
        if not self._responses:
            msg = f"unexpected request to {url}"
            raise AssertionError(msg)
        return self._responses.pop(0)

    async def aclose(self) -> None:
        return None


def _make_cache(tmp_path: Path) -> OnDiskCache:
    return OnDiskCache(tmp_path, "https://pypi.org/simple/")


def _build_tarball(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestHasMetadataFlag:
    """PEP 691 boolean variants of ``core-metadata`` / ``data-dist-info-metadata``."""

    def test_dict_value_advertises_metadata(self) -> None:
        from nab_index.client import _has_metadata

        assert _has_metadata({"core-metadata": {"sha256": "abc"}})

    def test_true_value_advertises_metadata(self) -> None:
        from nab_index.client import _has_metadata

        assert _has_metadata({"core-metadata": True})

    def test_legacy_data_dist_info_true(self) -> None:
        from nab_index.client import _has_metadata

        assert _has_metadata({"data-dist-info-metadata": True})

    def test_false_value_does_not_advertise(self) -> None:
        from nab_index.client import _has_metadata

        assert not _has_metadata({"core-metadata": False})

    def test_missing_field(self) -> None:
        from nab_index.client import _has_metadata

        assert not _has_metadata({})


class TestYankedFiltering:
    """PEP 592 ``yanked`` files are dropped from the listing."""

    def test_yanked_true_excluded(self) -> None:
        from nab_index.client import _parse_files

        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://example.com/foo-1.0-py3-none-any.whl",
                    "yanked": True,
                },
                {
                    "filename": "foo-2.0-py3-none-any.whl",
                    "url": "https://example.com/foo-2.0-py3-none-any.whl",
                },
            ],
        }
        files = _parse_files(data, "https://example.com/", "foo")
        assert [f.filename for f in files] == ["foo-2.0-py3-none-any.whl"]

    def test_yanked_reason_string_excluded(self) -> None:
        from nab_index.client import _parse_files

        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://example.com/foo-1.0-py3-none-any.whl",
                    "yanked": "security incident #42",
                },
            ],
        }
        files = _parse_files(data, "https://example.com/", "foo")
        assert files == []

    def test_yanked_false_kept(self) -> None:
        from nab_index.client import _parse_files

        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://example.com/foo-1.0-py3-none-any.whl",
                    "yanked": False,
                },
            ],
        }
        files = _parse_files(data, "https://example.com/", "foo")
        assert len(files) == 1

    def test_yanked_empty_string_kept(self) -> None:
        from nab_index.client import _parse_files

        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://example.com/foo-1.0-py3-none-any.whl",
                    "yanked": "",
                },
            ],
        }
        files = _parse_files(data, "https://example.com/", "foo")
        assert len(files) == 1


class TestZipSdistDropped:
    """nab admits only .tar.gz sdists; .zip is dropped at parse time."""

    def test_parse_sdist_filename_rejects_zip(self) -> None:
        assert _parse_sdist_filename("foo-1.0.zip") is None

    def test_parse_sdist_filename_accepts_tar_gz(self) -> None:
        assert _parse_sdist_filename("foo-1.0.tar.gz") == ("foo", "1.0")

    def test_zip_alongside_tar_gz_keeps_only_tar_gz(self) -> None:
        data = {
            "files": [
                {
                    "filename": "foo-1.0.tar.gz",
                    "url": "https://example.com/foo-1.0.tar.gz",
                },
                {
                    "filename": "foo-1.0.zip",
                    "url": "https://example.com/foo-1.0.zip",
                },
            ],
        }
        files = _parse_files(data, "https://example.com/", "foo")
        assert [f.filename for f in files] == ["foo-1.0.tar.gz"]
        assert all(isinstance(f, SdistFile) for f in files)

    def test_zip_only_release_yields_no_sdist(self) -> None:
        data = {
            "files": [
                {
                    "filename": "foo-1.0.zip",
                    "url": "https://example.com/foo-1.0.zip",
                },
            ],
        }
        assert _parse_files(data, "https://example.com/", "foo") == []

    def test_zip_dropped_wheel_kept(self) -> None:
        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://example.com/foo-1.0-py3-none-any.whl",
                },
                {
                    "filename": "foo-1.0.zip",
                    "url": "https://example.com/foo-1.0.zip",
                },
            ],
        }
        files = _parse_files(data, "https://example.com/", "foo")
        assert [f.filename for f in files] == ["foo-1.0-py3-none-any.whl"]
        assert all(isinstance(f, WheelFile) for f in files)


class TestParseMaxAge:
    def test_default_when_none(self) -> None:
        assert _parse_max_age(None) == 600

    def test_default_when_unparseable(self) -> None:
        assert _parse_max_age("public") == 600

    def test_extracts_value(self) -> None:
        assert _parse_max_age("max-age=900, public") == 900

    def test_extracts_value_with_spaces(self) -> None:
        assert _parse_max_age("public, max-age = 1200") == 1200


class TestHeader:
    def test_lowercase_lookup(self) -> None:
        resp = _FakeResponse(b"", headers={"etag": "abc"})
        assert _header(resp, "etag") == "abc"

    def test_titlecase_fallback(self) -> None:
        resp = _FakeResponse(b"", headers={"ETag": "abc"})
        assert _header(resp, "etag") == "abc"

    def test_missing_returns_none(self) -> None:
        resp = _FakeResponse(b"", headers={})
        assert _header(resp, "etag") is None


class TestGetFiles:
    def test_cold_cache_fetches_and_stores(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={
                        "etag": "v1",
                        "cache-control": "max-age=600, public",
                    },
                )
            ]
        )

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        files = asyncio.run(go())
        assert len(files) == 1
        # Cache populated.
        cached = cache.get_simple("pkg")
        assert cached is not None
        body, policy = cached
        assert body == LISTING_BYTES
        assert policy.etag == "v1"
        assert policy.max_age == 600
        # One transport call.
        assert len(transport.calls) == 1

    def test_fresh_hit_returns_without_network(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(
                fetched_at=2_000_000_000,
                max_age=99999,
                etag="x",
            ),
        )
        transport = _FakeTransport()

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        files = asyncio.run(go())
        assert len(files) == 1
        assert transport.calls == []

    def test_stale_revalidates_with_etag_304_reuses_body(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag="old-etag"),
        )
        transport = _FakeTransport(
            [
                _FakeResponse(
                    b"",
                    status=304,
                    headers={
                        "cache-control": "max-age=600, public",
                    },
                )
            ]
        )

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        files = asyncio.run(go())
        assert len(files) == 1
        # If-None-Match was sent.
        sent_headers = transport.calls[0][1]
        assert sent_headers is not None
        assert sent_headers.get("If-None-Match") == "old-etag"
        # Policy was refreshed; etag preserved (server did not send a new one).
        cached = cache.get_simple("pkg")
        assert cached is not None
        _, new_policy = cached
        assert new_policy.etag == "old-etag"
        assert new_policy.max_age == 600

    def test_stale_revalidates_304_with_new_etag_replaces_etag(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag="old"),
        )
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "new"})]
        )

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        asyncio.run(go())
        cached = cache.get_simple("pkg")
        assert cached is not None
        _, policy = cached
        assert policy.etag == "new"

    def test_stale_revalidates_no_etag_omits_header(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag=None),
        )
        transport = _FakeTransport([_FakeResponse(b"", status=304, headers={})])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        asyncio.run(go())
        sent = transport.calls[0][1]
        assert sent is not None
        assert "If-None-Match" not in sent

    def test_stale_revalidates_200_replaces_body(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            b'{"files": []}',
            CachePolicy(fetched_at=0, max_age=1, etag="old"),
        )
        transport = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    status=200,
                    headers={
                        "etag": "fresh",
                        "cache-control": "max-age=600",
                    },
                )
            ]
        )

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        files = asyncio.run(go())
        assert len(files) == 1
        cached = cache.get_simple("pkg")
        assert cached is not None
        body, policy = cached
        assert body == LISTING_BYTES
        assert policy.etag == "fresh"

    def test_offline_with_cached_returns_cached_even_when_stale(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag="old"),
        )
        transport = _FakeTransport()  # would raise on any call

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache, offline=True)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        files = asyncio.run(go())
        assert len(files) == 1
        assert transport.calls == []

    def test_offline_miss_raises(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport()

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache, offline=True)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        with pytest.raises(OfflineError, match="pkg"):
            asyncio.run(go())

    def test_revalidate_5xx_raises(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag="old"),
        )
        transport = _FakeTransport([_FakeResponse(b"", status=500)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        with pytest.raises(RuntimeError, match="500"):
            asyncio.run(go())

    def test_legacy_filename_with_build_tag_is_dropped(self, tmp_path: Path) -> None:
        """Legacy sdists like ``cffi-1.0.2-2.tar.gz`` parse to a different
        canonical name (``cffi-1-0-2``) under packaging's last-dash split.
        Without name validation they leak into the listing as a phantom
        version (``cffi==2``).  This guards against regressing that path.
        """
        listing = {
            "meta": {"api-version": "1.0"},
            "name": "cffi",
            "files": [
                {
                    "filename": "cffi-2.0.0.tar.gz",
                    "url": "https://files.example.com/cffi-2.0.0.tar.gz",
                },
                {
                    "filename": "cffi-1.0.2-2.tar.gz",
                    "url": "https://files.example.com/cffi-1.0.2-2.tar.gz",
                },
            ],
        }
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    json.dumps(listing).encode(),
                    headers={"etag": "v1", "cache-control": "max-age=600"},
                )
            ]
        )

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("cffi")
            finally:
                await client.aclose()

        files = asyncio.run(go())
        assert [f.version for f in files] == ["2.0.0"]

    def test_canonical_name_request_matches_listing(self, tmp_path: Path) -> None:
        """A request under a non-canonical name (``Foo.Bar``, ``Foo-Bar``,
        ``foo_bar``) still matches the canonical filename name.  Index
        callers are not required to canonicalize before calling
        get_files; PEP 503 normalisation collapses ``_``, ``-``, ``.``
        and folds case.
        """
        listing = {
            "meta": {"api-version": "1.0"},
            "name": "foo-bar",
            "files": [
                {
                    "filename": "foo_bar-2.0.0.tar.gz",
                    "url": "https://files.example.com/foo_bar-2.0.0.tar.gz",
                },
            ],
        }
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    json.dumps(listing).encode(),
                    headers={"etag": "v1", "cache-control": "max-age=600"},
                )
            ]
        )

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("Foo.Bar")
            finally:
                await client.aclose()

        files = asyncio.run(go())
        assert len(files) == 1


class TestGetMetadataText:
    def test_cold_cache_fetches_and_stores(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(b"Metadata-Version: 2.1\n")])

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata"
                )
            finally:
                await client.aclose()

        text = asyncio.run(go())
        assert text == "Metadata-Version: 2.1\n"
        assert cache.get_metadata("pkg", "1.0") == text

    def test_warm_cache_skips_network(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_metadata("pkg", "1.0", "stored")
        transport = _FakeTransport()

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata"
                )
            finally:
                await client.aclose()

        assert asyncio.run(go()) == "stored"
        assert transport.calls == []

    def test_offline_miss_raises(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport()

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache, offline=True)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata"
                )
            finally:
                await client.aclose()

        with pytest.raises(OfflineError, match="pkg==1.0"):
            asyncio.run(go())


class TestGetSdistFiles:
    def test_cold_cache_fetches_and_stores_both(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        body = _build_tarball(
            [
                ("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: pkg\n"),
                ("pkg-1.0/pyproject.toml", b'[project]\nname = "pkg"\n'),
            ]
        )
        transport = _FakeTransport([_FakeResponse(body)])

        async def go() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz"
                )
            finally:
                await client.aclose()

        pkg_info, pyproject = asyncio.run(go())
        assert pkg_info is not None
        assert "Name: pkg" in pkg_info
        assert pyproject is not None
        assert "[project]" in pyproject
        assert cache.get_sdist_pkginfo("pkg", "1.0") == pkg_info
        assert cache.get_sdist_pyproject("pkg", "1.0") == pyproject

    def test_warm_cache_skips_network(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_sdist_pkginfo("pkg", "1.0", "Name: cached\n")
        cache.put_sdist_pyproject("pkg", "1.0", "[project]\nname = 'cached'\n")
        transport = _FakeTransport()

        async def go() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz"
                )
            finally:
                await client.aclose()

        pkg_info, pyproject = asyncio.run(go())
        assert pkg_info == "Name: cached\n"
        assert pyproject == "[project]\nname = 'cached'\n"
        assert transport.calls == []

    def test_unreadable_sdist_returns_none_and_skips_cache(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(b"not-a-tarball")])

        async def go() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz"
                )
            finally:
                await client.aclose()

        assert asyncio.run(go()) == (None, None)
        assert cache.get_sdist_pkginfo("pkg", "1.0") is None
        assert cache.get_sdist_pyproject("pkg", "1.0") is None

    def test_offline_miss_raises(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport()

        async def go() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache, offline=True)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz"
                )
            finally:
                await client.aclose()

        with pytest.raises(OfflineError, match="pkg==1.0"):
            asyncio.run(go())


class TestContextManager:
    def test_aenter_returns_self_and_aclose_closes_transport(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        closes: list[bool] = []

        class _Closer:
            async def get(
                self, url: str, *, headers: dict[str, str] | None = None
            ) -> _FakeResponse:  # pragma: no cover - unused
                msg = "no get expected"
                raise AssertionError(msg)

            async def aclose(self) -> None:
                closes.append(True)

        async def go() -> None:
            async with CachedAsyncSimpleClient(_Closer(), cache) as client:
                assert isinstance(client, CachedAsyncSimpleClient)

        asyncio.run(go())
        assert closes == [True]
