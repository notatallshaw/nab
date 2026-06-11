"""Property tests for the PubGrub resolver's correctness invariants.

This file walks `solver.md`_'s "Overview" and "The Algorithm"
sections paragraph by paragraph and adds a property test for each
invariant the algorithm is required to maintain.  Where the spec
states a property as natural language, the test class docstring
quotes it verbatim.  Some classes here state invariants from the
broader Boolean-satisfiability literature that the spec relies on
without restating; those classes drop the ``Quote`` prefix to make
the distinction visible.

.. _solver.md: https://github.com/dart-lang/pub/blob/master/doc/solver.md
"""

# Spec quotations preserve set-theoretic operators verbatim; long
# lines are reproduced as written.

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nab_resolver.ranges import Range
from nab_resolver.resolver import ResolutionError, Resolver, ResolverObserver

from .providers import (
    FuzzProvider,
    OldestFirstProvider,
    PromotingFuzzProvider,
    brute_force_has_solution,
    reachable_packages,
    verify_solution,
)
from .strategies import (
    BRUTE_FORCE_SETTINGS,
    PROPERTY_SETTINGS,
    deep_chain_graphs,
    dependency_graphs,
    dependency_ranges,
    empty_dep_graphs,
    graph_and_constraints,
    guaranteed_solvable_graphs,
    mutual_back_edge_graphs,
    mutual_dep_exhaustive_graphs,
    pinning_cascade_graphs,
    single_version_conflict_graphs,
    small_exhaustive_graphs,
    wide_fan_in_graphs,
)

if TYPE_CHECKING:
    from nab_resolver.types import Incompatibility

pytestmark = pytest.mark.property

# Per-test timeout: individual resolutions must finish in a few seconds.
RESOLUTION_TIMEOUT_SECONDS = 5


class _MonotonicityObserver(ResolverObserver):
    """Observer that records incompatibility-set sizes after each learn step."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self.sizes: list[int] = []

    def on_learned(self, incompatibility: Incompatibility) -> None:
        del incompatibility
        self.sizes.append(len(self._resolver.incompatibilities))


class _DenseLevelsObserver(ResolverObserver[str, int]):
    """Observer that tracks the current decision level across backjumps.

    Records ``(package, version, level)`` per ``on_decision`` and the
    most recent post-backjump level so the property test can assert
    each new decision is exactly one above the prior depth.

    The :class:`FuzzProvider` keys its graph on ``str`` package names
    and ``int`` versions, so the observer specialises the generic
    parent on those concrete types.  The virtual ROOT package never
    flows through ``on_decision`` (the resolver short-circuits
    ROOT before notifying observers), so the narrower ``str`` typing
    is safe.
    """

    def __init__(self) -> None:
        self.decisions: list[tuple[str, int, int]] = []
        self.violations: list[str] = []
        # The level just before each on_decision: starts at 1 (the
        # virtual ROOT decision), updates on backjump.
        self._current_level: int = 1

    def on_decision(self, package: str, version: int, level: int) -> None:
        expected = self._current_level + 1
        if level != expected:
            self.violations.append(
                f"on_decision saw level {level}, expected {expected}"
            )
        self.decisions.append((package, version, level))
        self._current_level = level

    def on_backjump(self, from_level: int, to_level: int) -> None:
        del from_level
        self._current_level = to_level


class _InvariantObserver(ResolverObserver):
    """Observer that records violations of algorithmic invariants.

    Verifies backjumps go strictly downward, never below the root,
    and that every learned incompatibility has at least one term.
    """

    def __init__(self) -> None:
        self.decision_levels: list[int] = []
        self.backjumps: list[tuple[int, int]] = []
        self.conflicts: int = 0
        self.learned: int = 0
        self.violations: list[str] = []

    def on_decision(self, package: object, version: object, level: int) -> None:
        del package, version
        self.decision_levels.append(level)

    def on_backjump(self, from_level: int, to_level: int) -> None:
        self.backjumps.append((from_level, to_level))
        if to_level >= from_level:
            self.violations.append(f"Backjump went up: {from_level} -> {to_level}")
        if to_level < 1:
            self.violations.append(f"Backjump below root: {from_level} -> {to_level}")

    def on_conflict(self, incompatibility: Incompatibility) -> None:
        del incompatibility
        self.conflicts += 1

    def on_learned(self, incompatibility: Incompatibility) -> None:
        self.learned += 1
        if not incompatibility.terms:
            self.violations.append("Learned empty incompatibility")


class TestQuoteResolverNeverCrashes:
    """solver.md § Overview, paragraph 4:

    > "At a high level, Pubgrub works like many other search
    > algorithms. Its core loop involves speculatively choosing
    > package versions that match outstanding dependencies. Eventually
    > one of two things happens:
    >
    > * All dependencies are satisfied, in which case a solution has
    >   been found and Pubgrub has succeeded.
    >
    > * It finds a dependency that can't be satisfied, in which case
    >   the current set of versions are incompatible and the solver
    >   needs to backtrack."

    The implementation must reach one of these two outcomes on any
    well-typed input, never crash with an unexpected exception.

    Reference:
    https://github.com/dart-lang/pub/blob/master/doc/solver.md#overview
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs())
    @PROPERTY_SETTINGS
    def test_never_crashes(self, graph: dict) -> None:
        """``Resolver.resolve`` returns a solution or raises ``ResolutionError``."""
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            pass

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs())
    @PROPERTY_SETTINGS
    def test_terminates(self, graph: dict) -> None:
        """Resolution stops within ``max_iterations``."""
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            pass
        assert resolver.stats.decisions <= 1001


