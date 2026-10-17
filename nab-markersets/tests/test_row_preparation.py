"""Check direct predicate restriction and reuse of completed raw universe work."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nab_markersets import _markersets as engine
from nab_markersets.markersets import DecisionStore, MarkerSet

if TYPE_CHECKING:
    from collections.abc import Mapping


def _walks(monkeypatch: pytest.MonkeyPatch) -> list[engine.Formula]:
    """Observe real raw traversals without changing their charges."""
    calls: list[engine.Formula] = []
    original = engine._charge_formula_work

    def charge(node: engine.Formula) -> None:
        calls.append(node)
        original(node)

    monkeypatch.setattr(engine, "_charge_formula_work", charge)
    return calls


@pytest.mark.parametrize(
    ("text", "environment"),
    [
        ('sys_platform == "linux"', {}),
        ('sys_platform == "linux"', {"sys_platform": "linux"}),
        ('sys_platform == "linux"', {"sys_platform": "win32"}),
        ('"linux" == sys_platform', {"sys_platform": "linux"}),
        ('python_version == "3.11"', {"python_full_version": "3.11.2"}),
        ('python_version == "3.11"', {"python_version": "3.12"}),
        ('"3" in python_version', {"python_full_version": "3.11"}),
        ('"x" in os_name', {"os_name": "posix"}),
        ('"x" in platform_release', {"platform_release": "x1"}),
        ('"a" in extras', {"extras": {"a"}}),
        ('"a" not in extras', {"extras": {"a"}}),
        ('"docs" in dependency_groups', {"dependency_groups": {"docs"}}),
    ],
)
def test_cover_predicates_keep_generic_restriction_semantics(
    text: str, environment: Mapping[str, object]
) -> None:
    leaf = engine.parse(text)
    assert isinstance(leaf, engine.AtomLeaf)
    actual = engine._restrict_cover_atom(leaf.atom, environment)
    expected = engine.restrict_tree(leaf, environment)
    assert actual.key() == expected.key()


@pytest.mark.parametrize("varying", [False, True])
def test_row_predicates_avoid_generic_tree_dispatch(
    monkeypatch: pytest.MonkeyPatch, *, varying: bool
) -> None:
    atom = engine.collect_atoms(engine.parse('sys_platform == "linux"'))[0]
    row = engine._Row({"sys_platform": "linux"}, engine.TRUE)
    calls: list[engine.Formula] = []
    original = engine.restrict_tree

    def restrict(node: engine.Formula, env: Mapping[str, object]) -> engine.Formula:
        calls.append(node)
        return original(node, env)

    monkeypatch.setattr(engine, "restrict_tree", restrict)
    monkeypatch.setattr(engine._work_meter, "remaining", 100_000, raising=False)
    if varying:
        result = engine._row_pattern_columns((atom,), row, 100_000, engine.Memo())
        assert result is not None
        assert result.columns == (1,)
        assert result.within == 1
    else:
        result = engine._fixed_row_pattern((atom,), row, 100_000, engine.Memo())
        assert result == engine._RowPattern(nonempty=True, bits=1)
    assert calls == []


def test_related_public_simplifications_share_only_the_universe_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    within = MarkerSet.from_marker(
        '(sys_platform == "linux" and os_name == "posix") or '
        '(sys_platform == "win32" and os_name == "nt")'
    )
    first = MarkerSet.from_marker('sys_platform == "linux"')
    second = MarkerSet.from_marker('os_name == "posix"')
    calls = _walks(monkeypatch)
    store = DecisionStore()
    left = first.simplify(within=within, store=store)
    right = second.simplify(within=within, store=store)
    assert left.equivalent_within(first, within)
    assert right.equivalent_within(second, within)
    assert sum(node is within._tree for node in calls) == 1
    assert sum(node is first._tree for node in calls) == 1
    assert sum(node is second._tree for node in calls) == 1

    second.simplify(within=within, store=DecisionStore())
    assert sum(node is within._tree for node in calls) == 2


def test_unknown_owned_cost_is_completed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    universe = engine.parse('sys_platform == "linux" or sys_platform == "win32"')
    store = engine.Memo()
    rows = engine._decompose_rows(universe, store)
    assert store.row_work is not None
    assert store.row_work.units is None
    calls = _walks(monkeypatch)
    monkeypatch.setattr(engine._work_meter, "remaining", 10_000, raising=False)
    units = engine._charge_universe_work(universe, store)
    assert engine._decompose_rows(universe, store, raw_work=units) is rows
    record = store.row_work
    assert engine._charge_universe_work(universe, store) == units
    assert store.row_work is record
    assert calls == [universe]
    assert engine._work_meter.remaining == 10_000 - 2 * units


def test_equal_distinct_and_older_keys_are_conservative_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = engine.parse('sys_platform == "linux" or sys_platform == "win32"')
    equal = engine.parse('sys_platform == "linux" or sys_platform == "win32"')
    recent = engine.parse('os_name == "posix"')
    store = engine.Memo()
    calls = _walks(monkeypatch)
    monkeypatch.setattr(engine._work_meter, "remaining", 100_000, raising=False)
    units = engine._charge_universe_work(first, store)
    rows = engine._decompose_rows(first, store, raw_work=units)
    record = store.row_work
    assert equal.key() == first.key()
    assert equal.key() is not first.key()
    assert engine._decompose_rows(equal, store, raw_work=units) is rows
    assert store.row_work is record
    assert engine._charge_universe_work(equal, store) == units

    recent_units = engine._charge_universe_work(recent, store)
    engine._decompose_rows(recent, store, raw_work=recent_units)
    record = store.row_work
    assert engine._decompose_rows(first, store, raw_work=units) is rows
    assert store.row_work is record
    assert engine._charge_universe_work(first, store) == units
    assert calls == [first, equal, recent, first]


def test_raw_work_counts_repeated_subtrees(monkeypatch: pytest.MonkeyPatch) -> None:
    leaf = engine.parse('sys_platform == "linux"')
    assert isinstance(leaf, engine.AtomLeaf)
    universe = engine.AndNode((leaf, leaf))
    expected = 3 + 2 * (
        1
        + sum(
            len(value)
            for value in engine._atom_key(leaf.atom)
            if isinstance(value, str)
        )
    )
    store = engine.Memo()
    monkeypatch.setattr(engine._work_meter, "remaining", 10_000, raising=False)
    assert engine._charge_universe_work(universe, store) == expected
    engine._decompose_rows(universe, store, raw_work=expected)
    assert engine._charge_universe_work(universe, store) == expected
    assert engine._work_meter.remaining == 10_000 - 2 * expected


@pytest.mark.parametrize("units", [(1 << 64) - 1, 1 << 64])
def test_retained_work_cost_has_a_bounded_integer_payload(units: int) -> None:
    universe = engine.parse('sys_platform == "linux"')
    store = engine.Memo()
    engine._decompose_rows(universe, store, raw_work=units)
    assert store.row_work is not None
    assert store.row_work.units == (units if units.bit_length() <= 64 else None)


def test_failed_raw_walk_never_builds_a_key_or_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = engine.parse('sys_platform == "linux" or sys_platform == "win32"')
    store = engine.Memo()
    monkeypatch.setattr(engine._work_meter, "remaining", 0, raising=False)
    with pytest.raises(engine.IntractableMarkerSet, match="work exceeds"):
        engine._charge_universe_work(universe, store)
    assert universe._key is None
    assert store.row_work is None
    assert store.rows == {}


def test_failed_row_preparation_preserves_the_last_owned_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = engine.Memo()
    engine._decompose_rows(engine.parse('os_name == "posix"'), store, raw_work=100)
    record = store.row_work

    def failed(_node: engine.Formula) -> dict[str, object]:
        raise ValueError("row preparation failed")

    monkeypatch.setattr(engine, "_row_pins", failed)
    with pytest.raises(ValueError, match="row preparation failed"):
        engine._decompose_rows(
            engine.parse('sys_platform == "linux"'), store, raw_work=200
        )
    assert store.row_work is record
    assert len(store.rows) == 1


@pytest.mark.parametrize("constant_source", [False, True])
def test_cached_work_keeps_budget_failures_and_outer_meter(
    monkeypatch: pytest.MonkeyPatch, *, constant_source: bool
) -> None:
    universe = engine.parse('sys_platform == "linux" or sys_platform == "win32"')
    source = engine.TRUE if constant_source else engine.parse('sys_platform == "linux"')
    store = engine.Memo()
    monkeypatch.setattr(engine._work_meter, "remaining", 100_000, raising=False)
    source_start = engine._work_meter.remaining
    engine._charge_formula_work(source)
    source_units = source_start - engine._work_meter.remaining
    universe_units = engine._charge_universe_work(universe, store)
    engine._decompose_rows(universe, store, raw_work=universe_units)
    for actual_store in (store, engine.Memo()):
        engine._work_meter.remaining = 321
        with pytest.raises(engine.IntractableMarkerSet, match="work exceeds"):
            engine.simplify_within(
                source,
                universe,
                100_000,
                source_units + universe_units - 1,
                actual_store,
            )
        assert engine._work_meter.remaining == 321


def test_constant_sources_still_precharge_an_uncached_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = engine.parse('sys_platform == "linux"')
    store = engine.Memo()
    calls = _walks(monkeypatch)
    monkeypatch.setattr(engine._work_meter, "remaining", None, raising=False)
    assert (
        engine.simplify_within(engine.TRUE, universe, 100_000, 100_000, store)
        is engine.TRUE
    )
    assert calls == [engine.TRUE, universe]
    assert store.rows == {}
    assert store.row_work is None
    assert engine._work_meter.remaining is None
