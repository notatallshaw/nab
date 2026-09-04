"""Render help from the parser's option tables without writing it.

The caller supplies the colour decision. Only terminal width comes from the environment.
"""

from __future__ import annotations

import os

from nab._cli.parse import Row, Spec, Table, build

__all__ = ["page", "terminal_width", "wrap"]

# What a page falls back to when no terminal answers.
_DEFAULT_WIDTH = 80

# The narrowest help column worth wrapping into: below this a word simply
# takes its own line, which is still better than cutting one in half.
_MIN_HELP = 20

_INDENT = "  "
_GAP = 2

# The metavar each value type prints when the row declares no choices.
_METAVARS = {"path": "PATH", "int": "INT", "str": "STR"}

# Keep bold and cyan separate because some themes give their combined bright
# colour too little contrast. Local codes keep ``nab.output`` off this path.
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_RESET = "\033[0m"

# One entry holds measured text, displayed coloured text, and help.
# The display form has escapes, which occupy no cell.
_Entry = tuple[str, str, str]


def _paint(text: str, code: str, *, color: bool) -> str:
    """Wrap ``text`` in ``code``, or hand it back plain when colour is off."""
    return f"{code}{text}{_RESET}" if color else text


def terminal_width(environ: dict[str, str] | None = None) -> int:
    """Find the width to wrap to: ``COLUMNS``, then the terminal, then 80.

    ``COLUMNS`` is read first because it is what a user sets to widen or
    narrow a page, and it is the only way a test can pin one.
    """
    env = os.environ if environ is None else environ

    # str.isdigit() is not enough of a test: int() refuses a superscript
    # digit, and a digit run past CPython's conversion limit.
    try:
        declared = int(env.get("COLUMNS", ""))
    except ValueError:
        declared = 0

    if declared > 0:
        return declared

    try:
        return os.get_terminal_size().columns
    except OSError:
        return _DEFAULT_WIDTH


def wrap(text: str, width: int) -> list[str]:
    """Wrap ``text`` without splitting words.

    A word longer than ``width`` occupies one over-width line.
    """
    lines: list[str] = []
    line = ""

    for word in text.split():
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line = f"{line} {word}"
        else:
            lines.append(line)
            line = word

    if line:
        lines.append(line)

    return lines or [""]


def page(
    command: str,
    root: tuple[Spec, ...],
    commands: dict[str, tuple[Spec, ...]],
    texts: tuple[str, ...],
    dispatch: dict[str, tuple[str, str, str]],
    prog: str,
    columns: int = 0,
    *,
    color: bool = False,
) -> str:
    """Build the page for ``command``, or the root page when it is empty.

    ``columns`` overrides the terminal width, which is how a test pins a
    layout without touching the environment.  ``color`` is the caller's
    decision, already made against the stream the page is written to.
    """
    width = columns or terminal_width()
    root_table = build(root, root=True)
    usage = _paint("Usage:", _BOLD, color=color)

    if not command:
        sections = [
            ("Commands", _command_entries(dispatch, color=color)),
            ("Options", _option_entries(root_table, texts, color=color)),
        ]
        return _layout(
            f"{usage} {prog} [options] <command> [arguments]",
            "",
            sections,
            f"Try '{prog} <command> --help' for more information.",
            width,
            color=color,
        )

    table = build(commands[command])
    sections = [
        ("Arguments", _operand_entries(table, texts)),
        ("Options", _option_entries(table, texts, color=color)),
        ("Global options", _option_entries(root_table, texts, color=color)),
    ]
    return _layout(
        f"{usage} {prog} {command} [options]{_operand_usage(table)}",
        dispatch[command][2],
        sections,
        "",
        width,
        color=color,
    )


def _command_entries(
    dispatch: dict[str, tuple[str, str, str]],
    *,
    color: bool,
) -> list[_Entry]:
    """List the commands, in the order the table declares them."""
    return [
        (name, _paint(name, _CYAN, color=color), summary)
        for name, (_module, _function, summary) in dispatch.items()
    ]


