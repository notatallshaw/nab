"""Tests for nab_index.cached_client.CachedAsyncSimpleClient."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import pytest

import nab_index.cache as cache_mod
from nab_index.cache import CachePolicy, OfflineError, OnDiskCache
from nab_index.cached_client import (
    CachedAsyncSimpleClient,
    _header,
    _parse_max_age,
)
from nab_index.client import (
    MalformedSimpleResponseError,
    MetadataHashMismatchError,
    SdistFile,
    SdistHashMismatchError,
    WheelFile,
    _parse_files,
    _parse_sdist_filename,
    _select_artifact_hash,
)
from nab_index.transport import HttpError
from nab_python.metadata import parse_metadata

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
            raise HttpError(msg)


class _MischarsetResponse(_FakeResponse):
    """Response whose ``.text`` decodes the body as utf-16, not utf-8.

    Mimics a transport that decodes ``.text`` under the response
    Content-Type charset, so ``.text`` and the hash-verified ``.content``
    disagree.
    """

    @property
    def text(self) -> str:
        return self.content.decode("utf-16", errors="replace")


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


class _PathRoutingTransport:
    """Serves bodies by URL path, as a server does: the fragment is not sent."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self._bodies = bodies
        self.paths: list[str] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        path = urlsplit(url).path
        self.paths.append(path)
        return _FakeResponse(self._bodies[path])

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
    """PEP 691 boolean variants of ``core-metadata`` / ``dist-info-metadata``."""

    def test_dict_value_advertises_metadata(self) -> None:
        from nab_index.client import _has_metadata

        assert _has_metadata({"core-metadata": {"sha256": "abc"}})

    def test_true_value_advertises_metadata(self) -> None:
        from nab_index.client import _has_metadata

        assert _has_metadata({"core-metadata": True})

    def test_legacy_json_key_true(self) -> None:
        from nab_index.client import _has_metadata

        assert _has_metadata({"dist-info-metadata": True})

    def test_false_value_does_not_advertise(self) -> None:
        from nab_index.client import _has_metadata

        assert not _has_metadata({"core-metadata": False})

    def test_missing_field(self) -> None:
        from nab_index.client import _has_metadata

        assert not _has_metadata({})

    def test_core_metadata_false_suppresses_legacy_key(self) -> None:
        from nab_index.client import _has_metadata

        assert not _has_metadata(
            {"core-metadata": False, "dist-info-metadata": {"sha256": "deadbeef"}}
        )

    def test_core_metadata_true_ignores_legacy_key(self) -> None:
        from nab_index.client import _has_metadata

        assert _has_metadata({"core-metadata": True, "dist-info-metadata": False})


