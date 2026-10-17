"""Check bounded string truths during row restriction and equivalence."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from nab_markersets import _markersets as engine

if TYPE_CHECKING:
    from collections.abc import Mapping


def _row_walks(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[engine.Formula, engine.Memo]]:
    """Observe row-tree restrictions and their supplied stores."""
    original = engine._restrict_row_tree
    calls: list[tuple[engine.Formula, engine.Memo]] = []

    def restrict(
        node: engine.Formula, pins: Mapping[str, object], memo: engine.Memo
    ) -> engine.Formula:
        calls.append((node, memo))
        return original(node, pins, memo)

    monkeypatch.setattr(engine, "_restrict_row_tree", restrict)
    return calls


def _atom(text: str) -> engine.Atom:
    """Read one real predicate without preparing its scalar partition."""
    leaf = engine.parse(text)
    assert isinstance(leaf, engine.AtomLeaf)
    return leaf.atom


def _holds_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[engine.Atom, object]]:
    """Observe evaluations while retaining the actual predicate semantics."""
    calls: list[tuple[engine.Atom, object]] = []
    original = engine.Atom.holds

    def holds(atom: engine.Atom, point: object) -> bool:
        calls.append((atom, point))
        return original(atom, point)

    monkeypatch.setattr(engine.Atom, "holds", holds)
    return calls


@pytest.mark.parametrize("point", ["linux", "win32"])
def test_restriction_reuses_true_and_false_scalar_truths(
    monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    atom = _atom('sys_platform == "linux"')
    store = engine.Memo()
    expected = engine.TRUE if point == "linux" else engine.FALSE
    calls = _holds_calls(monkeypatch)
    for _ in range(2):
        assert (
            engine._restrict_cover_atom(atom, {"sys_platform": point}, store)
            is expected
        )
    assert calls == [(atom, point)]


def test_keys_distinguish_predicate_and_pin() -> None:
    linux = _atom('sys_platform == "linux"')
    other = _atom('sys_platform != "linux"')
    store = engine.Memo()
    assert engine._restrict_row_value(linux, {"sys_platform": "linux"}, store) is True
    assert engine._restrict_row_value(linux, {"sys_platform": "win32"}, store) is False
    assert engine._restrict_row_value(other, {"sys_platform": "linux"}, store) is False


def test_existing_partition_truth_survives_closed_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atom = _atom('sys_platform == "linux"')
    store = engine.Memo()
    assert engine._truth(atom, "win32", store) is False
    calls = _holds_calls(monkeypatch)
    monkeypatch.setattr(engine, "_RESTRICTION_TRUTH_MAX_ENTRIES", 0)
    monkeypatch.setattr(engine, "_RESTRICTION_TRUTH_MAX_CHARS", 0)
    assert engine._restrict_row_value(atom, {"sys_platform": "win32"}, store) is False
    assert calls == []


def test_misses_stop_at_entry_limit_and_existing_hits_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "_RESTRICTION_TRUTH_MAX_ENTRIES", 1)
    atom = _atom('sys_platform == "linux"')
    store = engine.Memo()
    calls = _holds_calls(monkeypatch)
    for point in ("linux", "win32", "win32", "linux"):
        engine._restrict_row_value(atom, {"sys_platform": point}, store)
    assert calls == [(atom, "linux"), (atom, "win32"), (atom, "win32")]
    assert len(store.truths) == 1


@pytest.mark.parametrize(("point", "evaluations"), [("x", 1), ("xx", 2)])
def test_operand_character_limit_counts_literal_and_pin(
    monkeypatch: pytest.MonkeyPatch, point: str, evaluations: int
) -> None:
    monkeypatch.setattr(engine, "_RESTRICTION_TRUTH_MAX_CHARS", 6)
    atom = _atom('sys_platform == "linux"')
    store = engine.Memo()
    calls = _holds_calls(monkeypatch)
    for _ in range(2):
        assert engine._restrict_row_value(atom, {"sys_platform": point}, store) is False
    assert len(calls) == evaluations


def test_unresolved_leaves_keep_their_identity() -> None:
    leaf = engine.parse('sys_platform == "linux"')
    store = engine.Memo()
    assert engine._restrict_row_tree(leaf, {}, store) is leaf
    assert store.truths == {}


@pytest.mark.parametrize(
    ("text", "environment"),
    [
        ('"linux" == sys_platform', {"sys_platform": "linux"}),
        ('"lin" in sys_platform', {"sys_platform": "linux"}),
        ('python_version == "3.11"', {"python_full_version": "3.11.2"}),
        ('implementation_version >= "3.11"', {"implementation_version": "3.12"}),
        ('"a" in extras', {"extras": {"a"}}),
        ('"x" in platform_release', {"platform_release": "x1"}),
    ],
)
def test_row_restriction_matches_generic_domains(text: str, environment: dict) -> None:
    leaf = engine.parse(text)
    assert isinstance(leaf, engine.AtomLeaf)
    store = engine.Memo()
    expected = engine.restrict_tree(leaf, environment)
    assert engine._restrict_row_tree(leaf, environment, store).key() == expected.key()
    assert (
        engine._restrict_cover_atom(leaf.atom, environment, store).key()
        == expected.key()
    )


def test_non_string_values_are_read_again() -> None:
    class Pin:
        """Expose a changed environment value on the next restriction."""

        value = "linux"

        def __str__(self) -> str:
            return self.value

    pin = Pin()
    atom = _atom('sys_platform == "linux"')
    store = engine.Memo()
    assert engine._restrict_row_value(atom, {"sys_platform": pin}, store) is True
    pin.value = "win32"
    assert engine._restrict_row_value(atom, {"sys_platform": pin}, store) is False
    assert store.truths == {}


def test_failed_comparison_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    atom = engine.Atom(engine.AXIS_VALUE, "sys_platform", "sys_platform", "~=", "linux")
    store = engine.Memo()
    calls = _holds_calls(monkeypatch)
    for _ in range(2):
        with pytest.raises(engine.UndefinedComparison):
            engine._restrict_row_value(atom, {"sys_platform": "linux"}, store)
    assert len(calls) == 2
    assert store.truths == {}


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"), reason="No runtime digit limit"
)
def test_version_restriction_still_reads_current_digit_limit() -> None:
    original = sys.get_int_max_str_digits()
    atom = _atom('python_full_version == "1.0"')
    point = "1." + "7" * 700
    store = engine.Memo()
    try:
        sys.set_int_max_str_digits(0)
        assert engine._truth(atom, point, store) is False
        sys.set_int_max_str_digits(640)
        with pytest.raises(ValueError, match="limit"):
            engine._restrict_row_value(atom, {"python_full_version": point}, store)
    finally:
        sys.set_int_max_str_digits(original)


@pytest.mark.parametrize("varying", [False, True])
def test_row_helpers_share_truths_without_changing_work(
    monkeypatch: pytest.MonkeyPatch, *, varying: bool
) -> None:
    atom = _atom('sys_platform == "linux"')
    row = engine._Row({"sys_platform": "linux"}, engine.TRUE)
    operation = engine._row_pattern_columns if varying else engine._fixed_row_pattern
    store = engine.Memo()
    calls = _holds_calls(monkeypatch)
    remaining = []
    results = []
    for _ in range(2):
        monkeypatch.setattr(engine._work_meter, "remaining", 100_000, raising=False)
        results.append(operation((atom,), row, 1000, store))
        remaining.append(engine._work_meter.remaining)
    assert results[0] == results[1]
    assert remaining[0] == remaining[1]
    assert calls == [(atom, "linux")]


def test_row_tree_preserves_negation_and_compounds() -> None:
    node = engine.make_or(
        (
            engine.make_not(
                engine.parse('sys_platform == "linux" and python_full_version < "3.12"')
            ),
            engine.parse('os_name == "nt"'),
        )
    )
    for pins in (
        {},
        {"sys_platform": "linux"},
        {"sys_platform": "win32", "os_name": "nt"},
    ):
        assert (
            engine._restrict_row_tree(node, pins, engine.Memo()).key()
            == engine.restrict_tree(node, pins).key()
        )


def test_cache_lifetime_belongs_to_the_supplied_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atom = _atom('sys_platform == "linux"')
    calls = _holds_calls(monkeypatch)
    for _ in range(2):
        store = engine.Memo()
        assert (
            engine._restrict_row_value(atom, {"sys_platform": "linux"}, store) is True
        )
    assert calls == [(atom, "linux"), (atom, "linux")]


def test_string_subclasses_keep_their_conversion_semantics() -> None:
    class Pin(str):
        """Return a changed value while retaining the same string key."""

        __slots__ = ("value",)

        def __init__(self, _text: str) -> None:
            self.value = "linux"

        def __str__(self) -> str:
            return self.value

    pin = Pin("linux")
    atom = _atom('sys_platform == "linux"')
    store = engine.Memo()
    assert engine._restrict_row_value(atom, {"sys_platform": pin}, store) is True
    pin.value = "win32"
    assert engine._restrict_row_value(atom, {"sys_platform": pin}, store) is False
    assert store.truths == {}


@pytest.mark.parametrize(
    "operation", ["_rows_equivalent", "_rows_within", "_rows_cover"]
)
def test_row_comparisons_use_the_shared_string_truths(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    store = engine.Memo()
    source = engine.parse('sys_platform == "linux"')
    row = engine._Row({"sys_platform": "linux"}, engine.TRUE)
    calls = _row_walks(monkeypatch)

    monkeypatch.setattr(engine._work_meter, "remaining", 100_000, raising=False)
    assert getattr(engine, operation)(source, [row], [engine.TRUE], 1000, store)
    assert calls == [(source, store)]


def test_equivalence_prepares_both_sides_with_the_same_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = engine.Memo()
    left = engine.parse('sys_platform == "linux"')
    right = engine.parse('sys_platform != "win32"')
    universe = engine.parse('sys_platform == "linux"')
    engine._decompose_rows(universe, store)
    calls = _row_walks(monkeypatch)

    monkeypatch.setattr(engine._work_meter, "remaining", 100_000, raising=False)
    assert engine.equivalent_within_rows(left, right, universe, 1000, store)
    assert calls == [(right, store), (left, store)]


def test_universe_decomposition_shares_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = engine.Memo()
    universe = engine.parse('sys_platform == "linux"')
    calls = _row_walks(monkeypatch)

    monkeypatch.setattr(engine._work_meter, "remaining", 100_000, raising=False)
    assert engine._decompose_rows(universe, store)
    assert [memo for _node, memo in calls] == [store]


def test_greedy_source_restriction_uses_the_row_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = engine.Memo()
    source = engine.parse('sys_platform == "linux"')
    assert isinstance(source, engine.AtomLeaf)
    row = engine._Row({"sys_platform": "linux"}, engine.TRUE)
    calls = _row_walks(monkeypatch)

    monkeypatch.setattr(engine._work_meter, "remaining", 100_000, raising=False)
    assert engine._simplify_greedy(source, [row], 1000, store) is engine.TRUE
    assert any(
        isinstance(node, engine.AtomLeaf) and node.atom is source.atom and memo is store
        for node, memo in calls
    )


@pytest.mark.parametrize("value", [False, True])
def test_row_restriction_preserves_constants(*, value: bool) -> None:
    node = engine.TRUE if value else engine.FALSE
    assert engine._restrict_row_tree(node, {}, engine.Memo()) is node
