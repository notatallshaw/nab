"""Small, reusable checks for implementations of ResolverProvider."""

from __future__ import annotations

from itertools import product
from math import prod
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, TypeVar

from ._compat import override
from .errors import ResolutionError, ResolutionTerminatedError
from .resolver import BaseProvider, Resolver
from .types import VersionType

_PackageType = TypeVar("_PackageType", bound="Hashable")

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Mapping, Sequence

    from .resolver import ResolverProvider, Solution
    from .types import Incompatibility, RangeProtocol

    _ProbeState = Callable[[ResolverProvider[_PackageType, VersionType]], object]
    _Requirements = Mapping[_PackageType, RangeProtocol[VersionType]]
    _DependencyGraph = Mapping[
        _PackageType, Mapping[VersionType, _Requirements[_PackageType, VersionType]]
    ]
    _Membership = Mapping[_PackageType, frozenset[VersionType]]
    _OracleGraph = Mapping[
        _PackageType, Mapping[VersionType, _Membership[_PackageType, VersionType]]
    ]

__all__ = ["ProviderScenario", "check_provider", "check_provider_scenarios"]

# Two roots and their shared dependency need three package keys.
_SCENARIO_PACKAGE_COUNT = 3
_SCENARIO_VERSION_COUNT = 2


class ProviderScenario(Generic[_PackageType, VersionType]):
    """A named graph, required packages and optional constraints for one check."""

    __slots__ = ("constraints", "graph", "name", "requirements")

    def __init__(
        self,
        name: str,
        requirements: _Requirements[_PackageType, VersionType],
        graph: _DependencyGraph[_PackageType, VersionType],
        *,
        constraints: _Requirements[_PackageType, VersionType] | None = None,
    ) -> None:
        """Copy the fixture mappings before giving them to a provider factory."""
        self.name = name
        self.requirements: _Requirements[_PackageType, VersionType] = MappingProxyType(
            dict(requirements)
        )

        copied_constraints: dict[_PackageType, RangeProtocol[VersionType]] = {}
        if constraints is not None:
            copied_constraints.update(constraints)
        self.constraints: _Requirements[_PackageType, VersionType] = MappingProxyType(
            copied_constraints
        )

        # Copy each mapping level while retaining immutable range values.
        copied_graph: dict[
            _PackageType,
            Mapping[VersionType, _Requirements[_PackageType, VersionType]],
        ] = {}
        for package, versions in graph.items():
            copied_versions: dict[
                VersionType, _Requirements[_PackageType, VersionType]
            ] = {}
            for version, dependencies in versions.items():
                copied_versions[version] = MappingProxyType(dict(dependencies))
            copied_graph[package] = MappingProxyType(copied_versions)
        self.graph: _DependencyGraph[_PackageType, VersionType] = MappingProxyType(
            copied_graph
        )


def _membership(
    requirements: _Requirements[_PackageType, VersionType],
    graph: _DependencyGraph[_PackageType, VersionType],
) -> dict[_PackageType, frozenset[VersionType]]:
    """Snapshot range membership over every version supplied by the fixture."""
    return {
        package: frozenset(
            version for version in graph.get(package, {}) if version in allowed
        )
        for package, allowed in requirements.items()
    }


def _range_membership(
    allowed: RangeProtocol[VersionType], versions: Sequence[VersionType]
) -> tuple[bool, ...]:
    """Snapshot an input range without retaining mutable keys or range state."""
    return tuple(version in allowed for version in versions)


class _FixtureMembership(Generic[_PackageType, VersionType]):
    """Independent membership facts retained across all provider calls."""

    __slots__ = ("constraints", "graph", "requirements")

    def __init__(
        self,
        graph: _OracleGraph[_PackageType, VersionType],
        requirements: _Membership[_PackageType, VersionType],
        constraints: _Membership[_PackageType, VersionType],
    ) -> None:
        self.graph = graph
        self.requirements = requirements
        self.constraints = constraints


