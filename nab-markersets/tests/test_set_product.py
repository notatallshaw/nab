"""Global set-product refusals preserve ordered guards and admitted cells."""

from __future__ import annotations

from collections.abc import Iterable
from math import prod

import pytest

from nab_markersets import _markersets as engine
from nab_markersets._compat import get_int_max_str_digits
from nab_markersets.errors import IntractableMarkerSet


@pytest.fixture
def subsets(monkeypatch: pytest.MonkeyPatch) -> list[frozenset[str]]:
    """Observe actual powerset allocations without replacing their values."""
    seen: list[frozenset[str]] = []

    def observed(elements: Iterable[str]) -> frozenset[str]:
        result = frozenset(elements)
        seen.append(result)
        return result

    monkeypatch.setattr(engine, "frozenset", observed, raising=False)
    return seen


@pytest.mark.parametrize(
    "text",
    [
        '"a" in extras',
        '"a" in extras or "a" not in extras or "b" in extras',
        '"a" in extras and "b" in dependency_groups',
        '"a" in extras and sys_platform == "linux"',
        'sys_platform == "linux" and "a" in extras',
        '"a" in extras and python_full_version < "3.12"',
        '"a" in extras and "tag" in platform_release',
    ],
)
def test_admitted_partitions_and_witness_order_match_eager_preparation(
    text: str,
) -> None:
    node = engine.parse(text)
    atoms = engine.collect_atoms(node)
    grouped = engine._atoms_by_axis(atoms)
    expected = [
        engine.partition_axis(axis, group, 100_000, engine.Memo())
        for axis, group in grouped.items()
    ]
    space = engine._cell_space(node, 100_000, engine.Memo())
    assert space.axes == list(grouped)
    assert space.atomlists == list(grouped.values())
    assert space.partitions == expected
    assert space.units == prod(map(len, expected)) * len(atoms)

    eager = engine._CellSpace(space.axes, space.atomlists, expected, space.units)
    assert list(engine._enumerate_cells(node, space)) == list(
        engine._enumerate_cells(node, eager)
    )


@pytest.mark.parametrize("max_cells", [7, 8])
def test_product_boundary_defers_only_refused_subsets(
    monkeypatch: pytest.MonkeyPatch,
    subsets: list[frozenset[str]],
    max_cells: int,
) -> None:
    node = engine.parse('"a" in extras and sys_platform == "linux"')
    store = engine.Memo()
    monkeypatch.setattr(engine._work_meter, "remaining", 100, raising=False)
    if max_cells == 7:
        with pytest.raises(IntractableMarkerSet, match="cell product exceeds"):
            engine._cell_space(node, max_cells, store)
        assert subsets == []
        assert engine._work_meter.remaining == 100
        assert all(key[0][0] != engine.AXIS_SET for key in store.partitions)
    else:
        space = engine._cell_space(node, max_cells, store)
        assert space.units == 8
        assert subsets == [frozenset(), frozenset({"a"})]
        assert engine._work_meter.remaining == 92
        assert len(store.partitions) == 2


@pytest.mark.parametrize("max_cells", [17, 18])
def test_product_counts_repeated_leaf_occurrences(
    subsets: list[frozenset[str]], max_cells: int
) -> None:
    leaf = engine.parse('"a" in extras')
    node = engine.AndNode((leaf,) * 9)
    if max_cells == 17:
        with pytest.raises(IntractableMarkerSet, match="cell product exceeds"):
            engine._cell_space(node, max_cells, engine.Memo())
        assert subsets == []
    else:
        assert engine._cell_space(node, max_cells, engine.Memo()).units == 18
        assert len(subsets) == 2


