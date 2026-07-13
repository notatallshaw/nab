"""Write a file by staging it beside the destination and renaming over it.

Shared by the on-disk cache and the lockfile emitters. A plain
``write_text`` truncates the destination before the first byte of the new
content lands, so a write that runs out of space, hits an I/O error, or is
interrupted destroys the old file and leaves a prefix of the new one in its
place. Staging the content in a temp file and renaming means the
destination only ever holds a complete version.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


__all__ = [
    "atomic_write",
    "atomic_write_text",
]


@contextmanager
def _staged(path: Path) -> Iterator[int]:
    """Yield a file descriptor for a temp file beside ``path``.

    On a clean exit the temp file is renamed over ``path``; on any
    exception it is removed and ``path`` is left as it was. The temp file
    sits in the destination directory so the rename is a same-filesystem
    operation.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        yield fd
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


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, encoded as UTF-8."""
    with _staged(path) as fd, os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
