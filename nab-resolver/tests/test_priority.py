"""Tests for conflict-driven priority reordering.

The provider receives conflict counts via ``prioritize`` and decides
how to use them. These tests verify the plumbing: conflict counts
reach the provider and a provider that promotes conflicting packages
changes resolution behavior.
"""

from __future__ import annotations

from collections.abc import Mapping

from nab_resolver.ranges import Range
from nab_resolver.resolver import ResolutionError, Resolver
from nab_resolver.types import Incompatibility, RangeProtocol


class _TrackingProvider:
    """Provider that records conflict_counts passed to prioritize."""

    def __init__(self, packages: dict[str, dict[int, dict[str, Range]]]) -> None:
        self._packages = packages
        self.seen_conflict_counts: list[dict[str, int]] = []

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        """Pick the newest version within the allowed range."""
        for version in sorted(self._packages.get(package, {}).keys(), reverse=True):
            if version in version_range:
                return version
        return None

    def get_dependencies(self, package: str, version: int) -> dict[str, Range]:
        """Return dependencies for a specific version."""
        return self._packages.get(package, {}).get(version, {})

    def begin_decision_scan(self) -> None:
        """No-op: nothing in this stub moves under a scan."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Record conflict counts and prioritize by version count."""
        self.seen_conflict_counts.append(dict(conflict_counts))
        versions = list(self._packages.get(package, {}).keys())
        return sum(1 for v in versions if v in version_range)

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return any(v in version_range for v in self._packages.get(package, {}))

    def is_ready(self, package: str) -> bool:
        """All packages are immediately decidable in tests."""
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        """No-op: test provider does not use partial solution state."""

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """No queued clauses for this test provider."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal from this test provider."""
        return []

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """No widening: dependency clauses keep the exact decided version."""
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        """Identity: constraints render as stored."""
        return constraint


class _PromotingProvider:
    """Provider that promotes packages with 3+ conflicts."""

    def __init__(self, packages: dict[str, dict[int, dict[str, Range]]]) -> None:
        self._packages = packages
        self.seen_conflict_counts: list[dict[str, int]] = []

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        """Pick the newest version within the allowed range."""
        for version in sorted(self._packages.get(package, {}).keys(), reverse=True):
            if version in version_range:
                return version
        return None

    def get_dependencies(self, package: str, version: int) -> dict[str, Range]:
        """Return dependencies for a specific version."""
        return self._packages.get(package, {}).get(version, {})

    def begin_decision_scan(self) -> None:
        """No-op: nothing in this stub moves under a scan."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[int, int]:
        """Promoted packages sort first (0 before 1)."""
        self.seen_conflict_counts.append(dict(conflict_counts))
        promoted = 0 if conflict_counts.get(package, 0) >= 3 else 1
        versions = list(self._packages.get(package, {}).keys())
        count = sum(1 for v in versions if v in version_range)
        return (promoted, count)

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        return any(v in version_range for v in self._packages.get(package, {}))

    def is_ready(self, package: str) -> bool:
        """All packages are immediately decidable in tests."""
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[int]],
        decisions: Mapping[str, int],
    ) -> None:
        """No-op: test provider does not use partial solution state."""

    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        """No queued clauses for this test provider."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal from this test provider."""
        return []

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """No widening: dependency clauses keep the exact decided version."""
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[int]
    ) -> RangeProtocol[int]:
        """Identity: constraints render as stored."""
        return constraint


class TestConflictCountsReachProvider:
    def test_provider_receives_conflict_counts(self) -> None:
        """The resolver passes conflict_counts to prioritize."""
        provider = _TrackingProvider(
            {
                "root": {1: {"foo": Range.full()}},
                "foo": {2: {"bar": Range.at_least(2)}, 1: {}},
                "bar": {1: {}},
            }
        )
        resolver = Resolver(provider)
        resolver.resolve({"root": Range.singleton(1)})
        assert len(provider.seen_conflict_counts) > 0

    def test_conflict_counts_accumulate(self) -> None:
        """After conflicts, the counts dict is non-empty."""
        a_deps: dict[str, Range] = {"D": Range.at_least(2)}
        provider = _TrackingProvider(
            {
                "root": {1: {"A": Range.full(), "E": Range.full()}},
                "A": dict.fromkeys(range(10, 0, -1), a_deps),
                "D": {3: {}, 2: {}, 1: {}},
                "E": {1: {"D": Range.less_than(1)}},
            }
        )
        resolver = Resolver(provider)
        try:
            resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            pass
        non_empty = [c for c in provider.seen_conflict_counts if c]
        assert len(non_empty) > 0


class TestPromotingProvider:
    def test_promoting_provider_gets_conflict_info(self) -> None:
        """A provider that promotes conflicting packages receives counts."""
        a_deps: dict[str, Range] = {"D": Range.at_least(2)}
        provider = _PromotingProvider(
            {
                "root": {1: {"A": Range.full(), "E": Range.full()}},
                "A": dict.fromkeys(range(20, 0, -1), a_deps),
                "D": {3: {}, 2: {}, 1: {}},
                "E": {1: {"D": Range.less_than(1)}},
            }
        )
        resolver = Resolver(provider)
        try:
            resolver.resolve({"root": Range.singleton(1)})
        except ResolutionError:
            pass
        assert resolver.stats.conflicts > 0
        # The credit lands on the depending package whose clause pinned D
        # (conflict_credit_target), not on D itself.
        assert resolver.stats.package_conflict_counts.get("E", 0) > 0
