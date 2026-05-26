"""Orchestrate dependency resolution from a pyproject.toml file."""

from __future__ import annotations

import contextlib
import itertools
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nab_resolver.resolver import (
    Incompatibility,
    IncompatibilityCause,
    ResolutionError,
    Resolver,
)

from ._vcs_admission import admit_vcs_url
from ._vendor.packaging.markers import default_environment
from ._vendor.packaging.ranges import VersionRange
from ._vendor.packaging.requirements import Requirement
from ._vendor.packaging.specifiers import SpecifierSet
from ._vendor.packaging.utils import canonicalize_name
from ._vendor.packaging.version import InvalidVersion, Version
from .config import ConfigError, NabProjectConfig, ResolveMode, read_pyproject_config
from .fetch import FetchCoordinator
from .lockfile import LockInput, build_lock_input_from_provider
from .provider import (
    Provider,
    ResolutionStrategy,
    join_extra,
    python_axis_environment,
    split_extra,
)
from .requirements_file import (
    expand_self_extras,
    raise_for_unsatisfiable,
    read_pyproject_dependencies,
    read_pyproject_groups,
    read_pyproject_name,
    read_pyproject_optional_dependencies,
    resolve_groups_to_requirements,
    select_optional_dependencies,
)
from .universal.matrix import Matrix
from .universal.resolve import UniversalResult, resolve_universal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from nab_index.transport import AsyncHttpTransport

    from .universal.matrix import MatrixTuple


__all__ = [
    "ResolutionResult",
    "UnsupportedModeError",
    "resolve_pyproject",
    "resolve_universal_pyproject",
]


_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """A finished single-environment resolution and its lock input.

    ``pins`` is the canonical-name -> :class:`Version` mapping.
    ``lock_input`` carries everything needed to write a PEP 751
    ``pylock.toml`` or a hashed ``requirements.txt`` and to download
    the chosen artefacts.
    """

    pins: dict[str, Version]
    lock_input: LockInput


class UnsupportedModeError(NotImplementedError):
    """Resolve mode requested is not handled by this entry point."""


