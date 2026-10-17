"""Check bounded truth masks against independent cell-by-cell evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from itertools import product

import pytest

from nab_markersets import _markersets as engine
from nab_markersets.markersets import DecisionStore, MarkerSet


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

    monkeypatch.setattr(engine, "_compile_cell_truth_masks", unexpected_masks)
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
    real = engine._compile_cell_truth_masks
    sizes = []

    def record_masks(
        space: engine._CellSpace, size: int
    ) -> tuple[dict[engine.Atom, int], int]:
        sizes.append(size)
        return real(space, size)

    monkeypatch.setattr(engine, "_compile_cell_truth_masks", record_masks)
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


def _compilations(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Observe tables the real compiler builds, including uncached fallthrough."""
    sizes: list[int] = []
    real = engine._compile_cell_truth_masks

    def compile_masks(
        space: engine._CellSpace, size: int
    ) -> tuple[dict[engine.Atom, int], int]:
        sizes.append(size)
        return real(space, size)

    monkeypatch.setattr(engine, "_compile_cell_truth_masks", compile_masks)
    return sizes


def _shared_layout_nodes() -> tuple[engine.Formula, ...]:
    """Return different decisions with the same ordered membership columns."""
    a, b, c, d, e, f = (
        engine.parse(f'"{name}" in extras') for name in ("a", "b", "c", "d", "e", "f")
    )
    return (
        engine.make_and((a, b, c, d, e, f)),
        engine.make_and((engine.make_or((a, b)), engine.make_or((c, d)), e, f)),
        engine.make_and(
            (engine.make_or((engine.make_and((a, b)), engine.make_and((c, d)))), e, f)
        ),
    )


def _mask_cache_units(store: engine.Memo) -> int:
    """Count retained container elements and integer bits independently of admission."""
    references = bits = 0
    for key, table in (store.masks or {}).items():
        layout, size, max_cells = key
        truth, full = table
        references += 2 + len(key) + len(layout) + len(table) + 2 * len(truth)
        for axis, atoms, cells in layout:
            references += 3 + len(axis) + len(atoms)
            bits += cells.bit_length()
        bits += sum(mask.bit_length() for mask in truth.values())
        bits += sum(value.bit_length() for value in (full, size, max_cells))
    return references + (bits + 63) // 64


def test_one_store_reuses_compilation_for_different_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes = _compilations(monkeypatch)
    nodes = _shared_layout_nodes()
    store = engine.Memo()
    for node in nodes:
        assert not engine.is_empty(node, 100_000, store)
    assert sizes == [64]

    other = engine.Memo()
    assert not engine.is_empty(nodes[-1], 100_000, other)
    assert sizes == [64, 64]


def test_one_off_decisions_keep_no_compiled_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores: list[engine.Memo] = []
    real = engine.Memo

    def observed_store() -> engine.Memo:
        store = real()
        stores.append(store)
        return store

    monkeypatch.setattr(engine, "Memo", observed_store)
    for node in _shared_layout_nodes():
        assert not engine.is_empty(node, 100_000)
    assert len(stores) == 3
    assert all(store.masks is None for store in stores)


@pytest.mark.parametrize("budget", [0, 1, 32, 64, 512])
def test_mask_budget_preserves_results_and_bounds_retention(
    monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    monkeypatch.setattr(engine, "_MASK_CACHE_MAX_UNITS", budget)
    store = engine.Memo()
    sizes = _compilations(monkeypatch)
    for node in _shared_layout_nodes():
        assert not engine.is_empty(node, 100_000, store)
    assert sizes == ([64] if budget >= 64 else [64, 64, 64])
    assert 0 <= store.mask_remaining <= budget
    assert _mask_cache_units(store) <= budget - store.mask_remaining


def test_mask_budget_prices_full_and_predicate_integers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "_MASK_CACHE_MAX_UNITS", 2_000)
    store = engine.Memo()
    node = engine.parse(" and ".join(f'"e{index}" in extras' for index in range(12)))
    space = engine._cell_space(node, 100_000, store)
    masks, full = engine._cell_truth_masks(space, 4096, store=store)
    assert len(masks) == 12
    assert full.bit_length() == 4096
    assert store.masks is not None
    assert 0 < _mask_cache_units(store) <= 2_000 - store.mask_remaining


def test_mask_budget_prices_large_namespace_integers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "_MASK_CACHE_MAX_UNITS", 64)
    sizes = _compilations(monkeypatch)
    store = engine.Memo()
    node = _shared_layout_nodes()[0]
    space = engine._cell_space(node, 1 << 4096, store)
    for _ in range(2):
        engine._cell_truth_masks(space, 64, store=store)
    assert sizes == [64, 64]
    assert store.masks is None
    assert store.mask_remaining == 64


