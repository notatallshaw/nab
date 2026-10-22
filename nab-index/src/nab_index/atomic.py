"""Write a file by staging it beside the destination and renaming over it.

Shared by the on-disk cache and the lockfile emitters. A write that fails
partway leaves any existing file untouched.
"""

from __future__ import annotations

__lazy_modules__ = (
    "base64",
    "os",
    "pathlib",
    "shutil",
    "sys",
)

import contextlib
import os
import shutil
import sys
import threading
from base64 import urlsafe_b64encode
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator


__all__ = [
    "atomic_write",
    "atomic_write_text",
]

_umask_lock = threading.Lock()
# Six bytes encode to eight filename characters without padding.
_TEMP_NAME_BYTES = 6


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


def _temporary_file(path: Path) -> tuple[int, Path]:
    """Open a private staging file exclusively beside the destination."""
    directory = path.parent.absolute()
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if sys.platform == "win32":  # pragma: no cover - Windows descriptor mode
        flags |= os.O_BINARY
    while True:
        token = urlsafe_b64encode(os.urandom(_TEMP_NAME_BYTES)).decode("ascii")
        temporary = directory / f"{path.name}.{token}.tmp"
        try:
            fd = os.open(temporary, flags, 0o600)
        except FileExistsError:
            continue
        return fd, temporary


@contextmanager
def _staged(path: Path) -> Generator[int, None, None]:
    """Yield a file descriptor for a temp file beside ``path``.

    On a clean exit the temp file is renamed over ``path``; on any exception it
    is removed and ``path`` is left as it was. Staging in the destination
    directory keeps the rename on one filesystem.
    """
    fd, tmp_path = _temporary_file(path)
    try:
        yield fd

        # The rename carries the staging file's mode across.
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
