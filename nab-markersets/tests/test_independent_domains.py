"""Independent set domains retain every supported scalar and membership outcome."""

from __future__ import annotations

from itertools import product
from typing import TYPE_CHECKING

import pytest

from nab_markersets import _markersets as engine
from nab_markersets.markersets import MarkerSet

if TYPE_CHECKING:
    from collections.abc import Sequence


def _cover(text: str, within: str, max_cells: int = 100_000) -> engine.Formula | None:
    """Attempt the independent cover directly so a fallback cannot hide a defect."""
    source = engine._prepare_cover(engine.to_nnf(engine.parse(text)))
    store = engine.Memo()
    return engine._independent_cover(
        source, engine._decompose_rows(engine.parse(within), store), max_cells, store
    )


@pytest.mark.parametrize(
    ("text", "within"),
    [
        (
            '"a" in extras and python_version >= "3.11"',
            'python_version >= "3.10" and python_version < "3.12"',
        ),
        (
            '("a" in extras or "b" not in extras) and python_version >= "3.11"',
            'python_version >= "3.10" and python_version < "3.12"',
        ),
        (
            '("a" in extras or "a" not in extras) and python_version >= "3.11"',
            'python_version >= "3.10"',
        ),
        ('"a" in extras and "a" not in extras', 'sys_platform == "linux"'),
        ('"a" in extras', 'sys_platform == "linux" and sys_platform == "win32"'),
        (
            '"a" in extras and python_full_version < "3.11"',
            'python_version == "3.10" or python_version == "3.11"',
        ),
        (
            '"a" in extras and python_version == "3.10"',
            'python_full_version == "3.10.0" or python_full_version == "3.11.0"',
        ),
        (
            '"a" in extras and sys_platform == "linux"',
            'sys_platform == "linux" or sys_platform != "win32"',
        ),
        ('extra == "A_B" or extra != "a-b"', 'sys_platform == "linux"'),
    ],
)
def test_independent_cover_matches_direct_points_and_public_fixed_point(
    text: str, within: str
) -> None:
    result = _cover(text, within)
    assert result is not None
    original, universe, selected = (
        MarkerSet.from_marker(text),
        MarkerSet.from_marker(within),
        MarkerSet._wrap(result),
    )
    for py, platform, a, b in product(
        ("3.10", "3.11", "3.12"),
        ("linux", "win32", "darwin"),
        (False, True),
        (False, True),
    ):
        env = {
            "python_version": py,
            "python_full_version": f"{py}.0",
            "sys_platform": platform,
            "extras": {
                name for name, present in (("a", a), ("a-b", a), ("b", b)) if present
            },
            "extra": {"a-b"} if a else set(),
        }
        if universe.evaluate(env):
            assert original.evaluate(env) == selected.evaluate(env)
    if not selected.is_empty():
        rendered = selected.to_marker_string()
        assert (
            MarkerSet.from_marker(rendered or "")
            .simplify(within=universe)
            .to_marker_string()
            == rendered
        )


@pytest.mark.parametrize(
    ("text", "within"),
    [
        (
            '("a" in extras and sys_platform == "linux") or ("b" in extras and sys_platform != "linux")',
            "",
        ),
        ('"a" in extras and "g" in dependency_groups', ""),
        ('"a" in extras', '"b" in extras'),
        ('sys_platform == "linux"', ""),
    ],
)
def test_nonseparable_or_constrained_domains_decline(text: str, within: str) -> None:
    assert _cover(text, within) is None
    source, universe = MarkerSet.from_marker(text), MarkerSet.from_marker(within)
    assert source.simplify(within=universe).equivalent_within(source, universe)


def test_high_membership_bits_and_source_false_scalar_rows_are_retained() -> None:
    names = tuple(f"n{index}" for index in range(8))
    text = '("n7" in extras or "n0" not in extras) and python_version >= "3.11"'
    # Keep all eight columns present without restricting their assignments.
    text += "".join(
        f' and ("{name}" in extras or "{name}" not in extras)' for name in names[1:7]
    )
    universe = 'python_version == "3.10" or python_version == "3.11"'
    result = _cover(text, universe)
    assert result is not None
    selected = MarkerSet._wrap(result)
    for py, flags in product(
        ("3.10", "3.11"), product((False, True), repeat=len(names))
    ):
        extras = {name for name, flag in zip(names, flags, strict=True) if flag}
        assert selected.evaluate({"python_version": py, "extras": extras}) == (
            py == "3.11" and ("n7" in extras or "n0" not in extras)
        )


