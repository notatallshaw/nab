"""Keyed coordinates preserve scalar set laws without enumerating the domain."""

import copy
import os
import pickle
import subprocess
import sys
from itertools import product

import pytest

from nab_resolver.keyed_ranges import KeyedRange
from nab_resolver.ranges import Range
from nab_resolver.types import RangeRelation


def test_coordinate_algebra_matches_membership_and_conflict_resolution() -> None:
    bounds = Range[int].at_least(1)
    ranges = (
        KeyedRange(Range[int].empty()),
        KeyedRange(Range[int].full()),
        KeyedRange(bounds),
        KeyedRange(Range[int].empty(), {"primary": bounds}),
        KeyedRange(bounds, {"backup": Range.singleton(1)}),
    )
    members = tuple(product(("primary", "backup", "unseen"), range(4)))
    assert ranges[0].is_empty
    assert not ranges[1].is_empty
    assert not ranges[3].is_empty
    for left, right in product(ranges, repeat=2):
        assert left.is_subset((left - right) | right)
        assert (left - right).is_disjoint(right)
        assert left | right == right | left
        assert left & right == right & left
        assert ~~left == left
        assert left.is_superset(left & right)

        for key, value in members:
            included_left = left.contains(key, value)
            included_right = right.contains(key, value)
            assert (left & right).contains(key, value) == (
                included_left and included_right
            )
            assert (left | right).contains(key, value) == (
                included_left or included_right
            )
            assert (left - right).contains(key, value) == (
                included_left and not included_right
            )
            assert (~left).contains(key, value) != included_left


def test_normalization_snapshots_unordered_keys_and_uses_last_duplicate() -> None:
    first, second = object(), object()
    default = Range[int].full()
    overrides = {first: Range.singleton(1), second: Range.singleton(2)}
    constraint = KeyedRange(default, overrides)
    overrides.clear()
    reordered = KeyedRange(
        default, [(second, Range.singleton(2)), (first, Range.singleton(1))]
    )
    assert constraint == reordered
    assert hash(constraint) == hash(reordered)
    assert constraint.contains(first, 1)
    assert not constraint.contains(first, 2)
    assert str(constraint)

    assert constraint != KeyedRange(Range.empty(), reordered.overrides)
    assert constraint != KeyedRange(default, {first: Range.singleton(1)})
    assert constraint != KeyedRange(
        default, {first: Range.singleton(2), second: Range.singleton(1)}
    )
    assert constraint != KeyedRange(
        default, {first: Range.singleton(1), object(): Range.singleton(2)}
    )

    redundant = KeyedRange(default, [(first, Range.empty()), (first, default)])
    assert redundant == KeyedRange(default)
    assert hash(redundant) == hash(KeyedRange(default))
    assert redundant.for_key(first) is default


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (KeyedRange(Range.empty()), KeyedRange(Range.full()), RangeRelation.EMPTY),
        (
            KeyedRange(Range.singleton(1)),
            KeyedRange(Range.at_least(1)),
            RangeRelation.SUBSET,
        ),
        (
            KeyedRange(Range.empty(), {"one": Range.singleton(1)}),
            KeyedRange(Range.empty(), {"two": Range.singleton(1)}),
            RangeRelation.DISJOINT,
        ),
        (
            KeyedRange(Range.full(), {"one": Range.empty()}),
            KeyedRange(Range.full(), {"two": Range.empty()}),
            RangeRelation.OVERLAPPING,
        ),
    ],
)
def test_relations_include_the_default_and_both_exception_sets(
    left: KeyedRange[str, int],
    right: KeyedRange[str, int],
    expected: RangeRelation,
) -> None:
    assert left.relation(right) is expected
    assert left.is_subset(right) is expected.is_subset
    assert left.is_disjoint(right) is expected.is_disjoint


def test_immutability_copying_and_unknown_operands() -> None:
    constraint = KeyedRange(Range[int].full(), {"one": Range.singleton(1)})
    copied = copy.copy(constraint)
    assert copied == constraint
    assert copied.default is constraint.default
    assert copied.overrides is constraint.overrides

    for name in ("default", "overrides", "_hash", "extra"):
        with pytest.raises(AttributeError, match="cannot assign to field"):
            setattr(constraint, name, None)
        with pytest.raises(AttributeError, match="cannot delete field"):
            delattr(constraint, name)

    assert constraint.__eq__(object()) is NotImplemented
    assert constraint.__and__(object()) is NotImplemented
    assert constraint.__or__(object()) is NotImplemented
    assert constraint.__sub__(object()) is NotImplemented


class DomainRange(KeyedRange[str, int]):
    """A host-specific range keeps its constructor through algebra."""

    __slots__ = ()


def test_operations_keep_the_host_specialization() -> None:
    first = DomainRange(Range[int].empty(), {"first": Range.singleton(1)})
    second = DomainRange(Range[int].empty(), {"second": Range.singleton(2)})
    for combined in (first & second, first | second, first - second, ~first):
        assert type(combined) is DomainRange


class MetadataRange(KeyedRange[str, int]):
    """Keep slotted host state without rerunning its required constructor."""

    __slots__ = ("metadata",)

    def __init__(self, metadata: list[str]) -> None:
        super().__init__(Range[int].full())
        self.metadata = metadata


def test_copying_a_specialized_range_preserves_host_state() -> None:
    constraint = MetadataRange(["prepared"])
    restored = copy.copy(constraint)
    assert restored == constraint
    assert restored.default is constraint.default
    assert restored.overrides is constraint.overrides
    assert restored.metadata is constraint.metadata

    del restored.metadata
    restored.metadata = ["updated"]


def test_serialization_recomputes_hashes_in_the_receiving_process() -> None:
    constraint = KeyedRange(Range[int].full(), {"one": Range.singleton(1)})
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import pickle, sys; "
                "from nab_resolver.keyed_ranges import KeyedRange; "
                "from nab_resolver.ranges import Range; "
                "restored = pickle.loads(sys.stdin.buffer.read()); "
                "expected = KeyedRange(Range.full(), {'one': Range.singleton(1)}); "
                "assert restored == expected; "
                "assert hash(restored) == hash(expected)"
            ),
        ],
        input=pickle.dumps(constraint),
        env={**os.environ, "PYTHONHASHSEED": "17"},
        check=True,
        capture_output=True,
    )
    assert child.returncode == 0
