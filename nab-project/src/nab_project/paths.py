"""Path resolution, plus presence checks that tell absent from unreadable."""

from __future__ import annotations

import errno
import stat
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "PathState",
    "is_absent_error",
    "is_usable_path_name",
    "path_state",
    "resolve_path",
]

_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR})


def is_absent_error(exc: OSError) -> bool:
    """Whether ``exc`` from a stat or an open means nothing is at the path.

    ``ENOTDIR`` is a non-directory component part-way along the path,
    which leaves nothing at the path just as surely as ``ENOENT`` does.
    """
    return exc.errno in _ABSENT_ERRNOS


class PathState(Enum):
    """What a stat of a path found."""

    ABSENT = "absent"
    FILE = "file"
    DIRECTORY = "directory"
    OTHER = "other"
    UNREADABLE = "unreadable"

    @property
    def should_read(self) -> bool:
        """Whether the caller should go on and open the path.

        True for a regular file, and for one whose stat failed: what an
        unreadable path means is the reader's call.
        """
        return self in (PathState.FILE, PathState.UNREADABLE)


def path_state(path: Path) -> PathState:
    """Classify ``path`` without raising, keeping the stat failures apart.

    ``Path.exists``/``is_file``/``is_dir`` answer with a bool, so an absent
    path and a stat that failed collapse into one answer.  An unsearchable
    parent directory fails the stat with ``EACCES``, which those methods
    re-raise on Python 3.13 and below and report as absent on 3.14.
    :data:`PathState.UNREADABLE` keeps that case separate, so the caller
    can hand the path to the read that names the errno.
    """
    try:
        st = path.stat()
    except ValueError:
        # A name the OS cannot carry (an embedded NUL, an unencodable
        # character) is nothing on disk, which is how pathlib's own
        # predicates report it too.
        return PathState.ABSENT
    except OSError as exc:
        if is_absent_error(exc):
            return PathState.ABSENT
        return PathState.UNREADABLE

    if stat.S_ISREG(st.st_mode):
        return PathState.FILE
    if stat.S_ISDIR(st.st_mode):
        return PathState.DIRECTORY
    return PathState.OTHER


def is_usable_path_name(entry: str) -> bool:
    """Whether the filesystem can carry ``entry`` as a name.

    An embedded NUL has to be tested for rather than caught: building a
    ``Path`` never raises on one, and a non-strict resolve keeps it in
    the path on Windows.
    """
    return "\x00" not in entry


def resolve_path(base: Path, entry: str) -> Path | None:
    """Resolve ``entry`` against ``base``, or ``None`` for an unusable name.

    Unencodable names other than a NUL raise from the resolve itself.
    """
    if not is_usable_path_name(entry):
        return None
    try:
        return (base / entry).resolve()
    except ValueError:
        return None
