"""Tests for nab_index.cached_client.CachedAsyncSimpleClient."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import json
import logging
import re
import sys
import tarfile
import time
import zipfile
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import replace
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import pytest
from packaging.utils import canonicalize_name
from urllib3 import HTTPHeaderDict

import nab_index.cache as cache_mod
import nab_index.cached_client as cached_client_mod
from nab_index._pep503 import json_listing
from nab_index.cache import CachePolicy, NullCache, OfflineError, OnDiskCache
from nab_index.cached_client import (
    CachedAsyncSimpleClient,
    ParsedCacheStats,
    SdistArchiveHold,
    _freshness_lifetime,
    _header,
    _max_age_directive,
    _parse_age,
)
from nab_index.client import (
    MalformedSimpleResponseError,
    MetadataHashMismatchError,
    SdistFile,
    SdistHashMismatchError,
    WheelFile,
    WheelHashMismatchError,
    _normalized_url,
    _parse_files,
    _parse_sdist_filename,
)
from nab_index.lazy_wheel import RangeMetadataResult, RangeOutcome
from nab_index.parsed_listing import encode as encode_parsed
from nab_index.transport import HttpError
from nab_provider.metadata import parse_metadata
from nab_provider.records import (
    parse_hash_table,
    select_artifact_hash,
    sidecar_hash,
)
from nab_provider.serialization import SimpleSerialization

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

# The same page after a second release lands on it.
RELISTING_BYTES = json.dumps(
    {
        "meta": {"api-version": "1.0"},
        "name": "pkg",
        "files": [
            *LISTING["files"],
            {
                "filename": "pkg-2.0-py3-none-any.whl",
                "url": "https://files.example.com/pkg-2.0-py3-none-any.whl",
            },
        ],
    }
).encode()

# A page whose only file is in a format nab does not read.
ZIP_ONLY_LISTING = {
    "meta": {"api-version": "1.0"},
    "name": "pkg",
    "files": [
        {
            "filename": "pkg-1.0.zip",
            "url": "https://files.example.com/pkg-1.0.zip",
            "hashes": {"sha256": "deadbeef"},
        },
    ],
}
ZIP_ONLY_BYTES = json.dumps(ZIP_ONLY_LISTING).encode()

# A release published as a wheel and a .zip sdist, so the parse keeps one file
# and drops the other.
WHEEL_AND_ZIP_LISTING = {
    "meta": {"api-version": "1.0"},
    "name": "pkg",
    "files": [
        *LISTING["files"],
        {
            "filename": "pkg-1.0.zip",
            "url": "https://files.example.com/pkg-1.0.zip",
        },
    ],
}
WHEEL_AND_ZIP_BYTES = json.dumps(WHEEL_AND_ZIP_LISTING).encode()

# A digit run just past CPython's int-from-string limit.
OVERSIZED_DIGITS = "9" * (sys.get_int_max_str_digits() + 1)

# Stands in for a body nested past the decoder's guard (``refuse_over_nested``).
OVER_NESTED = b"[[[]]]"


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        url: str = "",
    ) -> None:
        self.content = body
        self.status_code = status
        self.headers = headers or {}
        # Empty means the transport fills in the requested URL. Set it to
        # stand in for a page the index redirected to.
        self.url = url

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
        response = self._responses.pop(0)
        if not response.url:
            response.url = url
        return response

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


def _field_lines(*lines: tuple[str, str]) -> HTTPHeaderDict:
    """Headers holding one entry per field line, the shape urllib3 returns."""
    headers = HTTPHeaderDict()
    for name, value in lines:
        headers.add(name, value)
    return headers


def _make_cache(tmp_path: Path) -> OnDiskCache:
    return OnDiskCache(tmp_path, "https://pypi.org/simple/")


class _MemoryPolicyCache(OnDiskCache):
    """Cache that returns policies from memory, skipping the sidecar codec."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, "https://pypi.org/simple/")
        self._policies: dict[str, CachePolicy] = {}

    def put_simple(self, package: str, body: bytes, policy: CachePolicy) -> str | None:
        digest = super().put_simple(package, body, policy)
        self._policies[package] = replace(policy, body_digest=digest)
        return digest

    def get_simple(self, package: str) -> tuple[bytes, CachePolicy] | None:
        entry = super().get_simple(package)
        if entry is None:
            return None
        return entry[0], self._policies.get(package, entry[1])


def _build_tarball(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _parsed_wheel(file_info: Mapping[Any, object]) -> WheelFile:
    """The ``WheelFile`` ingest builds for an entry carrying ``file_info``."""
    entry: dict[Any, object] = {
        "filename": "foo-1.0-py3-none-any.whl",
        "url": "https://example.com/foo-1.0-py3-none-any.whl",
        **file_info,
    }
    (wheel,) = _parse_files({"files": [entry]}, "https://example.com/", "foo")
    assert isinstance(wheel, WheelFile)
    return wheel


def _has_metadata(file_info: Mapping[Any, object]) -> bool:
    """Whether ``file_info`` advertises a sidecar, read the way ingest reads it."""
    return _parsed_wheel(file_info).has_metadata


def _metadata_hash(file_info: Mapping[Any, object]) -> tuple[str, str] | None:
    """The sidecar hash ``file_info`` publishes, read the way ingest reads it."""
    return _parsed_wheel(file_info).metadata_hash


class TestHasMetadataFlag:
    """PEP 691 boolean variants of ``core-metadata`` / ``dist-info-metadata``."""

    def test_dict_value_advertises_metadata(self) -> None:
        assert _has_metadata({"core-metadata": {"sha256": "abc"}})

    def test_true_value_advertises_metadata(self) -> None:
        assert _has_metadata({"core-metadata": True})

    def test_legacy_json_key_true(self) -> None:
        assert _has_metadata({"dist-info-metadata": True})

    def test_false_value_does_not_advertise(self) -> None:
        assert not _has_metadata({"core-metadata": False})

    def test_missing_field(self) -> None:
        assert not _has_metadata({})

    def test_core_metadata_false_suppresses_legacy_key(self) -> None:
        assert not _has_metadata(
            {"core-metadata": False, "dist-info-metadata": {"sha256": "deadbeef"}}
        )

    def test_core_metadata_true_ignores_legacy_key(self) -> None:
        assert _has_metadata({"core-metadata": True, "dist-info-metadata": False})


class TestMetadataHashParsing:
    """``_metadata_hash`` carries the published hash to verify, or None."""

    def test_sha256_lowercased(self) -> None:
        assert _metadata_hash({"core-metadata": {"sha256": "ABCD"}}) == (
            "sha256",
            "abcd",
        )

    def test_uppercase_algo_name(self) -> None:
        assert _metadata_hash({"core-metadata": {"SHA256": "ABCD"}}) == (
            "sha256",
            "abcd",
        )

    def test_legacy_key_used(self) -> None:
        assert _metadata_hash({"dist-info-metadata": {"sha256": "ab"}}) == (
            "sha256",
            "ab",
        )

    def test_true_value_yields_none(self) -> None:
        assert _metadata_hash({"core-metadata": True}) is None

    def test_other_algo_only_yields_none(self) -> None:
        assert _metadata_hash({"core-metadata": {"blake2b": "ab"}}) is None

    def test_sha512_only_verified(self) -> None:
        assert _metadata_hash({"core-metadata": {"sha512": "ABCD"}}) == (
            "sha512",
            "abcd",
        )

    def test_sha384_only_verified(self) -> None:
        assert _metadata_hash({"core-metadata": {"sha384": "ABCD"}}) == (
            "sha384",
            "abcd",
        )

    def test_sha256_preferred_over_sha512(self) -> None:
        assert _metadata_hash(
            {"core-metadata": {"sha512": "aaaa", "sha256": "bbbb"}}
        ) == ("sha256", "bbbb")

    def test_sha384_preferred_over_sha512(self) -> None:
        assert _metadata_hash(
            {"core-metadata": {"sha512": "aaaa", "sha384": "bbbb"}}
        ) == ("sha384", "bbbb")

    def test_unsupported_with_supported_skips_unsupported(self) -> None:
        assert _metadata_hash(
            {"core-metadata": {"blake2b": "aaaa", "sha512": "bbbb"}}
        ) == ("sha512", "bbbb")

    def test_missing_field_yields_none(self) -> None:
        assert _metadata_hash({}) is None

    def test_core_metadata_false_ignores_legacy_hash(self) -> None:
        assert (
            _metadata_hash(
                {"core-metadata": False, "dist-info-metadata": {"sha256": "deadbeef"}}
            )
            is None
        )

    def test_core_metadata_true_ignores_legacy_hash(self) -> None:
        assert (
            _metadata_hash(
                {"core-metadata": True, "dist-info-metadata": {"sha256": "cafef00d"}}
            )
            is None
        )

    def test_core_metadata_hash_preferred_over_legacy(self) -> None:
        assert _metadata_hash(
            {
                "core-metadata": {"sha256": "AAAA"},
                "dist-info-metadata": {"sha256": "BBBB"},
            }
        ) == ("sha256", "aaaa")

    def test_ingest_holds_the_tables_until_a_reader_asks(self) -> None:
        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "https://example.com/foo-1.0-py3-none-any.whl",
                    "hashes": {"SHA256": "A" * 64},
                    "core-metadata": {"sha256": "b" * 64},
                },
                {
                    "filename": "foo-1.0.tar.gz",
                    "url": "https://example.com/foo-1.0.tar.gz",
                    "hashes": {"sha256": "c" * 64},
                },
            ],
        }

        wheel, sdist = _parse_files(data, "https://example.com/simple/", "foo")

        assert wheel.raw_hashes() == {"SHA256": "A" * 64}
        assert wheel.raw_sidecar() == {"sha256": "b" * 64}
        assert sdist.raw_hashes() == {"sha256": "c" * 64}

        assert wheel.hashes == (("sha256", "a" * 64),)
        assert wheel.metadata_hash == ("sha256", "b" * 64)
        assert sdist.hashes == (("sha256", "c" * 64),)

    def test_parse_files_populates_metadata_hash(self) -> None:
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
        assert _metadata_hash({"core-metadata": {"sha256": ""}}) is None

    def test_non_hex_digest_yields_none(self) -> None:
        assert _metadata_hash({"core-metadata": {"sha256": "not-a-digest"}}) is None

    def test_empty_digest_falls_through_to_valid_algo(self) -> None:
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

    def test_relative_url_resolves_against_the_page_that_was_served(self) -> None:
        """A redirect moves the project page, and with it the base."""
        data = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": "foo-1.0-py3-none-any.whl",
                },
            ],
        }
        files = _parse_files(
            data,
            "https://example.com/simple/",
            "foo",
            page_url="https://example.com/pypi/simple/foo/",
        )
        expected = "https://example.com/pypi/simple/foo/foo-1.0-py3-none-any.whl"
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


class TestControlCharacterUrl:
    """A file URL is stored without the tab, CR and LF bytes urlsplit removes."""

    _PAGE = "https://example.com/simple/foo/"
    _STRIPPED = "https://files.example.com/ab/foo-1.0-py3-none-any.whl"

    def _entry(self, url: str) -> dict[str, object]:
        """A PEP 691 wheel entry for ``url`` that advertises a metadata sidecar."""
        return {
            "filename": "foo-1.0-py3-none-any.whl",
            "url": url,
            "core-metadata": True,
        }

    @pytest.mark.parametrize("control", ["\t", "\r", "\n"], ids=["tab", "cr", "lf"])
    def test_artifact_and_sidecar_name_the_same_file(self, control: str) -> None:
        raw = f"https://files.example.com/a{control}b/foo-1.0-py3-none-any.whl"
        data = {"files": [self._entry(raw)]}

        (wheel,) = _parse_files(data, "https://example.com/simple/", "foo")

        assert isinstance(wheel, WheelFile)
        assert wheel.url == self._STRIPPED
        assert wheel.metadata_url == f"{self._STRIPPED}.metadata"

    def test_different_scheme_url_is_stripped(self) -> None:
        """urljoin returns an href verbatim when its scheme differs from the page's."""
        raw = "ftp://files.example.com/a\tb/foo-1.0.tar.gz"
        data = {"files": [{"filename": "foo-1.0.tar.gz", "url": raw}]}

        (sdist,) = _parse_files(data, "https://example.com/simple/", "foo")

        assert sdist.url == "ftp://files.example.com/ab/foo-1.0.tar.gz"

    def test_html_and_json_serializations_agree(self) -> None:
        """The HTML and JSON forms of one page yield the same file URL."""
        raw = "https://files.example.com/a\nb/foo-1.0-py3-none-any.whl"
        html_listing = json.loads(json_listing(f'<a href="{raw}">foo</a>', self._PAGE))

        (from_json,) = _parse_files(
            {"files": [self._entry(raw)]},
            "https://example.com/simple/",
            "foo",
            page_url=self._PAGE,
        )
        (from_html,) = _parse_files(
            html_listing, "https://example.com/simple/", "foo", page_url=self._PAGE
        )

        assert from_json.url == from_html.url == self._STRIPPED


