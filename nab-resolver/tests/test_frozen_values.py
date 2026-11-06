"""Immutable identities retain ordinary field, subclass and copy behavior."""

import copy
import pickle
import weakref
from typing import ClassVar

import pytest

from nab_resolver._compat import override
from nab_resolver.values import FrozenValue


class Point(FrozenValue):
    """Two immutable coordinates with ordinary construction."""

    __slots__ = __match_args__ = ("x", "y")
    x: int
    y: int

    def __init__(self, x: int, y: int) -> None:
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)


class LabeledPoint(Point):
    """Keep extra dictionary state outside the point's identity."""

    initializations: ClassVar[int] = 0

    def __init__(self, x: int, y: int, label: list[str]) -> None:
        super().__init__(x, y)
        self.label = label
        type(self).initializations += 1


class SlottedPoint(Point):
    """Keep extra slotted state while leaving one slot uninitialized."""

    __slots__ = ("label", "optional")
    initializations: ClassVar[int] = 0

    def __init__(self, x: int, y: int, label: list[str]) -> None:
        super().__init__(x, y)
        self.label = label
        type(self).initializations += 1


class ShadowPoint(Point):
    """Replace an inherited field with a distinct slot."""

    __slots__ = ("x",)


class PropertyPoint(Point):
    """Retain a field stored through a visible property."""

    writes: ClassVar[int] = 0
    storage_name = "x"

    @property
    @override
    def x(self) -> int:
        value: object = self.__dict__[self.storage_name]
        assert isinstance(value, int)
        return value

    @x.setter
    @override
    def x(self, value: int) -> None:  # pyright: ignore[reportIncompatibleVariableOverride] - Exercise a property replacing an inherited slot.
        self.__dict__[self.storage_name] = value
        type(self).writes += 1


class PrivatePropertyPoint(PropertyPoint):
    """Store the property under a different dictionary key."""

    storage_name = "_x"


def test_identity_fields_freeze_without_a_dictionary_and_remain_weakrefable() -> None:
    point = Point(1, 2)
    assert not hasattr(point, "__dict__")
    assert weakref.ref(point)() is point
    assert point == Point(1, 2)
    assert point != Point(2, 1)
    assert point != ShadowPoint(1, 2)
    assert point.__eq__(object()) is NotImplemented
    assert point.__eq__(ShadowPoint(1, 2)) is NotImplemented
    assert hash(point) == hash(Point(1, 2))
    assert repr(point) == "Point(x=1, y=2)"

    for name in ("x", "y", "extra"):
        with pytest.raises(AttributeError, match="cannot assign to field"):
            setattr(point, name, None)
        with pytest.raises(AttributeError, match="cannot delete field"):
            delattr(point, name)


@pytest.mark.parametrize("value_type", [Point, ShadowPoint])
def test_base_and_shadowed_slots_support_every_pickle_protocol(
    value_type: type[Point],
) -> None:
    point = value_type(1, 2)
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        restored = pickle.loads(pickle.dumps(point, protocol))  # noqa: S301 - local roundtrip
        assert restored == point
        assert hash(restored) == hash(point)


@pytest.mark.parametrize("value_type", [LabeledPoint, SlottedPoint])
def test_subclass_state_is_copied_without_reconstructing(
    value_type: type[LabeledPoint] | type[SlottedPoint],
) -> None:
    value_type.initializations = 0
    point = value_type(1, 2, ["label"])
    for restored in (copy.copy(point), copy.deepcopy(point)):
        assert restored == point
        assert restored.label == point.label
        assert value_type.initializations == 1

    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        restored = pickle.loads(pickle.dumps(point, protocol))  # noqa: S301 - local roundtrip
        assert restored == point
        assert restored.label == point.label
        assert value_type.initializations == 1

    del point.label
    point.label = ["replacement"]
    with pytest.raises(AttributeError, match="cannot assign to field"):
        point.x = 3
    with pytest.raises(AttributeError, match="cannot delete field"):
        del point.y


@pytest.mark.parametrize("value_type", [PropertyPoint, PrivatePropertyPoint])
def test_property_state_restores_without_replaying_setters(
    value_type: type[PropertyPoint],
) -> None:
    value_type.writes = 0
    point = value_type(1, 2)
    assert value_type.writes == 1
    for restored in (copy.copy(point), copy.deepcopy(point)):
        assert restored == point
        assert restored.x == 1
        assert value_type.writes == 1


class IdentityPoint(Point):
    """Allow a domain to refine identity beyond ordinary field equality."""

    @override
    def identity(self) -> tuple[int]:
        return (self.x,)


def test_identity_override_controls_both_equality_and_hashing() -> None:
    first = IdentityPoint(1, 2)
    second = IdentityPoint(1, 3)
    assert first == second
    assert hash(first) == hash(second)
    assert first != IdentityPoint(2, 2)
