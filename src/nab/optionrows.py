"""The vocabulary a declaration is written in: one class per option kind.

A row is an instance of one of the classes below, bound to a name in a
:class:`Table` body.  The class says how the parser reads the option and the
type parameter says what the value is, so ``kind``, ``vtype``, ``choices``,
``nullable`` and most of ``type_label`` are read off the declaration instead
of written into it::

    class Root(Table, on=GLOBAL, docs="reference/cli.md"):
        verbose = Count(short="v", help="raise verbosity")
        color = Value[ColorChoice](help="when to colour stderr")

Only the fields a kind allows appear in its signature, so a short name on an
operand and a default on a counter are checker errors rather than rules that
raise once the module is imported.

The row classes carry ``__get__`` under :data:`typing.TYPE_CHECKING` alone,
so a checker reads ``Root.color`` as ``ColorChoice`` while
``nab.optionlower``, reading ``Root.__dict__``, gets the row.  Defining it
for real would hide the rows from the lowering and cost a descriptor call
per read.

The imports here are ``enum``, ``typing`` and ``typing_extensions``, so the
module reads a declaration without nab installed.  It is not neutral of nab
for all that: :class:`Scope` is nab's configuration model and every field of
:class:`Layer` is a rung of nab's ladder.  :mod:`nab.optionlower` is where
the rows meet :class:`nab.optiondefs.Opt`.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from typing_extensions import override

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")
C = TypeVar("C")

# The marker for a field the row does not write.  It is typed Any so an
# omitted default checks against any T while a written one is checked;
# nab.optionlower turns it into the Opt sentinel or into None.
OMITTED: Any = object()


class Scope(enum.Enum):
    """Whether an option configures the project or the user's environment."""

    PROJECT = "project"
    USER = "user"


class Layer(Generic[C]):
    """The configuration ladder's half of a row, in the value's own type.

    ``C`` is what a source parses to and what rung 0 holds, so a ``parse``
    hook that cannot produce ``rdefault`` is a checker error rather than a
    wrong report at run time.  Nothing reads the parameter back, unlike a
    row's own, so the subscript hands the class straight back and builds no
    alias.  ``parse`` and ``render`` are the hooks the ladder reads a source
    with and prints the winner with.  ``sample`` is a token the row accepts,
    written down for a value whose type names no token set of its own, and
    ``label`` overrides the printed type where the type parameter cannot
    spell it.
    """

    __slots__ = ("label", "parse", "rdefault", "render", "sample")

    def __class_getitem__(cls, item: object) -> type[Layer[Any]]:
        """Hand the class back: the parameter is the checker's alone."""
        return cls

    def __init__(
        self,
        *,
        rdefault: C,
        parse: Callable[[Any, str], C],
        render: Callable[[C], str],
        label: str = "",
        sample: str = "",
    ) -> None:
        """Record the ladder half of one row."""
        self.rdefault = rdefault
        self.parse = parse
        self.render = render
        self.label = label
        self.sample = sample


class Row:
    """What every row holds once its table has named it.

    ``kind`` is the parser's word for how the option is read, and the empty
    string on a key with no command line at all.
    """

    kind = ""

    __slots__ = (
        "__orig_class__",
        "default",
        "deprecated",
        "docs",
        "env",
        "help",
        "key",
        "mirrors",
        "name",
        "negatable",
        "on",
        "required",
        "scope",
        "short",
    )

    def __init__(  # noqa: PLR0913 - the row's own fields, in its own order
        self,
        *,
        help: str,  # noqa: A002 - the row's own field name
        docs: str = "",
        short: str = "",
        default: Any = OMITTED,
        on: tuple[str, ...] = (),
        key: Layer[Any] | None = None,
        negatable: bool = False,
        env: bool = False,
        deprecated: bool = False,
        required: bool = False,
        mirrors: type[enum.Enum] | None = None,
    ) -> None:
        """Record one row; the table names it and fills its defaults."""
        self.help = help
        self.docs = docs
        self.short = short
        self.default = default
        self.on = on
        self.key = key
        self.negatable = negatable
        self.env = env
        self.deprecated = deprecated
        self.required = required
        self.mirrors = mirrors

        self.name = ""
        self.scope: Scope | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        """Take the row's long name from the attribute it is bound to."""
        self.name = name.replace("_", "-")

    @override
    def __repr__(self) -> str:
        """Name the row, so a refusal says which one broke."""
        return f"{type(self).__name__}({self.name!r})"


