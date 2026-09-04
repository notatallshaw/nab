"""Define lowered CLI options and validate option tables.

``Opt`` validates one row; :func:`validate` checks relationships across the table.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any

from ._compat import override
from .optionrows import Scope

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from typing_extensions import Protocol

    class _SourceKind(Protocol):
        """A configuration source, as the config layer names it."""

        @property
        def value(self) -> str:
            """The source name used to enforce option scope."""
            ...


__all__ = [
    "COMMANDS",
    "GLOBAL",
    "UNSET",
    "Kind",
    "Opt",
    "Scope",
    "Tokens",
    "VType",
    "validate",
]


class Kind(enum.Enum):
    """How the parser reads an option's occurrences off the command line.

    ``STAR`` is a repeatable flag.  An operand that takes every remaining
    word is ``POSITIONAL`` with ``default=()``.
    """

    FLAG = "flag"
    TRI = "tri"
    COUNT = "count"
    VALUE = "value"
    APPEND = "append"
    STAR = "star"
    EAGER = "eager"
    POSITIONAL = "positional"
    VERB = "verb"


class VType(enum.Enum):
    """The type a raw token converts to before anything else sees it."""

    STR = "str"
    PATH = "path"
    INT = "int"
    CHOICE = "choice"
    BOOL = "bool"


class Tokens(enum.Enum):
    """How the assembler reads a sub-row's tokens into its key's value."""

    SCALAR = "scalar"
    LIST = "list"
    ITEM = "item"
    ITEMS = "items"
    PAIRS = "pairs"


class _Unset:
    """The marker for a field a row does not declare."""

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        return "<unset>"


UNSET = _Unset()


def _no_hook(*_arguments: Any) -> Any:
    """Stand in for the parse and render a keyless row does not declare."""
    msg = "a row with no configuration key carries no parse or render hook"
    raise TypeError(msg)


# The commands marker for a root row: one that every command's table carries.
_ROOT = "*"
GLOBAL: tuple[str, ...] = (_ROOT,)

# The kinds that bind a positional operand rather than a flag.
_POSITIONAL_KINDS = frozenset({Kind.POSITIONAL, Kind.VERB})

# The kinds that read a value, and so need a vtype.
_VALUE_KINDS = frozenset(
    {Kind.VALUE, Kind.TRI, Kind.APPEND, Kind.STAR, Kind.POSITIONAL, Kind.VERB}
)

# The vtypes with no token set of their own, so a layered row of one has to
# write a sample token down.  A CHOICE row's tokens are its choices and a
# BOOL row's are True and False.
_SAMPLED_VTYPES = frozenset({VType.STR, VType.PATH, VType.INT})

# The TOML sources that may set a row of each scope, by the
# value of the SourceKind member naming them.
_ALLOWED_TOML_SOURCES = {
    Scope.PROJECT: frozenset({"pyproject", "project"}),
    Scope.USER: frozenset({"project", "user", "system"}),
}


class Opt:
    """One option: everything nab knows about it, in one place.

    The first argument is the configuration key on a layered row and the
    plain name on a command-local one.  ``default`` is what the command
    function receives when the option is absent; ``rdefault`` is rung 0
    of the configuration ladder, and only a layered row carries it.
    ``sample`` is a token the row accepts, written down for a value whose
    vtype names no token set of its own.  ``parse`` and ``render`` are the
    hooks the ladder reads a source with and prints the winner with, and a
    layered row carries those too.
    """

    __slots__ = (
        "choices",
        "commands",
        "default",
        "deprecated",
        "docs",
        "env",
        "help",
        "kind",
        "name",
        "needed",
        "negatable",
        "nullable",
        "opened_by",
        "parse",
        "rdefault",
        "render",
        "required",
        "sample",
        "scope",
        "short",
        "tokens",
        "type_label",
        "under",
        "vtype",
    )

    name: str
    scope: Scope | None
    kind: Kind | None
    vtype: VType | None
    choices: tuple[str, ...]
    nullable: bool
    negatable: bool
    env: bool
    commands: tuple[str, ...]
    default: Any
    rdefault: Any
    required: bool
    type_label: str
    deprecated: bool
    sample: str
    short: str
    help: str
    docs: str
    under: str
    needed: bool
    tokens: Tokens | None
    opened_by: str

    def __init__(  # noqa: PLR0913 - the type's own fields, in its own order
        self,
        name: str,
        *,
        scope: Scope | None = None,
        kind: Kind | None = None,
        vtype: VType | None = None,
        choices: tuple[str, ...] = (),
        nullable: bool = False,
        negatable: bool = False,
        env: bool = False,
        commands: tuple[str, ...] = (),
        default: Any = UNSET,
        rdefault: Any = UNSET,
        required: bool = False,
        parse: Callable[[Any, str], Any] | None = None,
        render: Callable[[Any], str] | None = None,
        type_label: str = "",
        deprecated: bool = False,
        sample: str = "",
        short: str = "",
        help: str = "",  # noqa: A002 - the row's own field name
        docs: str = "",
        under: str = "",
        needed: bool = False,
        tokens: Tokens | None = None,
        opened_by: str = "",
    ) -> None:
        """Record one option and run the rules a single row can be judged by."""
        self.name = name
        self.scope = scope
        self.kind = kind
        self.vtype = vtype
        self.choices = choices
        self.nullable = nullable
        self.negatable = negatable
        self.env = env
        self.commands = commands
        self.default = default
        self.rdefault = rdefault
        self.required = required

        # Annotated here, not in the field block: zuban reads a class-level
        # ``Callable`` as a method and drops the first argument at every call.
        self.parse: Callable[[Any, str], Any] = _no_hook if parse is None else parse
        self.render: Callable[[Any], str] = _no_hook if render is None else render

        self.type_label = type_label
        self.deprecated = deprecated
        self.sample = sample
        self.short = short
        self.help = help
        self.docs = docs
        self.under = under
        self.needed = needed
        self.tokens = tokens
        self.opened_by = opened_by

        self._check()

    @override
    def __repr__(self) -> str:
        """Name the row, so a rule's failure message says which one broke."""
        return f"Opt({self.name!r})"

    @property
    def key(self) -> str | None:
        """The configuration key, or ``None`` on a row no source can set.

        A row ``under`` a key spells one key of that key's table, so the
        parent holds the registry key and this row holds none.
        """
        return None if self.scope is None or self.under else self.name

    @property
    def scope_name(self) -> str:
        """The scope word a report prints, for a row a source can set."""
        if self.scope is None:
            msg = f"{self.name} is not a layered row, so it has no scope word"
            raise TypeError(msg)
        return self.scope.value

    @property
    def is_global(self) -> bool:
        """Whether this row sits at the root and in every command's table."""
        return _ROOT in self.commands

    @property
    def is_positional(self) -> bool:
        """Whether this row binds an operand rather than a flag."""
        return self.kind in _POSITIONAL_KINDS

    @property
    def cli_flag(self) -> str | None:
        """The long flag, or ``None`` on a row that has no flag.

        The scope decides the prefix. A repeatable option is singular
        because one occurrence contributes one value. A row under a table
        key includes that key.
        """
        if not self.commands or self.is_positional:
            return None
        prefix = "--project-" if self.scope is Scope.PROJECT else "--"
        stem = self.name[:-1] if self.kind is Kind.APPEND else self.name
        if self.under:
            stem = f"{self.under}-{stem}"
        return prefix + stem

    @property
    def dest(self) -> str:
        """The command parameter, or the value slot, this row binds."""
        flag = self.cli_flag
        source = self.name if flag is None else flag[2:]
        return source.replace("-", "_")

    @property
    def cli_param(self) -> str | None:
        """The command parameter behind the flag, ``None`` where there is none."""
        return None if self.cli_flag is None else self.dest

    @property
    def env_var(self) -> str | None:
        """The ``NAB_*`` variable that may set this row, or ``None``."""
        return f"NAB_{self.name.upper().replace('-', '_')}" if self.env else None

    def allowed_in_toml(self, kind: _SourceKind) -> bool:
        """Whether a TOML source of ``kind`` may set this row.

        The four TOML kinds are matched by value, so this check needs no
        name from the config layer.  A row with no key is set by no
        source, TOML included.
        """
        if self.scope is None or self.under:
            return False
        return kind.value in _ALLOWED_TOML_SOURCES[self.scope]

    def _check(self) -> None:
        """Run the rules one row can be judged by, raising on the first break."""
        if self.short and (len(self.short) != 1 or not self.short.isalnum()):
            msg = f"{self.name} takes one alphanumeric short name, not {self.short!r}"
            raise ValueError(msg)

        if self.negatable and self.name.startswith("no-"):
            msg = f"{self.name} is negatable, so it must not be named no-"
            raise ValueError(msg)

        if self.kind in _VALUE_KINDS and self.vtype is None:
            msg = f"{self.name} reads a value, so it needs a vtype"
            raise ValueError(msg)

        if not self.name or any(bad in self.name for bad in ("=", " ", "_")):
            msg = f"{self.name!r} is not a usable long name"
            raise ValueError(msg)

        self._check_derivation()
        self._check_sub_row()

        if self.env and self.scope is not Scope.USER:
            msg = f"{self.name} is not a USER key, so it takes no NAB_ name"
            raise ValueError(msg)

        if self.key is not None and (self.parse is _no_hook or self.render is _no_hook):
            msg = f"{self.name} is layered, so it needs a parse and a render hook"
            raise ValueError(msg)

        if not self.help:
            msg = f"{self.name} declares no help"
            raise ValueError(msg)

        self._check_file_only()

        if self.key is not None and self.vtype in _SAMPLED_VTYPES and not self.sample:
            msg = f"{self.name} is layered and free-form, so it needs a sample"
            raise ValueError(msg)

    def _check_derivation(self) -> None:
        """Check the name against the two derivations :attr:`cli_flag` runs on it."""
        if self.scope is Scope.PROJECT and self.name.startswith("project-"):
            msg = f"{self.name} takes its project- prefix from its scope"
            raise ValueError(msg)

        if self.kind is Kind.APPEND and not self.name.endswith("s"):
            msg = f"{self.name} is repeatable, so its name has to be plural"
            raise ValueError(msg)

        if self.under and self.name.startswith(f"{self.under}-"):
            msg = f"{self.name} takes its {self.under}- prefix from the key it is under"
            raise ValueError(msg)

    def _check_sub_row(self) -> None:
        """Check a row that spells one key of a table key, not a key of its own."""
        if not self.under:
            return

        if self.scope is None:
            msg = f"{self.name} is under {self.under!r}, so it needs a scope"
            raise ValueError(msg)

        if not self.commands:
            msg = f"{self.name} is under {self.under!r}, so it needs a command line"
            raise ValueError(msg)

        if self.is_positional:
            msg = f"{self.name} is under {self.under!r}, so it cannot be an operand"
            raise ValueError(msg)

        if self.rdefault is not UNSET or self.parse is not _no_hook or self.env:
            msg = f"{self.name} is under {self.under!r}, so it takes no key of its own"
            raise ValueError(msg)

    def _check_file_only(self) -> None:
        """Check a row with no command line, which is a configuration key alone."""
        if self.commands:
            return

        if self.key is None or self.rdefault is UNSET:
            msg = f"{self.name} has no command line, so it needs a key and rdefault"
            raise ValueError(msg)

        if self.short or self.env or self.default is not UNSET:
            msg = f"{self.name} has no command line, so it takes no CLI fields"
            raise ValueError(msg)


