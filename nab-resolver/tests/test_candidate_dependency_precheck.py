"""Precheck real host declarations without deciding or activating rejected candidates."""

from collections.abc import Iterable, Mapping, Sequence
from typing import cast

import pytest

from nab_resolver import conflict
from nab_resolver.candidate_provider import (
    CandidateProvider,
    CandidateRequirement,
    PreparedCandidate,
)
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.types import IncompatibilityCause, RangeProtocol


class Host:
    """Keep metadata fixed while an earlier hub choice blocks newer parents."""

    def __init__(self) -> None:
        self.catalog = {
            10: [PreparedCandidate(version, (10, version)) for version in (2, 1)],
            20: [
                PreparedCandidate(version, (20, version))
                for version in range(16, 0, -1)
            ],
            30: [
                PreparedCandidate(version, (30, version))
                for version in range(16, 0, -1)
            ],
        }
        self.events: list[str] = []
        self.self_requirement: Range[int] | None = None
        self.fail_after_dependency = False
        self.required = Range.less_than(2)

    def iter_candidates(
        self,
        package: int,
        allowed: RangeProtocol[int],
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> Iterable[PreparedCandidate[int]]:
        yield from self.catalog.get(package, ())

    def get_dependencies(
        self, candidate: PreparedCandidate[int]
    ) -> Iterable[CandidateRequirement[int, int]]:
        package, _ = cast("tuple[int, int]", candidate.origin)
        self.events.append(f"metadata:{package}")
        if package in (20, 30):
            self.events.append("child")
            yield CandidateRequirement(10, self.required, "child origin")
            if self.self_requirement is not None:
                self.events.append("self")
                yield CandidateRequirement(
                    package, self.self_requirement, "self origin"
                )
            if self.fail_after_dependency:
                self.events.append("error")
                raise OSError("late metadata error")

    def priority(
        self,
        package: int,
        requirements: Mapping[int, Sequence[CandidateRequirement[int, int]]],
    ) -> int:
        return package


def prepared_provider(
    host: Host, *, feedback: bool = False
) -> CandidateProvider[int, int]:
    """Supply the real hint snapshots a selected hub would leave behind."""
    provider = CandidateProvider(
        host,
        [CandidateRequirement(20, Range.full(), "root")],
        dependency_precheck=True,
        precheck_feedback=feedback,
    )
    provider.receive_partial_solution_hint({10: Range.singleton(2)}, {10: 2})
    return provider


def reject(
    provider: CandidateProvider[int, int], key: int, *, package: int = 20
) -> list[int]:
    """Reject one candidate, preserving its exact clause before consuming feedback."""
    before = dict(provider.active_requirements())
    assert provider.choose_version(package, Range.singleton(key)) is None
    assert dict(provider.active_requirements()) == before
    clauses = provider.consume_pending_clauses()
    assert len(clauses) == 1
    assert clauses[0].cause is IncompatibilityCause.DEPENDENCY
    assert clauses[0].terms[0].package == package
    assert clauses[0].terms[0].constraint == Range.singleton(key)
    assert clauses[0].terms[1].package == 10
    assert not clauses[0].terms[1].is_positive()
    assert provider.consume_pending_clauses() == []
    return provider.consume_force_backtrack_targets()


def test_precheck_clause_and_probe_do_not_activate_rejected_metadata() -> None:
    host = Host()
    provider = prepared_provider(host)
    assert provider.choose_version(20, Range.singleton(16)) is None
    before = list(host.events)
    assert provider.has_satisfying_version(20, Range.singleton(16))
    assert host.events == before
    assert 10 not in provider.active_requirements()
    assert len(provider.consume_pending_clauses()) == 1
    assert provider.consume_pending_clauses() == []
    assert provider.consume_force_backtrack_targets() == []


def test_disabled_precheck_leaves_dependency_reading_to_the_core() -> None:
    host = Host()
    provider = CandidateProvider(
        host, [CandidateRequirement(20, Range.full(), object())]
    )
    provider.receive_partial_solution_hint({10: Range.singleton(2)}, {10: 2})
    assert provider.choose_version(20, Range.full()) == 16
    assert host.events == []
    assert provider.consume_pending_clauses() == []


@pytest.mark.parametrize("self_bounds", [Range.full(), Range.singleton(99)])
def test_later_self_declaration_delegates_the_complete_candidate(
    self_bounds: Range[int],
) -> None:
    host = Host()
    host.self_requirement = self_bounds
    provider = prepared_provider(host)
    assert provider.choose_version(20, Range.singleton(16)) == 16
    assert host.events == ["metadata:20", "child", "self"]
    assert provider.consume_pending_clauses() == []


def test_late_metadata_error_is_not_hidden_by_an_earlier_blocker() -> None:
    host = Host()
    host.fail_after_dependency = True
    provider = prepared_provider(host)
    with pytest.raises(OSError, match="late metadata error"):
        provider.choose_version(20, Range.singleton(16))
    assert host.events == ["metadata:20", "child", "error"]
    assert provider.consume_pending_clauses() == []


def test_precheck_needs_both_a_decision_and_disjoint_positive_bounds() -> None:
    host = Host()
    provider = prepared_provider(host)
    for positive, decisions in [({}, {}), ({}, {10: 2}), ({10: Range.full()}, {10: 2})]:
        provider.receive_partial_solution_hint(positive, decisions)
        assert provider.choose_version(20, Range.singleton(16)) == 16
        assert provider.consume_pending_clauses() == []


def test_selected_singleton_must_also_be_disjoint() -> None:
    host = Host()
    provider = prepared_provider(host)
    provider.receive_partial_solution_hint({10: Range.singleton(2)}, {10: 1})
    assert provider.choose_version(20, Range.singleton(16)) == 16
    assert provider.consume_pending_clauses() == []


def test_feedback_requires_distinct_candidates_in_one_parent_and_blocker_group() -> (
    None
):
    provider = prepared_provider(Host(), feedback=True)
    for _ in range(5):
        assert reject(provider, 16) == []
    for key in (16, 15, 14):
        assert reject(provider, key, package=30) == []
    for key in (15, 14):
        assert reject(provider, key) == []
    assert reject(provider, 13) == [10]
    assert provider.consume_force_backtrack_targets() == []


def test_blocker_versions_have_separate_rejection_groups() -> None:
    provider = prepared_provider(Host(), feedback=True)
    for key in (16, 15, 14):
        assert reject(provider, key) == []
    provider.receive_partial_solution_hint({10: Range.singleton(3)}, {10: 3})
    for key in (16, 15, 14):
        assert reject(provider, key) == []
    provider.receive_partial_solution_hint({10: Range.singleton(2)}, {10: 2})
    assert reject(provider, 13) == [10]


def test_feedback_cap_survives_blocker_changes_and_resets_for_a_new_solve() -> None:
    provider = prepared_provider(Host(), feedback=True)
    requests = [target for key in range(16, 0, -1) for target in reject(provider, key)]
    assert requests == [10, 10, 10]
    provider.receive_partial_solution_hint({10: Range.singleton(3)}, {10: 3})
    for key in (16, 15, 14, 13):
        assert reject(provider, key) == []
    assert provider.prioritize(10, Range.full(), {}, None) > provider.prioritize(
        20, Range.full(), {}, None
    )

    provider.begin_resolution()
    assert provider.prioritize(10, Range.full(), {}, None) < provider.prioritize(
        20, Range.full(), {}, None
    )
    provider.receive_partial_solution_hint({10: Range.singleton(2)}, {10: 2})
    assert [target for key in (16, 15, 14, 13) for target in reject(provider, key)] == [
        10
    ]


def test_feedback_is_not_available_without_prechecking() -> None:
    with pytest.raises(
        ValueError, match="precheck_feedback requires dependency_precheck"
    ):
        CandidateProvider(Host(), [], precheck_feedback=True)


def test_prechecks_and_bounded_feedback_preserve_the_real_solution() -> None:
    rows = []
    for enabled, feedback in ((False, False), (True, False), (True, True)):
        host = Host()
        provider = CandidateProvider(
            host,
            [
                CandidateRequirement(package, Range.full(), object())
                for package in (10, 20)
            ],
            dependency_precheck=enabled,
            precheck_feedback=feedback,
        )
        resolver = Resolver(provider, availability_generation=lambda: 0)
        solution = resolver.solve(provider.root_requirements())
        rows.append(
            (
                solution.pins,
                resolver.stats.decisions,
                resolver.stats.conflicts,
                host.events.count("metadata:20"),
            )
        )
    assert rows[0][0] == rows[1][0] == rows[2][0] == {10: 1, 20: 16}
    assert rows[1][1] < rows[0][1]
    assert rows[2][1] <= rows[1][1]
    assert rows[2][2] < rows[1][2]
    assert rows[2][3] < rows[1][3]


def test_intrinsically_empty_dependencies_stop_metadata_consumption() -> None:
    class EmptyHost(Host):
        def get_dependencies(
            self, candidate: PreparedCandidate[int]
        ) -> Iterable[CandidateRequirement[int, int]]:
            yield CandidateRequirement(10, Range.singleton(1), object())
            yield CandidateRequirement(10, Range.singleton(2), object())
            raise AssertionError("metadata after an empty intersection was read")

    provider = prepared_provider(EmptyHost())
    assert provider.choose_version(20, Range.singleton(16)) is None
    clauses = provider.consume_pending_clauses()
    assert len(clauses) == 1
    assert clauses[0].terms[1].constraint.is_empty


def test_capped_blockers_do_not_accumulate_more_candidate_history() -> None:
    provider = prepared_provider(Host(), feedback=True)
    for key in range(16, 4, -1):
        reject(provider, key)
    feedback = provider._precheck_feedback
    assert feedback is not None
    before = {group: set(keys) for group, keys in feedback.rejected.items()}
    for key in range(4, 0, -1):
        assert reject(provider, key) == []
    assert feedback.rejected == before


def test_an_ordinary_restart_preserves_precheck_feedback() -> None:
    host = Host()
    provider = prepared_provider(host, feedback=True)
    resolver = Resolver(provider)
    resolver._reset(None)
    resolver._add_root_requirements(provider.root_requirements())
    provider.receive_partial_solution_hint({10: Range.singleton(2)}, {10: 2})
    assert [target for key in (16, 15, 14, 13) for target in reject(provider, key)] == [
        10
    ]
    resolver.stats.package_conflict_counts[20] = 5
    _, _, restarted = conflict.maybe_restart(resolver, 5, 1)
    assert restarted
    assert provider.prioritize(10, Range.full(), {}, None) > provider.prioritize(
        20, Range.full(), {}, None
    )
    provider.receive_partial_solution_hint({10: Range.singleton(2)}, {10: 2})
    assert [target for key in (12, 11, 10, 9) for target in reject(provider, key)] == [
        10
    ]


@pytest.mark.parametrize("blocker_present", [False, True])
def test_unusable_retreat_requests_keep_clauses_and_remain_bounded(
    blocker_present: bool,
) -> None:
    provider = prepared_provider(Host(), feedback=True)
    resolver = Resolver(provider)
    if blocker_present:
        resolver.solution.decide(10, 2)
    resolver.solution.decide(30, 1)
    before = dict(resolver.solution.decisions())
    requests = []
    for key in range(16, 0, -1):
        targets = reject(provider, key)
        requests.extend(targets)
        assert conflict.force_targeted_backtrack(resolver, targets) is None

    assert requests == [10, 10, 10]
    assert resolver.stats.targeted_backtracks == 0
    assert dict(resolver.solution.decisions()) == before
    assert provider.consume_pending_clauses() == []
