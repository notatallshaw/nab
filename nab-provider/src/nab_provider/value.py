"""Explicit record constructors with shared comparison, repr and copying."""

from __future__ import annotations

import dataclasses
from reprlib import recursive_repr
from typing import TYPE_CHECKING, ClassVar, TypeVar, overload

from ._compat import override

if TYPE_CHECKING:
    from collections.abc import Callable

    from _typeshed import DataclassInstance

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

_Value = TypeVar("_Value")
_RecordValue = TypeVar("_RecordValue", bound="Record")
_DataclassValue = TypeVar("_DataclassValue", bound="DataclassInstance")


class FactoryDefault:
    """The omitted argument of a constructor that allocates its default."""

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        """Render an omitted argument in a constructor signature."""
        return "<factory>"


FACTORY = FactoryDefault()


def factory_value(
    value: _Value | FactoryDefault, factory: Callable[[], _Value]
) -> _Value:
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


def _derived_field_error(name: str) -> ValueError:
    return ValueError(
        f"field {name} is declared with init=False, "
        "it cannot be specified with replace()"
    )


@overload
def replace(value: _RecordValue, /, **changes: object) -> _RecordValue: ...


@overload
def replace(value: _DataclassValue, /, **changes: object) -> _DataclassValue: ...


def replace(value: object, /, **changes: object) -> object:
    """Reconstruct a record, retaining omitted constructor arguments."""
    if not isinstance(value, Record):
        if not dataclasses.is_dataclass(value) or isinstance(value, type):
            msg = "replace() requires a record instance"
            raise TypeError(msg)

        # Preserve Nab's ValueError for derived fields across Python versions.
        if changes:
            for field in dataclasses.fields(value):
                if not field.init and field.name in changes:
                    raise _derived_field_error(field.name)

        # Zuban requires a concrete dataclass even after the instance check.
        return dataclasses.replace(value, **changes)  # zuban: ignore[misc]

    init_names = value.__record_init_fields__
    for name in field_names(value):
        if init_names is not None and name not in init_names:
            if name in changes:
                raise _derived_field_error(name)
        elif name not in changes:
            changes[name] = getattr(value, name)

    return type(value)(**changes)


class SlottedValue:
    """Equality, hashing and repr over a subclass's declared fields.

    ``__match_args__`` sets field order. Equality checks the exact class.
    Subclasses using ``cached_property`` retain a ``__dict__``.
    """

    __slots__ = ()
    __match_args__: ClassVar[tuple[str, ...]]

    @override
    def __eq__(self, other: object) -> bool:
        """Compare every field against an instance of the same class."""
        if other.__class__ is not self.__class__:
            return NotImplemented

        names = self.__match_args__
        return tuple(getattr(self, name) for name in names) == tuple(
            getattr(other, name) for name in names
        )

    @override
    def __hash__(self) -> int:
        """Hash every field together."""
        return hash(tuple(getattr(self, name) for name in self.__match_args__))

    @override
    def __repr__(self) -> str:
        """Return the class name and every field, in declaration order."""
        fields = ", ".join(
            f"{name}={getattr(self, name)!r}" for name in self.__match_args__
        )
        return f"{type(self).__qualname__}({fields})"
