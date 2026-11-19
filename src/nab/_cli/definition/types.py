"""Read a row's value shape and label from its type argument.

Generic metadata appears after construction, so the builder reads it.
"""

from __future__ import annotations

__lazy_modules__ = (
    "enum",
    "types",
)

import enum
from pathlib import Path
from types import UnionType
from typing import TYPE_CHECKING, Literal, NewType, Union, get_args, get_origin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .rows import Row

# The four scalars a token converts to, each with the vtype the parser reads
# it under and the word ``nab config explain`` prints for it.
_SCALARS: dict[type, str] = {
    str: "str",
    int: "int",
    bool: "bool",
    Path: "path",
}


class Shape:
    """What one row's type parameter says about its value."""

    __slots__ = ("choices", "label", "nullable", "vtype")

    def __init__(
        self,
        vtype: str,
        choices: tuple[str, ...],
        label: str,
        *,
        nullable: bool,
    ) -> None:
        """Record what one type parameter said."""
        self.vtype = vtype
        self.choices = choices
        self.label = label
        self.nullable = nullable


def type_argument(row: Row) -> object:
    """Return a row's type parameter, or ``None`` on a row with no value."""
    alias = getattr(row, "__orig_class__", None)
    return None if alias is None else get_args(alias)[0]


def shape(annotation: object, where: str = "") -> Shape:
    """Read the vtype, the choice set and the printed label off a type.

    ``where`` names the row in errors about unsupported annotations.
    """
    named = where or "a row"
    arguments = get_args(annotation)
    nullable = get_origin(annotation) in (Union, UnionType) and type(None) in arguments
    inner = annotation
    if nullable:
        alternatives = [arg for arg in arguments if arg is not type(None)]
        if len(alternatives) != 1:
            msg = (
                f"{named} holds {annotation}, and a row reads one type, or one and None"
            )
            raise ValueError(msg)
        inner = alternatives[0]

    # A NewType names the value the row holds, which is the word to print
    # where the base type's own name would say only ``str``.
    label = ""
    if isinstance(inner, NewType):
        label = inner.__name__.lower()
        inner = inner.__supertype__

    if get_origin(inner) is Literal:
        literals = get_args(inner)
        if None in literals:
            nullable = True
            literals = tuple(value for value in literals if value is not None)
        return _choices(literals, named, nullable=nullable)

    if isinstance(inner, type) and issubclass(inner, enum.Enum):
        return _choices((member.value for member in inner), named, nullable=nullable)

    scalar = _SCALARS.get(inner) if isinstance(inner, type) else None
    if scalar is None:
        held = inner.__name__ if isinstance(inner, type) else inner
        msg = f"{named} holds {held}, and a token converts to str, int, bool or Path"
        raise ValueError(msg)

    return Shape(scalar, (), label or scalar, nullable=nullable)


def _choices(values: Iterable[object], name: str, *, nullable: bool) -> Shape:
    """Collect the text tokens a choice field can pass to the command."""
    tokens: list[str] = []
    for value in values:
        if not isinstance(value, str):
            msg = f"{name} has choice {value!r}. CLI choices must be strings."
            raise ValueError(msg)  # noqa: TRY004 - invalid option declaration
        tokens.append(value)
    if not tokens:
        msg = f"{name} must declare at least one string choice"
        raise ValueError(msg)

    choices = tuple(tokens)
    return Shape("choice", choices, enum_label(choices), nullable=nullable)


def enum_label(choices: tuple[str, ...]) -> str:
    """Render the ``enum(a|b)`` type label ``nab config explain`` prints."""
    return f"enum({'|'.join(choices)})"