def validate(
    rows: Sequence[Opt], commands: Sequence[tuple[str, str, str]] | None = None
) -> None:
    """Run the rules that span rows over a whole table.

    ``commands`` omitted means the declared :data:`COMMANDS`; a test
    passes its own rows and the command entries that go with them.
    """
    entries = COMMANDS if commands is None else commands
    names = _command_names(entries)
    _check_command_cover(rows, names)
    _check_root_operands(rows)
    _check_parents(rows)

    root = [row for row in rows if row.is_global]
    _check_unique(root, "the root table")

    for name in names:
        table = [row for row in rows if name in row.commands]
        _check_unique(table, f"nab {name}")
        _check_root_collisions(table, root, name)
        _check_negations([*root, *table], name)
        _check_operand_order(table, name)


def _command_names(entries: Sequence[tuple[str, str, str]]) -> tuple[str, ...]:
    """Return the declared command names, refusing any that reads as an option."""
    for name, _module, _function in entries:
        if name.startswith("-"):
            msg = f"{name!r} is not a usable command name"
            raise ValueError(msg)
    return tuple(name for name, _module, _function in entries)


def _check_command_cover(rows: Sequence[Opt], names: Sequence[str]) -> None:
    """Check that rows and commands name each other and nothing else."""
    named: set[str] = set()
    for row in rows:
        if row.is_global:
            continue
        for command in row.commands:
            if command not in names:
                msg = f"{row.name} names the undeclared command {command!r}"
                raise ValueError(msg)
            named.add(command)

    missing = [name for name in names if name not in named]
    if missing:
        msg = f"no row names the command {missing[0]!r}"
        raise ValueError(msg)


