"""Check direct collection and fresh results for atomic formulas."""

from __future__ import annotations

import pytest

from nab_markersets import _markersets as engine


@pytest.mark.parametrize(
    "node",
    [engine.TRUE, engine.FALSE, engine.parse('sys_platform == "linux"')],
    ids=["true", "false", "atom"],
)
def test_atomic_collection_avoids_tree_dispatch(
    monkeypatch: pytest.MonkeyPatch, node: engine.Formula
) -> None:
    calls: list[engine.Formula] = []
    original = engine._walk

    def walk(tree: engine.Formula, atoms: list[engine.Atom]) -> None:
        calls.append(tree)
        original(tree, atoms)

    monkeypatch.setattr(engine, "_walk", walk)
    expected = [node.atom] if isinstance(node, engine.AtomLeaf) else []
    assert engine.collect_atoms(node) == expected
    assert calls == []


@pytest.mark.parametrize(
    "node",
    [engine.TRUE, engine.FALSE, engine.parse('sys_platform == "linux"')],
    ids=["true", "false", "atom"],
)
def test_atomic_collection_returns_a_fresh_list(node: engine.Formula) -> None:
    extra = engine.collect_atoms(engine.parse('os_name == "posix"'))[0]
    first = engine.collect_atoms(node)
    expected = first.copy()
    first.append(extra)

    second = engine.collect_atoms(node)
    assert second == expected
    assert second is not first


def test_compound_collection_keeps_order_and_occurrences() -> None:
    left = engine.parse('sys_platform == "linux"')
    right = engine.parse('os_name == "posix"')
    assert isinstance(left, engine.AtomLeaf)
    assert isinstance(right, engine.AtomLeaf)
    node = engine.AndNode(
        (
            engine.TRUE,
            left,
            engine.NotNode(right),
            engine.OrNode((left, engine.FALSE, right)),
        )
    )

    assert engine.collect_atoms(node) == [left.atom, right.atom, left.atom, right.atom]
