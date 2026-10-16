"""Extract explicitly selected source archives without permitting escaping members."""

from __future__ import annotations

import io
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from nab_provider.errors import UnsupportedWheelError

from .client import extract_sdist_archive
from .local_index import wheel_metadata_member

if TYPE_CHECKING:
    import zipfile

__all__ = ["extract_source_archive", "read_wheel_metadata_bytes"]


def read_wheel_metadata_bytes(data: bytes, expected: str) -> str:
    """Read the wheel's own METADATA from the bytes whose hashes were checked."""
    import zipfile  # noqa: PLC0415 - only archive operations need the ZIP reader

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = wheel_metadata_member(archive.namelist(), expected)
            text = archive.read(member).decode("utf-8") if member is not None else None
    except UnsupportedWheelError:
        raise
    except Exception as exc:  # ZIP codec exceptions vary by Python version.
        msg = f"unreadable wheel metadata: {exc}"
        raise UnsupportedWheelError(msg) from exc
    if text is None:
        msg = f"wheel for {expected!r} has no METADATA"
        raise UnsupportedWheelError(msg)
    return text


def _member_path(root: Path, member: zipfile.ZipInfo) -> Path:
    """Validate a ZIP member before it can create files below the extraction root."""
    name = member.orig_filename
    parts = PurePosixPath(name)
    mode = member.external_attr >> 16
    if (
        parts.is_absolute()
        or ".." in parts.parts
        or "\\" in name
        or ":" in name
        or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
    ):
        msg = f"unsupported source archive member {name!r}"
        raise ValueError(msg)
    path = root.joinpath(*parts.parts)
    if not path.resolve().is_relative_to(root):
        msg = f"source archive member {name!r} escapes the extraction directory"
        raise ValueError(msg)
    return path


def extract_source_archive(data: bytes, target_dir: Path) -> Path:
    """Extract a ZIP or tar source archive and return its project root."""
    import zipfile  # noqa: PLC0415 - only archive operations need the ZIP reader

    if not zipfile.is_zipfile(io.BytesIO(data)):
        return extract_sdist_archive(data, target_dir, mode="r:*")

    root = target_dir.resolve()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [
                (entry, _member_path(root, entry)) for entry in archive.infolist()
            ]
            for entry, path in members:
                if entry.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, path.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    mode = (entry.external_attr >> 16) & 0o777
                    path.chmod(mode | 0o600)
    except Exception as exc:  # ZIP codec exceptions vary by Python version.
        msg = f"unreadable source archive: {exc}"
        raise ValueError(msg) from exc

    entries = list(root.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        return entries[0]
    return root
