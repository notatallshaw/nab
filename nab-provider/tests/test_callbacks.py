"""Run the callback API with an opaque in-memory package domain."""

from collections.abc import Iterable, Mapping, Sequence
from itertools import product
from typing import NamedTuple

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_provider.callbacks import CallbackProvider, CandidateQuery
from nab_resolver.errors import ResolutionError, ResolutionTerminatedError
from nab_resolver.predicate_ranges import PredicateRange
from nab_resolver.resolver import Resolver
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term


class Candidate(NamedTuple):
    package: int
    version: int
    source: str = "index"


class Requirement(NamedTuple):
    package: int
    versions: frozenset[int]
    source: str | None = None


class MemoryHost:
    """Supply candidates using only selected declarations for source eligibility."""

    def __init__(
        self,
        candidates: Mapping[int, Sequence[Candidate]],
        dependencies: Mapping[Candidate, Sequence[Requirement]],
    ) -> None:
        self.candidates = candidates
        self.dependencies = dependencies
        self.queries: list[tuple[int, tuple[Requirement, ...]]] = []

    @staticmethod
    def identify(value: Requirement | Candidate) -> int:
        return value.package

    @staticmethod
    def is_satisfied_by(requirement: Requirement, candidate: Candidate) -> bool:
        return candidate.version in requirement.versions and (
            requirement.source is None or requirement.source == candidate.source
        )

    def find_matches(
        self,
        package: int,
        requirements: Mapping[int, Sequence[Requirement]],
        incompatibilities: Mapping[int, Sequence[Candidate]],
    ) -> CandidateQuery[Candidate]:
        active = tuple(requirements[package])
        self.queries.append((package, active))
        explicit = {
            requirement.source
            for requirement in active
            if requirement.source is not None
        }
        sources = explicit or {"index"}
        return CandidateQuery(
            (
                candidate
                for candidate in self.candidates[package]
                if candidate.source in sources
                and candidate not in incompatibilities.get(package, ())
                and all(
                    self.is_satisfied_by(requirement, candidate)
                    for requirement in active
                )
            ),
            may_expand=self.defer_absence(package),
        )

    @staticmethod
    def exact_candidate(requirement: Requirement) -> Candidate | None:
        del requirement
        return None

    @staticmethod
    def defer_absence(package: int) -> bool:
        del package
        return True

    def get_dependencies(self, candidate: Candidate) -> Iterable[Requirement]:
        return self.dependencies.get(candidate, ())

    @staticmethod
    def preference(
        package: int, requirements: Mapping[int, Sequence[Requirement]]
    ) -> int:
        del requirements
        return package


def test_failed_parent_does_not_exclude_a_later_explicit_source() -> None:
    newer = Candidate(10, 2)
    older = Candidate(10, 1)
    index_child = Candidate(20, 1)
    explicit_child = Candidate(20, 3, "direct")
    newer_dependency = Requirement(20, frozenset({2, 3}))
    older_dependency = Requirement(20, frozenset({3}), "direct")
    host = MemoryHost(
        {10: [newer, older], 20: [explicit_child, index_child]},
        {newer: [newer_dependency], older: [older_dependency]},
    )
    provider = CallbackProvider(host, [Requirement(10, frozenset({1, 2}))])
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    solution = resolver.solve(provider.root_requirements())

    assert solution.pins == {10: older, 20: explicit_child}
    assert resolver.stats.conflicts == 1
    child_queries = [
        requirements for package, requirements in host.queries if package == 20
    ]
    assert child_queries[0] == (newer_dependency,)
    assert child_queries[-1] == (older_dependency,)