class TestNormalizedUrl:
    """_normalized_url returns the same string as the split round trip."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://files.example.com/ab/foo-1.0-py3-none-any.whl",
            "https://files.example.com/foo-1.0-py3-none-any.whl?token=x",
            "https://files.example.com/foo-1.0-py3-none-any.whl#sha256=abc",
            "//files.example.com/foo-1.0-py3-none-any.whl",
            "file:///mirror/foo-1.0-py3-none-any.whl",
            "file:/mirror/foo-1.0-py3-none-any.whl",
            "https://files.example.com/a\tb/foo-1.0-py3-none-any.whl",
            "https://files.example.com/foo-1.0-py3-none-any.whl?",
            "https://files.example.com/foo-1.0-py3-none-any.whl#",
            "HTTPS://files.example.com/foo-1.0-py3-none-any.whl",
            "https://Files.Example.com/foo-1.0-py3-none-any.whl",
            "https:foo-1.0-py3-none-any.whl",
        ],
    )
    def test_matches_round_trip(self, url: str) -> None:
        assert _normalized_url(url) == urlunsplit(urlsplit(url))

    def test_clean_url_is_not_rebuilt(self) -> None:
        url = "https://files.example.com/ab/foo-1.0-py3-none-any.whl"
        assert _normalized_url(url) is url

    def test_malformed_authority_raises(self) -> None:
        with pytest.raises(ValueError, match="IPv6"):
            _normalized_url("https://[::1/foo-1.0-py3-none-any.whl")


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
        assert parse_hash_table({"sha256": "a" * 64}) == (("sha256", "a" * 64),)

    def test_single_entry_malformed(self) -> None:
        assert parse_hash_table({"sha256": 123}) == ()

    def test_multiple_entries(self) -> None:
        result = parse_hash_table({"sha256": "a" * 64, "md5": "b" * 32})
        assert result == (("sha256", "a" * 64), ("md5", "b" * 32))

    def test_multiple_entries_skips_malformed(self) -> None:
        result = parse_hash_table({"sha256": "a" * 64, "md5": 123})
        assert result == (("sha256", "a" * 64),)

    def test_non_dict(self) -> None:
        assert parse_hash_table("sha256:abc") == ()

    def test_empty_dict(self) -> None:
        assert parse_hash_table({}) == ()

    def test_single_empty_digest_dropped(self) -> None:
        assert parse_hash_table({"sha256": ""}) == ()

    def test_empty_digest_falls_through_to_valid(self) -> None:
        assert parse_hash_table({"sha256": "", "sha512": "f" * 128}) == (
            ("sha512", "f" * 128),
        )

    def test_empty_digest_yields_no_sdist_check(self) -> None:
        assert select_artifact_hash(parse_hash_table({"sha256": ""})) is None

    def test_single_non_hex_digest_dropped(self) -> None:
        assert parse_hash_table({"sha256": "not-a-digest"}) == ()

    def test_hex_digest_split_by_whitespace_dropped(self) -> None:
        assert parse_hash_table({"sha256": "0123abcd\nbeef"}) == ()

    def test_non_hex_digest_falls_through_to_valid(self) -> None:
        assert parse_hash_table({"sha256": "deadbeef ", "sha512": "f" * 128}) == (
            ("sha512", "f" * 128),
        )

    def test_uppercase_digest_kept_lowercased(self) -> None:
        assert parse_hash_table({"sha256": "A" * 64}) == (("sha256", "a" * 64),)

    def test_bare_true_publishes_no_sidecar_hash(self) -> None:
        assert sidecar_hash(True) is None  # noqa: FBT003


class TestSelectArtifactHash:
    def test_prefers_sha256(self) -> None:
        hashes = (("sha512", "f" * 128), ("sha256", "a" * 64))
        assert select_artifact_hash(hashes) == ("sha256", "a" * 64)

    def test_falls_through_to_sha384(self) -> None:
        hashes = (("sha512", "f" * 128), ("sha384", "b" * 96))
        assert select_artifact_hash(hashes) == ("sha384", "b" * 96)

    def test_falls_through_to_sha512(self) -> None:
        assert select_artifact_hash((("sha512", "f" * 128),)) == (
            "sha512",
            "f" * 128,
        )

    def test_empty_returns_none(self) -> None:
        assert select_artifact_hash(()) is None

    def test_only_unacceptable_returns_none(self) -> None:
        assert select_artifact_hash((("md5", "d" * 32),)) is None

    def test_empty_digest_returns_none(self) -> None:
        assert select_artifact_hash((("sha256", ""),)) is None

    def test_empty_digest_falls_through_to_valid_algo(self) -> None:
        hashes = (("sha256", ""), ("sha512", "f" * 128))
        assert select_artifact_hash(hashes) == ("sha512", "f" * 128)


class TestMaxAgeDirective:
    def test_none_when_field_absent(self) -> None:
        assert _max_age_directive(None) is None

    def test_none_when_directive_absent(self) -> None:
        assert _max_age_directive("public") is None

    def test_extracts_value(self) -> None:
        assert _max_age_directive("max-age=900, public") == 900

    def test_extracts_value_with_spaces(self) -> None:
        assert _max_age_directive("public, max-age = 1200") == 1200

    def test_leading_zeros_ignored(self) -> None:
        assert _max_age_directive("max-age=00000000900") == 900

    def test_above_ceiling_clamped(self) -> None:
        assert _max_age_directive("max-age=9999999999") == 2**31

    def test_digit_run_too_long_to_convert_clamped(self) -> None:
        assert _max_age_directive("max-age=" + "9" * 9000) == 2**31

    def test_directive_name_is_case_insensitive(self) -> None:
        assert _max_age_directive("Max-Age=0") == 0

    def test_uppercase_directive_among_others(self) -> None:
        assert _max_age_directive("public, MAX-AGE = 30, must-revalidate") == 30

    def test_quoted_value_extracted(self) -> None:
        assert _max_age_directive('max-age="300"') == 300


class TestFreshnessLifetime:
    """Freshness lifetime read from one response's headers."""

    _NOW = 1_700_000_000.0

    @pytest.fixture(autouse=True)
    def _frozen_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(time, "time", lambda: self._NOW)

    def _lifetime(self, headers: Mapping[str, str]) -> int:
        return _freshness_lifetime(_FakeResponse(b"", headers=headers))

    def _http_date(self, offset: float) -> str:
        return formatdate(self._NOW + offset, usegmt=True)

    def test_heuristic_when_response_states_nothing(self) -> None:
        assert self._lifetime({}) == 600

    def test_heuristic_when_cache_control_carries_no_max_age(self) -> None:
        assert self._lifetime({"cache-control": "public"}) == 600

    def test_max_age_read_from_a_second_cache_control_line(self) -> None:
        lines = _field_lines(
            ("Cache-Control", "public"), ("Cache-Control", "max-age=60")
        )
        assert self._lifetime(lines) == 60

    def test_max_age_outranks_expires(self) -> None:
        headers = {
            "cache-control": "max-age=30",
            "date": self._http_date(0),
            "expires": self._http_date(86400),
        }
        assert self._lifetime(headers) == 30

    def test_expires_measured_from_date(self) -> None:
        headers = {"date": self._http_date(-1000), "expires": self._http_date(800)}
        assert self._lifetime(headers) == 1800

    def test_expires_measured_from_arrival_without_date(self) -> None:
        assert self._lifetime({"expires": self._http_date(45)}) == 45

    def test_unparseable_date_falls_back_to_arrival(self) -> None:
        headers = {"date": "whenever", "expires": self._http_date(45)}
        assert self._lifetime(headers) == 45

    def test_expires_before_date_is_already_expired(self) -> None:
        headers = {"date": self._http_date(0), "expires": self._http_date(-1)}
        assert self._lifetime(headers) == 0

    def test_expires_zero_is_already_expired(self) -> None:
        assert self._lifetime({"expires": "0"}) == 0

    def test_unparseable_expires_is_already_expired(self) -> None:
        assert self._lifetime({"expires": "tomorrow, maybe"}) == 0

    def test_out_of_range_year_is_already_expired(self) -> None:
        assert self._lifetime({"expires": "Mon, 01 Jan 99999 00:00:00 GMT"}) == 0

    @pytest.mark.parametrize(
        "expires",
        [
            "Mon, 01 Jan 2030 00:00:00 +99999999999999999999999",
            "Mon, 01 Jan 9999999999999999999999999 00:00:00 GMT",
            "Mon, 99999999999999999999 Jan 2030 00:00:00 GMT",
            "Mon, 01 Jan 2030 99999999999999999999:00:00 GMT",
        ],
    )
    def test_overflowing_expires_is_already_expired(self, expires: str) -> None:
        assert self._lifetime({"expires": expires}) == 0

    def test_overflowing_date_falls_back_to_arrival(self) -> None:
        headers = {
            "date": "Mon, 01 Jan 2030 00:00:00 +99999999999999999999999",
            "expires": self._http_date(45),
        }
        assert self._lifetime(headers) == 45

    def test_zoneless_expires_reads_as_gmt(self) -> None:
        asctime = time.asctime(time.gmtime(self._NOW + 120))
        assert self._lifetime({"expires": asctime}) == 120

    def test_far_future_expires_clamped(self) -> None:
        assert self._lifetime({"expires": "Fri, 31 Dec 9999 23:59:59 GMT"}) == 2**31


class TestNoReuseDirectives:
    """A response that bars reuse gets no freshness window of its own."""

    _NOW = 1_700_000_000.0

    @pytest.fixture(autouse=True)
    def _frozen_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(time, "time", lambda: self._NOW)

    def _lifetime(self, cache_control: str) -> int:
        return _freshness_lifetime(
            _FakeResponse(b"", headers={"cache-control": cache_control})
        )

    def test_no_cache_has_no_window(self) -> None:
        assert self._lifetime("no-cache") == 0

    def test_full_directive_list_has_no_window(self) -> None:
        assert self._lifetime("no-cache,no-store,must-revalidate") == 0

    def test_no_cache_outranks_max_age(self) -> None:
        assert self._lifetime("max-age=600, no-cache") == 0

    def test_no_cache_is_case_insensitive(self) -> None:
        assert self._lifetime("public, No-Cache") == 0

    def test_qualified_no_cache_reads_as_bare(self) -> None:
        """RFC 9111 5.2.2.4 allows field names; nab revalidates either way."""
        assert self._lifetime('no-cache="set-cookie", max-age=600') == 0

    def test_no_store_has_no_window(self) -> None:
        assert self._lifetime("no-store") == 0

    def test_longer_directive_ending_in_no_cache_is_ignored(self) -> None:
        assert self._lifetime("x-no-cache, max-age=300") == 300

    def test_longer_directive_ending_in_no_store_is_ignored(self) -> None:
        assert self._lifetime("x-no-store, max-age=300") == 300

    def test_expires_is_not_consulted(self) -> None:
        response = _FakeResponse(
            b"",
            headers={
                "cache-control": "no-cache",
                "expires": formatdate(self._NOW + 86400, usegmt=True),
            },
        )
        assert _freshness_lifetime(response) == 0


