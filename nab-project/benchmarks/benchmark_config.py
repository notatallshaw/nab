"""Shared project and provider configuration for live-index benchmarks."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple

from nab_index.local_index import is_file_url
from nab_index.multi_index import IndexConfig
from nab_project.fetch import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, IndexRoute
from nab_project.inputs import ResolveInputs
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.utils import InvalidName, canonicalize_name
from nab_provider.overrides import PackageOverride
from nab_provider.provider import (
    BuildPolicy,
    DistPolicy,
    Provider,
    ResolutionStrategy,
    VcsConfig,
    VcsPolicy,
    join_extra,
    split_extra,
)
from nab_provider.serialization import SimpleSerialization

if TYPE_CHECKING:
    from collections.abc import AbstractSet, Mapping, Sequence
    from datetime import datetime

    from nab_project.fetch import FetchCoordinator
    from nab_provider.target import ResolveTarget


DEFAULT_SCENARIO_TRUST_UNVERIFIED_SDIST_DEPS = (
    ResolveInputs().trust_unverified_sdist_deps
)
DEFAULT_INDEXES: tuple[IndexConfig, ...] = (
    IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),
)
_INDEX_KEYS = frozenset({"name", "url", "serialization"})
_INDEX_ROUTE_KEYS = frozenset({"name", "index"})
_SCENARIO_SETTINGS = frozenset(
    {
        "requirements",
        "constraints",
        "project_name",
        "project_extras",
        "optional_dependencies",
        "indexes",
        "index_routes",
        "build_packages",
        "trust_unverified_sdist_deps",
        "vcs_policy",
        "vcs_allowed_schemes",
        "vcs_allowed_repos",
        "vcs_require_pin",
        "python_version",
        "marker_environment",
        "platform_system",
        "resolution",
        "datetime",
        "requires_matching_host",
        "unsupported_reason",
    }
)


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


def validate_scenario_settings(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> None:
    """Reject setting names that no live benchmark runner understands."""
    unknown = sorted(
        setting for setting in scenario if setting not in _SCENARIO_SETTINGS
    )
    if unknown:
        msg = f"{scenario_name}: unknown scenario settings: {unknown!r}"
        raise ValueError(msg)


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


def _parse_scenario_index_serialization(
    scenario_name: str,
    position: int,
    entry: Mapping[str, object],
    *,
    name: str,
    url: str,
) -> SimpleSerialization:
    """Return the serialization requested by one scenario index."""
    if "serialization" in entry and is_file_url(url):
        msg = (
            f"{scenario_name}: indexes[{position}].serialization must be omitted "
            f"for file:// index {name!r}"
        )
        raise ValueError(msg)

    serialization = entry.get("serialization")
    if serialization is None:
        return SimpleSerialization.NEGOTIATE
    if not isinstance(serialization, str):
        msg = (
            f"{scenario_name}: indexes[{position}].serialization must be a string, "
            f"got {type(serialization).__name__}"
        )
        raise TypeError(msg)

    try:
        return SimpleSerialization(serialization)
    except ValueError as exc:
        valid = sorted(member.value for member in SimpleSerialization)
        msg = (
            f"{scenario_name}: indexes[{position}].serialization must be one "
            f"of {valid!r}, got {serialization!r}"
        )
        raise ValueError(msg) from exc


def _parse_scenario_index(
    scenario_name: str,
    position: int,
    value: object,
) -> IndexConfig:
    """Validate and copy one index entry from a benchmark scenario."""
    if not isinstance(value, dict):
        msg = (
            f"{scenario_name}: indexes[{position}] must be a table, "
            f"got {type(value).__name__}"
        )
        raise TypeError(msg)

    unknown = sorted(set(value) - _INDEX_KEYS)
    if unknown:
        msg = (
            f"{scenario_name}: unknown indexes[{position}] keys: {unknown!r}; "
            f"expected {sorted(_INDEX_KEYS)!r}"
        )
        raise ValueError(msg)
    try:
        name = value["name"]
        url = value["url"]
    except KeyError as missing:
        msg = f"{scenario_name}: indexes[{position}] missing required key {missing!s}"
        raise ValueError(msg) from None
    if not isinstance(name, str) or not isinstance(url, str):
        msg = f"{scenario_name}: indexes[{position}] name and url must be strings"
        raise TypeError(msg)

    serialization = _parse_scenario_index_serialization(
        scenario_name,
        position,
        value,
        name=name,
        url=url,
    )
    return IndexConfig(name, url, serialization)


def _check_scenario_index_name_uniqueness(
    scenario_name: str,
    indexes: Sequence[IndexConfig],
) -> None:
    """Reject duplicate scenario index names after parsing every entry."""
    seen: set[str] = set()
    for index in indexes:
        if index.name in seen:
            msg = f"{scenario_name}: duplicate index name: {index.name!r}"
            raise ValueError(msg)
        seen.add(index.name)


def parse_scenario_indexes(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> list[IndexConfig]:
    """Validate and copy the indexes declared by a benchmark scenario."""
    if "indexes" not in scenario:
        return list(DEFAULT_INDEXES)

    raw = scenario["indexes"]
    if not isinstance(raw, list):
        msg = (
            f"{scenario_name}: indexes must be an array of tables, "
            f"got {type(raw).__name__}"
        )
        raise TypeError(msg)
    if not raw:
        msg = f"{scenario_name}: indexes must contain at least one entry when present"
        raise ValueError(msg)

    indexes = [
        _parse_scenario_index(scenario_name, position, entry)
        for position, entry in enumerate(raw)
    ]
    _check_scenario_index_name_uniqueness(scenario_name, indexes)
    return indexes


def _parse_scenario_index_route(
    scenario_name: str,
    position: int,
    value: object,
) -> IndexRoute:
    """Validate and copy one package route from a benchmark scenario."""
    if not isinstance(value, dict):
        msg = (
            f"{scenario_name}: index_routes[{position}] must be a table, "
            f"got {type(value).__name__}"
        )
        raise TypeError(msg)

    unknown = sorted(set(value) - _INDEX_ROUTE_KEYS)
    if unknown:
        msg = (
            f"{scenario_name}: unknown index_routes[{position}] keys: {unknown!r}; "
            f"expected {sorted(_INDEX_ROUTE_KEYS)!r}"
        )
        raise ValueError(msg)
    if "name" not in value:
        msg = f"{scenario_name}: index_routes[{position}] missing required key 'name'"
        raise ValueError(msg)
    if "index" not in value:
        msg = f"{scenario_name}: index_routes[{position}] missing required key 'index'"
        raise ValueError(msg)

    name = value["name"]
    index = value["index"]
    if not isinstance(name, str):
        msg = (
            f"{scenario_name}: index_routes[{position}].name must be a string, "
            f"got {type(name).__name__}"
        )
        raise TypeError(msg)
    if not isinstance(index, str):
        msg = (
            f"{scenario_name}: index_routes[{position}].index must be a string, "
            f"got {type(index).__name__}"
        )
        raise TypeError(msg)

    try:
        canonicalize_name(name, validate=True)
    except InvalidName as exc:
        msg = (
            f"{scenario_name}: index_routes[{position}].name must be a valid "
            f"distribution name, got {name!r}"
        )
        raise ValueError(msg) from exc
    return IndexRoute(name=name, index=index)


def _check_scenario_index_route_relationships(
    scenario_name: str,
    routes: Sequence[IndexRoute],
    indexes: Sequence[IndexConfig],
) -> None:
    """Reject duplicate package routes and references to undeclared indexes."""
    seen: set[str] = set()
    for route in routes:
        name = canonicalize_name(route.name, validate=True)
        if name in seen:
            msg = f"{scenario_name}: duplicate index route for {name!r}"
            raise ValueError(msg)
        seen.add(name)

    declared_indexes = {index.name for index in indexes}
    for route in routes:
        if route.index not in declared_indexes:
            msg = (
                f"{scenario_name}: index route for {route.name!r} names undeclared "
                f"index {route.index!r}; declared indexes are "
                f"{sorted(declared_indexes)!r}"
            )
            raise ValueError(msg)


def parse_scenario_index_routes(
    scenario_name: str,
    scenario: Mapping[str, object],
    indexes: Sequence[IndexConfig],
) -> list[IndexRoute]:
    """Validate and copy the package routes declared by a benchmark scenario."""
    raw = scenario.get("index_routes", [])
    if not isinstance(raw, list):
        msg = (
            f"{scenario_name}: index_routes must be an array of tables, "
            f"got {type(raw).__name__}"
        )
        raise TypeError(msg)

    routes = [
        _parse_scenario_index_route(scenario_name, position, entry)
        for position, entry in enumerate(raw)
    ]
    _check_scenario_index_route_relationships(scenario_name, routes, indexes)
    return routes


def parse_scenario_build_packages(
    scenario_name: str,
    scenario: Mapping[str, object],
) -> dict[str, BuildPolicy]:
    """Return remote-build overrides preserving declared spelling and order."""
    raw = scenario.get("build_packages", [])
    if type(raw) is not list:
        msg = (
            f"{scenario_name}: build_packages must be a list of package names, "
            f"got {type(raw).__name__}"
        )
        raise TypeError(msg)

    packages: list[tuple[str, str]] = []
    for position, name in enumerate(raw):
        if not isinstance(name, str):
            msg = (
                f"{scenario_name}: build_packages[{position}] must be a string, "
                f"got {type(name).__name__}"
            )
            raise TypeError(msg)
        try:
            canonical_name = canonicalize_name(name, validate=True)
        except InvalidName as exc:
            msg = (
                f"{scenario_name}: build_packages[{position}] must be a valid "
                f"distribution name, got {name!r}"
            )
            raise ValueError(msg) from exc
        packages.append((name, canonical_name))

    seen: set[str] = set()
    for _, canonical_name in packages:
        if canonical_name in seen:
            msg = f"{scenario_name}: duplicate build package {canonical_name!r}"
            raise ValueError(msg)
        seen.add(canonical_name)

    return {name: BuildPolicy.BUILD_REMOTE for name, _ in packages}


def validate_scenario_build_policy(
    scenario_name: str,
    marker_environment: Mapping[str, str],
    build_policy_overrides: Mapping[str, BuildPolicy],
) -> None:
    """Reject build policy paired with a marker environment overlay."""
    if marker_environment and build_policy_overrides:
        msg = (
            f"{scenario_name}: build_packages cannot be combined "
            "with a marker environment overlay"
        )
        raise ValueError(msg)


def benchmark_index_settings(
    indexes: Sequence[IndexConfig],
) -> list[dict[str, str]]:
    """Return effective index settings with default serialization omitted."""
    settings: list[dict[str, str]] = []
    for index in indexes:
        entry = {"name": index.name, "url": index.url}
        if index.serialization is not SimpleSerialization.NEGOTIATE:
            entry["serialization"] = index.serialization.value
        settings.append(entry)
    return settings


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
    trust_unverified_sdist_deps: bool,
    uploaded_prior_to: datetime | None = None,
    index_routes: Sequence[IndexRoute] = (),
    build_policy_overrides: Mapping[str, BuildPolicy] | None = None,
    resolution: ResolutionStrategy = ResolutionStrategy.HIGHEST,
    vcs: VcsConfig | None = None,
) -> ResolveInputs:
    """Build the settings one benchmark scenario resolves under."""
    return ResolveInputs(
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
    config: ResolveInputs,
    target: ResolveTarget,
    inputs: _BenchmarkResolveInputs,
) -> Provider:
    """Build a provider from a benchmark scenario's settings and roots."""
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
        resolution_strategy=config.resolution,
        direct_packages=direct_packages_from_requirements(inputs.requirements),
        constraints=inputs.constraints,
    )
