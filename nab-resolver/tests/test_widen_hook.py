"""Tests for the decision-widening provider hooks.

``widen_decision`` lets a provider replace the exact singleton parent term
of a cross-package dependency clause with a wider range in which every
selectable version has exactly the dependencies being recorded: adjacent
clauses merge contiguously instead of one hole per rejected version, and
one clause can reject a whole run of same-dependency versions.
``narrow_for_display`` maps possibly-widened constraints back onto known
versions at error-render time.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from nab_resolver.conflict import conflict_credit_target
from nab_resolver.partial_solution import Assignment
from nab_resolver.ranges import Range
from nab_resolver.report import format_error
from nab_resolver.resolver import ResolutionError, Resolver
from nab_resolver.root import ROOT
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    Term,
)


class _BaseProvider:
    """In-memory provider mirroring test_resolver's DictProvider."""

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


class _RecordingWidenProvider(_BaseProvider):
    """Records every ``widen_decision`` call and declines to widen."""

    def __init__(self, packages: dict[str, dict[int, dict[str, Range]]]) -> None:
        super().__init__(packages)
        self.widen_calls: list[tuple[object, int]] = []

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        self.widen_calls.append((package, version))
        return None


class _WideningProvider(_BaseProvider):
    """Widens a decided version to the open gap between its listed neighbors."""

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        universe = sorted(self._packages.get(package, {}).keys())
        if version not in universe:
            return None
        index = universe.index(version)
        widened: Range[int] = Range.full()
        if index > 0:
            widened = widened & Range.greater_than(universe[index - 1])
        if index < len(universe) - 1:
            widened = widened & Range.less_than(universe[index + 1])
        return widened


class _SnappingProvider(_WideningProvider):
    """Widens to the neighbor gap and snaps displayed constraints onto the listing."""

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        universe = sorted(self._packages.get(package, {}))
        inside = [version for version in universe if version in constraint]
        if not inside:
            return constraint
        if len(inside) == len(universe):
            return Range.full()
        if len(inside) == 1:
            return Range.singleton(inside[0])
        return Range.between(inside[0], inside[-1] + 1)


class _SpanWideningProvider(_BaseProvider):
    """Widens a decided version across adjacent versions with equal deps."""

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        versions_map = self._packages.get(package, {})
        universe = sorted(versions_map.keys())
        if version not in universe:
            return None
        deps = versions_map[version]
        low = universe.index(version)
        high = low
        while low > 0 and versions_map[universe[low - 1]] == deps:
            low -= 1
        while high < len(universe) - 1 and versions_map[universe[high + 1]] == deps:
            high += 1
        widened: Range[int] = Range.full()
        if low > 0:
            widened = widened & Range.greater_than(universe[low - 1])
        if high < len(universe) - 1:
            widened = widened & Range.less_than(universe[high + 1])
        return widened


class _NarrowingProvider(_BaseProvider):
    """Narrows displayed constraints for one package to a fixed range."""

    def __init__(
        self,
        packages: dict[str, dict[int, dict[str, Range]]],
        target: str,
        narrowed: Range[int],
    ) -> None:
        super().__init__(packages)
        self._target = target
        self._narrowed = narrowed
        self.narrow_calls: list[object] = []

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        self.narrow_calls.append(package)
        if package == self._target:
            return self._narrowed
        return constraint


