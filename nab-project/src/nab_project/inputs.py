"""What nab-project reads out of a project's configuration.

Declared here rather than by the host because nab-project may not import ``nab``.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from nab_provider.policy import (
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    ResolutionStrategy,
)
from nab_provider.records import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, IndexConfig
from nab_provider.vcs_admission import VcsConfig

from .value import ValueType

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from nab_provider.overrides import IndexOverride, PackageOverride
    from nab_provider.policy import ArchiveSource, LocalSource, VcsSource

    from .conflicts import ConflictSet

__all__ = ["ResolveInputs"]


_DEFAULT_INDEXES = (IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),)
_NO_INDEX_OVERRIDES: Mapping[str, IndexOverride] = MappingProxyType({})
_DEFAULT_VCS = VcsConfig()


class ResolveInputs(ValueType):
    """The settings one resolve runs under."""

    __slots__ = __match_args__ = (
        "archive_sources",
        "base_group",
        "build_group",
        "build_policy",
        "build_requires_depth",
        "conflicts",
        "constraints",
        "decision_order",
        "default_groups",
        "dist_policy",
        "index_overrides",
        "indexes",
        "local_sources",
        "package_overrides",
        "requires_python",
        "resolution",
        "trust_unverified_sdist_deps",
        "uploaded_prior_to",
        "vcs",
        "vcs_sources",
    )

    archive_sources: tuple[ArchiveSource, ...]
    base_group: str | None
    build_group: str | None
    build_policy: BuildPolicy
    build_requires_depth: int
    conflicts: tuple[ConflictSet, ...]
    constraints: tuple[str, ...]
    decision_order: DecisionOrder
    default_groups: tuple[str, ...]
    dist_policy: DistPolicy
    index_overrides: Mapping[str, IndexOverride]
    indexes: tuple[IndexConfig, ...]
    local_sources: tuple[LocalSource, ...]
    package_overrides: tuple[PackageOverride, ...]
    requires_python: str | None
    resolution: ResolutionStrategy
    trust_unverified_sdist_deps: bool
    uploaded_prior_to: datetime | None
    vcs: VcsConfig
    vcs_sources: tuple[VcsSource, ...]

    def __init__(  # noqa: PLR0913 - one keyword per setting a resolve reads
        self,
        *,
        archive_sources: tuple[ArchiveSource, ...] = (),
        base_group: str | None = None,
        build_group: str | None = None,
        build_policy: BuildPolicy = BuildPolicy.BUILD_LOCAL,
        build_requires_depth: int = 0,
        conflicts: tuple[ConflictSet, ...] = (),
        constraints: tuple[str, ...] = (),
        decision_order: DecisionOrder = DecisionOrder.ARRIVAL,
        default_groups: tuple[str, ...] = (),
        dist_policy: DistPolicy = DistPolicy.WHEEL_OR_SDIST,
        index_overrides: Mapping[str, IndexOverride] = _NO_INDEX_OVERRIDES,
        indexes: tuple[IndexConfig, ...] = _DEFAULT_INDEXES,
        local_sources: tuple[LocalSource, ...] = (),
        package_overrides: tuple[PackageOverride, ...] = (),
        requires_python: str | None = None,
        resolution: ResolutionStrategy = ResolutionStrategy.HIGHEST,
        trust_unverified_sdist_deps: bool = False,
        uploaded_prior_to: datetime | None = None,
        vcs: VcsConfig = _DEFAULT_VCS,
        vcs_sources: tuple[VcsSource, ...] = (),
    ) -> None:
        """Record the settings, each defaulting to what a bare project gets."""
        self.archive_sources = archive_sources
        self.base_group = base_group
        self.build_group = build_group
        self.build_policy = build_policy
        self.build_requires_depth = build_requires_depth
        self.conflicts = conflicts
        self.constraints = constraints
        self.decision_order = decision_order
        self.default_groups = default_groups
        self.dist_policy = dist_policy
        self.index_overrides = index_overrides
        self.indexes = indexes
        self.local_sources = local_sources
        self.package_overrides = package_overrides
        self.requires_python = requires_python
        self.resolution = resolution
        self.trust_unverified_sdist_deps = trust_unverified_sdist_deps
        self.uploaded_prior_to = uploaded_prior_to
        self.vcs = vcs
        self.vcs_sources = vcs_sources

    def replace(self, **changes: object) -> ResolveInputs:
        """Return a copy with ``changes`` applied, as ``dataclasses.replace`` would."""
        kept = {name: getattr(self, name) for name in self.__match_args__}
        return ResolveInputs(**{**kept, **changes})
