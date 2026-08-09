"""Shared project and provider configuration for live-index benchmarks."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python.config import NabProjectConfig, PackageOverride
from nab_python.provider import (
    BuildPolicy,
    DistPolicy,
    Provider,
    ResolutionStrategy,
    VcsConfig,
    split_extra,
)
from nab_python.resolve import constraints_with_root_extra_proxies

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from nab_index.multi_index import IndexConfig
    from nab_python.fetch import FetchCoordinator, IndexRoute
    from nab_python.target import ResolveTarget


class _BenchmarkResolveInputs(NamedTuple):
    """Inputs shared by one benchmark's provider and resolver.

    ``requirements`` holds the parsed roots, including extra proxy keys.
    ``constraints`` is an immutable copy extended to root-extra proxy keys.
    ``root_extras`` identifies extras requested directly by the benchmark.
    """

    requirements: dict[str, VersionRange]
    constraints: Mapping[str, VersionRange] | None
    root_extras: set[tuple[str, str]]


def _package_overrides(
    indexes: Sequence[IndexConfig],
    index_routes: Sequence[IndexRoute],
    build_policy_overrides: Mapping[str, BuildPolicy],
) -> tuple[PackageOverride, ...]:
    """Combine benchmark routing and build settings by canonical package name."""
    declared_indexes = {index.name for index in indexes}
    route_indexes: dict[str, str] = {}
    order: list[str] = []

    for route in index_routes:
        name = canonicalize_name(route.name)
        if name in route_indexes:
            msg = f"duplicate index route for {name!r}"
            raise ValueError(msg)
        if route.index not in declared_indexes:
            msg = f"index route for {name!r} names undeclared index {route.index!r}"
            raise ValueError(msg)
        route_indexes[name] = route.index
        order.append(name)

    build_policies: dict[str, BuildPolicy] = {}
    for raw_name, policy in build_policy_overrides.items():
        name = canonicalize_name(raw_name)
        if name not in order:
            order.append(name)
        build_policies[name] = policy

    return tuple(
        PackageOverride(
            requirement=Requirement(name),
            name=name,
            version_range=VersionRange.full(),
            build_policy=build_policies.get(name),
            index=route_indexes.get(name),
        )
        for name in order
    )


def build_benchmark_config(
    *,
    indexes: Sequence[IndexConfig],
    uploaded_prior_to: datetime | None = None,
    index_routes: Sequence[IndexRoute] = (),
    build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
    resolution: ResolutionStrategy = ResolutionStrategy.HIGHEST,
    trust_unverified_sdist_deps: bool = False,
    vcs: VcsConfig | None = None,
) -> NabProjectConfig:
    """Build the project config represented by one benchmark scenario."""
    return NabProjectConfig(
        uploaded_prior_to=uploaded_prior_to,
        dist_policy=DistPolicy.WHEEL_OR_SDIST,
        build_policy=BuildPolicy.NEVER,
        trust_unverified_sdist_deps=trust_unverified_sdist_deps,
        indexes=tuple(indexes),
        vcs=vcs or VcsConfig(),
        resolution=resolution,
        package_overrides=_package_overrides(
            indexes,
            index_routes,
            build_policy_overrides or {},
        ),
    )


def direct_packages_from_requirements(
    requirements: Mapping[str, VersionRange],
) -> frozenset[str]:
    """Return the direct non-extra package names represented by root requirements."""
    return frozenset(name for name in requirements if split_extra(name)[1] is None)


def build_benchmark_resolver_inputs(
    requirements: dict[str, VersionRange],
    constraints: Mapping[str, VersionRange] | None,
) -> _BenchmarkResolveInputs:
    """Copy constraints and extend them to the requested root-extra proxies."""
    root_extras: set[tuple[str, str]] = set()
    for package in requirements:
        name, extra = split_extra(package)
        if extra is not None:
            root_extras.add((name, extra))

    resolver_constraints = (
        constraints_with_root_extra_proxies(constraints, root_extras)
        if constraints is not None
        else None
    )
    return _BenchmarkResolveInputs(
        requirements=requirements,
        constraints=resolver_constraints,
        root_extras=root_extras,
    )


def build_benchmark_provider(
    coordinator: FetchCoordinator,
    *,
    config: NabProjectConfig,
    target: ResolveTarget,
    inputs: _BenchmarkResolveInputs,
) -> Provider:
    """Build a provider from a benchmark project config and its roots."""
    return Provider(
        coordinator,
        target=target,
        root_requirements=inputs.requirements,
        root_extras=inputs.root_extras,
        uploaded_prior_to=config.uploaded_prior_to,
        dist_policy=config.dist_policy,
        build_policy=config.build_policy,
        package_overrides=config.package_overrides,
        index_overrides=config.index_overrides,
        trust_unverified_sdist_deps=config.trust_unverified_sdist_deps,
        vcs_config=config.vcs,
        local_sources=list(config.local_sources) or None,
        vcs_sources=list(config.vcs_sources) or None,
        archive_sources=list(config.archive_sources) or None,
        build_config=config,
        resolution_strategy=config.resolution,
        direct_packages=direct_packages_from_requirements(inputs.requirements),
        constraints=inputs.constraints,
    )