@pytest.mark.parametrize(
    ("text", "max_cells", "error"),
    [
        ('"a" in extras and "b" in extras', 3, "set powerset over 2 names"),
        ('"a" in extras and "a" not in extras', 3, "axis work 2x2 atoms"),
        ('"a" in extras and sys_platform in "abcdefghij"', 8, "substring enumeration"),
        (
            '"a" in extras and "b" in dependency_groups and "c" in dependency_groups',
            3,
            "set powerset over 2 names",
        ),
        (
            '"a" in extras and "a" not in extras and sys_platform in "abcdefghij"',
            3,
            "axis work 2x2 atoms",
        ),
        (
            'sys_platform in "abcdefghij" and "a" in extras and "a" not in extras',
            3,
            "substring enumeration",
        ),
    ],
)
def test_axis_guards_keep_encounter_order_before_product_refusal(
    monkeypatch: pytest.MonkeyPatch,
    subsets: list[frozenset[str]],
    text: str,
    max_cells: int,
    error: str,
) -> None:
    store = engine.Memo()
    monkeypatch.setattr(engine._work_meter, "remaining", -1, raising=False)
    with pytest.raises(IntractableMarkerSet, match=error):
        engine._cell_space(engine.parse(text), max_cells, store)
    assert subsets == []
    assert engine._work_meter.remaining == -1
    assert all(key[0][0] != engine.AXIS_SET for key in store.partitions)


def test_refused_product_preserves_a_preexisting_set_partition(
    subsets: list[frozenset[str]],
) -> None:
    node = engine.parse('"a" in extras and sys_platform == "linux"')
    store = engine.Memo()
    set_axis = (engine.AXIS_SET, "extras")
    atoms = engine._atoms_by_axis(engine.collect_atoms(node))[set_axis]
    cached = engine.partition_axis(set_axis, atoms, 7, store)
    assert len(subsets) == 2

    with pytest.raises(IntractableMarkerSet, match="cell product exceeds"):
        engine._cell_space(node, 7, store)
    assert store.partitions[(set_axis, tuple(atoms), 7)] is cached
    assert len(subsets) == 2


def test_a_larger_budget_cache_does_not_skip_the_current_axis_guard(
    subsets: list[frozenset[str]],
) -> None:
    node = engine.parse('"a" in extras and "a" not in extras')
    atoms = engine.collect_atoms(node)
    store = engine.Memo()
    engine.partition_axis((engine.AXIS_SET, "extras"), atoms, 4, store)
    assert len(subsets) == 2

    with pytest.raises(IntractableMarkerSet, match="axis work 2x2 atoms"):
        engine._cell_space(node, 3, store)
    assert len(subsets) == 2
    assert len(store.partitions) == 1


def test_refusal_then_smaller_query_builds_once_and_reuses(
    subsets: list[frozenset[str]],
) -> None:
    store = engine.Memo()
    refused = engine.parse('"a" in extras and sys_platform == "linux"')
    for _ in range(2):
        with pytest.raises(IntractableMarkerSet, match="cell product exceeds"):
            engine._cell_space(refused, 7, store)
    assert subsets == []

    admitted = engine.parse('"a" in extras')
    first = engine._cell_space(admitted, 7, store)
    second = engine._cell_space(admitted, 7, store)
    assert first == second
    assert first.partitions[0] is second.partitions[0]
    direct = engine.partition_axis(
        (engine.AXIS_SET, "extras"), engine.collect_atoms(admitted), 7, store
    )
    assert direct == first.partitions[0]
    assert len(subsets) == 2


def test_work_refusal_happens_after_admitted_set_is_cached(
    monkeypatch: pytest.MonkeyPatch, subsets: list[frozenset[str]]
) -> None:
    node = engine.parse('"a" in extras')
    store = engine.Memo()
    monkeypatch.setattr(engine._work_meter, "remaining", 1, raising=False)
    with pytest.raises(IntractableMarkerSet, match="simplification work exceeds"):
        engine._cell_space(node, 100, store)
    assert len(subsets) == 2
    assert len(store.partitions) == 1
    assert engine._work_meter.remaining == -1

    with pytest.raises(IntractableMarkerSet, match="simplification work exceeds"):
        engine._cell_space(node, 100, store)
    assert len(subsets) == 2
    assert engine._work_meter.remaining == -3


def test_constant_space_keeps_zero_units_without_charging_a_negative_meter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine._work_meter, "remaining", -1, raising=False)
    space = engine._cell_space(engine.FALSE, -1, engine.Memo())
    assert space == engine._CellSpace([], [], [], 0)
    assert engine._work_meter.remaining == -1


