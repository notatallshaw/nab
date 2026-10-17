"""Check bounded truth masks against independent cell-by-cell evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from itertools import product

import pytest

from nab_markersets import _markersets as engine
from nab_markersets.markersets import MarkerSet


@pytest.mark.parametrize(
    "text",
    [
        'sys_platform == "linux" or sys_platform == "darwin"',
        '(sys_platform == "linux" and platform_machine != "x86_64") or (os_name == "nt" and platform_machine == "AMD64")',
        '(python_version == "3.11" and python_full_version >= "3.11.4") or python_full_version < "3.10"',
        '(platform_release <= "6.1" and "dev" in dependency_groups) or (platform_release != "generic" and "cpu" not in extras)',
        'sys_platform in "linuxwin32" and ("win" in sys_platform or "x" not in platform_machine)',
        " and ".join(f'"extra-{index}" in extras' for index in range(7))
        + ' and (sys_platform == "linux" or sys_platform == "darwin")'
        + ' and "dev" not in dependency_groups',
    ],
)
def test_truth_masks_match_each_semantic_cell(text: str) -> None:
    node = engine.parse(text)
    space = engine._cell_space(node, 100_000, engine.Memo())
    combinations = list(product(*space.partitions))
    masks, full = engine._cell_truth_masks(space, len(combinations))
    actual = engine._eval_mask(node, masks, full)
    complement = engine._eval_mask(engine.make_not(node), masks, full)
    expected = 0
    count = 0
    for index, combo in enumerate(combinations):
        truth = {
            atom: value
            for atoms, cell in zip(space.atomlists, combo, strict=True)
            for atom, value in zip(atoms, cell.vector, strict=True)
        }
        if engine._eval_cell(node, truth):
            expected |= 1 << index
        count += 1

    assert full == (1 << count) - 1
    assert actual == expected
    assert complement == full ^ expected
    assert engine.is_empty(node, 100_000) is (expected == 0)


def test_every_three_extra_boolean_function() -> None:
    """All 256 truth tables retain their meaning and bounded complement."""
    names = ("one", "two", "three")
    variables = [MarkerSet.from_marker(f'"{name}" in extras') for name in names]
    minterms = []
    for assignment in range(8):
        clause = MarkerSet.full()
        for index, variable in enumerate(variables):
            clause &= variable if assignment & (1 << index) else ~variable
        minterms.append(clause)

    for table in range(256):
        marker = MarkerSet.empty()
        for assignment, clause in enumerate(minterms):
            if table & (1 << assignment):
                marker |= clause
        assert marker.is_empty() is (table == 0)
        assert marker.is_full() is (table == 255)
        for assignment, clause in enumerate(minterms):
            assert marker.is_disjoint(clause) is (not table & (1 << assignment))


@pytest.mark.parametrize(("joiner", "op"), [(" and ", "not in"), (" or ", "in")])
def test_large_early_witness_skips_truth_masks(
    monkeypatch: pytest.MonkeyPatch, joiner: str, op: str
) -> None:
    text = joiner.join(f'"e{index}" {op} extras' for index in range(7))
    marker = MarkerSet.from_marker(text)

    def unexpected_masks(
        _space: engine._CellSpace, _size: int
    ) -> tuple[dict[engine.Atom, int], int]:
        pytest.fail("an early witness should avoid truth-mask construction")

    monkeypatch.setattr(engine, "_cell_truth_masks", unexpected_masks)
    assert not marker.is_empty()


def _root_cell_visits(
    monkeypatch: pytest.MonkeyPatch, node: engine.Formula
) -> list[Mapping[engine.Atom, bool]]:
    """Count whole-formula probes without counting their recursive children."""
    real = engine._eval_cell
    visited: list[Mapping[engine.Atom, bool]] = []

    def count_root(
        candidate: engine.Formula, truth: Mapping[engine.Atom, bool]
    ) -> bool:
        if candidate is node:
            visited.append(truth)
        return real(candidate, truth)

    monkeypatch.setattr(engine, "_eval_cell", count_root)
    return visited


@pytest.mark.parametrize(
    ("assignment", "attempts", "uses_masks"),
    [(0, 1, False), (1, 2, False), (3, 4, False), (4, 4, True), (127, 4, True)],
)
def test_early_search_stops_at_its_cell_limit(
    monkeypatch: pytest.MonkeyPatch, assignment: int, attempts: int, *, uses_masks: bool
) -> None:
    text = " and ".join(
        f'"e{index}" {"in" if assignment & (1 << index) else "not in"} extras'
        for index in range(7)
    )
    node = engine.parse(text)
    visited = _root_cell_visits(monkeypatch, node)
    real = engine._cell_truth_masks
    sizes = []

    def record_masks(
        space: engine._CellSpace, size: int
    ) -> tuple[dict[engine.Atom, int], int]:
        sizes.append(size)
        return real(space, size)

    monkeypatch.setattr(engine, "_cell_truth_masks", record_masks)
    assert not engine.is_empty(node, 100_000)
    assert len(visited) == attempts
    assert sizes == ([128] if uses_masks else [])


@pytest.mark.parametrize(("count", "attempts"), [(5, 0), (6, 1)])
def test_early_search_product_threshold(
    monkeypatch: pytest.MonkeyPatch, count: int, attempts: int
) -> None:
    node = engine.parse(
        " and ".join(f'"e{index}" not in extras' for index in range(count))
    )
    visited = _root_cell_visits(monkeypatch, node)
    assert not engine.is_empty(node, 100_000)
    assert len(visited) == attempts


def test_an_empty_large_product_exhausts_the_bounded_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positive = engine.parse(" and ".join(f'"e{index}" in extras' for index in range(7)))
    node = engine.make_and((positive, engine.make_not(positive)))
    visited = _root_cell_visits(monkeypatch, node)
    assert engine.is_empty(node, 100_000)
    assert len(visited) == 4