class TestMetadataHashParsing:
    """``_metadata_hash`` carries the published hash to verify, or None."""

    def test_sha256_lowercased(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash({"core-metadata": {"sha256": "ABCD"}}) == (
            "sha256",
            "abcd",
        )

    def test_uppercase_algo_name(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash({"core-metadata": {"SHA256": "ABCD"}}) == (
            "sha256",
            "abcd",
        )

    def test_legacy_key_used(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash({"dist-info-metadata": {"sha256": "ab"}}) == (
            "sha256",
            "ab",
        )

    def test_true_value_yields_none(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash({"core-metadata": True}) is None

    def test_other_algo_only_yields_none(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash({"core-metadata": {"blake2b": "ab"}}) is None

    def test_sha512_only_verified(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash({"core-metadata": {"sha512": "ABCD"}}) == (
            "sha512",
            "abcd",
        )

    def test_sha384_only_verified(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash({"core-metadata": {"sha384": "ABCD"}}) == (
            "sha384",
            "abcd",
        )

    def test_sha256_preferred_over_sha512(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash(
            {"core-metadata": {"sha512": "aaaa", "sha256": "bbbb"}}
        ) == ("sha256", "bbbb")

    def test_sha384_preferred_over_sha512(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash(
            {"core-metadata": {"sha512": "aaaa", "sha384": "bbbb"}}
        ) == ("sha384", "bbbb")

    def test_unsupported_with_supported_skips_unsupported(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash(
            {"core-metadata": {"blake2b": "aaaa", "sha512": "bbbb"}}
        ) == ("sha512", "bbbb")

    def test_missing_field_yields_none(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash({}) is None

    def test_core_metadata_false_ignores_legacy_hash(self) -> None:
        from nab_index.client import _metadata_hash

        assert (
            _metadata_hash(
                {"core-metadata": False, "dist-info-metadata": {"sha256": "deadbeef"}}
            )
            is None
        )

    def test_core_metadata_true_ignores_legacy_hash(self) -> None:
        from nab_index.client import _metadata_hash

        assert (
            _metadata_hash(
                {"core-metadata": True, "dist-info-metadata": {"sha256": "cafef00d"}}
            )
            is None
        )

    def test_core_metadata_hash_preferred_over_legacy(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash(
            {
                "core-metadata": {"sha256": "AAAA"},
                "dist-info-metadata": {"sha256": "BBBB"},
            }
        ) == ("sha256", "aaaa")

    def test_parse_files_populates_metadata_hash(self) -> None:
        from nab_index.client import WheelFile, _parse_files

        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://example.com/foo-1.0-py3-none-any.whl",
                    "core-metadata": {"sha256": "DEAD"},
                },
            ],
        }
        (wheel,) = _parse_files(data, "https://example.com/", "foo")
        assert isinstance(wheel, WheelFile)
        assert wheel.metadata_hash == ("sha256", "dead")

    def test_empty_digest_yields_none(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash({"core-metadata": {"sha256": ""}}) is None

    def test_empty_digest_falls_through_to_valid_algo(self) -> None:
        from nab_index.client import _metadata_hash

        assert _metadata_hash(
            {"core-metadata": {"sha256": "", "sha512": "a" * 128}}
        ) == ("sha512", "a" * 128)

    def test_parse_files_empty_digest_leaves_metadata_unverified(self) -> None:
        from nab_index.client import WheelFile, _parse_files

        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://example.com/foo-1.0-py3-none-any.whl",
                    "core-metadata": {"sha256": ""},
                },
            ],
        }
        (wheel,) = _parse_files(data, "https://example.com/", "foo")
        assert isinstance(wheel, WheelFile)
        assert wheel.has_metadata
        assert wheel.metadata_hash is None


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


class TestRelativeUrlResolution:
    """PEP 691: relative file URLs resolve against the package page."""

    def test_relative_filename_resolves_against_package_page(self) -> None:
        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "foo-1.0-py3-none-any.whl",
                },
            ],
        }
        files = _parse_files(data, "https://example.com/simple/", "foo")
        expected = "https://example.com/simple/foo/foo-1.0-py3-none-any.whl"
        assert files[0].url == expected

    def test_dot_dot_relative_url_is_normalised(self) -> None:
        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "../../packages/foo/foo-1.0-py3-none-any.whl",
                },
            ],
        }
        files = _parse_files(data, "https://example.com/simple/", "foo")
        expected = "https://example.com/packages/foo/foo-1.0-py3-none-any.whl"
        assert files[0].url == expected

    def test_absolute_url_unchanged(self) -> None:
        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://files.example.com/foo-1.0-py3-none-any.whl",
                },
            ],
        }
        files = _parse_files(data, "https://example.com/simple/", "foo")
        assert files[0].url == "https://files.example.com/foo-1.0-py3-none-any.whl"

    def test_absolute_url_matches_urljoin(self) -> None:
        raw = "https://files.pythonhosted.org/packages/ab/cd/foo-1.0-py3-none-any.whl"
        base = "https://pypi.org/simple/foo/"
        data = {"files": [{"filename": "foo-1.0-py3-none-any.whl", "url": raw}]}
        files = _parse_files(data, "https://pypi.org/simple/", "foo")
        assert files[0].url == urljoin(base, raw) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "https://files.example.com/a/../b/foo-1.0-py3-none-any.whl",
            "https://files.example.com/foo-1.0-py3-none-any.whl?token=x",
            "https://files.example.com/foo-1.0-py3-none-any.whl#sha256=abc",
            "http://files.example.com/foo-1.0-py3-none-any.whl",
        ],
    )
    def test_absolute_url_edge_forms_match_urljoin(self, raw: str) -> None:
        base = "https://example.com/simple/foo/"
        data = {"files": [{"filename": "foo-1.0-py3-none-any.whl", "url": raw}]}
        files = _parse_files(data, "https://example.com/simple/", "foo")
        assert files[0].url == urljoin(base, raw) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "//files.example.com/foo-1.0-py3-none-any.whl",
            "https:foo-1.0-py3-none-any.whl",
        ],
    )
    def test_non_absolute_url_resolves_against_page(self, raw: str) -> None:
        # The shortcut only fires on an https://-or-http:// prefix; a
        # protocol-relative or scheme-without-authority URL must fall through
        # to urljoin, not be used verbatim. A broadened prefix would keep these
        # unchanged and silently yield a wrong download URL.
        base = "https://example.com/simple/foo/"
        data = {"files": [{"filename": "foo-1.0-py3-none-any.whl", "url": raw}]}
        files = _parse_files(data, "https://example.com/simple/", "foo")
        assert files[0].url == urljoin(base, raw) != raw


