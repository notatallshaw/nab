"""Tests for the PubGrub resolver: unit propagation, conflict resolution,
and end-to-end resolution with a simple in-memory provider."""

from __future__ import annotations

import sys
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

import pytest

from nab_resolver import propagate
from nab_resolver.conflict import (
    apply_targeted_backtrack,
    conflict_resolution,
    find_most_recent_satisfier,
    is_terminal_incompatibility,
    maybe_restart,
    recompute_previous_level,
    try_force_resolution_step,
    update_culprit_counts,
)
from nab_resolver.incompat_index import (
    add_incompatibility,
    dependency_merge_key,
    maybe_merge_dependency,
)
from nab_resolver.partial_solution import Assignment, PartialSolution
from nab_resolver.propagate import classify_relation, term_relation
from nab_resolver.ranges import Range
from nab_resolver.report import (
    explain_incompatibility,
    format_error,
    format_term,
    prior_cause,
    union_terms,
)
from nab_resolver.resolver import (
    DEFAULT_MAX_ITERATIONS,
    ResolutionError,
    Resolver,
    ResolverObserver,
    ResolverStats,
    Solution,
)
from nab_resolver.root import ROOT
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    IncompatibilityState,
    RangeProtocol,
    RootRequirement,
    SetRelation,
    Term,
)


class DictProvider:
    """In-memory provider for testing. Packages are strings, versions are ints."""

    def __init__(self, packages: dict[str, dict[int, dict[str, Range]]]) -> None:
        self._packages = packages

    def _get_versions(self, package: str) -> list[int]:
        if package not in self._packages:
            return []
        return sorted(self._packages[package].keys(), reverse=True)

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        for version in self._get_versions(package):
            if version in version_range:
                return version
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return any(v in version_range for v in self._get_versions(package))

    def get_dependencies(self, package: str, version: int) -> dict[str, Range]:
        return self._packages.get(package, {}).get(version, {})

    def begin_decision_scan(self) -> None:
        """No-op: nothing in this stub moves under a scan."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> object:
        versions = self._get_versions(package)
        return sum(1 for v in versions if v in version_range)

    def is_ready(self, package: str) -> bool:
        """All packages decidable immediately in tests."""
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        """No-op: test provider does not use partial solution state."""

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """No queued clauses for this in-memory provider."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal from this provider."""
        return []

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """No widening: dependency clauses keep the exact decided version."""
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        """Identity: constraints render as stored."""
        return constraint


class PromotingProvider(DictProvider):
    """DictProvider that promotes packages with 5+ conflicts.

    The threshold is the provider's own, not the resolver's restart
    threshold.
    """

    _CONFLICT_THRESHOLD = 5

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[bool, int]:
        promoted = conflict_counts.get(package, 0) >= self._CONFLICT_THRESHOLD
        versions = self._get_versions(package)
        matching = sum(1 for v in versions if v in version_range)
        return (not promoted, matching)


class _PendingClauseProvider(DictProvider):
    """DictProvider that pushes a pre-canned incompatibility on every
    ``choose_version`` call by populating a queue drained via
    ``consume_pending_clauses``."""

    def __init__(
        self,
        packages: dict[str, dict[int, dict[str, Range]]],
        clauses: list[Incompatibility[str, int]],
    ) -> None:
        super().__init__(packages)
        self._queued = list(clauses)
        self._fired = False

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """Return the queued clauses once, then nothing."""
        if self._fired:
            return []
        self._fired = True
        return self._queued


class _NoCandidateProvider(DictProvider):
    """Provider that returns no version for a chosen package and queues a
    binary incompatibility on that same call. Exercises the branch where
    ``choose_version`` returns None AND ``consume_pending_clauses`` is
    non-empty: the resolver must add the queued clause and skip its
    default ``NO_VERSIONS`` clause."""

    def __init__(
        self,
        packages: dict[str, dict[int, dict[str, Range]]],
        target: str,
        clauses: list[Incompatibility[str, int]],
    ) -> None:
        super().__init__(packages)
        self._target = target
        self._queued = list(clauses)
        self._refused_target = False

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        if package == self._target:
            self._refused_target = True
            return None
        return super().choose_version(package, version_range)

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        # Only fire on the call right after we returned None for our target.
        if not self._refused_target:
            return []
        self._refused_target = False
        out = self._queued
        self._queued = []
        return out


