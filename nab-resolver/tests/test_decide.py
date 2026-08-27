"""Unit tests for :mod:`nab_resolver.decide`.

``absorb_redundant_requirement`` re-derives a requirement that is redundant on
a package's version set yet still changes its range. Packaging's pre-release
opt-in is one such refinement. Here a range double carries a plain passenger
flag that rides set algebra, so the contract can be exercised without any
PEP 440 semantics.

``choose_package_to_decide`` calls ``begin_decision_scan`` once before it reads
any sort key, which is how a provider fed by another thread knows when its
answers may move.  The same scan hands the provider both search counters, one
per parameter.

``choose_version`` skips the partial-solution hint, and the two snapshots it
carries, when the provider inherits ``BaseProvider``'s no-op.  The provider
classes below are the shapes that check has to tell apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest

from nab_resolver import decide
from nab_resolver.partial_solution import PartialSolution
from nab_resolver.ranges import Range
from nab_resolver.resolver import (
    BaseProvider,
    Resolver,
    ResolverObserver,
    ResolverProvider,
)
from nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    Term,
)


class RefiningRange(Range[int]):
    """A Range with a passenger flag that rides intersection and union.

    The flag is invisible to the version-set predicates (membership,
    ``is_subset``) but counts toward equality, so two ranges over the same
    versions can compare unequal. Intersecting or uniting keeps the flag when
    either side carries it.
    """

    __slots__ = ("_flag",)

    def __init__(self, intervals: tuple[Any, ...] = (), *, flag: bool = False) -> None:
        super().__init__(intervals)
        self._flag = flag

    @property
    def flag(self) -> bool:
        return self._flag

    def __and__(self, other: object) -> RefiningRange:
        """Intersect, keeping the flag if either side carries it."""
        base = super().__and__(other)
        if base is NotImplemented:
            return NotImplemented
        return RefiningRange(base._intervals, flag=self._flag or _flag_of(other))

    def __or__(self, other: object) -> RefiningRange:
        """Unite, keeping the flag if either side carries it."""
        base = super().__or__(other)
        if base is NotImplemented:
            return NotImplemented
        return RefiningRange(base._intervals, flag=self._flag or _flag_of(other))

    def __eq__(self, other: object) -> bool:
        """Equal when the version sets match and the flags agree."""
        if not isinstance(other, Range):
            return NotImplemented
        return self._intervals == other._intervals and self._flag == _flag_of(other)

    def __hash__(self) -> int:
        """Hash the version set together with the flag."""
        return hash((self._intervals, self._flag))


def _flag_of(range_: object) -> bool:
    return isinstance(range_, RefiningRange) and range_.flag


def _flagged(base: Range[int]) -> Range[int]:
    """Return a copy of ``base`` carrying the passenger flag."""
    return RefiningRange(base._intervals, flag=True)


class _RecordingObserver(ResolverObserver[Any, int]):
    """Records the packages that receive a derivation."""

    def __init__(self) -> None:
        self.derived: list[Any] = []

    def on_derivation(
        self, package: Any, *, positive: bool, cause: Incompatibility[Any, int]
    ) -> None:
        self.derived.append(package)


class _InertProvider:
    """Provider stub; these tests drive ``decide`` directly, never ``resolve``."""

    def choose_version(
        self, package: Any, version_range: RangeProtocol[int]
    ) -> int | None:
        return None

    def has_satisfying_version(
        self, package: Any, version_range: RangeProtocol[int]
    ) -> bool:
        return False

    def get_dependencies(
        self, package: Any, version: int
    ) -> Mapping[Any, RangeProtocol[int]]:
        return {}

    def begin_decision_scan(self) -> None:
        pass

    def prioritize(
        self,
        package: Any,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[Any, int],
        culprit_counts: Mapping[Any, int] | None = None,
    ) -> Any:
        return 0

    def is_ready(self, package: Any) -> bool:
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[Any, RangeProtocol[int]],
        decisions: Mapping[Any, int],
    ) -> None:
        pass

    def consume_pending_clauses(self) -> list[Incompatibility[Any, int]]:
        return []

    def consume_force_backtrack_targets(self) -> list[Any]:
        return []

    def widen_decision(self, package: Any, version: int) -> RangeProtocol[int] | None:
        return None

    def narrow_for_display(
        self, package: Any, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        return constraint


class _CounterRecordingProvider(_InertProvider):
    """Records the two counter mappings each sort key is built from.

    Copies each one, and records an omitted ``culprit_counts`` as ``None``
    so a dropped argument does not read back as an empty mapping.
    """

    def __init__(self) -> None:
        self.counters: list[tuple[dict[Any, int], dict[Any, int] | None]] = []

    def prioritize(
        self,
        package: Any,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[Any, int],
        culprit_counts: Mapping[Any, int] | None = None,
    ) -> Any:
        self.counters.append(
            (
                dict(conflict_counts),
                None if culprit_counts is None else dict(culprit_counts),
            )
        )
        return 0


class _ScanOrderProvider(_InertProvider):
    """Records the scan-boundary and sort-key calls in the order they arrive."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def begin_decision_scan(self) -> None:
        self.calls.append("begin")

    def prioritize(
        self,
        package: Any,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[Any, int],
        culprit_counts: Mapping[Any, int] | None = None,
    ) -> Any:
        self.calls.append(f"prioritize {package}")
        return 0

    def is_ready(self, package: Any) -> bool:
        self.calls.append(f"is_ready {package}")
        return True