class TestQuoteSolutionsAreValid:
    """solver.md § Overview, paragraph 2:

    > "Given a universe of package versions with constrained
    > dependencies on one another, one of which is designated as the
    > root, version solving is the problem of finding a set of package
    > versions such that
    >
    > * each version's dependencies are satisfied;
    > * only one version of each package is selected; and
    > * no extra packages are selected, that is, all selected packages
    >   are transitively reachable from the root package."

    The resolver may not return a solution that fails any of these
    three conditions.

    Reference:
    https://github.com/dart-lang/pub/blob/master/doc/solver.md#overview
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs())
    @PROPERTY_SETTINGS
    def test_solutions_are_valid(self, graph: dict) -> None:
        """Every returned solution satisfies all root requirements and dependencies."""
        provider = FuzzProvider(graph)
        requirements = {"root": Range.singleton(1)}
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve(requirements)
        except ResolutionError:
            return
        verify_solution(solution, requirements, graph)

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs())
    @PROPERTY_SETTINGS
    def test_no_extra_packages(self, graph: dict) -> None:
        """All packages in the solution are reachable from root."""
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            return
        root_required = set(graph.get("root", {}).get(1, {}).keys())
        root_required.add("root")
        reachable = reachable_packages(solution, graph, root_required)
        for package in solution:
            assert package in reachable, (
                f"Package {package!r} in solution but not reachable from root"
            )


class TestRootClosedSubgraph:
    """The solution is closed under the dependency relation.

    For every selected ``(package, version)`` and every dependency
    name declared by that version in the input graph, the dependency
    name must also appear in the solution.  This is the closure
    property of the selected sub-graph: nothing depended on is left
    out.  It complements :class:`TestQuoteSolutionsAreValid`'s
    range-respect check by stating the inclusion side directly so a
    bug that drops a dependency name surfaces here even if its
    range happens to be satisfied vacuously.
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs())
    @PROPERTY_SETTINGS
    def test_solution_is_closed_under_dependencies(self, graph: dict) -> None:
        """Every dep of an in-solution package is itself in the solution."""
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            return
        for package, version in solution.items():
            dependencies = graph.get(package, {}).get(version, {})
            for dep_name in dependencies:
                assert dep_name in solution, (
                    f"{package!r}@{version} depends on {dep_name!r} but it is "
                    f"absent from the solution"
                )


