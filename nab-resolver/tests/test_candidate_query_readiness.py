"""Keep query readiness tied to original declarations rather than discovered sources."""

from collections.abc import Iterable, Mapping, Sequence

from nab_resolver.candidate_provider import (
    CandidateProvider,
    CandidateRequirement,
    PreparedCandidate,
)
from nab_resolver.ranges import Range
from nab_resolver.types import RangeProtocol


class ScopedHost:
    """Cache one linked source while requiring its selected parent's declaration."""

    def __init__(self) -> None:
        self.parent = PreparedCandidate(1, object())
        self.index = PreparedCandidate(2, object())
        self.linked = PreparedCandidate(1, object())
        self.url = object()
        self.registered = False
        self.reads = 0

    def iter_candidates(
        self,
        package: int,
        allowed: RangeProtocol[int],
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> Iterable[PreparedCandidate[int]]:
        if package == 10:
            yield self.parent
        elif package == 20:
            if self.registered and any(
                cause.origin is self.url for cause in requirements.get(20, ())
            ):
                yield self.linked
            yield self.index

    def get_dependencies(
        self, candidate: PreparedCandidate[int]
    ) -> Iterable[CandidateRequirement[int, int]]:
        self.reads += 1
        if candidate is self.parent:
            self.registered = True
            yield CandidateRequirement(20, Range.singleton(1), self.url)

    def priority(
        self,
        package: int,
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> int:
        return package


def test_inferred_ranges_and_cached_sources_do_not_supply_original_requirements() -> (
    None
):
    host = ScopedHost()
    provider = CandidateProvider(
        host, [CandidateRequirement(10, Range.full(), object())]
    )
    assert provider.is_query_ready(10)
    assert not provider.is_query_ready(20)
    provider.receive_partial_solution_hint({20: Range.singleton(1)}, {})
    assert not provider.is_query_ready(20)

    assert provider.choose_version(10, Range.full()) == 1
    provider.get_dependencies(10, 1)
    assert host.registered
    assert not provider.is_query_ready(20)
    assert not provider.has_satisfying_version(20, Range.singleton(1))

    provider.receive_partial_solution_hint({10: Range.singleton(1)}, {10: 1})
    assert provider.is_query_ready(20)
    assert provider.has_satisfying_version(20, Range.singleton(1))
    provider.receive_partial_solution_hint({}, {})
    assert not provider.is_query_ready(20)
    assert not provider.has_satisfying_version(20, Range.singleton(1))


def test_prechecked_rejection_leaves_cached_url_ineligible() -> None:
    host = ScopedHost()
    provider = CandidateProvider(
        host,
        [CandidateRequirement(package, Range.full(), object()) for package in (10, 20)],
        dependency_precheck=True,
        precheck_feedback=True,
    )
    provider.receive_partial_solution_hint({20: Range.singleton(2)}, {20: 2})
    before = dict(provider.active_requirements())
    assert provider.choose_version(10, Range.singleton(1)) is None
    assert host.registered
    assert dict(provider.active_requirements()) == before
    assert provider.is_query_ready(20)
    reads = host.reads
    assert not provider.has_satisfying_version(20, Range.singleton(1))
    assert host.reads == reads
    assert len(provider.consume_pending_clauses()) == 1
    assert provider.consume_force_backtrack_targets() == []