def test_learned_exclusion_reaches_the_native_candidate_stream() -> None:
    incumbent = Candidate(10, 1, "installed")
    replacement = Candidate(10, 1)
    missing = Requirement(20, frozenset({9}))
    host = MemoryHost({10: [incumbent, replacement], 20: []}, {incumbent: [missing]})

    class InstalledHost(MemoryHost):
        def find_matches(
            self,
            package: int,
            requirements: Mapping[int, Sequence[Requirement]],
            incompatibilities: Mapping[int, Sequence[Candidate]],
        ) -> CandidateQuery[Candidate]:
            if package == 20:
                return CandidateQuery(())
            if incumbent in incompatibilities.get(package, ()):
                return CandidateQuery([replacement])
            return CandidateQuery([incumbent])

    host = InstalledHost(host.candidates, host.dependencies)
    provider = CallbackProvider(host, [Requirement(10, frozenset({1}))])
    solution = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    ).solve(provider.root_requirements())
    assert solution.pins == {10: replacement}


@given(
    bounds=st.lists(
        st.sets(st.integers(min_value=1, max_value=2)), min_size=6, max_size=6
    )
)
@pytest.mark.parametrize("defer", [False, True])
def test_callback_solver_agrees_with_finite_oracle(
    bounds: list[set[int]], *, defer: bool
) -> None:
    """Compare selected cycles and conflicting edges with exhaustive assignments."""
    candidates = {
        package: [Candidate(package, version) for version in (2, 1)]
        for package in (10, 20, 30)
    }
    dependencies = {
        candidate: [
            Requirement(
                20 if package == 10 else 30 if package == 20 else 10,
                frozenset(bounds[index]),
            )
        ]
        for index, (package, version) in enumerate(product((10, 20, 30), (2, 1)))
        for candidate in [Candidate(package, version)]
    }
    roots = [Requirement(10, frozenset({1, 2}))]
    feasible = []
    for choices in product(*(candidates[package] for package in (10, 20, 30))):
        selected = dict(zip((10, 20, 30), choices, strict=True))
        if all(
            MemoryHost.is_satisfied_by(requirement, selected[requirement.package])
            for candidate in choices
            for requirement in dependencies[candidate]
        ):
            feasible.append(selected)

    class OracleHost(MemoryHost):
        @staticmethod
        def defer_absence(package: int) -> bool:
            del package
            return defer

    host = OracleHost(candidates, dependencies)
    provider = CallbackProvider(host, roots)
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    if not feasible:
        with pytest.raises(ResolutionError) as failure:
            resolver.solve(provider.root_requirements())
        assert not isinstance(failure.value, ResolutionTerminatedError)
        return
    solution = resolver.solve(provider.root_requirements())
    assert solution.pins in feasible
    assert set(solution.edges) == {(10, 20), (20, 30), (30, 10)}


@pytest.mark.parametrize("defer", [False, True])
def test_host_final_absence_stops_before_unrelated_metadata(*, defer: bool) -> None:
    parent = Candidate(20, 1)
    child = Candidate(10, 2, "direct")

    class Host(MemoryHost):
        @staticmethod
        def defer_absence(package: int) -> bool:
            del package
            return defer

    host = Host(
        {10: [child], 20: [parent]},
        {parent: [Requirement(10, frozenset({2}), "direct")]},
    )
    roots = [Requirement(10, frozenset({2})), Requirement(20, frozenset({1}))]
    provider = CallbackProvider(host, roots)
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    if defer:
        assert resolver.solve(provider.root_requirements()).pins == {
            10: child,
            20: parent,
        }
    else:
        with pytest.raises(ResolutionError) as failure:
            resolver.solve(provider.root_requirements())
        assert all(package == 10 for package, _ in host.queries)
        assert not provider.dependencies
        causes = provider.failure_causes(failure.value.incompatibility)
        assert any(
            cause.requirement == roots[0] and cause.parent is None for cause in causes
        )


def test_probes_and_exclusions_do_not_change_the_selected_declarations() -> None:
    first, second = Candidate(10, 2), Candidate(10, 1)
    root = Requirement(10, frozenset({1, 2}))
    host = MemoryHost({10: [first, second]}, {})
    provider = CallbackProvider(host, [root])
    allowed = PredicateRange.singleton(second)
    assert provider.has_satisfying_version(10, allowed)
    assert not provider.has_satisfying_version(20, allowed)
    assert provider.choose_version(10, allowed) == second
    assert provider.active_requirements() == {10: (root,)}
    assert provider.choose_version(20, allowed) is None


