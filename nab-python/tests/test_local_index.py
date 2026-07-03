"""Tests for nab_index.local_index.LocalIndexClient."""

from __future__ import annotations

import asyncio
import io
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import pytest

from nab_index.client import SdistFile, WheelFile
from nab_index.local_index import (
    LocalIndexClient,
    _make_record,
    _parse_file_url,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

_T = TypeVar("_T")


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coro)


def _write_wheel(
    path: Path,
    name: str,
    version: str,
    requires_python: str | None = None,
    *,
    with_metadata: bool = True,
) -> None:
    """Write a real (zip) wheel whose METADATA carries ``requires_python``."""
    dist = f"{name}-{version}"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{name}/__init__.py", b"")
        if with_metadata:
            rp = f"Requires-Python: {requires_python}\n" if requires_python else ""
            zf.writestr(
                f"{dist}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{rp}",
            )


class TestParseFileUrl:
    def test_absolute_path(self, tmp_path: Path) -> None:
        url = tmp_path.as_uri()
        assert _parse_file_url(url) == tmp_path

    def test_url_encoding_round_trip(self, tmp_path: Path) -> None:
        # Spaces and unicode in the path must round-trip cleanly.
        # Build under tmp_path so the path is absolute on Windows.
        path = tmp_path / "with space" / "foo"
        path.parent.mkdir(parents=True)
        path.touch()
        url = path.as_uri()
        assert _parse_file_url(url) == path

    def test_rejects_non_file_scheme(self) -> None:
        with pytest.raises(ValueError, match="expected file:// URL"):
            _parse_file_url("https://example.com/")


