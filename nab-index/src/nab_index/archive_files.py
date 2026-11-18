"""Read selected declaration files from source archives without extracting a tree."""

from __future__ import annotations

import io
import stat
import tarfile
import zipfile
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = ["read_archive_files", "source_relative_path"]

_ArchiveMember = TypeVar("_ArchiveMember", tarfile.TarInfo, zipfile.ZipInfo)


def source_relative_path(value: str) -> str:
    """Normalize a relative path without parent traversal or drive syntax."""
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        msg = f"unsafe source path {value!r}"
        raise ValueError(msg)
    return str(path)


def _members(
    entries: Iterable[tuple[str, _ArchiveMember | None]],
) -> dict[str, _ArchiveMember]:
    """Index files by source-relative path, accounting for empty directories."""
    members: dict[str, _ArchiveMember | None] = {}
    roots: set[str] = set()
    for name, entry in entries:
        path = source_relative_path(name)
        if path == ".":
            if entry is None:
                continue
            msg = "archive member has no file name"
            raise ValueError(msg)

        # Repeated directory entries describe the same directory.
        if path in members and (entry is not None or members[path] is not None):
            msg = f"duplicate archive path {path!r}"
            raise ValueError(msg)
        members[path] = entry
        roots.add(path.partition("/")[0])

    # Strip the sole root only when it represents a directory.
    wrapped = len(roots) == 1 and members.get(next(iter(roots))) is None
    return {
        path.partition("/")[2] if wrapped else path: entry
        for path, entry in members.items()
        if entry is not None
    }


def read_archive_files(data: bytes, paths: Sequence[str]) -> dict[str, str]:
    """Read UTF-8 files, refusing links, duplicate names and unsafe archive paths.

    A single enclosing directory becomes the source root.
    """
    requested = [source_relative_path(path) for path in paths]
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            zip_members = _members(
                (entry.filename, None if entry.is_dir() else entry)
                for entry in archive.infolist()
            )
            result = {}
            for path in requested:
                zip_entry = zip_members[path]
                # Unix link metadata in ZIP files must not become declaration text.
                if stat.S_ISLNK(zip_entry.external_attr >> 16):
                    msg = f"source file {path!r} is a symbolic link"
                    raise ValueError(msg)
                result[path] = archive.read(zip_entry).decode("utf-8")
            return result

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        tar_members = _members(
            (entry.name, None if entry.isdir() else entry)
            for entry in archive.getmembers()
        )
        result = {}
        for path in requested:
            entry = tar_members[path]
            stream = archive.extractfile(entry) if entry.isfile() else None
            if stream is None:
                msg = f"source file {path!r} is not a regular file"
                raise ValueError(msg)
            with stream:
                result[path] = stream.read().decode("utf-8")
        return result