def _resolver_with(
    provider: ResolverProvider[Any, int],
) -> tuple[Resolver[Any, int], _RecordingObserver]:
    observer = _RecordingObserver()
    # RefiningRange is a valid range at runtime; the cast satisfies the
    # Self-typed RangeProtocol that a plain subclass cannot express.
    resolver = Resolver(
        provider,
        observer=observer,
        range_type=cast("type[RangeProtocol[int]]", RefiningRange),
    )
    return resolver, observer


def _resolver() -> tuple[Resolver[Any, int], _RecordingObserver]:
    return _resolver_with(_InertProvider())


def _dependency_cause() -> Incompatibility[Any, int]:
    return Incompatibility(
        [Term("a", RefiningRange.singleton(1), positive=True)],
        cause=IncompatibilityCause.DEPENDENCY,
    )


class TestAbsorbRedundantRequirement:
    """A version-set-redundant requirement is re-derived onto a package only
    when it still refines the range, and the refinement rides the parent
    decision level.
    """

    def test_first_seen_dependency_is_left_to_propagation(self) -> None:
        """A dependency with no accumulated range yet gains no derivation."""
        resolver, observer = _resolver()

        decide.absorb_redundant_requirement(
            resolver, "c", RefiningRange.at_least(1), _dependency_cause()
        )

        assert resolver.solution.positive_range("c") is None
        assert resolver.stats.derivations == 0
        assert observer.derived == []

    def test_unchanged_range_gains_no_derivation(self) -> None:
        """A requirement that leaves the range identical adds no derivation."""
        resolver, observer = _resolver()
        resolver.solution.derive(
            "c", RefiningRange.between(1, 3), positive=True, cause=_dependency_cause()
        )

        decide.absorb_redundant_requirement(
            resolver, "c", RefiningRange.at_least(1), _dependency_cause()
        )

        assert resolver.stats.derivations == 0
        assert observer.derived == []

    def test_narrowing_requirement_is_left_to_propagation(self) -> None:
        """A requirement that narrows the version set adds no derivation."""
        resolver, observer = _resolver()
        resolver.solution.derive(
            "c", RefiningRange.at_least(1), positive=True, cause=_dependency_cause()
        )

        decide.absorb_redundant_requirement(
            resolver, "c", RefiningRange.between(1, 3), _dependency_cause()
        )

        assert resolver.stats.derivations == 0
        assert observer.derived == []

    def test_redundant_refinement_reaches_the_package(self) -> None:
        """A refinement redundant on the version set is derived onto the range."""
        resolver, observer = _resolver()
        resolver.solution.derive(
            "c", RefiningRange.singleton(1), positive=True, cause=_dependency_cause()
        )

        requirement = _flagged(RefiningRange.at_least(1))
        decide.absorb_redundant_requirement(
            resolver, "c", requirement, _dependency_cause()
        )

        refined = resolver.solution.positive_range("c")
        assert isinstance(refined, RefiningRange)
        assert refined.flag is True
        assert 1 in refined
        assert resolver.stats.derivations == 1
        assert observer.derived == ["c"]

    def test_refinement_is_undone_on_backtracking(self) -> None:
        """The refinement rides the parent decision level, so backtracking lifts it."""
        resolver, _ = _resolver()
        resolver.solution.decide("root", 1)
        resolver.solution.derive(
            "c", RefiningRange.singleton(1), positive=True, cause=_dependency_cause()
        )
        resolver.solution.decide("a", 1)

        requirement = _flagged(RefiningRange.at_least(1))
        decide.absorb_redundant_requirement(
            resolver, "c", requirement, _dependency_cause()
        )
        refined = resolver.solution.positive_range("c")
        assert isinstance(refined, RefiningRange)
        assert refined.flag is True

        resolver.solution.backtrack(1)
        reverted = resolver.solution.positive_range("c")
        assert isinstance(reverted, RefiningRange)
        assert reverted.flag is False


