"""Observed-pattern covers over rows with varying source predicates."""

from __future__ import annotations

from itertools import product
from typing import TYPE_CHECKING

import pytest

from nab_markersets import _markersets as engine
from nab_markersets.markersets import DecisionStore, MarkerSet

if TYPE_CHECKING:
    from collections.abc import Sequence


def _observed_patterns(atoms: Sequence[engine.Atom], row: engine._Row) -> set[int]:
    """Enumerate actual cells and evaluate each source predicate as a Boolean."""
    restricted = [
        engine.restrict_tree(engine.AtomLeaf(atom), row.pins) for atom in atoms
    ]
    predicates = list(
        dict.fromkeys(
            (
                *engine.collect_atoms(row.bound),
                *(atom for node in restricted for atom in engine.collect_atoms(node)),
            )
        )
    )
    space = engine._cell_space(
        engine.make_and(engine.AtomLeaf(atom) for atom in predicates),
        100_000,
        engine.Memo(),
    )
    patterns = set()
    for cells in product(*space.partitions):
        truth = {
            atom: value
            for columns, cell in zip(space.atomlists, cells, strict=True)
            for atom, value in zip(columns, cell.vector, strict=True)
        }
        if engine._eval_cell(row.bound, truth):
            pattern = sum(
                1 << index
                for index, node in enumerate(restricted)
                if engine._eval_cell(node, truth)
            )
            patterns.add(pattern)
    return patterns


@pytest.mark.parametrize(
    ("text", "universe"),
    [
        ('"a" in extras', 'sys_platform == "linux"'),
        ('"a" not in extras and "b" in extras', '"c" in extras'),
        (
            '"a" in extras and "g" in dependency_groups',
            '"a" in extras or "g" not in dependency_groups',
        ),
        ('"a" in extras and sys_platform == "linux"', 'sys_platform == "linux"'),
        (
            'python_version >= "3.11" and "a" in extras',
            'python_version >= "3.10" and python_version < "3.12"',
        ),
        (
            'python_version == "3.11" and python_full_version < "3.11"',
            'python_full_version >= "3.10" and python_full_version < "3.12"',
        ),
        ('os_name == "posix" and "a" in platform_release', 'os_name == "posix"'),
        ('"linux" == sys_platform and "x" in os_name', 'sys_platform != "win32"'),
        (
            '"3" in python_version and "a" in extras',
            'python_version >= "3.9" and python_version < "3.12"',
        ),
        ('"a" in extras', 'sys_platform == "linux" and sys_platform == "win32"'),
        ('"a" in extras and "a" not in extras', 'sys_platform == "linux"'),
        (
            '"a" in extras or python_version >= "3.11"',
            '(sys_platform == "linux" and python_version >= "3.10") or (sys_platform == "linux" and python_version >= "3.11")',
        ),
    ],
)
def test_patterns_match_independent_cell_evaluation(text: str, universe: str) -> None:
    source = engine._prepare_cover(engine.to_nnf(engine.parse(text)))
    store = engine.Memo()
    rows = engine._decompose_rows(engine.parse(universe), store)
    for row in rows:
        columns = engine._row_pattern_columns(source.atoms, row, 100_000, store)
        assert columns is not None
        assert engine._split_row_patterns(columns, 0, 100_000) == _observed_patterns(
            source.atoms, row
        )


def test_false_source_patterns_block_widening_to_full() -> None:
    source = MarkerSet.from_marker('"a" in extras')
    within = MarkerSet.from_marker('sys_platform == "linux"')
    simplified = source.simplify(within=within, store=DecisionStore())
    assert not simplified.is_full()
    assert simplified.evaluate({"sys_platform": "linux", "extras": {"a"}})
    assert not simplified.evaluate({"sys_platform": "linux", "extras": set()})


def test_varying_cover_keeps_its_public_fixed_point() -> None:
    source = MarkerSet.from_marker(
        'python_version == "3.10" or python_version == "3.11"'
        ' or (python_version >= "3.10" and platform_system != "Linux")'
    )
    within = MarkerSet.from_marker(
        'python_version >= "3.9" and python_version < "3.12"'
    )
    simplified = source.simplify(within=within)
    text = simplified.to_marker_string()
    assert text == 'python_version >= "3.10"'
    assert simplified.equivalent_within(source, within)
    assert (
        MarkerSet.from_marker(text).simplify(within=within).to_marker_string() == text
    )