class TestParseAge:
    def test_absent_is_zero(self) -> None:
        assert _parse_age(None) == 0

    def test_extracts_value(self) -> None:
        assert _parse_age("472") == 472

    def test_surrounding_space_tolerated(self) -> None:
        assert _parse_age(" 472 ") == 472

    def test_non_numeric_is_zero(self) -> None:
        assert _parse_age("a while") == 0

    def test_negative_is_zero(self) -> None:
        assert _parse_age("-5") == 0

    def test_above_ceiling_clamped(self) -> None:
        assert _parse_age("9999999999") == 2**31

    def test_digit_run_too_long_to_convert_clamped(self) -> None:
        assert _parse_age("9" * 9000) == 2**31

    def test_leading_zeros_ignored(self) -> None:
        assert _parse_age("00000000472") == 472

    def test_all_zeros_is_zero(self) -> None:
        assert _parse_age("0" * 9000) == 0

    def test_padded_value_above_ceiling_clamped(self) -> None:
        assert _parse_age("0" * 9000 + "9" * 20) == 2**31


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

    def test_line_folded_value_is_unfolded(self) -> None:
        """RFC 9112 5.2: a receiver replaces a line fold with a space."""
        resp = _FakeResponse(b"", headers={"etag": '"abc\r\n def"'})
        assert _header(resp, "etag") == '"abc def"'

    def test_repeated_field_lines_are_combined(self) -> None:
        """RFC 9110 5.3: repeated lines are one value, joined in order."""
        resp = _FakeResponse(
            b"",
            headers=_field_lines(
                ("Cache-Control", "public"),
                ("Cache-Control", "max-age=60"),
            ),
        )
        assert _header(resp, "cache-control") == "public, max-age=60"

    def test_combined_lines_are_unfolded(self) -> None:
        resp = _FakeResponse(
            b"",
            headers=_field_lines(
                ("Cache-Control", "public,\r\n max-age=60"),
                ("Cache-Control", "no-transform"),
            ),
        )
        assert _header(resp, "cache-control") == "public, max-age=60, no-transform"

    def test_repeated_singleton_field_keeps_the_first_line(self) -> None:
        """A field that is not defined as a list is not combined."""
        resp = _FakeResponse(
            b"",
            headers=_field_lines(
                ("Content-Type", "text/html"),
                ("Content-Type", "text/html"),
            ),
        )
        assert _header(resp, "content-type") == "text/html"


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

    def test_uppercase_max_age_directive_is_stored(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={"etag": "v1", "cache-control": "public, Max-Age=0"},
                ),
                _FakeResponse(b"", status=304, headers={"etag": "v1"}),
            ]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1
        stored = _make_cache(tmp_path).get_simple("pkg")
        assert stored is not None
        assert stored[1].max_age == 0

        assert len(_run_get_files(transport, cache, "pkg")) == 1
        assert len(transport.calls) == 2

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

    def test_304_over_a_zip_only_listing_reports_the_format(
        self, tmp_path: Path
    ) -> None:
        """A 304 keeps the cached body, so the format report comes from it."""
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", ZIP_ONLY_BYTES, _stale_etag_policy())
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "v1"})]
        )

        files, unreadable_only = _get_files_and_report(transport, cache, "pkg")

        assert files == []
        assert unreadable_only

    def test_200_over_a_zip_only_listing_reports_the_fresh_body(
        self, tmp_path: Path
    ) -> None:
        """The replacement body decides the format report, not the one it replaced."""
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", ZIP_ONLY_BYTES, _stale_etag_policy())
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES)])

        files, unreadable_only = _get_files_and_report(transport, cache, "pkg")

        assert [file.filename for file in files] == ["pkg-1.0-py3-none-any.whl"]
        assert not unreadable_only

    def test_404_over_a_zip_only_listing_reports_the_absent_name(
        self, tmp_path: Path
    ) -> None:
        """A 404 drops the cached body, so the name reads absent, not unreadable."""
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", ZIP_ONLY_BYTES, _stale_etag_policy())
        transport = _FakeTransport([_FakeResponse(b"", status=404)])

        files, unreadable_only = _get_files_and_report(transport, cache, "pkg")

        assert files == []
        assert not unreadable_only
        assert cache.get_negative("pkg") is not None

    def test_zip_sdist_beside_a_wheel_names_its_release(self, tmp_path: Path) -> None:
        """The wheel survives the parse, so only the version records the ``.zip``."""
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(WHEEL_AND_ZIP_BYTES)])

        files, zip_sdists = _get_files_and_zip_sdists(transport, cache, "pkg")

        assert [file.filename for file in files] == ["pkg-1.0-py3-none-any.whl"]
        assert zip_sdists == frozenset({"1.0"})

    def test_readable_listing_names_no_release(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES)])

        _, zip_sdists = _get_files_and_zip_sdists(transport, cache, "pkg")

        assert zip_sdists == frozenset()

    def test_zip_sdist_survives_a_cached_read(self, tmp_path: Path) -> None:
        """The blob names the dropped release, so a warm read reports it too.

        No record survives the parse to say the release published an sdist, so
        a blob holding records alone would report it as never published.
        """
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(WHEEL_AND_ZIP_BYTES)])
        _get_files_and_zip_sdists(transport, cache, "pkg")

        stats = ParsedCacheStats()
        offline = _FakeTransport()
        files, zip_sdists = _get_files_and_zip_sdists(
            offline, cache, "pkg", parsed_stats=stats
        )

        assert offline.calls == []
        assert stats.hit == 1
        assert [file.filename for file in files] == ["pkg-1.0-py3-none-any.whl"]
        assert zip_sdists == frozenset({"1.0"})

    def test_a_seeded_blob_names_the_dropped_release(self, tmp_path: Path) -> None:
        """A blob already on disk answers the read, dropped releases and all."""
        cache = _make_cache(tmp_path)
        policy = CachePolicy(fetched_at=int(time.time()), max_age=99999, etag=None)
        digest = cache.put_simple("pkg", WHEEL_AND_ZIP_BYTES, policy)
        assert digest is not None

        files = _parse_files(
            json.loads(WHEEL_AND_ZIP_BYTES), "https://pypi.org/simple/", "pkg"
        )
        cache.put_simple_parsed("pkg", encode_parsed(files, digest, frozenset({"1.0"})))

        stats = ParsedCacheStats()
        _, zip_sdists = _get_files_and_zip_sdists(
            _FakeTransport(), cache, "pkg", parsed_stats=stats
        )

        assert stats.hit == 1
        assert zip_sdists == frozenset({"1.0"})

    def test_304_parses_the_cached_body_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 304 path parses the body it serves, and parses it once."""
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", LISTING_BYTES, _stale_etag_policy())
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "v1"})]
        )

        parsed: list[str] = []
        real_parse_files = cached_client_mod._parse_files

        def recording_parse_files(
            data: object, index_url: str, package: str, *, page_url: str | None = None
        ) -> list:
            parsed.append(package)
            return real_parse_files(data, index_url, package, page_url=page_url)

        monkeypatch.setattr(cached_client_mod, "_parse_files", recording_parse_files)
        files = _run_get_files(transport, cache, "pkg")

        assert [file.filename for file in files] == ["pkg-1.0-py3-none-any.whl"]
        assert parsed == ["pkg"]

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

    def test_stale_revalidates_non_ascii_etag_unconditionally(
        self, tmp_path: Path
    ) -> None:
        """An entity tag holding obs-text cannot go back in a request header."""
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag='"é"'),
        )
        transport = _FakeTransport(
            [_FakeResponse(LISTING_BYTES, headers={"etag": '"é"'})]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1

        sent = transport.calls[0][1]
        assert sent is not None
        assert "If-None-Match" not in sent

        cached = cache.get_simple("pkg")
        assert cached is not None
        assert cached[1].etag is None

    def test_stale_skips_non_ascii_etag_kept_by_backend(self, tmp_path: Path) -> None:
        """A backend that keeps the tag on read still must not send it."""
        cache = _MemoryPolicyCache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag='"é"'),
        )
        transport = _FakeTransport(
            [_FakeResponse(LISTING_BYTES, headers={"etag": '"ok"'})]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1

        sent = transport.calls[0][1]
        assert sent is not None
        assert "If-None-Match" not in sent

    def test_line_folded_etag_revalidates_on_one_line(self, tmp_path: Path) -> None:
        """A tag that arrives line-folded is stored and sent back on one line."""
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={"etag": '"abc\r\n def"', "cache-control": "max-age=0"},
                ),
                _FakeResponse(b"", status=304, headers={}),
            ]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1
        stored = cache.get_simple("pkg")
        assert stored is not None
        assert stored[1].etag == '"abc def"'

        assert len(_run_get_files(transport, cache, "pkg")) == 1
        sent = transport.calls[1][1]
        assert sent is not None
        assert sent["If-None-Match"] == '"abc def"'

    def test_stale_skips_folded_etag_kept_by_backend(self, tmp_path: Path) -> None:
        """A folded tag a backend keeps on read still must not be sent."""
        cache = _MemoryPolicyCache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag='"abc\r\n def"'),
        )
        transport = _FakeTransport(
            [_FakeResponse(LISTING_BYTES, headers={"etag": '"ok"'})]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1

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
        # A 404 writes a negative sentinel, never a positive entry.
        assert cache.get_simple("absent") is None
        assert cache.get_negative("absent") is not None

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

    def test_unwritable_cache_root_still_serves_the_listing(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "cache"
        root.write_bytes(b"not a directory")
        cache = OnDiskCache(root, "https://pypi.org/simple/")
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        files = asyncio.run(go())
        assert len(files) == 1

    def test_unwritable_parsed_bucket_still_serves_a_warm_listing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A refused blob write still serves the listing from the cached body.

        A fresh body with no blob beside it rebuilds one on every read, so the
        refusal repeats and the run warns about it once.
        """
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", LISTING_BYTES, _fresh_policy())

        bucket = tmp_path / f"simple-parsed-{cache_mod.CACHE_VERSION_SIMPLE_PARSED}"
        bucket.write_bytes(b"not a directory")

        async def go() -> tuple[list, list]:
            client = CachedAsyncSimpleClient(_FakeTransport(), cache)
            try:
                return (await client.get_files("pkg"), await client.get_files("pkg"))
            finally:
                await client.aclose()

        with caplog.at_level(logging.WARNING, logger="nab_index.cached_client"):
            first, second = asyncio.run(go())

        assert [f.filename for f in first] == ["pkg-1.0-py3-none-any.whl"]
        assert [f.filename for f in second] == [f.filename for f in first]

        assert len(_cached_warnings(caplog)) == 1


class TestResponseAge:
    """An Age header counts toward the entry's age (RFC 9111 4.2.3).

    A shared cache in front of the index reports how long ago the origin
    generated the representation, so the window opened before nab's receipt.
    """

    def test_cold_fetch_opens_window_at_origin_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={
                        "etag": "v1",
                        "cache-control": "max-age=600, must-revalidate",
                        "age": "472",
                    },
                )
            ]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1

        cached = cache.get_simple("pkg")
        assert cached is not None
        _, policy = cached
        assert policy.fetched_at == 528
        assert policy.is_fresh(now=1127) is True
        assert policy.is_fresh(now=1128) is False

    def test_relayed_entry_revalidates_before_receipt_window_ends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path)
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cold = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={
                        "etag": "v1",
                        "cache-control": "max-age=600",
                        "age": "472",
                    },
                )
            ]
        )
        assert len(_run_get_files(cold, cache, "pkg")) == 1

        monkeypatch.setattr(time, "time", lambda: 1300.0)
        warm = _FakeTransport([_FakeResponse(b"", status=304, headers={"etag": "v1"})])
        assert len(_run_get_files(warm, cache, "pkg")) == 1

        assert len(warm.calls) == 1
        sent_headers = warm.calls[0][1]
        assert sent_headers is not None
        assert sent_headers.get("If-None-Match") == "v1"

    def test_no_age_header_opens_window_at_receipt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={"etag": "v1", "cache-control": "max-age=600"},
                )
            ]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1

        cached = cache.get_simple("pkg")
        assert cached is not None
        _, policy = cached
        assert policy.fetched_at == 1000

    def test_304_refresh_backdates_by_age(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 5000.0)
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=60, etag="v1"),
        )
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "v1", "age": "30"})]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1

        cached = cache.get_simple("pkg")
        assert cached is not None
        _, policy = cached
        assert policy.fetched_at == 4970
        assert policy.max_age == 60

    def test_revalidated_200_backdates_by_age(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 5000.0)
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=60, etag="v1"),
        )
        transport = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={
                        "etag": "v2",
                        "cache-control": "max-age=600",
                        "age": "90",
                    },
                )
            ]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1

        cached = cache.get_simple("pkg")
        assert cached is not None
        _, policy = cached
        assert policy.fetched_at == 4910
        assert policy.etag == "v2"

    def test_404_sentinel_backdates_by_age(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    b"",
                    status=404,
                    headers={"cache-control": "max-age=600", "age": "500"},
                )
            ]
        )

        assert _run_get_files(transport, cache, "absent") == []

        neg = cache.get_negative("absent")
        assert neg is not None
        assert neg.fetched_at == 500
        assert neg.is_fresh(now=1099) is True
        assert neg.is_fresh(now=1100) is False

    def test_age_at_max_age_is_stale_on_arrival(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cache = _make_cache(tmp_path)
        cold = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={
                        "etag": "v1",
                        "cache-control": "max-age=600",
                        "age": "600",
                    },
                )
            ]
        )
        assert len(_run_get_files(cold, cache, "pkg")) == 1

        cached = cache.get_simple("pkg")
        assert cached is not None
        _, policy = cached
        assert policy.is_fresh(now=1000) is False

        warm = _FakeTransport([_FakeResponse(b"", status=304, headers={"etag": "v1"})])
        assert len(_run_get_files(warm, cache, "pkg")) == 1

        assert len(warm.calls) == 1
        sent_headers = warm.calls[0][1]
        assert sent_headers is not None
        assert sent_headers.get("If-None-Match") == "v1"


class TestExpiresFreshness:
    """Expires sets the freshness lifetime when Cache-Control does not.

    RFC 9111 4.2.1 ranks Expires minus Date above the heuristic window, and
    4.2.2 rules the heuristic out once a response carries an explicit expiry.
    """

    _NOW = 1_700_000_000.0

    def _freeze(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(time, "time", lambda: self._NOW)

    def _resolve_twice(
        self, tmp_path: Path, headers: dict[str, str]
    ) -> tuple[_FakeTransport, list]:
        """Read pkg, then read it again once a second release has landed."""
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(LISTING_BYTES, headers=headers),
                _FakeResponse(RELISTING_BYTES, headers=headers),
            ]
        )
        assert len(_run_get_files(transport, cache, "pkg")) == 1
        return transport, _run_get_files(transport, cache, "pkg")

    def _stored_policy(self, tmp_path: Path, headers: dict[str, str]) -> CachePolicy:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES, headers=headers)])
        assert len(_run_get_files(transport, cache, "pkg")) == 1
        cached = cache.get_simple("pkg")
        assert cached is not None
        return cached[1]

    def test_expires_in_the_past_revalidates_immediately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch)
        transport, files = self._resolve_twice(
            tmp_path,
            {
                "date": formatdate(self._NOW, usegmt=True),
                "expires": formatdate(self._NOW - 3600, usegmt=True),
            },
        )

        assert len(transport.calls) == 2
        assert len(files) == 2

    def test_expires_zero_is_already_expired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch)
        transport, files = self._resolve_twice(
            tmp_path,
            {"date": formatdate(self._NOW, usegmt=True), "expires": "0"},
        )

        assert len(transport.calls) == 2
        assert len(files) == 2

    def test_expires_grants_a_lifetime_past_the_heuristic_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch)
        policy = self._stored_policy(
            tmp_path,
            {
                "date": formatdate(self._NOW, usegmt=True),
                "expires": formatdate(self._NOW + 86400, usegmt=True),
            },
        )

        assert policy.max_age == 86400
        assert policy.is_fresh(now=int(self._NOW) + 3600) is True

    def test_expires_without_date_measured_from_arrival(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch)
        policy = self._stored_policy(
            tmp_path, {"expires": formatdate(self._NOW + 300, usegmt=True)}
        )

        assert policy.max_age == 300

    def test_max_age_outranks_expires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch)
        policy = self._stored_policy(
            tmp_path,
            {
                "cache-control": "max-age=60",
                "date": formatdate(self._NOW, usegmt=True),
                "expires": formatdate(self._NOW + 86400, usegmt=True),
            },
        )

        assert policy.max_age == 60

    def test_expires_with_an_overflowing_zone_offset_is_already_expired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch)
        policy = self._stored_policy(
            tmp_path,
            {"expires": "Mon, 01 Jan 2030 00:00:00 +99999999999999999999999"},
        )

        assert policy.max_age == 0

    def test_304_expires_replaces_the_stored_lifetime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch)
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg", LISTING_BYTES, CachePolicy(fetched_at=0, max_age=60, etag="v1")
        )
        transport = _FakeTransport(
            [
                _FakeResponse(
                    b"",
                    status=304,
                    headers={
                        "date": formatdate(self._NOW, usegmt=True),
                        "expires": formatdate(self._NOW + 1800, usegmt=True),
                    },
                )
            ]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1

        cached = cache.get_simple("pkg")
        assert cached is not None
        assert cached[1].max_age == 1800


class TestNoReuseListings:
    """A listing the origin bars from unvalidated reuse is checked every read.

    RFC 9111 5.2.2.4 for ``no-cache`` and 5.2.2.5 for ``no-store``. The
    responses here carry no max-age and no Expires, the case the heuristic
    window would otherwise cover.
    """

    def _read_twice(
        self, tmp_path: Path, cache_control: str
    ) -> tuple[_FakeTransport, list]:
        """Read pkg, then read it again once a second release has landed.

        Returns the transport, so its calls can be counted, and the records the
        second read answered with.
        """
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={"etag": "v1", "cache-control": cache_control},
                ),
                _FakeResponse(
                    RELISTING_BYTES,
                    headers={"etag": "v2", "cache-control": cache_control},
                ),
            ]
        )
        assert len(_run_get_files(transport, cache, "pkg")) == 1
        return transport, _run_get_files(transport, cache, "pkg")

    def test_no_cache_sees_a_release_published_between_reads(
        self, tmp_path: Path
    ) -> None:
        transport, files = self._read_twice(tmp_path, "no-cache")

        assert len(transport.calls) == 2
        assert [file.filename for file in files] == [
            "pkg-1.0-py3-none-any.whl",
            "pkg-2.0-py3-none-any.whl",
        ]

    def test_full_directive_list_sees_it_too(self, tmp_path: Path) -> None:
        transport, files = self._read_twice(
            tmp_path, "no-cache,no-store,must-revalidate"
        )

        assert len(transport.calls) == 2
        assert len(files) == 2

    @pytest.mark.parametrize("cache_control", ["no-cache", "no-store"])
    def test_listing_is_stored_and_revalidated_conditionally(
        self, tmp_path: Path, cache_control: str
    ) -> None:
        """The body is still written, so the next read costs a 304, not a refetch."""
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={"etag": "v1", "cache-control": cache_control},
                ),
                _FakeResponse(b"", status=304, headers={"etag": "v1"}),
            ]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 1
        stored = cache.get_simple("pkg")
        assert stored is not None
        assert stored[1].max_age == 0

        assert len(_run_get_files(transport, cache, "pkg")) == 1
        sent = transport.calls[1][1]
        assert sent is not None
        assert sent.get("If-None-Match") == "v1"

    def test_no_store_body_replaces_the_stored_one(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", LISTING_BYTES, _stale_etag_policy())
        transport = _FakeTransport(
            [
                _FakeResponse(
                    RELISTING_BYTES, headers={"etag": "v2", "cache-control": "no-store"}
                )
            ]
        )

        assert len(_run_get_files(transport, cache, "pkg")) == 2

        stored = cache.get_simple("pkg")
        assert stored is not None
        assert stored[0] == RELISTING_BYTES

    def test_offline_serves_a_no_store_listing_from_disk(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        online = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES, headers={"etag": "v1", "cache-control": "no-store"}
                )
            ]
        )
        assert len(_run_get_files(online, cache, "pkg")) == 1

        offline = _FakeTransport()
        assert len(_run_get_files(offline, cache, "pkg", offline=True)) == 1
        assert offline.calls == []

    @pytest.mark.parametrize("cache_control", ["no-cache", "no-store"])
    def test_404_sentinel_is_stale_at_once(
        self, tmp_path: Path, cache_control: str
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    b"", status=404, headers={"cache-control": cache_control}
                ),
                _FakeResponse(
                    b"", status=404, headers={"cache-control": cache_control}
                ),
            ]
        )

        assert _run_get_files(transport, cache, "absent") == []
        sentinel = cache.get_negative("absent")
        assert sentinel is not None
        assert sentinel.max_age == 0

        assert _run_get_files(transport, cache, "absent") == []
        assert len(transport.calls) == 2


