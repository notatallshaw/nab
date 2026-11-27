"""Field-based equality and hashing for candidate identities and metadata records."""

from __future__ import annotations

from reprlib import recursive_repr
from typing import ClassVar

from ._compat import override

__all__ = ["HashableRecord", "Record", "record_fields"]


class Record:  # noqa: PLW1641 - Mutable records are intentionally unhashable.
    """A value whose constructor and field names are written by its owner."""

    __slots__ = ()
    __record_fields__: ClassVar[tuple[str, ...] | None] = None
    __match_args__: ClassVar[tuple[str, ...]] = ()
    __record_compare_fields__: ClassVar[tuple[str, ...] | None] = None
    __record_repr_fields__: ClassVar[tuple[str, ...] | None] = None
    __record_init_fields__: ClassVar[tuple[str, ...] | None] = None

    def _comparison_values(self) -> tuple[object, ...]:
        """Read the fields participating in equality and hashing."""
        names = self.__record_compare_fields__
        if names is None:
            names = self.__record_fields__
            if names is None:
                names = self.__match_args__

        return tuple([getattr(self, name) for name in names])

    @override
    def __eq__(self, other: object) -> bool:
        """Compare same-type records by their selected fields."""
        if not isinstance(other, Record) or type(other) is not type(self):
            return NotImplemented

        return self._comparison_values() == other._comparison_values()

    @override
    @recursive_repr()
    def __repr__(self) -> str:
        """Render the record's visible fields."""
        names = self.__record_repr_fields__
        if names is None:
            names = record_fields(self)
        values = ", ".join(f"{name}={getattr(self, name)!r}" for name in names)
        return f"{type(self).__qualname__}({values})"


class HashableRecord(Record):
    """A record whose comparison fields support hashing.

    Owners declare fixed fields with ``Final``; no runtime write guard is installed.
    """

    __slots__ = ()

    @override
    def __hash__(self) -> int:
        """Hash the fields used by equality."""
        return hash(self._comparison_values())


def record_fields(value: Record | type[Record]) -> tuple[str, ...]:
    """Return the data fields declared by a record in constructor order."""
    return (
        value.__match_args__
        if value.__record_fields__ is None
        else value.__record_fields__
    )
