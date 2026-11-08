"""Explicit record constructors with shared comparison, repr and copying."""

from __future__ import annotations

import dataclasses
from reprlib import recursive_repr
from typing import TYPE_CHECKING, ClassVar, TypeVar

from nab_resolver.values import SlottedValue

from ._compat import override

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "FACTORY",
    "FactoryDefault",
    "HashableRecord",
    "Record",
    "SlottedValue",
    "factory_value",
    "field_names",
    "is_record",
    "replace",
]

ValueType = TypeVar("ValueType")


class FactoryDefault:
    """The omitted argument of a constructor that allocates its default."""

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        """Render an omitted argument in a constructor signature."""
        return "<factory>"


FACTORY = FactoryDefault()


def factory_value(
    value: ValueType | FactoryDefault, factory: Callable[[], ValueType]
) -> ValueType:
    """Allocate an omitted default, retaining explicitly supplied values."""
    if isinstance(value, FactoryDefault):
        return factory()
    return value


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
            names = field_names(self)
        return tuple(getattr(self, name) for name in names)

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
            names = field_names(self)
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


def is_record(value: object) -> bool:
    """Recognize explicit records and standard dataclass instances or types."""
    return (
        isinstance(value, Record)
        or (isinstance(value, type) and issubclass(value, Record))
        or dataclasses.is_dataclass(value)
    )


def field_names(value: object) -> tuple[str, ...]:
    """Return a record's declared data fields in order."""
    if isinstance(value, Record) or (
        isinstance(value, type) and issubclass(value, Record)
    ):
        return (
            value.__match_args__
            if value.__record_fields__ is None
            else value.__record_fields__
        )
    if dataclasses.is_dataclass(value):
        return tuple(field.name for field in dataclasses.fields(value))
    msg = "expected a record type or instance"
    raise TypeError(msg)


def replace(value: ValueType, /, **changes: object) -> ValueType:
    """Reconstruct a record, retaining omitted constructor arguments."""
    if isinstance(value, Record):
        names = field_names(value)
        init_names = value.__record_init_fields__
        if init_names is None:
            init_names = names
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        names = tuple(field.name for field in dataclasses.fields(value))
        init_names = tuple(
            field.name for field in dataclasses.fields(value) if field.init
        )
    else:
        msg = "replace() requires a record instance"
        raise TypeError(msg)

    for name in names:
        if name not in init_names:
            if name in changes:
                msg = (
                    f"field {name} is declared with init=False, "
                    "it cannot be specified with replace()"
                )
                raise ValueError(msg)
        elif name not in changes:
            changes[name] = getattr(value, name)

    constructor: Callable[..., ValueType] = type(value)
    return constructor(**changes)