class TestMetadataUrl:
    """PEP 658/714 sidecar URL derived from the PEP 691 file URL."""

    def _wheel(self, url: str, *, has_metadata: bool = True) -> WheelFile:
        return WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url=url,
            version="1.0",
            requires_python=None,
            has_metadata=has_metadata,
            upload_time=None,
        )

    def test_suffix_appended_to_path(self) -> None:
        wheel = self._wheel("https://files.example.com/foo-1.0-py3-none-any.whl")
        assert (
            wheel.metadata_url
            == "https://files.example.com/foo-1.0-py3-none-any.whl.metadata"
        )

    def test_hash_fragment_dropped(self) -> None:
        wheel = self._wheel(
            "https://files.example.com/foo-1.0-py3-none-any.whl#sha256=" + "a" * 64
        )
        assert (
            wheel.metadata_url
            == "https://files.example.com/foo-1.0-py3-none-any.whl.metadata"
        )

    def test_query_string_preserved(self) -> None:
        wheel = self._wheel(
            "https://files.example.com/foo-1.0-py3-none-any.whl?token=x#sha256=abc"
        )
        assert (
            wheel.metadata_url
            == "https://files.example.com/foo-1.0-py3-none-any.whl.metadata?token=x"
        )

    def test_file_url(self) -> None:
        wheel = self._wheel("file:///srv/wheels/foo-1.0-py3-none-any.whl")
        assert (
            wheel.metadata_url == "file:///srv/wheels/foo-1.0-py3-none-any.whl.metadata"
        )

    def test_no_sidecar_yields_none(self) -> None:
        wheel = self._wheel(
            "https://files.example.com/foo-1.0-py3-none-any.whl", has_metadata=False
        )
        assert wheel.metadata_url is None


class TestFragmentedUrlSidecarFetch:
    """A PEP 503 hash fragment on the file URL must not divert the sidecar fetch."""

    def test_sidecar_fetched_and_verified(self, tmp_path: Path) -> None:
        wheel_bytes = b"PK\x03\x04 not really a wheel"
        sidecar = b"Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
        wheel_path = "/packages/foo-1.0-py3-none-any.whl"
        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": (
                        f"https://files.example.com{wheel_path}"
                        f"#sha256={hashlib.sha256(wheel_bytes).hexdigest()}"
                    ),
                    "core-metadata": {
                        "sha256": hashlib.sha256(sidecar).hexdigest(),
                    },
                },
            ],
        }

        files = _parse_files(data, "https://example.com/simple/", "foo")
        wheel = files[0]
        assert isinstance(wheel, WheelFile)

        transport = _PathRoutingTransport(
            {wheel_path: wheel_bytes, wheel_path + ".metadata": sidecar}
        )

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, _make_cache(tmp_path))
            try:
                assert wheel.metadata_url is not None
                return await client.get_metadata_text(
                    "foo", "1.0", wheel.metadata_url, wheel.metadata_hash
                )
            finally:
                await client.aclose()

        assert asyncio.run(go()) == sidecar.decode()
        assert transport.paths == [wheel_path + ".metadata"]


