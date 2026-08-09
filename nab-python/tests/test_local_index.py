"""Tests for nab_index.local_index.LocalIndexClient."""

from __future__ import annotations

import asyncio
import errno
import io
import struct
import sys
import tarfile
import zipfile
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import pytest

from nab_index.client import SdistFile, WheelFile
from nab_index.errors import IndexAccessError
from nab_index.local_index import (
    LocalIndexClient,
    LocalIndexError,
    MalformedLocalListingError,
    NonLocalArtifactError,
    UnreadableLocalIndexError,
    UnsupportedWheelError,
    _is_zip_sdist,
    _make_record,
    _read_sdist_requires_python,
    parse_file_url,
    read_wheel_metadata,
)
from nab_index.transport import HttpError

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator
    from typing import Any

_T = TypeVar("_T")


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coro)


def _fail_path_call(
    monkeypatch: pytest.MonkeyPatch, method: str, target: Path, error: OSError
) -> None:
    """Make one ``Path`` call on ``target`` raise ``error``."""
    original = getattr(Path, method)

    def failing(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == target:
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, failing)


def _deny_access(monkeypatch: pytest.MonkeyPatch, method: str, target: Path) -> None:
    """Make one ``Path`` call on ``target`` fail with EACCES.

    A real chmod would not do: root ignores the mode bits and Windows has none.
    """
    denied = PermissionError(errno.EACCES, "Permission denied", str(target))
    _fail_path_call(monkeypatch, method, target, denied)


def _swallow_is_file_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give ``Path.is_file`` Python 3.14's error handling on every version.

    From 3.14 it answers False for any :class:`OSError`, so an entry the
    process cannot stat reads as "not a file". The scan does not call it, so
    the patch only bites if someone puts ``entry.is_file()`` back; that is what
    keeps the tests below able to fail on an older interpreter.
    """
    original = Path.is_file

    def is_file(self: Path, *args: Any, **kwargs: Any) -> bool:
        try:
            return original(self, *args, **kwargs)
        except OSError:
            return False

    monkeypatch.setattr(Path, "is_file", is_file)


def _deny_zip_open(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Make opening ``target`` as a zip fail with EACCES.

    ``zipfile`` opens the path itself, so :func:`_deny_access` does not reach
    it, and a real chmod would not do: root ignores the mode bits and Windows
    has none.
    """
    original = zipfile.ZipFile

    def denied(file: Any, *args: Any, **kwargs: Any) -> zipfile.ZipFile:
        if file == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return original(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", denied)


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


def _write_sdist(
    path: Path,
    name: str,
    version: str,
    requires_python: str | None = None,
    *,
    with_pkg_info: bool = True,
) -> None:
    """Write a real (tar.gz) sdist whose PKG-INFO carries ``requires_python``."""
    dist = f"{name}-{version}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if with_pkg_info:
            rp = f"Requires-Python: {requires_python}\n" if requires_python else ""
            body = (
                f"Metadata-Version: 2.2\nName: {name}\nVersion: {version}\n{rp}"
            ).encode()
            info = tarfile.TarInfo(name=f"{dist}/PKG-INFO")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    path.write_bytes(buf.getvalue())


_CLEAN_PREFIX = 1 << 20
_RESERVED_BLOCK_TYPE = b"\x07"
_INVALID_DEFLATE_BLOCK = _RESERVED_BLOCK_TYPE * 8


def _write_corrupt_wheel(path: Path, name: str, version: str) -> None:
    """Write a wheel whose METADATA holds a corrupt deflate stream.

    The zip's central directory is left intact, so the archive opens and lists its
    members; only reading METADATA hits the reserved block type zlib rejects.
    """
    member = f"{name}-{version}.dist-info/METADATA"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            member,
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            "Requires-Python: >=3.12\n",
        )
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(member)

    # A local file header is 30 fixed bytes, then the name and extra fields, then
    # the member's compressed data.
    raw = bytearray(path.read_bytes())
    name_len, extra_len = struct.unpack_from("<HH", raw, info.header_offset + 26)
    start = info.header_offset + 30 + name_len + extra_len

    raw[start : start + info.compress_size] = _RESERVED_BLOCK_TYPE * info.compress_size
    path.write_bytes(bytes(raw))


def _write_corrupt_lzma_wheel(path: Path, name: str, version: str) -> None:
    """Write a wheel whose LZMA-compressed METADATA holds a corrupt stream.

    The central directory and the member's LZMA header stay intact, so the archive
    opens and lists its members; only the raw stream after the header is overwritten,
    which lzma rejects when it decodes METADATA.
    """
    member = f"{name}-{version}.dist-info/METADATA"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_LZMA) as zf:
        zf.writestr(
            member,
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            "Requires-Python: >=3.12\n",
        )
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(member)

    # Local file header is 30 fixed bytes plus name and extra fields; the ZIP LZMA
    # member then has a 4-byte header and props before the raw stream.
    raw = bytearray(path.read_bytes())
    name_len, extra_len = struct.unpack_from("<HH", raw, info.header_offset + 26)
    start = info.header_offset + 30 + name_len + extra_len
    props_size = struct.unpack_from("<H", raw, start + 2)[0]
    payload = start + 4 + props_size
    raw[payload : start + info.compress_size] = b"\xff" * (
        start + info.compress_size - payload
    )
    path.write_bytes(bytes(raw))