def test_failure_causes_keep_original_parents_without_duplicates() -> None:
    older, newer = Candidate(10, 1), Candidate(10, 2)
    child = Requirement(20, frozenset({3}))
    root = Requirement(10, frozenset({1, 2}))
    host = MemoryHost(
        {10: [newer, older], 20: []}, {older: [child, child], newer: [child]}
    )
    provider = CallbackProvider(host, [root, root])
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    with pytest.raises(ResolutionError) as failure:
        resolver.solve(provider.root_requirements())
    causes = provider.failure_causes(failure.value.incompatibility)
    assert any(
        cause.requirement == child and cause.parent in (older, newer)
        for cause in causes
    )
    assert len({(cause.requirement, cause.parent) for cause in causes}) == len(causes)


def test_final_missing_child_rejects_parent_without_repeating_the_query() -> None:
    parent = Candidate(10, 1)
    child = Requirement(20, frozenset({3}))

    class FinalHost(MemoryHost):
        @staticmethod
        def defer_absence(package: int) -> bool:
            return package == 10

    host = FinalHost({10: [parent], 20: []}, {parent: [child]})
    provider = CallbackProvider(host, [Requirement(10, frozenset({1}))])
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
        max_iterations=20,
    )
    with pytest.raises(ResolutionError) as failure:
        resolver.solve(provider.root_requirements())
    assert type(failure.value) is ResolutionError
    assert failure.value.incompatibility is not None
    assert any(
        cause.requirement == child
        for cause in provider.failure_causes(failure.value.incompatibility)
    )
    assert resolver.stats.rounds < 20


def test_deferred_child_precedes_preparation_of_a_later_sibling() -> None:
    parent, known, unrelated = (
        Candidate(10, 1),
        Candidate(15, 1, "direct"),
        Candidate(30, 1),
    )
    exact = Requirement(15, frozenset({1}), "direct")
    missing = Requirement(20, frozenset({3}))

    class Host(MemoryHost):
        @staticmethod
        def exact_candidate(requirement: Requirement) -> Candidate | None:
            return known if requirement == exact else None

        @staticmethod
        def defer_absence(package: int) -> bool:
            del package
            return False

    host = Host(
        {10: [parent], 15: [known], 20: [], 30: [unrelated]},
        {parent: [exact, missing, Requirement(30, frozenset({1}))]},
    )
    provider = CallbackProvider(host, [Requirement(10, frozenset({1}))])
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
        max_iterations=30,
    )
    with pytest.raises(ResolutionError) as failure:
        resolver.solve(provider.root_requirements())
    assert type(failure.value) is ResolutionError
    assert all(package != 30 for package, _ in host.queries)
    assert resolver.stats.rounds < 30


def test_new_parent_bound_revisits_a_child_without_enumerating_middle_choices() -> None:
    newest, middle, oldest = (Candidate(10, version) for version in (3, 2, 1))
    parent = Candidate(20, 1)
    host = MemoryHost(
        {10: [newest, middle, oldest], 20: [parent]},
        {parent: [Requirement(10, frozenset({1}))]},
    )
    provider = CallbackProvider(
        host, [Requirement(10, frozenset({1, 2, 3})), Requirement(20, frozenset({1}))]
    )
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    assert resolver.solve(provider.root_requirements()).pins == {10: oldest, 20: parent}
    assert middle not in provider.considered
    assert resolver.stats.targeted_backtracks == 1


def test_later_root_tries_its_other_candidate_before_replacing_an_earlier_root() -> (
    None
):
    first, alternate = Candidate(10, 3), Candidate(10, 1)
    newer, older = Candidate(20, 2), Candidate(20, 1)
    host = MemoryHost(
        {10: [first, alternate], 20: [newer, older]},
        {
            newer: [Requirement(10, frozenset({1}))],
            older: [Requirement(10, frozenset({3}))],
        },
    )
    provider = CallbackProvider(
        host, [Requirement(10, frozenset({1, 3})), Requirement(20, frozenset({1, 2}))]
    )
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    assert resolver.solve(provider.root_requirements()).pins == {10: first, 20: older}
    assert resolver.stats.targeted_backtracks == 0