def resolve_pyproject(  # noqa: PLR0913 - the surface mirrors the CLI; bundling into a config object would hide it
    path: Path,
    transport: AsyncHttpTransport,
    *,
    config: NabProjectConfig | None = None,
    cache_dir: Path | None = None,
    offline: bool = False,
    python_version: str | None = None,
    groups: Sequence[str] = (),
    extras: Sequence[str] = (),
    resolution_strategy: ResolutionStrategy | None = None,
    seed_pins: dict[str, Version] | None = None,
) -> ResolutionResult:
    """Resolve a project's dependencies for a single environment.

    ``config`` defaults to :func:`read_pyproject_config(path)`. The
    caller supplies ``transport`` so the HTTP library choice stays
    outside nab-python. ``cache_dir``, ``offline`` and
    ``python_version`` are runtime overrides from the CLI.

    ``groups`` and ``extras`` name PEP 735 groups and
    ``[project.optional-dependencies]`` keys to fold in.
    ``resolution_strategy`` overrides ``config.resolution`` when set.
    ``seed_pins`` maps canonical names to versions preferred from a
    prior lock, keeping a re-lock stable unless a constraint forces a
    change.

    Use :func:`resolve_universal_pyproject` when
    ``config.mode is ResolveMode.UNIVERSAL``. Returns a
    :class:`ResolutionResult` with ``pins`` and ``lock_input``.
    """
    if config is None:
        config = read_pyproject_config(path)

    if config.mode is not ResolveMode.SPECIFIC:
        msg = (
            f"resolve_pyproject only handles ResolveMode.SPECIFIC; got"
            f" {config.mode.value}.  Use resolve_universal_pyproject for"
            " mode = 'universal'."
        )
        raise UnsupportedModeError(msg)

    if python_version is not None:
        effective_python = python_version
    elif config.requires_python is not None:
        effective_python = _resolve_target_python(config.requires_python)
    else:
        vi = sys.version_info
        effective_python = f"{vi.major}.{vi.minor}.{vi.micro}"

    effective_strategy = (
        resolution_strategy if resolution_strategy is not None else config.resolution
    )

    requirements = read_pyproject_dependencies(path)
    requirements.extend(_load_group_requirements(path, groups))
    requirements.extend(_load_extra_requirements(path, extras))
    marker_environment = _build_marker_environment(
        python_version=effective_python,
        overrides=config.marker_environment,
    )
    if len(groups) > 1:
        _check_group_disjointness(
            _load_group_requirements_by_group(path, groups),
            environment=marker_environment,
        )
    resolver_requirements, root_extras = _build_resolver_inputs(
        requirements, config, environment=marker_environment
    )
    resolver_constraints = _build_constraints(config, environment=marker_environment)
    direct_packages = frozenset(
        name for name in resolver_requirements if split_extra(name)[1] is None
    )

    with FetchCoordinator(
        transport,
        indexes=list(config.indexes),
        cache_dir=cache_dir,
        offline=offline,
        index_overrides=list(config.index_overrides),
        marker_environment=marker_environment,
    ) as coordinator:
        provider = Provider(
            coordinator,
            python_version=effective_python,
            root_requirements=resolver_requirements,
            uploaded_prior_to=config.uploaded_prior_to,
            uploaded_prior_to_overrides=config.uploaded_prior_to_overrides or None,
            root_extras=root_extras,
            dist_policy=config.dist_policy,
            dist_policy_overrides=config.dist_policy_overrides or None,
            build_policy=config.build_policy,
            build_policy_overrides=dict(config.build_policy_overrides) or None,
            vcs_config=config.vcs,
            marker_environment=dict(config.marker_environment) or None,
            local_sources=list(config.local_sources) or None,
            vcs_sources=list(config.vcs_sources) or None,
            vcs_cache_dir=cache_dir / "vcs" if cache_dir is not None else None,
            build_config=config,
            resolution_strategy=effective_strategy,
            direct_packages=direct_packages,
            preferences=seed_pins,
        )

        resolver: Resolver[str, Version] = Resolver(
            provider, range_type=VersionRange, root_version="0"
        )
        try:
            raw = resolver.resolve(
                resolver_requirements, constraints=resolver_constraints
            )
        except ResolutionError as exc:
            _augment_resolution_error(exc, provider)
            raise
        pins = {k: v for k, v in raw.items() if split_extra(k)[1] is None}
        lock_input = build_lock_input_from_provider(
            provider,
            pins,
            requires_python=config.requires_python,
            extras=tuple(extras),
            dependency_groups=tuple(groups),
            default_groups=config.default_groups,
            indexes=config.indexes,
        )
        return ResolutionResult(pins=pins, lock_input=lock_input)


def _load_group_requirements(path: Path, selected: Sequence[str]) -> list[Requirement]:
    """Read [dependency-groups] from ``path`` and expand ``selected``."""
    if not selected:
        return []
    groups = read_pyproject_groups(path)
    if not groups:
        msg = (
            "groups requested but [dependency-groups] is missing from"
            f" {path}: {sorted(selected)!r}"
        )
        raise LookupError(msg)
    return resolve_groups_to_requirements(groups, selected)


def _load_group_requirements_by_group(
    path: Path, selected: Sequence[str]
) -> dict[str, list[Requirement]]:
    """Expand ``selected`` groups, keyed by group name.

    Like :func:`_load_group_requirements`, but keeps each group's
    requirements separate so a caller can name the source group.
    """
    if not selected:
        return {}
    groups = read_pyproject_groups(path)
    if not groups:
        msg = (
            "groups requested but [dependency-groups] is missing from"
            f" {path}: {sorted(selected)!r}"
        )
        raise LookupError(msg)
    return {
        group: resolve_groups_to_requirements(groups, [group]) for group in selected
    }