class TestParseHashes:
    def test_single_entry(self) -> None:
        from nab_index.client import _parse_hashes

        assert _parse_hashes({"sha256": "a" * 64}) == (("sha256", "a" * 64),)

    def test_single_entry_malformed(self) -> None:
        from nab_index.client import _parse_hashes

        assert _parse_hashes({"sha256": 123}) == ()

    def test_multiple_entries(self) -> None:
        from nab_index.client import _parse_hashes

        result = _parse_hashes({"sha256": "a" * 64, "md5": "b" * 32})
        assert result == (("sha256", "a" * 64), ("md5", "b" * 32))

    def test_multiple_entries_skips_malformed(self) -> None:
        from nab_index.client import _parse_hashes

        result = _parse_hashes({"sha256": "a" * 64, "md5": 123})
        assert result == (("sha256", "a" * 64),)

    def test_non_dict(self) -> None:
        from nab_index.client import _parse_hashes

        assert _parse_hashes("sha256:abc") == ()

    def test_empty_dict(self) -> None:
        from nab_index.client import _parse_hashes

        assert _parse_hashes({}) == ()

    def test_single_empty_digest_dropped(self) -> None:
        from nab_index.client import _parse_hashes

        assert _parse_hashes({"sha256": ""}) == ()

    def test_empty_digest_falls_through_to_valid(self) -> None:
        from nab_index.client import _parse_hashes

        assert _parse_hashes({"sha256": "", "sha512": "f" * 128}) == (
            ("sha512", "f" * 128),
        )

    def test_empty_digest_yields_no_sdist_check(self) -> None:
        from nab_index.client import _parse_hashes, _select_artifact_hash

        assert _select_artifact_hash(_parse_hashes({"sha256": ""})) is None