def _fixture_membership(
    fixture: ProviderScenario[_PackageType, VersionType],
) -> _FixtureMembership[_PackageType, VersionType]:
    """Copy range meaning into immutable sets before the provider can change it."""
    # Store keys rather than ranges, so mutations cannot also rewrite the oracle.
    graph = {
        package: {
            version: _membership(dependencies, fixture.graph)
            for version, dependencies in versions.items()
        }
        for package, versions in fixture.graph.items()
    }
    return _FixtureMembership[_PackageType, VersionType](
        graph,
        _membership(fixture.requirements, fixture.graph),
        _membership(fixture.constraints, fixture.graph),
    )


def _checked_singleton(
    range_type: type[RangeProtocol[VersionType]],
    version: VersionType,
    versions: Sequence[VersionType],
) -> RangeProtocol[VersionType]:
    """Keep bad range constructors from changing both the input and its oracle."""
    singleton = range_type.singleton(version)

    if frozenset(item for item in versions if item in singleton) != frozenset(
        (version,)
    ):
        message = "range singleton does not preserve version membership"
        raise ValueError(message)
    return singleton


def _check_range_type(
    range_type: type[RangeProtocol[VersionType]],
    versions: Sequence[VersionType],
) -> None:
    """Refuse range backends that misrepresent the supplied fixture keys."""
    empty = range_type.empty()
    full = range_type.full()

    if any(version in empty or version not in full for version in versions):
        message = "range empty/full does not preserve version membership"
        raise ValueError(message)

    for version in versions:
        _checked_singleton(range_type, version, versions)


def _valid_selection(
    selection: Mapping[_PackageType, VersionType],
    requirements: _Membership[_PackageType, VersionType],
    graph: _OracleGraph[_PackageType, VersionType],
    constraints: _Membership[_PackageType, VersionType],
) -> bool:
    """Check reachable dependencies using sets, without resolver range operations."""
    pending = list(requirements.items())
    visited: set[_PackageType] = set()

    while pending:
        package, allowed = pending.pop()
        if package not in selection or selection[package] not in allowed:
            return False

        version = selection[package]
        if package in constraints and version not in constraints[package]:
            return False
        if package not in visited:
            visited.add(package)
            pending.extend(graph[package][version].items())

    return True


def _has_solution(
    requirements: _Membership[_PackageType, VersionType],
    graph: _OracleGraph[_PackageType, VersionType],
    constraints: _Membership[_PackageType, VersionType],
    max_combinations: int,
) -> bool:
    """Try every combination of fixture versions, without querying a provider."""
    domains = {
        package: tuple(versions) for package, versions in graph.items() if versions
    }
    combinations = prod(len(versions) for versions in domains.values())

    if max_combinations < 1 or combinations > max_combinations:
        message = (
            f"fixture needs {combinations} combinations; budget is {max_combinations}"
        )
        raise ValueError(message)

    # The closure check ignores choices outside the required dependency graph.
    return any(
        _valid_selection(
            dict(zip(domains, choices, strict=True)), requirements, graph, constraints
        )
        for choices in product(*domains.values())
    )


class _QueryAnswer(Generic[_PackageType, VersionType]):
    """Retain one choice until its pending clauses and backtrack requests are read."""

    __slots__ = ("available", "chosen", "package")

    def __init__(
        self, package: _PackageType, chosen: VersionType | None, *, available: bool
    ) -> None:
        self.package = package
        self.chosen = chosen
        self.available = available


class _WidenedDependencies(Generic[_PackageType, VersionType]):
    """Retain a widened fact until another version is actually chosen."""

    __slots__ = ("allowed", "dependencies", "package")

    def __init__(
        self,
        package: _PackageType,
        allowed: RangeProtocol[VersionType],
        dependencies: _Membership[_PackageType, VersionType],
    ) -> None:
        self.package = package
        self.allowed = allowed
        self.dependencies = dependencies