def test_cycle_retains_the_dependency_fact_when_reconsidering_its_root() -> None:
    newer, older = Candidate(10, 2), Candidate(10, 1)
    second, third = Candidate(20, 1), Candidate(30, 1)
    host = MemoryHost(
        {10: [newer, older], 20: [second], 30: [third]},
        {
            newer: [Requirement(20, frozenset({1}))],
            older: [Requirement(20, frozenset({1}))],
            second: [Requirement(30, frozenset({1}))],
            third: [Requirement(10, frozenset({1}))],
        },
    )
    provider = CallbackProvider(host, [Requirement(10, frozenset({1, 2}))])
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
        max_iterations=20,
    )
    assert resolver.solve(provider.root_requirements()).pins == {
        10: older,
        20: second,
        30: third,
    }
    assert resolver.stats.rounds < 20


def test_cached_orphan_decision_does_not_supply_active_source_declarations() -> None:
    root, orphan, source = (
        Candidate(10, 1),
        Candidate(20, 1),
        Candidate(30, 1, "direct"),
    )
    root_requirement = Requirement(10, frozenset({1}))
    hidden = Requirement(30, frozenset({1}), "direct")
    host = MemoryHost({10: [root], 20: [orphan], 30: [source]}, {orphan: [hidden]})
    provider = CallbackProvider(host, [root_requirement])
    provider.get_dependencies(20, orphan)
    provider.receive_partial_solution_hint({}, {10: root, 20: orphan})
    assert provider.active_requirements() == {10: (root_requirement,)}
    assert not provider.has_satisfying_version(30, PredicateRange.full())
    assert not host.queries


def test_later_root_can_replace_a_shared_child_without_changing_either_root() -> None:
    first, second, intermediary = Candidate(10, 1), Candidate(20, 1), Candidate(40, 1)
    indexed, direct = Candidate(30, 1), Candidate(30, 1, "direct")
    host = MemoryHost(
        {10: [first], 20: [second], 30: [indexed, direct], 40: [intermediary]},
        {
            first: [Requirement(30, frozenset({1}))],
            second: [Requirement(40, frozenset({1}))],
            intermediary: [Requirement(30, frozenset({1}), "direct")],
        },
    )
    provider = CallbackProvider(
        host, [Requirement(10, frozenset({1})), Requirement(20, frozenset({1}))]
    )
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
        max_iterations=30,
    )
    assert resolver.solve(provider.root_requirements()).pins == {
        10: first,
        20: second,
        30: direct,
        40: intermediary,
    }
    assert resolver.stats.targeted_backtracks == 1


def test_first_root_fallback_keeps_native_order_after_all_later_choices_conflict() -> (
    None
):
    current, replacement = Candidate(10, 2), Candidate(10, 1)
    newer, older = Candidate(20, 3), Candidate(20, 2)
    dependency = Requirement(10, frozenset({1}))
    host = MemoryHost(
        {10: [current, replacement], 20: [newer, older]},
        {newer: [dependency], older: [dependency]},
    )
    provider = CallbackProvider(
        host, [Requirement(10, frozenset({1, 2})), Requirement(20, frozenset({2, 3}))]
    )
    solution = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    ).solve(provider.root_requirements())
    assert solution.pins == {10: replacement, 20: newer}


@pytest.mark.parametrize("root_bound", [False, True])
def test_ineligible_exact_hints_cannot_defer_a_conclusive_conflict(
    *, root_bound: bool
) -> None:
    parent, exact_choice, other = Candidate(10, 1), Candidate(20, 1), Candidate(20, 2)
    exact = Requirement(20, frozenset({1}))
    bound = Requirement(20, frozenset({2}))

    class Host(MemoryHost):
        @staticmethod
        def exact_candidate(requirement: Requirement) -> Candidate | None:
            return exact_choice if requirement == exact else None

        @staticmethod
        def defer_absence(package: int) -> bool:
            del package
            return False

    roots = [Requirement(10, frozenset({1}))]
    dependencies = [exact]
    if root_bound:
        roots.append(bound)
    else:
        dependencies.append(bound)
    host = Host({10: [parent], 20: [exact_choice, other]}, {parent: dependencies})
    provider = CallbackProvider(host, roots)
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
        max_iterations=20,
    )
    with pytest.raises(ResolutionError) as failure:
        resolver.solve(provider.root_requirements())
    assert type(failure.value) is ResolutionError
    assert resolver.stats.rounds < 20


