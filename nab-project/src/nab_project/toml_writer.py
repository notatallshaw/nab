"""A deterministic TOML writer for generated documents, without layout heuristics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
from json import JSONEncoder
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TypeGuard

__all__ = ["dumps"]

_ENCODER = JSONEncoder(ensure_ascii=False)
_BARE_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _string(value: str) -> str:
    # TOML also forbids an unescaped DEL character.
    return _ENCODER.encode(value).replace("\x7f", r"\u007f")


def _key(value: str) -> str:
    if not isinstance(value, str):
        message = "TOML keys must be strings"
        raise TypeError(message)

    return value if value and _BARE_KEY_CHARS.issuperset(value) else _string(value)


def _literal(value: object) -> str:
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return int.__repr__(value)
    if isinstance(value, float):
        return float.__repr__(value)
    if isinstance(value, (date, time)):
        return _temporal(value)
    if isinstance(value, (Mapping, list, tuple)):
        return _collection(value)

    message = f"Unsupported TOML value: {type(value).__name__}"
    raise TypeError(message)


def _temporal(value: date | time) -> str:
    """Render date and time literals, always separating datetimes with T."""
    if isinstance(value, datetime):
        return datetime.isoformat(value, sep="T")
    if isinstance(value, date):
        return date.isoformat(value)
    if value.tzinfo is not None:
        message = "TOML does not support offset times"
        raise ValueError(message)

    return time.isoformat(value)


def _collection(
    value: Mapping[str, object] | list[object] | tuple[object, ...],
) -> str:
    """Render a collection nested inside a literal array or inline table."""
    if isinstance(value, Mapping):
        return (
            "{ "
            + ", ".join(
                _key(key) + " = " + _literal(item) for key, item in value.items()
            )
            + " }"
        )
    return "[" + ", ".join(_literal(item) for item in value) + "]"


def _table_array(value: object) -> TypeGuard[Sequence[Mapping[str, object]]]:
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and all(isinstance(item, Mapping) for item in value)
    )


def _table(
    values: Mapping[str, object], path: tuple[str, ...], output: list[str]
) -> None:
    children: list[
        tuple[str, Mapping[str, object] | Sequence[Mapping[str, object]]]
    ] = []
    # Emit scalar keys before child headers change the current table.
    for key, value in values.items():
        if isinstance(value, Mapping) or _table_array(value):
            children.append((key, value))
        else:
            output.append(_key(key) + " = " + _literal(value) + "\n")

    for key, value in children:
        nested_path = (*path, _key(key))
        name = ".".join(nested_path)
        if isinstance(value, Mapping):
            output.append("\n[" + name + "]\n")
            _table(value, nested_path, output)
        else:
            for row in value:
                output.append("\n[[" + name + "]]\n")
                _table(row, nested_path, output)


def dumps(document: Mapping[str, object]) -> str:
    """Render a generated document with tables and arrays of tables."""
    output: list[str] = []
    _table(document, (), output)
    return "".join(output).lstrip("\n")
