"""Exercise host adaptation without packaging types or package-name strings."""

from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from typing import Any, cast

import pytest

from nab_resolver.candidate_provider import (
    CandidateProvider,
    CandidateRequirement,
    PreparedCandidate,
)
from nab_resolver.decide import choose_package_to_decide
from nab_resolver.errors import ResolutionError
from nab_resolver.propagate import unit_propagation
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.root import ROOT
from nab_resolver.types import RangeProtocol


class MemoryHost:
    """Serve integer package identifiers and opaque string candidate keys."""

    def __init__(self) -> None:
        self.app = PreparedCandidate("app-one", object())
        self.dep = PreparedCandidate("dep-one", object())
        self.request = CandidateRequirement[int, str](
            20, Range.singleton("dep-one"), object()
        )

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


def test_duplicate_dependencies_intersect_and_reuse_host_metadata() -> None:
    class RepeatedHost(MemoryHost):
        """Count metadata reads while yielding two compatible restrictions."""

        reads = 0

        def get_dependencies(
            self, candidate: PreparedCandidate[str]
        ) -> Iterable[CandidateRequirement[int, str]]:
            self.reads += 1
            yield self.request
            yield CandidateRequirement(20, Range.full(), object())

    host = RepeatedHost()
    provider = CandidateProvider(
        host, [CandidateRequirement(10, Range.full(), object())]
    )
    provider.choose_version(10, Range.full())
    first = provider.get_dependencies(10, "app-one")
    assert first == {20: Range.singleton("dep-one")}
    assert len(provider.causes_for(10, "app-one")) == 2

    assert provider.get_dependencies(10, "app-one") is first
    assert host.reads == 1


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
        cast("MutableMapping[int, tuple[CandidateRequirement[int, str], ...]]", before)[
            20
        ] = (host.request,)

    provider.get_dependencies(10, "app-one")
    after = provider.active_requirements()
    assert after[20] == (host.request,)
    assert 20 not in before
    assert provider.active_requirements() is after

    provider.receive_partial_solution_hint({}, {})
    assert 20 not in provider.active_requirements()
    assert after[20] == (host.request,)


def test_query_feedback_prioritizes_current_generic_parents_then_targets() -> None:
    host = MemoryHost()
    provider = CandidateProvider(
        host, [CandidateRequirement(10, Range.full(), object())], query_feedback=True
    )
    provider.choose_version(10, Range.full())
    provider.get_dependencies(10, "app-one")
    provider.receive_partial_solution_hint({}, {10: "app-one"})

    assert provider.receive_contextual_failure(20)
    assert provider.prioritize(10, Range.full(), {}) == (-1, 0, 10)
    assert provider.prioritize(20, Range.full(), {}) == (0, -1, 20)

    provider.receive_partial_solution_hint({}, {})
    assert provider.receive_contextual_failure(20)
    assert provider.prioritize(10, Range.full(), {}) == (-1, 0, 10)
    assert provider.prioritize(20, Range.full(), {}) == (0, -2, 20)

    provider.begin_resolution()
    assert provider.prioritize(10, Range.full(), {}) == (0, 0, 10)
    assert provider.prioritize(20, Range.full(), {}) == (0, 0, 20)


def test_query_feedback_is_disabled_by_default() -> None:
    provider = CandidateProvider(MemoryHost(), [])
    provider.begin_resolution()
    assert provider.receive_contextual_failure(20) is False
    assert provider.prioritize(20, Range.full(), {}) == 20


def test_duplicate_declarations_credit_one_current_parent() -> None:
    class RepeatedHost(MemoryHost):
        """Retain two declarations of the same dependency from one candidate."""

        def get_dependencies(
            self, candidate: PreparedCandidate[str]
        ) -> Iterable[CandidateRequirement[int, str]]:
            yield self.request
            yield CandidateRequirement(20, Range.full(), object())

    provider = CandidateProvider(RepeatedHost(), [], query_feedback=True)
    provider.choose_version(10, Range.full())
    provider.get_dependencies(10, "app-one")
    provider.receive_partial_solution_hint({}, {10: "app-one"})
    provider.receive_contextual_failure(20)
    assert provider.prioritize(10, Range.full(), {}) == (-1, 0, 10)

    provider.receive_partial_solution_hint({}, {10: "another-key"})
    provider.receive_contextual_failure(20)
    assert provider.prioritize(10, Range.full(), {}) == (-1, 0, 10)
    assert provider.prioritize(20, Range.full(), {}) == (0, -2, 20)


def test_failed_solve_feedback_is_cleared_before_reuse() -> None:
    host = MemoryHost()
    host.dep = PreparedCandidate("dep-two", object())
    provider = CandidateProvider(
        host, [CandidateRequirement(10, Range.full(), object())], query_feedback=True
    )
    resolver = Resolver(
        provider, root_version="root", availability_generation=lambda: 0
    )
    with pytest.raises(ResolutionError):
        resolver.solve(provider.root_requirements())
    assert provider.prioritize(10, Range.full(), {})[:2] == (-1, 0)
    assert provider.prioritize(20, Range.full(), {})[:2] == (0, -1)

    host.dep = PreparedCandidate("dep-one", object())
    assert resolver.solve(provider.root_requirements()).pins == {
        10: "app-one",
        20: "dep-one",
    }
    assert provider.prioritize(10, Range.full(), {})[:2] == (0, 0)
    assert provider.prioritize(20, Range.full(), {})[:2] == (0, 0)


def test_disabled_feedback_preserves_an_opaque_host_key() -> None:
    priority = object()

    class OpaquePriorityHost(MemoryHost):
        """Return one opaque ordering key without preparing candidates."""

        def priority(
            self,
            package: int,
            requirements: Mapping[int, Sequence[CandidateRequirement[int, str]]],
        ) -> Any:
            return priority

    provider = CandidateProvider(OpaquePriorityHost(), [])
    assert provider.prioritize(10, Range.full(), {}) is priority
    assert provider.receive_contextual_failure(10) is False
    assert provider.prioritize(10, Range.full(), {}) is priority


def test_feedback_prefers_a_declaring_parent_after_backtrack() -> None:
    provider = CandidateProvider(
        MemoryHost(),
        [
            CandidateRequirement(10, Range.full(), object()),
            CandidateRequirement(20, Range.full(), object()),
        ],
        query_feedback=True,
    )
    resolver = Resolver(provider, root_version="root")
    resolver._reset(None)
    resolver._add_root_requirements(provider.root_requirements())
    assert unit_propagation(resolver, ROOT) is None
    assert provider.choose_version(10, Range.full()) == "app-one"
    provider.get_dependencies(10, "app-one")
    resolver.solution.decide(10, "app-one")
    provider.receive_partial_solution_hint(
        resolver.solution.positive_ranges(), resolver.solution.decisions()
    )
    assert provider.receive_contextual_failure(20)

    resolver.solution.backtrack(1)
    provider.receive_partial_solution_hint(
        resolver.solution.positive_ranges(), resolver.solution.decisions()
    )

    assert choose_package_to_decide(resolver) == 10