def test_axis_discarded_by_general_cover_still_reaches_public_fixed_point() -> None:
    source = MarkerSet.from_marker(
        '("a" in extras and sys_platform == "linux") or '
        '("a" in extras and "g" in dependency_groups and sys_platform == "win32")'
    )
    within = MarkerSet.from_marker('sys_platform == "linux"')
    once = source.simplify(within=within).to_marker_string()
    assert once == '"a" in extras'
    assert (
        MarkerSet.from_marker(once).simplify(within=within).to_marker_string() == once
    )


@pytest.mark.parametrize("equal", [False, True])
def test_nonseparable_equivalence_uses_original_formulas(equal: bool) -> None:
    left = engine.parse('("a" in extras and python_version >= "3.11") or "b" in extras')
    right = engine.parse(
        '("a" in extras and python_version >= "3.11") or "b" in extras'
        if equal
        else '("a" in extras and python_version >= "3.11") or "b" not in extras'
    )
    universe = engine.parse('python_version >= "3.10" and python_version < "3.12"')
    store = engine.Memo()
    rows = engine._decompose_rows(universe, store)
    assert (
        engine._independent_rows_equivalent(left, right, rows, 6, 100_000, store)
        is equal
    )
    assert (
        engine.equivalent_within_rows(
            left, right, universe, 100_000, max_work=1_000_000
        )
        is equal
    )


@pytest.mark.parametrize("budget", [-1, 0, 1])
def test_owned_comparison_budget_restores_unset_meter(
    monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    monkeypatch.setattr(engine._work_meter, "remaining", None, raising=False)
    with pytest.raises(engine.IntractableMarkerSet, match="max_work"):
        engine.equivalent_within_rows(
            engine.TRUE, engine.TRUE, engine.FALSE, 100_000, max_work=budget
        )
    assert engine._work_meter.remaining is None


def test_comparison_borrows_and_consumes_outer_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine._work_meter, "remaining", 10_000, raising=False)
    source = engine.parse('"a" in extras')
    assert engine.equivalent_within_rows(
        source, source, engine.TRUE, 100_000, max_work=0
    )
    assert 0 < engine._work_meter.remaining < 10_000
    monkeypatch.setattr(engine._work_meter, "remaining", -1)
    with pytest.raises(engine.IntractableMarkerSet, match="max_work"):
        engine.equivalent_within_rows(
            source, source, engine.FALSE, 100_000, max_work=10_000
        )
    assert engine._work_meter.remaining < -1


def test_partial_attempt_does_not_reset_fallback_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = engine._rows_equivalent
    remaining: list[int] = []

    def fallback(
        left: engine.Formula,
        rows: Sequence[engine._Row],
        right: Sequence[engine.Formula],
        max_cells: int,
        store: engine.Memo,
    ) -> bool:
        remaining.append(engine._work_meter.remaining)
        return original(left, rows, right, max_cells, store)

    monkeypatch.setattr(engine, "_rows_equivalent", fallback)
    monkeypatch.setattr(engine._work_meter, "remaining", 10_000, raising=False)
    source = engine.parse('"a" in extras')
    assert engine.equivalent_within_rows(
        source, source, engine.TRUE, 16, max_work=10_000
    )
    assert len(remaining) == 1
    assert 0 < engine._work_meter.remaining <= remaining[0] < 10_000


def test_repeated_source_leaves_are_charged_before_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = engine.parse('"a" in extras')
    # Deliberately retain repeated executable leaves instead of parser normalization.
    repeated = engine.AndNode((source,) * 100)
    monkeypatch.setattr(engine._work_meter, "remaining", None, raising=False)
    with pytest.raises(engine.IntractableMarkerSet, match="max_work"):
        engine.equivalent_within_rows(
            repeated, repeated, engine.TRUE, 100_000, max_work=1_000
        )
    assert engine._work_meter.remaining is None


