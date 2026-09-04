"""Equality, hashing and repr for explicit value types."""

from __future__ import annotations

from ._compat import override

__all__ = ["SlottedValue"]


class SlottedValue:
    """Equality, hashing and repr over a subclass's declared fields.

    ``__match_args__`` sets field order. Equality checks the exact class.
    Subclasses using ``cached_property`` retain a ``__dict__``.
    """

    __slots__ = ()
    __match_args__: tuple[str, ...]

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