def _write_corrupt_sdist(path: Path, name: str, version: str) -> None:
    """Write an sdist whose PKG-INFO body is behind a corrupt deflate block.

    The clean prefix is longer than the reader's decompression buffer, so the
    archive opens and its tar headers read; only reading PKG-INFO's body hits the
    reserved block type zlib rejects.
    """
    body = (
        f"Metadata-Version: 2.2\nName: {name}\nVersion: {version}\n\n"
        + "filler\n" * (2 * _CLEAN_PREFIX // 7)
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=f"{name}-{version}/PKG-INFO")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))

    deflate = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    clean = deflate.compress(buf.getvalue()[:_CLEAN_PREFIX])
    path.write_bytes(clean + deflate.flush(zlib.Z_SYNC_FLUSH) + _INVALID_DEFLATE_BLOCK)


def _patch_wheel_member(
    path: Path, member: str, *, method: int | None = None, encrypt: bool = False
) -> None:
    """Patch a member's compression method or encrypted flag in both zip headers.

    zipfile reads these from the central directory; both headers are patched so the
    archive stays internally consistent.
    """
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(member)
    raw = bytearray(path.read_bytes())

    # Local file header: compression method at +8, general-purpose flag at +6.
    local = info.header_offset
    if method is not None:
        struct.pack_into("<H", raw, local + 8, method)
    if encrypt:
        struct.pack_into(
            "<H", raw, local + 6, struct.unpack_from("<H", raw, local + 6)[0] | 0x1
        )

    # Central directory entry: compression method at +10, general-purpose flag at +8.
    eocd = raw.rfind(b"PK\x05\x06")
    central = struct.unpack_from("<I", raw, eocd + 16)[0]
    name_bytes = member.encode()
    while central + 4 <= len(raw) and raw[central : central + 4] == b"PK\x01\x02":
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", raw, central + 28)
        if raw[central + 46 : central + 46 + name_len] == name_bytes:
            if method is not None:
                struct.pack_into("<H", raw, central + 10, method)
            if encrypt:
                struct.pack_into(
                    "<H",
                    raw,
                    central + 8,
                    struct.unpack_from("<H", raw, central + 8)[0] | 0x1,
                )
            break
        central += 46 + name_len + extra_len + comment_len
    path.write_bytes(bytes(raw))


def _write_unsupported_compression_wheel(
    path: Path, name: str, version: str, method: int = 9
) -> None:
    """Write a wheel whose METADATA member declares an unsupported compression method.

    The member is stored, then patched to ``method`` (Deflate64 is 9). The archive
    opens and lists its members; only reading METADATA hits zipfile's compression
    check.
    """
    member = f"{name}-{version}.dist-info/METADATA"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(
            member,
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            "Requires-Python: >=3.12\n",
        )
    _patch_wheel_member(path, member, method=method)


def _write_encrypted_metadata_wheel(path: Path, name: str, version: str) -> None:
    """Write a wheel whose METADATA member has the encrypted flag set.

    The archive opens and lists its members; only reading METADATA hits the encrypted
    flag, which zipfile refuses without a password.
    """
    member = f"{name}-{version}.dist-info/METADATA"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(
            member,
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            "Requires-Python: >=3.12\n",
        )
    _patch_wheel_member(path, member, encrypt=True)


_LOCAL_ERRORS = [
    UnreadableLocalIndexError,
    MalformedLocalListingError,
    NonLocalArtifactError,
]


class TestErrorHierarchy:
    """What a caller catches for either index backend, and what it does not."""

    @pytest.mark.parametrize("error", _LOCAL_ERRORS)
    def test_local_errors_are_index_access_errors(
        self, error: type[LocalIndexError]
    ) -> None:
        assert issubclass(error, LocalIndexError)
        assert issubclass(error, IndexAccessError)

    @pytest.mark.parametrize("error", _LOCAL_ERRORS)
    def test_local_errors_are_not_http_errors(
        self, error: type[LocalIndexError]
    ) -> None:
        # A file:// index makes no request, so naming one of these an HTTP
        # failure would send a reader looking for a server that isn't there.
        assert not issubclass(error, HttpError)

    def test_http_errors_are_index_access_errors(self) -> None:
        assert issubclass(HttpError, IndexAccessError)