def test_prefix_patterns_keep_columns_above_bit_64() -> None:
    columns = tuple(1 if index % 2 else 2 for index in range(70))
    row = engine._RowTruth(columns, 3, 2)
    odd = sum(1 << index for index in range(70) if index % 2)
    even = sum(1 << index for index in range(70) if not index % 2)
    assert engine._split_row_patterns(row, 0, 100_000) == {odd, even}


@pytest.mark.parametrize(
    ("columns", "within", "size", "limit"),
    [
        ((1,), 3, 2, 1),
        ((1 << 1023, 1), (1 << 1024) - 1, 1024, 250),
        (tuple(1 if index % 2 else 2 for index in range(70)), 3, 2, 250),
    ],
)
def test_buffer_refusal_preserves_the_remaining_work(
    monkeypatch: pytest.MonkeyPatch,
    columns: tuple[int, ...],
    within: int,
    size: int,
    limit: int,
) -> None:
    monkeypatch.setattr(engine._work_meter, "remaining", 10_000, raising=False)
    assert (
        engine._split_row_patterns(engine._RowTruth(columns, within, size), 0, limit)
        is None
    )
    assert 0 < engine._work_meter.remaining <= 10_000


def test_prefix_word_work_has_an_exact_exhaustion_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = engine._RowTruth(tuple(1 if index % 2 else 2 for index in range(70)), 3, 2)
    monkeypatch.setattr(engine._work_meter, "remaining", 1676, raising=False)
    with pytest.raises(engine.IntractableMarkerSet, match="work exceeds"):
        engine._split_row_patterns(row, 0, 100_000)
    monkeypatch.setattr(engine._work_meter, "remaining", 1677)
    assert engine._split_row_patterns(row, 0, 100_000) is not None
    assert engine._work_meter.remaining == 0


def test_varying_suffix_keeps_forbidden_patterns_from_the_constant_prefix() -> None:
    source = MarkerSet.from_marker('os_name == "posix" or "a" in extras')
    within = MarkerSet.from_marker(
        '(os_name == "nt" and "a" not in extras) or os_name == "posix"'
    )
    result = source.simplify(within=within)
    assert not result.is_full()
    assert not result.evaluate({"os_name": "nt", "extras": set()})
    assert result.evaluate({"os_name": "posix", "extras": set()})
    assert result.equivalent_within(source, within)


def test_fully_bound_varying_predicates_avoid_dnf_expansion() -> None:
    choice = MarkerSet.from_marker('"a" in extras or "b" in extras')
    source = MarkerSet.full()
    for _ in range(20):
        source &= choice
    within = MarkerSet.from_marker(
        'sys_platform == "linux" and ("a" in extras or "b" in extras)'
    )
    assert source.simplify(within=within).is_full()