def _group_package_ranges(
    requirements: list[Requirement], environment: dict[str, str]
) -> tuple[dict[str, VersionRange], dict[str, list[str]]]:
    """Fold one group's direct requirements into per-package ranges.

    Mirrors :func:`_build_resolver_inputs` (marker filtering,
    canonicalisation, intersection); URL requirements are skipped. Also
    returns the requirement strings per package, for the conflict message.
    """
    ranges: dict[str, VersionRange] = {}
    sources: dict[str, list[str]] = defaultdict(list)
    for req in requirements:
        if req.marker is not None and not req.marker.evaluate(environment):
            continue
        if req.url is not None:
            continue
        name = str(canonicalize_name(req.name))
        previous = ranges.get(name, VersionRange.full())
        ranges[name] = previous & req.specifier.to_range()
        sources[name].append(str(req))
    return ranges, sources


@dataclass(frozen=True, slots=True)
class _GroupConflict:
    """One direct group-vs-group conflict on a single package.

    Group names are stored sorted so the conflict has a stable identity.
    """

    left_group: str
    right_group: str
    package: str
    left_req: str
    right_req: str


def _find_group_conflicts(
    per_group: Mapping[str, list[Requirement]],
    environment: dict[str, str],
) -> list[_GroupConflict]:
    """Return the direct group-vs-group conflicts under ``environment``.

    Only direct conflicts are caught; one that emerges through a shared
    transitive dependency falls through to the resolver. The result is
    sorted by ``(left_group, right_group, package)``.
    """
    # Invert to: package -> the groups that name it directly, each with
    # its folded range and the requirement strings behind it. Visiting
    # groups in sorted order makes every pair below read low-to-high.
    requirers: defaultdict[str, list[tuple[str, VersionRange, list[str]]]] = (
        defaultdict(list)
    )
    for group in sorted(per_group):
        ranges, sources = _group_package_ranges(per_group[group], environment)
        for package, package_range in ranges.items():
            requirers[package].append((group, package_range, sources[package]))

    # Two groups conflict on a package when their ranges cannot both
    # hold. Only groups that share a package are paired, so the work
    # scales with real overlap rather than with the number of groups.
    conflicts: list[_GroupConflict] = []
    for package, group_ranges in requirers.items():
        for left, right in itertools.combinations(group_ranges, 2):
            left_group, left_range, left_sources = left
            right_group, right_range, right_sources = right
            if (left_range & right_range).is_empty:
                conflicts.append(
                    _GroupConflict(
                        left_group=left_group,
                        right_group=right_group,
                        package=package,
                        left_req=", ".join(left_sources),
                        right_req=", ".join(right_sources),
                    )
                )

    # Sort so the first conflict, which specific mode reports, is stable.
    conflicts.sort(key=lambda c: (c.left_group, c.right_group, c.package))
    return conflicts


def _check_group_disjointness(
    per_group: Mapping[str, list[Requirement]],
    *,
    environment: dict[str, str],
) -> None:
    """Raise on the first direct conflict between two groups, naming them.

    Single-environment wrapper over :func:`_find_group_conflicts`.
    """
    for conflict in _find_group_conflicts(per_group, environment):
        msg = (
            f"Dependency groups {conflict.left_group!r} and"
            f" {conflict.right_group!r} conflict on {conflict.package!r}: group"
            f" {conflict.left_group!r} requires {conflict.left_req} but group"
            f" {conflict.right_group!r} requires {conflict.right_req}."
        )
        raise ResolutionError(msg)


def _check_group_disjointness_across_tuples(
    per_group: Mapping[str, list[Requirement]],
    tuples: Sequence[MatrixTuple],
) -> None:
    """Raise if a direct group conflict holds on any targeted tuple.

    Checks each tuple's marker environment, aggregates conflicts by
    identity, and names the affected tuples. A no-op below two groups.
    """
    affected: dict[_GroupConflict, set[str]] = defaultdict(set)
    for t in tuples:
        for conflict in _find_group_conflicts(per_group, t.environment):
            affected[conflict].add(t.label)
    if not affected:
        return
    clauses: list[str] = []
    for conflict in sorted(
        affected,
        key=lambda c: (c.left_group, c.right_group, c.package),
    ):
        labels = ", ".join(sorted(affected[conflict]))
        clauses.append(
            f"Dependency groups {conflict.left_group!r} and"
            f" {conflict.right_group!r} conflict on {conflict.package!r} for"
            f" tuple(s) {labels}: group {conflict.left_group!r} requires"
            f" {conflict.left_req} but group {conflict.right_group!r} requires"
            f" {conflict.right_req}."
        )
    raise ResolutionError("; ".join(clauses))