class TestRedirectedProjectPage:
    """A relative file URL resolves against the page the index served.

    An index may redirect a project page and still publish a relative
    ``files[].url``. RFC 3986 section 5.1.3 makes the redirect target the
    base, so the resolved URL must not point back at the requested path.
    """

    _MOVED_PAGE = "https://mirror.example.com/pypi/simple/pkg/"
    _EXPECTED_URL = f"{_MOVED_PAGE}pkg-1.0-py3-none-any.whl"
    _RELATIVE_LISTING = json.dumps(
        {
            "meta": {"api-version": "1.1"},
            "name": "pkg",
            "files": [
                {
                    "filename": "pkg-1.0-py3-none-any.whl",
                    "url": "pkg-1.0-py3-none-any.whl",
                    "hashes": {"sha256": "0" * 64},
                    "core-metadata": True,
                }
            ],
        }
    ).encode()

    def _run(self, transport: _FakeTransport, cache: OnDiskCache) -> list:
        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        return asyncio.run(go())

    def test_cold_fetch_resolves_against_the_redirect_target(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [_FakeResponse(self._RELATIVE_LISTING, url=self._MOVED_PAGE)]
        )

        (wheel,) = self._run(transport, cache)

        assert wheel.url == self._EXPECTED_URL
        assert isinstance(wheel, WheelFile)
        assert wheel.metadata_url == f"{self._EXPECTED_URL}.metadata"

    def test_warm_hit_reuses_the_recorded_page(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    self._RELATIVE_LISTING,
                    headers={"cache-control": "max-age=600"},
                    url=self._MOVED_PAGE,
                )
            ]
        )
        self._run(transport, cache)

        # A second transport with nothing queued fails any request it gets.
        (wheel,) = self._run(_FakeTransport(), cache)

        assert wheel.url == self._EXPECTED_URL
        assert len(transport.calls) == 1

    def test_offline_hit_reuses_the_recorded_page(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            self._RELATIVE_LISTING,
            CachePolicy(fetched_at=0, max_age=1, etag=None, page_url=self._MOVED_PAGE),
        )

        async def go() -> list:
            client = CachedAsyncSimpleClient(_FakeTransport(), cache, offline=True)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        (wheel,) = asyncio.run(go())

        assert wheel.url == self._EXPECTED_URL

    def test_304_revalidation_adopts_the_redirect_target(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            self._RELATIVE_LISTING,
            CachePolicy(fetched_at=0, max_age=1, etag="v1"),
        )
        transport = _FakeTransport(
            [
                _FakeResponse(
                    b"", status=304, headers={"etag": "v1"}, url=self._MOVED_PAGE
                )
            ]
        )

        (wheel,) = self._run(transport, cache)

        assert wheel.url == self._EXPECTED_URL
        cached = cache.get_simple("pkg")
        assert cached is not None
        assert cached[1].page_url == self._MOVED_PAGE

    def test_200_revalidation_adopts_the_redirect_target(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            b'{"files": []}',
            CachePolicy(fetched_at=0, max_age=1, etag="old"),
        )
        transport = _FakeTransport(
            [_FakeResponse(self._RELATIVE_LISTING, url=self._MOVED_PAGE)]
        )

        (wheel,) = self._run(transport, cache)

        assert wheel.url == self._EXPECTED_URL

    def test_entry_stored_without_a_page_falls_back_to_the_index(
        self, tmp_path: Path
    ) -> None:
        """A cache written before the page URL was recorded keeps working."""
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            self._RELATIVE_LISTING,
            CachePolicy(fetched_at=2_000_000_000, max_age=99999, etag=None),
        )

        (wheel,) = self._run(_FakeTransport(), cache)

        assert wheel.url == "https://pypi.org/simple/pkg/pkg-1.0-py3-none-any.whl"