def test_two_names_keep_subset_and_vector_order(
    subsets: list[frozenset[str]],
) -> None:
    node = engine.parse('"b" in extras and "a" not in extras')
    space = engine._cell_space(node, 16, engine.Memo())
    assert subsets == [
        frozenset(),
        frozenset({"b"}),
        frozenset({"a"}),
        frozenset({"a", "b"}),
    ]
    assert [cell.point for cell in space.partitions[0]] == subsets
    assert [cell.vector for cell in space.partitions[0]] == [
        (False, True),
        (True, True),
        (False, False),
        (True, False),
    ]


@pytest.mark.parametrize("cached_index", [0, 1])
@pytest.mark.parametrize("max_cells", [23, 24])
def test_mixed_warm_and_cold_sets_keep_exact_product_boundary(
    subsets: list[frozenset[str]], cached_index: int, max_cells: int
) -> None:
    node = engine.parse('"a" in extras and "b" in extras and "c" in dependency_groups')
    grouped = engine._atoms_by_axis(engine.collect_atoms(node))
    axes = list(grouped)
    store = engine.Memo()
    cached = engine.partition_axis(
        axes[cached_index], grouped[axes[cached_index]], max_cells, store
    )
    subsets.clear()

    if max_cells == 23:
        with pytest.raises(IntractableMarkerSet, match="cell product exceeds"):
            engine._cell_space(node, max_cells, store)
        assert subsets == []
        assert len(store.partitions) == 1
    else:
        space = engine._cell_space(node, max_cells, store)
        assert space.axes == axes
        assert list(map(len, space.partitions)) == [4, 2]
        assert space.partitions[cached_index] is cached
        assert space.units == 24
        assert len(subsets) == (2 if cached_index == 0 else 4)
        assert len(store.partitions) == 2


def test_repeated_refusal_reuses_the_later_scalar_partition(
    monkeypatch: pytest.MonkeyPatch, subsets: list[frozenset[str]]
) -> None:
    node = engine.parse('"a" in extras and sys_platform == "linux"')
    store = engine.Memo()
    real = engine.partition_value_axis
    calls: list[str] = []

    def observed(
        variable: str, atoms: Iterable[engine.Atom], max_cells: int, memo: engine.Memo
    ) -> list[engine.Cell]:
        calls.append(variable)
        return real(variable, list(atoms), max_cells, memo)

    monkeypatch.setattr(engine, "partition_value_axis", observed)
    for _ in range(2):
        with pytest.raises(IntractableMarkerSet, match="cell product exceeds"):
            engine._cell_space(node, 7, store)
    assert calls == ["sys_platform"]
    assert len(store.partitions) == 1
    assert subsets == []


@pytest.mark.skipif(
    get_int_max_str_digits() == 0, reason="requires a native integer parse limit"
)
@pytest.mark.parametrize("set_first", [True, False])
@pytest.mark.parametrize("max_cells", [3, 100_000])
def test_numeric_literal_and_set_guard_keep_original_priority(
    monkeypatch: pytest.MonkeyPatch,
    subsets: list[frozenset[str]],
    max_cells: int,
    *,
    set_first: bool,
) -> None:
    literal = "9" * (get_int_max_str_digits() + 1)
    version = engine.AtomLeaf(
        engine.Atom(
            engine.AXIS_VALUE,
            "python_full_version",
            "python_full_version",
            ">=",
            literal,
        )
    )
    membership = engine.parse('"a" in extras and "a" not in extras')
    node = engine.AndNode((membership, version) if set_first else (version, membership))
    expected = (
        "axis work 2x2 atoms"
        if set_first and max_cells == 3
        else "version literal numeric component"
    )
    monkeypatch.setattr(engine._work_meter, "remaining", -1, raising=False)
    with pytest.raises(IntractableMarkerSet, match=expected):
        engine._cell_space(node, max_cells, engine.Memo())
    assert subsets == []
    assert engine._work_meter.remaining == -1
