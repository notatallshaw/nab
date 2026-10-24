"""Value and copy contracts shared by explicit record constructors."""

from __future__ import annotations

import copy
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from nab_provider.value import (
    FACTORY,
    FactoryDefault,
    HashableRecord,
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


class HashableData(HashableRecord):
    """A dictionary-backed hashable record."""

    __match_args__ = ("value",)
    value: int

    def __init__(self, value: int) -> None:
        """Retain the integer field."""
        self.value = value


class SlottedData(HashableRecord):
    """A hashable record backed by a slot."""

    __slots__ = __match_args__ = ("value",)
    value: int

    def __init__(self, value: int) -> None:
        """Retain the integer slot."""
        self.value = value


class SelectedData(HashableRecord):
    """Only the key participates in equality, hashing and repr."""

    __slots__ = __record_fields__ = ("key", "payload")
    __match_args__ = __record_compare_fields__ = __record_repr_fields__ = ("key",)
    key: int
    payload: object

    def __init__(self, key: int, payload: object) -> None:
        """Keep the key and an otherwise unrestricted payload."""
        self.key = key
        self.payload = payload


class DerivedData(HashableRecord):
    """A constructor recomputes a derived field when copied."""

    __slots__ = __record_fields__ = ("value", "double")
    __match_args__ = __record_init_fields__ = ("value",)
    value: int
    double: int

    def __init__(self, value: int) -> None:
        """Derive the second slot from the constructor argument."""
        self.value = value
        self.double = value * 2


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
    assert left.__eq__(HashableData(1)) is NotImplemented
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


def test_slotted_values_support_copy_and_pickle() -> None:
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
