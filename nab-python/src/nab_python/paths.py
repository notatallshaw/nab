"""Presence checks that keep an absent path apart from an unreadable one."""

from __future__ import annotations

import errno
import stat
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "PathState",
    "path_state",
]

# ENOTDIR is a non-directory component part-way along the path, which leaves
# nothing at the path just as surely as ENOENT does.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR})


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

        True for a regular file, and for one whose stat failed: opening
        it is what turns that failure into a message naming the errno.
        """
        return self in (PathState.FILE, PathState.UNREADABLE)


def path_state(path: Path) -> PathState:
    """Classify ``path`` without raising, keeping the stat failures apart.

    ``Path.exists``/``is_file``/``is_dir`` answer with a bool, so the two
    ways a stat can fail collapse into one.  When the parent directory is
    not searchable the stat fails with ``EACCES``, which those methods
    re-raise on Python 3.13 and below and report as absent on 3.14.
    :data:`PathState.UNREADABLE` keeps that case its own, so a caller can
    hand the path to the read that names the real errno instead of
    calling a file that is there missing.
    """
    try:
        st = path.stat()
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return PathState.ABSENT
        return PathState.UNREADABLE
    if stat.S_ISREG(st.st_mode):
        return PathState.FILE
    if stat.S_ISDIR(st.st_mode):
        return PathState.DIRECTORY
    return PathState.OTHER
