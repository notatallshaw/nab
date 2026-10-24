"""Lexical containment checks for source-tree subdirectories."""

from __future__ import annotations

__lazy_modules__ = (
    "ntpath",
    "posixpath",
)

import ntpath
import posixpath

__all__ = ["subdirectory_escapes"]


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