def _check_root_operands(rows: Sequence[Opt]) -> None:
    """Check that no root row takes an operand, the slot the command name holds."""
    for row in rows:
        if row.is_global and row.is_positional:
            msg = f"{row.name} is a root operand, and the command holds that slot"
            raise ValueError(msg)


def _check_parents(rows: Sequence[Opt]) -> None:
    """Check every sub-row names a keyed row of its own scope and with no flag."""
    keyed = {row.name: row for row in rows if row.key is not None}
    for row in rows:
        if not row.under:
            continue

        parent = keyed.get(row.under)
        if parent is None:
            msg = f"{row.name} is under {row.under!r}, which no row declares as a key"
            raise ValueError(msg)

        if parent.scope is not row.scope:
            msg = (
                f"{row.name} is {row.scope_name}-scope under {row.under!r},"
                f" which is {parent.scope_name}-scope"
            )
            raise ValueError(msg)

        if parent.commands:
            msg = (
                f"{row.under!r} takes a flag of its own, so {row.name} cannot"
                " spell it as well"
            )
            raise ValueError(msg)


def _check_unique(table: Sequence[Opt], where: str) -> None:
    """Check that no flag name is declared twice inside one table."""
    seen_flags: set[str] = set()
    seen_shorts: set[str] = set()
    for row in table:
        flag = row.cli_flag
        if flag is not None:
            if flag in seen_flags:
                msg = f"{where} declares {flag} twice"
                raise ValueError(msg)
            seen_flags.add(flag)

        if row.short:
            if row.short in seen_shorts:
                msg = f"{where} declares -{row.short} twice"
                raise ValueError(msg)
            seen_shorts.add(row.short)


