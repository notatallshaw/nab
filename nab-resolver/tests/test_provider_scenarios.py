"""Run reusable scenarios through ordinary and opaque provider interfaces."""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

import pytest

from nab_resolver._compat import override
from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, ResolverProvider
from nab_resolver.root import ROOT
from nab_resolver.testing import (
    ProviderScenario,
    check_provider,
    check_provider_scenarios,
)
from nab_resolver.types import RangeProtocol

if TYPE_CHECKING:
    from typing_extensions import Self

    from nab_resolver.types import RelationProtocol

_PackageType = TypeVar("_PackageType", bound=Hashable)
_VersionType = TypeVar("_VersionType", bound=Hashable)


class ScenarioProvider(
    BaseProvider[_PackageType, _VersionType], Generic[_PackageType, _VersionType]
):
    """Read finite fixture data without assuming package or version key types."""

    def __init__(self, scenario: ProviderScenario[_PackageType, _VersionType]) -> None:
        self.scenario = scenario

    def choose_version(
        self, package: _PackageType, version_range: RangeProtocol[_VersionType]
    ) -> _VersionType | None:
        # Declared order avoids requiring version keys to support sorting.
        return next(
            (
                version
                for version in reversed(tuple(self.scenario.graph.get(package, {})))
                if version in version_range
            ),
            None,
        )

    def has_satisfying_version(
        self, package: _PackageType, version_range: RangeProtocol[_VersionType]
    ) -> bool:
        return self.choose_version(package, version_range) is not None

    def get_dependencies(
        self, package: _PackageType, version: _VersionType
    ) -> Mapping[_PackageType, RangeProtocol[_VersionType]]:
        return self.scenario.graph[package][version]

    def prioritize(
        self,
        package: _PackageType,
        version_range: RangeProtocol[_VersionType],
        conflict_counts: Mapping[_PackageType, int],
        culprit_counts: Mapping[_PackageType, int] | None = None,
    ) -> int:
        return len(self.scenario.graph.get(package, {}))

    def widen_decision(
        self, package: _PackageType, version: _VersionType
    ) -> RangeProtocol[_VersionType] | None:
        return None


def test_reusable_scenarios_cover_named_resolution_contracts() -> None:
    seen: set[str] = set()

    def factory(scenario: ProviderScenario[str, int]) -> ScenarioProvider[str, int]:
        seen.add(scenario.name)
        return ScenarioProvider(scenario)

    check_provider_scenarios(
        factory,
        packages=("first", "second", "third"),
        versions=(1, 2),
        range_type=Range,
    )

    assert seen == {
        "empty",
        "missing",
        "chain",
        "diamond",
        "cycle",
        "conflicting roots",
        "backtracking",
        "reverse backtracking",
        "pinned root",
        "constraint",
        "unused constraint",
        "equal dependencies",
    }


@dataclass(frozen=True)
class PackageKey:
    """A hashable package identity."""

    name: str


@dataclass(frozen=True, order=True)
class Revision:
    """An ordered version identity."""

    number: int


def test_scenarios_accept_opaque_keys() -> None:
    check_provider_scenarios(
        ScenarioProvider[PackageKey, Revision],
        packages=(PackageKey("one"), PackageKey("two"), PackageKey("three")),
        versions=(Revision(10), Revision(20)),
        range_type=Range,
    )


def test_fixture_mappings_are_read_only() -> None:
    scenario = ProviderScenario[str, int](
        "snapshot", {"first": Range.full()}, {"first": {1: {}}}
    )
    dependencies = scenario.graph["first"][1]

    with pytest.raises(TypeError):
        dependencies["second"] = Range.full()  # type: ignore[index] - deliberately mutates a read-only fixture


