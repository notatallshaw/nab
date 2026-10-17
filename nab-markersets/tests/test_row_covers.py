"""Behavioral checks for direct covers of row-constant predicates."""

from __future__ import annotations

from itertools import product
from typing import TYPE_CHECKING

import pytest

from nab_markersets import _markersets as engine
from nab_markersets.errors import IntractableMarkerSet
from nab_markersets.markersets import DecisionStore, MarkerSet

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


_VARIABLES = ("sys_platform", "os_name", "platform_machine")
_VALUES = (("linux", "win32"), ("posix", "nt"), ("x86_64", "aarch64"))
_ENVIRONMENTS = tuple(
    dict(zip(_VARIABLES, values, strict=True)) for values in product(*_VALUES)
)
_MEMBER_NAMES = ("a", "b", "c", "d")


def _point_marker(environment: Mapping[str, str]) -> MarkerSet:
    """Represent one exact assignment to the supplied string variables."""
    return MarkerSet.from_marker(
        " and ".join(
            f'{variable} == "{value}"'
            for variable, value in sorted(environment.items())
        )
    )


def _point_universe(environments: Sequence[Mapping[str, str]]) -> MarkerSet:
    """Unite the supplied exact string assignments into a declared universe."""
    within = MarkerSet.empty()
    for environment in environments:
        within |= _point_marker(environment)
    return within


def _membership_table(table: int, variable: str, *, start_negative: bool) -> MarkerSet:
    """Encode a four-predicate truth table through API complements."""
    variables = []
    for name in _MEMBER_NAMES:
        if variable == "extra":
            condition = f'extra {"!=" if start_negative else "=="} "{name}"'
        else:
            condition = f'"{name}" {"not in" if start_negative else "in"} {variable}'
        value = MarkerSet.from_marker(condition)
        variables.append(~value if start_negative else value)
    result = MarkerSet.empty()
    for assignment in range(16):
        if table & (1 << assignment):
            clause = MarkerSet.full()
            for index, value in enumerate(variables):
                clause &= value if assignment & (1 << index) else ~value
            result |= clause
    return result


@pytest.mark.parametrize("selection", range(256))
def test_all_three_variable_functions_preserve_selection(selection: int) -> None:
    within = _point_universe(_ENVIRONMENTS)
    source = MarkerSet.empty()
    for index, environment in enumerate(_ENVIRONMENTS):
        if selection & (1 << index):
            source |= _point_marker(environment)

    result = source.simplify(within=within)
    for index, environment in enumerate(_ENVIRONMENTS):
        assert result.evaluate(environment) == bool(selection & (1 << index))

    if not selection:
        assert result.is_empty()
        return
    text = result.to_marker_string()
    rebuilt = MarkerSet.full() if text is None else MarkerSet.from_marker(text)
    for index, environment in enumerate(_ENVIRONMENTS):
        assert rebuilt.evaluate(environment) == bool(selection & (1 << index))
    assert rebuilt.simplify(within=within).to_marker_string() == text

    clauses = engine._to_clauses(rebuilt._tree, 100_000)
    for removed in range(len(clauses)):
        others = MarkerSet.empty()
        for index, clause in enumerate(clauses):
            if index != removed:
                others |= MarkerSet._wrap(engine._clause_formula(clause))
        assert any(
            bool(selection & (1 << index)) and not others.evaluate(environment)
            for index, environment in enumerate(_ENVIRONMENTS)
        )


def test_cover_masks_retain_patterns_above_bit_64() -> None:
    variables = (
        "sys_platform",
        "os_name",
        "platform_machine",
        "platform_system",
        "platform_version",
        "implementation_name",
        "platform_python_implementation",
    )
    environments = tuple(
        dict(zip(variables, values, strict=True))
        for values in product(("selected", "other"), repeat=len(variables))
    )
    source = _point_marker(dict.fromkeys(variables, "selected"))
    within = _point_universe(environments)

    result = source.simplify(within=within)
    for environment in environments:
        assert result.evaluate(environment) == all(
            value == "selected" for value in environment.values()
        )
    assert result.to_marker_string() is not None


def test_empty_row_adds_no_selection_obligation() -> None:
    source = MarkerSet.from_marker('sys_platform == "linux"')
    within = MarkerSet.from_marker(
        'sys_platform == "linux"'
        ' or (sys_platform == "linux" and sys_platform == "win32")'
    )

    assert source.simplify(within=within).to_marker_string() is None


def test_partial_universe_cover_has_no_redundant_cube() -> None:
    source = MarkerSet.from_marker(
        '(os_name == "posix" and sys_platform == "linux")'
        ' or (os_name == "posix" and sys_platform == "win32")'
    )
    within = _point_universe(
        (
            {"os_name": "nt", "sys_platform": "win32"},
            {"os_name": "posix", "sys_platform": "win32"},
            {"os_name": "posix", "sys_platform": "linux"},
        )
    )

    assert source.simplify(within=within).to_marker_string() == 'os_name == "posix"'