class Count(Row):
    """A repeatable flag whose value is how many times it was written."""

    kind = "count"
    __slots__ = ()

    if TYPE_CHECKING:

        def __get__(self, obj: object, owner: type | None = None) -> int:
            """Read the value a command receives for this row."""
            ...

    def __init__(self, *, help: str, short: str = "", docs: str = "") -> None:  # noqa: A002
        """Record one row; the table names it and fills its defaults."""
        super().__init__(help=help, short=short, docs=docs, default=0)


class Switch(Row):
    """A flag that stores a constant."""

    kind = "flag"
    __slots__ = ()

    if TYPE_CHECKING:

        def __get__(self, obj: object, owner: type | None = None) -> bool:
            """Read the value a command receives for this row."""
            ...

    def __init__(
        self,
        *,
        help: str,  # noqa: A002
        default: bool,
        short: str = "",
        docs: str = "",
        on: tuple[str, ...] = (),
        negatable: bool = False,
    ) -> None:
        """Record one row; the table names it and fills its defaults."""
        super().__init__(
            help=help,
            default=default,
            short=short,
            docs=docs,
            on=on,
            negatable=negatable,
        )


class Eager(Row):
    """A flag acted on before anything else is parsed."""

    kind = "eager"
    __slots__ = ()

    if TYPE_CHECKING:

        def __get__(self, obj: object, owner: type | None = None) -> bool:
            """Read the value a command receives for this row."""
            ...

    def __init__(self, *, help: str, short: str = "", docs: str = "") -> None:  # noqa: A002
        """Record one row; the table names it and fills its defaults."""
        super().__init__(help=help, short=short, docs=docs, default=False)


class Tri(Row):
    """A flag with a negation, absent until one of the two is written."""

    kind = "tri"
    __slots__ = ()

    if TYPE_CHECKING:

        def __get__(self, obj: object, owner: type | None = None) -> bool | None:
            """Read the value a command receives for this row."""
            ...

    def __init__(
        self,
        *,
        help: str,  # noqa: A002
        key: Layer[Any] | None = None,
        docs: str = "",
        on: tuple[str, ...] = (),
        env: bool = False,
    ) -> None:
        """Record one row; the table names it and fills its defaults."""
        super().__init__(help=help, key=key, docs=docs, on=on, env=env, negatable=True)


class Value(Row, Generic[T]):
    """An option that reads one token, of the type parameter's type."""

    kind = "value"
    __slots__ = ()

    if TYPE_CHECKING:

        def __get__(self, obj: object, owner: type | None = None) -> T:
            """Read the value a command receives for this row."""
            ...

    def __init__(  # noqa: PLR0913 - the row's own fields, in its own order
        self,
        *,
        help: str,  # noqa: A002
        default: T = OMITTED,
        short: str = "",
        docs: str = "",
        on: tuple[str, ...] = (),
        key: Layer[Any] | None = None,
        env: bool = False,
        deprecated: bool = False,
        mirrors: type[enum.Enum] | None = None,
    ) -> None:
        """Record one row; the table names it and fills its defaults."""
        super().__init__(
            help=help,
            default=default,
            short=short,
            docs=docs,
            on=on,
            key=key,
            env=env,
            deprecated=deprecated,
            mirrors=mirrors,
        )


class Many(Row, Generic[T]):
    """A repeatable option: one occurrence contributes one value.

    The name is plural and the flag is its singular, so ``constraints``
    spells ``--project-constraint``.
    """

    kind = "append"
    __slots__ = ()

    if TYPE_CHECKING:

        def __get__(self, obj: object, owner: type | None = None) -> tuple[T, ...]:
            """Read the value a command receives for this row."""
            ...

    def __init__(
        self,
        *,
        help: str,  # noqa: A002
        docs: str = "",
        on: tuple[str, ...] = (),
        key: Layer[Any] | None = None,
    ) -> None:
        """Record one row; the table names it and fills its defaults."""
        super().__init__(help=help, docs=docs, on=on, key=key, default=())


