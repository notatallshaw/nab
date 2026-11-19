"""Read TOML, reporting a document that will not parse as ``TOMLDecodeError``.

tomli raises something else for two failures: a decimal integer with more digits
than :func:`sys.get_int_max_str_digits` allows raises :class:`ValueError` out of
:func:`int`, and inline arrays or tables nested too deeply raise
:class:`RecursionError`.  Neither says where in the document it happened, so the
substituted error points at the start.
"""

from __future__ import annotations

__lazy_modules__ = ("tomli",)

from collections.abc import Mapping
from math import isnan
from typing import TYPE_CHECKING, Any

import tomli

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO, TypeGuard

__all__ = ["is_table", "load", "load_path", "loads", "values_match"]


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


def is_table(value: object) -> TypeGuard[dict[str, object]]:
    """Return whether a value is a table with string keys."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def values_match(
    expected: object, actual: object, *, allow_extra_keys: bool = False
) -> bool:
    """Compare TOML values, optionally matching only the specified table keys."""
    if isinstance(actual, dict) and isinstance(expected, Mapping):
        if not allow_extra_keys and len(expected) != len(actual):
            return False
        return all(
            key in actual
            and values_match(value, actual[key], allow_extra_keys=allow_extra_keys)
            for key, value in expected.items()
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(expected) == len(actual) and all(
            values_match(left, right, allow_extra_keys=allow_extra_keys)
            for left, right in zip(expected, actual, strict=True)
        )

    # A NaN read twice compares unequal in Python. Conversely, True, 1 and
    # 1.0 compare equal even though they are different TOML types.
    if isinstance(expected, float) and isnan(expected):
        return isinstance(actual, float) and isnan(actual)
    return type(expected) is type(actual) and expected == actual
