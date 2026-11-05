"""Run real resolver entry points against an independent finite-domain oracle."""

from collections.abc import Mapping
from math import prod

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.errors import ResolutionError, ResolutionLimitError
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider
from nab_resolver.testing import ProviderScenario, check_provider
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    Term,
)

from .property.providers import FuzzProvider

Graph = dict[str, dict[int, dict[str, Range[int]]]]


class FixtureProvider(BaseProvider[str, int]):
    """Adapt a finite fixture with ordinary newest-first candidate ordering."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        return next(
            (
                version
                for version in sorted(self.graph.get(package, {}), reverse=True)
                if version in version_range
            ),
            None,
        )

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return self.choose_version(package, version_range) is not None

    def get_dependencies(
        self, package: str, version: int
    ) -> Mapping[str, RangeProtocol[int]]:
        return self.graph[package][version]

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        return len(self.graph.get(package, {}))

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        return None


def check_fixture(
    graph: Graph,
    provider: type[FixtureProvider] = FixtureProvider,
    *,
    constraints: dict[str, Range[int]] | None = None,
) -> None:
    """Exercise one test provider through the public third-party check."""
    scenario = ProviderScenario[str, int](
        "custom", {"app": Range.full()}, graph, constraints=constraints
    )
    check_provider(
        lambda _scenario: provider(graph),
        scenario,
        range_type=Range,
        root_version=0,
        max_combinations=prod(max(1, len(versions)) for versions in graph.values()),
    )


@pytest.mark.parametrize(
    "graph",
    [
        {},
        {"app": {}},
        {"app": {1: {}}},
        {
            "app": {2: {"lib": Range.singleton(2)}, 1: {"lib": Range.singleton(1)}},
            "lib": {1: {}},
        },
        {"app": {1: {"lib": Range.full()}}, "lib": {1: {"app": Range.full()}}},
        {"app": {1: {"missing": Range.full()}}},
    ],
)
def test_provider_scenarios(graph: Graph) -> None:
    check_fixture(graph)


def test_constraints_apply_only_to_reachable_packages() -> None:
    check_fixture(
        {"app": {1: {}}, "unused": {1: {}}}, constraints={"unused": Range.empty()}
    )

    check_fixture(
        {"app": {1: {"lib": Range.full()}}, "lib": {1: {}, 2: {}}},
        constraints={"lib": Range.singleton(1)},
    )

    check_fixture({"app": {1: {}}}, constraints={"app": Range.empty()})


def test_oracle_budget_precedes_provider_work() -> None:
    def forbidden(scenario: ProviderScenario[str, int]) -> FixtureProvider:
        raise AssertionError("provider constructed before budget check")

    for budget in (0, 1):
        with pytest.raises(ValueError, match="budget"):
            check_provider(
                forbidden,
                ProviderScenario[str, int]("budget", {}, {"app": {1: {}, 2: {}}}),
                range_type=Range,
                root_version=0,
                max_combinations=budget,
            )


def test_omitted_feasible_version_is_detected() -> None:
    class MissingVersion(FixtureProvider):
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            return 2 if 2 in version_range else None

    with pytest.raises(AssertionError, match="rejected a satisfiable fixture"):
        check_fixture({"app": {1: {}, 2: {}}}, MissingVersion)


def test_probe_disagreement_is_detected() -> None:
    class FalseProbe(FixtureProvider):
        def has_satisfying_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> bool:
            return False

    with pytest.raises(AssertionError, match="probe disagrees"):
        check_fixture({"app": {1: {}}}, FalseProbe)


def test_unknown_candidate_is_detected() -> None:
    class Unknown(FixtureProvider):
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int:
            return 9

    with pytest.raises(AssertionError, match="outside fixture"):
        check_fixture({"app": {1: {}}}, Unknown)


def test_wrong_dependencies_are_detected() -> None:
    class MissingDependency(FixtureProvider):
        def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
            return {}

    with pytest.raises(AssertionError, match="wrong dependencies"):
        check_fixture(
            {"app": {1: {"lib": Range.full()}}, "lib": {1: {}}}, MissingDependency
        )


def test_repeated_metadata_change_is_detected() -> None:
    class Changing(FixtureProvider):
        reads = 0

        def get_dependencies(
            self, package: str, version: int
        ) -> Mapping[str, RangeProtocol[int]]:
            self.reads += 1
            return super().get_dependencies(package, version) if self.reads == 1 else {}

    with pytest.raises(AssertionError, match="dependencies changed"):
        check_fixture({"app": {1: {"lib": Range.full()}}, "lib": {1: {}}}, Changing)


def test_returned_mapping_mutation_is_detected() -> None:
    class Mutating(FixtureProvider):
        retained_dependencies: dict[str, Range[int]] | None = None

        def get_dependencies(
            self, package: str, version: int
        ) -> Mapping[str, RangeProtocol[int]]:
            if package == "lib" and self.retained_dependencies is not None:
                self.retained_dependencies.clear()

            answer = dict(self.graph[package][version])
            if package == "app":
                self.retained_dependencies = answer

            return answer

    with pytest.raises(AssertionError, match="mapping was mutated"):
        check_fixture({"app": {1: {"lib": Range.full()}}, "lib": {1: {}}}, Mutating)


def test_widening_contracts_are_checked() -> None:
    class Excluding(FixtureProvider):
        def widen_decision(self, package: str, version: int) -> Range[int]:
            return Range.empty()

    class Unequal(FixtureProvider):
        def widen_decision(self, package: str, version: int) -> Range[int]:
            return Range.full()

    graph = {"app": {2: {"missing": Range.full()}, 1: {}}}

    with pytest.raises(AssertionError, match="excludes decision"):
        check_fixture(graph, Excluding)

    with pytest.raises(AssertionError, match="rejected a satisfiable fixture"):
        check_fixture(graph, Unequal)


def test_display_membership_is_checked() -> None:
    class Dropping(FixtureProvider):
        def narrow_for_display(
            self, package: str, constraint: RangeProtocol[int]
        ) -> Range[int]:
            return Range.empty()

    with pytest.raises(AssertionError, match="display narrowing"):
        check_fixture({"app": {1: {"missing": Range.full()}}}, Dropping)


@given(
    st.lists(st.tuples(st.integers(0, 3), st.integers(0, 3)), min_size=2, max_size=2)
)
def test_generated_backtracking_fixtures(bounds: list[tuple[int, int]]) -> None:
    graph: Graph = {"app": {}, "lib": {1: {}, 2: {}}}

    for version, interval_bounds in enumerate(bounds, start=1):
        lower, upper = sorted(interval_bounds)
        graph["app"][version] = {"lib": Range.between(lower, upper)}

    check_fixture(graph)


def test_full_query_cannot_invent_an_unlisted_version() -> None:
    class Inventing(FixtureProvider):
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            if 2 in version_range:
                return 2

            return super().choose_version(package, version_range)

        def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
            return self.graph.get(package, {}).get(version, {})

    with pytest.raises(AssertionError, match="outside fixture"):
        check_fixture({"app": {1: {}}}, Inventing)


def test_solver_limits_are_not_reported_as_unsatisfiability() -> None:
    class Limited(FixtureProvider):
        def prioritize(
            self,
            package: str,
            version_range: RangeProtocol[int],
            conflict_counts: Mapping[str, int],
            culprit_counts: Mapping[str, int] | None = None,
        ) -> int:
            raise ResolutionLimitError("fixture budget exhausted")

    with pytest.raises(ResolutionLimitError, match="fixture budget"):
        check_fixture({"app": {1: {}}}, Limited)


def test_valid_singleton_widening() -> None:
    class Widening(FixtureProvider):
        def widen_decision(self, package: str, version: int) -> Range[int]:
            return Range.singleton(version)

    check_fixture({"app": {1: {"lib": Range.full()}, 2: {}}, "lib": {1: {}}}, Widening)


def test_precheck_can_supply_a_sound_clause_instead_of_a_candidate() -> None:
    class Prechecking(FixtureProvider):
        pending: list[Incompatibility[str, int]]

        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> None:
            self.pending = [
                Incompatibility(
                    [Term(package, version_range, positive=True)],
                    cause=IncompatibilityCause.NO_VERSIONS,
                )
            ]

        def has_satisfying_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> bool:
            return False

        def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
            clauses = self.pending
            self.pending = []

            return clauses

    check_fixture({"app": {1: {"missing": Range.full()}}}, Prechecking)


def test_scan_precedes_direct_queries() -> None:
    class Initialized(FixtureProvider):
        scanned = False

        def begin_decision_scan(self) -> None:
            self.scanned = True

        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            assert self.scanned
            return super().choose_version(package, version_range)

    check_fixture({"app": {1: {}}}, Initialized)


def test_structural_provider_is_supported() -> None:
    graph: Graph = {"app": {1: {}}}
    check_provider(
        lambda _scenario: FuzzProvider(graph),
        ProviderScenario[str, int]("structural", {"app": Range.full()}, graph),
        range_type=Range,
        root_version=0,
        max_combinations=1,
    )


def test_proofless_provider_failure_is_not_an_unsatisfiability_result() -> None:
    class FailedOperation(FixtureProvider):
        def prioritize(
            self,
            package: str,
            version_range: RangeProtocol[int],
            conflict_counts: Mapping[str, int],
            culprit_counts: Mapping[str, int] | None = None,
        ) -> int:
            raise ResolutionError("provider operation failed")

    with pytest.raises(ResolutionError, match="provider operation failed"):
        check_fixture({"app": {1: {"missing": Range.full()}}}, FailedOperation)


def test_scan_only_context_hint_precedes_direct_query_scans() -> None:
    class ScanAware(FixtureProvider):
        hinted = False

        def receive_decision_scan_hint(
            self,
            positive_ranges: Mapping[str, RangeProtocol[int]],
            decisions: Mapping[str, int],
        ) -> None:
            self.hinted = True

        def begin_decision_scan(self) -> None:
            assert self.hinted

    check_fixture({"app": {1: {}}}, ScanAware)


def test_candidate_hashes_remain_stable() -> None:
    class MutableVersion(int):
        salt = 0

        def __hash__(self) -> int:
            return hash(int(self)) + self.salt

    version = MutableVersion(1)

    class HashChanging(FixtureProvider):
        def narrow_for_display(
            self, package: str, constraint: RangeProtocol[int]
        ) -> RangeProtocol[int]:
            version.salt += 1
            return constraint

    with pytest.raises(AssertionError, match="candidate hash changed"):
        check_fixture({"app": {version: {"missing": Range.full()}}}, HashChanging)


@pytest.mark.parametrize("widen_all", [False, True])
def test_widening_is_checked_when_another_version_is_actually_chosen(
    widen_all: bool,
) -> None:
    class OldestWithBroadFacts(FixtureProvider):
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            return next(
                (
                    version
                    for version in sorted(self.graph.get(package, {}))
                    if version in version_range
                ),
                None,
            )

        def prioritize(
            self,
            package: str,
            version_range: RangeProtocol[int],
            conflict_counts: Mapping[str, int],
            culprit_counts: Mapping[str, int] | None = None,
        ) -> int:
            return {"app": 0, "lib": 1, "bridge": 2}[package]

        def widen_decision(self, package: str, version: int) -> Range[int]:
            return Range.full() if widen_all else Range.singleton(version)

    graph: Graph = {
        "app": {1: {"lib": Range.singleton(1)}, 2: {"lib": Range.full()}},
        "lib": {1: {}, 2: {}},
        "bridge": {1: {"app": Range.singleton(2)}},
    }
    scenario = ProviderScenario[str, int](
        "observed widening", {"app": Range.full(), "bridge": Range.full()}, graph
    )

    def run() -> None:
        check_provider(
            lambda _scenario: OldestWithBroadFacts(graph),
            scenario,
            range_type=Range,
            root_version=0,
            max_combinations=4,
        )

    if widen_all:
        with pytest.raises(
            AssertionError, match="widening merges unequal dependencies"
        ):
            run()
    else:
        run()


@pytest.mark.parametrize("return_input", [False, True])
def test_display_callback_cannot_mutate_its_input(return_input: bool) -> None:
    class MutatingDisplay(FixtureProvider):
        def narrow_for_display(
            self, package: str, constraint: RangeProtocol[int]
        ) -> Range[int]:
            assert isinstance(constraint, Range)
            original = Range[int](constraint._intervals)
            constraint._intervals = ()
            constraint._hash = 0
            return constraint if return_input else original

    with pytest.raises(AssertionError, match="display narrowing"):
        check_fixture({"app": {1: {"missing": Range.full()}}}, MutatingDisplay)


@pytest.mark.parametrize("phase", ["probe", "choice"])
def test_provider_cannot_mutate_the_query_range(phase: str) -> None:
    class MutatingQuery(FixtureProvider):
        @staticmethod
        def change_full_range(allowed: RangeProtocol[int]) -> None:
            assert isinstance(allowed, Range)
            if allowed == Range.full():
                allowed._intervals = ((2, True, 2, True),)
                allowed._hash = 0

        def has_satisfying_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> bool:
            if phase == "probe":
                self.change_full_range(version_range)
            return any(
                version in version_range for version in self.graph.get(package, {})
            )

        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            if phase == "choice":
                self.change_full_range(version_range)
            return super().choose_version(package, version_range)

    with pytest.raises(AssertionError, match="mutated.*range"):
        check_fixture({"app": {1: {}, 2: {}}}, MutatingQuery)
