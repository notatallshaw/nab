"""Test providers used by the resolver property tests.

These are minimal :class:`AbstractProvider` implementations that look
up answers in a pre-built dependency-graph dict.  They appear here so
the property-test modules stay focused on invariants and so the
provider implementations can be shared across files.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping

from nab_resolver.ranges import Range
from nab_resolver.types import Incompatibility, RangeProtocol


class FuzzProvider:
    """Provider backed by a generated dependency graph.

    Picks the newest matching version, prioritises by candidate
    count, and never queues pending clauses.
    """

    def __init__(self, graph: dict[str, dict[int, dict[str, Range[int]]]]) -> None:
        """Create a provider from a dependency graph dict."""
        self._graph = graph

    def _get_versions(self, package: str) -> list[int]:
        """Return versions sorted newest-first."""
        if package not in self._graph:
            return []
        return sorted(self._graph[package].keys(), reverse=True)

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        """Pick the newest version within the allowed range."""
        for version in self._get_versions(package):
            if version in version_range:
                return version
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        """Report whether any available version falls in the range."""
        return any(v in version_range for v in self._get_versions(package))

    def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
        """Return dependencies for a specific version."""
        return self._graph.get(package, {}).get(version, {})

    def begin_decision_scan(self) -> Callable[[str], bool] | None:
        """Offer no probe: nothing in this stub moves under a scan."""
        return None

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Prioritize by number of matching versions."""
        del conflict_counts, culprit_counts
        versions = self._get_versions(package)
        return sum(1 for version in versions if version in version_range)

    def is_ready(self, package: str) -> bool:
        """All packages decidable immediately in tests."""
        del package
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        """No-op: test provider does not use partial solution state."""
        del positive_ranges, decisions

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """No queued clauses."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal from this test provider."""
        return []

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """No widening: dependency clauses keep the exact decided version."""
        del package, version
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        """Identity: constraints render as stored."""
        del package
        return constraint


class PromotingFuzzProvider:
    """Provider that promotes packages above a conflict threshold.

    The conflict threshold is drawn by Hypothesis, so the property
    tests sweep aggressive promotion (1), moderate (3-5), and
    effectively-disabled (large threshold) settings.
    """

    def __init__(
        self,
        graph: dict[str, dict[int, dict[str, Range[int]]]],
        conflict_threshold: int,
    ) -> None:
        """Create a provider with a specific conflict promotion threshold."""
        self._graph = graph
        self._conflict_threshold = conflict_threshold

    def _get_versions(self, package: str) -> list[int]:
        """Return versions sorted newest-first."""
        if package not in self._graph:
            return []
        return sorted(self._graph[package].keys(), reverse=True)

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        """Pick the newest version within the allowed range."""
        for version in self._get_versions(package):
            if version in version_range:
                return version
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        """Report whether any available version falls in the range."""
        return any(v in version_range for v in self._get_versions(package))

    def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
        """Return dependencies for a specific version."""
        return self._graph.get(package, {}).get(version, {})

    def begin_decision_scan(self) -> None:
        """No-op: nothing in this stub moves under a scan."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[int, int]:
        """Promote packages above the conflict threshold."""
        del culprit_counts
        promoted = (
            0 if conflict_counts.get(package, 0) >= self._conflict_threshold else 1
        )
        versions = self._get_versions(package)
        count = sum(1 for version in versions if version in version_range)
        return (promoted, count)

    def is_ready(self, package: str) -> bool:
        """All packages decidable immediately in tests."""
        del package
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        """No-op: test provider does not use partial solution state."""
        del positive_ranges, decisions

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """No queued clauses."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal from this test provider."""
        return []

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """No widening: dependency clauses keep the exact decided version."""
        del package, version
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        """Identity: constraints render as stored."""
        del package
        return constraint