class TestNonJsonListingBody:
    """A 200 response whose body is not JSON must not poison the cache.

    A proxy or captive portal can answer with a page of its own under status
    200 and no HTML content type, so the body reaches the JSON decoder.
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

    def test_cold_over_nested_body_raises_clean_and_skips_cache(
        self,
        tmp_path: Path,
        refuse_over_nested: Callable[[bytes], AbstractContextManager[None]],
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(OVER_NESTED, status=200)])

        with (
            refuse_over_nested(OVER_NESTED),
            pytest.raises(
                MalformedSimpleResponseError, match="nested too deeply to decode"
            ),
        ):
            _run_get_files(transport, cache, "foo")
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


class TestHtmlListing:
    """PEP 691: the served Content-Type picks the decoder, not the Accept header.

    An index may answer with a type the client did not ask for;
    download.pytorch.org answers the JSON request with a PEP 503 page.
    """

    _INDEX = "https://download.pytorch.org/whl/cpu/"
    _WHEEL = "torch-2.7.0+cpu-cp312-cp312-manylinux_2_28_x86_64.whl"
    _DIGEST = "c" * 64
    _HREF = (
        "/whl/cpu/torch-2.7.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl"
        f"#sha256={_DIGEST}"
    )
    _UPLOAD_TIME = "2025-04-23T15:03:12Z"
    _PAGE = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "  <body>\n"
        "    <h1>Links for torch</h1>\n"
        f'    <a href="{_HREF}" data-requires-python="&gt;=3.9" '
        f'data-upload-time="{_UPLOAD_TIME}">'
        f"{_WHEEL}</a><br/>\n"
        "  </body>\n"
        "</html>\n"
    ).encode()

    def _fetch(
        self,
        cache: OnDiskCache,
        body: bytes,
        content_type: str,
        package: str = "torch",
    ) -> tuple[list, _FakeTransport]:
        transport = _FakeTransport(
            [_FakeResponse(body, headers={"content-type": content_type})]
        )

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache, self._INDEX)
            try:
                return await client.get_files(package)
            finally:
                await client.aclose()

        return (asyncio.run(go()), transport)

    def test_pep503_page_is_read(self, tmp_path: Path) -> None:
        files, _ = self._fetch(_make_cache(tmp_path), self._PAGE, "text/html")
        (wheel,) = files
        assert isinstance(wheel, WheelFile)
        assert wheel.filename == self._WHEEL
        assert wheel.version == "2.7.0+cpu"
        assert wheel.url == (
            "https://download.pytorch.org/whl/cpu/"
            "torch-2.7.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl"
        )
        assert wheel.requires_python == ">=3.9"
        assert wheel.hashes == (("sha256", self._DIGEST),)
        assert wheel.upload_time == self._UPLOAD_TIME

    def test_hash_fragment_before_an_egg_part(self, tmp_path: Path) -> None:
        page = (
            f'<a href="torch-2.7.0-py3-none-any.whl#sha256={self._DIGEST}'
            '&amp;egg=torch-2.7.0">torch</a>'
        ).encode()
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert files[0].hashes == (("sha256", self._DIGEST),)

    def test_hash_fragment_after_an_egg_part(self, tmp_path: Path) -> None:
        page = (
            '<a href="torch-2.7.0-py3-none-any.whl#egg=torch-2.7.0'
            f'&amp;sha256={self._DIGEST}">torch</a>'
        ).encode()
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert files[0].hashes == (("sha256", self._DIGEST),)

    def test_every_fragment_hash_is_read(self, tmp_path: Path) -> None:
        sha512 = "f" * 128
        page = (
            f'<a href="torch-2.7.0-py3-none-any.whl#sha256={self._DIGEST}'
            f'&amp;sha512={sha512}">torch</a>'
        ).encode()
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert files[0].hashes == (("sha256", self._DIGEST), ("sha512", sha512))

    def test_relative_href_resolves_against_the_redirect_target(
        self, tmp_path: Path
    ) -> None:
        """A moved HTML page is the base for its own relative hrefs."""
        moved = "https://mirror.example.com/whl/cpu/torch/"
        page = b'<a href="torch-2.7.0-py3-none-any.whl">torch</a>'
        transport = _FakeTransport(
            [_FakeResponse(page, headers={"content-type": "text/html"}, url=moved)]
        )

        async def go() -> list:
            client = CachedAsyncSimpleClient(
                transport, _make_cache(tmp_path), self._INDEX
            )
            try:
                return await client.get_files("torch")
            finally:
                await client.aclose()

        (wheel,) = asyncio.run(go())

        assert wheel.url == f"{moved}torch-2.7.0-py3-none-any.whl"

    def test_malformed_ipv6_href_is_dropped(self, tmp_path: Path) -> None:
        # An unterminated IPv6 bracket makes the href join and split raise;
        # that anchor is dropped and its good sibling still resolves.
        page = (
            b'<a href="http://[bad">bad</a>'
            b'<a href="torch-2.7.0-py3-none-any.whl">good</a>'
        )
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert [f.filename for f in files] == ["torch-2.7.0-py3-none-any.whl"]

    def test_href_wrapped_in_whitespace_is_read(self, tmp_path: Path) -> None:
        # HTML allows a URL attribute's value to be surrounded by whitespace.
        page = b'<a href="\n      torch-2.7.0-py3-none-any.whl\n    ">torch</a>'
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert [f.filename for f in files] == ["torch-2.7.0-py3-none-any.whl"]
        assert files[0].url == f"{self._INDEX}torch/torch-2.7.0-py3-none-any.whl"

    def test_malformed_base_href_fails_the_listing(self, tmp_path: Path) -> None:
        # Every relative anchor resolves against <base href>, so one that
        # cannot be parsed leaves the page's targets unknown.
        page = (
            b'<base href="http://[bad"><a href="torch-2.7.0-py3-none-any.whl">good</a>'
        )
        with pytest.raises(MalformedSimpleResponseError, match="base href"):
            self._fetch(_make_cache(tmp_path), page, "text/html")

    def test_page_without_upload_time_leaves_it_unset(self, tmp_path: Path) -> None:
        page = b'<a href="torch-2.7.0-py3-none-any.whl">a</a>'
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert [f.upload_time for f in files] == [None]

    def test_accept_advertises_every_supported_type(self, tmp_path: Path) -> None:
        _, transport = self._fetch(_make_cache(tmp_path), self._PAGE, "text/html")
        sent = transport.calls[0][1]
        assert sent is not None
        assert sent["Accept"] == (
            "application/vnd.pypi.simple.v1+json, "
            "application/vnd.pypi.simple.v1+html;q=0.2, "
            "text/html;q=0.01"
        )

    @pytest.mark.parametrize(
        "content_type",
        [
            "text/html",
            "text/html; charset=UTF-8",
            "Text/HTML",
            "application/vnd.pypi.simple.v1+html",
            "application/vnd.pypi.simple.latest+html",
        ],
    )
    def test_html_content_types_all_decode(
        self, tmp_path: Path, content_type: str
    ) -> None:
        files, _ = self._fetch(_make_cache(tmp_path), self._PAGE, content_type)
        assert [f.version for f in files] == ["2.7.0+cpu"]

    def test_json_content_type_still_decodes_json(self, tmp_path: Path) -> None:
        files, _ = self._fetch(
            _make_cache(tmp_path),
            LISTING_BYTES,
            "application/vnd.pypi.simple.v1+json",
            package="pkg",
        )
        assert [f.filename for f in files] == ["pkg-1.0-py3-none-any.whl"]

    def test_cached_page_serves_warm_hit_without_network(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        first, _ = self._fetch(cache, self._PAGE, "text/html")
        offline = _FakeTransport()

        async def go() -> list:
            client = CachedAsyncSimpleClient(offline, cache, self._INDEX)
            try:
                return await client.get_files("torch")
            finally:
                await client.aclose()

        assert asyncio.run(go()) == first
        assert offline.calls == []

    def test_revalidation_replaces_body_with_new_page(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        self._fetch(cache, self._PAGE, "text/html")
        cached = cache.get_simple("torch")
        assert cached is not None
        cache.put_simple(
            "torch", cached[0], CachePolicy(fetched_at=0, max_age=1, etag="old")
        )
        newer = self._PAGE.replace(b"2.7.0", b"2.8.0")
        files, transport = self._fetch(cache, newer, "text/html")
        assert [f.version for f in files] == ["2.8.0+cpu"]
        assert len(transport.calls) == 1

    def test_yanked_anchor_dropped(self, tmp_path: Path) -> None:
        page = (
            b'<a href="torch-2.7.0-py3-none-any.whl" data-yanked="bad build">a</a>'
            b'<a href="torch-2.8.0-py3-none-any.whl">b</a>'
        )
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert [f.version for f in files] == ["2.8.0"]

    def test_relative_href_resolves_against_page(self, tmp_path: Path) -> None:
        page = b'<a href="../pkgs/torch-2.7.0-py3-none-any.whl">a</a>'
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert [f.url for f in files] == [
            "https://download.pytorch.org/whl/cpu/pkgs/torch-2.7.0-py3-none-any.whl"
        ]

    def test_base_href_redirects_relative_anchor(self, tmp_path: Path) -> None:
        page = (
            b'<html><head><base href="https://mirror.example/dl/"></head>'
            b'<body><a href="torch-2.7.0-py3-none-any.whl">a</a></body></html>'
        )
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert [f.url for f in files] == [
            "https://mirror.example/dl/torch-2.7.0-py3-none-any.whl"
        ]

    def test_anchor_without_a_filename_skipped(self, tmp_path: Path) -> None:
        page = b'<a href="../">parent</a><a href="torch-2.7.0-py3-none-any.whl">a</a>'
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert [f.version for f in files] == ["2.7.0"]

    def test_core_metadata_hash_carried(self, tmp_path: Path) -> None:
        page = (
            b'<a href="torch-2.7.0-py3-none-any.whl" '
            b'data-core-metadata="sha256=ABCD">a</a>'
        )
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        (wheel,) = files
        assert isinstance(wheel, WheelFile)
        assert wheel.has_metadata
        assert wheel.metadata_hash == ("sha256", "abcd")

    def test_bare_metadata_attribute_advertises_sidecar_without_hash(
        self, tmp_path: Path
    ) -> None:
        page = (
            b'<a href="torch-2.7.0-py3-none-any.whl" '
            b'data-dist-info-metadata="true">a</a>'
        )
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        (wheel,) = files
        assert isinstance(wheel, WheelFile)
        assert wheel.has_metadata
        assert wheel.metadata_hash is None

    def test_page_without_links_or_marker_is_not_an_empty_listing(
        self, tmp_path: Path
    ) -> None:
        # A 200 site error page would otherwise read as "package absent" and
        # let the multi-index router fall through to a lower-priority index.
        page = (
            b"<html><body><h1>Sorry, that page could not be found.</h1></body></html>"
        )
        with pytest.raises(MalformedSimpleResponseError, match="not a project page"):
            self._fetch(_make_cache(tmp_path), page, "text/html")
        assert _make_cache(tmp_path).get_simple("torch") is None

    def test_unrelated_meta_tags_do_not_make_it_a_project_page(
        self, tmp_path: Path
    ) -> None:
        page = (
            b'<html><head><meta charset="utf-8">'
            b'<meta name="generator" content="mkdocs"></head><body></body></html>'
        )
        with pytest.raises(MalformedSimpleResponseError, match="not a project page"):
            self._fetch(_make_cache(tmp_path), page, "text/html")

    def test_marker_lets_a_project_page_list_no_files(self, tmp_path: Path) -> None:
        page = (
            b'<html><head><meta name="pypi:repository-version" content="1.4">'
            b"</head><body></body></html>"
        )
        files, _ = self._fetch(_make_cache(tmp_path), page, "text/html")
        assert files == []

    def test_non_utf8_page_raises_clean_error(self, tmp_path: Path) -> None:
        page = '<a href="torch-2.7.0-py3-none-any.whl">café</a>'.encode("latin-1")
        with pytest.raises(
            MalformedSimpleResponseError, match="HTML body is not valid UTF-8"
        ) as caught:
            self._fetch(_make_cache(tmp_path), page, "text/html")
        assert isinstance(caught.value, HttpError)
        assert _make_cache(tmp_path).get_simple("torch") is None


class TestSerializationPin:
    """A pin fixes both the Accept header and the decoder."""

    _INDEX = "https://pypi.org/simple/"
    _PAGE = b'<a href="pkg-1.0-py3-none-any.whl">pkg</a>'
    _JSON_TYPE = "application/vnd.pypi.simple.v1+json"

    def _fetch(
        self,
        cache: OnDiskCache,
        serialization: SimpleSerialization,
        body: bytes = LISTING_BYTES,
        content_type: str | None = _JSON_TYPE,
    ) -> tuple[list, _FakeTransport]:
        headers = {} if content_type is None else {"content-type": content_type}
        transport = _FakeTransport([_FakeResponse(body, headers=headers)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(
                transport, cache, self._INDEX, serialization=serialization
            )
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        return (asyncio.run(go()), transport)

    def test_json_pin_asks_for_json_only(self, tmp_path: Path) -> None:
        _, transport = self._fetch(_make_cache(tmp_path), SimpleSerialization.JSON)
        sent = transport.calls[0][1]
        assert sent is not None
        assert sent["Accept"] == "application/vnd.pypi.simple.v1+json"

    def test_html_pin_asks_for_both_html_spellings(self, tmp_path: Path) -> None:
        _, transport = self._fetch(
            _make_cache(tmp_path),
            SimpleSerialization.HTML,
            self._PAGE,
            "text/html",
        )
        sent = transport.calls[0][1]
        assert sent is not None
        assert sent["Accept"] == (
            "application/vnd.pypi.simple.v1+html, text/html;q=0.01"
        )

    def test_revalidation_sends_the_pinned_accept_with_the_etag(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag="old-etag"),
        )
        _, transport = self._fetch(cache, SimpleSerialization.JSON)
        sent = transport.calls[0][1]
        assert sent is not None
        assert sent["Accept"] == "application/vnd.pypi.simple.v1+json"
        assert sent["If-None-Match"] == "old-etag"

    def test_json_pin_rejects_an_html_body(self, tmp_path: Path) -> None:
        with pytest.raises(MalformedSimpleResponseError) as caught:
            self._fetch(
                _make_cache(tmp_path),
                SimpleSerialization.JSON,
                self._PAGE,
                "text/html",
            )
        message = str(caught.value)
        assert "Content-Type 'text/html'" in message
        assert "pinned to serialization = 'json'" in message
        assert "set serialization = 'html'" in message

    def test_html_pin_rejects_a_json_body(self, tmp_path: Path) -> None:
        with pytest.raises(MalformedSimpleResponseError) as caught:
            self._fetch(_make_cache(tmp_path), SimpleSerialization.HTML)
        message = str(caught.value)
        assert f"Content-Type {self._JSON_TYPE!r}" in message
        assert "pinned to serialization = 'html'" in message

    def test_json_pin_decodes_a_json_body(self, tmp_path: Path) -> None:
        files, _ = self._fetch(_make_cache(tmp_path), SimpleSerialization.JSON)
        assert [f.filename for f in files] == ["pkg-1.0-py3-none-any.whl"]

    @pytest.mark.parametrize(
        "content_type",
        ["text/html", "application/vnd.pypi.simple.v1+html"],
    )
    def test_html_pin_decodes_both_html_content_types(
        self, tmp_path: Path, content_type: str
    ) -> None:
        files, _ = self._fetch(
            _make_cache(tmp_path),
            SimpleSerialization.HTML,
            self._PAGE,
            content_type,
        )
        assert [f.filename for f in files] == ["pkg-1.0-py3-none-any.whl"]

    def test_missing_content_type_reads_as_json_under_a_json_pin(
        self, tmp_path: Path
    ) -> None:
        files, _ = self._fetch(
            _make_cache(tmp_path), SimpleSerialization.JSON, content_type=None
        )
        assert [f.filename for f in files] == ["pkg-1.0-py3-none-any.whl"]

    def test_missing_content_type_is_rejected_under_an_html_pin(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(MalformedSimpleResponseError, match="no Content-Type"):
            self._fetch(
                _make_cache(tmp_path),
                SimpleSerialization.HTML,
                self._PAGE,
                content_type=None,
            )


class TestSerializationCacheFlip:
    """A body fetched under one pin never answers a request under the other."""

    _INDEX = "https://pypi.org/simple/"
    _PAGE = b'<a href="pkg-1.0-py3-none-any.whl#sha256=' + b"d" * 64 + b'">pkg</a>'
    _JSON_BYTES = json.dumps(
        {
            "meta": {"api-version": "1.0"},
            "name": "pkg",
            "files": [
                {
                    "filename": "pkg-1.0-py3-none-any.whl",
                    "url": "https://files.example.com/pkg-1.0-py3-none-any.whl",
                    "hashes": {"sha256": "e" * 64},
                    "size": 4321,
                    "upload-time": "2026-01-02T03:04:05Z",
                }
            ],
        }
    ).encode()

    def _cache(self, tmp_path: Path, serialization: SimpleSerialization) -> OnDiskCache:
        return OnDiskCache(tmp_path, self._INDEX, serialization=serialization)

    def _get(
        self,
        tmp_path: Path,
        serialization: SimpleSerialization,
        transport: _FakeTransport,
    ) -> list:
        cache = self._cache(tmp_path, serialization)

        async def go() -> list:
            client = CachedAsyncSimpleClient(
                transport, cache, self._INDEX, serialization=serialization
            )
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        return asyncio.run(go())

    def _warm_html(self, tmp_path: Path, cache_control: str | None = None) -> None:
        headers = {"content-type": "text/html"}
        if cache_control is not None:
            headers["cache-control"] = cache_control
        self._get(
            tmp_path,
            SimpleSerialization.HTML,
            _FakeTransport([_FakeResponse(self._PAGE, headers=headers)]),
        )

    def _json_transport(self) -> _FakeTransport:
        return _FakeTransport(
            [
                _FakeResponse(
                    self._JSON_BYTES,
                    headers={"content-type": "application/vnd.pypi.simple.v1+json"},
                )
            ]
        )

    def test_fresh_entry_from_the_other_pin_is_refetched(self, tmp_path: Path) -> None:
        self._warm_html(tmp_path, cache_control="max-age=86400")
        transport = self._json_transport()
        files = self._get(tmp_path, SimpleSerialization.JSON, transport)
        assert len(transport.calls) == 1
        sent = transport.calls[0][1]
        assert sent is not None
        assert "If-None-Match" not in sent
        (wheel,) = files
        assert wheel.hashes == (("sha256", "e" * 64),)
        assert wheel.size == 4321
        assert wheel.upload_time == "2026-01-02T03:04:05Z"

    def test_stale_entry_from_the_other_pin_is_not_revalidated(
        self, tmp_path: Path
    ) -> None:
        self._warm_html(tmp_path)
        html_cache = self._cache(tmp_path, SimpleSerialization.HTML)
        cached = html_cache.get_simple("pkg")
        assert cached is not None
        html_cache.put_simple(
            "pkg", cached[0], CachePolicy(fetched_at=0, max_age=1, etag="html-etag")
        )

        transport = self._json_transport()
        files = self._get(tmp_path, SimpleSerialization.JSON, transport)
        sent = transport.calls[0][1]
        assert sent is not None
        assert "If-None-Match" not in sent
        (wheel,) = files
        assert wheel.hashes == (("sha256", "e" * 64),)

    def test_negative_sentinel_from_the_other_pin_is_not_served(
        self, tmp_path: Path
    ) -> None:
        self._cache(tmp_path, SimpleSerialization.JSON).put_negative(
            "pkg", CachePolicy(fetched_at=int(time.time()), max_age=600, etag=None)
        )
        transport = _FakeTransport(
            [_FakeResponse(self._PAGE, headers={"content-type": "text/html"})]
        )
        files = self._get(tmp_path, SimpleSerialization.HTML, transport)
        assert len(transport.calls) == 1
        assert [f.filename for f in files] == ["pkg-1.0-py3-none-any.whl"]


class _UnchangedPageTransport:
    """Index whose page has not changed since it issued ``etag``.

    A conditional request gets a 304; an unconditional one gets the page.
    """

    def __init__(self, page: bytes, etag: str) -> None:
        self._page = page
        self._etag = etag
        self.calls: list[dict[str, str] | None] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self.calls.append(headers)
        if headers is not None and "If-None-Match" in headers:
            return _FakeResponse(b"", status=304, headers={"etag": self._etag}, url=url)
        return _FakeResponse(
            self._page,
            headers={"content-type": "text/html", "etag": self._etag},
            url=url,
        )

    async def aclose(self) -> None:
        return None


class TestRetiredListingBucket:
    """A body an older nab wrote into a retired bucket never answers.

    A 304 keeps the stored body, so an obsolete rendering of a PEP 503
    page is only retired by the bucket's version suffix.
    """

    _INDEX = "https://pypi.org/simple/"
    _PAGE_URL = "https://pypi.org/simple/pkg/"
    _ETAG = "unchanged"
    _DIGEST = "b" * 64
    _WHEEL = "pkg-1.0-py3-none-any.whl"
    _PAGE = f'<a href="{_WHEEL}#sha256={_DIGEST}&amp;egg=pkg">pkg</a>'.encode()
    # The page as an older nab rendered it, taking the fragment as one hash.
    _RETIRED_BODY = json.dumps(
        {
            "files": [
                {
                    "filename": _WHEEL,
                    "url": f"{_PAGE_URL}{_WHEEL}",
                    "hashes": {"sha256": f"{_DIGEST}&egg=pkg"},
                }
            ]
        }
    ).encode()

    def _seed_retired_bucket(self, root: Path) -> Path:
        """Write the older body and a stale policy into ``simple-v1``.

        Returns the seeded bucket directory.
        """
        bucket = root / "simple-v1" / "pypi"
        bucket.mkdir(parents=True)
        (bucket / "pkg.json").write_bytes(self._RETIRED_BODY)
        (bucket / "pkg.policy").write_bytes(
            json.dumps(
                {
                    "fetched_at": 0,
                    "max_age": 1,
                    "etag": self._ETAG,
                    "page_url": self._PAGE_URL,
                }
            ).encode()
        )
        return bucket

    def _get_files(self, root: Path, transport: _UnchangedPageTransport) -> list:
        """Resolve ``pkg`` through a client backed by the cache under ``root``."""
        cache = OnDiskCache(root, self._INDEX)

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache, self._INDEX)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        return asyncio.run(go())

    def test_retired_body_is_refetched_not_revalidated(self, tmp_path: Path) -> None:
        bucket = self._seed_retired_bucket(tmp_path)
        transport = _UnchangedPageTransport(self._PAGE, self._ETAG)

        (wheel,) = self._get_files(tmp_path, transport)

        assert wheel.hashes == (("sha256", self._DIGEST),)

        assert len(transport.calls) == 1
        sent = transport.calls[0]
        assert sent is not None
        assert "If-None-Match" not in sent

        assert (bucket / "pkg.json").read_bytes() == self._RETIRED_BODY


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


class TestNonOkStatusIsNotContent:
    """Only a 200 or a 203 carries content a body-reading call may use.

    ``raise_for_status`` draws its line at 400, so a 204 and a 3xx the
    transport did not follow reach the caller unflagged; neither is the
    artifact. A 203 reads like a 200.
    """

    def test_metadata_sidecar_203_is_content(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(b"Name: pkg\n", status=203)])

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata"
                )
            finally:
                await client.aclose()

        assert asyncio.run(go()) == "Name: pkg\n"
        assert cache.get_metadata("pkg", "https://x/pkg.metadata") == "Name: pkg\n"

    def test_metadata_sidecar_204_raises_and_caches_nothing(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(b"", status=204)])

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata"
                )
            finally:
                await client.aclose()

        with pytest.raises(HttpError, match="204"):
            asyncio.run(go())
        assert cache.get_metadata("pkg", "https://x/pkg.metadata") is None

    def test_metadata_sidecar_300_raises_and_caches_nothing(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [_FakeResponse(b"<html>pick one</html>", status=300)]
        )

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata"
                )
            finally:
                await client.aclose()

        with pytest.raises(HttpError, match="300"):
            asyncio.run(go())
        assert cache.get_metadata("pkg", "https://x/pkg.metadata") is None

    def test_listing_300_raises_and_caches_nothing(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES, status=300)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        with pytest.raises(HttpError, match="300"):
            asyncio.run(go())
        assert cache.get_simple("pkg") is None

    def test_revalidated_listing_300_raises_and_keeps_the_stored_body(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=0, max_age=1, etag="old-etag"),
        )
        transport = _FakeTransport([_FakeResponse(b"", status=300)])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        with pytest.raises(HttpError, match="300"):
            asyncio.run(go())
        cached = cache.get_simple("pkg")
        assert cached is not None
        assert cached[0] == LISTING_BYTES

    def test_sdist_files_204_raises_and_caches_nothing(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(b"", status=204)])

        async def go() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz"
                )
            finally:
                await client.aclose()

        with pytest.raises(HttpError, match="204"):
            asyncio.run(go())
        assert cache.get_sdist_files("pkg", "1.0") is None

    def test_sdist_archive_204_raises(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(b"", status=204)])

        async def go() -> bytes:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_sdist_archive(
                    "pkg", "1.0", "https://x/pkg.tar.gz"
                )
            finally:
                await client.aclose()

        with pytest.raises(HttpError, match="204"):
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

    @pytest.mark.parametrize(
        "members",
        [
            pytest.param([("pkg-1.0/setup.py", b"setup()\n")], id="no-metadata-file"),
            pytest.param(
                [("pkg-1.0/pyproject.toml", b'[project]\nname = "pkg"\n')],
                id="pyproject-only",
            ),
        ],
    )
    def test_sdist_without_pkg_info_is_cached_and_not_refetched(
        self, tmp_path: Path, members: list[tuple[str, bytes]]
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(_build_tarball(members))])

        async def go() -> list[tuple[str | None, str | None]]:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return [
                    await client.get_sdist_files("pkg", "1.0", "https://x/pkg.tar.gz")
                    for _ in range(2)
                ]
            finally:
                await client.aclose()

        assert asyncio.run(go()) == [(None, None), (None, None)]
        assert cache.get_sdist_files("pkg", "1.0") == (None, None)
        assert len(transport.calls) == 1

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

        The first fetch's write is dropped as the single record is
        committed; the second must still recover the full PKG-INFO and
        pyproject.toml pair.
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

        first_pkg_info, first_pyproject = asyncio.run(fetch())
        assert first_pkg_info is not None
        assert first_pyproject is not None
        assert cache.get_sdist_files("pkg", "1.0") is None

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


class TestSdistArchiveHold:
    def test_the_oldest_archive_goes_when_the_hold_is_full(self) -> None:
        """A full hold evicts the oldest rather than growing."""
        hold = SdistArchiveHold(2)
        hold.put("pkg", "1.0", b"one")
        hold.put("pkg", "2.0", b"two")
        hold.put("pkg", "3.0", b"three")

        assert hold.take("pkg", "1.0") is None
        assert hold.take("pkg", "2.0") == b"two"
        assert hold.take("pkg", "3.0") == b"three"

    def test_clear_drops_every_archive(self) -> None:
        hold = SdistArchiveHold()
        hold.put("pkg", "1.0", b"one")
        hold.clear()

        assert hold.take("pkg", "1.0") is None


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

    def test_a_held_archive_answers_without_a_second_request(
        self, tmp_path: Path
    ) -> None:
        """The PKG-INFO read's own download serves the build that follows."""
        cache = _make_cache(tmp_path)
        body = _build_tarball([("pkg-1.0/PKG-INFO", b"Name: pkg\n")])
        transport = _FakeTransport([_FakeResponse(body)])
        hold = SdistArchiveHold()

        async def go() -> bytes:
            client = CachedAsyncSimpleClient(transport, cache, sdist_archive_hold=hold)
            try:
                await client.get_sdist_files("pkg", "1.0", "https://x/pkg.tar.gz")
                return await client.get_sdist_archive(
                    "pkg", "1.0", "https://x/pkg.tar.gz"
                )
            finally:
                await client.aclose()

        assert asyncio.run(go()) == body
        assert len(transport.calls) == 1

    def test_a_held_archive_is_verified_against_the_published_hash(
        self, tmp_path: Path
    ) -> None:
        """Held bytes go through the same digest check a downloaded body does."""
        cache = _make_cache(tmp_path)
        published = _build_tarball([("pkg-1.0/PKG-INFO", b"Name: pkg\n")])
        tampered = _build_tarball([("pkg-1.0/PKG-INFO", b"Name: evil\n")])
        transport = _FakeTransport([])
        hold = SdistArchiveHold()
        hold.put("pkg", "1.0", tampered)

        async def go() -> bytes:
            client = CachedAsyncSimpleClient(transport, cache, sdist_archive_hold=hold)
            try:
                return await client.get_sdist_archive(
                    "pkg",
                    "1.0",
                    "https://x/pkg.tar.gz",
                    (("sha256", hashlib.sha256(published).hexdigest()),),
                )
            finally:
                await client.aclose()

        with pytest.raises(SdistHashMismatchError):
            asyncio.run(go())
        assert transport.calls == []

    def test_a_hold_holding_another_version_is_not_consulted(
        self, tmp_path: Path
    ) -> None:
        """The hold is keyed by version, so a sibling's archive is not served."""
        cache = _make_cache(tmp_path)
        body = _build_tarball([("pkg-2.0/PKG-INFO", b"Name: pkg\n")])
        transport = _FakeTransport([_FakeResponse(body)])
        hold = SdistArchiveHold()
        hold.put("pkg", "1.0", b"the other version")

        async def go() -> bytes:
            client = CachedAsyncSimpleClient(transport, cache, sdist_archive_hold=hold)
            try:
                return await client.get_sdist_archive(
                    "pkg", "2.0", "https://x/pkg-2.0.tar.gz"
                )
            finally:
                await client.aclose()

        assert asyncio.run(go()) == body
        assert hold.take("pkg", "1.0") == b"the other version"


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


