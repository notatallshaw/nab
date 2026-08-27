"""The shared base under the package's declared-source value types.

Those types are written out by hand instead of declared with
``@dataclass(slots=True)``, because applying the decorator is import-time work
every ``nab`` invocation pays for.  The comparison, hash and repr all eight
share live here; each type declares its own fields and constructor.
"""

from __future__ import annotations

from typing_extensions import override

__all__ = ["SlottedValue"]


class SlottedValue:
    """Comparison, hashing and repr over a subclass's declared fields.

    A subclass lists its fields in ``__slots__`` and repeats them, in
    declaration order, in ``__match_args__``, which is the order read here.
    Comparison tests the exact class, so a subclass holding equal field
    values is not equal.
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