class TestQuoteFailureCarriesProof:
    """solver.md § Derivation Graph:

    > "A derivation graph represents a proof that the terms in its
    > root incompatibility are in fact incompatible. Because all
    > derived incompatibilities track their causes, we can find a
    > derivation graph for any of them and thereby prove it. In
    > particular, when Pubgrub determines that no solution can be
    > found, it uses the derivation graph for the incompatibility
    > ``{root any}`` to explain to the user why no versions of the
    > root package can be selected and thus why version solving
    > failed."

    Failure must carry an :class:`Incompatibility` proving the cause.
    The only exception is iteration-limit timeouts, which the resolver
    surfaces as :class:`ResolutionError` without a witness incompat.

    Reference:
    https://github.com/dart-lang/pub/blob/master/doc/solver.md#derivation-graph
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs())
    @PROPERTY_SETTINGS
    def test_failures_have_proof(self, graph: dict) -> None:
        """Non-timeout failures carry ``error.incompatibility``."""
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError as error:
            if "exceeded" not in str(error):
                assert error.incompatibility is not None


class TestDeterministic:
    """The PubGrub spec does not state determinism explicitly, but
    every constructive step in the algorithm is a pure function of
    the partial solution and the input incompatibilities.  Given an
    identical (provider, requirements) pair, ``Resolver.resolve``
    must produce identical output every time.

    Determinism is required by the lockfile semantics in nab: a user
    re-running ``nab lock`` over the same inputs must get the same
    pins.
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs())
    @PROPERTY_SETTINGS
    def test_deterministic(self, graph: dict) -> None:
        """Two resolutions of the same graph produce equal results."""
        provider_a = FuzzProvider(graph)
        provider_b = FuzzProvider(graph)
        resolver_a = Resolver(provider_a, max_iterations=1000)
        resolver_b = Resolver(provider_b, max_iterations=1000)
        try:
            result_a = resolver_a.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            with pytest.raises(ResolutionError):
                resolver_b.resolve({"root": Range.singleton(1)})
            return
        result_b = resolver_b.resolve({"root": Range.singleton(1)})
        assert result_a == result_b


