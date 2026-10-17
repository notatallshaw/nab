"""Check scalar-vector assembly, cache misses and generic reduction boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nab_markersets import _markersets as engine
from nab_markersets.markersets import MarkerSet

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator


def _version_atoms() -> list[engine.Atom]:
    """Use two ordered value predicates on the implementation-version axis."""
    return engine.collect_atoms(
        engine.parse('implementation_version >= "1" and implementation_version < "3"')
    )


def test_scalar_vectors_reuse_false_and_true_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atoms = _version_atoms()
    calls: list[tuple[engine.Atom, object]] = []
    original = engine.Atom.holds

    def holds(atom: engine.Atom, point: object) -> bool:
        calls.append((atom, point))
        return original(atom, point)

    monkeypatch.setattr(engine.Atom, "holds", holds)
    store = engine.Memo()
    points = ("0", "2", "4")
    expected = [(False, True), (True, True), (True, False)]
    assert [
        engine._scalar_truth_vector(atoms, point, store) for point in points
    ] == expected
    assert len(calls) == 6
    assert len(store.truths) == 6
    assert [
        engine._scalar_truth_vector(atoms, point, store) for point in points
    ] == expected
    assert len(calls) == 6

    other = engine.Memo()
    assert [
        engine._scalar_truth_vector(atoms, point, other) for point in points
    ] == expected
    assert len(calls) == 12


@pytest.mark.parametrize("reverse_atoms", [False, True])
@pytest.mark.parametrize("reverse_points", [False, True])
def test_scalar_reductions_keep_column_order_and_first_representatives(
    *, reverse_atoms: bool, reverse_points: bool
) -> None:
    atoms = _version_atoms()
    points = ["4", "0", "2", "0.0"]
    expected = [
        engine.Cell("4", (True, False)),
        engine.Cell("0", (False, True)),
        engine.Cell("2", (True, True)),
    ]
    if reverse_points:
        points.reverse()
        expected = [
            engine.Cell("0.0", (False, True)),
            engine.Cell("2", (True, True)),
            engine.Cell("4", (True, False)),
        ]
    if reverse_atoms:
        atoms.reverse()
        expected = [
            engine.Cell(cell.point, tuple(reversed(cell.vector))) for cell in expected
        ]
    assert (
        engine._reduce_cells(points, atoms, 1000, engine.Memo(), value_points=True)
        == expected
    )
    assert engine._reduce_cells(points, atoms, 1000, engine.Memo()) == expected


@pytest.mark.parametrize("container", [list, tuple, iter])
def test_value_point_iterables_keep_the_work_cap(
    monkeypatch: pytest.MonkeyPatch,
    container: Callable[[list[str]], Iterable[str]],
) -> None:
    points = ["0", "2", "4"]
    atoms = _version_atoms()
    expected = engine._reduce_cells(points, atoms, 1000, engine.Memo())
    assert (
        engine._reduce_cells(
            container(points), atoms, 1000, engine.Memo(), value_points=True
        )
        == expected
    )
    calls = []
    original = engine._scalar_truth_vector

    def vector(
        predicates: Iterable[engine.Atom], point: object, store: engine.Memo
    ) -> tuple[bool, ...]:
        calls.append(point)
        return original(tuple(predicates), point, store)

    monkeypatch.setattr(engine, "_scalar_truth_vector", vector)
    with pytest.raises(engine.IntractableMarkerSet, match="axis work 3x2"):
        engine._reduce_cells(
            container(points), atoms, 5, engine.Memo(), value_points=True
        )
    assert calls == []


def test_list_subclasses_are_materialized_before_the_work_cap() -> None:
    class AdditionalPointList(list[str]):
        """Yield one more real point than the underlying list stores."""

        def __iter__(self) -> Iterator[str]:
            yield from super().__iter__()
            yield "4"

    points = AdditionalPointList(["0", "2"])
    with pytest.raises(engine.IntractableMarkerSet, match="axis work 3x2"):
        engine._reduce_cells(
            points, _version_atoms(), 5, engine.Memo(), value_points=True
        )


def test_only_multiple_value_predicates_use_scalar_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    generic_calls: list[object] = []
    original = engine._scalar_truth_vector
    original_truth = engine._truth

    def vector(
        atoms: Iterable[engine.Atom], point: object, store: engine.Memo
    ) -> tuple[bool, ...]:
        calls.append(point)
        return original(tuple(atoms), point, store)

    def truth(atom: engine.Atom, point: object, store: engine.Memo) -> bool:
        generic_calls.append(point)
        return original_truth(atom, point, store)

    monkeypatch.setattr(engine, "_scalar_truth_vector", vector)
    monkeypatch.setattr(engine, "_truth", truth)
    store = engine.Memo()
    atoms = _version_atoms()
    engine.partition_value_axis("implementation_version", atoms, 1000, store)
    assert calls
    assert generic_calls == []
    calls.clear()
    engine.partition_value_axis("implementation_version", atoms[:1], 1000, store)
    assert calls == []

    mixed = engine.collect_atoms(engine.parse('os_name == "posix" and "x" in os_name'))
    assert {atom.kind for atom in mixed} == {engine.AXIS_VALUE, engine.AXIS_CONTAINS}
    engine.partition_value_axis("os_name", mixed, 1000, store)
    members = engine.collect_atoms(engine.parse('"a" in extras and "b" not in extras'))
    engine.partition_set_axis(members, 1000, store)
    contains = engine.collect_atoms(engine.parse('"x" in platform_release'))
    engine.partition_boolean_axis(contains, 1000, store)
    assert calls == []
    assert generic_calls


def test_scalar_reduction_stops_after_all_truth_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atoms = engine.collect_atoms(
        engine.parse('sys_platform in "ab" and sys_platform in "ac"')
    )
    points = ["x", "b", "c", "a", "trailing"]
    observed: list[object] = []
    original = engine._scalar_truth_vector

    def vector(
        predicates: Iterable[engine.Atom], point: object, store: engine.Memo
    ) -> tuple[bool, ...]:
        observed.append(point)
        return original(tuple(predicates), point, store)

    monkeypatch.setattr(engine, "_scalar_truth_vector", vector)
    cells = engine._reduce_cells(points, atoms, 1000, engine.Memo(), value_points=True)
    assert cells == [
        engine.Cell("x", (False, False)),
        engine.Cell("b", (True, False)),
        engine.Cell("c", (False, True)),
        engine.Cell("a", (True, True)),
    ]
    assert observed == points[:4]


def test_generic_object_points_keep_per_atom_conversion() -> None:
    class Point:
        """Count conversions at the generic object-point boundary."""

        def __init__(self) -> None:
            self.reads = 0

        def __str__(self) -> str:
            self.reads += 1
            return "2"

    point = Point()
    atoms = _version_atoms()
    result = engine._reduce_cells([point], atoms, 1000, engine.Memo())
    assert result == [engine.Cell(point, (True, True))]
    assert point.reads == 4


def test_public_scalar_decisions_preserve_witnesses() -> None:
    marker = MarkerSet.from_marker(
        'implementation_version >= "1" and implementation_version < "3"'
    )
    witness = marker.witness()
    assert witness is not None
    assert marker.evaluate(witness)
    assert not marker.evaluate({"implementation_version": "0"})
    assert not marker.evaluate({"implementation_version": "4"})