class TestFlatWheelhouse:
    def test_finds_wheel(self, tmp_path: Path) -> None:
        wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        wheel.write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert isinstance(files[0], WheelFile)
        assert files[0].filename == "foo-1.0-py3-none-any.whl"
        assert files[0].version == "1.0"
        assert files[0].local_path == wheel

    def test_finds_sdist_tar_gz(self, tmp_path: Path) -> None:
        (tmp_path / "foo-1.0.tar.gz").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert isinstance(files[0], SdistFile)
        assert files[0].version == "1.0"

    def test_ignores_sdist_zip(self, tmp_path: Path) -> None:
        (tmp_path / "foo-1.0.zip").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        assert run(client.get_files("foo")) == []

    def test_filters_other_packages(self, tmp_path: Path) -> None:
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        (tmp_path / "bar-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        foo = run(client.get_files("foo"))
        assert len(foo) == 1
        assert foo[0].filename == "foo-1.0-py3-none-any.whl"

    def test_canonical_name_match(self, tmp_path: Path) -> None:
        (tmp_path / "foo_bar-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        # Underscore-vs-dash and case insensitive per PEP 503
        result = run(client.get_files("Foo-Bar"))
        assert len(result) == 1

    def test_skips_non_files(self, tmp_path: Path) -> None:
        (tmp_path / "subdir").mkdir()
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1

    def test_unparseable_filename_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-wheel.whl").write_bytes(b"")
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].filename == "foo-1.0-py3-none-any.whl"

    def test_missing_root_returns_empty(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        client = LocalIndexClient(missing.as_uri())
        assert run(client.get_files("foo")) == []

    def test_requires_python_read_from_wheel_metadata(self, tmp_path: Path) -> None:
        # Requires-Python is absent from the filename; it comes from METADATA.
        _write_wheel(tmp_path / "foo-2.0-py3-none-any.whl", "foo", "2.0", ">=3.12")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert files[0].requires_python == ">=3.12"

    def test_requires_python_none_when_metadata_omits_it(self, tmp_path: Path) -> None:
        _write_wheel(tmp_path / "foo-1.0-py3-none-any.whl", "foo", "1.0")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert files[0].requires_python is None

    def test_requires_python_none_without_metadata_member(self, tmp_path: Path) -> None:
        _write_wheel(
            tmp_path / "foo-1.0-py3-none-any.whl", "foo", "1.0", with_metadata=False
        )
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert files[0].requires_python is None

    def test_requires_python_none_for_unreadable_zip(self, tmp_path: Path) -> None:
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"not a zip")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert files[0].requires_python is None

    def test_foreign_wheel_metadata_not_read(self, tmp_path: Path) -> None:
        # A sibling package's Requires-Python must not leak onto foo.
        _write_wheel(tmp_path / "foo-1.0-py3-none-any.whl", "foo", "1.0", ">=3.8")
        _write_wheel(tmp_path / "bar-1.0-py3-none-any.whl", "bar", "1.0", ">=3.12")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert [f.requires_python for f in files] == [">=3.8"]


class TestPep503Directory:
    def _make_index(self, tmp_path: Path, body: str) -> Path:
        package_dir = tmp_path / "foo"
        package_dir.mkdir()
        (package_dir / "index.html").write_text(body, encoding="utf-8")
        return package_dir

    def test_parses_anchor_tags(self, tmp_path: Path) -> None:
        body = (
            "<html><body>"
            '<a href="foo-1.0-py3-none-any.whl"'
            ' data-requires-python=">=3.10">foo-1.0</a>'
            '<a href="foo-2.0-py3-none-any.whl">foo-2.0</a>'
            "</body></html>"
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        (package_dir / "foo-2.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 2
        v1 = next(r for r in result if r.version == "1.0")
        assert v1.requires_python == ">=3.10"
        v2 = next(r for r in result if r.version == "2.0")
        assert v2.requires_python is None

    def test_relative_href_resolves_against_dir(self, tmp_path: Path) -> None:
        body = '<a href="foo-1.0-py3-none-any.whl">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].url.endswith("foo-1.0-py3-none-any.whl")
        assert (
            result[0].local_path == package_dir.resolve() / "foo-1.0-py3-none-any.whl"
        )

    def test_relative_href_resolves_outside_package_dir(self, tmp_path: Path) -> None:
        simple = tmp_path / "simple"
        package_dir = simple / "foo"
        package_dir.mkdir(parents=True)
        body = '<a href="../../packages/ab/cd/foo-1.0-py3-none-any.whl">foo</a>'
        (package_dir / "index.html").write_text(body, encoding="utf-8")
        wheel_path = tmp_path / "packages" / "ab" / "cd" / "foo-1.0-py3-none-any.whl"
        wheel_path.parent.mkdir(parents=True)
        wheel_path.write_bytes(b"")
        client = LocalIndexClient(simple.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].local_path == wheel_path.resolve()

    def test_https_href_pass_through(self, tmp_path: Path) -> None:
        body = '<a href="https://example.com/foo/foo-1.0-py3-none-any.whl">foo</a>'
        self._make_index(tmp_path, body)
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].url == "https://example.com/foo/foo-1.0-py3-none-any.whl"
        assert result[0].local_path is None

    def test_unrecognised_anchor_skipped(self, tmp_path: Path) -> None:
        body = '<a href="random.txt">junk</a><a href="foo-1.0-py3-none-any.whl">foo</a>'
        self._make_index(tmp_path, body)
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1

    def test_anchor_without_href(self, tmp_path: Path) -> None:
        body = '<a>no href</a><a href="foo-1.0-py3-none-any.whl">foo</a>'
        self._make_index(tmp_path, body)
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1

    def test_missing_index_html_returns_empty(self, tmp_path: Path) -> None:
        # PEP 503 directory exists but no index.html: falls through to flat
        # wheelhouse which is also empty for this package.
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result == []

    def test_file_scheme_href(self, tmp_path: Path) -> None:
        # Uncommon but legal: a file:// scheme on the anchor
        wheel_path = tmp_path / "foo" / "foo-1.0-py3-none-any.whl"
        wheel_path.parent.mkdir()
        wheel_path.write_bytes(b"")
        body = f'<a href="{wheel_path.as_uri()}">foo</a>'
        (tmp_path / "foo" / "index.html").write_text(body, encoding="utf-8")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].filename == "foo-1.0-py3-none-any.whl"
        assert result[0].local_path == wheel_path

    def test_pep503_hash_fragment_extracted(self, tmp_path: Path) -> None:
        digest = "a" * 64
        body = f'<a href="foo-1.0-py3-none-any.whl#sha256={digest}">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].hashes == (("sha256", digest),)

    def test_pep503_hash_fragment_lowercased(self, tmp_path: Path) -> None:
        body = f'<a href="foo-1.0-py3-none-any.whl#sha256={"A" * 64}">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].hashes == (("sha256", "a" * 64),)

    def test_pep503_hash_fragment_algorithm_lowercased(self, tmp_path: Path) -> None:
        body = f'<a href="foo-1.0-py3-none-any.whl#SHA256={"a" * 64}">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].hashes == (("sha256", "a" * 64),)

    def test_pep503_hash_fragment_on_https_href(self, tmp_path: Path) -> None:
        digest = "b" * 64
        body = (
            f'<a href="https://example.com/foo-1.0-py3-none-any.whl#sha256={digest}">'
            "foo</a>"
        )
        self._make_index(tmp_path, body)
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].hashes == (("sha256", digest),)
        assert "#" not in result[0].url

    def test_pep503_hash_fragment_on_file_href(self, tmp_path: Path) -> None:
        wheel_path = tmp_path / "foo" / "foo-1.0-py3-none-any.whl"
        wheel_path.parent.mkdir()
        wheel_path.write_bytes(b"")
        digest = "c" * 64
        body = f'<a href="{wheel_path.as_uri()}#sha256={digest}">foo</a>'
        (tmp_path / "foo" / "index.html").write_text(body, encoding="utf-8")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].hashes == (("sha256", digest),)

    def test_pep503_malformed_fragment_yields_empty_hashes(
        self, tmp_path: Path
    ) -> None:
        body = '<a href="foo-1.0-py3-none-any.whl#=missing-algo">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].hashes == ()

    def test_pep503_sdist_hash_fragment(self, tmp_path: Path) -> None:
        digest = "d" * 64
        body = f'<a href="foo-1.0.tar.gz#sha256={digest}">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0.tar.gz").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].hashes == (("sha256", digest),)

    def test_pep503_zip_sdist_dropped(self, tmp_path: Path) -> None:
        body = '<a href="foo-1.0.zip">foo-zip</a><a href="foo-1.0.tar.gz">foo-sdist</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0.zip").write_bytes(b"")
        (package_dir / "foo-1.0.tar.gz").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert [r.filename for r in result] == ["foo-1.0.tar.gz"]
        assert isinstance(result[0], SdistFile)

    def test_pep503_yanked_link_excluded(self, tmp_path: Path) -> None:
        body = (
            '<a href="foo-1.0-py3-none-any.whl" data-yanked="security">yanked</a>'
            '<a href="foo-2.0-py3-none-any.whl">live</a>'
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        (package_dir / "foo-2.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert [r.version for r in result] == ["2.0"]

    def test_pep503_yanked_with_empty_attr_excluded(self, tmp_path: Path) -> None:
        body = '<a href="foo-1.0-py3-none-any.whl" data-yanked>foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result == []

    def test_pep503_build_tag_sdist_dropped(self, tmp_path: Path) -> None:
        # cffi-1.0.2-2.tar.gz parses as project cffi-1-0-2 at version 2;
        # without the name check it surfaces as a phantom cffi==2.
        package_dir = tmp_path / "cffi"
        package_dir.mkdir()
        body = '<a href="cffi-1.0.2-2.tar.gz">cffi-1.0.2-2.tar.gz</a>'
        (package_dir / "index.html").write_text(body, encoding="utf-8")
        (package_dir / "cffi-1.0.2-2.tar.gz").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        assert run(client.get_files("cffi")) == []

    def test_pep503_foreign_wheel_dropped(self, tmp_path: Path) -> None:
        body = (
            '<a href="bar-1.0-py3-none-any.whl">bar</a>'
            '<a href="foo-1.0-py3-none-any.whl">foo</a>'
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "bar-1.0-py3-none-any.whl").write_bytes(b"")
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert [r.filename for r in result] == ["foo-1.0-py3-none-any.whl"]

    def test_pep503_core_metadata_hash_advertised(self, tmp_path: Path) -> None:
        body = (
            '<a href="foo-1.0-py3-none-any.whl"'
            f' data-core-metadata="sha256={"a" * 64}">foo</a>'
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].has_metadata is True
        assert result[0].metadata_url is not None

    def test_pep503_core_metadata_true_advertised(self, tmp_path: Path) -> None:
        body = '<a href="foo-1.0-py3-none-any.whl" data-core-metadata="true">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].has_metadata is True

    def test_pep503_legacy_metadata_attr_advertised(self, tmp_path: Path) -> None:
        body = (
            '<a href="foo-1.0-py3-none-any.whl" data-dist-info-metadata="true">foo</a>'
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].has_metadata is True

    def test_pep503_no_metadata_attr(self, tmp_path: Path) -> None:
        body = '<a href="foo-1.0-py3-none-any.whl">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].has_metadata is False
        assert result[0].metadata_url is None

    def test_pep503_empty_metadata_attr_not_advertised(self, tmp_path: Path) -> None:
        body = '<a href="foo-1.0-py3-none-any.whl" data-core-metadata="">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].has_metadata is False


class TestMetadataAndSdist:
    def test_get_metadata_text(self, tmp_path: Path) -> None:
        meta_path = tmp_path / "foo-1.0.metadata"
        meta_path.write_text("Name: foo\nVersion: 1.0\n", encoding="utf-8")
        client = LocalIndexClient(tmp_path.as_uri())
        text = run(client.get_metadata_text("foo", "1.0", meta_path.as_uri()))
        assert text.startswith("Name: foo")

    def test_get_sdist_files(self, tmp_path: Path) -> None:
        sdist_path = tmp_path / "foo-1.0.tar.gz"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in [
                ("foo-1.0/PKG-INFO", b"Name: foo\nVersion: 1.0\n"),
                ("foo-1.0/pyproject.toml", b'[project]\nname = "foo"\n'),
            ]:
                data = io.BytesIO(content)
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, data)
        sdist_path.write_bytes(buf.getvalue())
        client = LocalIndexClient(tmp_path.as_uri())
        pkg_info, pyproject = run(
            client.get_sdist_files("foo", "1.0", sdist_path.as_uri())
        )
        assert pkg_info is not None
        assert "foo" in pkg_info
        assert pyproject is not None
        assert 'name = "foo"' in pyproject


class TestMakeRecord:
    def test_unrecognised_extension_returns_none(self) -> None:
        assert (
            _make_record(
                "README.txt",
                "file:///tmp/README.txt",
                Path("/tmp/README.txt"),
                None,
                (),
                "readme",
                has_metadata=False,
            )
            is None
        )


class TestContextManager:
    def test_async_with(self, tmp_path: Path) -> None:
        async def go() -> bool:
            async with LocalIndexClient(tmp_path.as_uri()) as client:
                files = await client.get_files("foo")
                return files == []

        assert run(go())