def _load_extra_requirements(path: Path, selected: Sequence[str]) -> list[Requirement]:
    """Read [project.optional-dependencies] from ``path`` and expand ``selected``.

    Self-references (``{project_name}[a, b]`` inside an extra's
    contents) are expanded transitively so that the third-party deps
    they ultimately reach reach the resolver as root requirements.
    See :func:`expand_self_extras` for the rationale.
    """
    if not selected:
        return []
    optional = read_pyproject_optional_dependencies(path)
    if not optional:
        msg = (
            "extras requested but [project.optional-dependencies] is"
            f" missing from {path}: {sorted(selected)!r}"
        )
        raise LookupError(msg)
    project_name = read_pyproject_name(path)
    expanded = expand_self_extras(optional, project_name, selected)
    return select_optional_dependencies(optional, expanded)


def _augment_resolution_error(exc: ResolutionError, provider: Provider) -> None:
    """Append per-package NO_VERSIONS diagnostics to ``exc`` in-place.

    Walks the derivation tree carried on the exception, collects
    every package that appears in a NO_VERSIONS clause, and looks up
    the provider-side reason for each.  When at least one reason is
    available, rewrites the exception's args so that ``str(exc)``
    surfaces the diagnostics alongside the original derivation tree.
    """
    if exc.incompatibility is None:
        return
    packages: list[str] = []
    seen: set[str] = set()
    for package in _walk_no_versions_packages(exc.incompatibility):
        if package in seen:
            continue
        seen.add(package)
        packages.append(package)
    hints: list[str] = []
    for package in packages:
        reason = provider.get_no_versions_reason(package)
        if reason is not None:
            hints.append(f"{package}: {reason}")
    if not hints:
        return
    base = str(exc)
    augmented = base + "\n\nDiagnostics:\n  - " + "\n  - ".join(hints)
    exc.args = (augmented,)


def _walk_no_versions_packages(
    incompatibility: Incompatibility[Any, Any],
) -> list[str]:
    """Return package names from every NO_VERSIONS clause in the tree."""
    out: list[str] = []
    seen_ids: set[int] = set()

    def visit(node: Incompatibility[Any, Any]) -> None:
        if id(node) in seen_ids:
            return
        seen_ids.add(id(node))
        if node.cause is IncompatibilityCause.NO_VERSIONS:
            for term in node.terms:
                pkg = term.package
                if isinstance(pkg, str):
                    out.append(pkg)
        if node.cause_left is not None:
            visit(node.cause_left)
        if node.cause_right is not None:
            visit(node.cause_right)

    visit(incompatibility)
    return out


def _build_resolver_inputs(
    requirements: list[Requirement],
    config: NabProjectConfig,
    *,
    environment: dict[str, str],
) -> tuple[dict[str, VersionRange], set[tuple[str, str]]]:
    """Convert PEP 508 ``Requirement`` objects to the resolver's input shape.

    Requirements whose PEP 508 marker evaluates to ``False`` under
    ``environment`` are skipped, matching pip/uv's root-requirement
    handling.  Repeated package names are intersected into one range;
    an empty intersection raises :class:`ResolutionError`.
    """
    resolver_requirements: dict[str, VersionRange] = {}
    sources: defaultdict[str, list[str]] = defaultdict(list)
    root_extras: set[tuple[str, str]] = set()
    for req in requirements:
        if req.marker is not None and not req.marker.evaluate(environment):
            continue
        if req.url is not None:
            admit_vcs_url(req.url, config.vcs)
            msg = (
                f"VCS requirement admitted by policy but resolver path is not"
                f" implemented: {req.name} @ {req.url}"
            )
            raise NotImplementedError(msg)
        name = str(canonicalize_name(req.name))
        previous = resolver_requirements.get(name, VersionRange.full())
        resolver_requirements[name] = previous & req.specifier.to_range()
        sources[name].append(str(req))
        for extra in req.extras:
            extra_key = join_extra(name, extra)
            resolver_requirements[extra_key] = VersionRange.full()
            _, normalized_extra = split_extra(extra_key)
            assert normalized_extra is not None  # join_extra always sets one
            root_extras.add((name, normalized_extra))
    raise_for_unsatisfiable(resolver_requirements, sources, kind="requirement")
    return resolver_requirements, root_extras