class _CheckingProvider(BaseProvider[_PackageType, VersionType]):
    """Observe real resolver calls without inventing partial-solution context."""

    def __init__(
        self,
        provider: ResolverProvider[_PackageType, VersionType],
        *,
        scenario: ProviderScenario[_PackageType, VersionType],
        expected: _FixtureMembership[_PackageType, VersionType],
        versions: Sequence[VersionType],
        probe_state: _ProbeState[_PackageType, VersionType] | None,
    ) -> None:
        self.provider = provider
        self.scenario = scenario
        self.expected = expected
        self.versions = versions
        self.probe_state = probe_state

        self._answer: _QueryAnswer[_PackageType, VersionType] | None = None
        self._pending = False
        self._known: dict[_PackageType, set[VersionType]] = {}
        self._hashes: list[tuple[VersionType, int]] = []
        self._retained: list[
            tuple[
                _Requirements[_PackageType, VersionType],
                _Membership[_PackageType, VersionType],
            ]
        ] = []
        self._widened: list[_WidenedDependencies[_PackageType, VersionType]] = []

    def _check_retained(self) -> None:
        """Check earlier answers while the provider's resolution is still active."""
        for version, original in self._hashes:
            assert hash(version) == original, "candidate hash changed"

        for actual, recorded in self._retained:
            assert _membership(actual, self.scenario.graph) == recorded, (
                "returned dependency mapping was mutated"
            )

    def _record_choice(
        self,
        package: _PackageType,
        version: VersionType,
        allowed: RangeProtocol[VersionType],
    ) -> None:
        """Record a real choice and compare it with earlier widened facts."""
        assert version in allowed, f"chosen version outside query for {package!r}"
        assert version in self.expected.graph.get(package, {}), (
            f"chosen version outside fixture for {package!r}"
        )

        self._known.setdefault(package, set()).add(version)
        self._hashes.append((version, hash(version)))

        # Only choices seen in this resolution prove that a version is selectable.
        for widened in self._widened:
            if widened.package == package and version in widened.allowed:
                assert widened.dependencies == self.expected.graph[package][version], (
                    f"widening merges unequal dependencies for {package!r}"
                )

    def has_satisfying_version(
        self, package: _PackageType, version_range: RangeProtocol[VersionType]
    ) -> bool:
        """Probe availability without changing owner-selected decision state."""
        self._check_retained()

        before = None if self.probe_state is None else self.probe_state(self.provider)
        input_membership = _range_membership(version_range, self.versions)
        available = self.provider.has_satisfying_version(package, version_range)

        assert _range_membership(version_range, self.versions) == input_membership, (
            "availability probe mutated its range"
        )
        if self.probe_state is not None:
            assert self.probe_state(self.provider) == before, (
                "availability probe changed decision state"
            )
        self._check_retained()

        return available

    def choose_version(
        self, package: _PackageType, version_range: RangeProtocol[VersionType]
    ) -> VersionType | None:
        """Retain a real choice until its pending work has been read."""
        # Extra availability probes use the solver's actual context and range.
        available = self.has_satisfying_version(package, version_range)
        input_membership = _range_membership(version_range, self.versions)
        chosen = self.provider.choose_version(package, version_range)

        assert _range_membership(version_range, self.versions) == input_membership, (
            "candidate query mutated its range"
        )
        self._check_retained()

        if chosen is not None:
            self._record_choice(package, chosen, version_range)
        self._answer = _QueryAnswer[_PackageType, VersionType](
            package, chosen, available=available
        )

        return chosen

    @override
    def consume_pending_clauses(
        self,
    ) -> list[Incompatibility[_PackageType, VersionType]]:
        """Forward clauses and record whether they replace an absence."""
        clauses = self.provider.consume_pending_clauses()

        self._pending = bool(clauses)

        return clauses

    @override
    def consume_force_backtrack_targets(self) -> list[_PackageType]:
        """Check availability agreement after all pending work is known."""
        targets = self.provider.consume_force_backtrack_targets()

        answer = self._answer
        pending = self._pending
        self._answer = None
        self._pending = False

        if answer is not None and not (answer.chosen is None and (pending or targets)):
            assert answer.available == (answer.chosen is not None), (
                f"availability probe disagrees for {answer.package!r}"
            )

        return targets

    def get_dependencies(
        self, package: _PackageType, version: VersionType
    ) -> Mapping[_PackageType, RangeProtocol[VersionType]]:
        """Compare real metadata reads and retain their mappings during this solve."""
        self._check_retained()

        actual = self.provider.get_dependencies(package, version)
        captured = _membership(actual, self.scenario.graph)
        assert captured == self.expected.graph[package][version], (
            f"wrong dependencies for {(package, version)!r}"
        )

        self._retained.append((actual, captured))

        # Repeated reads happen after the real decision, with its original hints.
        repeated = self.provider.get_dependencies(package, version)
        assert _membership(repeated, self.scenario.graph) == captured, (
            f"dependencies changed for {(package, version)!r}"
        )

        self._retained.append((repeated, captured))
        self._check_retained()

        return actual

    def widen_decision(
        self, package: _PackageType, version: VersionType
    ) -> RangeProtocol[VersionType] | None:
        """Compare widened facts with versions actually chosen in this resolution."""
        widened = self.provider.widen_decision(package, version)
        if widened is not None:
            assert version in widened, f"widening excludes decision for {package!r}"

            dependencies = self.expected.graph[package][version]
            for known in self._known.get(package, ()):
                if known in widened:
                    assert dependencies == self.expected.graph[package][known], (
                        f"widening merges unequal dependencies for {package!r}"
                    )

            self._widened.append(_WidenedDependencies(package, widened, dependencies))
        self._check_retained()

        return widened

    @override
    def narrow_for_display(
        self, package: _PackageType, constraint: RangeProtocol[VersionType]
    ) -> RangeProtocol[VersionType]:
        """Preserve observed version membership in real error reports."""
        known = (
            tuple(self._known.get(package, ()))
            if package in self.scenario.graph
            else tuple(self.versions)
        )
        original = _range_membership(constraint, known)
        shown = self.provider.narrow_for_display(package, constraint)

        # Returning the same mutated object must not change both sides of the check.
        assert _range_membership(shown, known) == original, (
            f"display narrowing changes membership for {package!r}"
        )
        assert _range_membership(constraint, known) == original, (
            f"display narrowing mutated input for {package!r}"
        )
        self._check_retained()

        return shown

    @override
    def begin_decision_scan(self) -> Callable[[_PackageType], bool] | None:
        return self.provider.begin_decision_scan()

    @override
    def is_ready(self, package: _PackageType) -> bool:
        return self.provider.is_ready(package)

    def prioritize(
        self,
        package: _PackageType,
        version_range: RangeProtocol[VersionType],
        conflict_counts: Mapping[_PackageType, int],
        culprit_counts: Mapping[_PackageType, int] | None = None,
    ) -> object:
        return self.provider.prioritize(
            package, version_range, conflict_counts, culprit_counts
        )

    @override
    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[_PackageType, RangeProtocol[VersionType]],
        decisions: Mapping[_PackageType, VersionType],
    ) -> None:
        self.provider.receive_partial_solution_hint(positive_ranges, decisions)

    def receive_decision_scan_hint(
        self,
        positive_ranges: Mapping[_PackageType, RangeProtocol[VersionType]],
        decisions: Mapping[_PackageType, VersionType],
    ) -> None:
        # This documented hook is optional for structural providers.
        hint = getattr(self.provider, "receive_decision_scan_hint", None)
        if hint is not None:
            hint(positive_ranges, decisions)


