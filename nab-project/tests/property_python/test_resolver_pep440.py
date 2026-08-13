"""Property tests for the resolver with PEP 440 version primitives.

This file mirrors :mod:`tests.property.test_pubgrub_resolver` but
uses ``packaging.version.Version`` and
``packaging.specifiers.SpecifierSet`` instead of the toy
``Range[int]`` provider.  It catches bugs in the ``VersionRange``
adapter or the ``PackagingProvider`` that wouldn't surface against
the toy provider.

Reference: `PEP 440`_, `PEP 503`_, `PubGrub solver.md`_.

.. _PEP 440: https://peps.python.org/pep-0440/
.. _PEP 503: https://peps.python.org/pep-0503/
.. _PubGrub solver.md: https://github.com/dart-lang/pub/blob/master/doc/solver.md
"""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given

from nab_project._packaging_provider import PackagingProvider
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_resolver.errors import ResolutionError
from nab_resolver.resolver import Resolver

from .strategies import (
    BRUTE_FORCE_SETTINGS,
    MAX_BRUTE_FORCE_COMBINATIONS,
    PROPERTY_SETTINGS,
    packaging_graphs,
    small_packaging_graphs,
)

pytestmark = pytest.mark.property

RESOLUTION_TIMEOUT_SECONDS = 5


def _verify_solution(
    solution: dict[str, Version],
    requirements: dict[str, VersionRange],
    graph: dict[str, dict[Version, dict[str, SpecifierSet]]],
) -> None:
    """Assert that ``solution`` satisfies ``requirements`` and graph deps."""
    for package, required_range in requirements.items():
        assert package in solution, f"Required package {package!r} not in solution"
        assert solution[package] in required_range, (
            f"{package!r} version {solution[package]} not in range {required_range}"
        )

    for package, version in solution.items():
        deps = graph.get(package, {}).get(version, {})
        for dep_package, dep_spec in deps.items():
            assert dep_package in solution, (
                f"Dependency {dep_package!r} of {package!r}@{version} not in solution"
            )
            assert solution[dep_package] in dep_spec, (
                f"{dep_package!r}@{solution[dep_package]} "
                f"of {package!r}@{version} not in {dep_spec}"
            )


def _brute_force_has_solution(
    graph: dict[str, dict[Version, dict[str, SpecifierSet]]],
    requirements: dict[str, VersionRange],
) -> bool | None:
    """Return ``True``/``False`` for solvability, or ``None`` if too large."""
    all_packages = sorted(
        (p for p in graph if p != "root" and graph[p]),
        key=str,
    )

    total = 1
    for package in all_packages:
        total *= len(graph[package])
        if total > MAX_BRUTE_FORCE_COMBINATIONS:
            return None

    candidate_lists = [sorted(graph[p].keys()) for p in all_packages]

    for combo in itertools.product(*candidate_lists):
        selection = dict(zip(all_packages, combo, strict=False))
        selection["root"] = Version("1.0")

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
            ver = selection.get(pkg)
            if ver is None:
                valid = False
                break
            deps = graph.get(pkg, {}).get(ver, {})
            for dep_pkg, dep_spec in deps.items():
                if dep_pkg not in selection or selection[dep_pkg] not in dep_spec:
                    valid = False
                    break
                if dep_pkg not in reachable:
                    queue.append(dep_pkg)
            if not valid:
                break

        if valid:
            return True

    return False


class TestSolutionsAreValidPEP440:
    """Returned solutions must satisfy every constraint in the input
    graph (the same invariant tested in
    :class:`tests.property.test_pubgrub_resolver.TestQuoteSolutionsAreValid`).

    This file's PEP 440 incarnation of the property catches bugs in
    the ``packaging.specifiers`` to ``VersionRange`` lifting that
    the integer-version test wouldn't see.
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 100)
    @given(graph=packaging_graphs())
    @PROPERTY_SETTINGS
    def test_solutions_are_valid(self, graph: dict) -> None:
        """Every returned PEP 440 solution satisfies all constraints."""
        provider = PackagingProvider(graph)
        requirements = {"root": VersionRange.singleton(Version("1.0"))}
        resolver = Resolver(
            provider, range_type=VersionRange, root_version="0", max_iterations=1000
        )
        try:
            solution = resolver.resolve(requirements)
        except ResolutionError:
            return
        _verify_solution(solution, requirements, graph)


class TestAgreesWithBruteForcePEP440:
    """For tiny PEP 440 graphs the resolver agrees with exhaustive
    enumeration over the search space.

    A disagreement is a soundness or completeness bug; failures here
    are usually traceable to ``VersionRange`` boundary handling
    (PEP 440's ``!=`` and pre-release exclusion rules).
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 100)
    @given(graph=small_packaging_graphs())
    @BRUTE_FORCE_SETTINGS
    def test_agrees_with_brute_force(self, graph: dict) -> None:
        """Resolver agrees with brute-force enumeration on PEP 440 graphs."""
        requirements = {"root": VersionRange.singleton(Version("1.0"))}
        brute_force_sat = _brute_force_has_solution(graph, requirements)
        if brute_force_sat is None:
            return

        provider = PackagingProvider(graph)
        resolver = Resolver(
            provider, range_type=VersionRange, root_version="0", max_iterations=1000
        )
        try:
            solution = resolver.resolve(requirements)
        except ResolutionError as error:
            if "exceeded" in str(error):
                return
            assert not brute_force_sat, (
                f"Resolver reported impossible but brute-force found a "
                f"solution.\nGraph: {graph}"
            )
            return

        assert brute_force_sat, (
            f"Resolver found a solution but brute-force says impossible.\n"
            f"Graph: {graph}\nSolution: {solution}"
        )
        _verify_solution(solution, requirements, graph)