class TestQuoteIncompatibilityMonotonicity:
    """solver.md § Overview, paragraph 5:

    > "Recording the root causes of conflicts allows Pubgrub to
    > avoid retreading dead ends in the search space when the
    > context has changed. This makes the solver substantially more
    > efficient than a naïve search algorithm when there are
    > consistent causes for each conflict."

    Learned incompatibilities are never retracted.  Without this
    guarantee the resolver could re-derive the same conflict in a
    loop and never make progress.

    Reference:
    https://github.com/dart-lang/pub/blob/master/doc/solver.md#overview
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs())
    @PROPERTY_SETTINGS
    def test_incompatibilities_only_grow(self, graph: dict) -> None:
        """Each learn step preserves or grows the incompatibility-set size."""
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        observer = _MonotonicityObserver(resolver)
        resolver.observer = observer
        try:
            resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            pass

        for index in range(1, len(observer.sizes)):
            assert observer.sizes[index] >= observer.sizes[index - 1], (
                f"Incompatibility count decreased: "
                f"{observer.sizes[index - 1]} -> {observer.sizes[index]}"
            )


def _assert_backjumps_strictly_downward(
    graph: dict,
    provider: FuzzProvider | PromotingFuzzProvider,
) -> None:
    """Resolve ``graph`` with ``provider`` and assert every backjump is downward."""
    observer = _InvariantObserver()
    resolver = Resolver(provider, observer=observer, max_iterations=1000)
    try:
        resolver.resolve({"root": Range.singleton(1)})
    except ResolutionError:
        pass
    assert not observer.violations, (
        f"Invariant violations: {observer.violations}\nGraph: {graph}"
    )
    for from_level, to_level in observer.backjumps:
        assert to_level < from_level


class TestQuoteBackjumpsGoDown:
    """solver.md § Conflict Resolution, the backtracking step:

    > "Backtrack by removing all assignments whose decision level is
    > greater than ``previousSatisfierLevel``."

    Strict downward motion means ``previousSatisfierLevel <
    currentLevel`` for every backjump; backjumping in place or
    upward indicates an infinite loop or a bug in the
    satisfier-search routine.  Backjumping below the root (level 1)
    would drop decisions the user made.

    Each test uses a different graph generator: random, mutual-dep,
    fan-in, and a promoting provider.  All four generator/provider
    combinations have historically exposed backjump bugs.

    Reference:
    https://github.com/dart-lang/pub/blob/master/doc/solver.md#conflict-resolution
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs())
    @PROPERTY_SETTINGS
    def test_random_graphs(self, graph: dict) -> None:
        """Random graphs preserve the backjump-strictly-downward invariant."""
        _assert_backjumps_strictly_downward(graph, FuzzProvider(graph))

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=mutual_back_edge_graphs())
    @PROPERTY_SETTINGS
    def test_mutual_back_edges(self, graph: dict) -> None:
        """Mutual-dep graphs preserve the backjump-strictly-downward invariant."""
        _assert_backjumps_strictly_downward(graph, FuzzProvider(graph))

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=wide_fan_in_graphs())
    @PROPERTY_SETTINGS
    def test_wide_fan_in(self, graph: dict) -> None:
        """Wide-fan-in graphs preserve the backjump-strictly-downward invariant."""
        _assert_backjumps_strictly_downward(graph, FuzzProvider(graph))

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(
        graph=dependency_graphs(),
        threshold=st.integers(min_value=1, max_value=20),
    )
    @PROPERTY_SETTINGS
    def test_promoting_provider(self, graph: dict, threshold: int) -> None:
        """Promoting providers preserve the backjump-strictly-downward invariant."""
        _assert_backjumps_strictly_downward(
            graph, PromotingFuzzProvider(graph, threshold)
        )