def test_native_stream_is_filtered_by_core_constraints_before_a_child_decision() -> (
    None
):
    parent, first, selected = Candidate(10, 1), Candidate(20, 1), Candidate(20, 2)
    root = Requirement(20, frozenset({1, 2}))
    host = MemoryHost({10: [parent], 20: [first, selected]}, {parent: [root]})
    provider = CallbackProvider(host, [Requirement(10, frozenset({1})), root])
    provider.receive_partial_solution_hint({20: PredicateRange.singleton(selected)}, {})
    assert provider.dependencies_allow(10, parent) is True
    assert not provider.consume_force_backtrack_targets()
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    solution = resolver.solve(
        provider.root_requirements(), {20: PredicateRange.singleton(selected)}
    )
    assert solution.pins == {10: parent, 20: selected}
    assert resolver.stats.targeted_backtracks == 0


def test_two_blocked_children_keep_the_first_reconsideration_and_both_dependencies() -> (
    None
):
    first, first_old = Candidate(10, 2), Candidate(10, 1)
    second, second_old = Candidate(20, 2), Candidate(20, 1)
    parent = Candidate(30, 1)
    host = MemoryHost(
        {10: [first, first_old], 20: [second, second_old], 30: [parent]},
        {parent: [Requirement(10, frozenset({1})), Requirement(20, frozenset({1}))]},
    )
    provider = CallbackProvider(
        host,
        [
            Requirement(10, frozenset({1, 2})),
            Requirement(20, frozenset({1, 2})),
            Requirement(30, frozenset({1})),
        ],
    )
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    assert resolver.solve(provider.root_requirements()).pins == {
        10: first_old,
        20: second_old,
        30: parent,
    }
    assert resolver.stats.targeted_backtracks == 1


def test_a_derived_choice_cannot_displace_an_earlier_root_when_its_parent_can_change() -> (
    None
):
    current, other = Candidate(10, 2), Candidate(10, 1)
    newer, older, extra = Candidate(20, 2), Candidate(20, 1), Candidate(30, 1)
    host = MemoryHost(
        {10: [current, other], 20: [newer, older], 30: [extra]},
        {
            newer: [Requirement(30, frozenset({1}))],
            extra: [Requirement(10, frozenset({1}))],
        },
    )
    provider = CallbackProvider(
        host, [Requirement(10, frozenset({1, 2})), Requirement(20, frozenset({1, 2}))]
    )
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    assert resolver.solve(provider.root_requirements()).pins == {10: current, 20: older}
    assert resolver.stats.targeted_backtracks == 0


def test_cached_parent_does_not_replace_a_root_failure_cause() -> None:
    cached, available = Candidate(10, 1), Candidate(20, 1)
    root = Requirement(20, frozenset({2}))
    host = MemoryHost(
        {10: [cached], 20: [available]}, {cached: [Requirement(20, frozenset({1}))]}
    )
    provider = CallbackProvider(host, [root])
    provider.get_dependencies(10, cached)
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    with pytest.raises(ResolutionError) as failure:
        resolver.solve(provider.root_requirements())
    assert "20" in str(failure.value)
    causes = provider.failure_causes(failure.value.incompatibility)
    assert [(cause.requirement, cause.parent) for cause in causes] == [(root, None)]


