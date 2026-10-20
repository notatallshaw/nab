"""Value, copy and refusal contracts shared by explicit record constructors."""

from __future__ import annotations

import copy
import importlib
import pickle
import typing
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass, field

import pytest

from nab_provider import _compat
from nab_provider.value import (
    FACTORY,
    FactoryDefault,
    Record,
    factory_value,
    field_names,
    is_record,
    replace,
)


class MutableData(Record):
    """A mutable record with a defaulted integer field."""

    __match_args__ = ("value",)
    value: int

    def __init__(self, value: int = 0) -> None:
        """Retain the supplied integer."""
        self.value = value


class FrozenData(Record, frozen=True):
    """A dictionary-backed frozen record."""

    __match_args__ = ("value",)
    value: int

    def __init__(self, value: int) -> None:
        """Initialize the frozen field."""
        object.__setattr__(self, "value", value)


class FrozenChild(FrozenData, frozen=True):
    """A subclass without a new record declaration."""


class SlottedData(Record, frozen=True):
    """A frozen record whose pickle state contains only declared slots."""

    __slots__ = __match_args__ = ("value",)
    value: int

    def __init__(self, value: int) -> None:
        """Initialize the frozen slot."""
        object.__setattr__(self, "value", value)


class SelectedData(Record, frozen=True):
    """Only the key participates in equality, hashing and repr."""

    __slots__ = __record_fields__ = ("key", "payload")
    __match_args__ = __record_compare_fields__ = __record_repr_fields__ = ("key",)
    key: int
    payload: object

    def __init__(self, key: int, payload: object) -> None:
        """Keep the key and an otherwise unrestricted payload."""
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "payload", payload)


class DerivedData(Record, frozen=True):
    """A constructor recomputes a derived field when copied."""

    __slots__ = __record_fields__ = ("value", "double")
    __match_args__ = __record_init_fields__ = ("value",)
    value: int
    double: int

    def __init__(self, value: int) -> None:
        """Derive the second slot from the constructor argument."""
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "double", value * 2)


class RecursiveData(Record):
    """A mutable payload may refer to its own record."""

    __match_args__ = ("value",)
    value: object

    def __init__(self, value: object) -> None:
        """Retain a payload without copying it."""
        self.value = value


@dataclass
class StandardData:
    """An existing dataclass remains usable with the copy helper."""

    value: int
    double: int = field(init=False)

    def __post_init__(self) -> None:
        """Compute the field excluded from construction."""
        self.double = self.value * 2


def test_mutable_values_compare_by_exact_type_and_stay_unhashable() -> None:
    left = MutableData(1)
    assert left == MutableData(1)
    assert left != MutableData(2)
    assert left.__eq__(FrozenData(1)) is NotImplemented
    assert left.__eq__(1) is NotImplemented
    with pytest.raises(TypeError):
        hash(left)
    left.value = 2
    assert left == MutableData(2)


def test_comparison_and_repr_can_exclude_non_key_payloads() -> None:
    left = SelectedData(1, [])
    right = SelectedData(1, {})
    assert left == right
    assert hash(left) == hash(right)
    assert left != SelectedData(2, [])
    assert repr(left) == "SelectedData(key=1)"
    assert field_names(left) == ("key", "payload")


@pytest.mark.parametrize(
    ("constructor", "name"),
    [(FrozenData, "value"), (FrozenData, "extra"), (FrozenChild, "value")],
)
def test_frozen_record_refuses_assignment_and_deletion(
    constructor: type[FrozenData], name: str
) -> None:
    value = constructor(1)
    with pytest.raises(FrozenInstanceError):
        setattr(value, name, 2)
    with pytest.raises(FrozenInstanceError):
        delattr(value, name)


def test_plain_subclass_can_manage_its_extra_attributes() -> None:
    value = FrozenChild(1)
    name = "extra"
    setattr(value, name, 2)
    assert getattr(value, name) == 2
    delattr(value, name)


def test_slotted_frozen_values_support_copy_and_pickle() -> None:
    value = SlottedData(3)
    assert copy.copy(value) == value
    assert copy.deepcopy(value) == value
    assert pickle.loads(pickle.dumps(value)) == value  # noqa: S301 - locally created test data.
    assert repr(value) == "SlottedData(value=3)"


def test_repr_stops_at_a_recursive_record() -> None:
    value = RecursiveData(None)
    value.value = value
    assert repr(value) == "RecursiveData(value=...)"


@pytest.mark.parametrize("constructor", [DerivedData, StandardData])
def test_replace_recomputes_derived_fields(
    constructor: type[DerivedData | StandardData],
) -> None:
    value = constructor(2)
    assert replace(value) == value
    updated = replace(value, value=5)
    assert updated.value == 5
    assert updated.double == 10
    with pytest.raises(ValueError, match="init=False"):
        replace(value, double=10)
    with pytest.raises(TypeError):
        replace(value, unknown=10)


@pytest.mark.parametrize(
    "value", [MutableData, StandardData, MutableData(), StandardData(1)]
)
def test_field_names_and_recognition_support_both_kinds(value: object) -> None:
    assert is_record(value)
    assert field_names(value)[0] == "value"


@pytest.mark.parametrize("value", [object(), object, 1])
def test_non_records_are_refused(value: object) -> None:
    assert not is_record(value)
    with pytest.raises(TypeError):
        field_names(value)
    with pytest.raises(TypeError):
        replace(value)


@pytest.mark.parametrize("value", [MutableData, StandardData])
def test_copy_requires_an_instance(value: type[object]) -> None:
    with pytest.raises(TypeError):
        replace(value)


def test_factories_allocate_only_omitted_values() -> None:
    first = factory_value(FACTORY, list[int])
    second = factory_value(FACTORY, list[int])
    assert first == second == []
    assert first is not second
    assert factory_value(first, list[int]) is first
    assert repr(FactoryDefault()) == "<factory>"


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (dict[str, int], {}),
        (tuple[int, ...], ()),
        (frozenset[str], frozenset()),
        (lambda: {"seed": 1}, {"seed": 1}),
    ],
)
def test_factories_call_the_supplied_factory(
    factory: Callable[[], object], expected: object
) -> None:
    assert factory_value(FACTORY, factory) == expected


def test_typing_decorator_fallback_leaves_runtime_classes_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.delattr(typing, "dataclass_transform", raising=False)
        importlib.reload(_compat)
        transform = _compat.dataclass_transform(frozen_default=True)
        assert transform(MutableData) is MutableData
    importlib.reload(_compat)
