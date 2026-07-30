"""Unit tests for :mod:`nab_resolver.decide`.

``absorb_redundant_requirement`` re-derives a requirement that is redundant on
a package's version set yet still changes its range. Packaging's pre-release
opt-in is one such refinement. Here a range double carries a plain passenger
flag that rides set algebra, so the contract can be exercised without any
PEP 440 semantics.

``choose_package_to_decide`` calls ``begin_decision_scan`` once before it reads
any sort key, which is how a provider fed by another thread knows when its
answers may move.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from nab_resolver import decide
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver, ResolverObserver
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
    provider: _InertProvider,
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