def _check_solution(
    solution: Solution[_PackageType, VersionType],
    requirements: _Membership[_PackageType, VersionType],
    graph: _OracleGraph[_PackageType, VersionType],
    constraints: _Membership[_PackageType, VersionType],
) -> None:
    """Check the result's pins, reachable packages, edges and roots independently."""
    assert _valid_selection(solution.pins, requirements, graph, constraints), (
        "solution violates fixture requirements"
    )

    reachable: set[_PackageType] = set()
    pending = list(requirements)
    edges: set[tuple[_PackageType, _PackageType]] = set()

    while pending:
        package = pending.pop()
        if package in reachable:
            continue

        reachable.add(package)
        dependencies = graph[package][solution.pins[package]]
        edges.update((package, child) for child in dependencies)
        pending.extend(dependencies)

    assert reachable == solution.pins.keys(), "solution includes unreachable packages"
    assert edges == set(solution.edges), (
        "solution edges differ from fixture dependencies"
    )
    assert tuple(requirements) == solution.roots, (
        "solution roots differ from input order"
    )


class _ResolutionQuery(Generic[_PackageType, VersionType]):
    """Pair a provider's actual input scope with independent root membership."""

    __slots__ = ("membership", "scenario")

    def __init__(
        self,
        scenario: ProviderScenario[_PackageType, VersionType],
        membership: _Membership[_PackageType, VersionType],
    ) -> None:
        self.scenario = scenario
        self.membership = membership