def test_simplification_shares_guard_and_row_masks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes = _compilations(monkeypatch)
    marker = MarkerSet.from_marker(
        '"a" in extras and "b" in extras and "c" in extras and "d" in extras and "e" in extras and "f" in extras'
    )
    simplified = marker.simplify(within=marker, store=DecisionStore())
    assert sizes == [64]
    assert simplified.is_full()


def test_varying_rows_share_compiled_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes = _compilations(monkeypatch)
    predicates = [f'"{name}" in extras' for name in "abcdef"]
    marker = MarkerSet.from_marker(" and ".join(predicates))
    universe = MarkerSet.from_marker(" or ".join(predicates))
    store = DecisionStore()
    for _ in range(2):
        simplified = marker.simplify(within=universe, store=store)
        for assignment in range(1, 64):
            selected = {
                name for index, name in enumerate("abcdef") if assignment & (1 << index)
            }
            assert simplified.evaluate({"extras": selected}) is (assignment == 63)

    assert sizes.count(64) == 1


def test_repeated_leaves_charge_the_current_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes = _compilations(monkeypatch)
    monkeypatch.setattr(engine._work_meter, "remaining", 10_000, raising=False)
    original = _shared_layout_nodes()[0]
    repeated = engine.parse(
        '"a" in extras and "b" in extras and "c" in extras and "d" in extras and "e" in extras and "f" in extras and "a" in extras'
    )
    store = engine.Memo()
    assert not engine.is_empty(original, 100_000, store)
    assert not engine.is_empty(repeated, 100_000, store)
    assert not engine.is_empty(repeated, 100_000, store)
    assert sizes == [64]
    assert engine._work_meter.remaining == 10_000 - 384 - 448 - 448


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            '"a" in extras and "b" in extras and "c" in extras and "d" in extras',
            '"d" in extras and "c" in extras and "b" in extras and "a" in extras',
        ),
        (
            '"a" in extras and "b" in extras and "c" in extras and "d" in extras',
            '"a" in dependency_groups and "b" in dependency_groups and "c" in dependency_groups and "d" in dependency_groups',
        ),
        (
            '"a" in extras and "b" in extras and "c" in extras and "d" in extras and os_name == "posix"',
            'os_name == "posix" and "a" in extras and "b" in extras and "c" in extras and "d" in extras',
        ),
        (
            '"a" in extras and "b" in extras and "c" in extras and "d" in extras',
            '"a" in extras and "b" in extras and "c" in extras and "other" in extras',
        ),
        (
            'python_version == "3.11" and python_full_version < "3.11" and "a" in extras and "b" in extras and "c" in extras and "d" in extras',
            'python_version == "3.11" and python_full_version >= "3.11" and "a" in extras and "b" in extras and "c" in extras and "d" in extras',
        ),
    ],
)
def test_cached_columns_match_actual_partition_order(
    monkeypatch: pytest.MonkeyPatch, first: str, second: str
) -> None:
    sizes = _compilations(monkeypatch)
    store = engine.Memo()
    for text in (first, second, first):
        node = engine.parse(f'({text}) and "e" in extras and "f" in extras')
        space = engine._cell_space(node, 100_000, store)
        combinations = list(product(*space.partitions))
        masks, full = engine._cell_truth_masks(space, len(combinations), store=store)
        assert full == (1 << len(combinations)) - 1
        for index, combination in enumerate(combinations):
            for atoms, cell in zip(space.atomlists, combination, strict=True):
                for atom, value in zip(atoms, cell.vector, strict=True):
                    assert bool(masks[atom] & (1 << index)) is value
    assert len(sizes) == 2


def test_guard_refusal_precedes_cached_masks(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes = _compilations(monkeypatch)
    store = engine.Memo()
    first, second, third = _shared_layout_nodes()
    assert not engine.is_empty(first, 100_000, store)
    with pytest.raises(engine.IntractableMarkerSet):
        engine.is_empty(second, 32, store)
    assert not engine.is_empty(third, 100_000, store)
    assert sizes == [64]


@pytest.mark.parametrize("budget", [0, 512])
def test_cached_masks_preserve_cell_work_charges(
    monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    monkeypatch.setattr(engine, "_MASK_CACHE_MAX_UNITS", budget)
    monkeypatch.setattr(engine._work_meter, "remaining", 10_000, raising=False)
    store = engine.Memo()
    nodes = _shared_layout_nodes()
    for node in (*nodes, nodes[0]):
        assert not engine.is_empty(node, 100_000, store)
    assert engine._work_meter.remaining == 10_000 - 4 * 384


@pytest.mark.parametrize("names", [("a", "b", "c"), ("a", "b", "c", "d", "e")])
def test_tiny_tables_skip_mask_retention(
    monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]
) -> None:
    sizes = _compilations(monkeypatch)
    store = engine.Memo()
    for joiner in (" and ", " or "):
        node = engine.parse(joiner.join(f'"{name}" in extras' for name in names))
        assert not engine.is_empty(node, 100_000, store)
    assert sizes == [2 ** len(names), 2 ** len(names)]
    assert store.masks is None
