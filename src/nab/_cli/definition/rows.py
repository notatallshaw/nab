"""Define the typed row classes used by :mod:`nab._cli.definition.options`.

Descriptor overloads exist only during type checking. The builder reads the row
objects on each table class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ..._compat import override

if TYPE_CHECKING:
    import enum
    from collections.abc import Callable

    from .model import Scope

T = TypeVar("T")
C = TypeVar("C")

# The marker for a field the row does not write.  It is typed Any so an
# omitted default checks against any T while a written one is checked;
# nab._cli.definition.build turns it into the Opt sentinel or into None.
OMITTED: Any = object()


class Layer(Generic[C]):
    """Configuration hooks and a built-in default of type ``C``.

    The type parameter checks the hooks and default; the builder does
    not read it. ``sample`` supplies a token for free-form values, and
    ``label`` overrides the inferred display type.
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
        "needed",
        "negatable",
        "on",
        "opened_by",
        "required",
        "scope",
        "short",
        "under",
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
        self.opened_by = ""
        self.under = ""
        self.needed = False

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


class Items(Star[T]):
    """A token run read as a list of tables: a bare token opens one.

    ``opened_by`` is the item's identifying key, so a bare token is
    shorthand for ``<opened_by>=<token>``; writing that key out opens an
    item too, which is how an id containing ``=`` is given.  Every other
    ``KEY=VALUE`` token sets a key on the item before it.
    """

    __slots__ = ()

    def __init__(
        self,
        *,
        help: str,  # noqa: A002
        opened_by: str,
        docs: str = "",
        on: tuple[str, ...] = (),
    ) -> None:
        """Record one row; the table names it and fills its defaults."""
        super().__init__(help=help, docs=docs, on=on)
        self.opened_by = opened_by


class Item(Items[T]):
    """A token run read as one table rather than a list of them.

    A second bare token would open a second item on a key that holds one,
    so it is refused rather than dropped.
    """

    __slots__ = ()


class Pairs(Star[T]):
    """A token run read as one free-key table: every token is ``KEY=VALUE``."""

    __slots__ = ()


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