def _resolution_queries(
    fixture: ProviderScenario[_PackageType, VersionType],
    expected: _FixtureMembership[_PackageType, VersionType],
    range_type: type[RangeProtocol[VersionType]],
    versions: Sequence[VersionType],
) -> list[_ResolutionQuery[_PackageType, VersionType]]:
    """Vary root order and isolate each version without reinterpreting inputs."""
    reversed_roots = dict(reversed(tuple(fixture.requirements.items())))
    reversed_membership = dict(reversed(tuple(expected.requirements.items())))
    reordered = ProviderScenario(
        fixture.name, reversed_roots, fixture.graph, constraints=fixture.constraints
    )
    queries = [
        _ResolutionQuery[_PackageType, VersionType](fixture, expected.requirements),
        _ResolutionQuery[_PackageType, VersionType](reordered, reversed_membership),
        _ResolutionQuery[_PackageType, VersionType](fixture, expected.requirements),
    ]

    # Singleton roots expose omissions hidden by another feasible alternative.
    for package, choices in fixture.graph.items():
        for version in choices:
            roots = {package: _checked_singleton(range_type, version, versions)}
            scenario = ProviderScenario(
                fixture.name, roots, fixture.graph, constraints=fixture.constraints
            )
            membership: dict[_PackageType, frozenset[VersionType]] = {
                package: frozenset((version,))
            }
            queries.append(
                _ResolutionQuery[_PackageType, VersionType](scenario, membership)
            )
    return queries


def _check_fixture(
    scenario: ProviderScenario[_PackageType, VersionType],
    expected: _FixtureMembership[_PackageType, VersionType],
) -> None:
    """Keep shared range mutations from changing the expected answers."""
    current = _fixture_membership(scenario)
    assert current.graph == expected.graph, "fixture metadata changed"
    assert current.requirements == expected.requirements, "fixture requirements changed"
    assert current.constraints == expected.constraints, "fixture constraints changed"


