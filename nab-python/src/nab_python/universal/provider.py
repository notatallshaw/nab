"""Provider subclass that accepts a full PEP 508 marker environment.

The stock :class:`nab_python.provider.Provider` only accepts a
``python_version`` override and inherits everything else from
``default_environment()`` (the host environment).  Universal resolution
needs to swap the entire environment per tuple, so this subclass
overlays a user-supplied dict on top.

Also adds a ``preferences`` knob: a ``{package_name: Version}`` dict
tried first when choosing a version.  Used for cross-tuple alignment
("if tuple A picked numpy 2.2.6, ask tuple B to try 2.2.6 first").

Resolution strategy (``highest``/``lowest``/``lowest-direct``) is
inherited from :class:`Provider` and threaded through via the
parent's ``resolution_strategy`` and ``direct_packages`` kwargs.

When ``platform_spec`` is supplied, the provider also filters wheel
candidates by tag compatibility at resolve time (hole 2 in
``universal_open_questions.md``).  Versions whose only wheels are
above the spec's manylinux/musllinux/macOS floor become unavailable
unless an sdist is present and ``build_policy`` allows building.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_index.client import SdistFile, WheelFile

from .._vendor.packaging.markers import default_environment
from .._vendor.packaging.ranges import VersionRange
from .._vendor.packaging.utils import canonicalize_name
from ..provider import (
    BuildPolicy,
    DistPolicy,
    ExtrasMode,
    LocalSource,
    Provider,
    ResolutionStrategy,
    VcsConfig,
    VcsSource,
)
from .wheel_selection import compatible_tags_for_tuple, wheel_tag_set

__all__ = [
    "DistFile",
    "UniversalProvider",
]


DistFile = WheelFile | SdistFile

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from nab_resolver.types import RangeProtocol

    from .._vendor.packaging.version import Version
    from ..config import NabProjectConfig
    from ..fetch import FetchCoordinator
    from .wheel_selection import PlatformSpec


class UniversalProvider(Provider):
    """Provider with a user-supplied marker environment + preferences."""

    def __init__(  # noqa: PLR0913 - matches Provider signature
        self,
        coordinator: FetchCoordinator,
        marker_environment: dict[str, str],
        *,
        root_requirements: dict[str, VersionRange] | None = None,
        uploaded_prior_to: datetime | None = None,
        uploaded_prior_to_overrides: Mapping[str, datetime | None] | None = None,
        extras_mode: ExtrasMode = ExtrasMode.ERROR_USER,
        root_extras: set[tuple[str, str]] | None = None,
        dist_policy: DistPolicy = DistPolicy.WHEEL_OR_SDIST,
        dist_policy_overrides: Mapping[str, DistPolicy] | None = None,
        build_policy: BuildPolicy = BuildPolicy.BUILD_LOCAL,
        build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
        vcs_config: VcsConfig | None = None,
        vcs_cache_dir: Path | None = None,
        local_sources: list[LocalSource] | None = None,
        vcs_sources: list[VcsSource] | None = None,
        build_config: NabProjectConfig | None = None,
        preferences: dict[str, Version] | None = None,
        resolution_strategy: ResolutionStrategy | str = ResolutionStrategy.HIGHEST,
        direct_packages: frozenset[str] | None = None,
        platform_spec: PlatformSpec | None = None,
    ) -> None:
        """Create a provider with overlay environment + uv-style preferences."""
        if isinstance(resolution_strategy, str):
            try:
                resolution_strategy = ResolutionStrategy(resolution_strategy)
            except ValueError as exc:
                valid = sorted(s.value for s in ResolutionStrategy)
                msg = (
                    f"resolution_strategy must be one of {valid!r};"
                    f" got {resolution_strategy!r}"
                )
                raise ValueError(msg) from exc
        super().__init__(
            coordinator,
            python_version=marker_environment.get("python_version"),
            root_requirements=root_requirements,
            uploaded_prior_to=uploaded_prior_to,
            uploaded_prior_to_overrides=uploaded_prior_to_overrides,
            extras_mode=extras_mode,
            root_extras=root_extras,
            dist_policy=dist_policy,
            dist_policy_overrides=dist_policy_overrides,
            build_policy=build_policy,
            build_policy_overrides=build_policy_overrides,
            vcs_config=vcs_config,
            local_sources=local_sources,
            vcs_sources=vcs_sources,
            vcs_cache_dir=vcs_cache_dir,
            build_config=build_config,
            resolution_strategy=resolution_strategy,
            direct_packages=direct_packages,
        )
        merged: dict[str, str] = {
            key: value
            for key, value in default_environment().items()
            if isinstance(value, str)
        }
        merged.update(marker_environment)
        self.environment = merged
        self.env_with_extra = dict(merged)
        # Normalize preferences keys so lookup matches the provider's
        # canonical naming scheme.
        self._preferences: dict[str, Version] = {
            canonicalize_name(k): v for k, v in (preferences or {}).items()
        }
        self._platform_spec = platform_spec
        self._py_minor = marker_environment.get("python_version")
        self.excluded_by_wheel_tags = 0
        self.excluded_versions_no_compatible_wheel = 0

    def choose_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> Version | None:
        """Pick a version under the configured strategy, honoring preferences."""
        assert isinstance(version_range, VersionRange)
        _, _, normalized = self.split_and_normalize(package)

        # Preferences win when the version is still in range AND its metadata
        # is extractable in this tuple; the look-ahead gate matters for
        # cross-tuple alignment (a candidate that worked elsewhere may be
        # tag-incompatible or unbuildable here).
        preferred = self._preferences.get(normalized)
        if preferred is not None:
            all_versions = self.versions_only(normalized, self.fetch_versions(package))
            if preferred in set(version_range.filter(all_versions)) and (
                self.split_and_normalize(package)[1] is not None
                or self._look_ahead_ok(normalized, preferred, check_decisions=True)
            ):
                self._flush_pending_blocks()
                return preferred

        return super().choose_version(package, version_range)

    def filter_distributions(
        self, normalized: str, files: Sequence[WheelFile | SdistFile]
    ) -> list[tuple[Version, DistFile]]:
        """Filter parent's result by wheel-tag compatibility.

        Hole 2 plug: a version is unavailable to the resolver if its
        only wheels are tag-incompatible with this tuple's
        ``platform_spec``.  Sdists keep the version alive at every
        :class:`BuildPolicy` level because static PKG-INFO and the
        bundled ``pyproject.toml`` fallback are read unconditionally;
        ``BUILD_LOCAL`` adds backend invocation on local checkouts and
        ``BUILD_REMOTE`` adds it on VCS clones and remote sdists.  The
        resolver's ``look_ahead_ok`` rejects per-version if metadata
        extraction actually fails (e.g. dynamic deps with no static
        fallback under :attr:`BuildPolicy.NEVER`).

        When ``platform_spec`` is unset (legacy callers) the override
        is a no-op.
        """
        base = super().filter_distributions(normalized, files)
        if self._platform_spec is None or self._py_minor is None:
            return base

        spec = self._platform_spec
        py_minor = self._py_minor
        # Look up the per-tuple compat tag set once outside the per-wheel
        # loop and inline the membership check; this loop runs for every
        # wheel of every package on every tuple, so the hoist matters on
        # large workloads.
        compat = compatible_tags_for_tuple(python_version=py_minor, spec=spec)
        kept: list[tuple[Version, DistFile]] = []
        versions_with_wheel: set[Version] = set()
        versions_with_sdist: set[Version] = set()
        for version, dist in base:
            if isinstance(dist, WheelFile):
                wheel_tags = wheel_tag_set(dist.filename)
                if wheel_tags is not None and not wheel_tags.isdisjoint(compat):
                    kept.append((version, dist))
                    versions_with_wheel.add(version)
                else:
                    self.excluded_by_wheel_tags += 1
            else:
                kept.append((version, dist))
                versions_with_sdist.add(version)

        build_allowed = self.build_policy != BuildPolicy.NEVER
        usable = versions_with_wheel | (versions_with_sdist if build_allowed else set())
        all_versions = {v for v, _ in base}
        self.excluded_versions_no_compatible_wheel += len(all_versions) - len(usable)
        return [pair for pair in kept if pair[0] in usable]