def _option_entries(
    table: Table,
    texts: tuple[str, ...],
    *,
    color: bool,
) -> list[_Entry]:
    """One entry per option, with a generated negation folded into its row."""
    negations = {row.dest: row.long for row in table.rows if row.kind == "neg"}

    entries = []
    for row in table.rows:
        if row.kind == "neg" or not row.long:
            continue
        negation = negations.get(row.dest, "")
        entries.append(
            (
                _spelling(row, negation, color=False),
                _spelling(row, negation, color=color),
                texts[row.help_index],
            )
        )

    return entries


def _operand_entries(table: Table, texts: tuple[str, ...]) -> list[_Entry]:
    """One entry per operand slot, in the order it binds.

    An operand names a placeholder rather than an option to type, so it
    takes no colour and its two forms are the same string.
    """
    entries = []
    for row in table.operands:
        spelling = _operand_spelling(row)
        entries.append((spelling, spelling, texts[row.help_index]))

    return entries


def _spelling(row: Row, negation: str, *, color: bool) -> str:
    """How one option is written: its names, its value, then its negation.

    The names and the negation are literal options; the metavar
    stands in for a value, so it stays the terminal's own foreground.
    """
    names = [f"-{row.short}", row.long] if row.short else [row.long]
    spelling = ", ".join(_paint(name, _CYAN, color=color) for name in names)

    metavar = _metavar(row)
    if metavar:
        spelling = f"{spelling} {metavar}"

    if negation:
        return f"{spelling} / {_paint(negation, _CYAN, color=color)}"
    return spelling


def _operand_spelling(row: Row) -> str:
    """How one operand slot is written: its name, then the verbs it takes."""
    if row.choices:
        return f"{row.dest} {{{','.join(row.choices)}}}"
    return row.dest


def _operand_usage(table: Table) -> str:
    """Write the operand half of a usage line, required slots in angles."""
    parts = []
    for row in table.operands:
        if row.required:
            parts.append(f" <{row.dest}>")
        elif row.default == ():
            parts.append(f" [{row.dest} ...]")
        else:
            parts.append(f" [{row.dest}]")
    return "".join(parts)


def _metavar(row: Row) -> str:
    """Name one option's value on the page, empty when it reads none."""
    if not row.takes_value:
        return ""

    if row.choices:
        name = f"{{{','.join(row.choices)}}}"
    elif row.vtype == "bool":
        name = "{True,False}"
    else:
        name = _METAVARS[row.vtype]

    if row.kind == "star":
        return f"{name} ..."
    # A tri reads its value only when one is written, so the bare flag is
    # valid too and the brackets are the page's way of saying so.
    return f"[{name}]" if row.kind == "tri" else name


def _layout(
    usage: str,
    summary: str,
    sections: list[tuple[str, list[_Entry]]],
    trailer: str,
    width: int,
    *,
    color: bool,
) -> str:
    """Lay out one option column and wrap every help line."""
    lines = [usage]
    if summary:
        lines.append("")
        lines.extend(wrap(summary, width))

    column = _column(sections, width)
    for heading, entries in sections:
        if not entries:
            continue
        lines.append("")
        lines.append(_paint(f"{heading}:", _BOLD, color=color))
        for plain, painted, text in entries:
            lines.extend(_entry(plain, painted, text, column, width))

    if trailer:
        lines.append("")
        lines.append(trailer)

    return "\n".join(lines) + "\n"


def _column(sections: list[tuple[str, list[_Entry]]], width: int) -> int:
    """Start help after the widest option that fits in half the page.

    A wider option takes its own line instead of
    pushing every help line into the right margin.
    """
    limit = width // 2
    widths = [
        len(plain)
        for _heading, entries in sections
        for plain, _painted, _text in entries
        if len(plain) <= limit
    ]
    return len(_INDENT) + max(widths, default=0) + _GAP


def _entry(plain: str, painted: str, text: str, column: int, width: int) -> list[str]:
    """Write one option and hang its help under the column.

    The padding is measured off ``plain`` and written with ``painted``, so a
    page keeps one column whether or not it carries escapes.
    """
    head = _INDENT + plain
    body = wrap(text, max(width - column, _MIN_HELP))
    hanging = [" " * column + line for line in body[1:]]

    if len(head) + _GAP <= column:
        padding = " " * (column - len(head))
        return [_INDENT + painted + padding + body[0], *hanging]

    return [_INDENT + painted, *[" " * column + line for line in body]]
