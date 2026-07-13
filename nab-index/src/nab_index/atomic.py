"""Write a file by staging it beside the destination and renaming over it.

Shared by the on-disk cache and the lockfile emitters. A write that fails
partway leaves any existing file untouched.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


__all__ = [
    "atomic_write",
    "atomic_write_text",
]

_umask_lock = threading.Lock()


def _default_mode() -> int:
    """Return the mode ``open`` would have given a new file.

    The umask can only be read by setting it, so set a restrictive one and
    put it straight back, under a lock because the cache writes from the
    fetch threads.
    """
    with _umask_lock:
        umask = os.umask(0o077)
        os.umask(umask)
    return 0o666 & ~umask


@contextmanager
def _staged(path: Path) -> Iterator[int]:
    """Yield a file descriptor for a temp file beside ``path``.

    On a clean exit the temp file is renamed over ``path``; on any exception it
    is removed and ``path`` is left as it was. Staging in the destination
    directory keeps the rename on one filesystem.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        yield fd

        # The rename carries the temp file's own mode across, and mkstemp
        # creates at 0600.
        if path.exists():
            shutil.copymode(path, tmp_path)
        else:
            tmp_path.chmod(_default_mode())

        # Path.replace would route around os.replace on Python 3.10
        # (pathlib captures it at import time), defeating monkeypatches.
        os.replace(tmp_path, path)  # noqa: PTH105
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data``."""
    with _staged(path) as fd, os.fdopen(fd, "wb") as f:
        f.write(data)

        # close() only reaches the page cache, where a full disk can still
        # surface at writeback, after the rename has replaced the file.
        f.flush()
        os.fsync(f.fileno())


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, encoded as UTF-8."""
    with _staged(path) as fd, os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)

        f.flush()
        os.fsync(f.fileno())
