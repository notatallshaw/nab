"""Lexical containment checks for source-tree subdirectories."""

from __future__ import annotations

__lazy_modules__ = (
    "ntpath",
    "posixpath",
    "urllib.parse",
)

import ntpath
import posixpath
from urllib.parse import unquote

__all__ = ["decode_subdirectory", "subdirectory_escapes"]


def subdirectory_escapes(subdirectory: str) -> bool:
    r"""Return True if a subdirectory escapes under either path convention.

    Materialisation uses native paths, so both conventions must be checked.
    A literal backslash in ``c\d/../..`` hides a POSIX escape from ntpath.
    """
    if not subdirectory:
        return False

    for path in (ntpath, posixpath):
        normalized = path.normpath(subdirectory)
        if path.splitdrive(subdirectory)[0] or normalized.startswith(path.sep):
            return True
        if normalized == ".." or normalized.startswith(f"..{path.sep}"):
            return True
    return False


def decode_subdirectory(value: str) -> str:
    """Read a UTF-8 source URL fragment as a relative filesystem path."""
    path = unquote(value, errors="strict")
    if "\0" in path or subdirectory_escapes(path):
        msg = f"invalid source subdirectory {path!r}"
        raise ValueError(msg)
    return path
