"""Parse ``argv`` from generated option tables without writing or exiting.

Occurrences are reduced and converted after the walk, so eager options can skip
conversion.
"""

from __future__ import annotations

__all__ = [
    "Parsed",
    "Row",
    "Spec",
    "Table",
    "UsageError",
    "build",
    "parse",
]

# A generated row: long, short, kind, dest, vtype, choices, const, default,
# required, help index.  ``nab/_cli/spec.py`` is a file of these.
Spec = tuple[str, str, str, str, str, tuple[str, ...], object, object, bool, int]

# The kinds that read no value: a flag, its generated negation, a counter
# and an option that ends the line where it stands.
_VALUELESS_KINDS = frozenset({"flag", "neg", "count", "eager"})

# The prefix that makes a token a long option.
_LONG = "--"

# What a tri-state boolean accepts as a separated value.
_TRI_VALUES = frozenset({"True", "False", "None"})

# The two spellings a bool converts from, in the order the message lists them.
_BOOL_VALUES = ("True", "False")


class UsageError(Exception):
    """A command line the walk refuses, with what a suggestion needs.

    ``token`` is the word the user typed and ``candidates`` the spellings
    it might have meant, both empty on an error no suggestion can help.
    ``root_options`` holds the root flags read before the refusal, so a
    refusal honours ``--color`` the way an eager page does.
    """

    __slots__ = ("candidates", "message", "prog", "root_options", "token")

    def __init__(
        self,
        prog: str,
        message: str,
        *,
        token: str = "",
        candidates: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.prog = prog
        self.message = message
        self.token = token
        self.candidates = candidates
        self.root_options: dict[str, object] = {}


class Row:
    """One generated row, unpacked once so the walk reads attributes.

    ``nullable`` and ``vtype`` come off the one generated field: a trailing
    ``?`` means the literal string ``None`` is accepted and means unset.
    ``root`` marks a row of the root table, which every command's line may
    also carry.
    """

    __slots__ = (
        "choices",
        "const",
        "default",
        "dest",
        "help_index",
        "kind",
        "long",
        "nullable",
        "required",
        "root",
        "short",
        "vtype",
    )

    def __init__(self, spec: Spec, *, root: bool = False) -> None:
        long, short, kind, dest, vtype, choices, const, default, required, index = spec
        self.long = long
        self.short = short
        self.kind = kind
        self.dest = dest
        self.nullable = vtype.endswith("?")
        self.vtype = vtype[:-1] if self.nullable else vtype
        self.choices = choices
        self.const = const
        self.default = default
        self.required = required
        self.help_index = index
        self.root = root

    @property
    def takes_value(self) -> bool:
        """Whether an occurrence of this row reads a value from the line."""
        return self.kind not in _VALUELESS_KINDS


class Table:
    """One command's rows: the spellings to match and the operands to fill."""

    __slots__ = ("operands", "options", "rows")

    def __init__(
        self, rows: tuple[Row, ...], options: dict[str, Row], operands: tuple[Row, ...]
    ) -> None:
        self.rows = rows
        self.options = options
        self.operands = operands


class Parsed:
    """What one command line means once the walk is done.

    ``eager`` names the option that short-circuited the line (``help`` or
    ``version``) and is empty on a line that parsed through.  ``values``
    holds the command's own parameters and ``options`` the root ones; on a
    short-circuited line ``values`` is empty and ``options`` holds only the
    root flags the line had reached, unconverted.
    """

    __slots__ = ("command", "eager", "options", "prog", "values")

    def __init__(
        self,
        command: str,
        values: dict[str, object],
        options: dict[str, object],
        prog: str,
        eager: str = "",
    ) -> None:
        self.command = command
        self.values = values
        self.options = options
        self.prog = prog
        self.eager = eager


# One occurrence of a row: the row it was, and what it stored.
_Hit = tuple[Row, object]

# The pair conversion works over: the row that stored a value and its dest.
_Pending = tuple[Row, str]

# Both live tables, innermost first.  A command's spellings never repeat the
# root's, so the order is unobservable and only makes the lookup deterministic.
_Tables = tuple[Table, Table]


def build(rows: tuple[Spec, ...], *, root: bool = False) -> Table:
    """Reduce ``rows`` to a table: spellings to rows, and the operands in order."""
    built: list[Row] = []
    options: dict[str, Row] = {}
    operands: list[Row] = []

    for spec in rows:
        row = Row(spec, root=root)
        built.append(row)
        if row.long:
            options[row.long] = row
            if row.short:
                options["-" + row.short] = row
        else:
            operands.append(row)

    return Table(tuple(built), options, tuple(operands))


def parse(
    argv: tuple[str, ...],
    root: tuple[Spec, ...],
    commands: dict[str, tuple[Spec, ...]],
    prog: str,
) -> Parsed:
    """Walk ``argv`` once and return what it means.

    A refusal carries the root flags read before it, so ``--color`` reaches
    a message the walk never finished reading the line for.
    """
    seen: dict[str, list[_Hit]] = {}
    try:
        return _walk(argv, root, commands, prog, seen)
    except UsageError as error:
        error.root_options = _root_options(seen)
        raise


def _walk(
    argv: tuple[str, ...],
    root: tuple[Spec, ...],
    commands: dict[str, tuple[Spec, ...]],
    prog: str,
    seen: dict[str, list[_Hit]],
) -> Parsed:
    """Walk ``argv`` once and return what it means.

    The first operand at the root level is the command name, so a word that
    names no command is an unknown-command error rather than an operand.
    Raises :class:`UsageError` on the first thing it cannot read.
    """
    root_table = build(root, root=True)
    table = build(())
    operands: list[str] = []
    command = ""
    prog_path = prog
    unnamed_commands = tuple(commands)
    index = 0
    ended = False

    while index < len(argv):
        word = argv[index]
        index += 1

        if not command and (ended or not _is_option(word)):
            if word not in commands:
                raise _unknown_command(prog_path, word, commands)
            command = word
            table = build(commands[word])
            prog_path = f"{prog} {word}"

            # The command opens a level of its own: a ``--`` before it
            # ended the root's options rather than the line's, and its
            # name has stopped being a spelling a suggestion may offer.
            unnamed_commands = ()
            ended = False
        elif ended:
            operands.append(word)
        elif word == "--":
            ended = True
        elif word.startswith(_LONG) and len(word) > len(_LONG):
            index, eager = _long(
                word,
                argv,
                index,
                (table, root_table),
                seen,
                prog_path,
                unnamed_commands,
            )
            if eager:
                return _short_circuit(command, seen, prog_path, eager)
        elif _is_option(word):
            index, eager = _short(
                word, argv, index, (table, root_table), seen, prog_path
            )
            if eager:
                return _short_circuit(command, seen, prog_path, eager)
        else:
            operands.append(word)

    return _finish(command, table, root_table, seen, operands, prog_path, commands)


def _reduce(hits: list[_Hit]) -> object:
    """Fold every occurrence of one dest into the single value it stands for."""
    row = hits[-1][0]
    if row.kind == "count":
        return len(hits)
    if row.kind == "append":
        return tuple(hit[1] for hit in hits)
    return hits[-1][1]


def _short_circuit(
    command: str, seen: dict[str, list[_Hit]], prog: str, eager: str
) -> Parsed:
    """End the line on an eager option, carrying the root flags it had read.

    Conversion has not run, so these are the tokens as typed; ``--color`` is
    the one an eager page reads, and it decides nothing a bad token could
    break.
    """
    return Parsed(command, {}, _root_options(seen), prog, eager)


def _root_options(seen: dict[str, list[_Hit]]) -> dict[str, object]:
    """Return the root flags read so far, as typed, before conversion runs."""
    return {dest: _reduce(hits) for dest, hits in seen.items() if hits[-1][0].root}


def _is_option(word: str) -> bool:
    """Whether ``word`` is option-shaped: a dash and at least one more character."""
    return word[:1] == "-" and len(word) > 1


def _match(tables: _Tables, name: str) -> Row | None:
    """Find the row ``name`` spells, retrying an unmatched name with hyphens.

    Both underscored and dashed spellings have always parsed, and no declared
    name holds an underscore, so the retry can never resolve to a second row.
    The alias is a lookup key: the caller still quotes the token the user
    typed.
    """
    inner, outer = tables
    row = inner.options.get(name) or outer.options.get(name)
    if row is not None:
        return row
    alias = name.replace("_", "-")
    return inner.options.get(alias) or outer.options.get(alias)


def _store(seen: dict[str, list[_Hit]], row: Row, value: object) -> None:
    """Record one occurrence of ``row``, to be reduced once the line is read."""
    seen.setdefault(row.dest, []).append((row, value))


def _long(
    word: str,
    argv: tuple[str, ...],
    index: int,
    tables: _Tables,
    seen: dict[str, list[_Hit]],
    prog: str,
    unnamed_commands: tuple[str, ...],
) -> tuple[int, str]:
    """Read one ``--name`` token, returning the new index and the eager dest.

    An eager option is honoured after the attached-value check rather than
    before it, so ``--help=x`` is refused for taking a value rather than answered
    with a page.
    """
    name, sep, attached = word.partition("=")
    row = _match(tables, name)
    if row is None:
        raise _unknown_option(prog, name, tables, unnamed_commands)

    if not row.takes_value:
        if sep:
            raise _takes_no_value(prog, name)
        if row.kind == "eager":
            return index, row.dest
        _store(seen, row, row.const)
        return index, ""

    if sep and row.kind != "star":
        _store(seen, row, attached)
    elif row.kind == "star":
        index = _star_value(row, argv, index, seen, (attached,) if sep else ())
    elif row.kind == "tri":
        index = _tri_value(row, argv, index, seen)
    else:
        index = _separated(row, argv, index, name, seen, prog)

    return index, ""


def _short(
    word: str,
    argv: tuple[str, ...],
    index: int,
    tables: _Tables,
    seen: dict[str, list[_Hit]],
    prog: str,
) -> tuple[int, str]:
    """Read one ``-abc`` cluster, one letter at a time.

    An unknown letter raises, and the occurrences already stored go
    unread, because ``seen`` is the walk's only record.  A value-taking
    short option ends the cluster: the rest of the token is its value, or
    the next token is.  An eager letter is honoured once the cluster has
    been read through, so ``-hx`` names the unknown ``-x`` rather than
    printing a page.
    """
    eager = ""
    position = 1
    while position < len(word):
        letter = word[position]
        position += 1

        row = _match(tables, "-" + letter)
        if row is None:
            raise _unknown_short(prog, letter, word)

        if row.kind == "eager":
            eager = eager or row.dest
        elif not row.takes_value:
            _store(seen, row, row.const)
        elif position < len(word):
            _store(seen, row, word[position:])
            return index, eager
        else:
            return _separated(row, argv, index, "-" + letter, seen, prog), eager

    return index, eager


def _separated(
    row: Row,
    argv: tuple[str, ...],
    index: int,
    name: str,
    seen: dict[str, list[_Hit]],
    prog: str,
) -> int:
    """Take the next token as ``row``'s value, refusing one that looks like an option.

    The numeric carve-out is a property of the row rather than of the walk,
    so declaring an option whose name reads as a negative number cannot
    change how any other option finds its value.
    """
    if index >= len(argv):
        raise _missing_value(prog, name)

    following = argv[index]
    if _is_option(following) and not _numeric(row, following):
        raise _looks_like_option(prog, name, following)

    _store(seen, row, following)
    return index + 1


def _numeric(row: Row, token: str) -> bool:
    """Whether ``token`` reads as a number ``row`` would accept."""
    return row.vtype == "int" and token[1].isdigit()


def _tri_value(
    row: Row, argv: tuple[str, ...], index: int, seen: dict[str, list[_Hit]]
) -> int:
    """Take ``True``, ``False`` or ``None`` when it follows, else store the constant.

    A tri-state boolean reads as a flag when it stands alone and takes the
    value it is given when it is given one, which is what lets a config
    layer keep deciding an absent one.
    """
    if index < len(argv) and argv[index] in _TRI_VALUES:
        _store(seen, row, argv[index])
        return index + 1

    _store(seen, row, row.const)
    return index


def _star_value(
    row: Row,
    argv: tuple[str, ...],
    index: int,
    seen: dict[str, list[_Hit]],
    attached: tuple[str, ...],
) -> int:
    """Take ``attached``, then every following token up to the next option-shaped one.

    The attached and separated spellings take the same words, so
    ``--groups=dev docs`` and ``--groups dev docs`` select the same two
    groups.
    """
    taken = list(attached)
    while index < len(argv) and not _is_option(argv[index]):
        taken.append(argv[index])
        index += 1

    _store(seen, row, tuple(taken))
    return index


def _finish(
    command: str,
    table: Table,
    root: Table,
    seen: dict[str, list[_Hit]],
    operands: list[str],
    prog: str,
    commands: dict[str, tuple[Spec, ...]],
) -> Parsed:
    """Reduce every occurrence, bind the operands and convert what was given.

    The root values convert before the missing-command error, so
    ``nab --color lock`` names the bad choice rather than the missing
    command.
    """
    _require(table.rows, seen, prog)
    _require(root.rows, seen, prog)

    values = {row.dest: row.default for row in table.rows}
    options = {row.dest: row.default for row in root.rows}
    pending_root: list[_Pending] = []
    pending_command: list[_Pending] = []

    for dest, hits in seen.items():
        row = hits[-1][0]
        value = _reduce(hits)

        if row.root:
            options[dest] = value
            pending_root.append((row, dest))
        else:
            values[dest] = value
            pending_command.append((row, dest))

    _convert(pending_root, options, prog)

    if not command:
        raise _missing_command(prog, commands)

    _bind(table.operands, operands, values, pending_command, prog)
    _convert(pending_command, values, prog)

    return Parsed(command, values, options, prog)


def _require(rows: tuple[Row, ...], seen: dict[str, list[_Hit]], prog: str) -> None:
    """Refuse a required option one table declares and the line never gave.

    An operand's ``required`` is :func:`_bind`'s to answer, because only
    it knows how many words are left over for the slot.
    """
    for row in rows:
        if row.required and row.long and row.dest not in seen:
            raise _missing_option(prog, row.long)


def _bind(
    operands: tuple[Row, ...],
    words: list[str],
    values: dict[str, object],
    pending: list[_Pending],
    prog: str,
) -> None:
    """Fill the operand slots, in declaration order.

    A positional slot defaulting to the empty tuple takes every remaining
    word, the encoding at most one operand per command may use.
    """
    index = 0
    for row in operands:
        if row.kind == "positional" and row.default == ():
            values[row.dest] = tuple(words[index:])
            index = len(words)
        elif index < len(words):
            values[row.dest] = words[index]
            index += 1
        elif row.required:
            raise _missing_operand(prog, row)
        else:
            continue
        pending.append((row, row.dest))

    if index < len(words):
        raise _unexpected_operand(prog, words[index])


def _convert(pending: list[_Pending], values: dict[str, object], prog: str) -> None:
    """Convert every value that was given, leaving an absent one at its default."""
    for row, dest in pending:
        value = values[dest]
        if isinstance(value, tuple):
            values[dest] = tuple(_token(row, item, prog) for item in value)
        else:
            values[dest] = _token(row, value, prog)


def _token(row: Row, value: object, prog: str) -> object:
    """Convert one raw token to the type ``row``'s parameter is annotated with.

    A value that is not a string was stored by the walk rather than typed by
    the user: a flag's constant, or a tri-state boolean standing alone.
    """
    if not isinstance(value, str):
        return value

    if row.nullable and value == "None":
        return None

    if row.vtype == "int":
        return _integer(row, value, prog)

    if row.vtype == "bool":
        return _boolean(row, value, prog)

    if row.choices and value not in row.choices:
        raise _bad_choice(prog, row, value, row.choices)

    return value


def _integer(row: Row, value: str, prog: str) -> int:
    """Convert ``value`` to an integer, or say what was expected."""
    try:
        return int(value)
    except ValueError:
        raise _bad_number(prog, row, value) from None


def _boolean(row: Row, value: str, prog: str) -> bool:
    """Convert ``True`` or ``False``, the two spellings a boolean flag takes."""
    if value not in _BOOL_VALUES:
        raise _bad_choice(prog, row, value, _BOOL_VALUES)
    return value == "True"


def _spelling(row: Row) -> str:
    """How an error names ``row``: its flag, or its slot when it has none."""
    return row.long or row.dest


def _quote(token: str) -> str:
    """Quote a token from ``argv``, escaping one that is not valid UTF-8.

    ``sys.argv`` is decoded with ``surrogateescape`` on POSIX, so a token can
    carry lone surrogates that no ASCII stream can write.
    """
    try:
        token.encode("utf-8")
    except UnicodeEncodeError:
        return repr(token)
    return f"'{token}'"


def _option_names(
    tables: _Tables, unnamed_commands: tuple[str, ...], *, negations: bool
) -> tuple[str, ...]:
    """Collect the spellings a suggestion may offer, in declaration order.

    ``negations`` admits the generated ``--no-`` rows.  They are withheld
    from a positive typo, where a negation resembles the row it negates
    closely enough to fill the second slot every time, so ``--upgrad``
    would answer ``--upgrade`` and ``--no-upgrade``.  A typo spelled
    ``no-`` needs them, or the one spelling offered means the opposite of
    what was typed.

    Until a command has been named its name is a candidate too,
    so ``nab --lock`` offers ``lock``.
    """
    inner, outer = tables
    names = [
        row.long
        for table in (inner, outer)
        for row in table.rows
        if row.long and (negations or row.kind != "neg")
    ]
    names.extend(unnamed_commands)
    return tuple(names)


def _unknown_option(
    prog: str, name: str, tables: _Tables, unnamed_commands: tuple[str, ...]
) -> UsageError:
    """Refuse a long spelling no table declares."""
    message = f"unrecognized option {_quote(name)}"
    negations = name.lstrip("-").replace("_", "-").startswith("no-")
    candidates = _option_names(tables, unnamed_commands, negations=negations)
    return UsageError(prog, message, token=name, candidates=candidates)


def _unknown_short(prog: str, letter: str, cluster: str) -> UsageError:
    """Refuse one letter of a cluster, naming the cluster it came from."""
    message = f"unrecognized option {_quote('-' + letter)} in {_quote(cluster)}"
    return UsageError(prog, message)


def _takes_no_value(prog: str, name: str) -> UsageError:
    """Refuse an attached value on an option that reads none."""
    message = f"option {_quote(name)} does not take a value"
    return UsageError(prog, message)


def _missing_value(prog: str, name: str) -> UsageError:
    """Refuse a line that ended where a value was due."""
    message = f"option {_quote(name)} requires a value"
    return UsageError(prog, message)


def _looks_like_option(prog: str, name: str, following: str) -> UsageError:
    """Refuse an option-shaped next token, naming the attached form as the escape.

    A short option attaches its value with no separator, because a leading
    ``=`` counts as part of the value.
    """
    separator = "=" if name.startswith(_LONG) else ""
    message = (
        f"option {_quote(name)} requires a value, but {_quote(following)} "
        f"looks like an option; write {name}{separator}{following} to pass it"
    )
    return UsageError(prog, message)


def _bad_choice(
    prog: str, row: Row, value: str, choices: tuple[str, ...]
) -> UsageError:
    """Refuse a value outside ``choices``, which the message lists."""
    message = (
        f"invalid value {_quote(value)} for {_quote(_spelling(row))}; "
        f"choose from {', '.join(choices)}"
    )
    return UsageError(prog, message)


def _bad_number(prog: str, row: Row, value: str) -> UsageError:
    """Refuse a value the row's numeric type cannot read."""
    message = (
        f"invalid value {_quote(value)} for {_quote(_spelling(row))}: "
        f"expected an integer"
    )
    return UsageError(prog, message)


def _unknown_command(
    prog: str, word: str, commands: dict[str, tuple[Spec, ...]]
) -> UsageError:
    """Refuse a name in the root's operand slot that no command answers to."""
    message = f"unknown command {_quote(word)}"
    return UsageError(prog, message, token=word, candidates=tuple(commands))


def _missing_command(prog: str, commands: dict[str, tuple[Spec, ...]]) -> UsageError:
    """Refuse a line that ended with no command named."""
    message = f"a command is required; choose from {', '.join(commands)}"
    return UsageError(prog, message)


def _missing_option(prog: str, name: str) -> UsageError:
    """Refuse a required option the line never gave."""
    message = f"option {_quote(name)} is required"
    return UsageError(prog, message)


def _missing_operand(prog: str, row: Row) -> UsageError:
    """Refuse a required slot the line left empty, with its verbs when it has them."""
    message = f"missing required argument {_quote(row.dest)}"
    if row.choices:
        message += f"; choose from {', '.join(row.choices)}"
    return UsageError(prog, message)


def _unexpected_operand(prog: str, word: str) -> UsageError:
    """Refuse a word past the last slot that could hold it."""
    message = f"unexpected argument {_quote(word)}"
    return UsageError(prog, message)