class TestDecisionLevelMonotonicity:
    """Decisions form a dense ascending sequence per restart cycle.

    The partial solution's ``decision_level`` rises by exactly one
    on every regular decision and falls only via :meth:`on_backjump`.
    Consequently, between two consecutive ``on_decision`` callbacks
    the observed level must be one more than the level was just
    before the new decision: either ``previous_level + 1`` (no
    backjump in between) or ``backjump_target + 1`` (one or more
    backjumps preceded the new decision).

    Restart events (:func:`conflict.maybe_restart`) reset the partial
    solution silently from the observer's perspective; ``on_decision``
    and ``on_backjump`` are the only public hooks today.  The test
    therefore skips runs where ``stats.restarts > 0``: on those, the
    sequence-level invariant cannot be checked without a restart hook.

    A gap on a non-restart run would mean the partial solution skipped
    a level, which breaks the backjump-target arithmetic used by the
    conflict-resolution loop.  A repeat would mean two decisions share
    a level, which breaks the per-level satisfier-search step.
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs())
    @PROPERTY_SETTINGS
    def test_decision_levels_are_dense_and_ascending(self, graph: dict) -> None:
        """Every recorded decision is exactly one level above the prior depth."""
        observer = _DenseLevelsObserver()
        resolver = Resolver(FuzzProvider(graph), observer=observer, max_iterations=1000)
        try:
            resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            pass
        if resolver.stats.restarts:
            return
        assert not observer.violations, (
            f"Decision-level invariant violations: {observer.violations}\n"
            f"Graph: {graph}"
        )


class TestAdversarialGraphShapes:
    """Specific graph shapes known to stress PubGrub-style resolvers.

    These do not derive a *new* invariant from the spec; they exercise
    the same correctness invariants over inputs the literature has
    flagged as historically problematic.
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=deep_chain_graphs())
    @PROPERTY_SETTINGS
    def test_deep_chains(self, graph: dict) -> None:
        """Deep linear chains resolve or fail cleanly without stack overflow."""
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            return
        verify_solution(solution, {"root": Range.singleton(1)}, graph)

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=pinning_cascade_graphs())
    @PROPERTY_SETTINGS
    def test_pinning_cascades(self, graph: dict) -> None:
        """Version-pinning cascades (the boto3 pattern) resolve correctly."""
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            return
        verify_solution(solution, {"root": Range.singleton(1)}, graph)

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=guaranteed_solvable_graphs())
    @PROPERTY_SETTINGS
    def test_guaranteed_solvable_succeeds(self, graph: dict) -> None:
        """Graphs where every package has a no-dep version always resolve.

        Completeness check: never report impossible when a solution
        provably exists.
        """
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        solution = resolver.resolve({"root": Range.singleton(1)})
        verify_solution(solution, {"root": Range.singleton(1)}, graph)

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=mutual_back_edge_graphs())
    @PROPERTY_SETTINGS
    def test_mutual_deps_solutions_valid(self, graph: dict) -> None:
        """Mutual-dependency graphs produce valid solutions when they resolve."""
        provider = FuzzProvider(graph)
        requirements = {"root": Range.singleton(1)}
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve(requirements)
        except ResolutionError:
            return
        verify_solution(solution, requirements, graph)

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=single_version_conflict_graphs())
    @PROPERTY_SETTINGS
    def test_single_version_conflicts(self, graph: dict) -> None:
        """Single-version-conflict graphs resolve or fail without rederivation loops."""
        provider = FuzzProvider(graph)
        requirements = {"root": Range.singleton(1)}
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve(requirements)
        except ResolutionError:
            return
        verify_solution(solution, requirements, graph)

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=wide_fan_in_graphs())
    @PROPERTY_SETTINGS
    def test_wide_fan_in(self, graph: dict) -> None:
        """Wide-fan-in graphs (many parents constrain a bottleneck) resolve correctly."""
        provider = FuzzProvider(graph)
        requirements = {"root": Range.singleton(1)}
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve(requirements)
        except ResolutionError:
            return
        verify_solution(solution, requirements, graph)

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=empty_dep_graphs())
    @PROPERTY_SETTINGS
    def test_empty_dep_ranges(self, graph: dict) -> None:
        """Empty-range dependencies cause version rejection, never invalid output."""
        provider = FuzzProvider(graph)
        requirements = {"root": Range.singleton(1)}
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve(requirements)
        except ResolutionError:
            return
        verify_solution(solution, requirements, graph)
        root_required = set(graph.get("root", {}).get(1, {}).keys())
        root_required.add("root")
        reachable = reachable_packages(solution, graph, root_required)
        for package in solution:
            assert package in reachable