def test_failure_causes_include_the_original_root_restricting_a_child() -> None:
    parent, child_one, child_two = Candidate(10, 1), Candidate(20, 1), Candidate(20, 2)
    root = Requirement(20, frozenset({2}))
    dependency = Requirement(20, frozenset({1}))
    host = MemoryHost(
        {10: [parent], 20: [child_one, child_two]}, {parent: [dependency]}
    )
    provider = CallbackProvider(host, [Requirement(10, frozenset({1})), root])
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
    )
    with pytest.raises(ResolutionError) as failure:
        resolver.solve(provider.root_requirements())
    causes = {
        (cause.requirement, cause.parent)
        for cause in provider.failure_causes(failure.value.incompatibility)
    }
    assert (root, None) in causes
    assert (dependency, parent) in causes


def test_original_root_context_is_retained_for_a_native_dependency_failure() -> None:
    parent = Candidate(10, 1)
    root, dependency = Requirement(20, frozenset({2})), Requirement(20, frozenset({1}))
    provider = CallbackProvider(
        MemoryHost({10: [parent], 20: []}, {parent: [dependency]}), [root]
    )
    edges = provider.get_dependencies(10, parent)
    failure = Incompatibility(
        [
            Term(10, PredicateRange.singleton(parent), positive=True),
            Term(20, edges[20], positive=False),
        ],
        cause=IncompatibilityCause.DEPENDENCY,
    )
    causes = {
        (cause.requirement, cause.parent) for cause in provider.failure_causes(failure)
    }
    assert causes == {(root, None), (dependency, parent)}


def test_scoped_parent_rejection_is_reconsidered_after_another_root_changes() -> None:
    newer, older, parent = Candidate(10, 2), Candidate(10, 1), Candidate(20, 1)
    child_one, child_two = Candidate(30, 1), Candidate(30, 2)

    class FinalHost(MemoryHost):
        @staticmethod
        def defer_absence(package: int) -> bool:
            del package
            return False

    host = FinalHost(
        {10: [newer, older], 20: [parent], 30: [child_two, child_one]},
        {
            newer: [Requirement(30, frozenset({1}))],
            older: [Requirement(30, frozenset({2}))],
            parent: [Requirement(30, frozenset({2}))],
        },
    )
    provider = CallbackProvider(
        host, [Requirement(10, frozenset({1, 2})), Requirement(20, frozenset({1}))]
    )
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
        max_iterations=30,
    )
    assert resolver.solve(provider.root_requirements()).pins == {
        10: older,
        20: parent,
        30: child_two,
    }


def test_dependency_mapping_conjoins_original_declarations_and_is_reused() -> None:
    parent = Candidate(10, 1)
    first, second = (
        Requirement(20, frozenset({1, 2})),
        Requirement(20, frozenset({2, 3})),
    )
    host = MemoryHost({10: [parent], 20: []}, {parent: [first, second]})
    provider = CallbackProvider(host, [Requirement(10, frozenset({1}))])
    dependencies = provider.get_dependencies(10, parent)
    assert {
        version for version in (1, 2, 3) if Candidate(20, version) in dependencies[20]
    } == {2}
    assert provider.get_dependencies(10, parent) is dependencies


def test_forced_child_metadata_can_admit_a_sibling_before_absence_is_final() -> None:
    parent, ready = Candidate(10, 1), Candidate(30, 1, "direct")
    sibling = Candidate(20, 2, "direct")
    exact = Requirement(30, frozenset({1}), "direct")

    class Host(MemoryHost):
        @staticmethod
        def exact_candidate(requirement: Requirement) -> Candidate | None:
            return ready if requirement == exact else None

        @staticmethod
        def defer_absence(package: int) -> bool:
            del package
            return False

    host = Host(
        {10: [parent], 20: [sibling], 30: [ready]},
        {
            parent: [Requirement(20, frozenset({2})), exact],
            ready: [Requirement(20, frozenset({2}), "direct")],
        },
    )
    provider = CallbackProvider(host, [Requirement(10, frozenset({1}))])
    resolver = Resolver(
        provider,
        range_type=PredicateRange,
        availability_generation=provider.availability_generation,
        max_iterations=30,
    )
    assert resolver.solve(provider.root_requirements()).pins == {
        10: parent,
        20: sibling,
        30: ready,
    }