def check_provider(
    factory: Callable[
        [ProviderScenario[_PackageType, VersionType]],
        ResolverProvider[_PackageType, VersionType],
    ],
    scenario: ProviderScenario[_PackageType, VersionType],
    *,
    range_type: type[RangeProtocol[VersionType]],
    root_version: VersionType,
    max_combinations: int,
    probe_state: _ProbeState[_PackageType, VersionType] | None = None,
) -> None:
    """Check one small, independently specified fixture against a provider.

    The graph defines the candidate versions and their unchanging dependencies.
    factory receives each query's roots and constraints and returns a fresh provider.
    Contract failures raise AssertionError; operational errors propagate.
    max_combinations limits search before the provider is called.
    probe_state may return an independent snapshot of decision state; exclude
    permitted cache and diagnostic changes from that snapshot.
    Run these checks with assertions enabled.
    """
    # Freeze inputs before provider construction, including shared range values.
    expected = _fixture_membership(scenario)
    _has_solution(
        expected.requirements, expected.graph, expected.constraints, max_combinations
    )
    supplied_versions = tuple(
        dict.fromkeys(
            (
                root_version,
                *(
                    version
                    for choices in scenario.graph.values()
                    for version in choices
                ),
            )
        )
    )
    _check_range_type(range_type, supplied_versions)

    queries = _resolution_queries(scenario, expected, range_type, supplied_versions)
    for query in queries:
        _check_fixture(scenario, expected)
        possible = _has_solution(
            query.membership, expected.graph, expected.constraints, max_combinations
        )

        # Provider state belongs to one resolution; shared caches belong to its factory.
        provider = _CheckingProvider(
            factory(query.scenario),
            scenario=query.scenario,
            expected=expected,
            versions=supplied_versions,
            probe_state=probe_state,
        )
        resolver = Resolver[_PackageType, VersionType](
            provider, range_type=range_type, root_version=root_version
        )
        try:
            solution = resolver.solve(
                query.scenario.requirements, query.scenario.constraints
            )
        except ResolutionTerminatedError:
            raise
        except ResolutionError as error:
            if error.incompatibility is None:
                raise
            assert not possible, "provider rejected a satisfiable fixture"
        else:
            assert possible, "provider accepted an impossible fixture"
            _check_solution(
                solution, query.membership, expected.graph, expected.constraints
            )

        _check_fixture(scenario, expected)


def _scenarios(
    packages: Sequence[_PackageType],
    versions: Sequence[VersionType],
    range_type: type[RangeProtocol[VersionType]],
) -> tuple[ProviderScenario[_PackageType, VersionType], ...]:
    """Build a bounded suite from the caller's package and version key types."""
    first_package, second_package, third_package = packages
    first_version, second_version = versions
    any_version = range_type.full()
    first_only = range_type.singleton(first_version)
    second_only = range_type.singleton(second_version)

    no_dependencies: dict[_PackageType, RangeProtocol[VersionType]] = {}
    first_dependency: dict[_PackageType, RangeProtocol[VersionType]] = {
        first_package: first_only
    }
    second_dependency: dict[_PackageType, RangeProtocol[VersionType]] = {
        second_package: first_only
    }
    second_alternative: dict[_PackageType, RangeProtocol[VersionType]] = {
        second_package: second_only
    }
    third_dependency: dict[_PackageType, RangeProtocol[VersionType]] = {
        third_package: first_only
    }
    any_second_version: dict[_PackageType, RangeProtocol[VersionType]] = {
        second_package: any_version
    }
    # Version catalogues keep each scenario graph to two mapping levels.
    requires_second: dict[VersionType, _Requirements[_PackageType, VersionType]] = {
        first_version: second_dependency
    }
    requires_third: dict[VersionType, _Requirements[_PackageType, VersionType]] = {
        first_version: third_dependency
    }
    requires_first: dict[VersionType, _Requirements[_PackageType, VersionType]] = {
        first_version: first_dependency
    }
    requires_second_alternative: dict[
        VersionType, _Requirements[_PackageType, VersionType]
    ] = {first_version: second_alternative}
    requires_any_second: dict[VersionType, _Requirements[_PackageType, VersionType]] = {
        first_version: any_second_version
    }
    leaf_versions: dict[VersionType, _Requirements[_PackageType, VersionType]] = {
        first_version: no_dependencies
    }
    two_leaf_versions: dict[VersionType, _Requirements[_PackageType, VersionType]] = {
        first_version: no_dependencies,
        second_version: no_dependencies,
    }
    alternating_dependencies: dict[
        VersionType, _Requirements[_PackageType, VersionType]
    ] = {first_version: second_dependency, second_version: second_alternative}
    equal_dependencies: dict[VersionType, _Requirements[_PackageType, VersionType]] = {
        first_version: second_dependency,
        second_version: second_dependency,
    }

    return (
        ProviderScenario[_PackageType, VersionType]("empty", {}, {}),
        ProviderScenario[_PackageType, VersionType](
            "missing", {first_package: any_version}, {}
        ),
        ProviderScenario[_PackageType, VersionType](
            "chain",
            {first_package: any_version},
            {
                first_package: requires_second,
                second_package: leaf_versions,
            },
        ),
        ProviderScenario[_PackageType, VersionType](
            "diamond",
            {first_package: any_version, second_package: any_version},
            {
                first_package: requires_third,
                second_package: requires_third,
                third_package: leaf_versions,
            },
        ),
        ProviderScenario[_PackageType, VersionType](
            "cycle",
            {first_package: any_version},
            {
                first_package: requires_second,
                second_package: requires_first,
            },
        ),
        ProviderScenario[_PackageType, VersionType](
            "conflicting roots",
            {first_package: first_only, third_package: first_only},
            {
                first_package: requires_second,
                third_package: requires_second_alternative,
                second_package: two_leaf_versions,
            },
        ),
        ProviderScenario[_PackageType, VersionType](
            "backtracking",
            {first_package: any_version, third_package: first_only},
            {
                first_package: alternating_dependencies,
                third_package: requires_second,
                second_package: two_leaf_versions,
            },
        ),
        ProviderScenario[_PackageType, VersionType](
            "reverse backtracking",
            {first_package: any_version, third_package: first_only},
            {
                first_package: alternating_dependencies,
                third_package: requires_second_alternative,
                second_package: two_leaf_versions,
            },
        ),
        ProviderScenario[_PackageType, VersionType](
            "pinned root",
            first_dependency,
            {first_package: two_leaf_versions},
        ),
        ProviderScenario[_PackageType, VersionType](
            "constraint",
            {first_package: any_version},
            {
                first_package: requires_any_second,
                second_package: two_leaf_versions,
            },
            constraints=second_dependency,
        ),
        ProviderScenario[_PackageType, VersionType](
            "unused constraint",
            first_dependency,
            {
                first_package: leaf_versions,
                second_package: leaf_versions,
            },
            constraints={second_package: range_type.empty()},
        ),
        ProviderScenario[_PackageType, VersionType](
            "equal dependencies",
            {first_package: any_version},
            {
                first_package: equal_dependencies,
                second_package: leaf_versions,
            },
        ),
    )