class OldestFirstProvider:
    """Provider that picks the oldest version instead of newest.

    Used for the version-order independence property: a graph that
    resolves with newest-first must also resolve with oldest-first
    (and vice versa for impossible).
    """

    def __init__(self, graph: dict[str, dict[int, dict[str, Range[int]]]]) -> None:
        """Create a provider from a dependency graph dict."""
        self._graph = graph

    def _get_versions(self, package: str) -> list[int]:
        """Return versions sorted oldest-first."""
        if package not in self._graph:
            return []
        return sorted(self._graph[package].keys())

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        """Pick the oldest version within the allowed range."""
        for version in self._get_versions(package):
            if version in version_range:
                return version
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        """Report whether any available version falls in the range."""
        return any(v in version_range for v in self._get_versions(package))

    def get_dependencies(self, package: str, version: int) -> dict[str, Range[int]]:
        """Return dependencies for a specific version."""
        return self._graph.get(package, {}).get(version, {})

    def begin_decision_scan(self) -> None:
        """No-op: nothing in this stub moves under a scan."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Prioritize by number of matching versions."""
        del conflict_counts, culprit_counts
        versions = self._get_versions(package)
        return sum(1 for version in versions if version in version_range)

    def is_ready(self, package: str) -> bool:
        """All packages decidable immediately in tests."""
        del package
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        """No-op: test provider does not use partial solution state."""
        del positive_ranges, decisions

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """No queued clauses."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal from this test provider."""
        return []

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """No widening: dependency clauses keep the exact decided version."""
        del package, version
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        """Identity: constraints render as stored."""
        del package
        return constraint


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
        dependencies = graph.get(package, {}).get(version, {})
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
        package = queue.pop(0)
        if package in reachable:
            continue
        reachable.add(package)
        version = solution.get(package)
        if version is None:
            continue
        dependencies = graph.get(package, {}).get(version, {})
        queue.extend(dep for dep in dependencies if dep not in reachable)
    return reachable


MAX_BRUTE_FORCE_COMBINATIONS = 10_000


def brute_force_has_solution(
    graph: dict[str, dict[int, dict[str, Range[int]]]],
    requirements: dict[str, Range[int]],
    constraints: dict[str, Range[int]] | None = None,
) -> bool | None:
    """Check whether any version selection satisfies all constraints.

    Tries every (package, version) combination over the reachable
    sub-graph.  Returns ``True``/``False``, or ``None`` when the
    search space exceeds :data:`MAX_BRUTE_FORCE_COMBINATIONS`.

    A user constraint restricts the version of a package only when that
    package is reachable from the root: a constrained package that is
    never pulled in leaves its constraint vacuous, mirroring the
    resolver's own constraint semantics.
    """
    constraints = constraints or {}
    all_packages = sorted(p for p in graph if p != "root" and graph[p])

    total_combinations = 1
    for package in all_packages:
        total_combinations *= len(graph[package])
        if total_combinations > MAX_BRUTE_FORCE_COMBINATIONS:
            return None

    candidate_lists = [sorted(graph[p].keys()) for p in all_packages]

    for combo in itertools.product(*candidate_lists):
        selection = dict(zip(all_packages, combo, strict=False))
        selection["root"] = 1

        valid = True
        for package, required_range in requirements.items():
            if package not in selection or selection[package] not in required_range:
                valid = False
                break
        if not valid:
            continue

        reachable: set[str] = set()
        queue = list(requirements.keys())
        while queue:
            pkg = queue.pop()
            if pkg in reachable:
                continue
            reachable.add(pkg)
            version = selection.get(pkg)
            if version is None:
                valid = False
                break
            deps = graph.get(pkg, {}).get(version, {})
            for dep_pkg, dep_range in deps.items():
                if dep_pkg not in selection or selection[dep_pkg] not in dep_range:
                    valid = False
                    break
                if dep_pkg not in reachable:
                    queue.append(dep_pkg)
            if not valid:
                break

        if valid and any(
            pkg in reachable and selection[pkg] not in crange
            for pkg, crange in constraints.items()
        ):
            valid = False

        if valid:
            return True

    return False