class TestWidenDecisionNoneIsToday:
    """The default hook (returning None) must not change behavior."""

    def test_multi_package_backtracking_scenario_unchanged(self) -> None:
        """Deep-backtracking graph: same result and same decision count as
        the exact-singleton resolver produces (captured before the hook
        existed: result {root: 1, a: 1, c: 2}, 9 decisions)."""
        provider = _BaseProvider(
            {
                "root": {1: {"a": Range.full(), "c": Range.full()}},
                "a": {2: {"b": Range.at_least(2)}, 1: {}},
                "b": {2: {"c": Range.at_least(3)}, 1: {}},
                "c": {2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        result = resolver.resolve({"root": Range.singleton(1)})
        assert result == {"root": 1, "a": 1, "c": 2}
        assert resolver.stats.decisions == 9


class TestWideningMergesDependencyClauses:
    def test_widened_clauses_merge_into_single_interval(self) -> None:
        """Three rejected versions of ``a`` sharing one dep constraint must
        leave a single-interval merged DEPENDENCY clause, not a 3-hole union.
        """
        provider = _WideningProvider(
            {
                "root": {1: {"a": Range.full()}},
                "a": {
                    3: {"b": Range.singleton(5)},
                    2: {"b": Range.singleton(5)},
                    1: {"b": Range.singleton(5)},
                },
                "b": {1: {}},
            }
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError):
            resolver.resolve({"root": Range.singleton(1)})

        merged = [
            inc
            for inc in resolver.incompatibilities
            if inc.cause is IncompatibilityCause.DEPENDENCY
            and len(inc.terms) == 2
            and inc.terms[0].package == "a"
            and inc.terms[1].package == "b"
        ]
        assert len(merged) == 1
        constraint = merged[0].terms[0].constraint
        assert isinstance(constraint, Range)
        assert len(constraint._intervals) == 1
        assert 1 in constraint
        assert 2 in constraint
        assert 3 in constraint

    def test_widening_does_not_change_unsat_outcome(self) -> None:
        """The same impossible graph fails with and without widening."""
        packages = {
            "root": {1: {"a": Range.full()}},
            "a": {
                3: {"b": Range.singleton(5)},
                2: {"b": Range.singleton(5)},
                1: {"b": Range.singleton(5)},
            },
            "b": {1: {}},
        }
        with pytest.raises(ResolutionError):
            Resolver(_BaseProvider(packages)).resolve({"root": Range.singleton(1)})
        with pytest.raises(ResolutionError):
            Resolver(_WideningProvider(packages)).resolve({"root": Range.singleton(1)})


class TestSpanWideningContract:
    """A range spanning several same-dependency versions is a valid
    ``widen_decision`` result: one clause rejects the whole run."""

    def test_span_rejects_identical_run_with_one_clause(self) -> None:
        packages = {
            "root": {1: {"a": Range.full()}},
            "a": {
                3: {"b": Range.singleton(5)},
                2: {"b": Range.singleton(5)},
                1: {"b": Range.singleton(5)},
            },
            "b": {1: {}},
        }
        resolver = Resolver(_SpanWideningProvider(packages))
        with pytest.raises(ResolutionError):
            resolver.resolve({"root": Range.singleton(1)})

        dep_clauses = [
            inc
            for inc in resolver.incompatibilities
            if inc.cause is IncompatibilityCause.DEPENDENCY
            and len(inc.terms) == 2
            and inc.terms[0].package == "a"
            and inc.terms[1].package == "b"
        ]
        assert len(dep_clauses) == 1
        constraint = dep_clauses[0].terms[0].constraint
        assert 1 in constraint
        assert 2 in constraint
        assert 3 in constraint

    def test_span_outcome_matches_unwidened(self) -> None:
        packages = {
            "root": {1: {"a": Range.full()}},
            "a": {
                3: {"b": Range.singleton(5)},
                2: {"b": Range.singleton(5)},
                1: {"b": Range.singleton(1)},
            },
            "b": {1: {}},
        }
        base = Resolver(_BaseProvider(packages)).resolve({"root": Range.singleton(1)})
        span_resolver = Resolver(_SpanWideningProvider(packages))
        span = span_resolver.resolve({"root": Range.singleton(1)})
        assert span == base == {"root": 1, "a": 1, "b": 1}


class TestSelfDependencyStaysExact:
    def test_self_dep_clause_keeps_singleton_term(self) -> None:
        """foo@2 depends on foo=={1}: the single-term clause asserts exactly
        "foo is never 2", even when the provider widens decisions."""
        provider = _WideningProvider({"foo": {2: {"foo": Range.singleton(1)}, 1: {}}})
        resolver = Resolver(provider, max_iterations=100)
        result = resolver.resolve({"foo": Range.full()})
        assert result == {"foo": 1}

        self_clauses = [
            inc
            for inc in resolver.incompatibilities
            if inc.cause is IncompatibilityCause.DEPENDENCY
            and len(inc.terms) == 1
            and inc.terms[0].package == "foo"
        ]
        assert len(self_clauses) == 1
        assert self_clauses[0].terms[0].constraint == Range.singleton(2)
        assert self_clauses[0].terms[0].is_positive()

    def test_self_dep_clause_keeps_singleton_with_span_provider(self) -> None:
        """A same-deps run must not leak the spanned range into self-dep
        clauses; they are built resolver-side from the exact singleton."""
        provider = _SpanWideningProvider(
            {
                "foo": {
                    3: {"foo": Range.singleton(1)},
                    2: {"foo": Range.singleton(1)},
                    1: {},
                }
            }
        )
        resolver = Resolver(provider, max_iterations=100)
        result = resolver.resolve({"foo": Range.full()})
        assert result == {"foo": 1}

        self_clauses = [
            inc
            for inc in resolver.incompatibilities
            if inc.cause is IncompatibilityCause.DEPENDENCY
            and len(inc.terms) == 1
            and inc.terms[0].package == "foo"
        ]
        assert {c.terms[0].constraint for c in self_clauses} <= {
            Range.singleton(3),
            Range.singleton(2),
        }
        assert self_clauses
        assert all(c.terms[0].is_positive() for c in self_clauses)


class TestRootIsNeverWidened:
    def test_widen_decision_never_called_with_root_sentinel(self) -> None:
        provider = _RecordingWidenProvider(
            {
                "root": {1: {"foo": Range.full(), "bar": Range.full()}},
                "foo": {1: {"baz": Range.at_least(2)}},
                "bar": {1: {}},
                "baz": {2: {}, 1: {}},
            }
        )
        resolver = Resolver(provider)
        resolver.resolve({"root": Range.singleton(1)})
        assert provider.widen_calls
        assert all(package is not ROOT for package, _ in provider.widen_calls)


class TestFormatErrorNarrow:
    def test_narrow_applies_to_parent_side_not_dependency_side(self) -> None:
        """The positive parent term is narrowed; the originally-negative dep
        term renders as requested even though it is displayed positively."""
        clause = Incompatibility(
            [
                Term("foo", Range.between(1, 4), positive=True),
                Term("bar", Range.at_least(3), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        narrowed_packages: list[object] = []

        def narrow(package: object, constraint: Range[int]) -> Range[int]:
            narrowed_packages.append(package)
            if package == "foo":
                return Range.singleton(2)
            return Range.singleton(9)

        message = format_error(clause, narrow=narrow)
        assert message == "because foo 2 depends on bar [3, +inf)"
        assert narrowed_packages == ["foo"]

    def test_no_versions_term_renders_its_own_range(self) -> None:
        """Narrowing it is what let it stop covering the requirement below."""
        clause = Incompatibility(
            [Term("qux", Range.at_least(5), positive=True)],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        for narrowed in (Range.singleton(7), Range.full()):
            message = format_error(
                clause, narrow=lambda package, constraint, r=narrowed: r
            )
            assert message == "because no versions of qux [5, +inf) are available"

    def test_other_causes_accept_a_narrowing_to_full(self) -> None:
        clause = Incompatibility(
            [Term("a", Range.between(1, 9), positive=True)],
            cause=IncompatibilityCause.DERIVED,
        )
        message = format_error(clause, narrow=lambda package, constraint: Range.full())
        assert message == "so all versions of a"

    def test_narrow_applies_to_derived_positive_terms(self) -> None:
        clause = Incompatibility(
            [
                Term("a", Range.between(1, 9), positive=True),
                Term("b", Range.between(1, 9), positive=False),
            ],
            cause=IncompatibilityCause.DERIVED,
        )

        def narrow(package: object, constraint: Range[int]) -> Range[int]:
            return Range.singleton(3)

        message = format_error(clause, narrow=narrow)
        assert message == "so a 3 and not b [1, 9)"

    def test_raise_site_narrows_through_provider(self) -> None:
        """The resolver passes ``narrow_for_display`` to ``format_error``.

        The availability line and the negative dependency side both render the
        range they hold; only an originally-positive term elsewhere is narrowed.
        """
        provider = _NarrowingProvider(
            {"root": {1: {"foo": Range.full()}}},
            target="foo",
            narrowed=Range.between(5, 7),
        )
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve({"root": Range.singleton(1)})
        message = str(exc_info.value)
        assert "because no versions of foo are available" in message.splitlines()
        assert "because root 1 depends on foo" in message.splitlines()
        assert provider.narrow_calls

    def test_identity_narrow_keeps_message_byte_identical(self) -> None:
        """A provider with the identity ``narrow_for_display`` renders the
        exact message the resolver produced before the hook existed."""
        provider = _BaseProvider({"root": {1: {"foo": Range.full()}}})
        resolver = Resolver(provider)
        with pytest.raises(ResolutionError) as exc_info:
            resolver.resolve({"root": Range.singleton(1)})
        assert str(exc_info.value) == (
            "because no versions of foo are available\n"
            "because root 1 depends on foo\n"
            "so root 1\n"
            "because your project depends on root 1\n"
            "so <root> 1"
        )


class TestConflictCreditTarget:
    """The one conflict credit moves to the depending package when the
    satisfier is a derivation propagated from its dependency clause (the
    situation widened parent terms make common)."""

    def _dependency_clause(self) -> Incompatibility[str, int]:
        return Incompatibility(
            [
                Term("parent", Range.between(1, 3), positive=True),
                Term("child", Range.at_least(2), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )

    def _derivation(
        self, package: str, cause: Incompatibility[str, int]
    ) -> Assignment[str, int]:
        return Assignment(
            package=package,
            accumulated_range=Range.at_least(2),
            decision_level=3,
            is_decision=False,
            cause=cause,
        )

    def test_derivation_from_dependency_clause_targets_parent(self) -> None:
        satisfier = self._derivation("child", self._dependency_clause())
        assert conflict_credit_target(satisfier) == "parent"

    def test_decision_satisfier_targets_itself(self) -> None:
        decision = Assignment(
            package="child",
            accumulated_range=Range.singleton(2),
            decision_level=3,
            is_decision=True,
        )
        assert conflict_credit_target(decision) == "child"

    def test_non_dependency_cause_targets_itself(self) -> None:
        derived: Incompatibility[str, int] = Incompatibility(
            [Term("child", Range.at_least(2), positive=False)],
            cause=IncompatibilityCause.DERIVED,
        )
        assert conflict_credit_target(self._derivation("child", derived)) == "child"

    def test_self_dependency_clause_targets_itself(self) -> None:
        self_dep: Incompatibility[str, int] = Incompatibility(
            [Term("child", Range.singleton(2), positive=True)],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert conflict_credit_target(self._derivation("child", self_dep)) == "child"


_REJECTED_RUN = {
    "a": {
        1: {},
        2: {"py": Range.at_least(10)},
        3: {"py": Range.at_least(11)},
        4: {"py": Range.at_least(12)},
    },
    "py": {9: {}},
}


class TestUnstatedVersions:
    """A line that reaches past its causes states the listing it needs, once."""

    def _message(
        self, packages: dict[str, dict[int, dict[str, Range]]], root: dict[str, Range]
    ) -> str:
        with pytest.raises(ResolutionError) as exc_info:
            Resolver(_SnappingProvider(packages)).resolve(root)
        return str(exc_info.value)

    def test_states_the_gaps_a_widened_run_leaves(self) -> None:
        """Several lines reach past their causes; the listing is stated once."""
        message = self._message(_REJECTED_RUN, {"a": Range.at_least(2)})
        assert message.count("because no versions of a") == 1
        assert "because no versions of a (2, 3) | (3, 4) | (4, +inf) are available" in (
            message
        )

    def test_states_the_gap_a_resolved_requirement_leaves(self) -> None:
        """The requirement runs past the exclusion that resolves it."""
        packages = {"a": {1: {}, 2: {"py": Range.at_least(11)}, 3: {}}, "py": {9: {}}}
        root = {"a": Range.greater_than(1) & Range.less_than(3)}
        message = self._message(packages, root)
        assert "because no versions of a (1, 2) | (2, 3) are available" in message

    def test_states_nothing_when_narrowing_drops_no_version(self) -> None:
        provider = _WideningProvider(_REJECTED_RUN)
        with pytest.raises(ResolutionError) as exc_info:
            Resolver(provider).resolve({"a": Range.at_least(2)})
        incompatibility = exc_info.value.incompatibility
        assert incompatibility is not None
        assert format_error(
            incompatibility, narrow=provider.narrow_for_display
        ) == format_error(incompatibility)

    def test_states_nothing_for_a_derivation_without_causes(self) -> None:
        clause = Incompatibility(
            [Term("a", Range.at_least(2), positive=True)],
            cause=IncompatibilityCause.DERIVED,
        )
        message = format_error(
            clause, narrow=lambda package, constraint: Range.singleton(3)
        )
        assert message == "so a 3"

    def test_states_nothing_for_a_package_the_line_shows_negated(self) -> None:
        cause = Incompatibility(
            [Term("a", Range.singleton(2), positive=True)],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        clause = Incompatibility(
            [Term("a", Range.at_least(1), positive=False)],
            cause=IncompatibilityCause.DERIVED,
            cause_left=cause,
            cause_right=cause,
        )
        message = format_error(clause, narrow=lambda package, constraint: constraint)
        assert message == "because no versions of a 2 are available\nso not a [1, +inf)"