def _check_root_collisions(
    table: Sequence[Opt], root: Sequence[Opt], name: str
) -> None:
    """Check that a command's option names never shadow the root's."""
    flags = {row.cli_flag for row in root}
    shorts = {row.short for row in root if row.short}
    for row in table:
        if row.cli_flag in flags:
            msg = f"nab {name} redeclares the root flag {row.cli_flag}"
            raise ValueError(msg)
        if row.short and row.short in shorts:
            msg = f"nab {name} redeclares the root short -{row.short}"
            raise ValueError(msg)


def _check_negations(table: Sequence[Opt], name: str) -> None:
    """Check that a generated ``--no-X`` never lands on a declared flag.

    The negation is built from the flag, so it carries the scope prefix and
    the repeatable singular that the flag does.
    """
    declared = {row.cli_flag for row in table}
    for row in table:
        flag = row.cli_flag
        if not row.negatable or flag is None:
            continue
        negation = "--no-" + flag[2:]
        if negation in declared:
            msg = f"nab {name} declares {negation}, which {row.name} generates"
            raise ValueError(msg)


def _check_operand_order(table: Sequence[Opt], name: str) -> None:
    """Check that the operand slots bind in one unambiguous order.

    An operand whose default is the empty tuple takes every remaining
    word, so a second one would have nothing left to bind.
    """
    operands = [row for row in table if row.is_positional]

    repeatable = [row for row in operands if row.default == ()]
    if len(repeatable) > 1:
        first, second = repeatable[0].name, repeatable[1].name
        msg = f"nab {name} gives the rest of the line to {first} and then to {second}"
        raise ValueError(msg)

    optional = ""
    for row in operands:
        if row.required and optional:
            msg = f"nab {name} requires {row.name} after optional {optional}"
            raise ValueError(msg)
        if not row.required:
            optional = row.name


# The four commands in help order, each with the module and function that
# runs it.  The function name does not follow from the command name.
COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("cache", "nab._cache_cmd", "cache_command"),
    ("config", "nab._config_cmd", "config_command"),
    ("lock", "nab._lock", "lock"),
    ("download", "nab._download", "download"),
)
