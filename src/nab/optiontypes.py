"""Read what a row's type parameter says about the value it holds.

``typing`` writes ``__orig_class__`` on the instance a subscripted generic
builds, so the parameter a checker reads statically is the same object read
here.  It is written after ``__init__`` returns, which is why a row cannot
judge its own type as it is built and :mod:`nab.optionlower` does it later.

A row that reads a value has to be written subscripted.  Inference gives a
checker the parameter and leaves nothing behind at run time.

The imports here are ``enum``, ``pathlib`` and ``typing``, but the four
vtype spellings and the ``enum(a|b)`` label format below are nab's, so the
module is a reader of types rather than a neutral one.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

# The four scalars a token converts to, each with the vtype the parser reads
# it under and the word ``nab config explain`` prints for it.
_SCALARS: dict[Any, tuple[str, str]] = {
    str: ("str", "str"),
    int: ("int", "int"),
    bool: ("bool", "bool"),
    Path: ("path", "path"),
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


def type_argument(row: object) -> Any:
    """Return a row's type parameter, or ``None`` on a row with no value."""
    alias = getattr(row, "__orig_class__", None)
    return None if alias is None else get_args(alias)[0]


def shape(annotation: Any, where: str = "") -> Shape:
    """Read the vtype, the choice set and the printed label off a type.

    ``where`` names the row in the two refusals below: a type parameter a
    checker accepts and this reader has no vtype for.
    """
    named = where or "a row"
    arguments = get_args(annotation)
    nullable = type(None) in arguments
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
    if hasattr(inner, "__supertype__"):
        label = inner.__name__.lower()
        inner = inner.__supertype__

    if get_origin(inner) is Literal:
        tokens = tuple(str(arg) for arg in get_args(inner))
        return Shape("choice", tokens, enum_label(tokens), nullable=nullable)

    if isinstance(inner, type) and issubclass(inner, enum.Enum):
        tokens = tuple(str(member.value) for member in inner)
        return Shape("choice", tokens, enum_label(tokens), nullable=nullable)

    scalar = _SCALARS.get(inner)
    if scalar is None:
        held = getattr(inner, "__name__", inner)
        msg = f"{named} holds {held}, and a token converts to str, int, bool or Path"
        raise ValueError(msg)

    vtype, name = scalar
    return Shape(vtype, (), label or name, nullable=nullable)


def enum_label(choices: tuple[str, ...]) -> str:
    """Render the ``enum(a|b)`` type label ``nab config explain`` prints."""
    return f"enum({'|'.join(choices)})"
