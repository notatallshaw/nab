"""Count early assumptions while preserving deferred source discovery."""

from collections.abc import Iterable, Mapping, Sequence

import pytest

from nab_resolver import decide
from nab_resolver._compat import override
from nab_resolver.candidate_provider import (
    CandidateProvider,
    CandidateRequirement,
    PreparedCandidate,
)
from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver
from nab_resolver.types import IncompatibilityCause, RangeProtocol


class MetadataProbeError(Exception):
    """Represent a host failure during a diagnostic candidate probe."""


class LateRootHost:
    """Make the first root satisfiable only after another root declares its source."""

    def __init__(self) -> None:
        self.index = PreparedCandidate(1, object())
        self.linked = PreparedCandidate(3, object())
        self.application = PreparedCandidate(1, object())
        self.url_origin = object()
        self.discovered = False
        self.dependency_queries = 0
        self.fail_probe = False

    def generation(self) -> int:
        """Advance when application metadata introduces its explicit source."""
        return int(self.discovered)

    def iter_candidates(
        self,
        package: int,
        allowed: RangeProtocol[int],
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> Iterable[PreparedCandidate[int]]:
        if package == 20:
            yield self.application
            return
        self.dependency_queries += 1
        if self.fail_probe and self.dependency_queries == 2:
            raise MetadataProbeError
        if any(cause.origin is self.url_origin for cause in requirements.get(10, ())):
            yield self.linked
        yield self.index

    def get_dependencies(
        self, candidate: PreparedCandidate[int]
    ) -> Iterable[CandidateRequirement[int, int]]:
        if candidate is self.application:
            self.discovered = True
            yield CandidateRequirement(10, Range.singleton(3), self.url_origin)

    def priority(
        self,
        package: int,
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> int:
        return package


def roots() -> list[CandidateRequirement[int, int]]:
    """Request the unavailable dependency before the application that supplies it."""
    return [
        CandidateRequirement(10, Range.at_least(2), object()),
        CandidateRequirement(20, Range.full(), object()),
    ]


def test_first_root_absence_is_counted_before_any_real_decision() -> None:
    host = LateRootHost()
    provider = CandidateProvider(host, roots())
    attempt = Resolver(
        provider, availability_generation=host.generation, provisional=True
    )
    with pytest.raises(ResolutionError):
        attempt.solve(provider.root_requirements())
    assert attempt.provisional_absences == 1
    assert not host.discovered

    fallback = CandidateProvider(host, roots())
    solution = Resolver(fallback, availability_generation=host.generation).solve(
        fallback.root_requirements()
    )
    assert solution.pins == {10: 3, 20: 1}
    assert fallback.validate_solution(solution)


class DeferredProvider(CandidateProvider[int, int]):
    """Decline provisional source assumptions while retaining ordinary readiness."""

    @override
    def is_query_ready(self, package: int) -> bool:
        return BaseProvider.is_query_ready(self, package)


def test_declined_query_readiness_retains_source_deferral() -> None:
    host = LateRootHost()
    provider = DeferredProvider(host, roots())
    attempt = Resolver(
        provider, availability_generation=host.generation, provisional=True
    )
    solution = attempt.solve(provider.root_requirements())
    assert solution.pins == {10: 3, 20: 1}
    assert provider.validate_solution(solution)
    assert attempt.provisional_absences == 0


def test_probe_failure_retains_the_early_assumption_count() -> None:
    host = LateRootHost()
    host.fail_probe = True
    provider = CandidateProvider(host, roots())
    attempt = Resolver(
        provider, availability_generation=host.generation, provisional=True
    )
    with pytest.raises(MetadataProbeError):
        attempt.solve(provider.root_requirements(), {10: Range.less_than(2)})
    assert attempt.provisional_absences == 1
    assert not host.discovered


def test_an_exhausted_ready_query_records_a_provisional_absence() -> None:
    host = LateRootHost()
    provider = CandidateProvider(host, roots())
    attempt = Resolver(
        provider, availability_generation=host.generation, provisional=True
    )
    attempt._reset(None)
    attempt._add_root_requirements(provider.root_requirements())
    assert provider.choose_version(10, Range.at_least(2)) is None

    decide.record_contextual_no_versions(attempt, 10)

    assert attempt.provisional_absences == 1
    absence = attempt.incompatibilities[-1]
    assert absence.cause is IncompatibilityCause.NO_VERSIONS
    assert len(absence.terms) == 1
    assert absence.terms[0].package == 10
