"""Bind table defaults to declared rows and preserve declaration order."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..._compat import override
from .rows import Row

if TYPE_CHECKING:
    from .model import Scope


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
    ``under`` names the configuration key the rows spell one key each of,
    and ``needs`` the ones a command line has to give when it spells any
    and no file declares the table.
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
        under: str = "",
        needs: tuple[str, ...] = (),
    ) -> None:
        """Apply the table's command set, scope, page and parent key to its rows."""
        super().__init_subclass__()
        cls._on = on
        cls._scope = scope
        cls._docs = docs

        if needs and not under:
            msg = f"{cls.__name__} declares needs without under"
            raise ValueError(msg)

        declared = rows(cls)
        names = {row.name for row in declared}
        unknown = [name for name in needs if name not in names]
        if unknown:
            msg = f"{cls.__name__} needs {unknown[0]!r}, which it does not declare"
            raise ValueError(msg)

        for row in declared:
            # File-only keys take no command set.
            if row.kind:
                row.on = row.on or on
            row.scope = scope
            row.docs = row.docs or docs
            row.under = under
            row.needed = row.name in needs


def rows(table: type[Table]) -> list[Row]:
    """Return one table's rows, in declaration order."""
    return [row for row in table.__dict__.values() if isinstance(row, Row)]
