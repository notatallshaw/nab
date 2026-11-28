"""A dependency clause reuses the singleton already recorded for its decision."""

from __future__ import annotations

from typing import Any

import pytest

from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.report import format_error
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


def test_self_dependency_rules_out_the_incompatible_part_of_the_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = {v: {"foo": Range.less_than(2)} for v in (1, 2, 3)}
    provider = DictProvider({"foo": versions})
    monkeypatch.setattr(provider, "widen_decision", lambda *_args: Range.full())
    resolver = Resolver(provider, root_version=0)
    singleton = Range.singleton
    constructions: list[int] = []

    def record_singleton(cls: Any, version: int) -> Range[int]:
        constructions.append(version)
        return singleton(version)

    monkeypatch.setattr(Range, "singleton", classmethod(record_singleton))

    assert resolver.resolve({"foo": Range.full()}) == {"foo": 1}
    assert resolver.stats.conflicts == 1
    assert constructions.count(3) == 1
    assert 2 not in constructions
    assert constructions.count(1) == 1


def test_widened_self_dependency_reports_the_observed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = DictProvider({"foo": {3: {"foo": Range.at_most(1)}}})
    monkeypatch.setattr(
        provider, "widen_decision", lambda *_args: Range.greater_than(1)
    )
    resolver = Resolver(provider)

    with pytest.raises(ResolutionError) as failure:
        resolver.resolve({"foo": Range.full()})

    assert "foo 3 depends on foo (-inf, 1]" in str(failure.value)
    assert "all selectable releases in foo (1, +inf)" in str(failure.value)
    proof = failure.value.incompatibility
    assert proof is not None
    assert "foo 3 depends on foo (-inf, 1]" in format_error(proof)
