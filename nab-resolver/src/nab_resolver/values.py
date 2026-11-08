"""Immutable slotted values with explicit field and identity declarations."""

from __future__ import annotations

from typing import ClassVar

from ._compat import override

__all__ = ["SlottedValue"]


class SlottedValue:
    """Equality, hashing and repr over a subclass's declared immutable identity.

    ``__match_args__`` declares field order. Override :meth:`identity` to refine
    field equality. This class installs no runtime write guard.
    """

    __slots__ = ()
    __match_args__: ClassVar[tuple[str, ...]]

    def identity(self) -> tuple[object, ...]:
        """Return the immutable values used for equality and hashing."""
        return tuple(getattr(self, name) for name in self.__match_args__)

    @override
    def __eq__(self, other: object) -> bool:
        """Compare identities within the exact value class."""
        if not isinstance(other, SlottedValue) or type(other) is not type(self):
            return NotImplemented

        return self.identity() == other.identity()

    @override
    def __hash__(self) -> int:
        """Hash the identity used by equality."""
        return hash(self.identity())

    @override
    def __repr__(self) -> str:
        """Render the declared fields in their construction order."""
        fields = ", ".join(
            f"{name}={getattr(self, name)!r}" for name in self.__match_args__
        )
        return f"{type(self).__qualname__}({fields})"
