"""Tests for conflict-driven priority reordering.

The provider receives conflict counts via ``prioritize`` and decides
how to use them. These tests verify the plumbing: conflict counts
reach the provider and a provider that promotes conflicting packages
changes resolution behavior.
"""

from __future__ import annotations

from nab_resolver.ranges import Range
from nab_resolver.resolver import ResolutionError, Resolver


class _TrackingProvider:
    """Provider that records conflict_counts passed to prioritize."""

    def __init__(self, packages: dict[str, dict[int, dict[str, Range]]]) -> None:
        self._packages = packages
        self.seen_conflict_counts: list[dict[str, int]] = []

    def choose_version(self, package: str, version_range: Range) -> int | None:
        """Pick the newest version within the allowed range."""
        for version in sorted(self._packages.get(package, {}).keys(), reverse=True):
            if version in version_range:
                return version
        return None

    def get_dependencies(self, package: str, version: int) -> dict[str, Range]:
        """Return dependencies for a specific version."""
        return self._packages.get(package, {}).get(version, {})

    def prioritize(
        self,
        package: str,
        version_range: Range,
        conflict_counts: dict[str, int],
        culprit_counts: dict[str, int] | None = None,
    ) -> int:
        """Record conflict counts and prioritize by version count."""
        self.seen_conflict_counts.append(dict(conflict_counts))
        versions = list(self._packages.get(package, {}).keys())
        return sum(1 for v in versions if v in version_range)

    def is_ready(self, package: str) -> bool:
        """All packages are immediately decidable in tests."""
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: dict[str, Range],
        decisions: dict[str, int],
    ) -> None:
        """No-op: test provider does not use partial solution state."""

    def consume_pending_clauses(self) -> list:
        """No queued clauses for this test provider."""
        return []

    def consume_force_backtrack_targets(self) -> list:
        """No force-backtrack signal from this test provider."""
        return []


class _PromotingProvider:
    """Provider that promotes packages with 3+ conflicts."""

    def __init__(self, packages: dict[str, dict[int, dict[str, Range]]]) -> None:
        self._packages = packages
        self.seen_conflict_counts: list[dict[str, int]] = []

    def choose_version(self, package: str, version_range: Range) -> int | None:
        """Pick the newest version within the allowed range."""
        for version in sorted(self._packages.get(package, {}).keys(), reverse=True):
            if version in version_range:
                return version
        return None

    def get_dependencies(self, package: str, version: int) -> dict[str, Range]:
        """Return dependencies for a specific version."""
        return self._packages.get(package, {}).get(version, {})

    def prioritize(
        self,
        package: str,
        version_range: Range,
        conflict_counts: dict[str, int],
        culprit_counts: dict[str, int] | None = None,
    ) -> tuple[int, int]:
        """Promoted packages sort first (0 before 1)."""
        self.seen_conflict_counts.append(dict(conflict_counts))
        promoted = 0 if conflict_counts.get(package, 0) >= 3 else 1
        versions = list(self._packages.get(package, {}).keys())
        count = sum(1 for v in versions if v in version_range)
        return (promoted, count)

    def is_ready(self, package: str) -> bool:
        """All packages are immediately decidable in tests."""
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: dict[str, Range],
        decisions: dict[str, int],
    ) -> None:
        """No-op: test provider does not use partial solution state."""

    def consume_pending_clauses(self) -> list:
        """No queued clauses for this test provider."""
        return []

    def consume_force_backtrack_targets(self) -> list:
        """No force-backtrack signal from this test provider."""
        return []


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
        # prioritize was called at least once
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
        # Later calls to prioritize should see non-empty conflict counts
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
        assert resolver.stats.package_conflict_counts.get("D", 0) > 0
