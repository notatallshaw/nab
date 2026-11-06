"""Immutable slotted values with explicit field and identity declarations."""

from __future__ import annotations

from types import MemberDescriptorType
from typing import Any, ClassVar

from ._compat import override

__all__ = ["FrozenValue", "SlottedValue"]


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


class FrozenValue(SlottedValue):
    """Snapshot fields named by ``__match_args__`` for hashable value semantics.

    Constructors initialize their own slots with ``object.__setattr__``.
    Override :meth:`identity` when field equality alone loses a domain identity.
    Subclasses that inherit the fields may retain additional mutable state.
    """

    __slots__ = ("__weakref__",)
    __match_args__: ClassVar[tuple[str, ...]]

    @override
    def __setattr__(self, name: str, value: object) -> None:
        """Refuse changes to the declared identity fields."""
        if "__match_args__" in type(self).__dict__ or name in self.__match_args__:
            message = f"cannot assign to field {name!r}"
            raise AttributeError(message)
        object.__setattr__(self, name, value)

    @override
    def __delattr__(self, name: str) -> None:
        """Refuse deletion of the declared identity fields."""
        if "__match_args__" in type(self).__dict__ or name in self.__match_args__:
            message = f"cannot delete field {name!r}"
            raise AttributeError(message)
        object.__delattr__(self, name)

    def __getstate__(
        self,
    ) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
        """Collect fields and subclass state without copying contained values."""
        dictionary = getattr(self, "__dict__", {}).copy()
        slots: dict[str, Any] = {}
        seen = set()
        for cls in type(self).__mro__:
            for name, descriptor in cls.__dict__.items():
                if not isinstance(descriptor, MemberDescriptorType) or name in seen:
                    continue
                try:
                    value = descriptor.__get__(self)
                except AttributeError:
                    continue
                seen.add(name)
                if name in self.__match_args__:
                    dictionary[name] = value
                else:
                    slots[name] = value
        return (dictionary, slots) if slots else dictionary

    def __setstate__(
        self, state: dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """Restore fields and subclass state without running their constructors."""
        dictionary, slots = state if isinstance(state, tuple) else (state, {})
        extra = dictionary.copy()
        for name in self.__match_args__:
            if name in extra and name not in slots:
                descriptor = _attribute_slot(type(self), name)
                if descriptor is not None:
                    descriptor.__set__(self, extra.pop(name))
        if extra:
            self.__dict__.update(extra)
        for name, value in slots.items():
            setattr(self, name, value)


def _attribute_slot(cls: type, name: str) -> MemberDescriptorType | None:
    """Find the visible slot without invoking a subclass property."""
    descriptor = next(
        base.__dict__[name] for base in cls.__mro__ if name in base.__dict__
    )
    return descriptor if isinstance(descriptor, MemberDescriptorType) else None