def test_partial_universe_removes_a_cube_without_dropping_its_predicates() -> None:
    source = MarkerSet.from_marker(
        '(os_name == "posix" and platform_machine == "aarch64" and sys_platform == "linux")'
        ' or (os_name == "posix" and platform_machine == "aarch64" and sys_platform == "win32")'
        ' or (platform_machine == "x86_64" and sys_platform == "win32")'
    )
    within = _point_universe(
        tuple(
            {"os_name": os_name, "sys_platform": platform, "platform_machine": machine}
            for os_name, platform, machine in (
                ("nt", "win32", "aarch64"),
                ("posix", "win32", "aarch64"),
                ("posix", "linux", "aarch64"),
                ("nt", "win32", "x86_64"),
                ("nt", "linux", "x86_64"),
            )
        )
    )

    assert source.simplify(within=within).to_marker_string() == (
        'os_name == "posix" or (platform_machine == "x86_64" and sys_platform == "win32")'
    )


def test_constant_rows_avoid_dnf_expansion() -> None:
    repeated = MarkerSet.from_marker('sys_platform == "linux" or os_name == "posix"')
    source = MarkerSet.full()
    for _ in range(20):
        source &= repeated

    result = source.simplify(within=_point_universe(_ENVIRONMENTS))

    assert result.to_marker_string() == 'os_name == "posix" or sys_platform == "linux"'
    for environment in _ENVIRONMENTS:
        assert result.evaluate(environment) == (
            environment["sys_platform"] == "linux" or environment["os_name"] == "posix"
        )


def test_partial_universe_normalization_is_a_fixed_point() -> None:
    environments = tuple(
        {**environment, "implementation_name": implementation}
        for environment in _ENVIRONMENTS
        for implementation in ("cpython", "pypy")
    )
    source = _point_universe(
        tuple(
            environment
            for index, environment in enumerate(environments)
            if 25234 & (1 << index)
        )
    )
    within = _point_universe(
        tuple(
            environment
            for index, environment in enumerate(environments)
            if 32668 & (1 << index)
        )
    )
    result = source.simplify(within=within)
    text = result.to_marker_string()
    assert text is not None
    rebuilt = MarkerSet.from_marker(text)

    assert rebuilt.simplify(within=within).to_marker_string() == text
    for index, environment in enumerate(environments):
        if 32668 & (1 << index):
            assert rebuilt.evaluate(environment) == bool(25234 & (1 << index))


@pytest.mark.parametrize(
    "variable",
    [
        "extra",
        "extras",
        "dependency_groups",
        "platform_version",
        "implementation_version",
    ],
)
@pytest.mark.parametrize("start_negative", [False, True])
def test_complemented_predicate_cover_is_stable_after_reparse(
    variable: str, *, start_negative: bool
) -> None:
    source = _membership_table(35469, variable, start_negative=start_negative)
    within = _membership_table(23545, variable, start_negative=start_negative)
    result = source.simplify(within=within)
    text = result.to_marker_string()
    assert text is not None
    rebuilt = MarkerSet.from_marker(text)

    assert rebuilt.simplify(within=within).to_marker_string() == text
    for assignment in range(16):
        if 23545 & (1 << assignment):
            members = frozenset(
                name
                for index, name in enumerate(_MEMBER_NAMES)
                if assignment & (1 << index)
            )
            value = (
                members
                if variable in {"extra", "extras", "dependency_groups"}
                else "".join(sorted(members))
            )
            assert result.evaluate({variable: value}) == bool(35469 & (1 << assignment))
            assert rebuilt.evaluate({variable: value}) == bool(
                35469 & (1 << assignment)
            )


def test_dead_branch_does_not_force_an_unrelated_membership_powerset() -> None:
    members = " and ".join(f'"e{index}" in extras' for index in range(16))
    source = MarkerSet.from_marker(
        f'sys_platform == "linux" or (sys_platform == "win32" and {members})'
    )
    within = MarkerSet.from_marker('sys_platform == "linux" and "selected" in extras')

    assert source.simplify(within=within).to_marker_string() is None


def test_exhausted_partition_work_does_not_start_the_greedy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = MarkerSet.from_marker('python_version == "3.9"')
    within = MarkerSet.from_marker('python_version == "3.10"')
    original = engine._simplify_greedy
    original_space = engine._cell_space
    fallbacks = []

    def exhaust_space(
        node: engine.Formula, max_cells: int, shared: engine.Memo
    ) -> engine._CellSpace:
        engine.charge_work(1_000_001)
        return original_space(node, max_cells, shared)

    def record_fallback(
        node: engine.Formula,
        rows: Sequence[engine._Row],
        max_cells: int,
        shared: engine.Memo,
    ) -> engine.Formula:
        fallbacks.append(node)
        return original(node, rows, max_cells, shared)

    monkeypatch.setattr(engine, "_cell_space", exhaust_space)
    monkeypatch.setattr(engine, "_simplify_greedy", record_fallback)
    monkeypatch.setattr(engine._work_meter, "remaining", 777, raising=False)
    with pytest.raises(IntractableMarkerSet, match="work exceeds"):
        engine.simplify_within(source._tree, within._tree, 100_000, 1_000_000)

    assert not fallbacks
    assert engine._work_meter.remaining == 777