def check_provider_scenarios(
    factory: Callable[
        [ProviderScenario[_PackageType, VersionType]],
        ResolverProvider[_PackageType, VersionType],
    ],
    *,
    packages: Sequence[_PackageType],
    versions: Sequence[VersionType],
    range_type: type[RangeProtocol[VersionType]],
    probe_state: _ProbeState[_PackageType, VersionType] | None = None,
) -> None:
    """Run reusable resolution scenarios using the caller's key and range types.

    Supply three distinct package keys and two distinct version keys.
    The range class must preserve empty, full and singleton membership for them.
    factory receives each scenario and returns a fresh provider for it.
    Checks include conflicts, backtracking, cycles, constraints, repeated solves,
    metadata stability and any widening offered by the provider.
    """
    # Keep caller-owned key lists stable while factories run.
    package_keys = tuple(packages)
    version_keys = tuple(versions)
    if (
        len(package_keys) != _SCENARIO_PACKAGE_COUNT
        or len(version_keys) != _SCENARIO_VERSION_COUNT
        or len(set(package_keys)) != len(package_keys)
        or len(set(version_keys)) != len(version_keys)
    ):
        message = "provide three distinct package keys and two distinct version keys"
        raise ValueError(message)

    _check_range_type(range_type, version_keys)

    for scenario in _scenarios(package_keys, version_keys, range_type):
        # The root package is a separate sentinel; any accepted version key is valid.
        budget = prod(max(1, len(choices)) for choices in scenario.graph.values())
        try:
            check_provider(
                factory,
                scenario,
                range_type=range_type,
                root_version=version_keys[0],
                max_combinations=budget,
                probe_state=probe_state,
            )
        except AssertionError as error:
            # Preserve the original exception and traceback on Python 3.10, too.
            error.args = (f"{scenario.name}: {error}",)
            raise