@pytest.mark.parametrize("target", ["root", "constraint"])
def test_mutating_input_ranges_cannot_rewrite_the_oracle(target: str) -> None:
    changed = Range.singleton(1)
    graph = {"first": {1: {}, 2: {}}}
    roots = {"first": changed if target == "root" else Range.full()}
    constraints = {"first": changed} if target == "constraint" else {}
    fixture = ProviderScenario("mutable", roots, graph, constraints=constraints)

    class MutatingProvider(ScenarioProvider[str, int]):
        @override
        def get_dependencies(
            self, package: str, version: int
        ) -> Mapping[str, RangeProtocol[int]]:
            # Break immutability while keeping ordinary range algebra consistent.
            changed._intervals = ((2, True, 2, True),)
            changed._hash = 0
            return super().get_dependencies(package, version)

    with pytest.raises(AssertionError, match="fixture.*changed"):
        check_provider(
            MutatingProvider,
            fixture,
            range_type=Range,
            root_version=0,
            max_combinations=2,
        )


class IntegerRange:
    """An independent range adapter with precise subclass-preserving return types."""

    def __init__(self, values: Range[int]) -> None:
        self.values = values

    @classmethod
    def empty(cls) -> Self:
        return cls(Range.empty())

    @classmethod
    def full(cls) -> Self:
        return cls(Range.full())

    @classmethod
    def singleton(cls, version: int) -> Self:
        return cls(Range.singleton(version))

    @property
    def is_empty(self) -> bool:
        return self.values.is_empty

    def __contains__(self, version: object) -> bool:
        """Test membership through the wrapped range."""
        return version in self.values

    def __and__(self, other: object) -> Self:
        """Intersect values while preserving the adapter type."""
        if not isinstance(other, IntegerRange):
            return NotImplemented

        return type(self)(self.values & other.values)

    def __or__(self, other: object) -> Self:
        """Union values while preserving the adapter type."""
        if not isinstance(other, IntegerRange):
            return NotImplemented

        return type(self)(self.values | other.values)

    def __sub__(self, other: object) -> Self:
        """Subtract values while preserving the adapter type."""
        if not isinstance(other, IntegerRange):
            return NotImplemented

        return type(self)(self.values - other.values)

    def __invert__(self) -> Self:
        """Complement values while preserving the adapter type."""
        return type(self)(~self.values)

    def is_subset(self, other: Self) -> bool:
        return self.values.is_subset(other.values)

    def is_superset(self, other: Self) -> bool:
        return self.values.is_superset(other.values)

    def is_disjoint(self, other: Self) -> bool:
        return self.values.is_disjoint(other.values)

    def relation(self, other: Self) -> RelationProtocol:
        return self.values.relation(other.values)

    @override
    def __eq__(self, other: object) -> bool:
        """Compare wrapped range values."""
        if not isinstance(other, IntegerRange):
            return NotImplemented

        return self.values == other.values

    @override
    def __hash__(self) -> int:
        """Hash the wrapped range value."""
        return hash(self.values)


def test_scenarios_accept_another_range_backend() -> None:
    check_provider_scenarios(
        ScenarioProvider[str, int],
        packages=("one", "two", "three"),
        versions=(1, 2),
        range_type=IntegerRange,
    )


def test_invalid_singleton_backend_is_refused_before_provider_work() -> None:
    class DropsHigherSingleton(IntegerRange):
        @classmethod
        @override
        def singleton(cls, version: int) -> Self:
            return cls.empty() if version == 2 else cls(Range.singleton(version))

    def factory(scenario: ProviderScenario[str, int]) -> ScenarioProvider[str, int]:
        raise AssertionError("factory called with invalid range backend")

    with pytest.raises(ValueError, match="singleton"):
        check_provider_scenarios(
            factory,
            packages=("one", "two", "three"),
            versions=(1, 2),
            range_type=DropsHigherSingleton,
        )


