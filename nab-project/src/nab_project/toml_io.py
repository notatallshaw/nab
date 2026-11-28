"""Read TOML, reporting a document that will not parse as ``TOMLDecodeError``.

tomli raises something else for two failures: a decimal integer with more digits
than :func:`sys.get_int_max_str_digits` allows raises :class:`ValueError` out of
:func:`int`, and inline arrays or tables nested too deeply raise
:class:`RecursionError`.  Neither says where in the document it happened, so the
substituted error points at the start.
"""

from __future__ import annotations

__lazy_modules__ = ("copy", "tomli")

import copy
from collections.abc import Mapping
from math import isnan
from typing import TYPE_CHECKING, Any

import tomli

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import BinaryIO, TypeGuard

__all__ = [
    "ReadCache",
    "copy_table",
    "is_table",
    "load",
    "load_path",
    "loads",
    "loads_if_valid",
    "tool_nab_section",
    "values_match",
]


def loads(text: str) -> dict[str, Any]:
    """Parse ``text`` as a TOML document."""
    try:
        return tomli.loads(text)
    except tomli.TOMLDecodeError:
        # A decode error is already a ValueError, so it passes through first.
        raise
    except (RecursionError, ValueError) as exc:
        raise tomli.TOMLDecodeError(str(exc), text, 0) from exc


def loads_if_valid(text: str) -> dict[str, object] | None:
    """Read optional TOML metadata, returning None when it cannot be parsed."""
    try:
        return loads(text)
    except tomli.TOMLDecodeError:
        return None


def load(f: BinaryIO) -> dict[str, Any]:
    """Parse the TOML document in the binary file ``f``.

    Reading and decoding here rather than calling :func:`tomli.load` routes the
    parse through :func:`loads`.  Bytes that are not UTF-8 still raise
    :class:`UnicodeDecodeError`.
    """
    return loads(f.read().decode())


class ReadCache:
    """Share the last parsed document between read-only consumers in one operation."""

    __slots__ = ("_last",)

    def __init__(self) -> None:
        """Start without a document."""
        self._last: tuple[bytes, dict[str, Any]] | None = None

    def parse(self, raw: bytes) -> dict[str, Any]:
        """Reuse the table only if the bytes still match."""
        if self._last is not None and raw == self._last[0]:
            return self._last[1]

        document = loads(raw.decode())
        self._last = (raw, document)
        return document


def load_path(path: Path, *, cache: ReadCache | None = None) -> dict[str, Any]:
    """Parse the TOML document in the file at ``path``.

    Failures reach the caller unwrapped: :class:`OSError` for an unreadable
    file, :class:`UnicodeDecodeError` for bytes that are not UTF-8, and
    :class:`~tomli.TOMLDecodeError` for a document that will not parse.
    """
    with path.open("rb") as f:
        if cache is not None:
            return cache.parse(f.read())

        return load(f)


def tool_nab_section(document: Mapping[str, object]) -> object:
    """Read [tool.nab], returning an empty table when [tool] has no Nab entry.

    A malformed Nab entry is returned as written so the caller can report it.
    """
    missing: dict[str, object] = {}
    tool = document.get("tool")
    return tool.get("nab", missing) if isinstance(tool, dict) else missing


def is_table(value: object) -> TypeGuard[dict[str, object]]:
    """Return whether a value is a table with string keys."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def copy_table(document: Mapping[str, object]) -> dict[str, object]:
    """Copy a parsed TOML table without using one Python frame per nested value."""
    result = dict(document)
    pending: list[dict[str, object] | list[object]] = [result]
    memo: dict[int, object] = {id(document): result}

    def copy_value(value: object) -> object:
        if id(value) in memo:
            return memo[id(value)]

        # Copy the container now and fill its nested values on the work list.
        child: dict[str, object] | list[object]
        if type(value) is dict:
            child = dict(value)
        elif type(value) is list:
            child = list(value)
        else:
            return copy.deepcopy(value, memo)
        memo[id(value)] = child
        pending.append(child)
        return child

    while pending:
        container = pending.pop()
        if isinstance(container, dict):
            for key, value in container.items():
                container[key] = copy_value(value)
        else:
            for index, value in enumerate(container):
                container[index] = copy_value(value)
    return result


def values_match(
    expected: object, actual: object, *, allow_extra_keys: bool = False
) -> bool:
    """Compare TOML values, optionally matching only the specified table keys."""
    # Keep one iterator per open table or array, including for wide documents.
    pending: list[Iterator[tuple[object, object]]] = [iter(((expected, actual),))]
    seen: dict[tuple[int, int], tuple[object, object]] = {}
    while pending:
        pair = next(pending[-1], None)
        if pair is None:
            pending.pop()
            continue

        left, right = pair
        if isinstance(left, Mapping) and isinstance(right, dict):
            if not allow_extra_keys and len(left) != len(right):
                return False
            children = _table_pairs(left, right)
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return False
            children = zip(left, right, strict=True)
        elif _same_scalar(left, right):
            continue
        else:
            return False

        # A mapping may create a fresh value on lookup. Keep compared pairs
        # alive while using their identities to skip shared containers.
        key = (id(left), id(right))
        if key not in seen:
            seen[key] = pair
            pending.append(children)
    return True


def _table_pairs(
    expected: Mapping[str, object], actual: dict[str, object]
) -> Iterator[tuple[object, object]]:
    missing = object()
    for key, value in expected.items():
        yield value, actual.get(key, missing)


def _same_scalar(left: object, right: object) -> bool:
    # A NaN read twice compares unequal; Python equates true, 1 and 1.0.
    if isinstance(left, float) and isnan(left):
        return isinstance(right, float) and isnan(right)
    return type(left) is type(right) and left == right
