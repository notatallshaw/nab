"""The base this package's hand-written value types share.

``@dataclass`` compiles ``__init__``, ``__eq__`` and ``__repr__`` with
``exec`` every time it is applied, and every ``nab`` invocation pays that
at import.  :class:`ValueType` carries the equality, hashing and repr
these types share, leaving each class its own fields and its own
constructor.
"""

from __future__ import annotations

__lazy_modules__ = ("typing",)

from typing import ClassVar

from nab_provider.value import SlottedValue

__all__ = ["ValueType"]


class ValueType(SlottedValue):
    """A value type whose field names live in ``__match_args__``.

    A subclass names its fields once, in declaration order, as both
    ``__slots__`` and ``__match_args__``, annotates them, and assigns them
    in its own ``__init__``.  Equality, hashing and repr read that tuple.
    """

    # Without this a subclass carries a __dict__ alongside its slots.
    __slots__ = ()

    __match_args__: ClassVar[tuple[str, ...]] = ()