class RootAwareProvider(ScenarioProvider[str, int]):
    """Bind candidate availability to the roots and constraints of one scope."""

    @override
    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        roots = self.scenario.requirements.get(package, Range.full())
        constraints = self.scenario.constraints.get(package, Range.full())
        return next(
            (
                version
                for version in reversed(tuple(self.scenario.graph.get(package, {})))
                if version in version_range
                and version in roots
                and version in constraints
            ),
            None,
        )

    @override
    def widen_decision(self, package: str, version: int) -> Range[int]:
        return Range.full()


def test_singleton_queries_get_their_actual_provider_scope() -> None:
    scenario = ProviderScenario[str, int](
        "pinned",
        {"first": Range.singleton(1)},
        {
            "first": {
                1: {"second": Range.singleton(1)},
                2: {"second": Range.singleton(2)},
            },
            "second": {1: {}, 2: {}},
        },
    )
    check_provider(
        RootAwareProvider,
        scenario,
        range_type=Range,
        root_version=0,
        max_combinations=4,
    )


def test_provider_instances_need_only_support_one_resolution() -> None:
    class OneScopeProvider(ScenarioProvider[str, int]):
        used = False

        @override
        def prioritize(
            self,
            package: str,
            version_range: RangeProtocol[int],
            conflict_counts: Mapping[str, int],
            culprit_counts: Mapping[str, int] | None = None,
        ) -> int:
            if self.used:
                raise RuntimeError("resolution scope reused")
            self.used = True
            return 0

    scenario = ProviderScenario[str, int](
        "one scope", {"first": Range.full()}, {"first": {1: {}}}
    )
    check_provider(
        OneScopeProvider, scenario, range_type=Range, root_version=0, max_combinations=1
    )


@pytest.mark.parametrize(
    ("packages", "versions"),
    [
        (("one", "two"), (1, 2)),
        (("one", "two", "three"), (1,)),
        (("one", "one", "three"), (1, 2)),
        (("one", "two", "three"), (1, 1)),
    ],
)
def test_invalid_keys_are_refused_before_provider_work(
    packages: tuple[str, ...], versions: tuple[int, ...]
) -> None:
    def forbidden(scenario: ProviderScenario[str, int]) -> ScenarioProvider[str, int]:
        raise AssertionError("factory called with invalid keys")

    with pytest.raises(ValueError, match="distinct"):
        check_provider_scenarios(
            forbidden, packages=packages, versions=versions, range_type=Range
        )


@pytest.mark.parametrize("bad_method", ["empty", "full"])
def test_invalid_range_boundaries_are_refused(bad_method: str) -> None:
    class BadEmpty(IntegerRange):
        @classmethod
        @override
        def empty(cls) -> Self:
            return cls(Range.singleton(1))

    class BadFull(IntegerRange):
        @classmethod
        @override
        def full(cls) -> Self:
            return cls(Range.singleton(1))

    def forbidden(scenario: ProviderScenario[str, int]) -> ScenarioProvider[str, int]:
        raise AssertionError("factory called with invalid range boundaries")

    with pytest.raises(ValueError, match="empty/full"):
        check_provider_scenarios(
            forbidden,
            packages=("one", "two", "three"),
            versions=(1, 2),
            range_type=BadEmpty if bad_method == "empty" else BadFull,
        )


class ChangingChoice(ScenarioProvider[str, int]):
    """Vary valid choices while keeping availability probes observational."""

    choices = 0
    diagnostics = 0

    @override
    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        self.choices += 1
        versions = tuple(self.scenario.graph.get(package, {}))
        ordered = versions if self.choices % 2 else tuple(reversed(versions))
        return next((version for version in ordered if version in version_range), None)

    @override
    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        self.diagnostics += 1
        return any(
            version in version_range for version in self.scenario.graph.get(package, {})
        )


def choice_state(provider: ResolverProvider[str, int]) -> int:
    """Observe chooser state while allowing diagnostic evidence to accumulate."""
    assert isinstance(provider, ChangingChoice)
    return provider.choices


def test_choice_order_can_vary_and_probes_can_collect_diagnostics() -> None:
    check_provider_scenarios(
        ChangingChoice,
        packages=("one", "two", "three"),
        versions=(1, 2),
        range_type=Range,
        probe_state=choice_state,
    )