class Star(Row, Generic[T]):
    """An option that takes every token up to the next flag."""

    kind = "star"
    __slots__ = ()

    if TYPE_CHECKING:

        def __get__(self, obj: object, owner: type | None = None) -> tuple[T, ...]:
            """Read the value a command receives for this row."""
            ...

    def __init__(
        self,
        *,
        help: str,  # noqa: A002
        docs: str = "",
        on: tuple[str, ...] = (),
    ) -> None:
        """Record one row; the table names it and fills its defaults."""
        super().__init__(help=help, docs=docs, on=on, default=())


class Operand(Row, Generic[T]):
    """A positional word.  It has no flag, so it takes no short name."""

    kind = "positional"
    __slots__ = ()

    if TYPE_CHECKING:

        def __get__(self, obj: object, owner: type | None = None) -> T:
            """Read the value a command receives for this row."""
            ...

    def __init__(
        self,
        *,
        help: str,  # noqa: A002
        default: T = OMITTED,
        docs: str = "",
        on: tuple[str, ...] = (),
    ) -> None:
        """Record one row; the table names it and fills its defaults."""
        super().__init__(help=help, default=default, docs=docs, on=on)


class Verb(Row, Generic[T]):
    """A required positional word out of a fixed set."""

    kind = "verb"
    __slots__ = ()

    if TYPE_CHECKING:

        def __get__(self, obj: object, owner: type | None = None) -> T:
            """Read the value a command receives for this row."""
            ...

    def __init__(
        self,
        *,
        help: str,  # noqa: A002
        docs: str = "",
        on: tuple[str, ...] = (),
    ) -> None:
        """Record one row; the table names it and fills its defaults."""
        super().__init__(help=help, docs=docs, on=on, required=True)


class Key(Row):
    """A configuration key with no command line at all."""

    __slots__ = ()

    def __init__(
        self,
        layer: Layer[Any],
        *,
        help: str,  # noqa: A002
        docs: str = "",
        deprecated: bool = False,
    ) -> None:
        """Record one row; the table names it and fills its defaults."""
        super().__init__(help=help, docs=docs, key=layer, deprecated=deprecated)


class _Body(dict[str, object]):
    """A class body that refuses a name bound twice where either is a row.

    A row rebound to something that is not one would leave the table
    without it and raise nothing, so the guard reads both sides.
    """

    @override
    def __setitem__(self, name: str, value: object) -> None:
        """Bind one name, raising when a row stands on either side of it."""
        if name in self and (isinstance(value, Row) or isinstance(self[name], Row)):
            msg = f"{name} is declared twice in one table"
            raise ValueError(msg)
        super().__setitem__(name, value)


class _TableMeta(type):
    """The metaclass whose prepared body catches a name declared twice."""

    @override
    @classmethod
    def __prepare__(
        cls, name: str, bases: tuple[type, ...], /, **kwds: object
    ) -> dict[str, object]:
        """Return the mapping the class body writes its names into."""
        return _Body()


class Table(metaclass=_TableMeta):
    """A group of rows that share a command set, a scope and a page.

    The class keywords are the table's defaults, and a row overrides the
    command set with ``on=`` or the page with ``docs=`` where it differs.
    """

    _on: tuple[str, ...] = ()
    _scope: Scope | None = None
    _docs = ""

    @override
    def __init_subclass__(
        cls,
        *,
        on: tuple[str, ...] = (),
        scope: Scope | None = None,
        docs: str = "",
    ) -> None:
        """Apply the table's command set, scope and page to its rows."""
        super().__init_subclass__()
        cls._on = on
        cls._scope = scope
        cls._docs = docs

        for row in cls.__dict__.values():
            if isinstance(row, Row):
                # A key with no command line takes no command set.
                if row.kind:
                    row.on = row.on or on
                row.scope = scope
                row.docs = row.docs or docs


def rows(table: type[Table]) -> list[Row]:
    """Return one table's rows, in declaration order."""
    return [row for row in table.__dict__.values() if isinstance(row, Row)]