class TestSelectArtifactHash:
    def test_prefers_sha256(self) -> None:
        hashes = (("sha512", "f" * 128), ("sha256", "a" * 64))
        assert _select_artifact_hash(hashes) == ("sha256", "a" * 64)

    def test_falls_through_to_sha384(self) -> None:
        hashes = (("sha512", "f" * 128), ("sha384", "b" * 96))
        assert _select_artifact_hash(hashes) == ("sha384", "b" * 96)

    def test_falls_through_to_sha512(self) -> None:
        assert _select_artifact_hash((("sha512", "f" * 128),)) == (
            "sha512",
            "f" * 128,
        )

    def test_empty_returns_none(self) -> None:
        assert _select_artifact_hash(()) is None

    def test_only_unacceptable_returns_none(self) -> None:
        assert _select_artifact_hash((("md5", "d" * 32),)) is None

    def test_empty_digest_returns_none(self) -> None:
        assert _select_artifact_hash((("sha256", ""),)) is None

    def test_empty_digest_falls_through_to_valid_algo(self) -> None:
        hashes = (("sha256", ""), ("sha512", "f" * 128))
        assert _select_artifact_hash(hashes) == ("sha512", "f" * 128)


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

    def test_bare_304_retains_stored_max_age(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=60, etag="v1"),
        )
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "v1"})]
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
        _, new_policy = cached
        assert new_policy.max_age == 60

    def test_304_cache_control_overrides_stored_max_age(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=60, etag="v1"),
        )
        transport = _FakeTransport(
            [
                _FakeResponse(
                    b"",
                    status=304,
                    headers={"etag": "v1", "cache-control": "max-age=120"},
                )
            ]
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
        _, new_policy = cached
        assert new_policy.max_age == 120

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

        with pytest.raises(HttpError, match="500"):
            asyncio.run(go())

    def test_cold_cache_404_returns_empty(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(b"not found", status=404)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("absent")
            finally:
                await client.aclose()

        assert asyncio.run(go()) == []
        # A 404 is not cached, so a later run re-queries the index.
        assert cache.get_simple("absent") is None

    def test_revalidate_404_returns_empty(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag="old"),
        )
        transport = _FakeTransport([_FakeResponse(b"gone", status=404)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        assert asyncio.run(go()) == []

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


class TestNonJsonListingBody:
    """A 200 response whose body is not JSON must not poison the cache.

    An index that ignores the JSON Accept header can serve PEP 503 HTML
    (or a proxy/captive-portal page) with status 200.
    """

    _HTML_BODY = b"<!DOCTYPE html><html><body>links</body></html>"

    def test_cold_non_json_body_raises_clean_and_skips_cache(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(self._HTML_BODY, status=200)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("foo")
            finally:
                await client.aclose()

        with pytest.raises(MalformedSimpleResponseError, match="malformed Simple-API"):
            asyncio.run(go())
        assert cache.get_simple("foo") is None

    def test_poisoned_cache_not_reproduced_offline(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        online = _FakeTransport([_FakeResponse(self._HTML_BODY, status=200)])

        async def cold() -> list:
            client = CachedAsyncSimpleClient(online, cache)
            try:
                return await client.get_files("foo")
            finally:
                await client.aclose()

        with pytest.raises(MalformedSimpleResponseError, match="malformed Simple-API"):
            asyncio.run(cold())
        assert len(online.calls) == 1

        offline = _FakeTransport()

        async def later() -> list:
            client = CachedAsyncSimpleClient(offline, cache, offline=True)
            try:
                return await client.get_files("foo")
            finally:
                await client.aclose()

        with pytest.raises(OfflineError, match="foo"):
            asyncio.run(later())
        assert offline.calls == []

    def test_revalidate_non_json_body_preserves_good_cache(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag="old"),
        )
        transport = _FakeTransport([_FakeResponse(self._HTML_BODY, status=200)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        with pytest.raises(MalformedSimpleResponseError, match="malformed Simple-API"):
            asyncio.run(go())
        cached = cache.get_simple("pkg")
        assert cached is not None
        body, _ = cached
        assert body == LISTING_BYTES


class TestNonUtf8ListingBody:
    """A 200 body that is not decodable text must raise a clean error, not crash.

    ``json.loads`` on non-UTF-8 bytes raises :class:`UnicodeDecodeError`,
    not :class:`json.JSONDecodeError`, so it has to be caught too.
    """

    _LATIN1_BODY = '{"files": [], "meta": {"author": "François"}}'.encode("latin-1")

    def test_cold_non_utf8_body_raises_http_error_and_skips_cache(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(self._LATIN1_BODY, status=200)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("foo")
            finally:
                await client.aclose()

        with pytest.raises(
            MalformedSimpleResponseError, match="malformed Simple-API"
        ) as caught:
            asyncio.run(go())
        assert isinstance(caught.value, HttpError)
        assert cache.get_simple("foo") is None

    def test_revalidate_non_utf8_body_preserves_good_cache(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag="old"),
        )
        transport = _FakeTransport([_FakeResponse(self._LATIN1_BODY, status=200)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        with pytest.raises(
            MalformedSimpleResponseError, match="malformed Simple-API"
        ) as caught:
            asyncio.run(go())
        assert isinstance(caught.value, HttpError)
        cached = cache.get_simple("pkg")
        assert cached is not None
        body, _ = cached
        assert body == LISTING_BYTES


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
        assert cache.get_metadata("pkg", "https://x/pkg.metadata") == text

    def test_warm_cache_skips_network(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_metadata("pkg", "https://x/pkg.metadata", "stored")
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

    def test_sibling_wheel_of_same_version_is_not_served(self, tmp_path: Path) -> None:
        """A cached sidecar is not reused for another wheel of the same version.

        The second wheel is fetched and verified against its own digest.
        """
        linux_url = "https://f.example/foo-1.0-cp311-manylinux_2_17_x86_64.whl.metadata"
        win_url = "https://f.example/foo-1.0-cp311-win_amd64.whl.metadata"
        linux_body = b"Metadata-Version: 2.1\nRequires-Dist: linux-only-dep\n"
        win_body = b"Metadata-Version: 2.1\nRequires-Dist: windows-only-dep\n"
        transport = _FakeTransport([_FakeResponse(linux_body), _FakeResponse(win_body)])
        win_digest = hashlib.sha256(win_body).hexdigest()

        async def go() -> str:
            warm = CachedAsyncSimpleClient(transport, _make_cache(tmp_path))
            await warm.get_metadata_text("foo", "1.0", linux_url)
            await warm.aclose()
            # A later process reading the same cache root.
            client = CachedAsyncSimpleClient(transport, _make_cache(tmp_path))
            try:
                return await client.get_metadata_text(
                    "foo", "1.0", win_url, ("sha256", win_digest)
                )
            finally:
                await client.aclose()

        assert asyncio.run(go()) == win_body.decode()
        assert [url for url, _ in transport.calls] == [linux_url, win_url]

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

    def test_matching_hash_returns_and_caches(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        body = b"Metadata-Version: 2.1\nName: pkg\n"
        digest = hashlib.sha256(body).hexdigest()
        transport = _FakeTransport([_FakeResponse(body)])

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata", ("sha256", digest)
                )
            finally:
                await client.aclose()

        text = asyncio.run(go())
        assert text == body.decode()
        assert cache.get_metadata("pkg", "https://x/pkg.metadata") == text

    def test_mismatching_hash_raises_and_skips_cache(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        digest = hashlib.sha256(b"Metadata-Version: 2.1\nName: pkg\n").hexdigest()
        tampered = _FakeResponse(b"Metadata-Version: 2.1\nName: evil\n")
        transport = _FakeTransport([tampered])

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata", ("sha256", digest)
                )
            finally:
                await client.aclose()

        with pytest.raises(MetadataHashMismatchError):
            asyncio.run(go())
        assert cache.get_metadata("pkg", "https://x/pkg.metadata") is None

    def test_returns_utf8_decode_of_verified_bytes(self, tmp_path: Path) -> None:
        """The result is the utf-8 decode of the hash-verified bytes.

        When the transport's ``.text`` would decode the body under a
        different charset, the returned metadata still matches the bytes
        the hash covers.
        """
        cache = _make_cache(tmp_path)
        body = (
            b"Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=2\n"
        )
        digest = hashlib.sha256(body).hexdigest()
        transport = _FakeTransport([_MischarsetResponse(body)])

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_metadata_text(
                    "foo", "1.0", "https://x/foo.metadata", ("sha256", digest)
                )
            finally:
                await client.aclose()

        text = asyncio.run(go())
        assert text == body.decode("utf-8")
        assert cache.get_metadata("foo", "https://x/foo.metadata") == text
        parsed = parse_metadata(text)
        assert [str(req) for req in parsed.requires_dist] == ["bar>=2"]


class TestNonUtf8MetadataSidecar:
    """A hash-valid but non-UTF-8 PEP 658 sidecar raises a clean HttpError.

    The bytes pass the published hash, then fail to decode as utf-8. Like
    ``TestNonUtf8ListingBody`` on the listing path, a body that cannot be
    decoded surfaces as an :class:`HttpError` subclass, not a raw
    :class:`UnicodeDecodeError`.
    """

    _LATIN1_SIDECAR = "Metadata-Version: 2.1\nName: pkg\nSummary: café\n".encode(
        "latin-1"
    )

    def test_hash_valid_non_utf8_raises_http_error_and_skips_cache(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        digest = hashlib.sha256(self._LATIN1_SIDECAR).hexdigest()
        transport = _FakeTransport([_FakeResponse(self._LATIN1_SIDECAR)])

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata", ("sha256", digest)
                )
            finally:
                await client.aclose()

        with pytest.raises(MalformedSimpleResponseError, match="metadata") as caught:
            asyncio.run(go())
        assert isinstance(caught.value, HttpError)
        assert cache.get_metadata("pkg", "https://x/pkg.metadata") is None

    def test_unhashed_non_utf8_raises_http_error_and_skips_cache(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(self._LATIN1_SIDECAR)])

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata"
                )
            finally:
                await client.aclose()

        with pytest.raises(MalformedSimpleResponseError, match="metadata") as caught:
            asyncio.run(go())
        assert isinstance(caught.value, HttpError)
        assert cache.get_metadata("pkg", "https://x/pkg.metadata") is None


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
        assert cache.get_sdist_files("pkg", "1.0") == (pkg_info, pyproject)

    def test_warm_cache_skips_network(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_sdist_files(
            "pkg", "1.0", "Name: cached\n", "[project]\nname = 'cached'\n"
        )
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
        assert cache.get_sdist_files("pkg", "1.0") is None

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

    def test_partial_write_does_not_poison_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash while writing the entry must not leave a partial cache.

        The first fetch fails as the single record is committed; the second
        must still recover the full PKG-INFO plus pyproject.toml pair.
        """
        cache = _make_cache(tmp_path)
        marker = b"partial-write-marker"
        pyproject = b"[project]\nname = 'pkg'\n# " + marker + b"\n"
        body = _build_tarball(
            [
                ("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: pkg\n"),
                ("pkg-1.0/pyproject.toml", pyproject),
            ]
        )
        transport = _FakeTransport([_FakeResponse(body), _FakeResponse(body)])

        real_atomic_write = cache_mod._atomic_write
        fail = {"active": True}

        def flaky_atomic_write(path: Path, data: bytes) -> None:
            if fail["active"] and marker in data:
                msg = "simulated crash committing pyproject"
                raise OSError(msg)
            real_atomic_write(path, data)

        monkeypatch.setattr(cache_mod, "_atomic_write", flaky_atomic_write)

        async def fetch() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz"
                )
            finally:
                await client.aclose()

        with pytest.raises(OSError, match="simulated crash"):
            asyncio.run(fetch())

        fail["active"] = False
        pkg_info, recovered_pyproject = asyncio.run(fetch())
        assert pkg_info is not None
        assert "Name: pkg" in pkg_info
        assert recovered_pyproject is not None
        assert "[project]" in recovered_pyproject

    def test_matching_hash_returns_and_caches(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        body = _build_tarball(
            [("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: pkg\n")]
        )
        digest = hashlib.sha256(body).hexdigest()
        transport = _FakeTransport([_FakeResponse(body)])

        async def go() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz", (("sha256", digest),)
                )
            finally:
                await client.aclose()

        pkg_info, _ = asyncio.run(go())
        assert pkg_info is not None
        assert "Name: pkg" in pkg_info
        assert cache.get_sdist_files("pkg", "1.0") == (pkg_info, None)

    def test_mismatching_hash_raises_and_skips_cache(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        published = _build_tarball(
            [("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: pkg\n")]
        )
        digest = hashlib.sha256(published).hexdigest()
        tampered = _build_tarball(
            [("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: evil\n")]
        )
        transport = _FakeTransport([_FakeResponse(tampered)])

        async def go() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz", (("sha256", digest),)
                )
            finally:
                await client.aclose()

        with pytest.raises(SdistHashMismatchError):
            asyncio.run(go())
        assert cache.get_sdist_files("pkg", "1.0") is None

    def test_no_published_hash_skips_verification(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        body = _build_tarball(
            [("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: pkg\n")]
        )
        transport = _FakeTransport([_FakeResponse(body)])

        async def go() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz", ()
                )
            finally:
                await client.aclose()

        pkg_info, _ = asyncio.run(go())
        assert pkg_info is not None
        assert "Name: pkg" in pkg_info

    def test_only_unacceptable_hash_skips_verification(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        body = _build_tarball(
            [("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\nName: pkg\n")]
        )
        transport = _FakeTransport([_FakeResponse(body)])

        async def go() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz", (("md5", "deadbeef"),)
                )
            finally:
                await client.aclose()

        pkg_info, _ = asyncio.run(go())
        assert pkg_info is not None
        assert "Name: pkg" in pkg_info


class TestGetSdistArchive:
    def test_matching_hash_returns_bytes(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        body = _build_tarball([("pkg-1.0/PKG-INFO", b"Name: pkg\n")])
        digest = hashlib.sha256(body).hexdigest()
        transport = _FakeTransport([_FakeResponse(body)])

        async def go() -> bytes:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_archive(
                    "pkg", "1.0", "https://x/pkg.tar.gz", (("sha256", digest),)
                )
            finally:
                await client.aclose()

        assert asyncio.run(go()) == body

    def test_mismatching_hash_raises(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        published = _build_tarball([("pkg-1.0/PKG-INFO", b"Name: pkg\n")])
        digest = hashlib.sha256(published).hexdigest()
        tampered = _build_tarball([("pkg-1.0/PKG-INFO", b"Name: evil\n")])
        transport = _FakeTransport([_FakeResponse(tampered)])

        async def go() -> bytes:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_archive(
                    "pkg", "1.0", "https://x/pkg.tar.gz", (("sha256", digest),)
                )
            finally:
                await client.aclose()

        with pytest.raises(SdistHashMismatchError):
            asyncio.run(go())

    def test_no_published_hash_returns_bytes(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        body = _build_tarball([("pkg-1.0/PKG-INFO", b"Name: pkg\n")])
        transport = _FakeTransport([_FakeResponse(body)])

        async def go() -> bytes:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_archive(
                    "pkg", "1.0", "https://x/pkg.tar.gz", ()
                )
            finally:
                await client.aclose()

        assert asyncio.run(go()) == body

    def test_offline_raises(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([])

        async def go() -> bytes:
            client = CachedAsyncSimpleClient(transport, cache, offline=True)
            try:
                return await client.get_sdist_archive(
                    "pkg", "1.0", "https://x/pkg.tar.gz", ()
                )
            finally:
                await client.aclose()

        with pytest.raises(OfflineError):
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