def test_probe_state_hook_detects_permanent_choice_changes() -> None:
    class ProbeChangesChoice(ChangingChoice):
        @override
        def has_satisfying_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> bool:
            self.choices += 1
            return super().has_satisfying_version(package, version_range)

    with pytest.raises(AssertionError, match="probe changed decision state"):
        check_provider_scenarios(
            ProbeChangesChoice,
            packages=("one", "two", "three"),
            versions=(1, 2),
            range_type=Range,
            probe_state=choice_state,
        )


def test_suite_failure_names_the_scenario_and_preserves_conflict_context() -> None:
    class MissingCandidates(ScenarioProvider[str, int]):
        @override
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> None:
            return None

    with pytest.raises(
        AssertionError, match="chain: provider rejected a satisfiable fixture"
    ) as caught:
        check_provider_scenarios(
            MissingCandidates,
            packages=("one", "two", "three"),
            versions=(1, 2),
            range_type=Range,
        )

    assert isinstance(caught.value.__context__, ResolutionError)


def test_suite_does_not_reinterpret_operational_errors() -> None:
    failure = OSError("provider data unavailable")

    def factory(scenario: ProviderScenario[str, int]) -> ScenarioProvider[str, int]:
        raise failure

    with pytest.raises(OSError, match="provider data unavailable") as caught:
        check_provider_scenarios(
            factory, packages=("one", "two", "three"), versions=(1, 2), range_type=Range
        )

    assert caught.value is failure


class RootHintProvider(ScenarioProvider[str, int]):
    """Require the same root context that the real resolver supplies."""

    @override
    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        assert ROOT in positive_ranges
        assert self.scenario.requirements.keys() <= positive_ranges.keys()
        for package, original in self.scenario.requirements.items():
            assert positive_ranges[package].is_subset(original)


def test_observations_preserve_real_root_and_virtual_root_context() -> None:
    scenario = ProviderScenario[str, int](
        "root context",
        {"first": Range.singleton(1)},
        {
            "first": {1: {"second": Range.singleton(1)}, 2: {}},
            "second": {1: {}},
        },
    )
    check_provider(
        RootHintProvider, scenario, range_type=Range, root_version=0, max_combinations=2
    )


def test_unselectable_advertised_versions_do_not_invalidate_widening() -> None:
    class PruningProvider(ScenarioProvider[str, int]):
        @override
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            if package == "first":
                return 1 if 1 in version_range else None
            return super().choose_version(package, version_range)

        @override
        def widen_decision(self, package: str, version: int) -> Range[int]:
            return Range.full()

    scenario = ProviderScenario[str, int](
        "pruned",
        {"first": Range.full()},
        {
            "first": {1: {"second": Range.singleton(1)}, 2: {"missing": Range.full()}},
            "second": {1: {}},
        },
    )
    check_provider(
        PruningProvider, scenario, range_type=Range, root_version=0, max_combinations=2
    )


@pytest.mark.parametrize("prefer_first", [False, True])
def test_scenarios_detect_harmful_widening_in_both_choice_orders(
    prefer_first: bool,
) -> None:
    class AlwaysWiden(ScenarioProvider[str, int]):
        @override
        def choose_version(
            self, package: str, version_range: RangeProtocol[int]
        ) -> int | None:
            versions = tuple(self.scenario.graph.get(package, {}))
            ordered = versions if prefer_first else tuple(reversed(versions))
            return next(
                (version for version in ordered if version in version_range), None
            )

        @override
        def widen_decision(self, package: str, version: int) -> Range[int]:
            return Range.full()

    with pytest.raises(AssertionError, match="provider rejected a satisfiable fixture"):
        check_provider_scenarios(
            AlwaysWiden,
            packages=("one", "two", "three"),
            versions=(1, 2),
            range_type=Range,
        )
