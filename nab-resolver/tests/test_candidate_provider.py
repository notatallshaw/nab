"""Exercise host adaptation without packaging types or package-name strings."""

from collections.abc import Iterable, Mapping, Sequence

import pytest

from nab_resolver.candidate_provider import (
    CandidateProvider,
    CandidateRequirement,
    PreparedCandidate,
)
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.types import RangeProtocol


class MemoryHost:
    """Serve integer package identifiers and opaque string candidate keys."""

    def __init__(self) -> None:
        self.app = PreparedCandidate("app-one", object())
        self.dep = PreparedCandidate("dep-one", object())
        self.request = CandidateRequirement(20, Range.singleton("dep-one"), object())

    def iter_candidates(
        self,
        package: int,
        allowed: RangeProtocol[str],
        requirements: Mapping[int, Sequence[CandidateRequirement[int, str]]],
    ) -> Iterable[PreparedCandidate[str]]:
        if package == 10:
            yield self.app
        elif package == 20:
            yield self.dep

    def get_dependencies(
        self, candidate: PreparedCandidate[str]
    ) -> Iterable[CandidateRequirement[int, str]]:
        if candidate is self.app:
            yield self.request

    def priority(
        self,
        package: int,
        requirements: Mapping[int, Sequence[CandidateRequirement[int, str]]],
    ) -> int:
        return package


def test_host_selection_retains_original_provenance() -> None:
    host = MemoryHost()
    origin = object()
    provider = CandidateProvider(host, [CandidateRequirement(10, Range.full(), origin)])
    resolver = Resolver(provider, root_version="root")
    solution = resolver.solve(provider.root_requirements())
    assert solution.pins == {10: "app-one", 20: "dep-one"}
    assert provider.root_requirements()[0].origin is origin
    assert provider.prepared(10, "app-one") is host.app
    assert provider.causes_for(10, "app-one") == (host.request,)
    assert provider.causes_for(0, "missing") == ()
    assert dict(provider.recorded_candidates()) == {10: host.app, 20: host.dep}


def test_probe_does_not_record_selection() -> None:
    provider = CandidateProvider(
        MemoryHost(), [CandidateRequirement(20, Range.full(), object())]
    )
    assert provider.has_satisfying_version(20, Range.singleton("dep-one"))
    assert not provider.has_satisfying_version(20, Range.singleton("dep-two"))
    assert list(provider.recorded_candidates()) == []
    assert provider.choose_version(20, Range.singleton("dep-two")) is None


def test_cached_dependencies_do_not_survive_their_selected_parent() -> None:
    host = MemoryHost()
    provider = CandidateProvider(
        host, [CandidateRequirement(10, Range.full(), object())]
    )
    provider.choose_version(10, Range.full())
    provider.get_dependencies(10, "app-one")
    assert 20 not in provider.active_requirements()
    provider.receive_partial_solution_hint({}, {10: "app-one"})
    assert provider.active_requirements()[20] == (host.request,)
    provider.receive_partial_solution_hint({}, {})
    assert 20 not in provider.active_requirements()
    assert provider.causes_for(10, "app-one") == (host.request,)


def test_contradictory_dependencies_stop_further_host_work() -> None:
    class ContradictoryHost(MemoryHost):
        """Refuse work after contradictory declarations invalidate a candidate."""

        def get_dependencies(
            self, candidate: PreparedCandidate[str]
        ) -> Iterable[CandidateRequirement[int, str]]:
            yield self.request
            yield CandidateRequirement(20, Range.singleton("dep-two"), object())
            raise AssertionError("unreachable dependency was materialized")

    host = ContradictoryHost()
    provider = CandidateProvider(
        host, [CandidateRequirement(10, Range.full(), object())]
    )
    provider.choose_version(10, Range.full())
    assert provider.get_dependencies(10, "app-one") == {20: Range.empty()}
    assert len(provider.causes_for(10, "app-one")) == 2


def test_cached_requirement_view_is_immutable_and_tracks_new_dependencies() -> None:
    host = MemoryHost()
    provider = CandidateProvider(
        host, [CandidateRequirement(10, Range.full(), object())]
    )
    provider.choose_version(10, Range.full())
    provider.receive_partial_solution_hint({}, {10: "app-one"})
    before = provider.active_requirements()
    assert 20 not in before
    with pytest.raises(TypeError):
        before[20] = (host.request,)

    provider.get_dependencies(10, "app-one")
    after = provider.active_requirements()
    assert after[20] == (host.request,)
    assert 20 not in before
    assert provider.active_requirements() is after

    provider.receive_partial_solution_hint({}, {})
    assert 20 not in provider.active_requirements()
    assert after[20] == (host.request,)