class TestPendingClausesHook:
    def test_provider_clauses_added_to_formula(self) -> None:
        """Clauses returned by consume_pending_clauses end up in the formula."""
        # Provider pushes ``{root==1, foo==2}`` impossible. Once the resolver
        # decides root==1 the clause unit-propagates ``not foo==2``, so foo
        # is decided at the next-best version (1).
        clause = Incompatibility(
            [
                Term[str, int]("root", Range.singleton(1), positive=True),
                Term[str, int]("foo", Range.singleton(2), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        provider = _PendingClauseProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {2: {}, 1: {}},
            },
            [clause],
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result == {"root": 1, "foo": 1}

    def test_pending_clauses_replace_no_versions(self) -> None:
        """When choose_version returns None but pending clauses are queued,
        the resolver skips the default NO_VERSIONS clause and uses the
        provider-supplied clause as the conflict source for backjumping."""
        # Clause: ``{root==1, foo==2}`` impossible. Provider refuses foo
        # and supplies the clause so the resolver has a recorded conflict
        # to backjump on instead of an over-broad NO_VERSIONS that would
        # forbid the entire ``foo`` range.
        clause = Incompatibility(
            [
                Term[str, int]("root", Range.singleton(1), positive=True),
                Term[str, int]("foo", Range.singleton(2), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        provider = _NoCandidateProvider(
            {
                "root": {1: {"foo": Range.singleton(2)}},
                "foo": {2: {}},
            },
            target="foo",
            clauses=[clause],
        )
        resolver = Resolver(provider)
        # Resolution fails because foo==2 can't be picked, but the failure
        # path goes through the queued clause rather than NO_VERSIONS.
        with pytest.raises(ResolutionError):
            resolver.resolve({"root": Range.singleton(1)})


class TestTrivialResolution:
    def test_no_dependencies(self) -> None:
        provider = DictProvider(
            {
                "root": {1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result == {"root": 1}

    def test_single_dependency(self) -> None:
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["root"] == 1
        assert result["foo"] == 3  # newest

    def test_transitive_dependency(self) -> None:
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {2: {"bar": Range.at_least(1)}, 1: {}},
                "bar": {1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["root"] == 1
        assert result["foo"] == 2
        assert result["bar"] == 1


class TestDiamondDependency:
    def test_diamond(self) -> None:
        """root -> foo, root -> bar, both depend on baz."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.full(), "bar": Range.full()}},
                "foo": {1: {"baz": Range.at_least(2)}},
                "bar": {1: {"baz": Range.less_than(4)}},
                "baz": {4: {}, 3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["baz"] in (2, 3)  # must satisfy both constraints


class TestBacktracking:
    def test_simple_backtrack(self) -> None:
        """foo@2 requires bar>=2, but only bar@1 exists.
        Resolver must backtrack to foo@1 which has no deps."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.full()}},
                "foo": {2: {"bar": Range.at_least(2)}, 1: {}},
                "bar": {1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["foo"] == 1  # backtracked from 2 to 1


class TestConflictLearning:
    def test_shared_conflict(self) -> None:
        """Many versions of A all require D>=2, E requires D<1.
        Impossible. Tests that the resolver terminates and reports error."""
        a_deps = {"D": Range.at_least(2)}
        provider = DictProvider(
            {
                "root": {1: {"A": Range.full(), "E": Range.full()}},
                "A": dict.fromkeys(range(50, 0, -1), a_deps),
                "D": {3: {}, 2: {}, 1: {}},
                "E": {1: {"D": Range.less_than(1)}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError):
            resolver.resolve({"root": Range.singleton(1)})

    def test_direct_conflict(self) -> None:
        """root requires both foo>=2 and foo<1. Immediately impossible."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(2)}},
                "foo": {3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError):
            resolver.resolve(
                {
                    "root": Range.singleton(1),
                    "foo": Range.less_than(1),
                }
            )


class TestMultiLevelConflict:
    def test_deep_backtracking(self) -> None:
        """A scenario requiring multiple levels of backtracking.

        root -> a, root -> c
        a@2 -> b >= 2
        b@2 -> c >= 3
        c only has versions 1, 2
        So a@2 -> b@2 -> c>=3 fails (no c>=3).
        Resolver should backtrack a to 1.
        """
        provider = DictProvider(
            {
                "root": {1: {"a": Range.full(), "c": Range.full()}},
                "a": {2: {"b": Range.at_least(2)}, 1: {}},
                "b": {2: {"c": Range.at_least(3)}, 1: {}},
                "c": {2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        # a@2 path fails because c >= 3 doesn't exist
        assert result["a"] == 1
        assert result["c"] in (1, 2)

    def test_conflict_with_multiple_parents(self) -> None:
        """Two packages both constrain a shared dependency conflictingly.

        root -> x, root -> y
        x@1 -> z >= 3
        y@1 -> z < 2
        z has versions 1, 2, 3
        Impossible: z can't be both >= 3 and < 2.
        """
        provider = DictProvider(
            {
                "root": {1: {"x": Range.full(), "y": Range.full()}},
                "x": {1: {"z": Range.at_least(3)}},
                "y": {1: {"z": Range.less_than(2)}},
                "z": {3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError):
            resolver.resolve({"root": Range.singleton(1)})


class OrderedProvider(DictProvider):
    """Provider with explicit priority ordering for testing."""

    def __init__(
        self,
        packages: dict[str, dict[int, dict[str, Range]]],
        order: dict[str, int],
    ) -> None:
        super().__init__(packages)
        self._order = order

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        return self._order.get(package, 99)


class TestMultiSatisfierConflict:
    def test_conflict_with_satisfiers_at_different_levels(self) -> None:
        """Conflict involving terms whose causes were decided at
        different depths in the decision trail.

        Graph (with forced decision order a, c, b):

            root -> a, b
            a@2 -> c >= 2           a@1 has no deps
            b@2 -> c == 1           b@1 has no deps
            c has versions 1, 2, 3

        The resolver decides a=2 (level 3), then c=3 (level 4),
        then b=2 (level 5).  b@2's constraint c==1 conflicts with
        the already-decided c=3.  During conflict resolution the
        algorithm must compare the trail positions of c's and b's
        contributions and jump back past the right one.
        """
        provider = OrderedProvider(
            {
                "root": {1: {"a": Range.full(), "b": Range.full()}},
                "a": {2: {"c": Range.at_least(2)}, 1: {}},
                "b": {2: {"c": Range.singleton(1)}, 1: {}},
                "c": {3: {}, 2: {}, 1: {}},
            },
            order={"a": 0, "c": 1, "b": 2},
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["root"] == 1
        assert "a" in result
        assert "b" in result


class TestThreeWayConflict:
    def test_three_packages_constrain_one(self) -> None:
        """Three packages all constrain z differently.

        root -> a, root -> b, root -> c
        a@1 -> z >= 3
        b@1 -> z <= 2
        c@1 -> z == 1
        z has 1, 2, 3
        b and a conflict (z can't be both >= 3 and <= 2).
        """
        provider = DictProvider(
            {
                "root": {
                    1: {
                        "a": Range.full(),
                        "b": Range.full(),
                        "c": Range.full(),
                    }
                },
                "a": {1: {"z": Range.at_least(3)}},
                "b": {1: {"z": Range.at_most(2)}},
                "c": {1: {"z": Range.singleton(1)}},
                "z": {3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError):
            resolver.resolve({"root": Range.singleton(1)})


class TestDictProvider:
    def test_unknown_package_returns_none(self) -> None:
        provider = DictProvider({"known": {1: {}}})
        assert provider.choose_version("unknown", Range.full()) is None
        assert provider.get_dependencies("unknown", 1) == {}


class TestCircularDependencies:
    def test_circular_with_impossible_version(self) -> None:
        """Circular dependency requiring a version that doesn't exist.

            root -> pkg0 -> pkg1 -> pkg0==2     (but pkg0 only has v1)

        Must report unsatisfiable. Found by Hypothesis fuzzer.
        """
        provider = DictProvider(
            {
                "root": {1: {"pkg0": Range.full()}},
                "pkg0": {1: {"pkg1": Range.full()}},
                "pkg1": {1: {"pkg0": Range.singleton(2)}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError):
            resolver.resolve({"root": Range.singleton(1)})


class TestSelfDependency:
    def test_self_dep_excluding_own_version_backtracks(self) -> None:
        """foo@2 depends on foo==1, contradicting itself; foo@1 is fine.

        The raw dependency clause would carry two terms for the same
        package, which the unit rule cannot fire on: the resolver
        re-decides foo@2 after every backjump and never terminates.
        The terms must be merged so foo@2 is rejected and foo@1 wins.
        """
        provider = DictProvider({"foo": {2: {"foo": Range.singleton(1)}, 1: {}}})
        resolver = Resolver(provider, max_iterations=100)
        result = resolver.resolve({"foo": Range.full()})
        assert result == {"foo": 1}

    def test_self_dep_excluding_own_version_unsat(self) -> None:
        """The only version foo@1 depends on foo==2, which doesn't exist.

        Must fail with an unsat proof, not by hitting max_iterations, and the
        proof must name the self-dependency edge the merged term cannot state.
        """
        provider = DictProvider({"foo": {1: {"foo": Range.singleton(2)}}})
        resolver = Resolver(provider, max_iterations=100)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve({"foo": Range.full()})
        assert str(exc_info.value).splitlines() == [
            "because no versions of foo (-inf, 1) | (1, +inf) are available",
            "because foo 1 depends on foo 2",
            "so all versions of foo",
            "because your project depends on foo",
            "so your project's requirements cannot be satisfied",
        ]

    def test_self_dep_containing_own_version_is_vacuous(self) -> None:
        """foo@1 depends on foo=={1}: satisfied by itself, resolves."""
        provider = DictProvider({"foo": {1: {"foo": Range.singleton(1)}}})
        result = Resolver(provider).resolve({"foo": Range.full()})
        assert result == {"foo": 1}

    def test_self_dep_empty_range_unsat(self) -> None:
        """foo@1 depends on foo in the empty range: unsatisfiable."""
        provider = DictProvider({"foo": {1: {"foo": Range.empty()}}})
        with pytest.raises(ResolutionError) as exc_info:
            Resolver(provider).resolve({"foo": Range.full()})
        assert "because foo 1 depends on foo <empty>" in str(exc_info.value)


class TestNoExtraPackages:
    def test_backtracked_dependencies_excluded(self) -> None:
        """Packages from a failed branch must not appear in result.

            root -> pkg1 (any)
            pkg1@2 -> pkg0, pkg2==2       pkg1@1 has no deps
            pkg2 has only v1              (no version satisfies ==2)

        The resolver backtracks from pkg1@2 to pkg1@1.  pkg0 and pkg2
        (only reachable through pkg1@2) must not be in the result.
        Found by Hypothesis fuzzer.
        """
        provider = DictProvider(
            {
                "root": {1: {"pkg1": Range.full()}},
                "pkg0": {1: {}},
                "pkg1": {1: {}, 2: {"pkg0": Range.full(), "pkg2": Range.singleton(2)}},
                "pkg2": {1: {}},
            }
        )
        result = Resolver(provider).resolve({"root": Range.singleton(1)})
        assert "pkg2" not in result
        assert result["pkg1"] == 1


LogEntry = tuple[str, Any, Any]


class CallLogProvider(DictProvider):
    """DictProvider that appends the questions it is asked to a shared log."""

    def __init__(
        self,
        packages: dict[str, dict[int, dict[str, Range]]],
        log: list[LogEntry],
    ) -> None:
        super().__init__(packages)
        self._log = log

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        chosen = super().choose_version(package, version_range)
        self._log.append(("choose_version", package, chosen))
        return chosen

    def get_dependencies(self, package: str, version: int) -> dict[str, Range]:
        self._log.append(("get_dependencies", package, version))
        return super().get_dependencies(package, version)


class DecisionLogObserver(ResolverObserver[str, int]):
    """Writes decisions into the provider's log so the two interleave."""

    def __init__(self, log: list[LogEntry]) -> None:
        self._log = log

    def on_decision(self, package: str, version: int, level: int) -> None:
        self._log.append(("decide", package, version))


def record_resolve(
    packages: dict[str, dict[int, dict[str, Range]]],
    requirements: dict[str, Range],
) -> tuple[list[LogEntry], Solution[str, int]]:
    """Resolve, logging every version choice, decision and dependency question."""
    log: list[LogEntry] = []
    provider = CallLogProvider(packages, log)
    solution = Resolver(provider, observer=DecisionLogObserver(log)).solve(requirements)
    return log, solution


class TestGetDependenciesCallingContract:
    """``get_dependencies`` is asked right after each decision, then once per pin."""

    def test_a_conflict_free_resolve_asks_once_per_decision_then_once_per_pin(
        self,
    ) -> None:
        log, solution = record_resolve(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {2: {"bar": Range.at_least(1)}, 1: {}},
                "bar": {2: {}, 1: {}},
            },
            {"root": Range.singleton(1)},
        )

        assert log == [
            ("choose_version", "root", 1),
            ("decide", "root", 1),
            ("get_dependencies", "root", 1),
            ("choose_version", "foo", 2),
            ("decide", "foo", 2),
            ("get_dependencies", "foo", 2),
            ("choose_version", "bar", 2),
            ("decide", "bar", 2),
            ("get_dependencies", "bar", 2),
            ("get_dependencies", "root", 1),
            ("get_dependencies", "foo", 2),
            ("get_dependencies", "bar", 2),
        ]
        assert solution.pins == {"root": 1, "foo": 2, "bar": 2}

    def test_a_backjump_asks_again_for_a_pair_already_answered(self) -> None:
        """foo 2 is decided, undone, and foo 1 decided in its place.

        ``bar`` offers no version, so it is never asked for dependencies.
        """
        log, solution = record_resolve(
            {
                "root": {1: {"foo": Range.full()}},
                "foo": {2: {"bar": Range.at_least(5)}, 1: {}},
                "bar": {1: {}},
            },
            {"root": Range.singleton(1)},
        )

        assert log == [
            ("choose_version", "root", 1),
            ("decide", "root", 1),
            ("get_dependencies", "root", 1),
            ("choose_version", "foo", 2),
            ("decide", "foo", 2),
            ("get_dependencies", "foo", 2),
            ("choose_version", "bar", None),
            ("choose_version", "root", 1),
            ("decide", "root", 1),
            ("get_dependencies", "root", 1),
            ("choose_version", "foo", 1),
            ("decide", "foo", 1),
            ("get_dependencies", "foo", 1),
            ("get_dependencies", "root", 1),
            ("get_dependencies", "foo", 1),
        ]
        assert solution.pins == {"root": 1, "foo": 1}


class TestPreference:
    def test_prefers_newest(self) -> None:
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {5: {}, 4: {}, 3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["foo"] == 5

    def test_prefers_newest_within_constraint(self) -> None:
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.between(2, 5)}},
                "foo": {5: {}, 4: {}, 3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["foo"] == 4  # 5 excluded by <5


class TestMaxIterations:
    def test_default_is_public(self) -> None:
        resolver = Resolver(DictProvider({}))

        assert DEFAULT_MAX_ITERATIONS == 200_000
        assert resolver.max_iterations == DEFAULT_MAX_ITERATIONS

    def test_exceeds_max_iterations(self) -> None:
        """Resolver raises when max_iterations is exceeded."""
        provider = DictProvider(
            {
                "root": {1: {"a": Range.full()}},
                "a": {v: {"b": Range.singleton(v)} for v in range(20, 0, -1)},
                "b": {v: {} for v in range(20, 0, -1)},
            }
        )
        resolver = Resolver(provider, max_iterations=3)
        with pytest.raises(ResolutionError, match="exceeded"):
            resolver.resolve({"root": Range.singleton(1)})


class ConflictStepObserver(ResolverObserver[str, int]):
    """Records how many conflict-resolution steps each conflict took."""

    def __init__(self) -> None:
        self.steps_per_conflict: list[int] = []

    def on_conflict(self, incompatibility: Incompatibility[str, int]) -> None:
        self.steps_per_conflict.append(0)

    def on_conflict_step(
        self,
        incompatibility: Incompatibility[str, int],
        *,
        satisfier_package: str,
        satisfier_is_decision: bool,
        satisfier_level: int,
        previous_level: int,
        can_backjump: bool,
    ) -> None:
        self.steps_per_conflict[-1] += 1


def stalled_solution(
    padding: int,
) -> tuple[PartialSolution[str, int], Incompatibility[str, int]]:
    """Build a trail on which one clause resolves with itself forever.

    ``app``'s only assignment is a derivation whose cause is the clause under
    resolution and whose accumulated range is empty.  An empty range satisfies
    both polarities, so the clause is satisfied by the derivation it caused:
    the loop resolves the clause with itself, :func:`prior_cause` returns the
    same term every time, and no trail depth is consumed.  ``padding``
    lengthens the trail without disturbing that fixed point.
    """
    solution: PartialSolution[str, int] = PartialSolution()
    solution.decide("root", 1)
    seed: Incompatibility[str, int] = Incompatibility(
        [Term("root", Range.singleton(1))], cause=IncompatibilityCause.ROOT
    )
    for index in range(padding):
        solution.derive(f"pad{index}", Range.at_least(1), positive=True, cause=seed)

    stalled: Incompatibility[str, int] = Incompatibility(
        [Term("app", Range.singleton(3))], cause=IncompatibilityCause.DERIVED
    )
    solution.derive("app", Range.empty(), positive=True, cause=stalled)
    return solution, stalled


# A regressed guard would hang the stall tests instead of failing them.
STALL_TIMEOUT_SECONDS = 60


class TestConflictProgressGuard:
    @pytest.mark.timeout(STALL_TIMEOUT_SECONDS)
    def test_self_resolving_clause_raises_instead_of_spinning(self) -> None:
        """A clause that resolves with itself hits the step budget."""
        solution, stalled = stalled_solution(padding=0)
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution = solution
        assert solution.trail_length == 2

        with pytest.raises(ResolutionError) as excinfo:
            conflict_resolution(resolver, stalled)

        message = str(excinfo.value)
        assert "no progress in 32 steps" in message
        assert "resolver bug" in message
        assert "app" in message
        assert excinfo.value.incompatibility is not None

    @pytest.mark.timeout(STALL_TIMEOUT_SECONDS)
    def test_step_budget_scales_with_trail_depth(self) -> None:
        """A deeper trail buys proportionally more steps before the raise."""
        solution, stalled = stalled_solution(padding=30)
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution = solution
        assert solution.trail_length == 32

        with pytest.raises(ResolutionError, match="no progress in 128 steps"):
            conflict_resolution(resolver, stalled)

    def test_backtracking_resolve_stays_far_inside_the_budget(self) -> None:
        """A conflict-heavy resolve never approaches the step budget."""
        provider = DictProvider(
            {
                "root": {1: {"a": Range.full(), "b": Range.full()}},
                "a": {v: {"c": Range.singleton(v)} for v in range(20, 0, -1)},
                "b": {v: {"c": Range.less_than(3)} for v in range(20, 0, -1)},
                "c": {v: {} for v in range(20, 0, -1)},
            }
        )
        observer = ConflictStepObserver()
        resolver = Resolver(provider, observer=observer)

        result = resolver.resolve({"root": Range.singleton(1)})

        assert result["c"] < 3
        assert resolver.stats.conflicts > 0
        assert max(observer.steps_per_conflict) < 32


class EventTrackingObserver(ResolverObserver):
    """Observer that records all events to a list for test assertions."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def on_decision(self, package: str, version: int, level: int) -> None:
        self.events.append(f"decide:{package}")

    def on_derivation(
        self,
        package: str,
        *,
        positive: bool,
        cause: Incompatibility[str, int],
    ) -> None:
        self.events.append(f"derive:{package}")

    def on_conflict(self, incompatibility: Incompatibility[str, int]) -> None:
        self.events.append("conflict")

    def on_backjump(self, from_level: int, to_level: int) -> None:
        self.events.append(f"backjump:{from_level}->{to_level}")

    def on_no_versions(self, package: str, version_range: RangeProtocol[int]) -> None:
        self.events.append(f"no_versions:{package}")


class TestObserver:
    def test_observer_receives_all_events(self) -> None:
        """Observer receives decision, derivation, conflict, backjump,
        and no_versions events during resolution with backtracking."""
        # Scenario with backtracking: foo@2 requires bar>=2, only bar@1
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.full()}},
                "foo": {2: {"bar": Range.at_least(2)}, 1: {}},
                "bar": {1: {}},
            }
        )
        observer = EventTrackingObserver()
        resolver = Resolver(provider, observer=observer)
        resolver.resolve({"root": Range.singleton(1)})
        assert "decide:root" in observer.events
        assert "conflict" in observer.events
        assert "no_versions:bar" in observer.events
        assert any(e.startswith("derive:") for e in observer.events)
        assert any(e.startswith("backjump:") for e in observer.events)


class TestResolverInternals:
    """Unit tests for internal helper functions."""

    def testunion_terms_both_positive(self) -> None:
        a = Term("foo", Range.between(1, 3), positive=True)
        b = Term("foo", Range.between(5, 7), positive=True)
        result = union_terms(a, b)
        assert result is not None
        assert result.is_positive()
        assert 2 in result.constraint
        assert 6 in result.constraint

    def testunion_terms_both_negative_universal(self) -> None:
        """not({2}) union not({3}) = not({2} & {3}) = not(empty) = universal."""
        a = Term("foo", Range.singleton(2), positive=False)
        b = Term("foo", Range.singleton(3), positive=False)
        result = union_terms(a, b)
        assert result is None  # universal, every version is not-2 or not-3

    def testunion_terms_both_negative_non_universal(self) -> None:
        """not([1,3)) union not([2,5)) = not([2,3)) (the intersection)."""
        a = Term("foo", Range.between(1, 3), positive=False)
        b = Term("foo", Range.between(2, 5), positive=False)
        result = union_terms(a, b)
        assert result is not None
        assert not result.is_positive()
        # [1,3) & [2,5) = [2,3), so the result is not([2,3))
        assert 2 in result.constraint
        assert 1 not in result.constraint

    def testunion_terms_positive_full_range_is_kept(self) -> None:
        """A full-range positive union is not a tautology.

        It still requires the package to be selected, so dropping it
        from a resolvent would make the clause fire for solutions that
        omit the package entirely.
        """
        a = Term("foo", Range.full(), positive=True)
        b = Term("foo", Range.singleton(1), positive=True)
        result = union_terms(a, b)
        assert result is not None
        assert result.is_positive()
        assert (~result.constraint).is_empty

    def testunion_terms_negative_empty_intersection_returns_none(self) -> None:
        """Negative union where intersection is empty is universal."""
        a = Term("foo", Range.less_than(3), positive=False)
        b = Term("foo", Range.at_least(3), positive=False)
        assert union_terms(a, b) is None

    def testunion_terms_mixed_polarity(self) -> None:
        """Positive union negative produces a negative remainder."""
        a = Term("foo", Range.at_least(3), positive=True)
        b = Term("foo", Range.at_least(2), positive=False)
        result = union_terms(a, b)
        # pos([3,+inf)) union neg([2,+inf)) = all versions >= 3 OR not >= 2
        # = all versions >= 3 OR < 2 = everything except [2,3)
        # As negative: not([2,3))
        assert result is not None
        assert (
            1 in ~result.constraint
            if result.is_positive()
            else 1 not in result.constraint
        )

    def testprior_cause_basic(self) -> None:
        """Resolve two incompatibilities sharing package bar."""
        inc1 = Incompatibility(
            [Term("foo", Range.singleton(2)), Term("bar", Range.singleton(1))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        inc2 = Incompatibility(
            [Term("bar", Range.singleton(1)), Term("baz", Range.singleton(3))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        result = prior_cause(inc1, inc2, "bar")
        packages = {t.package for t in result}
        assert "foo" in packages
        assert "baz" in packages

    def testprior_cause_shared_package_union_drops_tautology(self) -> None:
        """When the shared package's terms union to a tautology, it's dropped.

        Mixed polarity over the same range is the classic case: the
        union ``bar >= 2 or not bar >= 2`` holds for every solution.
        Two positive terms never qualify (their union still requires
        the package), so only this form reduces.
        """
        inc1 = Incompatibility(
            [Term("foo", Range.singleton(1)), Term("bar", Range.at_least(2))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        inc2 = Incompatibility(
            [
                Term("bar", Range.at_least(2), positive=False),
                Term("baz", Range.singleton(3)),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        result = prior_cause(inc1, inc2, "bar")
        packages = {t.package for t in result}
        # The bar terms union to a tautology, so bar is dropped.
        assert "bar" not in packages
        assert "foo" in packages
        assert "baz" in packages

    def testprior_cause_shared_package_kept_when_not_universal(self) -> None:
        """Shared package kept when its union is not universal."""
        inc1 = Incompatibility(
            [Term("foo", Range.singleton(1)), Term("shared", Range.singleton(2))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        inc2 = Incompatibility(
            [Term("shared", Range.singleton(3)), Term("baz", Range.singleton(4))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        result = prior_cause(inc1, inc2, "shared")
        packages = {t.package for t in result}
        # shared's union is {2}|{3} which is not universal, so kept
        assert "shared" in packages
        assert "foo" in packages
        assert "baz" in packages

    def testprior_cause_other_packages_intersected(self) -> None:
        """Terms for the same non-shared package get intersected."""
        inc1 = Incompatibility(
            [Term("shared", Range.singleton(1)), Term("foo", Range.at_least(2))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        inc2 = Incompatibility(
            [Term("shared", Range.singleton(1)), Term("foo", Range.less_than(5))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        result = prior_cause(inc1, inc2, "shared")
        foo_terms = [t for t in result if t.package == "foo"]
        assert len(foo_terms) == 1
        assert 3 in foo_terms[0].constraint
        assert 1 not in foo_terms[0].constraint
        assert 5 not in foo_terms[0].constraint

    def test_conflict_resolution_multi_satisfier(self) -> None:
        """Conflict resolution with two terms decided at different levels.

        Manually constructs a trail with x=2 (level 1) and y=3 (level 2),
        then triggers conflict resolution on {x==2, y==3}.  The resolver
        should jump back to level 1 (before the y decision).
        """
        solution = PartialSolution()
        inc_root = Incompatibility(
            [Term("x", Range.full())], cause=IncompatibilityCause.ROOT
        )
        solution.derive("x", Range.at_least(1), positive=True, cause=inc_root)
        solution.decide("x", 2)
        solution.derive("y", Range.at_least(1), positive=True, cause=inc_root)
        solution.decide("y", 3)

        conflict = Incompatibility(
            [
                Term("x", Range.singleton(2)),
                Term("y", Range.singleton(3)),
            ],
            cause=IncompatibilityCause.DERIVED,
        )

        resolver = Resolver(DictProvider({}))
        resolver.solution = solution

        conflict_resolution(resolver, conflict)
        assert resolver.solution.decision_level == 1

    def test_explain_incompatibility(self) -> None:
        """Test the error explanation for a derived incompatibility."""
        t1 = Term("foo", Range.singleton(2))
        t2 = Term("bar", Range.singleton(1))
        inc_root = Incompatibility([t1], cause=IncompatibilityCause.ROOT)
        inc_dep = Incompatibility([t2], cause=IncompatibilityCause.DEPENDENCY)
        inc_derived = Incompatibility(
            [t1, t2],
            cause=IncompatibilityCause.DERIVED,
            cause_left=inc_root,
            cause_right=inc_dep,
        )
        lines: list[str] = []
        explain_incompatibility(inc_derived, lines, set())
        assert len(lines) == 3  # root, dep, derived
        assert any("root" in line for line in lines)

    def test_explain_with_none_causes(self) -> None:
        """Derived incompatibility with None causes should not crash."""
        derived = Incompatibility(
            [Term("a", Range.singleton(1))],
            cause=IncompatibilityCause.DERIVED,
            cause_left=None,
            cause_right=None,
        )
        lines: list[str] = []
        explain_incompatibility(derived, lines, set())
        assert len(lines) == 1

    def test_explain_shared_cause_deduplication(self) -> None:
        """When two derived incompatibilities share a common cause, the
        explanation should only include it once (visited_ids check)."""
        shared = Incompatibility(
            [Term("x", Range.singleton(1))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        left = Incompatibility(
            [Term("a", Range.singleton(1))],
            cause=IncompatibilityCause.DERIVED,
            cause_left=shared,
            cause_right=None,
        )
        right = Incompatibility(
            [Term("b", Range.singleton(1))],
            cause=IncompatibilityCause.DERIVED,
            cause_left=shared,
            cause_right=None,
        )
        top = Incompatibility(
            [Term("a", Range.singleton(1)), Term("b", Range.singleton(1))],
            cause=IncompatibilityCause.DERIVED,
            cause_left=left,
            cause_right=right,
        )
        lines: list[str] = []
        explain_incompatibility(top, lines, set())
        # shared should appear only once despite being in two branches
        x_mentions = [line for line in lines if "x" in line]
        assert len(x_mentions) == 1

    def testprior_cause_shared_only_in_incompatibility(self) -> None:
        """Shared package only appears in the first incompatibility."""
        inc1 = Incompatibility(
            [Term("shared", Range.singleton(1)), Term("foo", Range.singleton(2))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        inc2 = Incompatibility(
            [Term("baz", Range.singleton(3))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        result = prior_cause(inc1, inc2, "shared")
        packages = {t.package for t in result}
        assert "shared" in packages
        assert "foo" in packages
        assert "baz" in packages

    def testprior_cause_shared_only_in_cause(self) -> None:
        """Shared package only appears in the second incompatibility."""
        inc1 = Incompatibility(
            [Term("foo", Range.singleton(2))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        inc2 = Incompatibility(
            [Term("shared", Range.singleton(1)), Term("baz", Range.singleton(3))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        result = prior_cause(inc1, inc2, "shared")
        packages = {t.package for t in result}
        assert "shared" in packages
        assert "foo" in packages
        assert "baz" in packages

    def testprior_cause_non_shared_only_in_cause(self) -> None:
        """Non-shared package appears only in the cause side."""
        inc1 = Incompatibility(
            [Term("shared", Range.singleton(1))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        inc2 = Incompatibility(
            [
                Term("shared", Range.singleton(2)),
                Term("only_cause", Range.singleton(3)),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        result = prior_cause(inc1, inc2, "shared")
        packages = {t.package for t in result}
        assert "only_cause" in packages

    def testprior_cause_other_package_intersection_empty(self) -> None:
        """Non-shared terms that intersect to empty are dropped."""
        inc1 = Incompatibility(
            [Term("shared", Range.singleton(1)), Term("foo", Range.at_least(5))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        inc2 = Incompatibility(
            [Term("shared", Range.singleton(2)), Term("foo", Range.less_than(3))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        result = prior_cause(inc1, inc2, "shared")
        # foo terms: at_least(5) intersect less_than(3) = empty.
        # But Term.intersect returns a Term with empty constraint,
        # which is still appended. Check: both are positive, so
        # intersection is at_least(5) & less_than(3) = empty range.
        foo_terms = [t for t in result if t.package == "foo"]
        # The intersected term has an empty range, which is kept
        # (it still represents a constraint, even if vacuous).
        assert len(foo_terms) == 1
        assert foo_terms[0].constraint.is_empty

    def testprior_cause_shared_in_neither(self) -> None:
        """Shared package appears in neither incompatibility."""
        inc1 = Incompatibility(
            [Term("foo", Range.singleton(1))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        inc2 = Incompatibility(
            [Term("bar", Range.singleton(2))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        result = prior_cause(inc1, inc2, "missing")
        packages = {t.package for t in result}
        assert "foo" in packages
        assert "bar" in packages
        assert "missing" not in packages

    def testunion_terms_mixed_returns_negative(self) -> None:
        """Mixed polarity union that produces a negative term."""
        # pos([3,5)) union neg([2,6)) = all versions in [3,5) OR not in [2,6)
        # = everything except [2,3) and [5,6)
        # As negative: not([2,3) | [5,6))
        a = Term("foo", Range.between(3, 5), positive=True)
        b = Term("foo", Range.between(2, 6), positive=False)
        result = union_terms(a, b)
        assert result is not None
        # The remainder is neg.constraint & ~pos.constraint = [2,6) & ~[3,5)
        # = [2,3) | [5,6), which is non-empty, so we get a negative term
        assert not result.is_positive()

    def test_empty_incompatibility_is_terminal(self) -> None:
        """An incompatibility with no terms is terminal (always false)."""
        empty = Incompatibility([], cause=IncompatibilityCause.DERIVED)
        assert is_terminal_incompatibility(empty)

    def test_backjump_target_clamped_for_decision_satisfier(self) -> None:
        """Single-term conflict where the satisfier is a decision at level 1.

        previous_level defaults to 1, satisfier.decision_level is 1,
        so 1 >= 1 triggers the clamp: backjump_target = 1 - 1 = 0.
        Target 0 means the conflict is at the root, so ResolutionError.
        """
        solution = PartialSolution()
        solution.decide("x", 1)  # level 1

        conflict = Incompatibility(
            [Term("x", Range.singleton(1))],
            cause=IncompatibilityCause.DERIVED,
        )
        resolver = Resolver(DictProvider({}))
        resolver.solution = solution
        with pytest.raises(ResolutionError):
            conflict_resolution(resolver, conflict)

    def test_terminal_clause_raises_before_the_satisfier_search(self) -> None:
        """A clause holding only ROOT terms ends conflict resolution.

        The raise sits at the top of the loop, ahead of any satisfier
        lookup, so an empty solution is enough to reach it.
        """
        conflict = Incompatibility(
            [Term(ROOT, Range.singleton(1))],
            cause=IncompatibilityCause.DERIVED,
        )
        resolver = Resolver(DictProvider({}))
        with pytest.raises(ResolutionError) as exc_info:
            conflict_resolution(resolver, conflict)
        assert exc_info.value.incompatibility is conflict

    def test_recompute_previous_level_no_cause(self) -> None:
        """recompute_previous_level returns unchanged when cause is None."""
        resolver = Resolver(DictProvider({}))
        satisfier = PartialSolution()
        satisfier.decide("x", 1)
        assignment = satisfier.assignments_for("x")[0]
        # Decisions have cause=None, so recompute should return as-is.
        term = Term("x", Range.singleton(1))
        result = recompute_previous_level(resolver, assignment, term, 5)
        assert result == 5

    def test_recompute_previous_level_no_matching_cause_term(self) -> None:
        """recompute_previous_level returns unchanged when cause has no
        term for the satisfier's package."""
        resolver = Resolver(DictProvider({}))
        # Create a derivation whose cause doesn't mention "x"
        cause = Incompatibility(
            [Term("other", Range.singleton(1))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution = PartialSolution()
        solution.derive("x", Range.at_least(1), positive=True, cause=cause)
        assignment = solution.assignments_for("x")[0]
        term = Term("x", Range.at_least(1))
        result = recompute_previous_level(resolver, assignment, term, 3)
        assert result == 3

    def test_recompute_previous_level_no_difference_satisfier(self) -> None:
        """If satisfier(difference) is None, previous_level is unchanged."""
        resolver = Resolver(DictProvider({}))
        cause = Incompatibility(
            [Term("x", Range.at_least(3), positive=False)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution = PartialSolution()
        solution.derive("x", Range.at_least(3), positive=True, cause=cause)

        # Force satisfier() to return None to exercise the defensive
        # branch in recompute_previous_level. In normal resolution this
        # branch is unreachable because the partial solution always
        # contains an assignment that excludes the difference.
        solution.satisfier = lambda _term: None  # type: ignore[method-assign]

        term = Term("x", Range.between(1, 5))
        assignment = solution.assignments_for("x")[-1]
        resolver.solution = solution
        result = recompute_previous_level(resolver, assignment, term, 7)
        assert result == 7

    def test_recompute_previous_level_partial_positive_satisfier(self) -> None:
        """The satisfier's own assertion overshoots the term.

        The trail must also exclude the overshoot, so the level of the
        earlier assignment that does is folded in.
        """
        resolver = Resolver(DictProvider({}))
        solution = PartialSolution()
        solution.decide("a", 1)  # level 1
        narrow_cause = Incompatibility(
            [Term("x", Range.between(1, 6), positive=False)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution.derive("x", Range.between(1, 6), positive=True, cause=narrow_cause)
        solution.decide("b", 1)  # level 2
        cause = Incompatibility(
            [Term("x", Range.between(3, 9), positive=False)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution.derive("x", Range.between(3, 9), positive=True, cause=cause)

        term = Term("x", Range.between(3, 7))
        assignment = solution.assignments_for("x")[-1]
        resolver.solution = solution
        # The satisfier's own [3,9) leaves [7,9) outside the term; the
        # level-1 derivation [1,6) is what excludes it.
        result = recompute_previous_level(resolver, assignment, term, 0)
        assert result == 1

    def test_recompute_previous_level_partial_negative_satisfier(self) -> None:
        """A negative satisfier needs the earlier positive range.

        Excluding [5,10) only satisfies x [1,5) together with the
        level-1 derivation [1,10), so that level is folded in.
        """
        resolver = Resolver(DictProvider({}))
        solution = PartialSolution()
        solution.decide("a", 1)  # level 1
        pos_cause = Incompatibility(
            [Term("x", Range.between(1, 10), positive=False)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution.derive("x", Range.between(1, 10), positive=True, cause=pos_cause)
        solution.decide("b", 1)  # level 2
        neg_cause = Incompatibility(
            [Term("x", Range.between(5, 10), positive=True)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution.derive("x", Range.between(5, 10), positive=False, cause=neg_cause)

        term = Term("x", Range.between(1, 5))
        assignment = solution.assignments_for("x")[-1]
        resolver.solution = solution
        result = recompute_previous_level(resolver, assignment, term, 0)
        assert result == 1

    def test_recompute_previous_level_sole_satisfier(self) -> None:
        """No refinement when the satisfier's own assertion covers the term."""
        resolver = Resolver(DictProvider({}))
        solution = PartialSolution()
        solution.decide("a", 1)  # level 1
        wide_cause = Incompatibility(
            [Term("x", Range.between(1, 9), positive=False)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution.derive("x", Range.between(1, 9), positive=True, cause=wide_cause)
        solution.decide("b", 1)  # level 2
        cause = Incompatibility(
            [Term("x", Range.between(3, 5), positive=False)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution.derive("x", Range.between(3, 5), positive=True, cause=cause)

        # Own assertion [3,5) is a subset of the term [2,6).
        term = Term("x", Range.between(2, 6))
        assignment = solution.assignments_for("x")[-1]
        resolver.solution = solution
        result = recompute_previous_level(resolver, assignment, term, 0)
        assert result == 0

    def test_previous_level_includes_partial_satisfier_contribution(self) -> None:
        """A partial satisfier raises previous_level past the other terms.

        x's satisfier asserts [3,9), which only partially covers the
        conflicting term x [3,7); the level-2 derivation [1,6) supplies
        the rest, so the previous satisfier level is 2, not r's level 1.
        """
        solution = PartialSolution()
        solution.decide("r", 1)  # level 1
        solution.decide("a", 1)  # level 2
        cause_a = Incompatibility(
            [
                Term("a", Range.singleton(1)),
                Term("x", Range.between(1, 6), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution.derive("x", Range.between(1, 6), positive=True, cause=cause_a)
        solution.decide("b", 1)  # level 3
        cause_b = Incompatibility(
            [
                Term("b", Range.singleton(1)),
                Term("x", Range.between(3, 9), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution.derive("x", Range.between(3, 9), positive=True, cause=cause_b)

        resolver = Resolver(DictProvider({}))
        resolver.solution = solution
        conflict = Incompatibility(
            [Term("r", Range.singleton(1)), Term("x", Range.between(3, 7))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        satisfier, term, previous_level = find_most_recent_satisfier(resolver, conflict)
        assert satisfier.decision_level == 3
        assert term.package == "x"
        assert previous_level == 2


class TestBruteForceRegressions:
    def test_backtrack_past_failed_branch_with_mutual_deps(self) -> None:
        """Conditional dependency with circular back-edge and dead end.

        Graph:
            root -> pkg0 (any)
            pkg0@3 -> pkg1 (any)          pkg0@1, pkg0@2 have no deps
            pkg1@1 -> pkg0 (any), pkg2==2
            pkg1@2 -> pkg0 (any), pkg2==2
            pkg2 has only v1              (no version satisfies ==2)

        Expected: backtrack pkg0 from v3 to v2 (which has no deps).

        This is a completeness test: the resolver must not falsely
        report "impossible" when a valid solution exists. The tricky
        part is the circular dependency (pkg1 -> pkg0) combined with
        a dead end (pkg2==2 doesn't exist). After the resolver learns
        that all versions of pkg1 are unusable, it must trace that
        failure back through the pkg0@3 -> pkg1 dependency edge and
        exclude pkg0@3, rather than concluding the whole problem is
        unsolvable.

        Found by brute-force comparison fuzzer.
        """
        provider = DictProvider(
            {
                "root": {1: {"pkg0": Range.full()}},
                "pkg0": {1: {}, 2: {}, 3: {"pkg1": Range.full()}},
                "pkg1": {
                    1: {"pkg0": Range.full(), "pkg2": Range.singleton(2)},
                    2: {"pkg0": Range.full(), "pkg2": Range.singleton(2)},
                },
                "pkg2": {1: {}},
            }
        )
        result = Resolver(provider).resolve({"root": Range.singleton(1)})
        assert result["pkg0"] in (1, 2)
        assert "pkg1" not in result
        assert "pkg2" not in result

    def test_positive_full_union_term_is_not_dropped(self) -> None:
        """Resolving two positive terms must keep a full-range union.

        Graph:
            root -> pkg0 (any)
            pkg0@1 has no deps          pkg0@2 -> pkg1 (any)
            pkg1@1 -> pkg2 (any)
            pkg2@1 -> pkg0 (any), pkg1 (any), pkg3 (any)
            pkg3@2 -> pkg0 (any), pkg1==2  (no pkg1@2 exists)

        Expected: {root: 1, pkg0: 1}; pkg2 and pkg3 stay unselected.

        Conflict resolution combines "no versions of pkg2 outside 1"
        with a clause containing "pkg2 == 1"; the union is the positive
        full range.  Dropping it as a tautology asserted "pkg3 must be
        2 in every solution" even for solutions without pkg2, deriving
        a false unsat.

        Found by the removing-unselected-version property test.
        """
        provider = DictProvider(
            {
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
                "pkg3": {2: {"pkg0": Range.full(), "pkg1": Range.singleton(2)}},
            }
        )
        result = Resolver(provider).resolve({"root": Range.singleton(1)})
        assert result == {"root": 1, "pkg0": 1}


class TestBackjumpToRoot:
    def test_single_package_no_versions(self) -> None:
        """Root requires foo but foo has no versions at all.

        The conflict is at the root level: the NO_VERSIONS
        incompatibility for foo has no escape. The resolver
        must detect this and raise ResolutionError.
        """
        provider = DictProvider({"root": {1: {"foo": Range.full()}}, "foo": {}})
        with pytest.raises(ResolutionError):
            Resolver(provider).resolve({"root": Range.singleton(1)})

    def test_decision_satisfier_backjump_adjustment(self) -> None:
        """Backjump target gets clamped to decision_level - 1.

            root -> a, b
            a@1 -> c >= 2
            b@1 -> c < 2
            c has versions 1, 2

        The conflict on c involves a (which required c >= 2) and
        b (which required c < 2), both at the same level as c's
        decision. The satisfier for one of c's terms is a decision,
        and the backjump target must be adjusted below it.
        """
        provider = DictProvider(
            {
                "root": {1: {"a": Range.full(), "b": Range.full()}},
                "a": {1: {"c": Range.at_least(2)}},
                "b": {1: {"c": Range.less_than(2)}},
                "c": {2: {}, 1: {}},
            }
        )
        with pytest.raises(ResolutionError):
            Resolver(provider).resolve({"root": Range.singleton(1)})

    def test_terminal_clause_reports_the_whole_derivation(self) -> None:
        """A real resolve reaches a clause holding only ROOT terms.

            root -> a, b
            a@1 -> c >= 3
            b@1 -> c <= 1
            c has versions 1, 2, 3

        Resolving backwards eliminates a, b and c, leaving a clause
        with only ROOT terms, and the error still renders the whole
        derivation that produced it.
        """
        provider = DictProvider(
            {
                "root": {1: {"a": Range.full(), "b": Range.full()}},
                "a": {1: {"c": Range.at_least(3)}},
                "b": {1: {"c": Range.at_most(1)}},
                "c": {3: {}, 2: {}, 1: {}},
            }
        )
        with pytest.raises(ResolutionError) as exc_info:
            Resolver(provider).resolve({"root": Range.singleton(1)})
        incompatibility = exc_info.value.incompatibility
        assert incompatibility is not None
        assert is_terminal_incompatibility(incompatibility)
        message = str(exc_info.value)
        assert "a 1 depends on c [3, +inf)" in message
        assert "b 1 depends on c (-inf, 1]" in message
        assert "your project depends on root 1" in message


class TestRestart:
    """Verify the resolver restarts when a package causes many conflicts."""

    def test_restart_fires_on_repeated_conflicts(self) -> None:
        """A package that keeps conflicting triggers a restart, and the
        resolve still lands on the only solution.

        root -> a (any), b (any)
        a has versions 10..1, each requiring b >= v (so a@10 -> b>=10, etc.)
        b only has version 1.
        Only a@1 is compatible (b>=1 satisfied by b@1).

        The resolver decides b first (fewer versions), then walks a down
        from 10, conflicting on each version. Ten versions are too few
        for a restart to pay for itself, so the decision count asserted
        below is a ceiling rather than a saving.
        """
        a_versions = {}
        for v in range(10, 0, -1):
            a_versions[v] = {"b": Range.at_least(v)}

        provider = PromotingProvider(
            {
                "root": {1: {"a": Range.full(), "b": Range.full()}},
                "a": a_versions,
                "b": {1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["a"] == 1
        assert result["b"] == 1
        assert resolver.stats.restarts >= 1
        assert resolver.stats.decisions < 30

    def test_restarts_are_bounded(self) -> None:
        """Resolver stops restarting after _MAX_RESTARTS."""
        # 70 versions of "a", each requiring b >= v. Only a@1 works.
        # "a" takes every conflict and the threshold doubles per restart,
        # so restarts fire at 8, 16 and 32. The corpus reaches the 64 a
        # fourth would need, so the spent budget is what stops it.
        a_versions = {}
        for v in range(70, 0, -1):
            a_versions[v] = {"b": Range.at_least(v)}

        provider = PromotingProvider(
            {
                "root": {1: {"a": Range.full(), "b": Range.full()}},
                "a": a_versions,
                "b": {1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["a"] == 1
        assert resolver.stats.restarts == 3


class TestTargetedBacktrack:
    """Cover ``apply_targeted_backtrack`` and the call site that wires
    its triggering package back into the main loop.

    Why direct unit tests: the targeted backtrack only fires when a
    culprit has accumulated five hits AND the resolver has logged at
    least thirty conflicts in the current restart segment. The default
    thresholds make this hard to reach without a contrived integration
    scenario, so we lower them for the integration test and exercise
    the helper's own branches directly.
    """

    def _resolver(self) -> Resolver:
        """Build a Resolver with state primed (matches a fresh resolve)."""
        provider = DictProvider({"root": {1: {}}})
        resolver = Resolver(provider)
        resolver._reset(None)
        return resolver

    def test_returns_none_when_cap_reached(self) -> None:
        r = self._resolver()
        r.stats.targeted_backtracks = r.MAX_TARGETED_BACKTRACKS
        r.pending_targeted_backtrack.append("a")
        assert apply_targeted_backtrack(r) is None
        assert r.pending_targeted_backtrack == []

    def test_returns_none_when_no_decision_for_culprit(self) -> None:
        r = self._resolver()
        # Pending package has no matching decision in the partial
        # solution, so triggering_package stays None.
        r.pending_targeted_backtrack.append("ghost")
        assert apply_targeted_backtrack(r) is None

    def test_returns_none_when_target_level_not_smaller(self) -> None:
        r = self._resolver()
        # Decide root at level 1, then a culprit at the same level via
        # a derivation. With no decision for "a", the inner break does
        # not fire and triggering_package remains None.
        r.solution.decide(ROOT, 1)
        r.pending_targeted_backtrack.append("a")
        assert apply_targeted_backtrack(r) is None

    def test_successful_backtrack_returns_triggering_package(self) -> None:
        r = self._resolver()
        # Two decisions; the culprit is the second one. target_level
        # should land before that decision and the package returns.
        r.solution.decide(ROOT, 1)
        r.solution.decide("a", 1)
        r.solution.decide("b", 1)
        before = r.solution.decision_level
        r.pending_targeted_backtrack.append("b")
        assert apply_targeted_backtrack(r) == "b"
        assert r.solution.decision_level < before
        assert r.stats.targeted_backtracks == 1
        assert r.pending_targeted_backtrack == []

    def test_backtrack_lands_on_root_level(self) -> None:
        r = self._resolver()
        # Culprit decision at level 2; target = 1 (just before "a").
        # Root stays decided at level 1.
        r.solution.decide(ROOT, 1)
        r.solution.decide("a", 1)
        r.pending_targeted_backtrack.append("a")
        assert apply_targeted_backtrack(r) == "a"
        assert r.solution.decision_level == 1

    def test_skips_culprit_already_at_higher_level(self) -> None:
        """Cover the inner-loop branch where a second pending culprit's
        decision level is not lower than the running ``target_level``."""
        r = self._resolver()
        r.solution.decide(ROOT, 1)
        r.solution.decide("low", 1)  # level 2
        r.solution.decide("high", 1)  # level 3
        # Order matters: process "low" first, target_level becomes 1;
        # then "high" with candidate=2, which is NOT < 1, so the inner
        # ``if candidate < target_level`` evaluates False and we break.
        r.pending_targeted_backtrack.extend(["low", "high"])
        assert apply_targeted_backtrack(r) == "low"
        assert r.solution.decision_level == 1

    def test_targets_propagated_culprit(self) -> None:
        """A culprit whose only trail entry is a derivation still triggers
        a backtrack: we look at the first assignment of any kind at level
        >= 2, since the level-1 floor preserves ROOT.
        """
        r = self._resolver()
        r.solution.decide(ROOT, 1)
        r.solution.decide("a", 1)  # level 2
        # Add a derivation for "p" at level 2 with a synthetic cause.
        cause = Incompatibility(
            [Term(ROOT, Range.full(), positive=True)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        r.solution.derive("p", Range.full(), positive=True, cause=cause)
        r.pending_targeted_backtrack.append("p")
        assert apply_targeted_backtrack(r) == "p"
        # Backtrack lands at level 1: above ROOT, below "a"/"p" derivation.
        assert r.solution.decision_level == 1

    def test_skips_root_level_derivation(self) -> None:
        """Derivations forced at level 1 (right after ROOT) cannot be
        moved by a backtrack, so the helper skips them rather than
        attempting to backtrack past ROOT.
        """
        r = self._resolver()
        r.solution.decide(ROOT, 1)
        # Derivation at level 1: backtracking to level 0 would remove ROOT.
        cause = Incompatibility(
            [Term(ROOT, Range.full(), positive=True)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        r.solution.derive("p", Range.full(), positive=True, cause=cause)
        r.pending_targeted_backtrack.append("p")
        assert apply_targeted_backtrack(r) is None
        # ROOT preserved: level still at 1.
        assert r.solution.decision_level == 1

    def test_call_site_uses_triggering_as_changed_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end cover for lines 303-309: the targeted-backtrack
        call site fires, returns a triggering package, and that package
        becomes the next ``changed_package`` for re-propagation.

        We lower the thresholds so a single backtracking scenario hits
        the gate, and we raise the restart threshold so restart never
        fires (which would clear the pending queue first).
        """
        monkeypatch.setattr(Resolver, "TARGETED_BT_MIN_CONFLICTS", 1)
        monkeypatch.setattr(Resolver, "CULPRIT_THRESHOLD", 1)
        monkeypatch.setattr(Resolver, "_RESTART_THRESHOLD", 10_000)
        provider = DictProvider(
            {
                "root": {1: {"a": Range.full(), "b": Range.full()}},
                "a": {v: {"b": Range.at_least(v)} for v in range(5, 0, -1)},
                "b": {1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["a"] == 1
        assert result["b"] == 1
        assert resolver.stats.targeted_backtracks >= 1

    def test_call_site_handles_none_triggering(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cover branch 308->318: the gate fires, ``_apply`` returns
        ``None`` (cap reached), and ``changed_package`` is left alone.

        We lower the gate to fire on the first conflict and set the cap
        to zero so the helper short-circuits immediately. The resolver
        still completes successfully because the cap clears the pending
        queue and the loop continues normally.
        """
        monkeypatch.setattr(Resolver, "TARGETED_BT_MIN_CONFLICTS", 1)
        monkeypatch.setattr(Resolver, "CULPRIT_THRESHOLD", 1)
        monkeypatch.setattr(Resolver, "MAX_TARGETED_BACKTRACKS", 0)
        monkeypatch.setattr(Resolver, "_RESTART_THRESHOLD", 10_000)
        provider = DictProvider(
            {
                "root": {1: {"a": Range.full(), "b": Range.full()}},
                "a": {v: {"b": Range.at_least(v)} for v in range(5, 0, -1)},
                "b": {1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result["a"] == 1
        assert result["b"] == 1
        assert resolver.stats.targeted_backtracks == 0


class _ForceBackTrackProvider(DictProvider):
    """Provider that returns a force-backtrack target after N calls.

    ``fire_after`` is the number of ``choose_version`` calls that must
    happen before the next drain returns the target. Counting choose
    calls lets the test build up decision-level state before firing.
    """

    def __init__(
        self,
        packages: dict[str, dict[int, dict[str, Range]]],
        target: str,
        fire_after: int = 0,
    ) -> None:
        super().__init__(packages)
        self._target = target
        self._fire_after = fire_after
        self._choose_count = 0
        self._fired = False

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        self._choose_count += 1
        return super().choose_version(package, version_range)

    def consume_force_backtrack_targets(self) -> list[str]:
        if self._fired or self._choose_count <= self._fire_after:
            return []
        self._fired = True
        return [self._target]


class TestForceBacktrack:
    """Cover ``force_targeted_backtrack`` and its resolver call site."""

    def test_empty_packages_returns_none(self) -> None:
        provider = DictProvider({"root": {1: {}}})
        resolver = Resolver(provider)
        resolver._reset(None)
        from nab_resolver.conflict import force_targeted_backtrack

        assert force_targeted_backtrack(resolver, []) is None

    def test_bumps_count_and_queues(self) -> None:
        provider = DictProvider({"root": {1: {"a": Range.singleton(1)}}, "a": {1: {}}})
        resolver = Resolver(provider)
        resolver._reset(None)
        resolver.solution.decide(ROOT, resolver.root_version)
        resolver.solution.decide("a", 1)
        from nab_resolver.conflict import force_targeted_backtrack

        force_targeted_backtrack(resolver, ["a"])
        assert resolver.stats.package_culprit_counts["a"] >= resolver.CULPRIT_THRESHOLD

    def test_resolver_calls_force_backtrack_target(self) -> None:
        provider = _ForceBackTrackProvider(
            {
                "root": {1: {"a": Range.singleton(1), "b": Range.singleton(1)}},
                "a": {1: {}},
                "b": {1: {}},
            },
            target="a",
        )
        resolver = Resolver(provider)
        resolver.resolve({"root": Range.singleton(1)})
        assert provider._fired

    def test_resolver_triggers_backtrack_path(self) -> None:
        """Resolver returns from _decide_next via the triggering branch."""
        provider = _ForceBackTrackProvider(
            {
                "root": {1: {"a": Range.singleton(1), "b": Range.singleton(1)}},
                "a": {1: {}},
                "b": {1: {}},
            },
            target="a",
            fire_after=2,
        )
        resolver = Resolver(provider)
        resolver.resolve({"root": Range.singleton(1)})
        assert provider._fired

    def test_force_backtrack_triggering_returns_package(self) -> None:
        """When the queued target has a decision, force_targeted_backtrack
        returns it and the resolver wires it as the next changed package."""
        # Construct a state where "a" is decided at level 2, force-backtrack
        # the resolver to before that level.
        provider = DictProvider(
            {
                "root": {1: {"a": Range.singleton(1), "b": Range.singleton(1)}},
                "a": {1: {}},
                "b": {1: {}},
            }
        )
        resolver = Resolver(provider)
        resolver._reset(None)
        resolver.solution.decide(ROOT, resolver.root_version)
        resolver.solution.decide("b", 1)
        resolver.solution.decide("a", 1)
        from nab_resolver.conflict import force_targeted_backtrack

        triggering = force_targeted_backtrack(resolver, ["a"])
        assert triggering == "a"

    def test_skips_count_bump_when_already_at_threshold(self) -> None:
        """Packages already at or above the threshold keep their count."""
        provider = DictProvider({"root": {1: {"a": Range.singleton(1)}}, "a": {1: {}}})
        resolver = Resolver(provider)
        resolver._reset(None)
        resolver.solution.decide(ROOT, resolver.root_version)
        resolver.solution.decide("a", 1)
        resolver.stats.package_culprit_counts["a"] = resolver.CULPRIT_THRESHOLD + 7
        from nab_resolver.conflict import force_targeted_backtrack

        force_targeted_backtrack(resolver, ["a"])
        assert (
            resolver.stats.package_culprit_counts["a"] == resolver.CULPRIT_THRESHOLD + 7
        )

    def test_skips_queue_when_already_pending(self) -> None:
        """A package already in the queue is not re-added before apply."""
        provider = DictProvider({"root": {1: {"a": Range.singleton(1)}}, "a": {1: {}}})
        resolver = Resolver(provider)
        resolver._reset(None)
        # Pre-populate pending without applying; force_targeted_backtrack
        # then calls apply_targeted_backtrack which clears the queue.
        # No decisions exist for "a" so apply returns None.
        resolver.pending_targeted_backtrack.append("a")
        from nab_resolver.conflict import force_targeted_backtrack

        result = force_targeted_backtrack(resolver, ["a"])
        # Still no triggering package, but the bump still happened.
        assert result is None
        assert resolver.stats.package_culprit_counts["a"] == resolver.CULPRIT_THRESHOLD


class TestDependencyMerge:
    """Cover the dependency-clause merge helpers."""

    def _resolver(self) -> Resolver:
        provider = DictProvider({"root": {1: {}}})
        resolver = Resolver(provider)
        resolver._reset(None)
        return resolver

    def test_merge_key_skips_non_dependency_cause(self) -> None:
        """A non-DEPENDENCY clause is never mergeable."""
        inc = Incompatibility(
            [
                Term("a", Range.singleton(1), positive=True),
                Term("b", Range.singleton(1), positive=True),
            ],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        assert dependency_merge_key(inc) is None

    def test_merge_key_skips_negative_first_term(self) -> None:
        """A dependency clause whose first term is negative is unmergeable.

        The resolver and look-ahead helpers always emit positive-
        first DEPENDENCY clauses; the helper still guards against
        a hand-built clause that doesn't follow that convention.
        """
        r = self._resolver()
        inc = Incompatibility(
            [
                Term("a", Range.singleton(1), positive=False),
                Term("b", Range.singleton(1), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert dependency_merge_key(inc) is None
        # ``maybe_merge_dependency`` short-circuits on the same key
        # check, so an unmergeable clause never invokes the merge path.
        assert not maybe_merge_dependency(r, inc)

    def test_merge_unions_package_ranges(self) -> None:
        """Two clauses with the same dep term merge into one."""
        r = self._resolver()
        first = Incompatibility(
            [
                Term("a", Range.singleton(1), positive=True),
                Term("b", Range.at_least(2), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        add_incompatibility(r, first)
        second = Incompatibility(
            [
                Term("a", Range.singleton(2), positive=True),
                Term("b", Range.at_least(2), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        add_incompatibility(r, second)
        assert len(r.incompatibilities) == 1
        merged = r.incompatibilities[0]
        assert 1 in merged.terms[0].constraint
        assert 2 in merged.terms[0].constraint

    def test_merge_subsumed_clause_is_dropped(self) -> None:
        """A clause whose package range is already covered is a no-op."""
        r = self._resolver()
        first = Incompatibility(
            [
                Term("a", Range.at_least(1), positive=True),
                Term("b", Range.at_least(2), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        add_incompatibility(r, first)
        second = Incompatibility(
            [
                Term("a", Range.singleton(3), positive=True),
                Term("b", Range.at_least(2), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        add_incompatibility(r, second)
        assert len(r.incompatibilities) == 1
        assert r.incompatibilities[0] is first


class TestErrorMessages:
    def test_error_has_incompatibility_chain(self) -> None:
        """Resolution errors should carry the derivation chain."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(2)}},
                "foo": {},  # no versions at all
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve({"root": Range.singleton(1)})
        assert exc_info.value.incompatibility is not None

    def test_error_message_contains_derived_explanation(self) -> None:
        """Error message traces through derived incompatibilities."""
        a_deps = {"D": Range.at_least(2)}
        provider = DictProvider(
            {
                "root": {1: {"A": Range.full(), "E": Range.full()}},
                "A": dict.fromkeys(range(5, 0, -1), a_deps),
                "D": {3: {}, 2: {}, 1: {}},
                "E": {1: {"D": Range.less_than(1)}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve({"root": Range.singleton(1)})
        message = str(exc_info.value)
        assert "D" in message or "A" in message or "E" in message
        root_inc = exc_info.value.incompatibility
        assert root_inc is not None

    def test_dependency_clause_attributes_parent_to_dependency(self) -> None:
        """A DEPENDENCY clause reads as parent depends on dependency."""
        parent = Term("foo", Range.singleton(2), positive=True)
        dependency = Term("bar", Range.at_least(3), positive=False)
        clause = Incompatibility(
            [parent, dependency], cause=IncompatibilityCause.DEPENDENCY
        )
        message = format_error(clause)
        assert message == "because foo 2 depends on bar [3, +inf)"

    def test_self_dependency_clause_names_the_edge(self) -> None:
        """A self-dependency clause names the range its merged term cannot hold."""
        clause = Incompatibility(
            [Term("foo", Range.singleton(2), positive=True)],
            cause=IncompatibilityCause.DEPENDENCY,
            dependency_range=Range.at_least(3),
        )
        message = format_error(clause)
        assert message == "because foo 2 depends on foo [3, +inf)"

    def test_dependency_clause_without_a_range_reads_as_a_prefix(self) -> None:
        """A one-term clause carrying no range names no dependency edge."""
        clause = Incompatibility(
            [Term("foo", Range.singleton(2), positive=True)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        message = format_error(clause)
        assert message == "because foo 2"

    def test_positive_dependency_term_reads_as_incompatible(self) -> None:
        """A both-positive DEPENDENCY clause reads as an incompatibility.

        The look-ahead flush groups rejected candidates into
        ``{candidate in {v}, blocker == w}`` with the blocker term positive,
        so it holds the version the candidate forbids, not a required range.
        It must not claim the candidate depends on that version.
        """
        candidate = Term("app", Range.singleton(3), positive=True)
        blocker = Term("lib", Range.singleton(9), positive=True)
        clause = Incompatibility(
            [candidate, blocker], cause=IncompatibilityCause.DEPENDENCY
        )
        message = format_error(clause)
        assert message == "because app 3 is incompatible with lib 9"
        assert "depends on" not in message

    def test_root_clause_attributes_to_the_project(self) -> None:
        """A ROOT clause reads as a project dependency and ignores the root term."""
        root = Term("root", Range.singleton(0), positive=True)
        dependency = Term("baz", Range.at_least(1), positive=False)
        clause = Incompatibility([root, dependency], cause=IncompatibilityCause.ROOT)
        message = format_error(clause)
        assert message == "because your project depends on baz [1, +inf)"

    def test_no_versions_clause_reports_availability(self) -> None:
        """A NO_VERSIONS clause names the unavailable range."""
        clause = Incompatibility(
            [Term("qux", Range.at_least(5), positive=True)],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        message = format_error(clause)
        assert message == "because no versions of qux [5, +inf) are available"

    def test_derived_clause_chains_children_with_so(self) -> None:
        """A DERIVED clause renders its child cause first, then a `so` line."""
        no_versions = Incompatibility(
            [Term("qux", Range.at_least(5), positive=True)],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        derived = Incompatibility(
            [Term("qux", Range.at_least(5), positive=True)],
            cause=IncompatibilityCause.DERIVED,
            cause_left=no_versions,
        )
        assert format_error(derived).splitlines() == [
            "because no versions of qux [5, +inf) are available",
            "so qux [5, +inf)",
        ]

    def test_clause_holding_only_the_root_states_the_conclusion(self) -> None:
        """A clause left with the virtual root alone has no package to name."""
        # Term[Any, int] sidesteps the invariant PackageType TypeVar so
        # ROOT and str entries can share a list.
        root_term: Term[Any, int] = Term(ROOT, Range.singleton(0), positive=True)
        absent_baz: Term[Any, int] = Term("baz", Range.full(), positive=False)
        project = Incompatibility(
            [root_term, absent_baz],
            cause=IncompatibilityCause.ROOT,
        )
        terminal = Incompatibility(
            [root_term],
            cause=IncompatibilityCause.DERIVED,
            cause_left=project,
        )
        assert format_error(terminal).splitlines() == [
            "because your project depends on baz",
            "so your project's requirements cannot be satisfied",
        ]

    def test_report_never_names_the_root_sentinel(self) -> None:
        """A derived line that absorbed a project requirement drops the root term."""
        provider = DictProvider({"a": {2: {"b": Range.at_least(2)}}, "b": {1: {}}})
        with pytest.raises(ResolutionError) as exc_info:
            Resolver(provider).resolve(
                {"a": Range.singleton(2), "b": Range.between(1, 2)}
            )
        assert str(exc_info.value).splitlines() == [
            "because a 2 depends on b [2, +inf)",
            "because your project depends on b [1, 2)",
            "so a 2",
            "because your project depends on a 2",
            "so your project's requirements cannot be satisfied",
        ]

    def test_format_term_marks_negation(self) -> None:
        """format_term prefixes a negated term with `not` and leaves positives bare."""
        positive = format_term(Term("a", Range.at_least(1), positive=True))
        negative = format_term(Term("a", Range.at_least(1), positive=False))
        assert positive == "a [1, +inf)"
        assert negative == "not a [1, +inf)"


class WideningProvider(DictProvider):
    """Widens every decision to the full range, narrowing it back at display time.

    The narrowing has to change the report, or a render that skipped the hook
    would look the same.
    """

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        return Range.full()

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        return Range.singleton(1) if package == "foo" else constraint


def bracketed(constraint: object) -> str:
    """Render a constraint unlike ``str`` does, to tell the two renders apart."""
    return f"<{constraint}>"


# foo and bar each depend on a half of baz the other rules out.
CONFLICTING: dict[str, dict[int, dict[str, Range]]] = {
    "root": {1: {"foo": Range.full(), "bar": Range.full()}},
    "foo": {1: {"baz": Range.at_least(2)}},
    "bar": {1: {"baz": Range.less_than(2)}},
    "baz": {2: {}, 1: {}},
}


class TestResolutionErrorMessageContract:
    """What ``str(error)`` is, and the two raise sites where it is something else."""

    def test_message_is_the_report_the_display_hooks_produce(self) -> None:
        provider = WideningProvider(CONFLICTING)
        resolver = Resolver(provider, format_range=bracketed)

        with pytest.raises(ResolutionError) as excinfo:
            resolver.resolve({"root": Range.singleton(1)})

        error = excinfo.value
        assert error.incompatibility is not None
        assert str(error) == format_error(
            error.incompatibility,
            narrow=provider.narrow_for_display,
            format_range=bracketed,
        )

        # Neither hook is optional: leaving either out gives a different report.
        assert str(error) != format_error(
            error.incompatibility, narrow=provider.narrow_for_display
        )
        assert str(error) != format_error(error.incompatibility, format_range=bracketed)

    def test_the_iteration_limit_carries_no_derivation(self) -> None:
        resolver = Resolver(DictProvider(CONFLICTING), max_iterations=1)

        with pytest.raises(ResolutionError) as excinfo:
            resolver.resolve({"root": Range.singleton(1)})

        assert excinfo.value.incompatibility is None
        assert str(excinfo.value) == "Resolution exceeded 1 iterations"

    @pytest.mark.timeout(STALL_TIMEOUT_SECONDS)
    def test_a_stalled_conflict_loop_reports_the_bug_not_the_derivation(self) -> None:
        solution, stalled = stalled_solution(padding=0)
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution = solution

        with pytest.raises(ResolutionError) as excinfo:
            conflict_resolution(resolver, stalled)

        error = excinfo.value
        assert error.incompatibility is not None
        assert str(error) != format_error(error.incompatibility)


class TestFormatRangeHook:
    """``format_range`` renders every constraint a report line shows.

    A range type whose ``str`` is a debug representation supplies its own; the
    default is ``str``, which reads well for :class:`Range`.
    """

    @staticmethod
    def _shown(_constraint: object) -> str:
        return "SHOWN"

    @staticmethod
    def _blank(_constraint: object) -> str:
        return ""

    def test_hook_renders_terms_on_both_sides_of_a_dependency(self) -> None:
        dependency = Incompatibility(
            [
                Term("a", Range.at_least(1), positive=True),
                Term("b", Range.at_least(3), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert (
            format_error(dependency, format_range=self._shown)
            == "because a SHOWN depends on b SHOWN"
        )

    def test_hook_renders_the_user_constraint_line(self) -> None:
        constrained = Incompatibility(
            [Term("a", Range.at_least(1), positive=True)],
            cause=IncompatibilityCause.CONSTRAINT,
            constraint_range=Range.less_than(2),
        )
        assert (
            format_error(constrained, format_range=self._shown)
            == "because the user constrained a SHOWN"
        )

    def test_a_range_rendered_as_nothing_leaves_no_trailing_space(self) -> None:
        """An unconstrained range renders empty, so the name carries the line."""
        term = Term("a", Range.at_least(1), positive=False)
        assert format_term(term, self._blank) == "not a"

        constrained = Incompatibility(
            [Term("a", Range.at_least(1), positive=True)],
            cause=IncompatibilityCause.CONSTRAINT,
            constraint_range=Range.full(),
        )
        assert (
            format_error(constrained, format_range=self._blank)
            == "because the user constrained a"
        )


class TestFullRangeWording:
    """A term admitting every version reads as prose, not as an interval.

    The wording depends on where in the sentence the term sits.
    """

    def test_format_term_renders_a_full_positive_term_as_all_versions(self) -> None:
        assert (
            format_term(Term("a", Range.full(), positive=True)) == "all versions of a"
        )

    def test_format_term_keeps_the_range_on_a_full_negative_term(self) -> None:
        """A negated full term is not "all versions"; it excludes them all."""
        assert format_term(Term("a", Range.full(), positive=False)) == "not a *"

    def test_a_gapless_union_reads_as_full(self) -> None:
        """A union whose parts meet admits every version."""
        gapless = Range.less_than(3) | Range.at_least(3)
        assert format_term(Term("a", gapless, positive=True)) == "all versions of a"

    def test_no_versions_clause_drops_the_range(self) -> None:
        clause = Incompatibility(
            [Term("qux", Range.full(), positive=True)],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        assert format_error(clause) == "because no versions of qux are available"

    def test_root_requires_clause_drops_the_range(self) -> None:
        """The prefix form, reached by a ROOT clause that is not the two-term one."""
        clause = Incompatibility(
            [Term("baz", Range.full(), positive=True)],
            cause=IncompatibilityCause.ROOT,
        )
        assert format_error(clause) == "because root requires baz"

    def test_project_dependency_clause_drops_the_range(self) -> None:
        clause = Incompatibility(
            [
                Term("root", Range.singleton(0), positive=True),
                Term("baz", Range.full(), positive=False),
            ],
            cause=IncompatibilityCause.ROOT,
        )
        assert format_error(clause) == "because your project depends on baz"

    def test_dependency_side_drops_the_range(self) -> None:
        clause = Incompatibility(
            [
                Term("foo", Range.singleton(2), positive=True),
                Term("bar", Range.full(), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert format_error(clause) == "because foo 2 depends on bar"

    def test_full_parent_takes_a_plural_verb(self) -> None:
        clause = Incompatibility(
            [
                Term("foo", Range.full(), positive=True),
                Term("bar", Range.at_least(3), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert format_error(clause) == (
            "because all versions of foo depend on bar [3, +inf)"
        )

    def test_full_parent_takes_a_plural_verb_on_the_incompatible_form(self) -> None:
        clause = Incompatibility(
            [
                Term("app", Range.full(), positive=True),
                Term("lib", Range.singleton(9), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert format_error(clause) == (
            "because all versions of app are incompatible with lib 9"
        )

    def test_full_blocker_keeps_the_all_versions_form(self) -> None:
        """The blocker is not a requirement, so it keeps the subject wording."""
        clause = Incompatibility(
            [
                Term("app", Range.singleton(3), positive=True),
                Term("lib", Range.full(), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert format_error(clause) == (
            "because app 3 is incompatible with all versions of lib"
        )

    def test_derived_clause_keeps_the_all_versions_form(self) -> None:
        clause = Incompatibility(
            [
                Term("a", Range.full(), positive=True),
                Term("b", Range.between(1, 9), positive=False),
            ],
            cause=IncompatibilityCause.DERIVED,
        )
        assert format_error(clause) == "so all versions of a and not b [1, 9)"

    def test_derivation_deeper_than_the_recursion_limit_renders(self) -> None:
        """A chain longer than the recursion limit renders instead of overflowing."""
        depth = sys.getrecursionlimit() + 100
        node = Incompatibility(
            [Term("tail", Range.at_least(1), positive=True)],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        for index in range(depth):
            satisfier = Incompatibility(
                [
                    Term(f"p{index}", Range.singleton(1), positive=True),
                    Term("tail", Range.at_least(1), positive=False),
                ],
                cause=IncompatibilityCause.DEPENDENCY,
            )
            node = Incompatibility(
                [Term(f"p{index}", Range.singleton(1), positive=True)],
                cause=IncompatibilityCause.DERIVED,
                cause_left=node,
                cause_right=satisfier,
            )

        lines = format_error(node).splitlines()

        assert len(lines) == 2 * depth + 1
        assert lines[0] == "because no versions of tail [1, +inf) are available"
        assert lines[1] == "because p0 1 depends on tail [1, +inf)"
        assert lines[2] == "so p0 1"
        assert lines[-2] == f"because p{depth - 1} 1 depends on tail [1, +inf)"
        assert lines[-1] == f"so p{depth - 1} 1"


class TestRootRequirements:
    def test_disjoint_requirements_are_named_separately(self) -> None:
        """Two roots on one package each get a line naming what was written."""
        provider = DictProvider({"pkg": {2: {}, 1: {}}})
        with pytest.raises(ResolutionError) as excinfo:
            Resolver(provider).resolve(
                [
                    RootRequirement("pkg", Range.greater_than(1)),
                    RootRequirement("pkg", Range.singleton(1)),
                ]
            )

        lines = str(excinfo.value).splitlines()
        assert "because your project depends on pkg (1, +inf)" in lines
        assert "because your project depends on pkg 1" in lines
        assert not any("empty" in line for line in lines)

    def test_overlapping_requirements_survive_to_the_solution(self) -> None:
        """Roots that intersect to a live range still resolve, once."""
        provider = DictProvider({"pkg": {3: {}, 2: {}, 1: {}}})
        result = Resolver(provider).resolve(
            [
                RootRequirement("pkg", Range.at_least(1)),
                RootRequirement("pkg", Range.at_most(2)),
            ]
        )
        assert result == {"pkg": 2}

    def test_transitive_narrowing_names_the_written_requirement(self) -> None:
        """A conflict past the intersection still quotes a root as written."""
        provider = DictProvider(
            {
                "pkg": {2: {"dep": Range.singleton(9)}},
                "dep": {1: {}},
            }
        )
        with pytest.raises(ResolutionError) as excinfo:
            Resolver(provider).resolve(
                [
                    RootRequirement("pkg", Range.at_least(1)),
                    RootRequirement("pkg", Range.at_most(2)),
                ]
            )

        lines = str(excinfo.value).splitlines()
        assert "because your project depends on pkg [1, +inf)" in lines
        assert "because your project depends on pkg (-inf, 2]" in lines

    def test_repeated_package_keeps_its_first_mention_order(self) -> None:
        """Naming a package twice must not push its decision later."""
        provider = DictProvider({"pkg": {1: {}}, "other": {1: {}}})
        resolver = Resolver(provider)
        resolver.resolve(
            [
                RootRequirement("pkg", Range.full()),
                RootRequirement("other", Range.full()),
                RootRequirement("pkg", Range.at_least(1)),
            ]
        )
        assert resolver.root_package_order["pkg"] == (0, 0, "")
        assert resolver.root_package_order["other"] == (0, 1, "")

    def test_origin_travels_onto_the_root_clause(self) -> None:
        """The caller's opaque origin is readable off the clause it produced."""
        provider = DictProvider({"pkg": {1: {}}})
        resolver = Resolver(provider)
        resolver.resolve([RootRequirement("pkg", Range.full(), "pkg>=1 (line 3)")])

        origins = [
            incompatibility.origin
            for incompatibility in resolver.incompatibilities
            if incompatibility.cause is IncompatibilityCause.ROOT
        ]
        assert origins == ["pkg>=1 (line 3)"]

    def test_mapping_form_leaves_the_origin_unset(self) -> None:
        """A mapping caller keeps working and sets no origin."""
        provider = DictProvider({"pkg": {1: {}}})
        resolver = Resolver(provider)
        assert resolver.resolve({"pkg": Range.full()}) == {"pkg": 1}

        roots = [
            incompatibility
            for incompatibility in resolver.incompatibilities
            if incompatibility.cause is IncompatibilityCause.ROOT
        ]
        assert [incompatibility.origin for incompatibility in roots] == [None]


class TestConstraints:
    def test_constraint_narrows_version(self) -> None:
        """A constraint restricts which version is picked."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve(
            {"root": Range.singleton(1)},
            constraints={"foo": Range.less_than(3)},
        )
        assert result["foo"] == 2

    def test_constraint_ignored_when_package_unused(self) -> None:
        """A constraint on an unreferenced package has no effect."""
        provider = DictProvider(
            {
                "root": {1: {}},
                "bar": {1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve(
            {"root": Range.singleton(1)},
            constraints={"bar": Range.less_than(1)},
        )
        assert "bar" not in result

    def test_constraint_conflict_reports_provenance(self) -> None:
        """Error message distinguishes constraint from dependency."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {1: {"bar": Range.at_least(3)}},
                "bar": {3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(
                {"root": Range.singleton(1)},
                constraints={"bar": Range.less_than(2)},
            )
        message = str(exc_info.value)
        assert "the user constrained" in message

    def test_constraint_conflict_message_shows_user_constraint(self) -> None:
        """The CONSTRAINT line names the user's constraint, not the requirement.

        The user constrains bar<2 while foo depends on bar>=3. The line that
        attributes a range to the user must show bar<2, never foo's bar>=3.
        """
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {1: {"bar": Range.at_least(3)}},
                "bar": {3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(
                {"root": Range.singleton(1)},
                constraints={"bar": Range.less_than(2)},
            )
        message = str(exc_info.value)
        constraint_line = next(
            line for line in message.splitlines() if "the user constrained" in line
        )
        assert str(Range.less_than(2)) in constraint_line
        assert str(Range.at_least(3)) not in constraint_line

    def test_constraint_with_matching_requirement(self) -> None:
        """A constraint on a root requirement narrows the range."""
        provider = DictProvider(
            {
                "foo": {3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve(
            {"foo": Range.at_least(1)},
            constraints={"foo": Range.less_than(3)},
        )
        assert result["foo"] == 2

    def test_constraint_causes_backtracking(self) -> None:
        """A constraint forces the resolver to backtrack to a compatible version."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {
                    3: {"bar": Range.at_least(3)},
                    2: {"bar": Range.at_least(1)},
                    1: {},
                },
                "bar": {3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve(
            {"root": Range.singleton(1)},
            constraints={"bar": Range.less_than(3)},
        )
        # foo 3 needs bar>=3, but constraint says bar<3, so falls back to foo 2
        assert result["foo"] == 2
        assert result["bar"] == 2

    def test_no_constraints_is_same_as_empty(self) -> None:
        """Passing no constraints or empty dict gives same result."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {2: {}, 1: {}},
            }
        )
        r1 = Resolver(provider)
        r2 = Resolver(provider)
        r3 = Resolver(provider)
        result1 = r1.resolve({"root": Range.singleton(1)})
        result2 = r2.resolve({"root": Range.singleton(1)}, constraints=None)
        result3 = r3.resolve({"root": Range.singleton(1)}, constraints={})
        assert result1 == result2 == result3

    def test_constraint_unsatisfiable(self) -> None:
        """A constraint that excludes all versions causes resolution failure."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {3: {}, 2: {}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(
                {"root": Range.singleton(1)},
                constraints={"foo": Range.less_than(1)},
            )
        message = str(exc_info.value)
        assert "the user constrained" in message

    def test_constraint_any_is_no_op(self) -> None:
        """A constraint allowing all versions has no effect."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(1)}},
                "foo": {3: {}, 2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve(
            {"root": Range.singleton(1)},
            constraints={"foo": Range.full()},
        )
        assert result["foo"] == 3

    def test_constraint_on_transitive_dependency(self) -> None:
        """A constraint on a transitive dep is applied when it enters resolution."""
        provider = DictProvider(
            {
                "root": {1: {"A": Range.at_least(1)}},
                "A": {1: {"B": Range.at_least(1)}},
                "B": {3: {"C": Range.at_least(1)}, 2: {"C": Range.at_least(1)}, 1: {}},
                "C": {1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve(
            {"root": Range.singleton(1)},
            constraints={"B": Range.less_than(3)},
        )
        assert result["B"] == 2

    def test_constraint_does_not_cause_false_unsat(self) -> None:
        """A constraint must not block a solution that never uses the package.

        root needs foo<=7. foo==7 pulls in bar; bar's only version needs
        foo<2, so the foo==7 branch conflicts. foo==4 has no deps, so bar
        is never used and its constraint is vacuous. The resolver must
        find {foo: 4} rather than reporting UNSAT.
        """
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_most(7)}},
                "foo": {7: {"bar": Range.at_least(1)}, 4: {}},
                "bar": {8: {"foo": Range.less_than(2)}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve(
            {"root": Range.singleton(1)},
            constraints={"bar": Range.singleton(8)},
        )
        assert result["foo"] == 4
        assert "bar" not in result

    def test_no_versions_in_range_is_not_attributed_to_constraint(self) -> None:
        """A no-versions failure keeps its own provenance when the
        constraint does not narrow the searched range."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(5)}},
                "foo": {3: {}, 2: {}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(
                {"root": Range.singleton(1)},
                constraints={"foo": Range.at_least(1)},
            )
        assert "the user constrained" not in str(exc_info.value)

    def test_missing_package_not_blamed_on_clipping_constraint(self) -> None:
        """A package with zero versions fails as NO_VERSIONS even when a
        benign constraint clips the searched range.

        The constraint narrows ``*`` to ``< 100`` but is not why no version
        was found, so it must not take the blame nor have its range printed.
        """
        provider = DictProvider({"root": {1: {"foo": Range.full()}}})
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(
                {"root": Range.singleton(1)},
                constraints={"foo": Range.less_than(100)},
            )
        message = str(exc_info.value)
        assert "the user constrained" not in message
        assert "no versions of foo are available" in message

    def test_out_of_range_version_not_blamed_on_constraint(self) -> None:
        """foo publishes only v5 but is required ``>= 10``. A ``!= 50``
        constraint cannot exclude a candidate that never fell in range, so
        the failure stays NO_VERSIONS over the requirement range."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(10)}},
                "foo": {5: {}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(
                {"root": Range.singleton(1)},
                constraints={"foo": ~Range.singleton(50)},
            )
        message = str(exc_info.value)
        assert "the user constrained" not in message
        assert "no versions of foo [10, +inf) are available" in message

    def test_constraint_that_excludes_only_candidate_is_blamed(self) -> None:
        """When the sole in-range version is the one the constraint excludes,
        the constraint is the genuine cause and keeps the blame."""
        provider = DictProvider(
            {
                "root": {1: {"foo": Range.at_least(10)}},
                "foo": {50: {}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve(
                {"root": Range.singleton(1)},
                constraints={"foo": ~Range.singleton(50)},
            )
        assert "the user constrained" in str(exc_info.value)


class TestForceResolutionStep:
    """Direct unit tests for ``try_force_resolution_step``.

    The always-learn path resolves a single-term NO_VERSIONS clause
    once with the satisfier's cause to produce a multi-term clause
    that supports a backjump.  The early-return paths are reached
    when the preconditions or post-resolve invariants don't hold.
    """

    def _resolver(self) -> Resolver:
        provider = DictProvider({"root": {1: {}}})
        resolver = Resolver(provider)
        resolver._reset(None)
        return resolver

    def test_returns_none_when_resolved_clause_cant_backjump(self) -> None:
        """If the force-resolved clause's most-recent satisfier is itself
        a derivation at the same decision level as the next-most-recent
        satisfier, the resolved clause cannot drive a backjump.  In
        that case the function aborts and the caller stays with the
        original clause."""
        resolver = self._resolver()
        sol = resolver.solution

        sol.decide(ROOT, 1)

        # Term[Any, int] sidesteps the invariant PackageType TypeVar so
        # ROOT and str entries can share a list.
        root_y_terms: list[Term[Any, int]] = [
            Term(ROOT, Range.singleton(1), positive=True),
            Term("y", Range.between(1, 10), positive=False),
        ]
        inc_root_y_wide = Incompatibility(
            root_y_terms, cause=IncompatibilityCause.DEPENDENCY
        )
        sol.derive("y", Range.between(1, 10), positive=True, cause=inc_root_y_wide)

        root_x_terms: list[Term[Any, int]] = [
            Term(ROOT, Range.singleton(1), positive=True),
            Term("x", Range.singleton(1), positive=False),
        ]
        inc_root_x = Incompatibility(
            root_x_terms, cause=IncompatibilityCause.DEPENDENCY
        )
        sol.derive("x", Range.singleton(1), positive=True, cause=inc_root_x)

        inc_x_y_narrow = Incompatibility(
            [
                Term("x", Range.singleton(1), positive=True),
                Term("y", Range.between(3, 10), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        sol.derive("y", Range.between(3, 5), positive=True, cause=inc_x_y_narrow)

        no_versions = Incompatibility(
            [Term("y", Range.between(3, 5), positive=True)],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        y_satisfier = sol._assignments_by_package["y"][-1]

        result = try_force_resolution_step(
            resolver,
            no_versions,
            y_satisfier,
            no_versions.terms[0],
        )

        assert result is None


class TestRegressions:
    def test_remainder_without_satisfier(self) -> None:
        """Regression: partial satisfier with no remainder satisfier.

        When conflict resolution computes a remainder term for a
        package that has been backjumped away, the partial solution
        has no assignments for that package. Previously this raised
        RuntimeError; now it returns current_previous_level.
        """
        provider = DictProvider(
            {
                "pkg0": {
                    1: {
                        "pkg1": Range.singleton(2),
                        "pkg2": Range.full(),
                        "pkg3": Range.full(),
                    },
                    2: {"pkg2": Range.full(), "pkg4": Range.full()},
                    3: {"pkg2": Range.full(), "pkg4": Range.full()},
                },
                "pkg1": {1: {"pkg0": Range.singleton(4), "pkg2": Range.full()}},
                "pkg2": {
                    1: {},
                    2: {
                        "pkg0": Range.full(),
                        "pkg1": Range.full(),
                        "pkg3": Range.full(),
                    },
                },
                "pkg3": {1: {}},
                "pkg4": {
                    1: {
                        "pkg0": Range.full(),
                        "pkg1": Range.full(),
                        "pkg2": Range.full(),
                    },
                    2: {
                        "pkg0": Range.full(),
                        "pkg1": Range.full(),
                        "pkg2": Range.full(),
                    },
                    3: {"pkg1": Range.full()},
                },
                "root": {1: {"pkg0": Range.full()}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError):
            resolver.resolve({"root": Range.singleton(1)})


class _SeededPackage:
    """Package key with an explicit hash, standing in for one str hash seed."""

    def __init__(self, name: str, hash_value: int) -> None:
        self.name = name
        self.hash_value = hash_value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _SeededPackage) and self.name == other.name

    def __hash__(self) -> int:
        return self.hash_value


class TestHashOrderIndependence:
    """The conflict path must not depend on the per-process str hash seed."""

    _NAMES = tuple(f"pkg{i}" for i in range(8))

    def _prior_cause_order(self, hashes: list[int]) -> list[str]:
        packages = [
            _SeededPackage(n, h) for n, h in zip(self._NAMES, hashes, strict=True)
        ]
        shared = _SeededPackage("shared", 100)
        incompatibility = Incompatibility(
            [Term(p, Range.singleton(1)) for p in [shared, *packages[:4]]],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        cause = Incompatibility(
            [Term(p, Range.singleton(1)) for p in [shared, *packages[4:]]],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        terms = prior_cause(incompatibility, cause, shared)
        return [term.package.name for term in terms]

    def test_prior_cause_term_order_ignores_package_hashes(self) -> None:
        forward = self._prior_cause_order(list(range(8)))
        backward = self._prior_cause_order(list(range(7, -1, -1)))
        assert forward == backward

    def _culprit_queue_order(self, hashes: list[int]) -> list[str]:
        resolver: Resolver[Any, int] = Resolver(DictProvider({}))
        packages = [
            _SeededPackage(n, h) for n, h in zip(self._NAMES, hashes, strict=True)
        ]
        affected = _SeededPackage("affected", 100)
        incompatibility = Incompatibility(
            [Term(p, Range.singleton(1)) for p in [affected, *packages]],
            cause=IncompatibilityCause.DERIVED,
        )
        for package in packages:
            resolver.stats.package_culprit_counts[package] = (
                resolver.CULPRIT_THRESHOLD - 1
            )
        satisfier = Assignment(
            package=affected,
            accumulated_range=Range.singleton(1),
            decision_level=1,
            is_decision=True,
        )
        update_culprit_counts(resolver, incompatibility, affected, satisfier)
        return [package.name for package in resolver.pending_targeted_backtrack]

    def test_culprit_queue_order_ignores_package_hashes(self) -> None:
        forward = self._culprit_queue_order(list(range(8)))
        backward = self._culprit_queue_order(list(range(7, -1, -1)))
        assert forward == backward


class TestClassifyRelation:
    @pytest.mark.parametrize(
        ("positive", "subset", "disjoint", "expected"),
        [
            (True, True, False, SetRelation.SATISFIED),
            (True, False, True, SetRelation.CONTRADICTED),
            (True, False, False, SetRelation.UNDETERMINED),
            (False, False, True, SetRelation.SATISFIED),
            (False, True, False, SetRelation.CONTRADICTED),
            (False, False, False, SetRelation.UNDETERMINED),
        ],
    )
    def test_maps_each_relation_to_its_member(
        self,
        *,
        positive: bool,
        subset: bool,
        disjoint: bool,
        expected: SetRelation,
    ) -> None:
        term = Term("foo", Range.at_least(1), positive=positive)

        result = classify_relation(term, subset=subset, disjoint=disjoint)

        assert result is expected

    @pytest.mark.parametrize("positive", [True, False])
    def test_an_empty_assignment_reads_as_satisfied(self, *, positive: bool) -> None:
        """An empty assignment is both a subset of and disjoint from anything.

        Satisfied is tested first, so it wins for a term of either sign.
        """
        term = Term("foo", Range.at_least(1), positive=positive)

        result = classify_relation(term, subset=True, disjoint=True)

        assert result is SetRelation.SATISFIED


class TestRelationCache:
    @staticmethod
    def _token_key(
        resolver: Resolver[str, Any], term: Term[str, Any]
    ) -> tuple[bool, int, int]:
        """Return the relation-cache key a positive ``term`` probes for."""
        assignment = resolver.solution.get(term.package)
        assert assignment is not None
        return (
            True,
            resolver.range_tokens[assignment],
            resolver.range_tokens[term.constraint],
        )

    def test_caches_relation_and_reuses_it(self) -> None:
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution.decide("foo", 2)
        term = Term("foo", Range.at_least(1), positive=True)

        assert resolver.relation_cache == {}
        first = term_relation(resolver, term)
        key = self._token_key(resolver, term)
        assert resolver.relation_cache == {key: first}

        assert term_relation(resolver, term) is first
        assert resolver.relation_cache == {key: first}

    def test_equal_ranges_share_a_token(self) -> None:
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution.decide("foo", 2)
        term = Term("foo", Range.at_least(1), positive=True)
        equal_term = Term("foo", Range.at_least(1), positive=True)
        assert equal_term.constraint is not term.constraint

        first = term_relation(resolver, term)

        assert term_relation(resolver, equal_term) is first
        assert len(resolver.relation_cache) == 1

    def test_clears_cache_on_overflow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(propagate, "RELATION_CACHE_MAX", 1)
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution.decide("foo", 2)
        # Filler for another range pair, to put the cache at its cap.
        resolver.relation_cache[(False, 0, 0)] = SetRelation.SATISFIED

        term = Term("foo", Range.at_least(1), positive=True)
        result = term_relation(resolver, term)

        assert resolver.relation_cache == {self._token_key(resolver, term): result}

    def test_wiped_token_table_does_not_reissue_tokens(self) -> None:
        """A token is minted once, so a wipe cannot point it at another range."""
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution.decide("foo", 2)
        term_relation(resolver, Term("foo", Range.at_least(1), positive=True))
        minted = set(resolver.range_tokens.values())

        resolver.range_tokens.clear()
        resolver.range_token_by_id.clear()
        term_relation(resolver, Term("foo", Range.at_least(3), positive=True))

        assert minted
        assert minted.isdisjoint(resolver.range_tokens.values())

    def test_clears_address_memo_on_overflow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(propagate, "RANGE_ID_MEMO_MAX", 1)
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution.decide("foo", 2)
        term = Term("foo", Range.at_least(1), positive=True)

        first = term_relation(resolver, term)
        key = self._token_key(resolver, term)

        assert len(resolver.range_token_by_id) == 1
        assert len(resolver.interned_ranges) == 1

        assert term_relation(resolver, term) is first
        assert self._token_key(resolver, term) == key
        assert resolver.relation_cache == {key: first}

    @staticmethod
    def _probe(resolver: Resolver[str, int], lower: int) -> None:
        """Probe ``foo`` against ``>= lower``.

        A ``lower`` not used before misses; a repeat hits while the memo is on.
        """
        term_relation(resolver, Term("foo", Range.at_least(lower), positive=True))

    def test_gate_switches_the_memo_off_when_probes_mostly_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Too few hits in a window switch the memo off and drop its entries."""
        monkeypatch.setattr(propagate, "RELATION_GATE_WINDOW", 3)
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution.decide("foo", 2)
        # Filler for another range pair, to show the whole memo is dropped.
        resolver.relation_cache[(False, 0, 0)] = SetRelation.SATISFIED

        # A distinct constraint each time, so no probe in the window hits.
        for lower in (1, 3, 5):
            self._probe(resolver, lower)

        assert resolver.relation_cache_on is False
        assert resolver.relation_cache == {}
        assert resolver.relation_gate_probes_left == propagate.RELATION_GATE_RECHECK

    def test_gate_keeps_the_memo_while_probes_hit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enough hits keep the memo and its entries, and the next window opens.

        A hit spends a probe of the window without recomputing anything, so the
        third probe here closes a window of three on the second miss.
        """
        monkeypatch.setattr(propagate, "RELATION_GATE_WINDOW", 3)
        monkeypatch.setattr(propagate, "RELATION_GATE_MIN_HITS", 1)
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution.decide("foo", 2)

        self._probe(resolver, 1)
        self._probe(resolver, 1)
        assert resolver.relation_gate_hits == 1

        self._probe(resolver, 3)

        assert resolver.relation_cache_on is True
        assert len(resolver.relation_cache) == 2

        assert resolver.relation_gate_hits == 0
        assert resolver.relation_gate_probes_left == propagate.RELATION_GATE_WINDOW

    def test_a_hit_never_closes_the_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hit fills a probe of the window but leaves the judging to a miss.

        Two probes fill a window of two here, and it stays open because the
        second of them was a hit.
        """
        monkeypatch.setattr(propagate, "RELATION_GATE_WINDOW", 2)
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution.decide("foo", 2)

        self._probe(resolver, 1)
        self._probe(resolver, 1)

        assert resolver.relation_gate_probes_left == 1
        assert resolver.relation_gate_hits == 1

        # One hit is under the default threshold, so a window judged here would
        # have switched the memo off.
        assert resolver.relation_cache_on is True
        assert len(resolver.relation_cache) == 1

    def test_gate_tries_the_memo_again_after_the_recheck_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A switched-off memo comes back, and starts collecting again."""
        monkeypatch.setattr(propagate, "RELATION_GATE_WINDOW", 2)
        monkeypatch.setattr(propagate, "RELATION_GATE_RECHECK", 3)
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution.decide("foo", 2)

        self._probe(resolver, 1)
        self._probe(resolver, 3)
        assert resolver.relation_cache_on is False

        # The recheck window is longer than the one that judged the memo.
        self._probe(resolver, 5)
        self._probe(resolver, 7)
        assert resolver.relation_cache_on is False

        self._probe(resolver, 9)
        assert resolver.relation_cache_on is True
        assert resolver.relation_cache == {}

        term = Term("foo", Range.at_least(11), positive=True)
        result = term_relation(resolver, term)
        assert resolver.relation_cache == {self._token_key(resolver, term): result}

    def test_relation_is_unchanged_while_the_memo_is_off(self) -> None:
        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        resolver.solution.decide("foo", 2)
        term = Term("foo", Range.at_least(1), positive=True)
        expected = term_relation(resolver, term)

        resolver.relation_cache_on = False
        resolver.relation_cache.clear()

        assert term_relation(resolver, term) is expected
        assert resolver.relation_cache == {}

    def test_a_second_resolve_starts_with_the_memo_on(self) -> None:
        resolver: Resolver[str, int] = Resolver(DictProvider({"foo": {1: {}}}))
        resolver.resolve({"foo": Range.at_least(1)})
        resolver.relation_cache_on = False

        resolver.resolve({"foo": Range.at_least(1)})

        assert resolver.relation_cache_on is True


class LowestVersionProvider(DictProvider):
    """Provider that picks the lowest matching version rather than the highest."""

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        for version in reversed(self._get_versions(package)):
            if version in version_range:
                return version
        return None


def climbing_packages(count: int) -> dict[str, dict[int, dict[str, Range]]]:
    """Package data whose lowest-first resolve tries every version of ``a``.

    Version ``i`` of ``a`` needs ``b`` at exactly ``i`` and only the last ``b``
    exists, so each attempt adds a dependency clause and both packages' clause
    lists grow with the search.
    """
    return {
        "a": {
            version: {"b": Range.singleton(version)} for version in range(1, count + 1)
        },
        "b": {count: {}},
    }


class StampAuditor(ResolverObserver[str, int]):
    """Collects clauses a live stamp claims are contradicted but are not.

    A stale stamp is invisible from outside the solver, because propagation
    skips the clause instead of evaluating it.  An assignment is what leaves
    one behind, so the audit runs on each derivation.
    """

    def __init__(self, resolver: Resolver[str, int]) -> None:
        self._resolver = resolver
        self.stale: list[Incompatibility[str, int]] = []

    def on_derivation(
        self,
        package: str,
        *,
        positive: bool,
        cause: Incompatibility[str, int],
    ) -> None:
        del package, positive, cause
        resolver = self._resolver
        epoch = resolver.solution.contradiction_epoch

        for index, stamp in enumerate(resolver.clause_contradicted_at):
            if stamp != epoch:
                continue
            clause = resolver.incompatibilities[index]
            if (
                propagate.evaluate_incompatibility(resolver, clause)
                is not IncompatibilityState.CONTRADICTED
            ):
                self.stale.append(clause)


class TestSettledClauseSkip:
    """Cover the skip stamp unit propagation keeps for each clause."""

    def test_a_settled_clause_is_not_re_evaluated_before_a_rollback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One contradicted term settles a clause for the rest of the epoch."""
        evaluate = propagate.evaluate_incompatibility
        settled: set[tuple[int, Incompatibility[str, int]]] = set()
        repeats: list[Incompatibility[str, int]] = []

        def record(
            resolver: Resolver[str, int], incompatibility: Incompatibility[str, int]
        ) -> IncompatibilityState | Term[str, int] | None:
            """Note any clause evaluated twice in the epoch that settled it."""
            key = (resolver.solution.contradiction_epoch, incompatibility)
            if key in settled:
                repeats.append(incompatibility)

            result = evaluate(resolver, incompatibility)
            if result is IncompatibilityState.CONTRADICTED:
                settled.add(key)
            return result

        monkeypatch.setattr(propagate, "evaluate_incompatibility", record)
        resolver = Resolver(LowestVersionProvider(climbing_packages(20)))

        assert resolver.resolve({"a": Range.full()}) == {"a": 20, "b": 20}
        assert repeats == [], f"{len(repeats)} settled clauses were re-evaluated"

    def test_a_skipped_clause_is_still_contradicted(self) -> None:
        """Emptying a package's range must retire the stamps on its clauses.

        ``b`` has no versions at all, so propagation excludes every version of
        it while clauses naming ``b`` already carry a stamp.
        """
        provider = DictProvider(
            {"a": {1: {"b": Range.full()}, 2: {"b": Range.at_least(1)}}, "b": {}}
        )
        resolver = Resolver(provider)
        auditor = StampAuditor(resolver)
        resolver.observer = auditor

        with pytest.raises(ResolutionError):
            resolver.resolve({"a": Range.full()})

        assert auditor.stale == []

    def test_an_emptied_range_retires_stamps_within_one_propagation(self) -> None:
        """A range emptied mid-propagation retires the stamps taken before it.

        ``p`` carries only exclusions, so one more exclusion empties it and the
        clause stamped earlier in the same pass stops being contradicted.  No
        rollback happens in between, so propagation has to notice within the
        call and derive from that clause after all.
        """
        setup_cause = Incompatibility(
            [Term("p", Range.at_least(5))], cause=IncompatibilityCause.NO_VERSIONS
        )
        stamped = Incompatibility(
            [Term("x", Range.singleton(1)), Term("p", Range.at_least(10))],
            cause=IncompatibilityCause.DERIVED,
        )
        empties_p = Incompatibility(
            [Term("x", Range.singleton(1)), Term("p", Range.less_than(5))],
            cause=IncompatibilityCause.DERIVED,
        )

        resolver: Resolver[str, int] = Resolver(DictProvider({}))
        solution = resolver.solution
        solution.derive("p", Range.at_least(5), positive=False, cause=setup_cause)
        solution.decide("x", 1)

        add_incompatibility(resolver, stamped)
        add_incompatibility(resolver, empties_p)

        assert propagate.unit_propagation(resolver, "x") is None

        causes = [entry.cause for entry in solution.assignments_for("p")]
        assert any(cause is stamped for cause in causes)

    def test_a_second_resolve_starts_from_clean_stamps(self) -> None:
        """A finished resolve's stamps must not survive into the next one.

        The epoch restarts at zero, so a leftover stamp would read as current
        against a clause list it no longer lines up with.
        """
        provider = DictProvider(
            {
                "a": {1: {"b": Range.singleton(1)}, 2: {"b": Range.singleton(2)}},
                "b": {1: {}, 2: {}},
                "c": {1: {"b": Range.singleton(9)}},
            }
        )
        resolver = Resolver(provider)
        assert resolver.resolve({"a": Range.full()}) == {"a": 2, "b": 2}

        with pytest.raises(ResolutionError):
            resolver.resolve({"c": Range.full()})

        assert len(resolver.clause_contradicted_at) == len(resolver.incompatibilities)

    def test_a_restart_continues_the_contradiction_epoch(self) -> None:
        """A restart drops the whole trail, so stamps taken before it go stale."""
        resolver: Resolver[str, int] = Resolver(DictProvider({"a": {1: {}}}))
        resolver.solution.backtrack(0)
        before = resolver.solution.contradiction_epoch
        resolver.stats.package_conflict_counts["a"] = 5

        _, _, restarted = maybe_restart(resolver, 5, 3)

        assert restarted
        assert resolver.solution.contradiction_epoch > before


class TestResolverStats:
    def test_a_fresh_bag_starts_at_zero_with_its_own_counters(self) -> None:
        """The two count maps are per-instance, not shared class state."""
        stats: ResolverStats[str] = ResolverStats()
        other: ResolverStats[str] = ResolverStats()

        assert stats.rounds == 0
        assert stats.incompatibilities_learned == 0
        assert stats.package_conflict_counts == {}
        assert stats.package_culprit_counts == {}

        stats.package_conflict_counts["a"] += 1
        assert other.package_conflict_counts == {}

    def test_counters_can_be_supplied(self) -> None:
        stats: ResolverStats[str] = ResolverStats(
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            defaultdict(int, {"a": 1}),
            defaultdict(int, {"b": 2}),
        )

        assert (stats.rounds, stats.decisions, stats.conflicts) == (1, 2, 3)
        assert (stats.derivations, stats.backjumps, stats.restarts) == (4, 5, 6)
        assert (stats.targeted_backtracks, stats.incompatibilities_learned) == (7, 8)
        assert stats.package_conflict_counts == {"a": 1}
        assert stats.package_culprit_counts == {"b": 2}

    def test_equality_covers_every_counter_and_declines_other_types(self) -> None:
        """Vary one counter at a time, so none can drop out of __eq__."""
        counters: dict[str, Any] = {
            "rounds": 1,
            "decisions": 2,
            "conflicts": 3,
            "derivations": 4,
            "backjumps": 5,
            "restarts": 6,
            "targeted_backtracks": 7,
            "incompatibilities_learned": 8,
            "package_conflict_counts": defaultdict(int, {"a": 1}),
            "package_culprit_counts": defaultdict(int, {"b": 2}),
        }
        assert tuple(sorted(counters)) == ResolverStats.__slots__

        stats: ResolverStats[str] = ResolverStats(**counters)

        assert stats == ResolverStats(**counters)
        for name, value in counters.items():
            other = value + 1 if isinstance(value, int) else defaultdict(int, {"z": 9})
            assert stats != ResolverStats(**{**counters, name: other}), name

        assert stats.__eq__("stats") is NotImplemented

    def test_a_mutable_bag_of_counters_is_unhashable(self) -> None:
        assert ResolverStats.__hash__ is None

    def test_a_bag_of_counters_carries_no_instance_dict(self) -> None:
        """The ten slots are the whole layout."""
        stats: ResolverStats[str] = ResolverStats()

        with pytest.raises(AttributeError):
            _ = stats.__dict__

    def test_pattern_matching_reads_every_counter_positionally(self) -> None:
        """Ten sub-patterns, in declaration order rather than slot order."""
        stats: ResolverStats[str] = ResolverStats(rounds=2, conflicts=5)

        match stats:
            case ResolverStats(
                rounds,
                decisions,
                conflicts,
                derivations,
                backjumps,
                restarts,
                targeted_backtracks,
                learned,
                conflict_counts,
                culprit_counts,
            ):
                assert (rounds, decisions, conflicts) == (2, 0, 5)
                assert (derivations, backjumps, restarts) == (0, 0, 0)
                assert (targeted_backtracks, learned) == (0, 0)
                assert (conflict_counts, culprit_counts) == ({}, {})

    def test_repr_names_the_class_and_every_counter(self) -> None:
        assert repr(ResolverStats(rounds=2)) == (
            "ResolverStats(rounds=2, decisions=0, conflicts=0, derivations=0,"
            " backjumps=0, restarts=0, targeted_backtracks=0,"
            " incompatibilities_learned=0,"
            " package_conflict_counts=defaultdict(<class 'int'>, {}),"
            " package_culprit_counts=defaultdict(<class 'int'>, {}))"
        )
