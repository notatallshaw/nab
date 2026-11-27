"""A dependency clause reuses the singleton already recorded for its decision."""

from __future__ import annotations

from typing import Any

import pytest

from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver

from .solver_fixtures import DictProvider, FlaggedRange


@pytest.mark.parametrize("range_name", ["Range", "FlaggedRange"])
@pytest.mark.parametrize("dependency", [None, "bar", "foo"])
@pytest.mark.parametrize("widen", [False, True])
def test_decision_constructs_one_singleton(
    monkeypatch: pytest.MonkeyPatch,
    range_name: str,
    dependency: str | None,
    widen: bool,
) -> None:
    range_type: Any = Range if range_name == "Range" else FlaggedRange
    dependencies = {} if dependency is None else {dependency: range_type.full()}
    provider = DictProvider({"foo": {2: dependencies}, "bar": {1: {}}})
    widened = range_type.full() if widen else None
    monkeypatch.setattr(provider, "widen_decision", lambda *_args: widened)
    resolver = Resolver(provider, range_type=range_type, root_version=0)
    singleton = range_type.singleton
    constructions: list[int] = []

    def record_singleton(cls: Any, version: int) -> Any:
        constructions.append(version)
        return singleton(version)

    monkeypatch.setattr(range_type, "singleton", classmethod(record_singleton))
    result = resolver.resolve({"foo": range_type.full()})

    assert result == ({"foo": 2, "bar": 1} if dependency == "bar" else {"foo": 2})
    # The virtual root and foo each construct their own singleton.
    assert constructions.count(0) == 1
    assert constructions.count(2) == 1


def test_rejected_self_dependency_keeps_the_exact_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = DictProvider({"foo": {2: {"foo": Range.less_than(2)}, 1: {}}})
    # Only version 2 has dependencies. Widening must not make its self-conflict
    # rule out version 1, which is still a valid solution.
    monkeypatch.setattr(provider, "widen_decision", lambda *_args: Range.full())
    resolver = Resolver(provider, root_version=0)
    singleton = Range.singleton
    constructions: list[int] = []

    def record_singleton(cls: Any, version: int) -> Range[int]:
        constructions.append(version)
        return singleton(version)

    monkeypatch.setattr(Range, "singleton", classmethod(record_singleton))

    assert resolver.resolve({"foo": Range.full()}) == {"foo": 1}
    assert resolver.stats.backjumps > 0
    assert constructions.count(2) == 1
    assert constructions.count(1) == 1