class TestAgreesWithBruteForce:
    """PubGrub is described as a complete search algorithm in
    `solver.md`_: it returns a solution iff one exists, and only
    returns valid solutions.

    For tiny graphs we can verify completeness directly by enumerating
    every (package, version) tuple.  Disagreement between the
    resolver and the exhaustive search is a soundness or completeness
    bug.

    .. _solver.md: https://github.com/dart-lang/pub/blob/master/doc/solver.md
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(
        graph=small_exhaustive_graphs(),
        threshold=st.integers(min_value=1, max_value=10),
    )
    @BRUTE_FORCE_SETTINGS
    def test_agrees_with_brute_force(self, graph: dict, threshold: int) -> None:
        """Resolver agrees with exhaustive enumeration for small graphs."""
        requirements = {"root": Range.singleton(1)}
        brute_force_sat = brute_force_has_solution(graph, requirements)
        if brute_force_sat is None:
            return

        for provider in (FuzzProvider(graph), PromotingFuzzProvider(graph, threshold)):
            resolver = Resolver(provider, max_iterations=1000)
            try:
                solution = resolver.resolve(requirements)
            except ResolutionError as error:
                if "exceeded" in str(error):
                    continue
                assert not brute_force_sat, (
                    f"Resolver reported impossible but brute-force found a "
                    f"solution.\nGraph: {graph}"
                )
                continue

            assert brute_force_sat, (
                f"Resolver found a solution but brute-force says impossible.\n"
                f"Graph: {graph}\nSolution: {solution}"
            )
            verify_solution(solution, requirements, graph)

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(case=graph_and_constraints())
    @example(
        (
            {
                "root": {1: {"foo": Range.at_most(7)}},
                "foo": {7: {"bar": Range.at_least(1)}, 4: {}},
                "bar": {8: {"foo": Range.less_than(2)}},
            },
            {"bar": Range.singleton(8)},
        )
    )
    @BRUTE_FORCE_SETTINGS
    def test_agrees_with_brute_force_under_constraints(
        self, case: tuple[dict, dict]
    ) -> None:
        """Resolver agrees with constrained brute force for small graphs.

        Constraints restrict but never force a package, so a constraint
        on a package that a solution never selects is vacuous. Reporting
        impossible because of such a constraint is a completeness bug.
        """
        graph, constraints = case
        requirements = {"root": Range.singleton(1)}
        brute_force_sat = brute_force_has_solution(graph, requirements, constraints)
        if brute_force_sat is None:
            return

        for provider in (FuzzProvider(graph), PromotingFuzzProvider(graph, 1)):
            resolver = Resolver(provider, max_iterations=1000)
            try:
                solution = resolver.resolve(requirements, constraints=constraints)
            except ResolutionError as error:
                if "exceeded" in str(error):
                    continue
                assert not brute_force_sat, (
                    f"Resolver reported impossible but brute-force found a "
                    f"solution.\nGraph: {graph}\nConstraints: {constraints}"
                )
                continue

            assert brute_force_sat, (
                f"Resolver found a solution but brute-force says impossible.\n"
                f"Graph: {graph}\nConstraints: {constraints}\nSolution: {solution}"
            )
            verify_solution(solution, requirements, graph)
            for package, constraint_range in constraints.items():
                if package in solution:
                    assert solution[package] in constraint_range

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(
        graph=mutual_dep_exhaustive_graphs(),
        threshold=st.integers(min_value=1, max_value=10),
    )
    @BRUTE_FORCE_SETTINGS
    def test_mutual_deps_agree_with_brute_force(
        self, graph: dict, threshold: int
    ) -> None:
        """Mutual-dep graphs agree with exhaustive enumeration."""
        requirements = {"root": Range.singleton(1)}
        brute_force_sat = brute_force_has_solution(graph, requirements)
        if brute_force_sat is None:
            return

        for provider in (FuzzProvider(graph), PromotingFuzzProvider(graph, threshold)):
            resolver = Resolver(provider, max_iterations=1000)
            try:
                solution = resolver.resolve(requirements)
            except ResolutionError as error:
                if "exceeded" in str(error):
                    continue
                assert not brute_force_sat, (
                    f"Resolver reported impossible but brute-force found a "
                    f"solution.\nGraph: {graph}"
                )
                continue

            assert brute_force_sat, (
                f"Resolver found a solution but brute-force says impossible.\n"
                f"Graph: {graph}\nSolution: {solution}"
            )
            verify_solution(solution, requirements, graph)


class TestOrderInsensitivity:
    """A graph's *solvability* must not depend on the order the
    provider explores candidates in.

    Adapted from `pubgrub-rs`_'s proptest suite: if the
    newest-first provider finds a solution then the oldest-first
    provider must also find one (potentially different); if newest
    says impossible then oldest must too.

    .. _pubgrub-rs: https://github.com/pubgrub-rs/pubgrub
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 50)
    @given(graph=dependency_graphs())
    @BRUTE_FORCE_SETTINGS
    def test_version_order_independence(self, graph: dict) -> None:
        """Newest-first and oldest-first agree on solvability."""
        requirements = {"root": Range.singleton(1)}

        newest_resolver = Resolver(FuzzProvider(graph), max_iterations=1000)
        oldest_resolver = Resolver(OldestFirstProvider(graph), max_iterations=1000)

        try:
            newest_resolver.resolve(requirements)
        except ResolutionError as error:
            if "exceeded" in str(error):
                return
            try:
                oldest_resolver.resolve(requirements)
            except ResolutionError:
                return
            pytest.fail(
                f"Newest-first says impossible but oldest-first found a "
                f"solution.\nGraph: {graph}"
            )

        try:
            oldest_resolver.resolve(requirements)
        except ResolutionError as error:
            if "exceeded" in str(error):
                return
            pytest.fail(
                f"Newest-first found a solution but oldest-first "
                f"says impossible.\nGraph: {graph}"
            )


