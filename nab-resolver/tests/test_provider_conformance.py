"""Run real resolver entry points against an independent finite-domain oracle."""

from collections.abc import Mapping

import pytest

from nab_resolver._compat import override
from nab_resolver.errors import ResolutionError, ResolutionLimitError
from nab_resolver.ranges import Range
from nab_resolver.resolver import Solution
from nab_resolver.testing import (
    ProviderConformanceError,
    ProviderScenario,
    _check_solution,
    check_provider,
)
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    Term,
)

from .conformance_fixture import FixtureProvider, Graph, check_fixture
from .property.providers import FuzzProvider


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
        @override
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            return 2 if 2 in version_range else None

    with pytest.raises(
        ProviderConformanceError, match="rejected a satisfiable fixture"
    ):
        check_fixture({"app": {1: {}, 2: {}}}, MissingVersion)


def test_solution_pin_outside_fixture_reports_the_selection() -> None:
    solution = Solution({"app": 2}, (), ("app",))
    with pytest.raises(ProviderConformanceError, match="bad pin.*app.*2") as caught:
        _check_solution(
            "bad pin", solution, {"app": frozenset({1})}, {"app": {1: {}}}, {}
        )
    assert caught.value.scenario == "bad pin"


def test_probe_disagreement_is_detected() -> None:
    class FalseProbe(FixtureProvider):
        @override
        def has_satisfying_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> bool:
            return False

    with pytest.raises(ProviderConformanceError, match="probe disagrees"):
        check_fixture({"app": {1: {}}}, FalseProbe)


def test_unknown_candidate_is_detected() -> None:
    class Unknown(FixtureProvider):
        @override
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int:
            return 9

    with pytest.raises(ProviderConformanceError, match="outside fixture"):
        check_fixture({"app": {1: {}}}, Unknown)


def test_known_candidate_outside_the_query_is_detected() -> None:
    class IgnoresRange(FixtureProvider):
        @override
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int:
            return 1

    with pytest.raises(
        ProviderConformanceError, match="outside query.*app.*1"
    ) as caught:
        check_fixture({"app": {1: {}, 2: {}}}, IgnoresRange)
    assert caught.value.scenario == "custom"


def test_wrong_dependencies_are_detected() -> None:
    class MissingDependency(FixtureProvider):
        @override
        def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
            return {}

    with pytest.raises(ProviderConformanceError, match="wrong dependencies"):
        check_fixture(
            {"app": {1: {"lib": Range.full()}}, "lib": {1: {}}}, MissingDependency
        )


def test_repeated_metadata_change_is_detected() -> None:
    class Changing(FixtureProvider):
        reads = 0

        @override
        def get_dependencies(
            self, package: str, version: int
        ) -> Mapping[str, RangeProtocol[int]]:
            self.reads += 1
            return super().get_dependencies(package, version) if self.reads == 1 else {}

    with pytest.raises(ProviderConformanceError, match="dependencies changed"):
        check_fixture({"app": {1: {"lib": Range.full()}}, "lib": {1: {}}}, Changing)


def test_returned_mapping_mutation_is_detected() -> None:
    class Mutating(FixtureProvider):
        retained_dependencies: dict[str, Range[int]] | None = None

        @override
        def get_dependencies(
            self, package: str, version: int
        ) -> Mapping[str, RangeProtocol[int]]:
            if package == "lib" and self.retained_dependencies is not None:
                self.retained_dependencies.clear()

            answer = dict(self.graph[package][version])
            if package == "app":
                self.retained_dependencies = answer

            return answer

    with pytest.raises(ProviderConformanceError, match="mapping was mutated"):
        check_fixture({"app": {1: {"lib": Range.full()}}, "lib": {1: {}}}, Mutating)


def test_widening_contracts_are_checked() -> None:
    class Excluding(FixtureProvider):
        @override
        def widen_decision(self, package: str, version: int) -> Range[int]:
            return Range.empty()

    class Unequal(FixtureProvider):
        @override
        def widen_decision(self, package: str, version: int) -> Range[int]:
            return Range.full()

    graph = {"app": {2: {"missing": Range.full()}, 1: {}}}

    with pytest.raises(ProviderConformanceError, match="excludes decision"):
        check_fixture(graph, Excluding)

    with pytest.raises(
        ProviderConformanceError, match="rejected a satisfiable fixture"
    ):
        check_fixture(graph, Unequal)


