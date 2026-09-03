"""The flags that spell one configuration table, and how they assemble.

A table key such as ``matrix`` carries no flag of its own.  Its rows are
declared ``under`` it (:mod:`nab.optiontable`), so each spells one key of
the table and the command line writes ``--project-matrix-python`` rather
than a table.

:func:`build_cli_tables` reads those parameters back into a
:class:`CliTable`: the keys the line set, each with the flag that set it
and the tokens as typed.  :func:`fold_cli_table` lays them over the table
the project files declare, so one merged table reaches the key's own parse
hook and a key no flag named keeps the file's value.

A row's ``tokens`` field says how its own tokens read: one value, a list
of values, a list of tables, one table, or a table of ``KEY=VALUE`` pairs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..optiondefs import Opt, Tokens
from ..optiontable import ALL
from .values import CliTableError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "BY_PARENT",
    "CliKey",
    "CliTable",
    "build_cli_tables",
    "cli_table_label",
    "fold_cli_table",
    "render_cli_table",
]

# The rows that spell one key each of a table key, grouped by that key,
# in declaration order.
BY_PARENT: dict[str, tuple[Opt, ...]] = {
    parent: tuple(row for row in ALL if row.under == parent)
    for parent in dict.fromkeys(row.under for row in ALL if row.under)
}

# The two words TOML reads as booleans, so a knob reaches its hook as the
# type a file would have given it.
_BOOLEANS = frozenset({"true", "false"})

# No parameter is spelled by a flag other than its row's own.
_NO_FLAG_ALIASES: Mapping[str, str] = {}


class CliKey:
    """One table key the command line set: the flag, its tokens, its value."""

    __slots__ = ("flag", "key", "tokens", "value")

    def __init__(
        self, key: str, flag: str, tokens: tuple[str, ...], value: Any
    ) -> None:
        """Record what one sub-flag wrote."""
        self.key = key
        self.flag = flag
        self.tokens = tokens
        self.value = value

    @property
    def written(self) -> str:
        """The tokens as typed, for a record a reader retypes."""
        return " ".join(self.tokens)


class CliTable:
    """The keys of one table key the command line set, in declaration order."""

    __slots__ = ("keys",)

    def __init__(self, keys: tuple[CliKey, ...]) -> None:
        """Record the keys the line wrote."""
        self.keys = keys

    @property
    def overlay(self) -> dict[str, Any]:
        """The keys as a table, ready to lay over the one a file declares."""
        return {key.key: key.value for key in self.keys}


def build_cli_tables(
    locals_by_param: Mapping[str, Any], flags: Mapping[str, str] = _NO_FLAG_ALIASES
) -> dict[str, CliTable]:
    """Assemble each table key's sub-flag values into the keys they wrote.

    Returns ``{key: table}`` for a key whose sub-flags the line set, and
    nothing for one it did not.  ``flags`` names the flag a parameter was
    spelled by where that is not the row's own: ``--python`` writes the
    environment's ``python`` axis.
    """
    out: dict[str, CliTable] = {}
    for parent, sub_rows in BY_PARENT.items():
        keys = tuple(_cli_keys(sub_rows, locals_by_param, flags))
        if keys:
            out[parent] = CliTable(keys)
    return out


def _cli_keys(
    sub_rows: Sequence[Opt],
    locals_by_param: Mapping[str, Any],
    flags: Mapping[str, str],
) -> list[CliKey]:
    """Read one table's written sub-flags into its keys, in declaration order."""
    keys: list[CliKey] = []
    for row in sub_rows:
        param = str(row.cli_param)
        raw = locals_by_param[param]
        if not _written(row, raw):
            continue
        tokens = (raw,) if row.tokens is Tokens.SCALAR else tuple(raw)
        keys.append(
            CliKey(
                key=row.name,
                flag=flags.get(param, str(row.cli_flag)),
                tokens=tokens,
                value=_table_value(row, raw),
            )
        )
    return keys