_RANGE_META = b"Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n\nbody\n"
_WHEEL_URL = "https://files.example.com/pkg-1.0-py3-none-any.whl"


def _build_range_wheel() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pkg/__init__.py", b"value = 1\n")
        zf.writestr("pkg-1.0.dist-info/METADATA", _RANGE_META)
        zf.writestr("pkg-1.0.dist-info/WHEEL", b"Wheel-Version: 1.0\n")
    return buf.getvalue()


class _WellBehavedRangeTransport:
    """Serve a wheel over well-behaved 206 range responses, counting calls."""

    def __init__(self, wheel: bytes) -> None:
        self.wheel = wheel
        self.total = len(wheel)
        self.calls = 0

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self.calls += 1
        headers = headers or {}
        body = headers["Range"].removeprefix("bytes=")
        if body.startswith("-"):
            start = max(0, self.total - int(body[1:]))
            end = self.total - 1
        else:
            lo, _, hi = body.partition("-")
            start, end = int(lo), min(int(hi), self.total - 1)
        data = self.wheel[start : end + 1]
        return _FakeResponse(
            data,
            status=206,
            headers={"content-range": f"bytes {start}-{end}/{self.total}"},
        )

    async def aclose(self) -> None:
        return None


class _FullBodyJunkTransport:
    """Ignore the range and hand back a 200 body that is not a zip."""

    def __init__(self) -> None:
        self.calls = 0

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse(b"not a zip file", status=200)

    async def aclose(self) -> None:
        return None


class _FullBodyWheelTransport:
    """Ignore the range and hand back the whole wheel with a 200."""

    def __init__(self, wheel: bytes) -> None:
        self.wheel = wheel
        self.calls = 0

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse(self.wheel, status=200)

    async def aclose(self) -> None:
        return None


