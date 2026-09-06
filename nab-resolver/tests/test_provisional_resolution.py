"""Validate provisional plans against a host with requirement-scoped sources."""

from collections.abc import Iterable, Mapping, Sequence

import pytest

from nab_resolver._compat import override
from nab_resolver.candidate_provider import (
    CandidateProvider,
    CandidateRequirement,
    PreparedCandidate,
)
from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver, ResolverObserver, Solution
from nab_resolver.types import IncompatibilityCause, RangeProtocol


class SourceHost:
    """Keep discovered sources cached but require their original declaration."""

    def __init__(self) -> None:
        self.high = PreparedCandidate(2, object())
        self.low = PreparedCandidate(1, object())
        self.index = PreparedCandidate(1, object())
        self.linked = PreparedCandidate(3, object())
        self.reveal = PreparedCandidate(1, object())
        self.url_origin = object()
        self.index_origin = object()
        self.registered_url = False
        self.reads: list[PreparedCandidate[int]] = []
        self.queries: list[tuple[int, bool]] = []

    def generation(self) -> int:
        return int(self.registered_url)

    def iter_candidates(
        self,
        package: int,
        allowed: RangeProtocol[int],
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> Iterable[PreparedCandidate[int]]:
        declared_url = any(
            cause.origin is self.url_origin for cause in requirements.get(20, ())
        )
        self.queries.append((package, declared_url))
        if package == 10:
            yield self.high
            yield self.low
        elif package == 20:
            if self.registered_url and declared_url:
                yield self.linked
            yield self.index
        elif package == 30:
            yield self.reveal

    def get_dependencies(
        self, candidate: PreparedCandidate[int]
    ) -> Iterable[CandidateRequirement[int, int]]:
        self.reads.append(candidate)
        if candidate is self.high:
            yield CandidateRequirement(20, Range.at_least(2), self.index_origin)
        elif candidate is self.low or candidate is self.reveal:
            self.registered_url = True
            yield CandidateRequirement(20, Range.singleton(3), self.url_origin)

    def priority(
        self,
        package: int,
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> int:
        return package


class ContextOnlyProvider(CandidateProvider[int, int]):
    """Keep deferred-query handling when the host declines early assumptions."""

    @override
    def is_query_ready(self, package: int) -> bool:
        return False


def provider_for(
    host: SourceHost, *, pinned: bool = False
) -> CandidateProvider[int, int]:
    """Start with a fresh requirement view over reusable host metadata."""
    bounds = Range.singleton(1) if pinned else Range.full()
    return CandidateProvider(host, [CandidateRequirement(10, bounds, object())])


def solved_provider() -> tuple[
    SourceHost, CandidateProvider[int, int], Solution[int, int]
]:
    """Select a URL-backed dependency through its declaring application."""
    host = SourceHost()
    provider = provider_for(host, pinned=True)
    solution = Resolver(provider, availability_generation=host.generation).solve(
        provider.root_requirements()
    )
    assert solution.pins == {10: 1, 20: 3}
    return host, provider, solution


def changed_pins(
    solution: Solution[int, int], pins: dict[int, int]
) -> Solution[int, int]:
    return Solution(pins=pins, roots=solution.roots, edges=solution.edges)


def test_provisional_failure_retries_without_cached_source_admission() -> None:
    host = SourceHost()
    first = provider_for(host)
    provisional = Resolver(
        first, availability_generation=host.generation, provisional=True
    )
    with pytest.raises(ResolutionError):
        provisional.solve(first.root_requirements())
    assert provisional.provisional_absences > 0
    assert host.registered_url

    complete = provider_for(host)
    assert complete.choose_version(20, Range.singleton(3)) is None
    fallback = Resolver(complete, availability_generation=host.generation)
    solution = fallback.solve(complete.root_requirements())
    assert solution.pins == {10: 1, 20: 3}
    assert complete.validate_solution(solution)
    assert fallback.provisional_absences == 0
    assert (20, False) in host.queries
    assert (20, True) in host.queries


def test_provisional_keeps_deferral_before_exhaustion() -> None:
    host = SourceHost()
    provider = ContextOnlyProvider(
        host,
        [
            CandidateRequirement(10, Range.singleton(2), object()),
            CandidateRequirement(30, Range.full(), object()),
        ],
    )
    resolver = Resolver(
        provider,
        availability_generation=host.generation,
        provisional=True,
    )
    solution = resolver.solve(provider.root_requirements())
    assert solution.pins == {10: 2, 30: 1, 20: 3}
    assert provider.validate_solution(solution)
    assert resolver.provisional_absences == 0
    assert (20, False) in host.queries


def test_validation_reads_complete_host_declarations() -> None:
    host, provider, solution = solved_provider()
    before = len(host.reads)
    assert provider.validate_solution(solution)
    assert host.reads[before:] == [host.low, host.linked]
    assert not provider.validate_solution(changed_pins(solution, {10: 1}))


def test_validation_rejects_missing_unprepared_and_unreachable_pins() -> None:
    _, provider, solution = solved_provider()
    for pins in ({20: 3}, {10: 99, 20: 3}, {10: 1, 20: 3, 99: 1}):
        assert not provider.validate_solution(changed_pins(solution, pins))


def test_validation_preserves_original_url_admission() -> None:
    host = SourceHost()
    provider = provider_for(host)
    solution = Resolver(provider, availability_generation=host.generation).solve(
        provider.root_requirements()
    )
    assert host.registered_url
    assert not provider.validate_solution(changed_pins(solution, {10: 2, 20: 3}))
    assert host.queries[-1] == (20, False)


def test_validation_rejects_root_and_dependency_bounds() -> None:
    _, provider, solution = solved_provider()
    assert provider.choose_version(10, Range.singleton(2)) == 2
    assert not provider.validate_solution(changed_pins(solution, {10: 2, 20: 3}))

    assert provider.choose_version(20, Range.singleton(1)) == 1
    assert not provider.validate_solution(changed_pins(solution, {10: 1, 20: 1}))


def test_validation_checks_constraints_without_installing_unused_packages() -> None:
    _, provider, solution = solved_provider()
    assert provider.validate_solution(solution, {20: Range.singleton(3)})
    assert provider.validate_solution(solution, {99: Range.empty()})
    assert not provider.validate_solution(solution, {20: Range.singleton(1)})


def test_validation_empty_roots_need_no_host_queries() -> None:
    host = SourceHost()
    provider = CandidateProvider(host, [])
    solution = Resolver(provider).solve(provider.root_requirements())
    assert provider.validate_solution(solution)
    assert host.reads == []
    assert host.queries == []


def test_validation_retains_duplicate_declarations_and_cycles() -> None:
    class CyclicHost(SourceHost):
        @override
        def get_dependencies(
            self, candidate: PreparedCandidate[int]
        ) -> Iterable[CandidateRequirement[int, int]]:
            yield from super().get_dependencies(candidate)
            if candidate is self.low:
                yield CandidateRequirement(20, Range.full(), self.index_origin)
            elif candidate is self.linked:
                yield CandidateRequirement(10, Range.singleton(1), self.index_origin)

    host = CyclicHost()
    provider = provider_for(host, pinned=True)
    solution = Resolver(provider, availability_generation=host.generation).solve(
        provider.root_requirements()
    )
    assert provider.validate_solution(solution)


def test_validation_propagates_host_preparation_errors() -> None:
    class FailingHost(SourceHost):
        failed = False

        @override
        def get_dependencies(
            self, candidate: PreparedCandidate[int]
        ) -> Iterable[CandidateRequirement[int, int]]:
            if self.failed:
                raise OSError("metadata unavailable")
            yield from super().get_dependencies(candidate)

    host = FailingHost()
    provider = provider_for(host, pinned=True)
    solution = Resolver(provider, availability_generation=host.generation).solve(
        provider.root_requirements()
    )
    host.failed = True
    with pytest.raises(OSError, match="metadata unavailable"):
        provider.validate_solution(solution)


def test_provisional_absence_observer_precedes_priority_feedback() -> None:
    events = []

    class ObservedProvider(CandidateProvider[int, int]):
        @override
        def receive_contextual_failure(self, package: int) -> bool:
            events.append("feedback")
            return True

    class Observer(ResolverObserver[int, int]):
        @override
        def on_no_versions(
            self, package: int, version_range: RangeProtocol[int]
        ) -> None:
            events.append("absence")

    host = SourceHost()
    provider = ObservedProvider(host, provider_for(host).roots)
    resolver = Resolver(
        provider,
        observer=Observer(),
        availability_generation=host.generation,
        provisional=True,
        max_iterations=2,
    )
    with pytest.raises(ResolutionError, match="Resolution exceeded 2 iterations"):
        resolver.solve(provider.root_requirements())
    assert events == ["absence", "feedback"]
    assert any(
        item.cause is IncompatibilityCause.NO_VERSIONS
        for item in resolver.incompatibilities
    )


def test_provisional_constraint_failure_retains_its_diagnostic() -> None:
    host = SourceHost()
    provider = provider_for(host, pinned=True)
    constraint = Range.less_than(3)
    resolver = Resolver(
        provider,
        availability_generation=host.generation,
        provisional=True,
        max_iterations=2,
    )
    with pytest.raises(ResolutionError, match="Resolution exceeded 2 iterations"):
        resolver.solve(provider.root_requirements(), {20: constraint})
    assert resolver.provisional_absences == 1
    assert any(
        item.cause is IncompatibilityCause.CONSTRAINT
        and item.constraint_range == constraint
        for item in resolver.incompatibilities
    )


def test_unguarded_missing_root_does_not_make_a_provisional_assumption() -> None:
    host = SourceHost()
    provider = ContextOnlyProvider(
        host, [CandidateRequirement(20, Range.at_least(2), object())]
    )
    resolver = Resolver(
        provider,
        availability_generation=host.generation,
        provisional=True,
    )
    with pytest.raises(ResolutionError) as caught:
        resolver.solve(provider.root_requirements())
    assert caught.value.incompatibility is not None
    assert resolver.provisional_absences == 0