class TestScanBoundary:
    """One ``begin_decision_scan`` opens each scan, ahead of every key read."""

    def test_scan_opens_once_before_any_key_is_read(self) -> None:
        """The boundary call comes first and does not repeat inside the scan."""
        provider = _ScanOrderProvider()
        resolver, _ = _resolver_with(provider)
        for package in ("a", "b", "c"):
            resolver.solution.derive(
                package,
                RefiningRange.at_least(1),
                positive=True,
                cause=_dependency_cause(),
            )

        assert decide.choose_package_to_decide(resolver) is not None
        assert provider.calls[0] == "begin"
        assert provider.calls.count("begin") == 1
        assert len(provider.calls) == 1 + 2 * 3

    def test_each_scan_opens_a_new_one(self) -> None:
        """A second scan gets its own boundary call."""
        provider = _ScanOrderProvider()
        resolver, _ = _resolver_with(provider)
        resolver.solution.derive(
            "a", RefiningRange.at_least(1), positive=True, cause=_dependency_cause()
        )

        decide.choose_package_to_decide(resolver)
        decide.choose_package_to_decide(resolver)

        assert provider.calls.count("begin") == 2

    def test_nothing_undecided_opens_no_scan(self) -> None:
        """With every package decided the provider is left alone."""
        provider = _ScanOrderProvider()
        resolver, _ = _resolver_with(provider)

        assert decide.choose_package_to_decide(resolver) is None
        assert provider.calls == []


class TestBothCountersReachTheProvider:
    """Every sort key is built from both of the resolver's search counters.

    The two mean opposite things: how often a decision on the package was
    discarded, against how often the package discarded another's.  They are
    passed positionally, so nothing else pins which parameter each reaches.
    """

    def test_each_counter_arrives_in_the_parameter_named_for_it(self) -> None:
        """Conflict counts third, culprit counts fourth, once per package."""
        provider = _CounterRecordingProvider()
        resolver, _ = _resolver_with(provider)
        for package in ("a", "b"):
            resolver.solution.derive(
                package,
                RefiningRange.at_least(1),
                positive=True,
                cause=_dependency_cause(),
            )

        resolver.stats.package_conflict_counts["a"] = 3
        resolver.stats.package_culprit_counts["b"] = 7

        assert decide.choose_package_to_decide(resolver) is not None
        assert provider.counters == [({"a": 3}, {"b": 7})] * 2


_GRAPH: Mapping[Any, Mapping[Any, RangeProtocol[int]]] = {
    "a": {"b": RefiningRange.at_least(1)}
}


def _graph_dependencies(package: Any) -> Mapping[Any, RangeProtocol[int]]:
    """Dependencies in the graph the hint tests resolve: ``a`` needs ``b``."""
    return _GRAPH.get(package, {})