def _forbid_column_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail if a rejected comparison allocates its scalar or membership columns."""

    def unexpected(*_args: object) -> None:
        pytest.fail("comparison built truth columns before admission")

    monkeypatch.setattr(engine, "_compile_cell_truth_masks", unexpected)
    monkeypatch.setattr(engine, "_membership_columns", unexpected)


def test_empty_rows_do_not_allocate_a_membership_powerset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_column_builds(monkeypatch)
    source = engine.parse('"a" in extras')
    assert engine.equivalent_within_rows(
        source, engine.FALSE, engine.FALSE, 100_000, max_work=100_000
    )


def test_first_row_difference_precedes_later_intractable_row() -> None:
    left = engine.parse('"a" in extras')
    universe = engine.parse(
        'sys_platform == "linux" or ('
        + " and ".join(f'"e{index}" in dependency_groups' for index in range(16))
        + ")"
    )
    assert not engine.equivalent_within_rows(
        left, engine.FALSE, universe, 100_000, max_work=100_000
    )


@pytest.mark.parametrize("max_cells", [16, 512])
def test_membership_admission_precedes_positioned_integer_construction(
    monkeypatch: pytest.MonkeyPatch, max_cells: int
) -> None:
    atoms = engine._prepare_cover(
        engine.parse(" and ".join(f'"n{index}" in extras' for index in range(6)))
    ).atoms
    axis = engine._FreeSetAxis(atoms, ())
    reads: list[engine.Atom] = []
    original = engine.Atom.__getattribute__

    def observed(atom: engine.Atom, name: str) -> object:
        if name == "positive":
            reads.append(atom)
        return original(atom, name)

    monkeypatch.setattr(engine.Atom, "__getattribute__", observed)
    assert engine._membership_patterns(axis, 1, max_cells) is None
    assert reads == []


def test_combined_table_allowance_declines_before_mask_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_column_builds(monkeypatch)
    source = engine.parse('"a" in extras')
    assert (
        engine._independent_rows_equivalent(
            source, source, [engine._Row({}, engine.TRUE)], 2, 75, engine.Memo()
        )
        is None
    )


@pytest.mark.parametrize(
    ("bits", "units"), [(0, 1), (63, 1), (64, 2), (65, 2), (127, 2), (128, 3)]
)
def test_payload_accounts_for_full_mask_construction_carry(
    bits: int, units: int
) -> None:
    assert engine._integer_payload_units(bits) == units


def test_original_complements_are_compared_without_a_cover_normal_form() -> None:
    source = MarkerSet.from_marker('"a" in extras')
    within = MarkerSet.from_marker('sys_platform == "linux"')
    assert (~source).equivalent_within(
        MarkerSet.from_marker('"a" not in extras'), within
    )
    assert not (~source).equivalent_within(source, within)


def test_cap_declined_factor_becomes_admissible_after_general_atom_shrink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[tuple[int, bool]] = []
    original = engine._independent_cover

    def cover(
        source: engine._CoverInput,
        rows: Sequence[engine._Row],
        max_cells: int,
        store: engine.Memo,
    ) -> engine.Formula | None:
        result = original(source, rows, max_cells, store)
        attempts.append((len(source.atoms), result is None))
        return result

    monkeypatch.setattr(engine, "_independent_cover", cover)
    text = '"a" in extras and os_name == "posix"'
    universe = engine.parse('os_name == "posix"')
    result = engine.simplify_within(engine.parse(text), universe, 100, 1_000_000)
    assert MarkerSet._wrap(result).to_marker_string() == '"a" in extras'
    assert attempts[0] == (2, True)
    assert attempts[-1] == (1, False)
    assert (
        engine.simplify_within(result, universe, 100, 1_000_000).key() == result.key()
    )


def test_bound_only_columns_are_reserved_before_truth_compilation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = engine.parse('"a" in extras')
    bound = engine.parse(
        'python_version >= "3.10" and python_version < "3.12" and "x" in platform_release'
    )
    store = engine.Memo()
    rows = engine._decompose_rows(bound, store)
    axis = engine._free_set_axis(engine.collect_atoms(source), rows)
    assert axis is not None
    calls: list[int] = []
    original = engine._compile_cell_truth_masks

    def compile_columns(
        space: engine._CellSpace, size: int
    ) -> tuple[dict[engine.Atom, int], int]:
        calls.append(size)
        return original(space, size)

    # No source scalar columns exist; every scalar partition column belongs to the bound.
    predicates = engine.collect_atoms(rows[0].bound)
    space = engine._cell_space(
        engine.make_and(engine.AtomLeaf(atom) for atom in predicates), 100_000, store
    )
    size = 1
    for partition in space.partitions:
        size *= len(partition)
    required = 2 * len(predicates) + (
        len(predicates) + 3
    ) * engine._integer_payload_units(size)
    monkeypatch.setattr(engine, "_compile_cell_truth_masks", compile_columns)
    assert (
        engine._row_pattern_columns(
            (), rows[0], 100_000, store, max_buffer_units=required - 1
        )
        is None
    )
    assert calls == []
    assert (
        engine._row_pattern_columns(
            (), rows[0], 100_000, store, max_buffer_units=required
        )
        is not None
    )
    assert calls == [size]


def test_original_node_work_prices_repeated_complements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = engine.parse('"a" in extras')
    nested = source
    for _ in range(40):
        nested = engine.NotNode(nested)
    charges: list[int] = []
    original = engine.charge_work

    def charge(units: int) -> None:
        charges.append(units)
        original(units)

    monkeypatch.setattr(engine, "charge_work", charge)
    monkeypatch.setattr(engine._work_meter, "remaining", None, raising=False)
    assert engine.equivalent_within_rows(
        nested, source, engine.TRUE, 100_000, max_work=100_000
    )
    # The per-cell comparison prices41 source nodes,1 right node and its loop work.
    assert 2 * (42 + 1) + 1 in charges


def test_equivalence_skips_false_and_semantically_empty_rows() -> None:
    source = engine.parse('"a" in extras')
    store = engine.Memo()
    empty = engine.parse('python_version == "3.10" and python_full_version >= "3.11"')
    rows = [
        engine._Row({}, engine.FALSE),
        engine._Row({}, empty),
        engine._Row({}, engine.TRUE),
    ]
    assert engine._independent_rows_equivalent(source, source, rows, 2, 100_000, store)


@pytest.mark.parametrize("variable", ["extras", "dependency_groups"])
def test_membership_columns_match_each_subset(variable: str) -> None:
    names = ("z", "a", "m")
    predicates = [
        f'"{name}" {op} {variable}' for name in names for op in ("in", "not in")
    ]
    atoms = engine.collect_atoms(engine.parse(" and ".join(predicates)))
    table = engine._membership_columns(atoms, 1 << len(names))

    assert table.size == 1 << len(names)
    assert table.within == (1 << table.size) - 1
    for bits in range(table.size):
        members = {name for index, name in enumerate(names) if bits & (1 << index)}
        assert [bool(column & (1 << bits)) for column in table.columns] == [
            atom.holds(members) for atom in atoms
        ]


def test_unadmitted_membership_domain_declines_without_preparing_columns() -> None:
    source = engine.parse(" and ".join(f'"n{index}" in extras' for index in range(16)))
    assert (
        engine._independent_rows_equivalent(
            source, source, [engine._Row({}, engine.TRUE)], 32, 100_000, engine.Memo()
        )
        is None
    )


def test_scalar_projection_work_prices_every_column_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = engine.parse(
        '"a" in extras and python_version >= "3.11" and "x" in platform_release'
    )
    universe = engine.parse('python_version >= "3.10" and python_version < "3.12"')
    store = engine.Memo()
    rows = engine._decompose_rows(universe, store)
    axis = engine._free_set_axis(engine.collect_atoms(source), rows)
    assert axis is not None
    columns = engine._row_pattern_columns(axis.others, rows[0], 100_000, store)
    membership = engine._row_pattern_columns(
        axis.members, engine._Row({}, engine.TRUE), 100_000, store
    )
    assert columns is not None
    assert membership is not None
    charges: list[int] = []
    original = engine.charge_work

    def charge(units: int) -> None:
        charges.append(units)
        original(units)

    monkeypatch.setattr(engine, "charge_work", charge)
    assert engine._compare_independent_row(source, source, axis, membership, columns, 8)
    expected = membership.size * (8 + len(axis.others) + 1) + columns.size * (
        len(axis.others) + 1
    )
    assert charges == [
        len(axis.members) + len(axis.others),
        *([expected] * columns.within.bit_count()),
    ]