@pytest.mark.parametrize("limit", [1, 400])
def test_inadmissible_set_or_column_work_declines(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    monkeypatch.setattr(engine._work_meter, "remaining", 1_000_000, raising=False)
    atoms = engine._prepare_cover(
        engine.parse(" and ".join(f'"e{index}" in extras' for index in range(6)))
    ).atoms
    assert (
        engine._row_pattern_columns(
            atoms, engine._Row({}, engine.TRUE), limit, engine.Memo()
        )
        is None
    )


def test_known_cap_refusal_does_not_retry_the_varying_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    original = engine._row_pattern_columns

    def columns(
        atoms: Sequence[engine.Atom],
        row: engine._Row,
        max_cells: int,
        store: engine.Memo,
    ) -> engine._RowTruth | None:
        calls.append(row)
        return original(atoms, row, max_cells, store)

    monkeypatch.setattr(engine, "_row_pattern_columns", columns)
    source = engine.parse('"a" in extras')
    within = engine.parse(" and ".join(f'"e{index}" in extras' for index in range(6)))
    with pytest.raises(engine.IntractableMarkerSet):
        engine.simplify_within(source, within, 32, 1_000_000)
    assert calls == []


def test_cross_axis_cell_refusal_keeps_the_remaining_meter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atoms = engine._prepare_cover(
        engine.parse(
            '"a" in extras and "b" in extras and "c" in extras'
            ' and "a" in dependency_groups and "b" in dependency_groups and "c" in dependency_groups'
        )
    ).atoms
    monkeypatch.setattr(engine._work_meter, "remaining", 10_000, raising=False)
    assert (
        engine._row_pattern_columns(
            atoms, engine._Row({}, engine.TRUE), 64, engine.Memo()
        )
        is None
    )
    assert 0 < engine._work_meter.remaining < 10_000


def test_row_work_exhaustion_never_starts_greedy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = engine._cell_space
    fallback_calls = []
    greedy = engine._simplify_greedy

    def exhaust(
        node: engine.Formula, max_cells: int, store: engine.Memo
    ) -> engine._CellSpace:
        engine.charge_work(1_000_001)
        return original(node, max_cells, store)

    def fallback(
        node: engine.Formula,
        rows: Sequence[engine._Row],
        max_cells: int,
        store: engine.Memo,
    ) -> engine.Formula:
        fallback_calls.append(node)
        return greedy(node, rows, max_cells, store)

    monkeypatch.setattr(engine, "_cell_space", exhaust)
    monkeypatch.setattr(engine, "_simplify_greedy", fallback)
    monkeypatch.setattr(engine._work_meter, "remaining", 777, raising=False)
    with pytest.raises(engine.IntractableMarkerSet, match="work exceeds"):
        engine.simplify_within(
            engine.parse('"a" in extras'), engine.TRUE, 100_000, 1_000_000
        )
    assert fallback_calls == []
    assert engine._work_meter.remaining == 777


@pytest.mark.parametrize("reason", ["buffers", "aggregate"])
def test_optional_refusal_carries_spent_work_into_fallback(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    if reason == "buffers":
        source = engine.parse(
            " and ".join(f'"e{index}" in extras' for index in range(6))
        )
        within = engine.TRUE
    else:
        source = engine.parse(
            " or ".join(
                [
                    *(f'sys_platform == "p{index}"' for index in range(70)),
                    '"a" in extras',
                ]
            )
        )
        within = engine.parse(
            " or ".join(f'sys_platform == "p{index}"' for index in range(4))
        )
    charges, remaining = [], []
    original_charge = engine.charge_work
    greedy = engine._simplify_greedy

    def charge(units: int) -> None:
        charges.append(units)
        original_charge(units)

    def fallback(
        node: engine.Formula,
        rows: Sequence[engine._Row],
        max_cells: int,
        store: engine.Memo,
    ) -> engine.Formula:
        assert engine._work_meter.remaining == 1_000_000 - sum(charges)
        remaining.append(engine._work_meter.remaining)
        return greedy(node, rows, max_cells, store)

    monkeypatch.setattr(engine, "charge_work", charge)
    monkeypatch.setattr(engine, "_simplify_greedy", fallback)
    monkeypatch.setattr(engine._work_meter, "remaining", 777, raising=False)
    if reason == "buffers":
        with pytest.raises(engine.IntractableMarkerSet):
            engine.simplify_within(source, within, 512, 1_000_000)
    else:
        result = engine.simplify_within(source, within, 512, 1_000_000)
        assert MarkerSet._wrap(result).is_full()
    assert len(remaining) == 1
    assert 0 < remaining[0] < 1_000_000
    assert engine._work_meter.remaining == 777


def test_known_doomed_powerset_does_not_start_cell_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine._work_meter, "remaining", 1_000_000, raising=False)
    calls = []
    original = engine._cell_space

    def space(
        node: engine.Formula, max_cells: int, store: engine.Memo
    ) -> engine._CellSpace:
        calls.append(node)
        return original(node, max_cells, store)

    monkeypatch.setattr(engine, "_cell_space", space)
    atoms = engine._prepare_cover(
        engine.parse(" and ".join(f'"e{index}" in extras' for index in range(16)))
    ).atoms
    assert (
        engine._row_pattern_columns(
            atoms, engine._Row({}, engine.TRUE), 100_000, engine.Memo()
        )
        is None
    )
    assert calls == []


@pytest.mark.parametrize("pinned", [False, True])
def test_small_buffer_budget_retains_a_valid_greedy_result(pinned: bool) -> None:
    text = '"a" in extras and os_name == "posix"' if pinned else '"a" in extras'
    source = engine.parse(text)
    within = engine.parse('os_name == "posix"')
    result = MarkerSet._wrap(engine.simplify_within(source, within, 16, 1_000_000))
    assert result.to_marker_string() == '"a" in extras'
    assert result.equivalent_within(MarkerSet._wrap(source), MarkerSet._wrap(within))


def test_greedy_atom_shrink_can_admit_a_varying_refinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = []
    original = engine._split_row_patterns

    def split(row: engine._RowTruth, existing: int, max_cells: int) -> set[int] | None:
        result = original(row, existing, max_cells)
        attempts.append((len(row.columns), result is None))
        return result

    monkeypatch.setattr(engine, "_split_row_patterns", split)
    result = engine.simplify_within(
        engine.parse('"a" in extras and os_name == "posix"'),
        engine.parse('os_name == "posix"'),
        95,
        1_000_000,
    )
    assert attempts == [(2, True), (1, False)]
    assert MarkerSet._wrap(result).to_marker_string() == '"a" in extras'
