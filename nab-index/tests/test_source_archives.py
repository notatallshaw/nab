"""Direct source archives retain files without allowing escaping members."""

from __future__ import annotations

import io
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from nab_index.source_archives import extract_source_archive, read_wheel_metadata_bytes
from nab_provider.errors import UnsupportedWheelError


def zipped(entries: dict[str, str | bytes | zipfile.ZipInfo]) -> bytes:
    """Return a ZIP with file contents or explicit member attributes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in entries.items():
            if isinstance(value, zipfile.ZipInfo):
                archive.writestr(value, "contents")
            else:
                entry = zipfile.ZipInfo(name)
                entry.filename = name  # Keep original separators on Windows.
                archive.writestr(entry, value)
    return buffer.getvalue()


@pytest.mark.parametrize("prefix", ["", "package/"])
def test_zip_project_root_and_executable_files(tmp_path: Path, prefix: str) -> None:
    script = zipfile.ZipInfo(prefix + "build.sh")
    script.external_attr = (stat.S_IFREG | 0o755) << 16
    entries: dict[str, str | bytes | zipfile.ZipInfo] = {
        prefix + "pyproject.toml": "[project]\n",
        "script": script,
    }
    if prefix:
        entries[prefix] = ""
    data = zipped(entries)
    root = extract_source_archive(data, tmp_path)
    assert root == tmp_path / prefix
    assert (root / "pyproject.toml").read_text() == "[project]\n"
    assert (root / "build.sh").read_text() == "contents"
    if os.name != "nt":
        assert (root / "build.sh").stat().st_mode & stat.S_IXUSR


def test_zip_with_multiple_root_directories(tmp_path: Path) -> None:
    data = zipped({"one/file": "one", "two/file": "two"})
    assert extract_source_archive(data, tmp_path) == tmp_path
    assert (tmp_path / "one/file").read_text() == "one"


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/absolute", "a\\b"])
def test_zip_refuses_escaping_names_before_writing(tmp_path: Path, name: str) -> None:
    data = zipped({"first": "untouched", name: "refused"})
    with pytest.raises(ValueError, match="unsupported source archive member"):
        extract_source_archive(data, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_zip_validates_names_before_platform_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay the ZIP reader's Windows separator normalization on every platform."""
    data = zipped({"first": "untouched", "a\\b": "refused"})
    infolist = zipfile.ZipFile.infolist

    def normalized(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        entries = infolist(archive)
        for entry in entries:
            entry.filename = entry.filename.replace("\\", "/")
        return entries

    monkeypatch.setattr(zipfile.ZipFile, "infolist", normalized)
    with pytest.raises(ValueError, match="unsupported source archive member"):
        extract_source_archive(data, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR])
def test_zip_refuses_non_regular_members(tmp_path: Path, kind: int) -> None:
    member = zipfile.ZipInfo("special")
    member.external_attr = (kind | 0o777) << 16
    with pytest.raises(ValueError, match="unsupported source archive member"):
        extract_source_archive(zipped({"special": member}), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_zip_refuses_existing_escaping_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem-resolved member must remain below the extraction root."""
    resolve = Path.resolve

    def escaped(path: Path, *args: object, **kwargs: object) -> Path:
        if path.name == "escaped":
            return tmp_path.parent / "outside"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escaped)
    with pytest.raises(ValueError, match="escapes the extraction directory"):
        extract_source_archive(zipped({"escaped": "refused"}), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_zip_write_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("read-only destination")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ValueError, match="read-only destination"):
        extract_source_archive(zipped({"file": "contents"}), tmp_path)


def test_tar_source_archive_supports_other_compression(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as archive:
        member = tarfile.TarInfo("project/pyproject.toml")
        member.size = len(b"[project]\n")
        archive.addfile(member, io.BytesIO(b"[project]\n"))
    if not hasattr(tarfile, "data_filter"):
        with pytest.raises(ValueError, match="tar data filter"):
            extract_source_archive(buffer.getvalue(), tmp_path)
    else:
        root = extract_source_archive(buffer.getvalue(), tmp_path)
        assert root == tmp_path / "project"
        assert (root / "pyproject.toml").read_text() == "[project]\n"


def test_wheel_metadata_reader_uses_its_own_distribution() -> None:
    data = zipped({"pkg-1.dist-info/METADATA": "Name: pkg\nVersion: 1\n"})
    assert read_wheel_metadata_bytes(data, "pkg") == "Name: pkg\nVersion: 1\n"
    with pytest.raises(UnsupportedWheelError, match="different distribution"):
        read_wheel_metadata_bytes(data, "other")


@pytest.mark.parametrize(
    "data",
    [b"not a zip", zipped({"pkg-1.dist-info/METADATA": b"\xff"})],
)
def test_unreadable_wheel_metadata_is_reported(data: bytes) -> None:
    with pytest.raises(UnsupportedWheelError, match="unreadable wheel metadata"):
        read_wheel_metadata_bytes(data, "pkg")


def test_wheel_without_metadata_is_reported() -> None:
    with pytest.raises(UnsupportedWheelError, match="has no METADATA"):
        read_wheel_metadata_bytes(zipped({"module.py": ""}), "pkg")


def test_corrupt_deflate_is_reported_by_both_readers(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    name = "pkg-1.dist-info/METADATA"
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, "Name: pkg\nVersion: 1\n" * 10)
        offset = archive.getinfo(name).header_offset + 30 + len(name)
    data = bytearray(buffer.getvalue())
    data[offset] = 0x06  # Invalid DEFLATE block type.

    with pytest.raises(UnsupportedWheelError, match="unreadable wheel metadata"):
        read_wheel_metadata_bytes(bytes(data), "pkg")
    with pytest.raises(ValueError, match="unreadable source archive"):
        extract_source_archive(bytes(data), tmp_path)