class _SnapshotCountingSolution(PartialSolution[Any, Any]):
    """A real partial solution that counts the snapshots it hands out."""

    def __init__(self, range_type: type[RangeProtocol[Any]]) -> None:
        super().__init__(range_type=range_type)
        self.snapshots_taken = 0

    def positive_ranges(self) -> Mapping[Any, RangeProtocol[Any]]:
        self.snapshots_taken += 1
        return super().positive_ranges()

    def decisions(self) -> Mapping[Any, Any]:
        self.snapshots_taken += 1
        return super().decisions()


class _HintRecordingProvider(_InertProvider):
    """Copies each hint it is given, and answers 1 wherever the range allows."""

    def __init__(self) -> None:
        self.hints: list[tuple[dict[Any, RangeProtocol[int]], dict[Any, int]]] = []

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[Any, RangeProtocol[int]],
        decisions: Mapping[Any, int],
    ) -> None:
        self.hints.append((dict(positive_ranges), dict(decisions)))

    def choose_version(
        self, package: Any, version_range: RangeProtocol[int]
    ) -> int | None:
        return 1 if 1 in version_range else None

    def has_satisfying_version(
        self, package: Any, version_range: RangeProtocol[int]
    ) -> bool:
        return 1 in version_range

    def get_dependencies(
        self, package: Any, version: int
    ) -> Mapping[Any, RangeProtocol[int]]:
        return _graph_dependencies(package)


class _InheritedHintProvider(BaseProvider[Any, int]):
    """The five methods ``BaseProvider`` leaves owed, and its no-op hint."""

    def choose_version(
        self, package: Any, version_range: RangeProtocol[int]
    ) -> int | None:
        return 1 if 1 in version_range else None

    def has_satisfying_version(
        self, package: Any, version_range: RangeProtocol[int]
    ) -> bool:
        return 1 in version_range

    def get_dependencies(
        self, package: Any, version: int
    ) -> Mapping[Any, RangeProtocol[int]]:
        return _graph_dependencies(package)

    def prioritize(
        self,
        package: Any,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[Any, int],
        culprit_counts: Mapping[Any, int] | None = None,
    ) -> Any:
        return 0

    def widen_decision(self, package: Any, version: int) -> RangeProtocol[int] | None:
        return None


class _OverridingHintProvider(_InheritedHintProvider):
    """A ``BaseProvider`` subclass that does read the hint."""

    def __init__(self) -> None:
        self.hints: list[tuple[dict[Any, RangeProtocol[int]], dict[Any, int]]] = []

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[Any, RangeProtocol[int]],
        decisions: Mapping[Any, int],
    ) -> None:
        self.hints.append((dict(positive_ranges), dict(decisions)))


class _HooklessProvider:
    """A provider that never grew a ``receive_partial_solution_hint``."""

    def choose_version(
        self, package: Any, version_range: RangeProtocol[int]
    ) -> int | None:
        return 1 if 1 in version_range else None


class _ProviderSwappingObserver(ResolverObserver[Any, int]):
    """Hands the resolver a different provider as soon as a decision lands."""

    def __init__(self, replacement: ResolverProvider[Any, int]) -> None:
        self.replacement = replacement
        self.resolver: Resolver[Any, int] | None = None

    def on_decision(self, package: Any, version: int, level: int) -> None:
        assert self.resolver is not None
        self.resolver.provider = self.replacement


def _counting_resolver(
    provider: ResolverProvider[Any, int],
) -> tuple[Resolver[Any, int], _SnapshotCountingSolution]:
    """A resolver whose solution counts snapshots, ``a`` decided, ``b`` derived."""
    resolver, _ = _resolver_with(provider)
    solution = _SnapshotCountingSolution(resolver.range_type)
    resolver.solution = solution

    solution.decide("a", 2)
    solution.derive(
        "b", RefiningRange.at_least(1), positive=True, cause=_dependency_cause()
    )
    solution.snapshots_taken = 0
    return resolver, solution