def fold_cli_table(
    parent: Opt, table: CliTable, file_raw: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Lay the keys the command line set over the table the files declare.

    A key the line did not name keeps the file's value, and a list key the
    line did name is replaced whole; nothing appends.  With no file table
    there is nothing to fall back on, so every ``needs`` key has to be on
    the command line.
    """
    if file_raw is None:
        _check_needed(parent, table)
        return table.overlay
    return {**file_raw, **table.overlay}


def cli_table_label(parent: Opt) -> str:
    """Name the flag family a table key is spelled by.

    Read off a sub-row's own flag, so the label cannot drift from the
    flags it stands for.
    """
    row = BY_PARENT[parent.name][0]
    return str(row.cli_flag).removesuffix(row.name) + "*"


def render_cli_table(table: CliTable) -> str:
    """Render only the keys the command line set, as ``key=tokens``."""
    return ", ".join(f"{key.key}={key.written}" for key in table.keys)


def _written(row: Opt, raw: Any) -> bool:
    """Whether the command line wrote this sub-flag.

    A token run with no token stores the empty tuple, which is what an
    absent flag stores too, so it counts as unwritten.
    """
    if row.tokens is Tokens.SCALAR:
        return raw is not None
    return bool(raw)


def _check_needed(parent: Opt, table: CliTable) -> None:
    """Refuse a table missing a key the command line has to give with the rest."""
    needed = [row for row in BY_PARENT[parent.name] if row.needed]
    named = {key.key for key in table.keys}
    missing = [row for row in needed if row.name not in named]
    if not missing:
        return

    written = [key.flag for key in table.keys]
    missing_flags = [str(row.cli_flag) for row in missing]

    # "both" reads only when the two flags it stands for are the two the
    # sentence has already named.
    required = (
        "both"
        if len(written) + len(missing) == len(needed)
        else _flag_names([str(row.cli_flag) for row in needed])
    )

    msg = (
        f"{_flags_are(written)} set but {_flags_are(missing_flags)} not; the"
        f" project declares no [tool.nab.{parent.name}] for the command line to"
        f" narrow, so {required} are required."
    )
    raise CliTableError(msg)


def _flag_names(flags: Sequence[str]) -> str:
    """Join flags as a sentence names them: ``--a and --b``."""
    return " and ".join(flags)


def _flags_are(flags: Sequence[str]) -> str:
    """Name each flag, with the verb that agrees with the count."""
    return f"{_flag_names(flags)} {'is' if len(flags) == 1 else 'are'}"


def _table_value(row: Opt, raw: Any) -> Any:
    """Read one sub-flag's tokens into the value its key takes."""
    if row.tokens is Tokens.ITEM:
        return _one_item(row, raw)
    if row.tokens is Tokens.ITEMS:
        return _items(row, raw)
    if row.tokens is Tokens.PAIRS:
        return _pairs(row, raw)
    if row.tokens is Tokens.LIST:
        return [_scalar(token) for token in raw]
    return _scalar(raw)


def _one_item(row: Opt, tokens: Sequence[str]) -> dict[str, Any]:
    """Read a token run as the one table its key holds."""
    items = _items(row, tokens)
    if len(items) > 1:
        second = items[1][row.opened_by]
        msg = f"{row.cli_flag} takes one {row.name}, and {second!r} opens a second"
        raise CliTableError(msg)
    return items[0]


def _items(row: Opt, tokens: Sequence[str]) -> list[dict[str, Any]]:
    """Group a token run into tables, one opened per identifying key.

    A key written twice on one item is refused as TOML would refuse it.
    """
    items: list[dict[str, Any]] = []
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep:
            items.append({row.opened_by: _scalar(token)})
        elif key == row.opened_by:
            items.append({row.opened_by: _scalar(value)})
        elif not items:
            msg = (
                f"{row.cli_flag} reads {token!r} as a key on the item before"
                " it, and no item is open; write the id first"
            )
            raise CliTableError(msg)
        elif key in items[-1]:
            msg = f"{row.cli_flag} sets {key!r} twice on {items[-1][row.opened_by]!r}"
            raise CliTableError(msg)
        else:
            items[-1][key] = _scalar(value)
    return items


def _pairs(row: Opt, tokens: Sequence[str]) -> dict[str, Any]:
    """Read a token run as one table, refusing a key written twice as TOML would."""
    table: dict[str, Any] = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep:
            msg = f"{row.cli_flag} takes KEY=VALUE tokens, and {token!r} has no '='"
            raise CliTableError(msg)
        if key in table:
            msg = f"{row.cli_flag} sets {key!r} twice"
            raise CliTableError(msg)
        table[key] = _scalar(value)
    return table


def _scalar(text: str) -> object:
    """Read a value token: TOML's own booleans, else the text."""
    if text in _BOOLEANS:
        return text == "true"
    return text
