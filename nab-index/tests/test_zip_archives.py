"""ZIP sdists use the same metadata-root and extraction contracts as tarballs."""

from __future__ import annotations

import io
import os
import stat
import warnings
import zipfile
import zlib
from typing import TYPE_CHECKING

import pytest

from nab_index.client import _extract_sdist_files, extract_sdist_archive

if TYPE_CHECKING:
    from pathlib import Path


def _zip(members: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    """Build ZIP bytes preserving duplicate names and input order."""
    output = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(output, "w") as archive:
        warnings.simplefilter("ignore", UserWarning)
        for name, content in members:
            entry = name
            if isinstance(name, str) and "\\" in name:
                # ZipInfo normally rewrites Windows separators before writing.
                entry = zipfile.ZipInfo()
                entry.filename = entry.orig_filename = name
            archive.writestr(entry, content)
    return output.getvalue()


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        ([("pkg-1/PKG-INFO", b"metadata")], ("metadata", None)),
        (
            [("pkg-1/PKG-INFO", b"metadata"), ("pkg-1/pyproject.toml", b"project")],
            ("metadata", "project"),
        ),
        (
            [("pkg-1/PKG-INFO", b"first"), ("pkg-1/PKG-INFO", b"second")],
            ("first", None),
        ),
        (
            [("pkg-1/PKG-INFO", b"one"), ("other-1/PKG-INFO", b"two")],
            (None, None),
        ),
        (
            [("pkg-1/PKG-INFO", b"metadata"), ("other-1/pyproject.toml", b"other")],
            ("metadata", None),
        ),
        (
            [
                ("PKG-INFO", b"root"),
                ("pkg-1/deep/PKG-INFO", b"deep"),
                ("pkg-1/PKG-INFO/", b""),
                ("pkg-1/setup.py", b"setup"),
            ],
            (None, None),
        ),
        ([("pkg-1/PKG-INFO", b"\xff")], (None, None)),
    ],
)
def test_zip_metadata_root(
    members: list[tuple[str, bytes]], expected: tuple[str | None, str | None]
) -> None:
    assert _extract_sdist_files(_zip(list(members))) == expected


def test_zip_metadata_does_not_read_a_symlink_as_text() -> None:
    member = zipfile.ZipInfo("pkg-1/PKG-INFO")
    member.external_attr = (stat.S_IFLNK | 0o777) << 16

    assert _extract_sdist_files(_zip([(member, b"elsewhere")])) == (None, None)


def test_zip_metadata_bad_crc_is_unreadable() -> None:
    data = _zip([("pkg-1/PKG-INFO", b"metadata")]).replace(b"metadata", b"corrupt!")

    assert _extract_sdist_files(data) == (None, None)


@pytest.mark.parametrize("rooted", [False, True])
def test_zip_extraction_returns_the_source_root(
    tmp_path: Path, *, rooted: bool
) -> None:
    prefix = "pkg-1/" if rooted else ""
    data = _zip([(f"{prefix}setup.py", b"setup"), (f"{prefix}PKG-INFO", b"metadata")])
    root = extract_sdist_archive(data, tmp_path)

    assert root == (tmp_path / "pkg-1" if rooted else tmp_path)
    assert (root / "setup.py").read_bytes() == b"setup"
    assert (root / "PKG-INFO").read_bytes() == b"metadata"


def test_tar_only_extraction_refuses_zip_bytes(tmp_path: Path) -> None:
    data = _zip([("pkg-1/setup.py", b"setup")])
    with pytest.raises(ValueError, match="unreadable sdist archive|tar data filter"):
        extract_sdist_archive(data, tmp_path, allow_zip=False)

    assert list(tmp_path.iterdir()) == []


def test_zip_extraction_preserves_execute_bits_without_special_modes(
    tmp_path: Path,
) -> None:
    script = zipfile.ZipInfo("pkg-1/configure")
    script.external_attr = (stat.S_IFREG | 0o4755) << 16
    data = _zip([("pkg-1/", b""), (script, b"#!/bin/sh\nexit 0\n")])
    root = extract_sdist_archive(data, tmp_path)

    assert (root / "configure").read_bytes() == b"#!/bin/sh\nexit 0\n"

    mode = (root / "configure").stat().st_mode

    assert mode & stat.S_ISUID == 0

    if os.name != "nt":
        assert mode & 0o111 == 0o111


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/outside",
        "pkg/../../outside",
        "C:/outside",
        "pkg/file:stream",
        "pkg\\outside",
        "pkg?/outside",
        "pkg</outside",
        "pkg>/outside",
        "pkg|/outside",
        "pkg*/outside",
        'pkg"/outside',
        "pkg./outside",
        "pkg /outside",
    ],
)
def test_zip_extraction_refuses_unsafe_names_before_writing(
    tmp_path: Path, name: str
) -> None:
    data = _zip([("pkg-1/setup.py", b"setup"), (name, b"outside")])
    target = tmp_path / "unpacked"
    with pytest.raises(ValueError, match="unsafe sdist member"):
        extract_sdist_archive(data, target)

    assert not target.exists()


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR])
def test_zip_extraction_refuses_links_and_special_files(
    tmp_path: Path, mode: int
) -> None:
    member = zipfile.ZipInfo("pkg-1/link")
    member.external_attr = (mode | 0o777) << 16
    with pytest.raises(ValueError, match="unsafe sdist member"):
        extract_sdist_archive(_zip([(member, b"../../outside")]), tmp_path)


def test_zip_extraction_refuses_existing_escaping_symlink(tmp_path: Path) -> None:
    target = tmp_path / "unpacked"
    target.mkdir()
    (target / "pkg-1").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe sdist member"):
        extract_sdist_archive(_zip([("pkg-1/outside", b"outside")]), target)

    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize(
    "error",
    [
        zipfile.BadZipFile("bad"),
        OSError("unreadable"),
        EOFError(),
        zlib.error("bad"),
        RuntimeError("encrypted"),
        NotImplementedError("compression"),
    ],
)
def test_zip_extraction_normalizes_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    data = _zip([("pkg-1/setup.py", b"setup")])

    def fail_read(_self: zipfile.ZipFile, _member: zipfile.ZipInfo) -> None:
        raise error

    monkeypatch.setattr(zipfile.ZipFile, "open", fail_read)
    with pytest.raises(ValueError, match="unreadable sdist archive"):
        extract_sdist_archive(data, tmp_path)
