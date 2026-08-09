"""Shared project and provider configuration for live-index benchmarks."""

from __future__ import annotations

from types import MappingProxyType
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
    VcsPolicy,
    join_extra,
    split_extra,
)

if TYPE_CHECKING:
    from collections.abc import AbstractSet, Mapping, Sequence
    from datetime import datetime

    from nab_index.multi_index import IndexConfig
    from nab_python.fetch import FetchCoordinator, IndexRoute
    from nab_python.target import ResolveTarget


# Search benchmarks trust pre-2.2 PKG-INFO dependency metadata by default.
DEFAULT_SCENARIO_TRUST_UNVERIFIED_SDIST_DEPS = True


class _BenchmarkResolveInputs(NamedTuple):
    """Inputs shared by one benchmark's provider and resolver.

    ``requirements`` holds the parsed roots, including extra proxy keys.
    ``constraints`` is an immutable copy extended to root-extra proxy keys.
    ``root_extras`` identifies extras requested directly by the benchmark.
    """

    requirements: dict[str, VersionRange]
    constraints: Mapping[str, VersionRange] | None
    root_extras: set[tuple[str, str]]


class ScenarioRequirementStrings(NamedTuple):
    """Copied requirement and constraint strings from one scenario."""

    requirements: list[str]
    constraints: list[str]


class ScenarioProjectMetadata(NamedTuple):
    """Copied project metadata used to expand a scenario's selected extras."""

    project_name: str | None
    project_extras: list[str]
    optional_dependencies: dict[str, list[str]]


def _scenario_string_list(
    scenario_name: str,
    field: str,
    value: object,
) -> list[str]:
    """Validate and copy one scenario list of strings."""
    if type(value) is not list:
        msg = f"{scenario_name}: {field} must be a list, got {type(value).__name__}"
        raise TypeError(msg)

    strings: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            msg = (
                f"{scenario_name}: {field}[{index}] must be a non-empty string, "
                f"got {type(item).__name__}"
            )
            raise TypeError(msg)
        if not item:
            msg = f"{scenario_name}: {field}[{index}] must be a non-empty string"
            raise ValueError(msg)
        strings.append(item)
    return strings