class TestParseFileUrl:
    def test_absolute_path(self, tmp_path: Path) -> None:
        url = tmp_path.as_uri()
        assert parse_file_url(url) == tmp_path

    def test_url_encoding_round_trip(self, tmp_path: Path) -> None:
        # Spaces and unicode in the path must round-trip cleanly.
        # Build under tmp_path so the path is absolute on Windows.
        path = tmp_path / "with space" / "foo"
        path.parent.mkdir(parents=True)
        path.touch()
        url = path.as_uri()
        assert parse_file_url(url) == path

    def test_rejects_non_file_scheme(self) -> None:
        with pytest.raises(ValueError, match="expected file:// URL"):
            parse_file_url("https://example.com/")

    def test_rejects_null_character(self) -> None:
        with pytest.raises(ValueError, match="null character"):
            parse_file_url("file:///srv/sub%00dir/foo-1.0-py3-none-any.whl")

    def test_localhost_authority_is_local(self, tmp_path: Path) -> None:
        # RFC 8089: a "localhost" authority resolves like an empty one.
        with_host = tmp_path.as_uri().replace("file://", "file://localhost", 1)
        assert parse_file_url(with_host) == tmp_path

    def test_remote_authority_rejected_off_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(ValueError, match="non-local file://"):
            parse_file_url("file://otherhost/srv/wheels")

    def test_remote_authority_is_unc_on_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        with_host = str(parse_file_url("file://server/srv/wheels"))
        without_host = str(parse_file_url("file:///srv/wheels"))
        assert "server" in with_host
        assert "server" not in without_host


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
        assert client.served_unreadable_only("foo")

    @pytest.mark.parametrize(
        "filename",
        [
            pytest.param("bar-1.0.zip", id="another-package"),
            pytest.param("foo-notaversion.zip", id="unparseable-version"),
            pytest.param("foo-1.5.win32.exe", id="installer-names-no-package"),
        ],
    )
    def test_unmatched_file_is_not_an_unreadable_listing(
        self, tmp_path: Path, filename: str
    ) -> None:
        """A wheelhouse serves every package, so only a named dist counts."""
        (tmp_path / filename).write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        assert run(client.get_files("foo")) == []
        assert not client.served_unreadable_only("foo")

    def test_zip_sdist_check_rejects_oversized_version(self) -> None:
        """An oversized version answers False instead of raising.

        Called directly because the filename is longer than any filesystem
        allows, so it cannot arrive from a directory scan.
        """
        oversized = "1" * (sys.get_int_max_str_digits() + 1)
        assert not _is_zip_sdist(f"foo-{oversized}.zip", "foo")

    def test_zip_beside_readable_wheel_is_not_unreadable_only(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "foo-1.0.zip").write_bytes(b"")
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        assert len(run(client.get_files("foo"))) == 1
        assert not client.served_unreadable_only("foo")

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

    def test_relative_root_lists_both_layouts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cwd-relative ``file:`` root names artefacts by absolute URI."""
        root = tmp_path / "idx"
        (root / "foo").mkdir(parents=True)
        (root / "foo" / "index.html").write_text(
            '<a href="foo-1.0-py3-none-any.whl">foo-1.0-py3-none-any.whl</a>',
            encoding="utf-8",
        )
        listed = root / "foo" / "foo-1.0-py3-none-any.whl"
        listed.write_bytes(b"")

        flat = root / "bar-2.0-py3-none-any.whl"
        flat.write_bytes(b"")

        monkeypatch.chdir(tmp_path)
        client = LocalIndexClient("file:idx")

        pep503 = run(client.get_files("foo"))
        assert [f.url for f in pep503] == [listed.as_uri()]
        assert [f.local_path for f in pep503] == [listed]

        wheelhouse = run(client.get_files("bar"))
        assert [f.url for f in wheelhouse] == [flat.as_uri()]
        assert [f.local_path for f in wheelhouse] == [flat]

    def test_unreadable_root_raises_index_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wheelhouse that cannot be listed must raise, not return an empty
        # list: an empty result would read as "package absent".
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        _deny_access(monkeypatch, "iterdir", tmp_path)
        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(UnreadableLocalIndexError) as caught:
            run(client.get_files("foo"))
        assert isinstance(caught.value, IndexAccessError)
        assert not isinstance(caught.value, OSError)
        assert "Permission denied" in str(caught.value)

    def test_unreadable_entry_raises_index_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A release the process cannot stat must fail the listing rather than
        # drop out of it, which would read as "1.0 is all there is".
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        denied = tmp_path / "foo-2.0-py3-none-any.whl"
        denied.write_bytes(b"")

        _swallow_is_file_errors(monkeypatch)
        _deny_access(monkeypatch, "stat", denied)

        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(UnreadableLocalIndexError) as caught:
            run(client.get_files("foo"))

        assert isinstance(caught.value, IndexAccessError)
        assert not isinstance(caught.value, OSError)
        assert "Permission denied" in str(caught.value)

    def test_unreadable_entry_of_another_package_raises_index_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wheelhouse serves every package from one directory, so an entry
        # naming none of them still fails the listing.
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        denied = tmp_path / "bar-2.0-py3-none-any.whl"
        denied.write_bytes(b"")

        _swallow_is_file_errors(monkeypatch)
        _deny_access(monkeypatch, "stat", denied)

        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(UnreadableLocalIndexError):
            run(client.get_files("foo"))

    def test_symlink_cycle_entry_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A junk entry that stats as a symlink loop is not a wheelhouse fault.

        Faked rather than made with :func:`os.symlink`, which needs a
        privilege the Windows CI runner does not have.
        """
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        cycle = tmp_path / "cycle-link"
        cycle.write_bytes(b"")

        loop = OSError(errno.ELOOP, "Too many levels of symbolic links", str(cycle))
        _fail_path_call(monkeypatch, "stat", cycle, loop)

        client = LocalIndexClient(tmp_path.as_uri())
        assert [f.version for f in run(client.get_files("foo"))] == ["1.0"]

    def test_entry_gone_since_listing_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A name the directory no longer holds drops out of the listing."""
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        original = Path.iterdir

        def iterdir(self: Path) -> Iterator[Path]:
            yield from original(self)
            yield self / "foo-2.0-py3-none-any.whl"

        monkeypatch.setattr(Path, "iterdir", iterdir)

        client = LocalIndexClient(tmp_path.as_uri())
        assert [f.version for f in run(client.get_files("foo"))] == ["1.0"]

    def test_listing_order_independent_of_readdir_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # These dists tie on every ordering rule, so only the directory-entry
        # order can separate them.
        names = [
            "foo-1.0-py3-none-any.whl",
            "foo-1.0-py2.py3-none-any.whl",
            "foo-1.0.tar.gz",
        ]
        for name in names:
            (tmp_path / name).write_bytes(b"")

        client = LocalIndexClient(tmp_path.as_uri())
        real_iterdir = Path.iterdir

        def listing(*, reverse: bool) -> list[str]:
            def fake_iterdir(self: Path) -> Iterator[Path]:
                return iter(sorted(real_iterdir(self), reverse=reverse))

            monkeypatch.setattr(Path, "iterdir", fake_iterdir)
            return [f.filename for f in run(client.get_files("foo"))]

        assert listing(reverse=False) == listing(reverse=True) == sorted(names)

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

    def test_requires_python_none_for_corrupt_zip(self, tmp_path: Path) -> None:
        # A flat-wheelhouse wheel carries no published hash, so a corrupt one
        # reaches the reader.
        _write_corrupt_wheel(tmp_path / "foo-1.0-py3-none-any.whl", "foo", "1.0")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert files[0].requires_python is None

    def test_requires_python_none_for_unsupported_compression(
        self, tmp_path: Path
    ) -> None:
        _write_unsupported_compression_wheel(
            tmp_path / "foo-1.0-py3-none-any.whl", "foo", "1.0"
        )
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert files[0].requires_python is None

    def test_requires_python_none_for_encrypted_member(self, tmp_path: Path) -> None:
        _write_encrypted_metadata_wheel(
            tmp_path / "foo-1.0-py3-none-any.whl", "foo", "1.0"
        )
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert files[0].requires_python is None

    def test_requires_python_none_for_corrupt_lzma(self, tmp_path: Path) -> None:
        _write_corrupt_lzma_wheel(tmp_path / "foo-1.0-py3-none-any.whl", "foo", "1.0")
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

    def test_flat_wheelhouse_skips_mismatched_dist_info(self, tmp_path: Path) -> None:
        # A foo wheel carrying a bar .dist-info has no readable Requires-Python;
        # the good sibling still lists.
        mismatched = tmp_path / "foo-1.0-py3-none-any.whl"
        with zipfile.ZipFile(mismatched, "w") as zf:
            zf.writestr(
                "bar-2.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: bar\nRequires-Python: >=3.12\n",
            )
        _write_wheel(tmp_path / "foo-2.0-py3-none-any.whl", "foo", "2.0", ">=3.8")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert {f.version: f.requires_python for f in files} == {
            "1.0": None,
            "2.0": ">=3.8",
        }

    def test_sdist_requires_python_read_from_pkg_info(self, tmp_path: Path) -> None:
        # Requires-Python is absent from the filename; it comes from PKG-INFO.
        _write_sdist(tmp_path / "foo-1.0.tar.gz", "foo", "1.0", ">=3.12")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert isinstance(files[0], SdistFile)
        assert files[0].requires_python == ">=3.12"

    def test_sdist_requires_python_none_when_pkg_info_omits_it(
        self, tmp_path: Path
    ) -> None:
        _write_sdist(tmp_path / "foo-1.0.tar.gz", "foo", "1.0")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert files[0].requires_python is None

    def test_sdist_requires_python_none_for_unreadable_archive(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "foo-1.0.tar.gz").write_bytes(b"not a gzip")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert files[0].requires_python is None

    def test_sdist_requires_python_none_for_truncated_archive(
        self, tmp_path: Path
    ) -> None:
        # The sdist cannot be read, so the version lists with no Requires-Python.
        path = tmp_path / "foo-1.0.tar.gz"
        _write_sdist(path, "foo", "1.0", ">=3.12")
        whole = path.read_bytes()
        path.write_bytes(whole[: len(whole) // 2])
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert files[0].requires_python is None

    def test_sdist_requires_python_none_for_corrupt_archive(
        self, tmp_path: Path
    ) -> None:
        # A flat-wheelhouse sdist carries no published hash, so a corrupt one
        # reaches the reader.
        _write_corrupt_sdist(tmp_path / "foo-1.0.tar.gz", "foo", "1.0")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert len(files) == 1
        assert files[0].requires_python is None

    def test_foreign_sdist_metadata_not_read(self, tmp_path: Path) -> None:
        # A sibling package's sdist Requires-Python must not leak onto foo.
        _write_sdist(tmp_path / "foo-1.0.tar.gz", "foo", "1.0", ">=3.8")
        _write_sdist(tmp_path / "bar-1.0.tar.gz", "bar", "1.0", ">=3.12")
        client = LocalIndexClient(tmp_path.as_uri())
        files = run(client.get_files("foo"))
        assert [f.requires_python for f in files] == [">=3.8"]

    def test_read_sdist_requires_python_missing_file(self, tmp_path: Path) -> None:
        assert _read_sdist_requires_python(tmp_path / "absent.tar.gz") is None


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

    def test_page_of_zip_sdists_is_an_unreadable_listing(self, tmp_path: Path) -> None:
        body = '<a href="foo-1.0.zip">foo-1.0</a><a href="foo-2.0.zip">foo-2.0</a>'
        self._make_index(tmp_path, body)
        client = LocalIndexClient(tmp_path.as_uri())
        assert run(client.get_files("foo")) == []
        assert client.served_unreadable_only("foo")

    def test_page_of_other_names_is_not_an_unreadable_listing(
        self, tmp_path: Path
    ) -> None:
        """A page listing another project's wheel is a mismatch, not a format."""
        body = '<a href="bar-1.0-py3-none-any.whl">bar-1.0</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "bar-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        assert run(client.get_files("foo")) == []
        assert not client.served_unreadable_only("foo")

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

    def test_relative_href_with_query_names_the_artifact(self, tmp_path: Path) -> None:
        # RFC 3986 puts the query outside the path, so a cache-busting query
        # on a relative link is not part of the filename.
        digest = "b" * 64
        body = f'<a href="foo-1.0-py3-none-any.whl?rev=7#sha256={digest}">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        wheel_path = package_dir / "foo-1.0-py3-none-any.whl"
        wheel_path.write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        (record,) = run(client.get_files("foo"))
        assert record.filename == "foo-1.0-py3-none-any.whl"
        assert record.version == "1.0"
        assert record.local_path == wheel_path.resolve()
        assert parse_file_url(record.url) == wheel_path.resolve()
        assert record.hashes == (("sha256", digest),)

    def test_relative_href_wrapped_in_whitespace_names_the_artifact(
        self, tmp_path: Path
    ) -> None:
        # HTML allows a URL attribute's value to be surrounded by whitespace.
        body = '<a href="\n      foo-1.0-py3-none-any.whl\n    ">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        wheel_path = package_dir / "foo-1.0-py3-none-any.whl"
        wheel_path.write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        (record,) = run(client.get_files("foo"))
        assert record.filename == "foo-1.0-py3-none-any.whl"
        assert record.local_path == wheel_path.resolve()

    def test_base_href_redirects_relative_anchor(self, tmp_path: Path) -> None:
        # An absolute <base href> moves the resolution base off the page
        # directory; the relative anchor must land on the real wheel there.
        packages = tmp_path / "packages"
        packages.mkdir()
        wheel_path = packages / "foo-1.0-py3-none-any.whl"
        wheel_path.write_bytes(b"")
        package_dir = tmp_path / "foo"
        package_dir.mkdir()
        body = (
            f'<base href="{packages.as_uri()}/">'
            '<a href="foo-1.0-py3-none-any.whl">foo</a>'
        )
        (package_dir / "index.html").write_text(body, encoding="utf-8")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].local_path == wheel_path
        assert parse_file_url(result[0].url) == wheel_path

    def test_base_href_relative_resolves_against_page(self, tmp_path: Path) -> None:
        # A relative <base href> resolves against the index page URL, so a
        # sibling directory is reachable from the listing.
        packages = tmp_path / "packages"
        packages.mkdir()
        wheel_path = packages / "foo-1.0-py3-none-any.whl"
        wheel_path.write_bytes(b"")
        package_dir = tmp_path / "foo"
        package_dir.mkdir()
        body = '<base href="../packages/"><a href="foo-1.0-py3-none-any.whl">foo</a>'
        (package_dir / "index.html").write_text(body, encoding="utf-8")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].local_path == wheel_path

    def test_first_base_href_wins(self, tmp_path: Path) -> None:
        packages = tmp_path / "packages"
        packages.mkdir()
        wheel_path = packages / "foo-1.0-py3-none-any.whl"
        wheel_path.write_bytes(b"")
        package_dir = tmp_path / "foo"
        package_dir.mkdir()
        body = (
            f'<base href="{packages.as_uri()}/">'
            f'<base href="{tmp_path.as_uri()}/other/">'
            '<a href="foo-1.0-py3-none-any.whl">foo</a>'
        )
        (package_dir / "index.html").write_text(body, encoding="utf-8")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].local_path == wheel_path

    def test_base_without_href_ignored(self, tmp_path: Path) -> None:
        body = '<base target="_blank"><a href="foo-1.0-py3-none-any.whl">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert (
            result[0].local_path == package_dir.resolve() / "foo-1.0-py3-none-any.whl"
        )

    def test_base_href_ignored_by_absolute_anchor(self, tmp_path: Path) -> None:
        body = (
            f'<base href="{tmp_path.as_uri()}/packages/">'
            '<a href="https://example.com/foo/foo-1.0-py3-none-any.whl">foo</a>'
        )
        self._make_index(tmp_path, body)
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert len(result) == 1
        assert result[0].url == "https://example.com/foo/foo-1.0-py3-none-any.whl"
        assert result[0].local_path is None

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

    def test_non_utf8_index_html_raises_index_error(self, tmp_path: Path) -> None:
        # A non-UTF-8 listing must raise, not return an empty list: an empty
        # result would read as "package absent".
        package_dir = tmp_path / "foo"
        package_dir.mkdir()
        (package_dir / "index.html").write_bytes(
            b'<a href="foo-1.0-py3-none-any.whl">foo-1.0</a>\xff\xfe'
        )
        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(MalformedLocalListingError) as caught:
            run(client.get_files("foo"))
        assert isinstance(caught.value, IndexAccessError)
        assert "not valid UTF-8" in str(caught.value)

    def test_unreadable_index_html_raises_index_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        package_dir = self._make_index(
            tmp_path, '<a href="foo-1.0-py3-none-any.whl">foo-1.0</a>'
        )
        _deny_access(monkeypatch, "read_text", package_dir / "index.html")
        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(UnreadableLocalIndexError) as caught:
            run(client.get_files("foo"))
        assert isinstance(caught.value, IndexAccessError)
        assert not isinstance(caught.value, OSError)
        assert "Permission denied" in str(caught.value)

    def test_unreadable_package_dir_raises_index_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The layout probe stats <package>/index.html, so an unreadable package
        # directory fails there.
        package_dir = self._make_index(
            tmp_path, '<a href="foo-1.0-py3-none-any.whl">foo-1.0</a>'
        )
        _deny_access(monkeypatch, "stat", package_dir / "index.html")
        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(UnreadableLocalIndexError) as caught:
            run(client.get_files("foo"))
        assert isinstance(caught.value, IndexAccessError)
        assert not isinstance(caught.value, OSError)
        assert "Permission denied" in str(caught.value)

    def test_pep503_non_local_file_href_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A file:// href a local client cannot serve (non-local authority)
        # drops just that anchor; the rest of the listing is kept.
        monkeypatch.setattr(sys, "platform", "linux")
        body = (
            '<a href="file://otherhost/foo-1.0-py3-none-any.whl">foo-1.0</a>'
            '<a href="foo-2.0-py3-none-any.whl">foo-2.0</a>'
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-2.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert [r.version for r in result] == ["2.0"]

    def test_pep503_null_byte_href_dropped(self, tmp_path: Path) -> None:
        # A percent-encoded null byte makes path resolution raise ValueError;
        # drop that anchor and keep the good sibling wheel.
        body = (
            '<a href="foo%001.0.whl">foo-bad</a>'
            '<a href="foo-2.0-py3-none-any.whl">foo-2.0</a>'
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-2.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert [r.version for r in result] == ["2.0"]

    def test_pep503_null_byte_directory_href_dropped(self, tmp_path: Path) -> None:
        # The null byte need not sit in the filename: the guard covers the
        # whole path, not just its last segment.
        body = (
            '<a href="sub%00dir/foo-1.0-py3-none-any.whl">foo-bad</a>'
            '<a href="foo-2.0-py3-none-any.whl">foo-2.0</a>'
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-2.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert [r.version for r in result] == ["2.0"]

    def test_pep503_malformed_ipv6_href_dropped(self, tmp_path: Path) -> None:
        # An unterminated IPv6 bracket makes the href join raise ValueError
        # before the scheme is known; drop that anchor and keep the sibling.
        body = (
            '<a href="http://[bad">foo-bad</a>'
            '<a href="foo-2.0-py3-none-any.whl">foo-2.0</a>'
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-2.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert [r.version for r in result] == ["2.0"]

    def test_pep503_malformed_ipv6_href_under_valid_base_dropped(
        self, tmp_path: Path
    ) -> None:
        # A usable <base href> does not turn one unparseable anchor into a
        # page failure: the anchor is dropped and its sibling kept.
        body = (
            '<base href="https://mirror.example/simple/foo/">'
            '<a href="http://[bad">foo-bad</a>'
            '<a href="foo-2.0-py3-none-any.whl">foo-2.0</a>'
        )
        self._make_index(tmp_path, body)
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert [r.version for r in result] == ["2.0"]

    def test_pep503_malformed_base_href_raises(self, tmp_path: Path) -> None:
        # A <base href> that cannot be parsed leaves every relative anchor's
        # target unknown, so the page fails rather than silently resolving
        # links against the package directory instead.
        body = '<base href="http://[bad"><a href="foo-1.0-py3-none-any.whl">foo-1.0</a>'
        self._make_index(tmp_path, body)
        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(MalformedLocalListingError, match="base href"):
            run(client.get_files("foo"))

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

    def test_pep503_hash_fragment_beside_subdirectory(self, tmp_path: Path) -> None:
        digest = "e" * 64
        body = (
            f'<a href="foo-1.0-py3-none-any.whl#sha256={digest}'
            '&amp;subdirectory=pkg">foo</a>'
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].hashes == (("sha256", digest),)

    def test_pep503_fragment_keeps_every_hash_part(self, tmp_path: Path) -> None:
        sha256, sha512 = "e" * 64, "f" * 128
        body = (
            f'<a href="foo-1.0-py3-none-any.whl#sha256={sha256}'
            f'&amp;sha512={sha512}">foo</a>'
        )
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].hashes == (("sha256", sha256), ("sha512", sha512))

    def test_pep503_egg_fragment_yields_no_hashes(self, tmp_path: Path) -> None:
        body = '<a href="foo-1.0-py3-none-any.whl#egg=foo-1.0">foo</a>'
        package_dir = self._make_index(tmp_path, body)
        (package_dir / "foo-1.0-py3-none-any.whl").write_bytes(b"")
        client = LocalIndexClient(tmp_path.as_uri())
        result = run(client.get_files("foo"))
        assert result[0].hashes == ()

    def test_pep503_empty_digest_yields_no_hashes(self, tmp_path: Path) -> None:
        body = '<a href="foo-1.0-py3-none-any.whl#sha256=&amp;egg=foo-1.0">foo</a>'
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

    def test_non_utf8_metadata_sidecar_raises_index_error(self, tmp_path: Path) -> None:
        # A non-UTF-8 sidecar must fail through the index-error path like the
        # index.html reader, not a raw UnicodeDecodeError.
        meta_path = tmp_path / "foo-1.0.metadata"
        meta_path.write_bytes(b"Author: J\xe9an\n")
        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(MalformedLocalListingError) as caught:
            run(client.get_metadata_text("foo", "1.0", meta_path.as_uri()))
        assert isinstance(caught.value, IndexAccessError)
        assert "not valid UTF-8" in str(caught.value)

    def test_missing_metadata_sidecar_raises_index_error(self, tmp_path: Path) -> None:
        # An advertised-but-absent sidecar must fail through the index-error
        # path, not a raw FileNotFoundError, matching a remote 404.
        meta_path = tmp_path / "foo-1.0.metadata"
        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(UnreadableLocalIndexError) as caught:
            run(client.get_metadata_text("foo", "1.0", meta_path.as_uri()))
        assert isinstance(caught.value, IndexAccessError)
        assert not isinstance(caught.value, OSError)

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

    def test_missing_sdist_files_raises_index_error(self, tmp_path: Path) -> None:
        # An advertised-but-absent sdist must fail through the index-error
        # path, not a raw FileNotFoundError, matching a remote 404.
        sdist_path = tmp_path / "foo-1.0.tar.gz"
        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(UnreadableLocalIndexError) as caught:
            run(client.get_sdist_files("foo", "1.0", sdist_path.as_uri()))
        assert isinstance(caught.value, IndexAccessError)
        assert not isinstance(caught.value, OSError)

    def test_missing_sdist_archive_raises_index_error(self, tmp_path: Path) -> None:
        sdist_path = tmp_path / "foo-1.0.tar.gz"
        client = LocalIndexClient(tmp_path.as_uri())
        with pytest.raises(UnreadableLocalIndexError) as caught:
            run(client.get_sdist_archive("foo", "1.0", sdist_path.as_uri()))
        assert isinstance(caught.value, IndexAccessError)
        assert not isinstance(caught.value, OSError)

    def test_https_metadata_url_raises_index_error(self, tmp_path: Path) -> None:
        # An absolute-href record admitted by get_files must fetch through the
        # index-error path, not a raw ValueError, when its sidecar is fetched.
        package_dir = tmp_path / "foo"
        package_dir.mkdir()
        (package_dir / "index.html").write_text(
            '<a href="https://files.example.com/foo-1.0-py3-none-any.whl"'
            ' data-core-metadata="true">foo-1.0</a>',
            encoding="utf-8",
        )
        client = LocalIndexClient(tmp_path.as_uri())
        (record,) = run(client.get_files("foo"))
        assert isinstance(record, WheelFile)
        assert record.local_path is None
        metadata_url = record.metadata_url
        assert metadata_url == (
            "https://files.example.com/foo-1.0-py3-none-any.whl.metadata"
        )
        with pytest.raises(NonLocalArtifactError):
            run(client.get_metadata_text("foo", "1.0", metadata_url))

    def test_https_sdist_url_raises_index_error(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "foo"
        package_dir.mkdir()
        (package_dir / "index.html").write_text(
            '<a href="https://files.example.com/foo-1.0.tar.gz">foo-1.0</a>',
            encoding="utf-8",
        )
        client = LocalIndexClient(tmp_path.as_uri())
        (record,) = run(client.get_files("foo"))
        assert isinstance(record, SdistFile)
        assert record.local_path is None
        assert record.url == "https://files.example.com/foo-1.0.tar.gz"
        with pytest.raises(NonLocalArtifactError):
            run(client.get_sdist_files("foo", "1.0", record.url))
        with pytest.raises(NonLocalArtifactError):
            run(client.get_sdist_archive("foo", "1.0", record.url))


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


class TestReadWheelMetadata:
    def test_reads_metadata_from_wheel_zip(self, tmp_path: Path) -> None:
        wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr(
                "foo-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            )
        text = read_wheel_metadata(wheel)
        assert text is not None
        assert text.startswith("Metadata-Version: 2.1")

    def test_returns_none_when_no_metadata_member(self, tmp_path: Path) -> None:
        wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("foo/__init__.py", "")
            zf.writestr("foo-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        assert read_wheel_metadata(wheel) is None

    def test_returns_none_for_non_zip(self, tmp_path: Path) -> None:
        not_a_wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        not_a_wheel.write_bytes(b"not a zip archive")
        assert read_wheel_metadata(not_a_wheel) is None

    def test_returns_none_for_corrupt_zip(self, tmp_path: Path) -> None:
        wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        _write_corrupt_wheel(wheel, "foo", "1.0")
        assert read_wheel_metadata(wheel) is None

    def test_returns_none_for_unsupported_compression(self, tmp_path: Path) -> None:
        wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        _write_unsupported_compression_wheel(wheel, "foo", "1.0")
        assert read_wheel_metadata(wheel) is None

    def test_returns_none_for_encrypted_member(self, tmp_path: Path) -> None:
        wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        _write_encrypted_metadata_wheel(wheel, "foo", "1.0")
        assert read_wheel_metadata(wheel) is None

    def test_returns_none_for_corrupt_lzma(self, tmp_path: Path) -> None:
        wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        _write_corrupt_lzma_wheel(wheel, "foo", "1.0")
        assert read_wheel_metadata(wheel) is None

    def test_returns_none_for_non_wheel_filename(self, tmp_path: Path) -> None:
        assert read_wheel_metadata(tmp_path / "notes.txt") is None

    def test_unreadable_wheel_raises_index_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A wheel the process cannot open must raise, not read back as None.
        wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr(
                "foo-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n",
            )
        _deny_zip_open(monkeypatch, wheel)

        with pytest.raises(UnreadableLocalIndexError) as caught:
            read_wheel_metadata(wheel)
        assert str(wheel) in str(caught.value)
        assert "Permission denied" in str(caught.value)

    def test_rejects_multiple_dist_info_dirs(self, tmp_path: Path) -> None:
        wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr(
                "bar-2.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: bar\nRequires-Dist: wrong-dep\n",
            )
            zf.writestr(
                "foo-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: foo\nRequires-Dist: real-dep\n",
            )
            zf.writestr("foo/__init__.py", b"")
        with pytest.raises(UnsupportedWheelError):
            read_wheel_metadata(wheel)

    def test_rejects_mismatched_dist_info_name(self, tmp_path: Path) -> None:
        wheel = tmp_path / "foo-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr(
                "bar-2.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: bar\nRequires-Dist: wrong-dep\n",
            )
        with pytest.raises(UnsupportedWheelError):
            read_wheel_metadata(wheel)

    def test_reads_matching_dist_info_with_dashed_name(self, tmp_path: Path) -> None:
        wheel = tmp_path / "zope.interface-5.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr(
                "zope_interface-5.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: zope.interface\nVersion: 5.0\n",
            )
        text = read_wheel_metadata(wheel)
        assert text is not None
        assert "Name: zope.interface" in text


class TestContextManager:
    def test_async_with(self, tmp_path: Path) -> None:
        async def go() -> bool:
            async with LocalIndexClient(tmp_path.as_uri()) as client:
                files = await client.get_files("foo")
                return files == []

        assert run(go())