def _build_marker_environment(
    *,
    python_version: str,
    overrides: Mapping[str, str],
) -> dict[str, str]:
    """Return the merged PEP 508 environment used for root-marker evaluation.

    Mirrors :class:`Provider`: defaults from
    :func:`default_environment`, then ``python_version`` /
    ``python_full_version`` rewritten from the effective Python, then
    user overrides.
    """
    env: dict[str, str] = {
        key: value
        for key, value in default_environment().items()
        if isinstance(value, str)
    }
    with contextlib.suppress(InvalidVersion):
        env.update(python_axis_environment(python_version))
    env.update(overrides)
    return env


def resolve_universal_pyproject(  # noqa: PLR0913 - the surface mirrors the CLI; bundling into a config object would hide it
    path: Path,
    *,
    config: NabProjectConfig | None = None,
    cache_dir: Path | None = None,
    transport: AsyncHttpTransport | None = None,
    offline: bool = False,
    groups: Sequence[str] = (),
    extras: Sequence[str] = (),
    resolution_strategy: ResolutionStrategy | None = None,
    seed_pins: dict[str, Version] | None = None,
) -> UniversalResult:
    """Run a universal resolve for the project at ``path``.

    Reads ``[project].dependencies`` as the requirement list and the
    matrix declaration from ``[tool.nab.matrix]``.  Requires
    ``config.mode == ResolveMode.UNIVERSAL``.

    ``groups`` names PEP 735 dependency groups; ``extras`` names
    entries from ``[project.optional-dependencies]``.  Both are
    folded into every per-tuple resolve.  The CLI passes the
    selections through to ``merge_universal_lock_inputs`` so the
    lockfile records what produced the pin set.
    """
    if config is None:
        config = read_pyproject_config(path)
    if config.mode is not ResolveMode.UNIVERSAL:
        msg = (
            f"resolve_universal_pyproject requires mode = 'universal'; got"
            f" {config.mode.value}.  Set [tool.nab].mode = 'universal'."
        )
        raise UnsupportedModeError(msg)
    if config.matrix is None:  # pragma: no cover - guarded at config parse
        msg = "mode = 'universal' requires a [tool.nab.matrix] table"
        raise UnsupportedModeError(msg)

    requirements = read_pyproject_dependencies(path)
    requirements.extend(_load_group_requirements(path, groups))
    requirements.extend(_load_extra_requirements(path, extras))
    requirement_strings = [str(r) for r in requirements]
    matrix = Matrix(
        python=config.matrix.python,
        platforms=config.matrix.platforms,
        python_order=config.matrix.python_order,
        python_patches=(
            dict(config.matrix.python_patches)
            if config.matrix.python_patches is not None
            else None
        ),
        implementations=config.matrix.implementations,
    )
    if len(groups) > 1:
        _check_group_disjointness_across_tuples(
            _load_group_requirements_by_group(path, groups),
            matrix.expand(),
        )
    effective_strategy = (
        resolution_strategy if resolution_strategy is not None else config.resolution
    )
    return resolve_universal(
        matrix=matrix,
        requirements=requirement_strings,
        transport=transport,
        offline=offline,
        constraints=list(config.constraints) or None,
        cache_dir=cache_dir,
        uploaded_prior_to=config.uploaded_prior_to,
        uploaded_prior_to_overrides=config.uploaded_prior_to_overrides or None,
        dist_policy=config.dist_policy,
        dist_policy_overrides=config.dist_policy_overrides or None,
        build_policy=config.build_policy,
        build_policy_overrides=dict(config.build_policy_overrides) or None,
        vcs_config=config.vcs,
        local_sources=list(config.local_sources) or None,
        vcs_sources=list(config.vcs_sources) or None,
        vcs_cache_dir=cache_dir / "vcs" if cache_dir is not None else None,
        build_config=config,
        indexes=list(config.indexes),
        index_overrides=list(config.index_overrides) or None,
        resolution_strategy=effective_strategy.value,
        preferences=seed_pins,
    )


