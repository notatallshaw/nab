"""Check reused row lookups, equivalent work charges and temporary ownership."""

from __future__ import annotations

import gc
import weakref
from functools import partial

import pytest

from nab_markersets import _markersets as engine
from nab_markersets.markersets import MarkerSet


@pytest.fixture(autouse=True)
def _meter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give private row-preparation calls an owned work meter."""
    monkeypatch.setattr(engine._work_meter, "remaining", 1_000_000, raising=False)


def _prepare(
    source: str, universe: str, max_cells: int, store: engine.Memo
) -> tuple[tuple[engine.Atom, ...], engine._Row, engine._PreparedRow]:
    """Classify one actual residual row that needs varying-predicate columns."""
    cover = engine._prepare_cover(engine.to_nnf(engine.parse(source)))
    rows = engine._decompose_rows(engine.parse(universe), store)
    assert len(rows) == 1
    prepared = engine._fixed_row_pattern(cover.atoms, rows[0], max_cells, store)
    assert isinstance(prepared, engine._PreparedRow)
    return cover.atoms, rows[0], prepared


@pytest.mark.parametrize(
    ("source", "universe"),
    [
        ('python_full_version >= "3.11.2"', 'python_version == "3.11"'),
        (
            '"a" in extras',
            'sys_platform == "linux" and ("a" in extras or "b" in extras)',
        ),
        (
            'sys_platform == "linux" and "a" in extras',
            'sys_platform == "linux" and ("a" in extras or "b" in extras)',
        ),
        (
            'sys_platform == "linux" and python_full_version >= "3.11.2"',
            'sys_platform == "win32" and python_version == "3.11"',
        ),
        ('os_name in "ab"', 'os_name != "z"'),
        ('os_name in "ab"', 'os_name != "z" and os_name != "z"'),
        (
            'python_version >= "3.11"',
            'python_version >= "3.10" and python_version < "3.12"',
        ),
    ],
)
def test_prepared_columns_match_full_preparation_and_work(
    source: str, universe: str
) -> None:
    store = engine.Memo()
    atoms, row, prepared = _prepare(source, universe, 100_000, store)
    engine._work_meter.remaining = 1_000_000
    expected = engine._row_pattern_columns(atoms, row, 100_000, store)
    remaining = engine._work_meter.remaining
    engine._work_meter.remaining = 1_000_000
    assert engine._prepared_row_columns(prepared, 100_000) == expected
    assert engine._work_meter.remaining == remaining


@pytest.mark.parametrize("mask_budget", [0, 16_384])
def test_work_parity_includes_large_cached_and_uncached_tables(
    monkeypatch: pytest.MonkeyPatch, mask_budget: int
) -> None:
    monkeypatch.setattr(engine, "_MASK_CACHE_MAX_UNITS", mask_budget)
    store = engine.Memo()
    universe = " or ".join(f'"{name}" in extras' for name in "bcdefg")
    atoms = (engine.collect_atoms(engine.parse('"a" in extras'))[0],)
    row = engine._Row({}, engine.parse(universe))
    prepared = engine._fixed_row_pattern(atoms, row, 100_000, store)
    assert isinstance(prepared, engine._PreparedRow)
    assert prepared.size >= engine._MASK_CACHE_MIN_CELLS
    assert bool(store.masks) is bool(mask_budget)
    engine._work_meter.remaining = 1_000_000
    expected = engine._row_pattern_columns(atoms, row, 100_000, store)
    remaining = engine._work_meter.remaining
    engine._work_meter.remaining = 1_000_000
    assert engine._prepared_row_columns(prepared, 100_000) == expected
    assert engine._work_meter.remaining == remaining


def test_prepared_columns_avoid_recompiling_the_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    original = engine._compile_cell_truth_masks

    def compile_masks(space: engine._CellSpace, size: int) -> engine.MaskTable:
        calls.append(size)
        return original(space, size)

    monkeypatch.setattr(engine, "_compile_cell_truth_masks", compile_masks)
    store = engine.Memo()
    atoms, row, prepared = _prepare(
        'python_full_version >= "3.11.2"', 'python_version == "3.11"', 100_000, store
    )
    assert calls == [prepared.size]
    assert prepared.size < engine._MASK_CACHE_MIN_CELLS
    columns = engine._prepared_row_columns(prepared, 100_000)
    assert calls == [prepared.size]
    assert engine._row_pattern_columns(atoms, row, 100_000, store) == columns
    assert calls == [prepared.size, prepared.size]


def test_prepared_work_has_the_same_exhaustion_boundary() -> None:
    store = engine.Memo()
    atoms, row, prepared = _prepare(
        'python_full_version >= "3.11.2"', 'python_version == "3.11"', 100_000, store
    )
    engine._work_meter.remaining = 1_000_000
    expected = engine._row_pattern_columns(atoms, row, 100_000, store)
    units = 1_000_000 - engine._work_meter.remaining
    operations = (
        partial(engine._prepared_row_columns, prepared, 100_000),
        partial(engine._row_pattern_columns, atoms, row, 100_000, store),
    )
    for budget in (0, prepared.setup_units - 1, prepared.setup_units, units - 1, units):
        for operation in operations:
            engine._work_meter.remaining = budget
            if budget < units:
                with pytest.raises(engine.IntractableMarkerSet, match="work exceeds"):
                    operation()
            else:
                assert operation() == expected
                assert engine._work_meter.remaining == 0


def test_column_evaluation_follows_its_full_work_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = engine.Memo()
    _atoms, _row, prepared = _prepare(
        'python_full_version >= "3.11.2"', 'python_version == "3.11"', 100_000, store
    )
    calls = []
    original = engine._eval_mask

    def evaluate(node: engine.Formula, truth: dict[engine.Atom, int], full: int) -> int:
        calls.append(node)
        return original(node, truth, full)

    monkeypatch.setattr(engine, "_eval_mask", evaluate)
    engine._work_meter.remaining = prepared.setup_units + prepared.evaluation_units - 1
    with pytest.raises(engine.IntractableMarkerSet, match="work exceeds"):
        engine._prepared_row_columns(prepared, 100_000)
    assert calls == []


@pytest.mark.parametrize("max_cells", [8, 35, 36, 100_000])
def test_source_column_cap_still_follows_setup_charges(max_cells: int) -> None:
    pins = {
        "sys_platform": "linux",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_system": "Linux",
        "platform_version": "6.0",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    }
    fixed = " and ".join(f'{key} == "{value}"' for key, value in pins.items())
    store = engine.Memo()
    atoms, row, prepared = _prepare(
        f'{fixed} and "a" in extras',
        f'{fixed} and ("a" in extras or "b" in extras)',
        max_cells,
        store,
    )
    engine._work_meter.remaining = 1_000_000
    expected = engine._row_pattern_columns(atoms, row, max_cells, store)
    remaining = engine._work_meter.remaining
    engine._work_meter.remaining = 1_000_000
    assert engine._prepared_row_columns(prepared, max_cells) == expected
    assert engine._work_meter.remaining == remaining
    assert (expected is not None) is (max_cells >= 36)

    engine._work_meter.remaining = 1_000_000
    source = engine._prepare_cover(
        engine.to_nnf(engine.parse(f'{fixed} and "a" in extras'))
    )
    result = engine._row_cover(source, [row], max_cells, engine.Memo())
    assert (result is not None) is (max_cells >= 36)


def test_aggregate_pattern_cap_can_refuse_an_admitted_table() -> None:
    masks = (0b10101010, 0b11001100, 0b11110000)
    columns = engine._RowTruth(tuple(masks[index % 3] for index in range(70)), 255, 8)
    assert len(engine._split_row_patterns(columns, 0, 500)) == 8
    patterns: set[int] = set()
    assert not engine._add_row_patterns(columns, patterns, atom_count=70, max_cells=500)
    assert len(patterns) == 8


def test_split_buffer_prices_the_existing_prefix() -> None:
    columns = engine._RowTruth((0xAAAA, 0xCCCC, 0xF0F0, 0xFF00), (1 << 15) | 1, 16)
    assert engine._split_row_patterns(columns, 0, 130) == {0, 15}
    patterns = set(range(12))
    assert not engine._add_row_patterns(columns, patterns, atom_count=4, max_cells=130)
    assert patterns == set(range(12))


def test_prepared_lookup_is_released_before_split_regions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Lookup(dict[engine.Atom, int]):
        """Allow weak observation of the real uncached mask lookup."""

    references: list[weakref.ReferenceType[Lookup]] = []
    original_compile = engine._compile_cell_truth_masks
    original_split = engine._split_row_patterns

    def compile_masks(space: engine._CellSpace, size: int) -> engine.MaskTable:
        truth, full = original_compile(space, size)
        lookup = Lookup(truth)
        references.append(weakref.ref(lookup))
        return lookup, full

    def split(
        columns: engine._RowTruth, existing: int, max_cells: int
    ) -> set[int] | None:
        gc.collect()
        assert references
        assert all(reference() is None for reference in references)
        return original_split(columns, existing, max_cells)

    monkeypatch.setattr(engine, "_compile_cell_truth_masks", compile_masks)
    monkeypatch.setattr(engine, "_split_row_patterns", split)
    source = MarkerSet.from_marker('"a" in extras')
    within = MarkerSet.from_marker('"a" in extras or "b" in extras')
    assert source.simplify(within=within).equivalent_within(source, within)


def test_later_prepared_row_keeps_unselected_prefix_patterns() -> None:
    within = MarkerSet.from_marker(
        '(sys_platform == "linux" and python_version == "3.11" and python_full_version < "3.11.2.dev0") or '
        '(sys_platform == "linux" and python_version == "3.11" and python_full_version >= "3.11.2.dev0") or '
        '(sys_platform == "win32" and python_version == "3.11")'
    )
    source = MarkerSet.from_marker(
        'sys_platform == "linux" and python_full_version >= "3.11.2.dev0"'
    )
    result = source.simplify(within=within)
    assert result.equivalent_within(source, within)
    for platform in ("linux", "win32"):
        for version in ("3.11.0", "3.11.1", "3.11.2.dev0", "3.11.2", "3.11.3"):
            environment = {"sys_platform": platform, "python_full_version": version}
            assert result.evaluate(environment) == source.evaluate(environment)


def test_prepared_columns_are_released_before_later_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live: set[int] = set()
    calls = []

    class Columns(tuple[int, ...]):
        """Observe the lifetime of the real source-column values."""

        __slots__ = ()

        def __init__(self, _values: tuple[int, ...]) -> None:
            live.add(id(self))

        def __del__(self) -> None:
            live.remove(id(self))

    def tracked(row: engine._RowTruth | None) -> engine._RowTruth | None:
        """Wrap actual column values without replacing their row semantics."""
        if row is None:
            return None
        return engine._RowTruth(Columns(row.columns), row.within, row.size)

    original_prepared = engine._prepared_row_columns
    original_full = engine._row_pattern_columns

    def prepared_columns(
        prepared: engine._PreparedRow, max_cells: int
    ) -> engine._RowTruth | None:
        return tracked(original_prepared(prepared, max_cells))

    def full_columns(
        atoms: tuple[engine.Atom, ...],
        row: engine._Row,
        max_cells: int,
        store: engine.Memo,
    ) -> engine._RowTruth | None:
        gc.collect()
        assert not live
        calls.append(row)
        return tracked(original_full(atoms, row, max_cells, store))

    monkeypatch.setattr(engine, "_prepared_row_columns", prepared_columns)
    monkeypatch.setattr(engine, "_row_pattern_columns", full_columns)
    source = MarkerSet.from_marker(
        'sys_platform == "linux" and python_full_version >= "3.11.2.dev0"'
    )
    within = MarkerSet.from_marker(
        '(sys_platform == "win32" and python_version == "3.11") or '
        '(sys_platform == "linux" and python_version == "3.11" and python_full_version < "3.11.2.dev0") or '
        '(sys_platform == "linux" and python_version == "3.11" and python_full_version >= "3.11.2.dev0")'
    )
    assert source.simplify(within=within).equivalent_within(source, within)
    assert len(calls) == 2
    gc.collect()
    assert not live


def test_public_cover_consumes_the_prepared_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    original_prepared = engine._prepared_row_columns
    original_full = engine._row_pattern_columns

    def prepared_columns(
        prepared: engine._PreparedRow, max_cells: int
    ) -> engine._RowTruth | None:
        calls.append("prepared")
        return original_prepared(prepared, max_cells)

    def full_columns(
        atoms: tuple[engine.Atom, ...],
        row: engine._Row,
        max_cells: int,
        store: engine.Memo,
    ) -> engine._RowTruth | None:
        calls.append("full")
        return original_full(atoms, row, max_cells, store)

    monkeypatch.setattr(engine, "_prepared_row_columns", prepared_columns)
    monkeypatch.setattr(engine, "_row_pattern_columns", full_columns)
    source = MarkerSet.from_marker('python_full_version >= "3.11.2"')
    within = MarkerSet.from_marker('python_version == "3.11"')
    result = source.simplify(within=within)
    assert calls == ["prepared"]
    assert not result.evaluate({"python_full_version": "3.11.1"})
    assert result.evaluate({"python_full_version": "3.11.2"})


def test_work_refusal_in_prepared_setup_restores_the_outer_meter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = engine.parse('python_full_version >= "3.11.2"')
    universe = engine.parse('python_version == "3.11"')
    calls: list[tuple[int, int]] = []
    original = engine._prepared_row_columns

    def columns(
        prepared: engine._PreparedRow, max_cells: int
    ) -> engine._RowTruth | None:
        calls.append((engine._work_meter.remaining, prepared.setup_units))
        return original(prepared, max_cells)

    monkeypatch.setattr(engine, "_prepared_row_columns", columns)
    engine.simplify_within(source, universe, 100_000, 1_000_000)
    assert len(calls) == 1
    remaining, setup = calls[0]
    limit = 1_000_000 - remaining + setup - 1
    engine._work_meter.remaining = 123
    with pytest.raises(engine.IntractableMarkerSet, match="work exceeds"):
        engine.simplify_within(source, universe, 100_000, limit)
    assert len(calls) == 2
    assert engine._work_meter.remaining == 123