def parse_scenario_requirement_strings(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> ScenarioRequirementStrings:
    """Validate and copy a scenario's root and constraint strings."""
    if "requirements" not in scenario:
        msg = f"{scenario_name}: missing required field 'requirements'"
        raise ValueError(msg)

    return ScenarioRequirementStrings(
        requirements=_scenario_string_list(
            scenario_name,
            "requirements",
            scenario["requirements"],
        ),
        constraints=_scenario_string_list(
            scenario_name,
            "constraints",
            scenario.get("constraints", []),
        ),
    )


def _scenario_optional_dependencies(
    scenario_name: str,
    value: object,
) -> dict[str, list[str]]:
    """Validate and copy a scenario's optional-dependency table."""
    if type(value) is not dict:
        msg = (
            f"{scenario_name}: optional_dependencies must be a table, "
            f"got {type(value).__name__}"
        )
        raise TypeError(msg)

    optional_dependencies: dict[str, list[str]] = {}
    for extra, dependencies in value.items():
        if not isinstance(extra, str):
            msg = (
                f"{scenario_name}: optional_dependencies keys must be "
                f"non-empty strings, got {type(extra).__name__}"
            )
            raise TypeError(msg)
        if not extra:
            msg = (
                f"{scenario_name}: optional_dependencies keys must be non-empty strings"
            )
            raise ValueError(msg)
        optional_dependencies[extra] = _scenario_string_list(
            scenario_name,
            f"optional_dependencies[{extra!r}]",
            dependencies,
        )
    return optional_dependencies


def parse_scenario_project_metadata(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> ScenarioProjectMetadata:
    """Validate and copy project metadata from one benchmark scenario."""
    project_name = scenario.get("project_name")
    if project_name is not None and not isinstance(project_name, str):
        msg = (
            f"{scenario_name}: project_name must be a non-empty string, "
            f"got {type(project_name).__name__}"
        )
        raise TypeError(msg)
    if project_name == "":
        msg = f"{scenario_name}: project_name must be a non-empty string"
        raise ValueError(msg)

    return ScenarioProjectMetadata(
        project_name=project_name,
        project_extras=_scenario_string_list(
            scenario_name,
            "project_extras",
            scenario.get("project_extras", []),
        ),
        optional_dependencies=_scenario_optional_dependencies(
            scenario_name,
            scenario.get("optional_dependencies", {}),
        ),
    )


def parse_trust_unverified_sdist_deps(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> bool:
    """Read whether a scenario accepts unverified sdist dependency metadata."""
    value = scenario.get(
        "trust_unverified_sdist_deps",
        DEFAULT_SCENARIO_TRUST_UNVERIFIED_SDIST_DEPS,
    )
    if type(value) is not bool:
        msg = (
            f"{scenario_name}: trust_unverified_sdist_deps must be a boolean, "
            f"got {type(value).__name__}"
        )
        raise TypeError(msg)
    return value


def parse_vcs_require_pin(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> bool:
    """Read whether a scenario requires VCS URLs to pin a commit."""
    value = scenario.get("vcs_require_pin", True)
    if type(value) is not bool:
        msg = (
            f"{scenario_name}: vcs_require_pin must be a boolean, "
            f"got {type(value).__name__}"
        )
        raise TypeError(msg)
    return value


def parse_vcs_policy(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> VcsPolicy:
    """Return the VCS policy declared by a scenario."""
    value = scenario.get("vcs_policy", VcsPolicy.BLOCK.value)
    try:
        return VcsPolicy(value)
    except (TypeError, ValueError) as exc:
        valid = sorted(policy.value for policy in VcsPolicy)
        msg = f"{scenario_name}: vcs_policy must be one of {valid!r}, got {value!r}"
        raise ValueError(msg) from exc


def _scenario_vcs_string_list(
    scenario_name: str,
    field: str,
    value: object,
) -> list[str]:
    """Validate and copy one VCS list without interpreting its strings."""
    if type(value) is not list:
        msg = f"{scenario_name}: {field} must be a list, got {type(value).__name__}"
        raise TypeError(msg)

    strings: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            msg = (
                f"{scenario_name}: {field}[{index}] must be a string, "
                f"got {type(item).__name__}"
            )
            raise TypeError(msg)
        strings.append(item)
    return strings


def parse_vcs_allowed_schemes(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> frozenset[str]:
    """Return a scenario's copied VCS scheme allowlist as a set."""
    return frozenset(
        _scenario_vcs_string_list(
            scenario_name,
            "vcs_allowed_schemes",
            scenario.get("vcs_allowed_schemes", []),
        )
    )


def parse_vcs_allowed_repos(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> tuple[str, ...]:
    """Return a scenario's copied VCS repository allowlist in declaration order."""
    return tuple(
        _scenario_vcs_string_list(
            scenario_name,
            "vcs_allowed_repos",
            scenario.get("vcs_allowed_repos", []),
        )
    )


def parse_scenario_vcs_config(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> VcsConfig:
    """Build the VCS config declared by a benchmark scenario."""
    require_pin = parse_vcs_require_pin(scenario_name, scenario)
    policy = parse_vcs_policy(scenario_name, scenario)
    allowed_schemes = parse_vcs_allowed_schemes(scenario_name, scenario)
    allowed_repos = parse_vcs_allowed_repos(scenario_name, scenario)

    return VcsConfig(
        policy=policy,
        allowed_schemes=allowed_schemes,
        allowed_repos=allowed_repos,
        require_pin=require_pin,
    )


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


def _constraints_with_extra_proxies(
    constraints: Mapping[str, VersionRange],
    root_extras: AbstractSet[tuple[str, str]],
) -> Mapping[str, VersionRange]:
    """Return immutable constraints extended to the requested root-extra proxies.

    An extra proxy resolves under its own ``name[extra]`` key, so copying the base
    range there both constrains selection and attributes an empty range to the proxy,
    which is what the product does through its own constraints mapping.
    """
    extended = dict(constraints)
    for name, extra in root_extras:
        constraint = extended.get(name)
        if constraint is not None:
            extended[join_extra(name, extra)] = constraint
    return MappingProxyType(extended)


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
        _constraints_with_extra_proxies(constraints, root_extras)
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
