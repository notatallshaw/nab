"""The base this package's hand-written value types share.

``@dataclass`` compiles ``__init__``, ``__eq__`` and ``__repr__`` with
``exec`` every time it is applied, and every ``nab`` invocation pays that
at import.  :class:`ValueType` carries the equality, hashing and repr
these types share, leaving each class its own fields and its own
constructor.
"""

from __future__ import annotations

from typing import ClassVar

from typing_extensions import override

__all__ = ["ValueType"]


class ValueType:
    """A value type whose field names live in ``__match_args__``.

    A subclass names its fields once, in declaration order, as both
    ``__slots__`` and ``__match_args__``, annotates them, and assigns them
    in its own ``__init__``.  Equality, hashing and repr read that tuple.
    """

    # Without this a subclass carries a __dict__ alongside its slots.
    __slots__ = ()

    __match_args__: ClassVar[tuple[str, ...]] = ()

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
        """Return the qualified class name and every field, in declaration order."""
        fields = ", ".join(
            f"{name}={getattr(self, name)!r}" for name in self.__match_args__
        )
        return f"{type(self).__qualname__}({fields})"