class TestThePartialSolutionHint:
    """Each provider shape that might read the hint is given it.

    The two snapshots ``choose_version`` passes are what the hook costs, so a
    provider inheriting the no-op must not make the solution build them.
    """

    def test_a_structural_implementer_is_given_both_maps(self) -> None:
        """Both maps arrive as they stand: a range each, a decision only for ``a``."""
        provider = _HintRecordingProvider()
        resolver, solution = _counting_resolver(provider)

        assert decide.choose_version(resolver, "b") == 1

        assert len(provider.hints) == 1
        positive_ranges, decisions = provider.hints[0]
        assert set(positive_ranges) == {"a", "b"}
        assert decisions == {"a": 2}
        assert solution.snapshots_taken == 2

    def test_an_overriding_subclass_is_given_the_hint(self) -> None:
        """Inheriting ``BaseProvider`` does not disqualify an override."""
        provider = _OverridingHintProvider()
        resolver, solution = _counting_resolver(provider)

        assert decide.choose_version(resolver, "b") == 1

        assert len(provider.hints) == 1
        assert solution.snapshots_taken == 2

    def test_an_instance_attribute_override_is_given_the_hint(self) -> None:
        """A hook installed on the instance is not the inherited no-op."""
        hints: list[tuple[Mapping[Any, RangeProtocol[int]], Mapping[Any, int]]] = []

        def record(
            positive_ranges: Mapping[Any, RangeProtocol[int]],
            decisions: Mapping[Any, int],
        ) -> None:
            hints.append((positive_ranges, decisions))

        provider = _InheritedHintProvider()
        provider.receive_partial_solution_hint = record

        resolver, solution = _counting_resolver(provider)

        assert decide.choose_version(resolver, "b") == 1

        assert len(hints) == 1
        assert solution.snapshots_taken == 2

    def test_the_inherited_no_op_costs_no_snapshot(self) -> None:
        """Nothing can read the hint, so the solution is never asked for one."""
        resolver, solution = _counting_resolver(_InheritedHintProvider())

        assert decide.choose_version(resolver, "b") == 1

        assert solution.snapshots_taken == 0

    def test_a_provider_missing_the_hook_raises_at_the_call(self) -> None:
        """Construction does not read the method, so the decision is where it fails."""
        provider = cast("ResolverProvider[Any, int]", _HooklessProvider())
        resolver, _ = _counting_resolver(provider)

        with pytest.raises(AttributeError, match="receive_partial_solution_hint"):
            decide.choose_version(resolver, "b")

    def test_a_provider_swapped_mid_resolve_is_given_the_hint(self) -> None:
        """The provider is read per decision, so a swap lands on the new one."""
        recorder = _HintRecordingProvider()
        observer = _ProviderSwappingObserver(recorder)
        resolver = Resolver(
            _InheritedHintProvider(),
            observer=observer,
            range_type=cast("type[RangeProtocol[int]]", RefiningRange),
        )
        observer.resolver = resolver

        assert resolver.resolve({"a": RefiningRange.at_least(1)}) == {"a": 1, "b": 1}

        assert len(recorder.hints) == 1

    def test_a_hint_installed_between_resolves_is_honoured(self) -> None:
        """The answer is re-asked per resolve, not kept from the one before."""
        hints: list[tuple[Mapping[Any, RangeProtocol[int]], Mapping[Any, int]]] = []

        def record(
            positive_ranges: Mapping[Any, RangeProtocol[int]],
            decisions: Mapping[Any, int],
        ) -> None:
            hints.append((positive_ranges, decisions))

        provider = _InheritedHintProvider()
        resolver, _ = _resolver_with(provider)
        requirements = {"a": RefiningRange.at_least(1)}

        assert resolver.resolve(requirements) == {"a": 1, "b": 1}
        assert hints == []

        provider.receive_partial_solution_hint = record

        assert resolver.resolve(requirements) == {"a": 1, "b": 1}
        assert len(hints) == 2

    def test_a_whole_resolve_takes_one_snapshot_for_the_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one snapshot a whole resolve takes is the result being built."""
        monkeypatch.setattr(
            "nab_resolver.resolver.PartialSolution", _SnapshotCountingSolution
        )
        resolver, _ = _resolver_with(_InheritedHintProvider())

        assert resolver.resolve({"a": RefiningRange.at_least(1)}) == {"a": 1, "b": 1}

        solution = cast("_SnapshotCountingSolution", resolver.solution)
        assert resolver.stats.decisions == 3
        assert solution.snapshots_taken == 1
