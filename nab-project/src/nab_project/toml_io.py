"""Read TOML, reporting a document that will not parse as ``TOMLDecodeError``.

tomli raises something else for two failures: a decimal integer with more digits
than :func:`sys.get_int_max_str_digits` allows raises :class:`ValueError` out of
:func:`int`, and inline arrays or tables nested too deeply raise
:class:`RecursionError`.  Neither says where in the document it happened, so the
substituted error points at the start.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import tomli

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO

__all__ = ["load", "load_path", "loads"]


def loads(text: str) -> dict[str, Any]:
    """Parse ``text`` as a TOML document."""
    try:
        return tomli.loads(text)
    except tomli.TOMLDecodeError:
        # A decode error is already a ValueError, so it passes through first.
        raise
    except (RecursionError, ValueError) as exc:
        raise tomli.TOMLDecodeError(str(exc), text, 0) from exc


def load(f: BinaryIO) -> dict[str, Any]:
    """Parse the TOML document in the binary file ``f``.

    Reading and decoding here rather than calling :func:`tomli.load` routes the
    parse through :func:`loads`.  Bytes that are not UTF-8 still raise
    :class:`UnicodeDecodeError`.
    """
    return loads(f.read().decode())


def load_path(path: Path) -> dict[str, Any]:
    """Parse the TOML document in the file at ``path``.

    Failures reach the caller unwrapped: :class:`OSError` for an unreadable
    file, :class:`UnicodeDecodeError` for bytes that are not UTF-8, and
    :class:`~tomli.TOMLDecodeError` for a document that will not parse.
    """
    with path.open("rb") as f:
        return load(f)
