"""Tests for the PubGrub resolver: unit propagation, conflict resolution,
and end-to-end resolution with a simple in-memory provider."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from nab_resolver.conflict import (
    apply_targeted_backtrack,
    conflict_resolution,
    is_terminal_incompatibility,
    recompute_previous_level,
    try_force_resolution_step,
)
from nab_resolver.incompat_index import (
    add_incompatibility,
    dependency_merge_key,
    maybe_merge_dependency,
)
from nab_resolver.partial_solution import PartialSolution
from nab_resolver.propagate import evaluate_incompatibility, unit_propagation
from nab_resolver.ranges import Range
from nab_resolver.report import explain_incompatibility, prior_cause, union_terms
from nab_resolver.resolver import (
    ResolutionError,
    Resolver,
    ResolverObserver,
)
from nab_resolver.root import ROOT
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    IncompatibilityState,
    RangeProtocol,
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

    def get_dependencies(self, package: str, version: int) -> dict[str, Range]:
        return self._packages.get(package, {}).get(version, {})

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


class PromotingProvider(DictProvider):
    """DictProvider that promotes packages with 5+ conflicts."""

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
    def test_exceeds_max_iterations(self) -> None:
        """Resolver raises when max_iterations is exceeded."""
        # Create a scenario that needs many rounds
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

    def testunion_terms_positive_covers_all_returns_none(self) -> None:
        """Positive union of Range.full() is universal, returns None."""
        a = Term("foo", Range.full(), positive=True)
        b = Term("foo", Range.singleton(1), positive=True)
        assert union_terms(a, b) is None

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

    def testprior_cause_shared_package_union_drops_universal(self) -> None:
        """When the shared package's terms union to any(), it's dropped."""
        inc1 = Incompatibility(
            [Term("foo", Range.singleton(1)), Term("bar", Range.at_least(2))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        inc2 = Incompatibility(
            [Term("bar", Range.less_than(2)), Term("baz", Range.singleton(3))],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        result = prior_cause(inc1, inc2, "bar")
        packages = {t.package for t in result}
        # bar terms union to any(), so bar is dropped
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

    def test_effective_range_before_mixed(self) -> None:
        """_effective_range_before accumulates multiple positives and a negative."""
        solution = PartialSolution()
        cause = Incompatibility(
            [Term("x", Range.full())], cause=IncompatibilityCause.ROOT
        )
        # Two positive derivations: [1,10) then [2,10)
        solution.derive("x", Range.between(1, 10), positive=True, cause=cause)
        solution.derive("x", Range.at_least(2), positive=True, cause=cause)
        # One negative derivation excludes x==5
        solution.derive("x", Range.singleton(5), positive=False, cause=cause)
        # Final derivation as the "satisfier"
        solution.derive("x", Range.less_than(4), positive=True, cause=cause)

        satisfier = solution.assignments_for("x")[-1]
        # Effective before satisfier: [1,10) & [2,inf) & ~{5} = [2,5) | (5,10)
        # Use a term that IS satisfied by [2,5) | (5,10): e.g. [2,10) minus {5}
        term = Term("x", Range.between(2, 5))
        # [2,5) | (5,10) is a subset of [2,5)? No, (5,10) is outside.
        # Use a wider term: [2,10)
        term = Term("x", Range.between(2, 10))
        # Is [2,5) | (5,10) a subset of [2,10)?
        # [2,5) is in [2,10). (5,10) is in [2,10). Yes.
        assert not solution.satisfier_is_sole(satisfier, term)

    def test_recompute_previous_level_no_remainder_satisfier(self) -> None:
        """If satisfier(remainder) is None, previous_level is unchanged."""
        resolver = Resolver(DictProvider({}))
        cause = Incompatibility(
            [Term("x", Range.at_least(3), positive=False)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution = PartialSolution()
        solution.derive("x", Range.less_than(3), positive=True, cause=cause)

        # Force satisfier() to return None to exercise the defensive
        # branch in recompute_previous_level. In normal resolution this
        # branch is unreachable because the partial solution always
        # contains an assignment that satisfies the remainder.
        solution.satisfier = lambda _term: None  # type: ignore[method-assign]

        term = Term("x", Range.between(1, 5))
        assignment = solution.assignments_for("x")[-1]
        resolver.solution = solution
        result = recompute_previous_level(resolver, assignment, term, 7)
        assert result == 7

    def test_recompute_previous_level_with_remainder(self) -> None:
        """recompute_previous_level finds the remainder satisfier."""
        resolver = Resolver(DictProvider({}))
        # Set up a solution where the satisfier's cause contributes
        # only part of the term's range, leaving a non-empty remainder
        # that was satisfied by an earlier assignment.
        cause = Incompatibility(
            [Term("x", Range.at_least(3), positive=False)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        solution = PartialSolution()
        # Level 1: narrow x to [1, 5)
        root_cause = Incompatibility(
            [Term("x", Range.full())], cause=IncompatibilityCause.ROOT
        )
        solution.derive("x", Range.between(1, 5), positive=True, cause=root_cause)
        solution.decide("pkg", 1)
        # Level 2: narrow x further to [1, 3) via cause that says "not x >= 3"
        solution.derive("x", Range.less_than(3), positive=True, cause=cause)
        # The satisfier is the level-2 derivation.  Its individual
        # contribution is less_than(3).  The term is between(1,3).
        # Remainder = between(1,3) intersect negate(less_than(3)) = between(1,3) & at_least(3) = empty.
        # Since remainder is empty, previous_level stays unchanged.
        # But let's use a term where remainder is non-empty:
        term = Term("x", Range.between(1, 5))
        assignment = solution.assignments_for("x")[-1]
        # individual from cause: negate of neg-term for x = pos(at_least(3))
        # negate of individual = neg(at_least(3))
        # remainder = term.intersect(neg(at_least(3))) = between(1,5) & less_than(3) = between(1,3)
        # between(1,3) is non-empty, so it looks for satisfier of between(1,3)
        resolver.solution = solution
        result = recompute_previous_level(resolver, assignment, term, 1)
        # The remainder [1,3) was satisfied at level 0 (before the
        # decision at level 1) by the derivation of [1,5).
        # max(1, 0) = 1.
        assert result == 1


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


class TestRestart:
    """Verify the resolver restarts when a package causes many conflicts."""

    def test_restart_reduces_decisions(self) -> None:
        """Restart with conflict-driven promotion avoids re-deciding
        downstream packages on every backtrack.

        root -> a (any), b (any)
        a has versions 10..1, each requiring b >= v (so a@10 -> b>=10, etc.)
        b only has version 1.
        Only a@1 is compatible (b>=1 satisfied by b@1).

        Without restart: resolver decides b first (fewer versions),
        then tries a@10, conflict, backtracks, re-decides b, tries a@9, etc.
        With restart: after 5 conflicts, a is promoted, decided first,
        and b is only decided once at the end.
        """
        # Provider that promotes high-conflict packages
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
        # With restart + promotion, fewer decisions than without
        assert resolver.stats.decisions < 30

    def test_restarts_are_bounded(self) -> None:
        """Resolver stops restarting after _MAX_RESTARTS."""
        # 50 versions of "a", each requiring b >= v. Only a@1 works.
        # With threshold=5 and max_restarts=3, restarts fire at
        # conflicts 5, 10, 20. After 3 restarts (exhausting the
        # budget), resolution continues without further restarts.
        a_versions = {}
        for v in range(50, 0, -1):
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
        # Force-backtrack target should have been queued at least once.
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
        # One stored clause; package term covers both versions.
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
        # The new clause is fully subsumed by the existing range.
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
        # The error should have a derived incompatibility with causes
        root_inc = exc_info.value.incompatibility
        assert root_inc is not None


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


class TestContradictedCache:
    """The contradicted-incompatibility skip cache (card 026)."""

    def _decided(self, package: str, version: int) -> PartialSolution:
        solution: PartialSolution = PartialSolution()
        root = Incompatibility(
            [Term(package, Range.full())], cause=IncompatibilityCause.ROOT
        )
        solution.derive(package, Range.at_least(1), positive=True, cause=root)
        solution.decide(package, version)
        return solution

    def test_evaluate_returns_contradicted(self) -> None:
        """A positive term whose range excludes the decision is contradicted."""
        resolver = Resolver(DictProvider({}))
        resolver.solution = self._decided("x", 2)
        incompatibility = Incompatibility(
            [Term("x", Range.singleton(5), positive=True)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert (
            evaluate_incompatibility(resolver, incompatibility)
            is IncompatibilityState.CONTRADICTED
        )

    def test_evaluate_two_undetermined_is_not_cacheable(self) -> None:
        """Two undetermined terms return None, not the contradicted sentinel."""
        resolver = Resolver(DictProvider({}))
        incompatibility = Incompatibility(
            [
                Term("a", Range.full(), positive=True),
                Term("b", Range.full(), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert evaluate_incompatibility(resolver, incompatibility) is None

    def test_propagation_records_contradicted_index(self) -> None:
        """Unit propagation caches a contradicted clause at the current level."""
        resolver = Resolver(DictProvider({}))
        resolver.solution = self._decided("x", 2)
        add_incompatibility(
            resolver,
            Incompatibility(
                [Term("x", Range.singleton(5), positive=True)],
                cause=IncompatibilityCause.DEPENDENCY,
            ),
        )
        assert unit_propagation(resolver, "x") is None
        assert resolver.contradicted_at == {0: resolver.solution.decision_level}

    def test_propagation_skips_cached_index(self) -> None:
        """A cached index is skipped even when it would otherwise conflict."""
        resolver = Resolver(DictProvider({}))
        resolver.solution = self._decided("x", 2)
        add_incompatibility(
            resolver,
            Incompatibility(
                [Term("x", Range.singleton(2), positive=True)],
                cause=IncompatibilityCause.DEPENDENCY,
            ),
        )
        resolver.contradicted_at[0] = 0
        assert unit_propagation(resolver, "x") is None

    def test_prune_keeps_low_levels_drops_high(self) -> None:
        resolver = Resolver(DictProvider({}))
        resolver.contradicted_at = {1: 2, 2: 4, 3: 5}
        resolver.prune_contradicted(4)
        assert resolver.contradicted_at == {1: 2, 2: 4}

    def test_merge_evicts_widened_index(self) -> None:
        """Widening a clause on merge drops its cached contradiction."""
        resolver = Resolver(DictProvider({}))
        dep = Term("d", Range.singleton(9), positive=False)
        add_incompatibility(
            resolver,
            Incompatibility(
                [Term("p", Range.singleton(1), positive=True), dep],
                cause=IncompatibilityCause.DEPENDENCY,
            ),
        )
        resolver.contradicted_at[0] = 1
        add_incompatibility(
            resolver,
            Incompatibility(
                [Term("p", Range.singleton(2), positive=True), dep],
                cause=IncompatibilityCause.DEPENDENCY,
            ),
        )
        assert 0 not in resolver.contradicted_at
