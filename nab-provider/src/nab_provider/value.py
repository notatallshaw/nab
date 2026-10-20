"""Explicit record constructors with shared value, copy and frozen behavior."""

from __future__ import annotations

import dataclasses
from reprlib import recursive_repr
from typing import TYPE_CHECKING, ClassVar, TypeVar

from ._compat import dataclass_transform, override

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "FACTORY",
    "FactoryDefault",
    "Record",
    "SlottedValue",
    "factory_value",
    "field_names",
    "is_record",
    "replace",
]

_T = TypeVar("_T")


class FactoryDefault:
    """The omitted argument of a constructor that allocates its default."""

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        """Render an omitted argument in a constructor signature."""
        return "<factory>"


FACTORY = FactoryDefault()


def factory_value(value: _T | FactoryDefault, factory: Callable[[], _T]) -> _T:
    """Allocate an omitted default, retaining explicitly supplied values."""
    if isinstance(value, FactoryDefault):
        return factory()
    return value


@dataclass_transform(field_specifiers=(dataclasses.field,))
class Record:  # noqa: PLW1641 - Frozen subclasses install their hash method.
    """A value whose constructor and field names are written by its owner."""

    __slots__ = ()
    __record_fields__: ClassVar[tuple[str, ...] | None] = None
    __match_args__: ClassVar[tuple[str, ...]] = ()
    __record_compare_fields__: ClassVar[tuple[str, ...] | None] = None
    __record_repr_fields__: ClassVar[tuple[str, ...] | None] = None
    __record_init_fields__: ClassVar[tuple[str, ...] | None] = None

    @override
    def __init_subclass__(cls, *, frozen: bool = False) -> None:
        """Install shared refusal and state methods on frozen records."""
        super().__init_subclass__()
        if frozen:
            # These assignments also update Python's special-method slots.
            methods: dict[str, object] = {
                "__setattr__": _frozen_setattr,
                "__delattr__": _frozen_delattr,
                "__hash__": _record_hash,
            }
            if "__slots__" in cls.__dict__:
                methods.update(
                    __getstate__=_record_getstate, __setstate__=_record_setstate
                )
            for name, method in methods.items():
                setattr(cls, name, method)

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


def _field_owner(value: Record) -> type[object]:
    """Find the declaration supplying an instance's record fields."""
    return next(
        parent
        for parent in type(value).__mro__
        if "__record_fields__" in parent.__dict__ or "__match_args__" in parent.__dict__
    )


def _frozen_setattr(self: Record, name: str, value: object) -> None:
    if type(self) is _field_owner(self) or name in field_names(self):
        msg = f"cannot assign to field {name!r}"
        raise dataclasses.FrozenInstanceError(msg)
    object.__setattr__(self, name, value)


def _frozen_delattr(self: Record, name: str) -> None:
    if type(self) is _field_owner(self) or name in field_names(self):
        msg = f"cannot delete field {name!r}"
        raise dataclasses.FrozenInstanceError(msg)
    object.__delattr__(self, name)


def _record_hash(self: Record) -> int:
    return hash(self._comparison_values())


def _record_getstate(self: Record) -> list[object]:
    return [getattr(self, name) for name in field_names(self)]


def _record_setstate(self: Record, state: Sequence[object]) -> None:
    for name, value in zip(field_names(self), state, strict=False):
        object.__setattr__(self, name, value)


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


def replace(value: _T, /, **changes: object) -> _T:
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
    constructor: Callable[..., _T] = type(value)
    return constructor(**changes)


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
