"""Build option records from typed row declarations.

Checks that need both the row declaration and inferred value shape live here.
"""

from __future__ import annotations

__lazy_modules__ = (
    "enum",
    "nab._cli.definition.tables",
    "pathlib",
)

import enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .model import UNSET, Kind, Opt, Tokens, VType

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet
from .rows import (
    OMITTED,
    Item,
    Items,
    Many,
    Pairs,
    Row,
    Star,
    Tri,
    Verb,
)
from .tables import Table, rows
from .types import Shape, shape, type_argument

# What a row with no value at all reads as: a flag, a counter or a key.
_NO_VALUE = Shape("", (), "", nullable=False)

# The token grammar each row class reads, on a row under a table key.  A
# class listed nowhere here reads its tokens as one value.
_TOKENS: dict[type[Row], Tokens] = {
    Item: Tokens.ITEM,
    Items: Tokens.ITEMS,
    Pairs: Tokens.PAIRS,
    Star: Tokens.LIST,
}


def build_option(row: Row) -> Opt:
    """Build the ``Opt`` for one declared row."""
    read = _read(row)
    _check_mirror(row, read)
    _check_rung_zero(row, read)

    fields: dict[str, Any] = {
        "scope": row.scope,
        "kind": Kind(row.kind) if row.kind else None,
        "vtype": VType(read.vtype) if read.vtype else None,
        "choices": read.choices,
        "nullable": read.nullable,
        "negatable": row.negatable,
        "env": row.env,
        "commands": row.on,
        "default": _default(row, read),
        "required": row.required,
        "deprecated": row.deprecated,
        "short": row.short,
        "help": row.help,
        "docs": row.docs,
        "under": row.under,
        "needed": row.needed,
        "tokens": _tokens(row),
        "opened_by": row.opened_by,
    }

    layer = row.key
    if layer is not None:
        fields.update(
            rdefault=layer.rdefault,
            parse=layer.parse,
            render=layer.render,
            sample=layer.sample,
            type_label=layer.label or _label(row, read),
        )

    return Opt(row.name, **fields)


def _tokens(row: Row) -> Tokens | None:
    """How a row's tokens read, and ``None`` on a row under no table key."""
    if not row.under:
        return None
    return _TOKENS.get(type(row), Tokens.SCALAR)


def build_options(
    *tables: type[Table], environment_names: AbstractSet[str] = frozenset()
) -> tuple[Opt, ...]:
    """Build options only when each setting has a second configuration form."""
    declared = tuple(row for table in tables for row in rows(table))
    built = tuple(build_option(row) for row in declared)
    for row in declared:
        _check_surface(row, declared, environment_names)
    return built


def _check_surface(
    row: Row, declared: tuple[Row, ...], environment_names: AbstractSet[str]
) -> None:
    """Keep ordinary settings layered and require explicit actions or aliases."""
    alternatives = bool(row.alias) + bool(row.environ) + row.action
    if alternatives > 1 or (row.key is not None and alternatives):
        msg = f"{row.name} declares competing setting/action forms"
        raise ValueError(msg)
    if row.key is not None:
        if not row.kind and not any(other.alias == "*" for other in declared):
            msg = (
                f"{row.name} has no dedicated CLI flag "
                "or configuration assignment option"
            )
            raise ValueError(msg)
        return
    if row.under or row.kind in {"positional", "verb", "eager"}:
        return
    if row.action:
        if row.kind != "flag" and _read(row).vtype != "path":
            msg = (
                f"{row.name} is a configurable value, "
                "not an input/output path or action flag"
            )
            raise ValueError(msg)
        return
    if row.environ:
        missing = set(row.environ) - environment_names
        if missing:
            msg = f"{row.name} names unregistered environment forms: {sorted(missing)}"
            raise ValueError(msg)
        return
    if row.alias:
        _check_alias(row, declared)
        return
    msg = f"{row.name} is CLI-only; declare a layered setting, alias or explicit action"
    raise ValueError(msg)


def _check_alias(row: Row, declared: tuple[Row, ...]) -> None:
    """Check that a shortcut reaches a declared setting or the assignment parser."""
    if row.alias == "*":
        if row.name != "settings" or row.kind != "append":
            msg = (
                "only the settings assignment option may target every configuration key"
            )
            raise ValueError(msg)
        return
    key, dot, child = row.alias.partition(".")
    if not any(other.name == key and other.key is not None for other in declared):
        msg = f"{row.name} aliases an undeclared setting {key!r}"
        raise ValueError(msg)
    if dot and not any(
        other.name == child and other.under == key for other in declared
    ):
        msg = f"{row.name} aliases an undeclared table key {row.alias!r}"
        raise ValueError(msg)


def _read(row: Row) -> Shape:
    """Read the row's type parameter, with the two kinds that override it.

    A negatable flag reads the words ``true`` and ``false`` rather than a
    declared type, and a verb reads a plain token out of its choice set
    rather than the ``Literal`` that names them.
    """
    if isinstance(row, Tri):
        return Shape("bool", (), "bool", nullable=True)

    argument = type_argument(row)
    read = _NO_VALUE if argument is None else shape(argument, row.name)
    if isinstance(row, Verb):
        return Shape("str", read.choices, read.label, nullable=False)
    return read


def _check_mirror(row: Row, read: Shape) -> None:
    """Check a choice row's tokens against the enum it says it mirrors."""
    if row.mirrors is None:
        return

    members = tuple(str(member.value) for member in row.mirrors)
    if members != read.choices:
        msg = (
            f"{row.name} lists {read.choices} and "
            f"{row.mirrors.__name__} holds {members}"
        )
        raise ValueError(msg)


def _check_rung_zero(row: Row, read: Shape) -> None:
    """Check that rung 0 is a token the row's own choice set offers."""
    layer = row.key
    if layer is None or not read.choices:
        return

    # A rung 0 that is no token at all: dist-policy holds a policy and
    # whether the flag was written.  tests/test_cli_table.py pins it as the
    # only row in the table taking this path.
    rung = layer.rdefault
    if not isinstance(rung, (str, enum.Enum)):
        return

    token = rung.value if isinstance(rung, enum.Enum) else rung
    if token not in read.choices:
        msg = f"{row.name} starts at {token!r}, which is not one of {read.choices}"
        raise ValueError(msg)


def _default(row: Row, read: Shape) -> Any:
    """Choose the default for a row that declares none.

    A row with no command line has no default at all, and a flag the parser
    can leave absent hands its command ``None``.
    """
    if row.default is not OMITTED:
        return _token(row.default, read)
    return UNSET if row.required or not row.on else None


def _label(row: Row, read: Shape) -> str:
    """Return the printed type label, which a repeatable row wraps in a list."""
    return f"list({read.label})" if isinstance(row, Many) else read.label


def _token(default: Any, read: Shape) -> Any:
    """Convert a declared default to the token the parser stores.

    A path row declares ``Path("pyproject.toml")``, which is what the
    command parameter takes, while the parser holds the word it was written
    as.
    """
    if read.vtype == "path" and isinstance(default, Path):
        return str(default)
    return default
