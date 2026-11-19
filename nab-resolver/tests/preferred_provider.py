"""Test provider with explicit version preferences and pending solver requests."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from nab_resolver._compat import override
from nab_resolver.ranges import Range
from nab_resolver.resolver import BaseProvider
from nab_resolver.types import Incompatibility, RangeProtocol

Graph = dict[str, dict[int, dict[str, Range[int]]]]


class GraphProvider(BaseProvider[str, int]):
    """A finite package registry with explicit version preferences and facts."""

    def __init__(self, packages: Graph) -> None:
        self.packages = packages
        self.preferences: dict[str, int] = {}
        self.choices: list[str] = []
        self.reads: list[str] = []
        self.pending: list[Incompatibility[str, int]] = []
        self.forced: list[str] = []
        self.on_choice: Callable[[GraphProvider, str], None] | None = None
        self.on_metadata: Callable[[GraphProvider, str], None] | None = None

    def choose_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> int | None:
        self.choices.append(package)
        if self.on_choice is not None:
            self.on_choice(self, package)
        versions = self.packages.get(package, {})
        preferred = self.preferences.get(package)
        if (
            preferred is not None
            and preferred in versions
            and preferred in version_range
        ):
            return preferred
        return next(
            (v for v in sorted(versions, reverse=True) if v in version_range), None
        )

    def get_dependencies(self, package: str, version: int) -> Mapping[str, Range[int]]:
        self.reads.append(package)
        if self.on_metadata is not None:
            self.on_metadata(self, package)
        return self.packages[package][version]

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Choose the package with the fewest currently available versions."""
        return sum(
            version in version_range for version in self.packages.get(package, {})
        )

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[int]
    ) -> bool:
        """Read availability without changing provider state."""
        return any(
            version in version_range for version in self.packages.get(package, {})
        )

    def widen_decision(self, package: str, version: int) -> RangeProtocol[int] | None:
        """Keep dependency facts specific to their selected version."""
        return None

    @override
    def consume_pending_clauses(self) -> list[Incompatibility[str, int]]:
        pending, self.pending = self.pending, []
        return pending

    @override
    def consume_force_backtrack_targets(self) -> list[str]:
        forced, self.forced = self.forced, []
        return forced