def test_display_membership_is_checked() -> None:
    class Dropping(FixtureProvider):
        @override
        def narrow_for_display(
            self, package: str, constraint: RangeProtocol[int]
        ) -> Range[int]:
            return Range.empty()

    with pytest.raises(ProviderConformanceError, match="display narrowing"):
        check_fixture({"app": {1: {"missing": Range.full()}}}, Dropping)


def test_full_query_cannot_invent_an_unlisted_version() -> None:
    class Inventing(FixtureProvider):
        @override
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            if 2 in version_range:
                return 2

            return super().choose_version(package, version_range)

        @override
        def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
            return self.graph.get(package, {}).get(version, {})

    with pytest.raises(ProviderConformanceError, match="outside fixture"):
        check_fixture({"app": {1: {}}}, Inventing)


def test_solver_limits_are_not_reported_as_unsatisfiability() -> None:
    class Limited(FixtureProvider):
        @override
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
        @override
        def widen_decision(self, package: str, version: int) -> Range[int]:
            return Range.singleton(version)

    check_fixture({"app": {1: {"lib": Range.full()}, 2: {}}, "lib": {1: {}}}, Widening)


def test_precheck_can_supply_a_sound_clause_instead_of_a_candidate() -> None:
    class Prechecking(FixtureProvider):
        pending: list[Incompatibility[str, int]]

        @override
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> None:
            self.pending = [
                Incompatibility(
                    [Term(package, version_range, positive=True)],
                    cause=IncompatibilityCause.NO_VERSIONS,
                )
            ]

        @override
        def has_satisfying_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> bool:
            return False

        @override
        def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
            clauses = self.pending
            self.pending = []

            return clauses

    check_fixture({"app": {1: {"missing": Range.full()}}}, Prechecking)


def test_scan_precedes_direct_queries() -> None:
    class Initialized(FixtureProvider):
        scanned = False

        @override
        def begin_decision_scan(self) -> None:
            self.scanned = True

        @override
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
        @override
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

        @override
        def begin_decision_scan(self) -> None:
            assert self.hinted

    check_fixture({"app": {1: {}}}, ScanAware)


def test_candidate_hashes_remain_stable() -> None:
    class MutableVersion(int):
        salt = 0

        @override
        def __hash__(self) -> int:
            return hash(int(self)) + self.salt

    version = MutableVersion(1)

    class HashChanging(FixtureProvider):
        @override
        def narrow_for_display(
            self, package: str, constraint: RangeProtocol[int]
        ) -> RangeProtocol[int]:
            version.salt += 1
            return constraint

    with pytest.raises(ProviderConformanceError, match="candidate hash changed"):
        check_fixture({"app": {version: {"missing": Range.full()}}}, HashChanging)


@pytest.mark.parametrize("widen_versions", [(), (2,), (1, 2)])
def test_widening_is_checked_when_another_version_is_actually_chosen(
    widen_versions: tuple[int, ...],
) -> None:
    class OldestWithBroadFacts(FixtureProvider):
        @override
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

        @override
        def prioritize(
            self,
            package: str,
            version_range: RangeProtocol[int],
            conflict_counts: Mapping[str, int],
            culprit_counts: Mapping[str, int] | None = None,
        ) -> int:
            return {"app": 0, "lib": 1, "bridge": 2}[package]

        @override
        def widen_decision(self, package: str, version: int) -> Range[int]:
            return (
                Range.full() if version in widen_versions else Range.singleton(version)
            )

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

    if widen_versions:
        with pytest.raises(
            ProviderConformanceError, match="widening merges unequal dependencies"
        ):
            run()
    else:
        run()


@pytest.mark.parametrize("return_input", [False, True])
def test_display_callback_cannot_mutate_its_input(return_input: bool) -> None:
    class MutatingDisplay(FixtureProvider):
        @override
        def narrow_for_display(
            self, package: str, constraint: RangeProtocol[int]
        ) -> Range[int]:
            assert isinstance(constraint, Range)
            original = Range[int](constraint._intervals)
            constraint._intervals = ()
            constraint._hash = 0
            return constraint if return_input else original

    with pytest.raises(ProviderConformanceError, match="display narrowing"):
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

        @override
        def has_satisfying_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> bool:
            if phase == "probe":
                self.change_full_range(version_range)
            return any(
                version in version_range for version in self.graph.get(package, {})
            )

        @override
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            if phase == "choice":
                self.change_full_range(version_range)
            return super().choose_version(package, version_range)

    with pytest.raises(ProviderConformanceError, match="mutated.*range"):
        check_fixture({"app": {1: {}, 2: {}}}, MutatingQuery)
