"""Lower a declared row to the :class:`nab.optiondefs.Opt` everything reads.

This is the one module that sees both sides, so the declaration gains a
vocabulary without ``Opt``, its rules or any reader of :data:`ALL` changing.

Two refusals live here because they need both sides of a row at once, and
both name the row that broke:

- a choice row that says it ``mirrors`` an enum whose members it no longer
  lists, which is the tie a hand-written ``Literal`` alias otherwise loses;
- a layered choice row whose rung 0 is not one of the tokens its own flag
  offers.

The refusals :func:`nab.optiontypes.shape` raises take the row's name from
here, so those read like the two above.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any

from .optiondefs import UNSET, Kind, Opt, Tokens, VType
from .optionrows import (
    OMITTED,
    Item,
    Items,
    Many,
    Pairs,
    Row,
    Star,
    Table,
    Tri,
    Verb,
    rows,
)
from .optiontypes import Shape, shape, type_argument

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


def lower(row: Row) -> Opt:
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


def table_rows(*tables: type[Table]) -> tuple[Opt, ...]:
    """Lower every row of every table, in declaration order."""
    return tuple(lower(row) for table in tables for row in rows(table))


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
    """Lower the default an unwritten one leaves to the kind.

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
    """Lower a declared default to the token the parser stores.

    A path row declares ``Path("pyproject.toml")``, which is what the
    command parameter takes, while the parser holds the word it was written
    as.
    """
    if read.vtype == "path" and isinstance(default, Path):
        return str(default)
    return default
