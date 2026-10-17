"""Check identical row constants without losing decision or budget behavior."""

from __future__ import annotations

from itertools import product

import pytest

from nab_markersets import _markersets as engine


@pytest.fixture(autouse=True)
def _meter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each row comparison an independent work meter."""
    monkeypatch.setattr(engine._work_meter, "remaining", None, raising=False)


def test_restricted_constants_use_one_empty_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left = engine.parse('sys_platform == "linux"')
    right = engine.parse('sys_platform != "win32"')
    universe = engine.parse('sys_platform == "linux"')
    calls: list[engine.Formula] = []
    original = engine.is_empty

    def empty(
        node: engine.Formula, max_cells: int, store: engine.Memo | None = None
    ) -> bool:
        calls.append(node)
        return original(node, max_cells, store)

    monkeypatch.setattr(engine, "is_empty", empty)
    assert engine.equivalent_within_rows(left, right, universe, 1000, engine.Memo())
    assert calls == [engine.FALSE]


@pytest.mark.parametrize(
    ("left_value", "right_value", "bound_value"),
    tuple(product((False, True), repeat=3)),
)
def test_constant_rows_keep_their_truth_table(
    *, left_value: bool, right_value: bool, bound_value: bool
) -> None:
    left = engine.TRUE if left_value else engine.FALSE
    right = engine.TRUE if right_value else engine.FALSE
    bound = engine.TRUE if bound_value else engine.FALSE
    row = engine._Row({}, bound)

    actual = engine._rows_equivalent(left, [row], [right], 1000, engine.Memo())
    assert actual is (not bound_value or left_value == right_value)


@pytest.mark.parametrize("value", [False, True])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("remaining", [None, 0, 9, -1])
def test_identical_constants_preserve_work_and_refusal(
    monkeypatch: pytest.MonkeyPatch,
    *,
    value: bool,
    warm: bool,
    remaining: int | None,
) -> None:
    store = engine.Memo()
    if warm:
        assert engine.is_empty(engine.FALSE, 1000, store)
    monkeypatch.setattr(engine._work_meter, "remaining", remaining)
    node = engine.TRUE if value else engine.FALSE
    row = engine._Row({}, engine.TRUE)

    if remaining is not None and remaining < 0:
        with pytest.raises(engine.IntractableMarkerSet):
            engine._rows_equivalent(node, [row], [node], 1000, store)
    else:
        assert engine._rows_equivalent(node, [row], [node], 1000, store)
    assert engine._work_meter.remaining == remaining


def test_identical_constants_retain_the_zero_work_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = engine.Memo()
    row = engine._Row({}, engine.TRUE)
    assert engine._rows_equivalent(engine.TRUE, [row], [engine.TRUE], 1000, store)

    monkeypatch.setattr(engine._work_meter, "remaining", -1)
    with pytest.raises(engine.IntractableMarkerSet):
        engine.is_empty(engine.FALSE, 1000, store)


def test_nonconstant_identity_keeps_partition_guards() -> None:
    source = engine.parse('python_full_version < "3.12"')
    row = engine._Row({}, engine.TRUE)
    with pytest.raises(engine.IntractableMarkerSet):
        engine._rows_equivalent(source, [row], [source], 1, engine.Memo())


def test_identical_constants_do_not_skip_later_rows() -> None:
    source = engine.parse('sys_platform == "linux"')
    rows = [
        engine._Row({"sys_platform": "win32"}, engine.TRUE),
        engine._Row({"sys_platform": "linux"}, engine.TRUE),
    ]
    assert not engine._rows_equivalent(
        source, rows, [engine.FALSE, engine.FALSE], 1000, engine.Memo()
    )