def test_python_micro_version_variation_keeps_its_comparison() -> None:
    source = MarkerSet.from_marker('python_full_version < "3.11.1"')
    within = MarkerSet.from_marker('python_version == "3.11"')

    result = source.simplify(within=within)
    assert result.to_marker_string() == 'python_full_version < "3.11.1"'
    assert result.evaluate({"python_version": "3.11", "python_full_version": "3.11.0"})
    assert not result.evaluate(
        {"python_version": "3.11", "python_full_version": "3.11.2"}
    )


def test_compiled_partitions_serve_the_emission_equivalence_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = MarkerSet.from_marker('python_version == "3.9"')
    within = MarkerSet.from_marker(
        'python_version == "3.9" or python_version == "3.10"'
    )
    store = DecisionStore()
    original = engine._partition_axis
    calls = []

    def record_partition(
        axis: engine.Axis,
        atoms: Sequence[engine.Atom],
        max_cells: int,
        shared: engine.Memo,
    ) -> list[engine.Cell]:
        calls.append(axis)
        return original(axis, atoms, max_cells, shared)

    monkeypatch.setattr(engine, "_partition_axis", record_partition)
    result = source.simplify(within=within, store=store)
    text = result.to_marker_string(store=store)
    assert text is not None
    emitted = MarkerSet.from_marker(text)
    compiled = len(calls)

    assert source.equivalent_within(emitted, within, store=store)
    assert len(calls) == compiled


def test_aggregate_pattern_cap_falls_back_with_remaining_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = MarkerSet.from_marker('sys_platform == "linux" and os_name == "posix"')
    environments = (
        {"sys_platform": "win32", "os_name": "nt"},
        {"sys_platform": "linux", "os_name": "nt"},
        {"sys_platform": "linux", "os_name": "posix"},
    )
    within = _point_universe(environments)
    original = engine._simplify_greedy
    original_charge = engine.charge_work
    store = DecisionStore()
    remaining = []
    charges = []

    def record_charge(units: int) -> None:
        charges.append(units)
        original_charge(units)

    def record_fallback(
        node: engine.Formula,
        rows: Sequence[engine._Row],
        max_cells: int,
        shared: engine.Memo,
    ) -> engine.Formula:
        for environment in environments:
            assert MarkerSet._wrap(node).evaluate(environment) == source.evaluate(
                environment
            )
        assert tuple(row.pins for row in rows) == environments
        assert all(row.bound is engine.TRUE for row in rows)
        assert max_cells == 4
        assert shared is store
        assert engine._work_meter.remaining == 1_000_000 - sum(charges)
        remaining.append(engine._work_meter.remaining)
        return original(node, rows, max_cells, shared)

    monkeypatch.setattr(engine, "_simplify_greedy", record_fallback)
    monkeypatch.setattr(engine, "charge_work", record_charge)
    monkeypatch.setattr(engine._work_meter, "remaining", 777, raising=False)
    result = engine.simplify_within(source._tree, within._tree, 4, 1_000_000, store)

    assert engine.serialize(result) == 'os_name == "posix"'
    assert len(remaining) == 1
    assert 0 < remaining[0] < 1_000_000
    assert engine._work_meter.remaining == 777


def test_work_limit_precedes_normalization_and_large_child_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = MarkerSet.from_marker('os_name == "posix"')
    within = MarkerSet.from_marker(" and ".join(['os_name == "posix"'] * 1000))
    original = engine.to_nnf
    atom_key = engine._atom_key
    normalized = []
    visited = []

    def record_atom_key(atom: engine.Atom) -> engine.AtomKey:
        visited.append(atom)
        return atom_key(atom)

    def record_normalization(node: engine.Formula) -> engine.Formula:
        normalized.append(node)
        return original(node)

    monkeypatch.setattr(engine, "to_nnf", record_normalization)
    monkeypatch.setattr(engine, "_atom_key", record_atom_key)
    monkeypatch.setattr(engine._work_meter, "remaining", 777, raising=False)
    with pytest.raises(IntractableMarkerSet, match="work exceeds"):
        engine.simplify_within(source._tree, within._tree, 100_000, 50)

    assert not normalized
    assert visited == engine.collect_atoms(source._tree)
    assert engine._work_meter.remaining == 777