class TestIndependenceOfIrrelevantAlternatives:
    """Adapted from `pubgrub-rs`_'s proptest suite:

    a. If resolution succeeds, removing any version that was *not*
       selected must still succeed.
    b. Removing a single dependency edge cannot break a successful
       resolution.

    A failure here indicates the resolver depends on a candidate it
    didn't actually pick - a soundness leak.

    .. _pubgrub-rs: https://github.com/pubgrub-rs/pubgrub
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 50)
    @given(graph=dependency_graphs())
    @BRUTE_FORCE_SETTINGS
    def test_removing_dep_cant_break(self, graph: dict) -> None:
        """Removing any single dep edge cannot break a working resolution."""
        requirements = {"root": Range.singleton(1)}
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            resolver.resolve(requirements)
        except ResolutionError:
            return

        for package, versions in graph.items():
            for version, deps in versions.items():
                for dep_to_remove in deps:
                    reduced = {
                        p: {
                            v: {
                                d: r
                                for d, r in ds.items()
                                if not (
                                    p == package and v == version and d == dep_to_remove
                                )
                            }
                            for v, ds in vs.items()
                        }
                        for p, vs in graph.items()
                    }
                    reduced_provider = FuzzProvider(reduced)
                    reduced_resolver = Resolver(reduced_provider, max_iterations=1000)
                    try:
                        solution = reduced_resolver.resolve(requirements)
                    except ResolutionError as error:
                        if "exceeded" in str(error):
                            continue
                        pytest.fail(
                            f"Removing {dep_to_remove!r} from "
                            f"{package!r}@{version} broke resolution.\n"
                            f"Graph: {graph}"
                        )
                    verify_solution(solution, requirements, reduced)

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 50)
    @given(graph=dependency_graphs())
    @example(
        graph={
            "root": {1: {"pkg0": Range.full()}},
            "pkg0": {1: {}, 2: {"pkg1": Range.full()}},
            "pkg1": {1: {"pkg2": Range.full()}},
            "pkg2": {
                1: {
                    "pkg0": Range.full(),
                    "pkg1": Range.full(),
                    "pkg3": Range.full(),
                }
            },
            "pkg3": {
                1: {"pkg0": Range.singleton(1)},
                2: {"pkg0": Range.full(), "pkg1": Range.singleton(2)},
            },
        }
    )
    @BRUTE_FORCE_SETTINGS
    def test_removing_unselected_version_cant_break(self, graph: dict) -> None:
        """Removing an unselected version cannot break a working resolution.

        The explicit example pins the case where dropping ``pkg3``@1
        once made ``union_terms`` collapse two positive terms into a
        full-range union and discard it as a tautology, deriving an
        unsound clause and a false unsat.
        """
        requirements = {"root": Range.singleton(1)}
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)

        try:
            solution = resolver.resolve(requirements)
        except ResolutionError as error:
            if "exceeded" in str(error):
                return
            return

        for package, versions in graph.items():
            if package == "root":
                continue
            for version in versions:
                if solution.get(package) == version:
                    continue
                reduced = {
                    p: {
                        v: ds
                        for v, ds in vs.items()
                        if not (p == package and v == version)
                    }
                    for p, vs in graph.items()
                }
                if not reduced.get(package):
                    reduced.pop(package, None)
                reduced_provider = FuzzProvider(reduced)
                reduced_resolver = Resolver(reduced_provider, max_iterations=1000)
                try:
                    reduced_solution = reduced_resolver.resolve(requirements)
                except ResolutionError as error:
                    if "exceeded" in str(error):
                        continue
                    pytest.fail(
                        f"Removing unselected {package!r}@{version} broke "
                        f"resolution.\nOriginal solution: {solution}\n"
                        f"Graph: {graph}"
                    )
                verify_solution(reduced_solution, requirements, reduced)


class TestConstraintsRespected:
    """User-supplied constraints must be respected when applicable.

    A constraint differs from a requirement: it restricts the allowed
    range of a package without forcing it to be installed.  The
    resolver must (a) honor the range when the package is in the
    solution, and (b) not pull packages in just because they have a
    constraint.  This is the same semantics as `pip's --constraint
    flag`_.

    .. _pip's --constraint flag:
       https://pip.pypa.io/en/stable/user_guide/#constraints-files
    """

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs(), data=st.data())
    @PROPERTY_SETTINGS
    def test_solutions_satisfy_constraints(
        self, graph: dict, data: st.DataObject
    ) -> None:
        """Constrained packages in the solution honor the constraint range."""
        constraints = self._draw_constraints(data, graph)
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve(
                {"root": Range.singleton(1)}, constraints=constraints
            )
        except ResolutionError:
            return

        for package, constraint_range in constraints.items():
            if package in solution:
                assert solution[package] in constraint_range, (
                    f"Package {package!r}@{solution[package]} "
                    f"violates constraint {constraint_range}"
                )

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs(), data=st.data())
    @PROPERTY_SETTINGS
    def test_constraints_dont_add_packages(
        self, graph: dict, data: st.DataObject
    ) -> None:
        """Constraints alone do not pull packages into the solution."""
        constraints = self._draw_constraints(data, graph)
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            solution = resolver.resolve(
                {"root": Range.singleton(1)}, constraints=constraints
            )
        except ResolutionError:
            return

        root_required = set(graph.get("root", {}).get(1, {}).keys())
        root_required.add("root")
        reachable = reachable_packages(solution, graph, root_required)
        for package in solution:
            assert package in reachable, (
                f"Package {package!r} in solution but not reachable from root"
            )

    @pytest.mark.timeout(RESOLUTION_TIMEOUT_SECONDS * 200)
    @given(graph=dependency_graphs(), data=st.data())
    @PROPERTY_SETTINGS
    def test_constraint_failure_has_provenance(
        self, graph: dict, data: st.DataObject
    ) -> None:
        """An empty-range constraint on a required package fails with proof."""
        root_deps = graph.get("root", {}).get(1, {})
        if not root_deps:
            return
        target = data.draw(st.sampled_from(sorted(root_deps.keys())))
        constraints = {target: Range.empty()}
        provider = FuzzProvider(graph)
        resolver = Resolver(provider, max_iterations=1000)
        try:
            resolver.resolve({"root": Range.singleton(1)}, constraints=constraints)
        except ResolutionError as error:
            assert error.incompatibility is not None

    @staticmethod
    def _draw_constraints(
        data: st.DataObject,
        graph: dict[str, dict[int, dict[str, Range[int]]]],
    ) -> dict[str, Range[int]]:
        """Draw a small set of random constraints for packages in ``graph``."""
        packages = [p for p in graph if p != "root"]
        if not packages:
            return {}
        num_constraints = data.draw(
            st.integers(min_value=0, max_value=min(3, len(packages)))
        )
        if num_constraints == 0:
            return {}
        constrained = data.draw(
            st.lists(
                st.sampled_from(packages),
                min_size=num_constraints,
                max_size=num_constraints,
                unique=True,
            )
        )
        return {pkg: data.draw(dependency_ranges()) for pkg in constrained}
