"""Provider that bridges packaging's PEP 440 types to nab-resolver."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._vendor.packaging.ranges import VersionRange

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nab_resolver.types import Incompatibility, RangeProtocol

    from ._vendor.packaging.specifiers import SpecifierSet
    from ._vendor.packaging.version import Version

__all__ = [
    "PackagingProvider",
]


class PackagingProvider:
    """In-memory provider using PEP 440 versions.

    Packages are strings, versions are :class:`packaging.version.Version`,
    and dependency constraints are :class:`packaging.specifiers.SpecifierSet`,
    converted to :class:`packaging.ranges.VersionRange` for the resolver.
    """

    def __init__(
        self,
        packages: dict[str, dict[Version, dict[str, SpecifierSet]]],
    ) -> None:
        """Create a provider from a package graph."""
        self._packages = packages

    def _get_versions(self, package: str) -> list[Version]:
        if package not in self._packages:
            return []
        return sorted(self._packages[package].keys(), reverse=True)

    def choose_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> Version | None:
        """Pick the newest version within the allowed range."""
        for version in self._get_versions(package):
            if version in version_range:
                return version
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> bool:
        """Report whether any available version falls in the range."""
        return any(v in version_range for v in self._get_versions(package))

    def get_dependencies(
        self, package: str, version: Version
    ) -> dict[str, VersionRange]:
        """Convert SpecifierSet deps to VersionRange deps."""
        raw = self._packages.get(package, {}).get(version, {})
        return {
            dep: (spec.to_range() if spec else VersionRange.full(admit_arbitrary=False))
            for dep, spec in raw.items()
        }

    _CONFLICT_THRESHOLD = 5

    def begin_decision_scan(self) -> None:
        """No-op: nothing this in-memory provider answers arrives async."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[Version],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[bool, int]:
        """Prioritize packages for resolution order.

        Returns a tuple compared with min(), so lower = decided first.
        Packages with many conflicts are promoted so the resolver
        discovers incompatibilities before deciding downstream packages.
        ``culprit_counts`` is accepted for protocol compatibility but
        not used by this provider.
        """
        del culprit_counts
        promoted = conflict_counts.get(package, 0) >= self._CONFLICT_THRESHOLD
        versions = self._get_versions(package)
        matching = sum(1 for v in versions if v in version_range)
        return (not promoted, matching)

    def is_ready(self, package: str) -> bool:
        """All packages are immediately decidable for this in-memory provider."""
        del package
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[Version]],
        decisions: Mapping[str, Version],
    ) -> None:
        """No-op: in-memory provider does not use partial solution state."""

    def consume_pending_clauses(self) -> list[Incompatibility[str, Version]]:
        """No queued clauses for this in-memory provider."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal from this in-memory provider."""
        return []

    def widen_decision(self, package: str, version: Version) -> VersionRange | None:
        """No widening: dependency clauses keep the exact decided version."""
        del package, version
        return None

    def narrow_for_display(
        self, package: str, constraint: RangeProtocol[Version]
    ) -> RangeProtocol[Version]:
        """Identity: constraints render as stored."""
        del package
        return constraint