class TestGetRangeMetadata:
    _NAME = canonicalize_name("pkg")

    def test_cache_hit_returns_without_transport_and_works_offline(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_metadata("pkg", _WHEEL_URL, _RANGE_META.decode())
        transport = _FakeTransport()  # raises on any request

        async def go() -> RangeMetadataResult:
            client = CachedAsyncSimpleClient(transport, cache, offline=True)
            try:
                return await client.get_range_metadata(
                    "pkg", "1.0", _WHEEL_URL, self._NAME
                )
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.text == _RANGE_META.decode()
        assert result.outcome is RangeOutcome.PARTIAL
        assert transport.calls == []

    def test_cold_offline_raises(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport()

        async def go() -> RangeMetadataResult:
            client = CachedAsyncSimpleClient(transport, cache, offline=True)
            try:
                return await client.get_range_metadata(
                    "pkg", "1.0", _WHEEL_URL, self._NAME
                )
            finally:
                await client.aclose()

        with pytest.raises(OfflineError, match="pkg==1.0"):
            asyncio.run(go())

    def test_recovery_writes_metadata_v1_store(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _WellBehavedRangeTransport(_build_range_wheel())

        async def go() -> RangeMetadataResult:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_range_metadata(
                    "pkg", "1.0", _WHEEL_URL, self._NAME
                )
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.text == _RANGE_META.decode()
        assert result.outcome is RangeOutcome.PARTIAL
        assert transport.calls > 0
        # Recovered METADATA lands in the metadata-v1 store keyed by wheel URL.
        assert cache.get_metadata("pkg", _WHEEL_URL) == _RANGE_META.decode()

    def test_warm_range_cache_returns_without_transport(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _WellBehavedRangeTransport(_build_range_wheel())

        async def warm() -> None:
            client = CachedAsyncSimpleClient(transport, cache)
            await client.get_range_metadata("pkg", "1.0", _WHEEL_URL, self._NAME)
            await client.aclose()

        asyncio.run(warm())
        first = transport.calls

        offline = _FakeTransport()

        async def again() -> RangeMetadataResult:
            client = CachedAsyncSimpleClient(offline, cache, offline=True)
            try:
                return await client.get_range_metadata(
                    "pkg", "1.0", _WHEEL_URL, self._NAME
                )
            finally:
                await client.aclose()

        result = asyncio.run(again())
        assert result.text == _RANGE_META.decode()
        assert transport.calls == first
        assert offline.calls == []

    def test_unreadable_wheel_returns_none_and_skips_cache(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FullBodyJunkTransport()

        async def go() -> RangeMetadataResult:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_range_metadata(
                    "pkg", "1.0", _WHEEL_URL, self._NAME
                )
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.text is None
        assert result.outcome is RangeOutcome.MISSING
        assert cache.get_metadata("pkg", _WHEEL_URL) is None

    def test_injected_memo_is_used(self, tmp_path: Path) -> None:
        from nab_index.lazy_wheel import RangeCapability, RangeCapabilityMemo

        cache = _make_cache(tmp_path)
        memo = RangeCapabilityMemo()
        transport = _WellBehavedRangeTransport(_build_range_wheel())

        async def go() -> None:
            client = CachedAsyncSimpleClient(transport, cache, range_memo=memo)
            try:
                await client.get_range_metadata("pkg", "1.0", _WHEEL_URL, self._NAME)
            finally:
                await client.aclose()

        asyncio.run(go())
        assert memo.capability("files.example.com") is RangeCapability.SUFFIX_OK

    def test_full_body_matching_published_hash_is_cached(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        wheel = _build_range_wheel()
        transport = _FullBodyWheelTransport(wheel)
        published = (("sha256", hashlib.sha256(wheel).hexdigest()),)

        async def go() -> RangeMetadataResult:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_range_metadata(
                    "pkg", "1.0", _WHEEL_URL, self._NAME, published
                )
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.outcome is RangeOutcome.FULL_BODY
        assert result.text == _RANGE_META.decode()
        assert cache.get_metadata("pkg", _WHEEL_URL) == _RANGE_META.decode()

    def test_full_body_failing_published_hash_raises_and_skips_cache(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        transport = _FullBodyWheelTransport(_build_range_wheel())
        wrong = (("sha256", "0" * 64),)

        async def go() -> RangeMetadataResult:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_range_metadata(
                    "pkg", "1.0", _WHEEL_URL, self._NAME, wrong
                )
            finally:
                await client.aclose()

        with pytest.raises(WheelHashMismatchError):
            asyncio.run(go())
        assert cache.get_metadata("pkg", _WHEEL_URL) is None


def _run_get_files(
    transport: object, cache: object, package: str, *, offline: bool = False
) -> list:
    async def go() -> list:
        client = CachedAsyncSimpleClient(transport, cache, offline=offline)  # type: ignore[arg-type]
        try:
            return await client.get_files(package)
        finally:
            await client.aclose()

    return asyncio.run(go())


def _get_files_and_report(
    transport: object, cache: object, package: str
) -> tuple[list, bool]:
    """Return ``get_files``' records and the client's unreadable-only report.

    The report is per-client state, so it is read while the client the call ran
    on is still in scope.
    """

    async def go() -> tuple[list, bool]:
        client = CachedAsyncSimpleClient(transport, cache)  # type: ignore[arg-type]
        try:
            files = await client.get_files(package)
        finally:
            await client.aclose()
        return files, client.served_unreadable_only(package)

    return asyncio.run(go())


def _get_files_and_zip_sdists(
    transport: object,
    cache: object,
    package: str,
    parsed_stats: ParsedCacheStats | None = None,
) -> tuple[list, frozenset[str]]:
    """Return ``get_files``' records and the releases served as a ``.zip`` sdist.

    Both are per-client state, so they are read while the client the call ran
    on is still in scope.  ``parsed_stats``, when given, records whether the
    read was served from the parsed blob.
    """

    async def go() -> tuple[list, frozenset[str]]:
        client = CachedAsyncSimpleClient(
            transport,  # type: ignore[arg-type]
            cache,  # type: ignore[arg-type]
            parsed_stats=parsed_stats,
        )
        try:
            files = await client.get_files(package)
        finally:
            await client.aclose()
        return files, client.served_zip_sdists(package)

    return asyncio.run(go())


def _fresh_policy() -> CachePolicy:
    return CachePolicy(fetched_at=2_000_000_000, max_age=99999, etag=None)


def _stale_policy() -> CachePolicy:
    return CachePolicy(fetched_at=0, max_age=1, etag=None)


def _stale_etag_policy() -> CachePolicy:
    """A stale policy carrying the ETag a 304 answers."""
    return CachePolicy(fetched_at=0, max_age=1, etag="v1")


class TestNegativeCaching:
    """Name-level 404s are cached as a bounded, revalidating sentinel."""

    def test_404_writes_sentinel_next_read_no_transport(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        online = _FakeTransport([_FakeResponse(b"not found", status=404)])

        assert _run_get_files(online, cache, "absent") == []
        assert len(online.calls) == 1
        assert cache.get_negative("absent") is not None

        raising = _FakeTransport()
        assert _run_get_files(raising, cache, "absent") == []
        assert raising.calls == []

    def test_negative_fresh_below_boundary_no_transport(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1599.0)
        cache = _make_cache(tmp_path)
        cache.put_negative(
            "absent", CachePolicy(fetched_at=1000, max_age=600, etag=None)
        )
        transport = _FakeTransport()

        assert _run_get_files(transport, cache, "absent") == []
        assert transport.calls == []

    def test_negative_stale_at_boundary_online_refetches_and_restamps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1600.0)
        cache = _make_cache(tmp_path)
        cache.put_negative(
            "absent", CachePolicy(fetched_at=1000, max_age=600, etag=None)
        )
        transport = _FakeTransport([_FakeResponse(b"gone", status=404)])

        assert _run_get_files(transport, cache, "absent") == []
        assert len(transport.calls) == 1
        restamped = cache.get_negative("absent")
        assert restamped is not None
        assert restamped.fetched_at == 1600

    def test_stale_negative_online_200_publishes_and_drops(
        self, tmp_path: Path
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_negative("pkg", _stale_policy())
        transport = _FakeTransport(
            [
                _FakeResponse(
                    LISTING_BYTES,
                    headers={"etag": "v1", "cache-control": "max-age=600"},
                )
            ]
        )

        files = _run_get_files(transport, cache, "pkg")
        assert len(files) == 1
        assert cache.get_negative("pkg") is None
        assert cache.get_simple("pkg") is not None

        raising = _FakeTransport()
        assert len(_run_get_files(raising, cache, "pkg")) == 1
        assert raising.calls == []

    def test_404_max_age_capped_at_600(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [_FakeResponse(b"", status=404, headers={"cache-control": "max-age=9999"})]
        )

        _run_get_files(transport, cache, "absent")
        neg = cache.get_negative("absent")
        assert neg is not None
        assert neg.max_age == 600

    def test_404_max_age_from_header_below_cap(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [_FakeResponse(b"", status=404, headers={"cache-control": "max-age=120"})]
        )

        _run_get_files(transport, cache, "absent")
        neg = cache.get_negative("absent")
        assert neg is not None
        assert neg.max_age == 120

    def test_404_uppercase_max_age_directive_is_stored(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(b"", status=404, headers={"cache-control": "Max-Age=0"}),
                _FakeResponse(b"", status=404, headers={"cache-control": "Max-Age=0"}),
            ]
        )

        assert _run_get_files(transport, cache, "absent") == []
        neg = _make_cache(tmp_path).get_negative("absent")
        assert neg is not None
        assert neg.max_age == 0

        assert _run_get_files(transport, cache, "absent") == []
        assert len(transport.calls) == 2

    def test_404_no_cache_control_defaults_600(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(b"", status=404)])

        _run_get_files(transport, cache, "absent")
        neg = cache.get_negative("absent")
        assert neg is not None
        assert neg.max_age == 600

    def test_404_expires_in_the_past_refetches_on_the_next_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = 1_700_000_000.0
        monkeypatch.setattr(time, "time", lambda: now)

        published = json.dumps(
            {
                "meta": {"api-version": "1.0"},
                "name": "absent",
                "files": [
                    {
                        "filename": "absent-1.0-py3-none-any.whl",
                        "url": "https://files.example.com/absent-1.0-py3-none-any.whl",
                    }
                ],
            }
        ).encode()

        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    b"",
                    status=404,
                    headers={
                        "date": formatdate(now, usegmt=True),
                        "expires": formatdate(now - 60, usegmt=True),
                    },
                ),
                _FakeResponse(published, headers={"cache-control": "max-age=600"}),
            ]
        )

        assert _run_get_files(transport, cache, "absent") == []
        neg = cache.get_negative("absent")
        assert neg is not None
        assert neg.max_age == 0

        assert len(_run_get_files(transport, cache, "absent")) == 1
        assert len(transport.calls) == 2

    def test_404_expires_still_capped_at_600(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = 1_700_000_000.0
        monkeypatch.setattr(time, "time", lambda: now)

        cache = _make_cache(tmp_path)
        transport = _FakeTransport(
            [
                _FakeResponse(
                    b"",
                    status=404,
                    headers={
                        "date": formatdate(now, usegmt=True),
                        "expires": formatdate(now + 86400, usegmt=True),
                    },
                )
            ]
        )

        _run_get_files(transport, cache, "absent")
        neg = cache.get_negative("absent")
        assert neg is not None
        assert neg.max_age == 600

    def test_null_cache_never_caches_negative(self, tmp_path: Path) -> None:
        cache = NullCache()
        transport = _FakeTransport(
            [_FakeResponse(b"", status=404), _FakeResponse(b"", status=404)]
        )

        assert _run_get_files(transport, cache, "absent") == []
        assert _run_get_files(transport, cache, "absent") == []
        assert len(transport.calls) == 2

    def test_negative_is_per_index(self, tmp_path: Path) -> None:
        url_a = "https://a.example/simple/"
        url_b = "https://b.example/simple/"
        cache_a = OnDiskCache(tmp_path, url_a)
        cache_b = OnDiskCache(tmp_path, url_b)
        transport_a = _FakeTransport([_FakeResponse(b"", status=404)])

        async def visit_a() -> list:
            client = CachedAsyncSimpleClient(transport_a, cache_a, index_url=url_a)
            try:
                return await client.get_files("absent")
            finally:
                await client.aclose()

        asyncio.run(visit_a())
        assert cache_a.get_negative("absent") is not None
        assert cache_b.get_negative("absent") is None

        transport_b = _FakeTransport([_FakeResponse(b"", status=404)])

        async def visit_b() -> list:
            client = CachedAsyncSimpleClient(transport_b, cache_b, index_url=url_b)
            try:
                return await client.get_files("absent")
            finally:
                await client.aclose()

        assert asyncio.run(visit_b()) == []
        assert len(transport_b.calls) == 1

    def test_negative_shared_across_clients_same_index(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        first = _FakeTransport([_FakeResponse(b"", status=404)])
        assert _run_get_files(first, cache, "absent") == []

        second = _FakeTransport()
        assert _run_get_files(second, cache, "absent") == []
        assert second.calls == []

    def test_offline_serves_fresh_negative(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_negative("absent", _fresh_policy())
        transport = _FakeTransport()

        assert _run_get_files(transport, cache, "absent", offline=True) == []
        assert transport.calls == []

    def test_offline_serves_stale_negative(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_negative("absent", _stale_policy())
        transport = _FakeTransport()

        assert _run_get_files(transport, cache, "absent", offline=True) == []
        assert transport.calls == []

    def test_offline_cold_negative_miss_raises(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport()

        with pytest.raises(OfflineError, match="absent"):
            _run_get_files(transport, cache, "absent", offline=True)
        assert transport.calls == []

    def test_revalidate_404_writes_sentinel(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg", LISTING_BYTES, CachePolicy(fetched_at=0, max_age=1, etag="old")
        )
        transport = _FakeTransport(
            [
                _FakeResponse(
                    b"gone", status=404, headers={"cache-control": "max-age=9999"}
                )
            ]
        )

        assert _run_get_files(transport, cache, "pkg") == []
        neg = cache.get_negative("pkg")
        assert neg is not None
        assert neg.max_age == 600

    def test_present_positive_beats_negative_sentinel(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg",
            LISTING_BYTES,
            CachePolicy(fetched_at=2_000_000_000, max_age=99999, etag="x"),
        )
        cache.put_negative("pkg", _fresh_policy())
        transport = _FakeTransport()

        files = _run_get_files(transport, cache, "pkg")
        assert len(files) == 1
        assert transport.calls == []


def _cached_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "nab_index.cached_client"
    ]


class TestCorruptCachedListing:
    """A structurally corrupt cached Simple body self-heals, loudly.

    A body that will not decode as JSON is treated as a miss: online it
    re-fetches, offline it raises ``OfflineError``. A body that decodes but
    is the wrong shape raises the same error as the wire path when it is
    served, and is replaced or dropped when it is not.
    """

    _TRUNCATED = b'{"files": ['
    _NON_UTF8 = b'\xff\xfe{"files": []}'
    _WRONG_SHAPE = b'{"files": 123}'

    def _fresh(self) -> CachePolicy:
        return CachePolicy(fetched_at=2_000_000_000, max_age=99999, etag=None)

    def test_truncated_cached_body_self_heals_online(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", self._TRUNCATED, self._fresh())
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES, status=200)])

        with caplog.at_level(logging.WARNING, logger="nab_index.cached_client"):
            files = _run_get_files(transport, cache, "pkg")

        cold_cache = _make_cache(tmp_path / "cold")
        cold_transport = _FakeTransport([_FakeResponse(LISTING_BYTES, status=200)])
        cold_files = _run_get_files(cold_transport, cold_cache, "pkg")

        assert [f.filename for f in files] == [f.filename for f in cold_files]
        assert len(transport.calls) == 1
        healed = cache.get_simple("pkg")
        assert healed is not None
        assert healed[0] == LISTING_BYTES
        assert len(_cached_warnings(caplog)) == 1

    def test_non_utf8_cached_body_self_heals_online(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", self._NON_UTF8, self._fresh())
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES, status=200)])

        with caplog.at_level(logging.WARNING, logger="nab_index.cached_client"):
            files = _run_get_files(transport, cache, "pkg")

        assert len(files) == 1
        assert len(transport.calls) == 1
        healed = cache.get_simple("pkg")
        assert healed is not None
        assert healed[0] == LISTING_BYTES
        assert len(_cached_warnings(caplog)) == 1

    def test_over_nested_cached_body_self_heals_online(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        refuse_over_nested: Callable[[bytes], AbstractContextManager[None]],
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", OVER_NESTED, self._fresh())
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES, status=200)])

        with (
            refuse_over_nested(OVER_NESTED),
            caplog.at_level(logging.WARNING, logger="nab_index.cached_client"),
        ):
            files = _run_get_files(transport, cache, "pkg")

        assert len(files) == 1
        assert len(transport.calls) == 1
        healed = cache.get_simple("pkg")
        assert healed is not None
        assert healed[0] == LISTING_BYTES

        (warning,) = _cached_warnings(caplog)
        assert "nested too deeply to decode" in warning.getMessage()

    def test_corrupt_cached_body_offline_raises(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", self._TRUNCATED, self._fresh())
        transport = _FakeTransport()

        with (
            caplog.at_level(logging.WARNING, logger="nab_index.cached_client"),
            pytest.raises(OfflineError, match="pkg"),
        ):
            _run_get_files(transport, cache, "pkg", offline=True)

        assert transport.calls == []
        assert len(_cached_warnings(caplog)) == 1

    def test_corrupt_positive_beats_fresh_negative_online(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", self._TRUNCATED, self._fresh())
        cache.put_negative("pkg", _fresh_policy())
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES, status=200)])

        with caplog.at_level(logging.WARNING, logger="nab_index.cached_client"):
            files = _run_get_files(transport, cache, "pkg")

        assert len(files) == 1
        assert len(transport.calls) == 1
        healed = cache.get_simple("pkg")
        assert healed is not None
        assert healed[0] == LISTING_BYTES
        assert cache.get_negative("pkg") is None
        assert len(_cached_warnings(caplog)) == 1

    def test_corrupt_positive_beats_fresh_negative_offline(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", self._TRUNCATED, self._fresh())
        cache.put_negative("pkg", _fresh_policy())
        transport = _FakeTransport()

        with (
            caplog.at_level(logging.WARNING, logger="nab_index.cached_client"),
            pytest.raises(OfflineError, match="pkg"),
        ):
            _run_get_files(transport, cache, "pkg", offline=True)

        assert transport.calls == []
        assert len(_cached_warnings(caplog)) == 1

    def test_parseable_wrong_shape_does_not_self_heal(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", self._WRONG_SHAPE, self._fresh())
        transport = _FakeTransport()

        with (
            caplog.at_level(logging.WARNING, logger="nab_index.cached_client"),
            pytest.raises(MalformedSimpleResponseError) as caught,
        ):
            _run_get_files(transport, cache, "pkg")

        assert transport.calls == []
        assert _cached_warnings(caplog) == []

        with pytest.raises(MalformedSimpleResponseError) as wire:
            _parse_files(
                json.loads(self._WRONG_SHAPE), "https://pypi.org/simple/", "pkg"
            )
        assert str(caught.value) == str(wire.value)

    def test_stale_wrong_shape_is_replaced_by_a_200(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 200 serves the replacement, so the body it replaces is never parsed."""
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", self._WRONG_SHAPE, _stale_etag_policy())
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES)])

        with caplog.at_level(logging.WARNING, logger="nab_index.cached_client"):
            files = _run_get_files(transport, cache, "pkg")

        assert [file.filename for file in files] == ["pkg-1.0-py3-none-any.whl"]
        assert len(transport.calls) == 1

        healed = cache.get_simple("pkg")
        assert healed is not None
        assert healed[0] == LISTING_BYTES

        # The body decoded, so no self-heal warning is logged.
        assert _cached_warnings(caplog) == []

    def test_stale_wrong_shape_is_dropped_by_a_404(self, tmp_path: Path) -> None:
        """A 404 serves nothing, so the cached body is never parsed."""
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", self._WRONG_SHAPE, _stale_etag_policy())
        transport = _FakeTransport([_FakeResponse(b"", status=404)])

        assert _run_get_files(transport, cache, "pkg") == []
        assert len(transport.calls) == 1
        assert cache.get_negative("pkg") is not None

    def test_stale_wrong_shape_raises_on_a_304_and_stays_stale(
        self, tmp_path: Path
    ) -> None:
        """A 304 serves the cached body, so it raises without extending freshness."""
        cache = _make_cache(tmp_path)
        stale = _stale_etag_policy()
        cache.put_simple("pkg", self._WRONG_SHAPE, stale)
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "v1"})]
        )

        with pytest.raises(MalformedSimpleResponseError):
            _run_get_files(transport, cache, "pkg")

        assert len(transport.calls) == 1

        # Still stale, so a later 200 can replace the body the 304 could not.
        policy = cache.get_simple_policy("pkg")
        assert policy is not None
        assert policy.fetched_at == stale.fetched_at


class TestOversizedListingInt:
    """A listing integer too long to convert reads as an undecodable body.

    ``json.loads`` builds a JSON integer with ``int()``, so a numeric literal
    past CPython's conversion limit raises a bare :class:`ValueError` rather
    than a :class:`json.JSONDecodeError`. PEP 700 makes ``size`` a required
    field, so every api-version 1.1 listing carries one.
    """

    # json.dumps hits the same limit writing the int out, so splice it in.
    _BODY = (
        json.dumps(
            {
                "meta": {"api-version": "1.1"},
                "name": "pkg",
                "files": [
                    {
                        "filename": "pkg-1.0-py3-none-any.whl",
                        "url": "https://files.example.com/pkg-1.0-py3-none-any.whl",
                        "size": "PLACEHOLDER",
                    },
                ],
            }
        )
        .replace('"PLACEHOLDER"', OVERSIZED_DIGITS)
        .encode()
    )

    def test_wire_body_raises_clean_and_skips_cache(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        transport = _FakeTransport([_FakeResponse(self._BODY, status=200)])

        with pytest.raises(
            MalformedSimpleResponseError, match="malformed Simple-API"
        ) as caught:
            _run_get_files(transport, cache, "pkg")

        assert isinstance(caught.value, HttpError)
        assert cache.get_simple("pkg") is None

    def test_cached_body_self_heals_online(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple("pkg", self._BODY, _fresh_policy())
        transport = _FakeTransport([_FakeResponse(LISTING_BYTES, status=200)])

        with caplog.at_level(logging.WARNING, logger="nab_index.cached_client"):
            files = _run_get_files(transport, cache, "pkg")

        assert len(files) == 1
        assert len(transport.calls) == 1
        healed = cache.get_simple("pkg")
        assert healed is not None
        assert healed[0] == LISTING_BYTES
        assert len(_cached_warnings(caplog)) == 1


class TestModuleDocstring:
    """Keep the module docstring in step with the client's constructor."""

    @staticmethod
    def _parse_module() -> tuple[str, ast.Module]:
        source = Path(cached_client_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        doc = ast.get_docstring(tree)
        assert doc is not None
        return doc, tree

    @staticmethod
    def _class_references(doc: str) -> set[str]:
        return {
            target.lstrip("~").split(".")[-1]
            for target in re.findall(r":class:`([^`]+)`", doc)
        }

    @staticmethod
    def _bound_names(tree: ast.Module) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(
                    (alias.asname or alias.name).split(".")[0] for alias in node.names
                )
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
        return names

    @staticmethod
    def _init_annotations(tree: ast.Module) -> dict[str, str]:
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef) and cls.name == "CachedAsyncSimpleClient":
                for item in cls.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        args = item.args
                        return {
                            arg.arg: ast.unparse(arg.annotation)
                            for arg in (*args.args, *args.kwonlyargs)
                            if arg.annotation is not None
                        }
        return {}

    def test_cross_references_only_bound_symbols(self) -> None:
        doc, tree = self._parse_module()
        referenced = self._class_references(doc)
        assert referenced, "module docstring should reference at least one class"
        unbound = referenced - self._bound_names(tree)
        assert not unbound, (
            f"module docstring references unbound symbols: {sorted(unbound)}"
        )

    def test_names_constructor_types(self) -> None:
        doc, tree = self._parse_module()
        referenced = self._class_references(doc)
        annotations = self._init_annotations(tree)
        transport_type = annotations["transport"].split(".")[-1]
        cache_type = annotations["cache"].split(".")[-1]
        assert transport_type in referenced
        assert cache_type in referenced


def _run_get_files_floor(
    transport: object,
    cache: object,
    package: str,
    *,
    min_fresh_seconds: int | None = None,
    offline: bool = False,
) -> list:
    async def go() -> list:
        client = CachedAsyncSimpleClient(
            transport,  # type: ignore[arg-type]
            cache,  # type: ignore[arg-type]
            offline=offline,
            min_fresh_seconds=min_fresh_seconds,
        )
        try:
            return await client.get_files(package)
        finally:
            await client.aclose()

    return asyncio.run(go())


def _debug_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and r.name == "nab_index.cached_client"
    ]


class TestAssumeFreshFloor:
    """Read-time freshness floor: extend, never shorten, and never rewrite disk."""

    def test_stale_positive_within_floor_serves_cached_no_transport(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg", LISTING_BYTES, CachePolicy(fetched_at=500, max_age=1, etag="e")
        )
        transport = _FakeTransport()  # raises on any call

        with caplog.at_level(logging.DEBUG, logger="nab_index.cached_client"):
            files = _run_get_files_floor(
                transport, cache, "pkg", min_fresh_seconds=3600
            )

        assert len(files) == 1
        assert transport.calls == []
        records = _debug_records(caplog)
        assert len(records) == 1
        assert "listing" in records[0].getMessage()

    def test_stale_positive_past_floor_revalidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 200.0)
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg", LISTING_BYTES, CachePolicy(fetched_at=0, max_age=1, etag="old")
        )
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "old"})]
        )

        files = _run_get_files_floor(transport, cache, "pkg", min_fresh_seconds=100)
        assert len(files) == 1
        assert len(transport.calls) == 1

    def test_no_floor_revalidates_and_no_debug_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg", LISTING_BYTES, CachePolicy(fetched_at=0, max_age=1, etag="old")
        )
        transport = _FakeTransport(
            [_FakeResponse(b"", status=304, headers={"etag": "old"})]
        )

        with caplog.at_level(logging.DEBUG, logger="nab_index.cached_client"):
            files = _run_get_files_floor(
                transport, cache, "pkg", min_fresh_seconds=None
            )
        assert len(files) == 1
        assert len(transport.calls) == 1
        assert _debug_records(caplog) == []

    def test_stale_sentinel_within_floor_answers_empty_no_transport(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cache = _make_cache(tmp_path)
        cache.put_negative("absent", CachePolicy(fetched_at=500, max_age=1, etag=None))
        transport = _FakeTransport()

        with caplog.at_level(logging.DEBUG, logger="nab_index.cached_client"):
            result = _run_get_files_floor(
                transport, cache, "absent", min_fresh_seconds=3600
            )

        assert result == []
        assert transport.calls == []
        records = _debug_records(caplog)
        assert len(records) == 1
        assert "absent-name sentinel" in records[0].getMessage()

    def test_stale_sentinel_past_floor_reprobes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cache = _make_cache(tmp_path)
        cache.put_negative("absent", CachePolicy(fetched_at=0, max_age=1, etag=None))
        transport = _FakeTransport([_FakeResponse(b"gone", status=404)])

        result = _run_get_files_floor(transport, cache, "absent", min_fresh_seconds=100)
        assert result == []
        assert len(transport.calls) == 1

    def test_floor_smaller_than_stored_window_is_noop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1300.0)
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg", LISTING_BYTES, CachePolicy(fetched_at=1000, max_age=600, etag="e")
        )
        transport = _FakeTransport()  # raises on any call

        with caplog.at_level(logging.DEBUG, logger="nab_index.cached_client"):
            files = _run_get_files_floor(transport, cache, "pkg", min_fresh_seconds=100)

        assert len(files) == 1
        assert transport.calls == []
        assert _debug_records(caplog) == []

    def test_offline_with_floor_serves_cached_no_helper_no_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = _make_cache(tmp_path)
        cache.put_simple(
            "pkg", LISTING_BYTES, CachePolicy(fetched_at=0, max_age=1, etag="old")
        )
        transport = _FakeTransport()  # raises on any call

        with caplog.at_level(logging.DEBUG, logger="nab_index.cached_client"):
            files = _run_get_files_floor(
                transport, cache, "pkg", min_fresh_seconds=3600, offline=True
            )

        assert len(files) == 1
        assert transport.calls == []
        assert _debug_records(caplog) == []

    def test_stored_policy_unchanged_after_floor_suppressed_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cache = _make_cache(tmp_path)
        stored = CachePolicy(fetched_at=500, max_age=1, etag="e")
        digest = cache.put_simple("pkg", LISTING_BYTES, stored)
        transport = _FakeTransport()

        _run_get_files_floor(transport, cache, "pkg", min_fresh_seconds=3600)

        cached = cache.get_simple("pkg")
        assert cached is not None
        body, policy = cached
        assert body == LISTING_BYTES
        # put_simple stamps the body digest; the suppressed read leaves it be.
        assert policy == replace(stored, body_digest=digest)

    def test_stored_sentinel_unchanged_after_floor_suppressed_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        cache = _make_cache(tmp_path)
        stored = CachePolicy(fetched_at=500, max_age=1, etag=None)
        cache.put_negative("absent", stored)
        transport = _FakeTransport()

        _run_get_files_floor(transport, cache, "absent", min_fresh_seconds=3600)

        assert cache.get_negative("absent") == stored

    def test_immutable_metadata_text_warm_hit_with_floor(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_metadata("pkg", "https://x/pkg.metadata", "stored")
        transport = _FakeTransport()

        async def go() -> str:
            client = CachedAsyncSimpleClient(transport, cache, min_fresh_seconds=3600)
            try:
                return await client.get_metadata_text(
                    "pkg", "1.0", "https://x/pkg.metadata"
                )
            finally:
                await client.aclose()

        assert asyncio.run(go()) == "stored"
        assert transport.calls == []

    def test_immutable_range_metadata_warm_hit_with_floor(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_metadata("pkg", _WHEEL_URL, _RANGE_META.decode())
        transport = _FakeTransport()

        async def go() -> RangeMetadataResult:
            client = CachedAsyncSimpleClient(transport, cache, min_fresh_seconds=3600)
            try:
                return await client.get_range_metadata(
                    "pkg", "1.0", _WHEEL_URL, canonicalize_name("pkg")
                )
            finally:
                await client.aclose()

        result = asyncio.run(go())
        assert result.text == _RANGE_META.decode()
        assert transport.calls == []

    def test_immutable_sdist_files_warm_hit_with_floor(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.put_sdist_files("pkg", "1.0", "Name: cached\n", None)
        transport = _FakeTransport()

        async def go() -> tuple[str | None, str | None]:
            client = CachedAsyncSimpleClient(transport, cache, min_fresh_seconds=3600)
            try:
                return await client.get_sdist_files(
                    "pkg", "1.0", "https://x/pkg.tar.gz"
                )
            finally:
                await client.aclose()

        pkg_info, pyproject = asyncio.run(go())
        assert pkg_info == "Name: cached\n"
        assert pyproject is None
        assert transport.calls == []


class TestRepeatedCacheControlLines:
    """An index that sends Cache-Control on two field lines."""

    _LINES = (("Cache-Control", "public"), ("Cache-Control", "max-age=60"))

    def _stored_policy(
        self, tmp_path: Path, response: _FakeResponse, seed: CachePolicy | None = None
    ) -> CachePolicy:
        """Serve ``response`` to one get_files and return the policy it stored.

        ``seed`` pre-fills the cache, so the request goes out as a
        revalidation rather than a cold fetch.
        """
        cache = _make_cache(tmp_path)
        if seed is not None:
            cache.put_simple("pkg", LISTING_BYTES, seed)

        transport = _FakeTransport([response])

        async def go() -> list:
            client = CachedAsyncSimpleClient(transport, cache)
            try:
                return await client.get_files("pkg")
            finally:
                await client.aclose()

        assert len(asyncio.run(go())) == 1

        cached = cache.get_simple("pkg")
        assert cached is not None
        return cached[1]

    def test_cold_fetch_stores_max_age_from_the_second_line(
        self, tmp_path: Path
    ) -> None:
        response = _FakeResponse(LISTING_BYTES, headers=_field_lines(*self._LINES))

        policy = self._stored_policy(tmp_path, response)

        assert policy.max_age == 60
        assert not policy.is_fresh(policy.fetched_at + 120)

    def test_304_reads_max_age_instead_of_the_heuristic(self, tmp_path: Path) -> None:
        response = _FakeResponse(b"", status=304, headers=_field_lines(*self._LINES))
        seed = CachePolicy(fetched_at=0, max_age=1, etag="v1")

        policy = self._stored_policy(tmp_path, response, seed)

        assert policy.max_age == 60