_PYTHON_CANDIDATE_MAJORS = (3, 4)
_PYTHON_CANDIDATE_MINORS = range(30)
_PYTHON_CANDIDATE_PATCHES = range(30)


def _resolve_target_python(specifier: str) -> str:
    """Pick a concrete Python version that satisfies ``specifier``.

    ``specifier`` is a PEP 440 specifier set (already validated at
    config-parse time, see :func:`_parse_requires_python`).  The
    resolve target is the lowest enumerated ``M.N.P`` release that the
    specifier admits: deterministic regardless of host, and matches
    the user's written intent ("lock for >=3.13" -> "use 3.13.0
    markers"; "lock for ==3.10.5" -> "use 3.10.5 markers").

    Falls back to the host Python when no enumerated candidate
    satisfies (e.g. an open-ended ``<X.Y`` specifier with no lower
    bound, or a range that admits only versions outside the candidate
    grid).  The host fallback warns via the logger so the operator can
    notice when their lockfile's ``requires-python`` field implies a
    target the resolve could not actually impersonate.
    """
    spec_set = SpecifierSet(specifier)

    # M.N.0 first so >=3.13 resolves to 3.13.0 (not 3.13.<something>).
    # filter() preserves input order, so the first match is the lowest.
    candidates: list[Version] = []
    for major in _PYTHON_CANDIDATE_MAJORS:
        for minor in _PYTHON_CANDIDATE_MINORS:
            candidates.append(Version(f"{major}.{minor}.0"))
            for patch in _PYTHON_CANDIDATE_PATCHES:
                if patch == 0:
                    continue
                candidates.append(Version(f"{major}.{minor}.{patch}"))

    matches = [str(v) for v in spec_set.filter(candidates)]
    if matches:
        return matches[0]
    vi = sys.version_info
    host = f"{vi.major}.{vi.minor}.{vi.micro}"
    _logger.warning(
        "requires-python = %r matches no enumerated CPython release;"
        " falling back to host Python %s for the resolve target",
        specifier,
        host,
    )
    return host


def _build_constraints(
    config: NabProjectConfig, *, environment: dict[str, str]
) -> dict[str, VersionRange]:
    """Parse constraint strings from config into resolver-input ranges.

    Marker-gated constraints whose marker is False in ``environment`` are
    dropped, matching the universal path and pip. Repeated package names are
    intersected into one range; an empty intersection raises
    :class:`ResolutionError`.
    """
    out: dict[str, VersionRange] = {}
    sources: defaultdict[str, list[str]] = defaultdict(list)
    for cstr in config.constraints:
        req = Requirement(cstr)
        if req.marker is not None and not req.marker.evaluate(environment):
            continue
        if req.extras:
            msg = f"Constraints cannot have extras: {cstr}"
            raise ConfigError(msg)
        if req.url is not None:
            admit_vcs_url(req.url, config.vcs)
            msg = (
                f"VCS constraint admitted by policy but resolver path is not"
                f" implemented: {req.name} @ {req.url}"
            )
            raise NotImplementedError(msg)
        name = str(canonicalize_name(req.name))
        out[name] = out.get(name, VersionRange.full()) & req.specifier.to_range()
        sources[name].append(cstr)
    raise_for_unsatisfiable(out, sources, kind="constraint")
    return out
