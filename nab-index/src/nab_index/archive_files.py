"""Read selected declaration files from source archives without extracting a tree."""

from __future__ import annotations

import io
import stat
import tarfile
import zipfile
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["read_archive_files", "source_relative_path"]


def source_relative_path(value: str) -> str:
    """Normalize a relative path without parent traversal or drive syntax."""
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        msg = f"unsafe source path {value!r}"
        raise ValueError(msg)
    return str(path)


def _members(names: Sequence[str], directories: Sequence[str]) -> dict[str, str]:
    """Use the same enclosing source directory for every requested file."""
    normalized: dict[str, str] = {}
    for name in names:
        path = source_relative_path(name)
        if path == ".":
            msg = "archive member has no file name"
            raise ValueError(msg)
        if path in normalized:
            msg = f"duplicate archive path {path!r}"
            raise ValueError(msg)
        normalized[path] = name
    roots = {PurePosixPath(name).parts[0] for name in normalized}
    for name in directories:
        directory = source_relative_path(name)
        if directory != ".":
            roots.add(PurePosixPath(directory).parts[0])
    wrapped = len(roots) == 1 and all("/" in name for name in normalized)
    return {
        name.split("/", 1)[1] if wrapped else name: original
        for name, original in normalized.items()
    }


def read_archive_files(data: bytes, paths: Sequence[str]) -> dict[str, str]:
    """Read UTF-8 files, refusing links, duplicate names and unsafe archive paths.

    A single enclosing directory becomes the source root.
    """
    requested = [source_relative_path(path) for path in paths]
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            files = [entry for entry in entries if not entry.is_dir()]
            directories = [entry.filename for entry in entries if entry.is_dir()]
            members = _members([entry.filename for entry in files], directories)
            result = {}
            for path in requested:
                zip_entry = archive.getinfo(members[path])
                # Unix link metadata in ZIP files must not become declaration text.
                if stat.S_ISLNK(zip_entry.external_attr >> 16):
                    msg = f"source file {path!r} is a symbolic link"
                    raise ValueError(msg)
                result[path] = archive.read(zip_entry).decode("utf-8")
            return result
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        tar_entries = archive.getmembers()
        tar_files = [entry for entry in tar_entries if not entry.isdir()]
        directories = [entry.name for entry in tar_entries if entry.isdir()]
        members = _members([entry.name for entry in tar_files], directories)
        result = {}
        for path in requested:
            entry = archive.getmember(members[path])
            if not entry.isfile():
                msg = f"source file {path!r} is not a regular file"
                raise ValueError(msg)
            stream = archive.extractfile(entry)
            assert stream is not None  # The selected member is a regular file.
            with stream:
                result[path] = stream.read().decode("utf-8")
        return result
