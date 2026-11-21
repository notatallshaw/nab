"""Graph-backed test providers and independent solution checks."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from nab_resolver._compat import override
from nab_resolver.errors import ResolutionError, ResolutionTerminatedError
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider, Resolver
from nab_resolver.types import RangeProtocol


class _GraphProvider(BaseProvider[str, int]):
    """Read package versions and dependencies from a generated graph."""

    def __init__(self, graph: dict[str, dict[int, dict[str, Range[int]]]]) -> None:
        self._graph = graph

    def _matching_versions(
        self, package: str, version_range: RangeProtocol[int]
    ) -> Iterator[int]:
        return (v for v in self._graph.get(package, {}) if v in version_range)

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        return max(self._matching_versions(package, version_range), default=None)

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return next(self._matching_versions(package, version_range), None) is not None

    def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
        return self._graph.get(package, {}).get(version, {})

    def widen_decision(self, package: str, version: int) -> None:
        return None


class FuzzProvider(_GraphProvider):
    """Choose the newest version and prioritize by the matching candidate count."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        return sum(1 for _ in self._matching_versions(package, version_range))


class PromotingFuzzProvider(_GraphProvider):
    """Promote packages once their conflict count reaches the supplied threshold."""

    def __init__(
        self,
        graph: dict[str, dict[int, dict[str, Range[int]]]],
        conflict_threshold: int,
    ) -> None:
        super().__init__(graph)
        self._conflict_threshold = conflict_threshold

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[int, int]:
        tier = 0 if conflict_counts.get(package, 0) >= self._conflict_threshold else 1
        return tier, sum(1 for _ in self._matching_versions(package, version_range))


class OldestFirstProvider(FuzzProvider):
    """Use the same candidate-count priority while choosing the oldest match."""

    @override
    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        return min(self._matching_versions(package, version_range), default=None)


def verify_solution(
    solution: dict[str, int],
    requirements: dict[str, Range[int]],
    graph: dict[str, dict[int, dict[str, Range[int]]]],
) -> None:
    """Assert that a solution satisfies all requirements and dependencies.

    Used by every property test that calls the resolver: even when
    Hypothesis isn't checking a specific algebraic invariant the
    returned solution still has to be self-consistent.
    """
    for package, required_range in requirements.items():
        assert package in solution, f"Required package {package!r} not in solution"
        assert solution[package] in required_range, (
            f"Package {package!r} version {solution[package]} "
            f"not in required range {required_range}"
        )

    for package, version in solution.items():
        assert package in graph, (
            f"Selected package {package!r} is absent from the graph"
        )
        assert version in graph[package], f"Unknown version {version} of {package!r}"
        dependencies = graph[package][version]
        for dep_package, dep_range in dependencies.items():
            assert dep_package in solution, (
                f"Dependency {dep_package!r} of {package!r}@{version} not in solution"
            )
            assert solution[dep_package] in dep_range, (
                f"Dependency {dep_package!r}@{solution[dep_package]} "
                f"of {package!r}@{version} not in range {dep_range}"
            )


def reachable_packages(
    solution: dict[str, int],
    graph: dict[str, dict[int, dict[str, Range[int]]]],
    root_required: set[str],
) -> set[str]:
    """Return packages transitively reachable from the root requirements."""
    reachable: set[str] = set()
    queue = list(root_required)
    while queue:
        package = queue.pop()
        if package in reachable:
            continue
        reachable.add(package)
        version = solution.get(package)
        if version is None:
            continue
        dependencies = graph.get(package, {}).get(version, {})
        queue.extend(dep for dep in dependencies if dep not in reachable)
    return reachable


def is_proven_conflict(error: ResolutionError) -> bool:
    """A solver fault can carry a diagnostic incompatibility without proving a conflict."""
    return (
        not isinstance(error, ResolutionTerminatedError)
        and error.incompatibility is not None
    )


def resolve_or_prove_conflict(
    resolver: Resolver[str, int],
    requirements: dict[str, Range[int]],
    constraints: dict[str, Range[int]] | None = None,
) -> dict[str, int] | None:
    """Return pins or a proven conflict. Limits and solver faults fail the test."""
    try:
        return resolver.resolve(requirements, constraints)
    except ResolutionError as error:
        if not is_proven_conflict(error):
            raise
        return None


def brute_force_has_solution(
    graph: dict[str, dict[int, dict[str, Range[int]]]],
    requirements: dict[str, Range[int]],
    constraints: dict[str, Range[int]] | None = None,
) -> bool:
    """Try every published version of each package reached by the requirements."""
    constraints = constraints or {}

    def visit(pins: dict[str, int], pending: list[tuple[str, Range[int]]]) -> bool:
        if not pending:
            return True

        package, allowed = pending[-1]
        rest = pending[:-1]
        if package in pins:
            return pins[package] in allowed and visit(pins, rest)

        constraint = constraints.get(package)
        for version, dependencies in graph.get(package, {}).items():
            if version not in allowed or (
                constraint is not None and version not in constraint
            ):
                continue
            # Each branch owns its choices. Revisiting a package only checks its pin.
            if visit({**pins, package: version}, [*rest, *dependencies.items()]):
                return True
        return False

    return visit({}, list(requirements.items()))
