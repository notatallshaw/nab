"""Read ``[tool.nab]`` from a ``pyproject.toml`` into a typed config.

``[tool.nab]`` holds the keys that decide what a project resolves.  A
``--project-<key>`` flag overrides a scalar or list key for one run, and a
``--project-<table>-<key>`` flag replaces one key inside a table.  This
module owns the project side.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import tomli

from nab_project import toml_io
from nab_project.build_policy import enforce_build_policy_for_targets
from nab_project.conflicts import (
    MIN_ENGAGED_MEMBERS,
    ConflictKind,
    ConflictPolicy,
    ConflictSet,
    conflict_exclusion_groups,
)
from nab_project.inputs import ResolveInputs
from nab_project.paths import realpath
from nab_project.value import ValueType
from nab_project.workspace import (
    WorkspaceConfig,
    discover_workspace_root,
    merge_workspace_local_sources,
    read_workspace_members,
    workspace_local_sources,
)
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import ConfigError
from nab_provider.policy import (
    ArchiveSource,
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    LocalSource,
    ResolutionStrategy,
    ResolveMode,
    VcsSource,
)
from nab_provider.records import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, IndexConfig
from nab_provider.target import (
    PLATFORM_MARKERS,
    ResolveTarget,
    check_free_threaded,
    host_environment,
    names_a_micro,
    python_axis_environment,
)
from nab_provider.vcs_admission import VcsConfig, VcsPolicy

from .hooks import resolve_anchor
from .ladder import (
    EffectiveValue,
    SourceKind,
    SourceRoots,
    build_cli_layer,
    discover_layers,
    pyproject_registry_keys,
    read_env_layer,
    reject_user_keys_in_pyproject,
    resolve_config,
    tool_nab_section,
)
from .values import (
    ENVIRONMENT_KEYS,
    MatrixConfig,
    check_package_override_overlap,
    environment_platform_spec,
    matrix_from_config,
    parse_requires_python,
    validate_environment_values,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from nab_provider.overrides import IndexOverride, PackageOverride
    from nab_provider.tags import PlatformSpec


__all__ = [
    "ConfigError",
    "EnvironmentConfig",
    "NabProjectConfig",
    "ResolveMode",
    "plan_targets",
    "read_pyproject_config",
]


# How a ``requires-python`` declaration is named back to the user.  Both
# tables hold a key of that name, so each names its own.
_TOOL_NAB_REQUIRES_PYTHON = "[tool.nab] requires-python"
_PROJECT_REQUIRES_PYTHON = "[project] requires-python"


_DEFAULT_INDEXES = (IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),)
_NO_INDEX_OVERRIDES: Mapping[str, IndexOverride] = MappingProxyType({})
_DEFAULT_VCS = VcsConfig()


class EnvironmentConfig(ValueType):
    """The single environment ``[tool.nab.environment]`` declares.

    The axes a target is made of, the same ones a matrix entry carries.
    An unset axis takes the host's value, so an empty table is the host
    and a table naming only ``python`` is the host machine running
    another Python.

    ``platform`` is the same :class:`~nab_provider.tags.PlatformSpec` a
    ``matrix.platforms`` entry parses to, so the wheel-tag knobs (the libc
    family, the libc and macOS the lock must run on, the kernel
    marker values, the free-threaded build) are declarable here too.
    """

    __slots__ = __match_args__ = ("python", "platform", "implementation")

    python: str | None
    platform: PlatformSpec | None
    implementation: str | None

    def __init__(
        self,
        python: str | None = None,
        platform: PlatformSpec | None = None,
        implementation: str | None = None,
    ) -> None:
        """Record the axes the table sets; an unset one is ``None``."""
        self.python = python
        self.platform = platform
        self.implementation = implementation


class NabProjectConfig(ValueType):
    """Everything ``[tool.nab]`` says about how to resolve this project."""

    __slots__ = __match_args__ = (
        "mode",
        "constraints",
        "default_groups",
        "base_group",
        "build_group",
        "requires_python",
        "requires_python_source",
        "uploaded_prior_to",
        "dist_policy",
        "build_policy",
        "build_requires_depth",
        "trust_unverified_sdist_deps",
        "environment",
        "indexes",
        "vcs",
        "local_sources",
        "vcs_sources",
        "archive_sources",
        "matrix",
        "resolution",
        "decision_order",
        "workspace",
        "conflicts",
        "package_overrides",
        "index_overrides",
        "workspace_member_names",
    )

    mode: ResolveMode
    constraints: tuple[str, ...]
    default_groups: tuple[str, ...]
    base_group: str | None
    build_group: str | None
    # The project's declared Python support range: recorded as the lock's
    # top-level ``requires-python`` and checked against the resolve target.
    # It does not choose the target; ``environment`` does.
    requires_python: str | None
    # The surface ``requires_python`` was read from, named by the error when
    # the declaration excludes a target.
    requires_python_source: str
    uploaded_prior_to: datetime | None
    dist_policy: DistPolicy
    build_policy: BuildPolicy
    # How many build environments may be opened beneath the first one, to
    # build a build requirement that publishes no wheel this host installs.
    build_requires_depth: int
    trust_unverified_sdist_deps: bool
    # The declared resolve environment from ``[tool.nab.environment]``, or
    # ``None`` for the host.  Mutually exclusive with ``matrix``.
    environment: EnvironmentConfig | None
    indexes: tuple[IndexConfig, ...]
    vcs: VcsConfig
    local_sources: tuple[LocalSource, ...]
    vcs_sources: tuple[VcsSource, ...]
    archive_sources: tuple[ArchiveSource, ...]
    matrix: MatrixConfig | None
    resolution: ResolutionStrategy
    decision_order: DecisionOrder
    workspace: WorkspaceConfig | None
    conflicts: tuple[ConflictSet, ...]
    # Per-package overrides from ``[tool.nab.packages.<name>]`` and
    # ``[[tool.nab.package-rules]]``, one per requirement, in declared
    # order.  Version-scoped: a policy field applies only to candidate
    # versions inside its requirement's range.  Routing entries (those
    # that set ``index``) are also projected into coordinator routes by
    # ``nab_project.fetch.index_routes``.
    package_overrides: tuple[PackageOverride, ...]
    # Per-index overrides from ``[tool.nab.index.<name>]``, keyed by
    # declared index name.  Each applies to every package served from
    # that index; no routing, no version scope.
    index_overrides: Mapping[str, IndexOverride]
    # Canonical names of workspace members. Populated by
    # _apply_workspace_discovery; empty otherwise. Distinct from
    # ``local_sources``, which also carries explicit
    # ``[[tool.nab.local-sources]]`` entries.
    workspace_member_names: frozenset[str]

    def __init__(  # noqa: PLR0913 - one keyword per [tool.nab] key
        self,
        *,
        mode: ResolveMode = ResolveMode.SPECIFIC,
        constraints: tuple[str, ...] = (),
        default_groups: tuple[str, ...] = (),
        base_group: str | None = None,
        build_group: str | None = None,
        requires_python: str | None = None,
        requires_python_source: str = _TOOL_NAB_REQUIRES_PYTHON,
        uploaded_prior_to: datetime | None = None,
        dist_policy: DistPolicy = DistPolicy.WHEEL_OR_SDIST,
        build_policy: BuildPolicy = BuildPolicy.BUILD_LOCAL,
        build_requires_depth: int = 0,
        trust_unverified_sdist_deps: bool = False,
        environment: EnvironmentConfig | None = None,
        indexes: tuple[IndexConfig, ...] = _DEFAULT_INDEXES,
        vcs: VcsConfig = _DEFAULT_VCS,
        local_sources: tuple[LocalSource, ...] = (),
        vcs_sources: tuple[VcsSource, ...] = (),
        archive_sources: tuple[ArchiveSource, ...] = (),
        matrix: MatrixConfig | None = None,
        resolution: ResolutionStrategy = ResolutionStrategy.HIGHEST,
        decision_order: DecisionOrder = DecisionOrder.ARRIVAL,
        workspace: WorkspaceConfig | None = None,
        conflicts: tuple[ConflictSet, ...] = (),
        package_overrides: tuple[PackageOverride, ...] = (),
        index_overrides: Mapping[str, IndexOverride] = _NO_INDEX_OVERRIDES,
        workspace_member_names: frozenset[str] = frozenset(),
    ) -> None:
        """Record the settings, each defaulting to what a bare project gets."""
        self.mode = mode
        self.constraints = constraints
        self.default_groups = default_groups
        self.base_group = base_group
        self.build_group = build_group
        self.requires_python = requires_python
        self.requires_python_source = requires_python_source
        self.uploaded_prior_to = uploaded_prior_to
        self.dist_policy = dist_policy
        self.build_policy = build_policy
        self.build_requires_depth = build_requires_depth
        self.trust_unverified_sdist_deps = trust_unverified_sdist_deps
        self.environment = environment
        self.indexes = indexes
        self.vcs = vcs
        self.local_sources = local_sources
        self.vcs_sources = vcs_sources
        self.archive_sources = archive_sources
        self.matrix = matrix
        self.resolution = resolution
        self.decision_order = decision_order
        self.workspace = workspace
        self.conflicts = conflicts
        self.package_overrides = package_overrides
        self.index_overrides = index_overrides
        self.workspace_member_names = workspace_member_names

    def replace(self, **changes: object) -> NabProjectConfig:
        """Return a copy with ``changes`` applied, as ``dataclasses.replace`` would."""
        kept: dict[str, Any] = {
            name: getattr(self, name) for name in self.__match_args__
        }
        kept.update(changes)
        return NabProjectConfig(**kept)

    def resolve_inputs(self) -> ResolveInputs:
        """Return the settings nab-project resolves this project under."""
        return ResolveInputs(
            **{name: getattr(self, name) for name in ResolveInputs.__match_args__}
        )


def read_pyproject_config(
    path: Path,
    *,
    discover_workspace: bool = True,
    anchor: datetime | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> NabProjectConfig:
    """Parse ``[tool.nab]`` from ``path`` into :class:`NabProjectConfig`.

    Returns the default ``NabProjectConfig`` when the table is absent.
    Unknown keys at the top level are rejected so typos fail loud.

    When ``discover_workspace`` is true (the default), the merged
    ``workspace`` table drives discovery and its members resolve against
    the project directory.  A project that declares no workspace of its
    own walks up from ``path`` for the first ancestor project file that
    declares one.  Either way every member is materialised as an
    additional :class:`LocalSource` (explicit
    ``[[tool.nab.local-sources]]`` entries win on collision).  A workspace
    does not change the effective ``build-policy``: a member with dynamic
    metadata needs a build, and under ``never`` it is refused like any
    other source.  Pass ``discover_workspace=False`` to skip discovery;
    useful for tests or for callers that layer their own workspace logic
    on top of a base config.

    The ``[tool.nab]``-config portion is sourced from the registry merged
    ladder (pyproject ``[tool.nab]`` plus a project-dir ``nab.toml``,
    merged by :func:`nab.config.ladder.resolve_config` with its per-key merge,
    cross-file conflict check, and category gate), so a project-dir
    ``nab.toml`` value configures the resolve exactly as the inspector
    reports it.  The
    cross-field transforms (mode/matrix, the build-policy host-build gate,
    universal marker-environment ban, source-name uniqueness, declared
    index references) then run on the merged config; workspace discovery
    runs last.

    ``anchor`` is the timestamp ``P<n>D`` durations resolve against.
    Defaults to ``datetime.now(UTC)`` when not supplied, which gives
    fresh-resolve semantics.  The ``nab lock`` CLI passes the anchor
    captured in any existing lockfile so re-locks reproduce the same
    cutoff for relative durations.

    ``cli_overrides`` carries the ``--project-*`` overrides for the
    PROJECT options that take a CLI flag, keyed by registry key.  They
    layer as the highest-precedence source, so a flag wins over both
    project files; a table key arrives as a
    :class:`~nab.config.subflags.CliTable` and replaces the keys it names
    inside whatever table the files declared.  ``None`` (the default) is a
    file-only resolve, byte-identical to before.
    """
    if anchor is None:
        anchor = datetime.now(timezone.utc)
    pyproject_dir = realpath(path.parent)
    document = _read_pyproject_document(path)
    _reject_unknown_pyproject_keys(document)
    project_requires_python = _read_project_requires_python(document)
    # Point the pyproject root at ``pyproject_dir / path.name`` (not
    # ``realpath(path)``) so the registry's declaring directory is the
    # symlink's own directory, which is what a relative local-source path
    # and the project-dir nab.toml lookup resolve against.  ``open`` still
    # follows the symlink, so the same file is read.
    roots = SourceRoots(project_dir=pyproject_dir, pyproject=pyproject_dir / path.name)
    # Bind the lock anchor so the registry resolves ``P<n>D`` durations
    # (top-level and override-body) against it.  System/user nab.toml and
    # env/CLI carry no PROJECT key, so they are excluded here: this is the
    # file-only project config.
    with resolve_anchor(anchor):
        layers = discover_layers(roots)
        cli_layer = build_cli_layer(cli_overrides or {})
        effective = resolve_config(layers, read_env_layer({}), cli_layer)
    config = _config_from_effective(
        effective,
        anchor=anchor,
        pyproject_dir=pyproject_dir,
        project_requires_python=project_requires_python,
    )
    _validate_configured_groups(
        effective["base-group"], effective["build-group"], document
    )
    if discover_workspace:
        config = _apply_workspace_discovery(
            path, config, declared_in=effective["workspace"].origin.label
        )
    return config


def _config_from_effective(
    effective: Mapping[str, EffectiveValue],
    *,
    anchor: datetime,
    pyproject_dir: Path,
    project_requires_python: str | None = None,
) -> NabProjectConfig:
    """Assemble :class:`NabProjectConfig` from the registry merged ladder.

    Each ``[tool.nab]`` config key is taken from its effective (merged)
    value; the registry has already applied the per-key merge, the
    cross-file conflict rule, and the category gate.  The cross-field
    transforms the single-key rows deliberately defer (mode/matrix mutual
    requirement, declared-index references for routing and per-index
    overrides, the cross-surface package-override overlap, the resolve-target
    plan and the build-policy enforcement it drives, the
    default-groups-vs-conflicts check, and source-name uniqueness) then run
    here over the merged whole.  Workspace discovery is applied by the
    caller afterwards.

    ``project_requires_python`` is ``[project].requires-python``, the
    fallback source for the declaration when ``[tool.nab]`` sets none.
    """
    del anchor  # P<n>D durations are already anchored in the effective map.
    mode_value = effective["mode"]
    matrix_value = effective["matrix"]
    mode: ResolveMode = mode_value.value
    matrix: MatrixConfig | None = matrix_value.value
    if mode is ResolveMode.UNIVERSAL and matrix is None:
        if mode_value.origin.kind is SourceKind.CLI:
            msg = (
                "--project-mode universal needs a matrix: pass"
                " --project-matrix-python and --project-matrix-platforms, add"
                " [tool.nab.matrix] to the project's pyproject.toml, or a"
                " top-level [matrix] table to the project's nab.toml, or drop"
                " --project-mode universal."
            )
        else:
            # PROJECT_TOML is the project-dir nab.toml, whose keys are top-level.
            table = (
                "top-level [matrix] table in nab.toml"
                if mode_value.origin.kind is SourceKind.PROJECT_TOML
                else "[tool.nab.matrix] table in pyproject.toml"
            )
            msg = (
                f"mode = 'universal' requires a {table} declaring python and platforms"
            )
        raise ConfigError(msg)
    if mode is ResolveMode.SPECIFIC and matrix is not None:
        if not mode_value.origin.outranks(matrix_value.origin):
            raise ConfigError(_specific_mode_message(matrix_value))
        # A higher-precedence source (--project-mode) selected a
        # single-environment resolve, so the matrix it shadows does not apply.
        matrix = None

    dist_policy, trust_unverified = effective["dist-policy"].value
    indexes: tuple[IndexConfig, ...] = effective["indexes"].value
    declared_index_names = frozenset(i.name for i in indexes)

    package_overrides = (
        *effective["packages"].value,
        *effective["package-rules"].value,
    )
    check_package_override_overlap(package_overrides)
    _validate_routes_declared(package_overrides, declared_index_names)

    index_overrides: Mapping[str, IndexOverride] = effective["index"].value
    _validate_index_overrides_declared(index_overrides, declared_index_names)

    environment = _environment_from_effective(effective)
    if matrix is not None and environment is not None:
        raise ConfigError(
            _matrix_environment_message(matrix_value, effective["environment"])
        )

    targets = _plan_targets(matrix, environment)
    build_policy = enforce_build_policy_for_targets(
        targets=targets,
        build_policy=effective["build-policy"].value,
        build_policy_set=effective["build-policy"].origin.kind
        is not SourceKind.DEFAULT,
        package_overrides=package_overrides,
        index_overrides=index_overrides,
    )

    requires_python = effective["requires-python"].value
    requires_python_source = _TOOL_NAB_REQUIRES_PYTHON
    if requires_python is None and project_requires_python is not None:
        requires_python = project_requires_python
        requires_python_source = _PROJECT_REQUIRES_PYTHON

    default_groups = effective["default-groups"].value
    conflicts = effective["conflicts"].value
    _validate_default_groups_against_conflicts(
        default_groups, conflicts, effective["base-group"].value
    )
    _validate_configured_conflict_sets(
        conflicts, effective["base-group"].value, effective["build-group"].value
    )

    local_sources = effective["local-sources"].value
    vcs_sources = effective["vcs-sources"].value
    archive_sources = effective["archive-sources"].value
    _reject_duplicate_source_names(local_sources, vcs_sources, archive_sources)

    vcs_config = effective["vcs"].value
    _reject_vcs_sources_under_block(effective["vcs-sources"], effective["vcs"])

    del pyproject_dir  # paths were resolved per-layer by the registry.
    return NabProjectConfig(
        mode=mode,
        constraints=effective["constraints"].value,
        default_groups=default_groups,
        base_group=effective["base-group"].value,
        build_group=effective["build-group"].value,
        requires_python=requires_python,
        requires_python_source=requires_python_source,
        uploaded_prior_to=effective["uploaded-prior-to"].value,
        dist_policy=dist_policy,
        build_policy=build_policy,
        build_requires_depth=effective["build-requires-depth"].value,
        trust_unverified_sdist_deps=trust_unverified,
        environment=environment,
        indexes=indexes,
        vcs=vcs_config,
        local_sources=local_sources,
        vcs_sources=vcs_sources,
        archive_sources=archive_sources,
        matrix=matrix,
        resolution=effective["resolution"].value,
        decision_order=effective["decision-order"].value,
        workspace=effective["workspace"].value,
        conflicts=conflicts,
        package_overrides=package_overrides,
        index_overrides=index_overrides,
    )


def _declared_by(ev: EffectiveValue) -> tuple[str, ...]:
    """Return the flags that declared this key, empty for a file source."""
    if ev.cli_table is None:
        return ()
    return tuple(key.flag for key in ev.cli_table.keys)


def _declares(flags: tuple[str, ...]) -> str:
    """Agree the verb with the subject: several flags declare, one name declares."""
    return "declare" if len(flags) > 1 else "declares"


def _specific_mode_message(matrix_value: EffectiveValue) -> str:
    """Say why a declared matrix is refused while mode is 'specific'."""
    flags = _declared_by(matrix_value)
    if flags:
        named = " and ".join(flags)
        return (
            f"{named} {_declares(flags)} a matrix but mode is 'specific';"
            " pass --project-mode universal as well, or drop the matrix flags."
        )
    declared = (
        "[matrix] in nab.toml"
        if matrix_value.origin.kind is SourceKind.PROJECT_TOML
        else "[tool.nab.matrix] in pyproject.toml"
    )
    return (
        f"{declared} is set but mode is 'specific'; set mode = 'universal' to"
        " resolve for every target the matrix declares, or remove the table."
        " The multi-target lockfile format universal mode produces is"
        " experimental."
    )


def _matrix_environment_message(
    matrix_value: EffectiveValue, environment_value: EffectiveValue
) -> str:
    """Say why a matrix and a declared environment cannot both stand."""
    matrix_flags = _declared_by(matrix_value)
    environment_flags = _declared_by(environment_value)
    if not matrix_flags and not environment_flags:
        return (
            "[tool.nab.matrix] and [tool.nab.environment] cannot both be set:"
            " the matrix declares one environment per tuple, so a single"
            " declared environment would contradict it.  Drop one."
        )
    matrix = " and ".join(matrix_flags) or "[tool.nab.matrix]"
    environment = " and ".join(environment_flags) or "[tool.nab.environment]"
    return (
        f"{matrix} {_declares(matrix_flags)} a matrix and"
        f" {environment} {_declares(environment_flags)} one environment;"
        " the matrix declares one environment per tuple, so the two"
        " contradict.  Drop one."
    )


def _validate_routes_declared(
    package_overrides: tuple[PackageOverride, ...],
    declared_index_names: frozenset[str],
) -> None:
    """Reject a routing override that names an index not in ``indexes``.

    A per-package surface is parsed without the declared index set, so the
    route-points-at-a-real-index check runs here, after the index list is
    known.  The error names the surface the route was declared on
    (``packages.'<name>'`` or ``package-rules[N]``).
    """
    for pkg_override in package_overrides:
        route = pkg_override.index
        if route is not None and route not in declared_index_names:
            valid = sorted(declared_index_names)
            msg = (
                f"{pkg_override.source_label}.index routes to undeclared index"
                f" {route!r}; declared indexes are {valid!r}"
            )
            raise ConfigError(msg)


def _validate_index_overrides_declared(
    index_overrides: Mapping[str, IndexOverride],
    declared_index_names: frozenset[str],
) -> None:
    """Reject a ``[tool.nab.index.<name>]`` key naming an undeclared index.

    The registry parses this surface with ``declared_index_names=None``, so
    the cross-key check runs post-merge with the single-file message.
    """
    for name in index_overrides:
        if name not in declared_index_names:
            valid = sorted(declared_index_names)
            msg = (
                f"index.{name} names undeclared index {name!r};"
                f" declared indexes are {valid!r}"
            )
            raise ConfigError(msg)


_logger = logging.getLogger(__name__)


def _apply_workspace_discovery(
    path: Path, config: NabProjectConfig, *, declared_in: str
) -> NabProjectConfig:
    """Materialise the workspace members as local sources.

    The project's own ``workspace`` table wins, whichever project file
    declared it (``declared_in`` names that file), and its members
    resolve against the project directory.  A project that declares none
    walks up for an ancestor project file that does, so ``nab lock
    <member>`` still resolves against the workspace root.
    """
    if config.workspace is not None:
        discovered = workspace_local_sources(
            config.workspace.members,
            root_dir=realpath(path.parent),
            declared_in=declared_in,
        )
    else:
        root_file = discover_workspace_root(path)
        if root_file is None:
            return config
        discovered = read_workspace_members(root_file)

    if not discovered:
        return config

    merged = merge_workspace_local_sources(config.local_sources, discovered)
    _reject_duplicate_source_names(merged, config.vcs_sources, config.archive_sources)
    explicit_names = {canonicalize_name(src.name) for src in config.local_sources}
    return config.replace(
        local_sources=merged,
        workspace_member_names=frozenset(
            canonicalize_name(src.name)
            for src in discovered
            if canonicalize_name(src.name) not in explicit_names
        ),
    )


def _read_pyproject_document(path: Path) -> dict[str, Any]:
    """Parse ``path``, reporting an unreadable or malformed file as ``ConfigError``."""
    try:
        return toml_io.load_path(path)
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise ConfigError(msg) from exc


def _reject_unknown_pyproject_keys(document: Mapping[str, Any]) -> None:
    """Reject a USER-scope or unknown key in pyproject ``[tool.nab]``.

    Run before the registry merge so the resolve reports a typo'd
    ``[tool.nab]`` key and a USER-scope key set in pyproject with the
    established messages.  A USER-scope option (``offline``, ``cache-dir``)
    surfaces the category error; an unknown key fails loud rather than being
    silently dropped.
    """
    raw = tool_nab_section(document)
    if not isinstance(raw, dict):
        msg = f"[tool.nab] must be a table, got {type(raw).__name__}"
        raise ConfigError(msg)
    # Parser fold: a USER-scope registry option in pyproject [tool.nab]
    # surfaces the registry category error before the generic unknown-key
    # error below.
    reject_user_keys_in_pyproject(raw)
    known = pyproject_registry_keys()
    unknown = sorted(set(raw) - known)
    if unknown:
        msg = f"unknown [tool.nab] keys: {unknown!r}; expected one of {sorted(known)!r}"
        raise ConfigError(msg)


def plan_targets(config: NabProjectConfig) -> tuple[ResolveTarget, ...]:
    """Return every environment ``config`` resolves against, in matrix order.

    The host is the target unless the project says otherwise: a declared
    matrix expands to one target per tuple, ``[tool.nab.environment]``
    names a single target (declared when it moves the platform axis, the
    host machine on another Python when it names only ``python``), and a
    project that declares neither resolves against the running interpreter,
    like pip.

    The ``requires-python`` declaration and the free-threaded floor are
    checked here rather than at parse time because ``--python`` moves the
    target after the config is read, and it is the flag that rescues a
    project whose declaration excludes the host.

    Every target is checked, matrix included: the lock records the
    declaration at top level and the targets in ``environments``, so a
    target the declaration excludes would be a lock that contradicts itself
    and that a PEP 751 installer refuses.
    """
    targets = _plan_targets(config.matrix, config.environment)

    if config.environment is not None:
        _check_free_threaded_environment(
            config.environment, (targets[0].python_version,)
        )

    for target in targets:
        _check_requires_python_admits_target(
            config.requires_python,
            target,
            source=config.requires_python_source,
            matrix=config.matrix is not None,
        )
    return targets


def _plan_targets(
    matrix: MatrixConfig | None, environment: EnvironmentConfig | None
) -> tuple[ResolveTarget, ...]:
    """Plan the targets from the two declaring surfaces, pre-assembly.

    Takes the pieces rather than a :class:`NabProjectConfig` so the config
    parse can plan (and enforce the build policy) while it is still
    assembling one.
    """
    if matrix is not None:
        return tuple(matrix_from_config(matrix).expand())
    if environment is None:
        return (ResolveTarget.for_host(),)
    if environment.platform is not None:
        return (_declared_target(environment),)
    # An implementation without a platform is rejected at parse, so a
    # remaining environment declares the python axis and nothing else.
    assert environment.python is not None
    return (ResolveTarget.for_host_python(environment.python),)


def _declared_target(environment: EnvironmentConfig) -> ResolveTarget:
    """Build the one target ``[tool.nab.environment]`` declares.

    The platform is named, so the target's markers and wheel tags are
    synthesized from it rather than read off the host.  An unset ``python``
    takes the host's release, and an unset ``implementation`` is CPython,
    matching the matrix default.  A python naming a point inside its minor
    (``"3.12.0"``, ``"3.14rc1"``) is pinned whole; a bare ``"3.12"`` resolves
    as a micro interval.
    """
    assert environment.platform is not None  # the caller checked
    python = environment.python or host_environment()["python_full_version"]
    axis = python_axis_environment(python)
    implementation = environment.implementation or "cpython"

    _check_free_threaded_environment(
        environment, (axis["python_version"],) if environment.python else ()
    )

    return ResolveTarget.for_declared(
        python_version=axis["python_version"],
        spec=environment.platform,
        implementation=implementation,
        python_full_version=(
            axis["python_full_version"] if names_a_micro(Version(python)) else None
        ),
    )


def _check_free_threaded_environment(
    environment: EnvironmentConfig, python_versions: Sequence[str]
) -> None:
    """Hold ``[tool.nab.environment]`` to the free-threaded interpreter floor.

    ``python_versions`` is empty for a table that names no python, so the
    parse checks only the implementation and :func:`plan_targets` checks
    the python the target ended up on.
    """
    if environment.platform is None:
        return
    try:
        check_free_threaded(
            platforms=(environment.platform,),
            implementations=(environment.implementation or "cpython",),
            python_versions=python_versions,
        )
    except ValueError as exc:
        msg = f"invalid [tool.nab.environment]: {exc}"
        raise ConfigError(msg) from exc


def _check_requires_python_admits_target(
    requires_python: str | None,
    target: ResolveTarget,
    *,
    source: str,
    matrix: bool,
) -> None:
    """Reject a ``requires-python`` declaration that excludes the resolve target.

    ``requires-python`` declares the Python range the project supports; it
    does not steer the resolve.  Resolving for a Python the project says it
    does not support would produce a lock the project's own metadata
    rejects, so it fails loud and names the knob that moves the target.

    The declaration goes through
    :meth:`~nab_provider.target.ResolveTarget.admits_requires_python`, the
    comparison every candidate's ``Requires-Python`` takes, so it is read at
    the language minor and a micro floor like ``>= "3.11.4"`` admits a 3.11
    target.  Which knob the error names depends on the target.  A matrix
    declares the python axis of every target it expands, and both
    ``--python`` and ``[tool.nab.environment]`` are themselves errors
    alongside one, so a matrix target is moved by ``matrix.python`` instead.
    """
    if requires_python is None:
        return
    if target.admits_requires_python(SpecifierSet(requires_python)):
        return
    excludes = (
        f"{source} = {requires_python!r} excludes the resolve target"
        f" Python {target.python_version} ({target.label})."
    )
    if matrix:
        msg = (
            f"{excludes}  [tool.nab.matrix] declares the python axis of every"
            " target it expands: narrow matrix.python to drop the version, or"
            " widen requires-python."
        )
    else:
        msg = (
            f"{excludes}  nab resolves for the host interpreter unless told"
            " otherwise; pass --python with a version the declaration admits, set"
            " [tool.nab.environment] python to one, or widen requires-python."
        )
    raise ConfigError(msg)


def _option_label(value: EffectiveValue) -> str:
    """Name an option by its CLI flag when the CLI set it, else by its config key.

    ``base-group`` and ``build-group`` are the only values that reach here
    and both carry a flag, so a CLI origin on a flagless row is a bug.
    """
    if value.origin.kind is not SourceKind.CLI:
        return value.spec.name
    if value.spec.cli_flag is None:
        msg = f"Bug: {value.spec.name!r} has no CLI flag but carries a CLI origin"
        raise RuntimeError(msg)
    return value.spec.cli_flag


def _declared_group_names(document: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the group names the project declares in ``[dependency-groups]``."""
    groups = document.get("dependency-groups")
    if not isinstance(groups, dict):
        return ()
    return tuple(groups)


def _reject_declared_group_collision(
    option: EffectiveValue, declared: Sequence[str]
) -> None:
    """Reject ``option``'s group name when a declared group already spells it.

    Both would emit ``'name' in dependency_groups`` and no marker could
    say which was meant.
    """
    name: str = option.value
    taken = sorted(group for group in declared if canonicalize_name(group) == name)
    if not taken:
        return

    names = ", ".join(repr(group) for group in taken)
    msg = (
        f"{_option_label(option)} {name!r} and [dependency-groups] {names} are"
        " the same name; one marker cannot mean both"
    )
    raise ConfigError(msg)


def _validate_configured_groups(
    base_group: EffectiveValue,
    build_group: EffectiveValue,
    document: Mapping[str, Any],
) -> None:
    """Check ``base-group`` and ``build-group`` against each other and the file.

    Checked as the file is read rather than at emission, so it costs no
    resolve and holds for every output format.  ``[dependency-groups]`` is
    consulted only when ``base-group`` is set, since a ``build-group``
    without one has already been refused.
    """
    _validate_build_group_has_a_base_group(build_group, base_group)
    if base_group.value is None:
        return

    declared = _declared_group_names(document)
    _reject_declared_group_collision(base_group, declared)
    _validate_build_group_is_free(build_group, base_group, declared)


def _validate_build_group_has_a_base_group(
    build_group: EffectiveValue, base_group: EffectiveValue
) -> None:
    """Reject a ``build-group`` without a ``base-group`` to answer for the rest.

    Unnamed, the project's own dependencies carry no marker and install
    under every selection, so asking for the build group returns them too
    and nothing can install the build requirements alone, which is the
    reason to name them.  Naming both costs nothing: an install that wants
    them together selects both groups.
    """
    if build_group.value is None or base_group.value is not None:
        return

    msg = (
        f"{_option_label(build_group)} is {build_group.value!r}, but base-group"
        " is unset, so the project's own dependencies carry no marker and"
        " install alongside every group; set base-group to name them, or drop"
        " build-group and use nab lock --build-requirements for a separate lock"
    )
    raise ConfigError(msg)


def _validate_build_group_is_free(
    build_group: EffectiveValue, base_group: EffectiveValue, declared: Sequence[str]
) -> None:
    """Reject a ``build-group`` some other group already answers to.

    A declared ``[dependency-groups]`` name and ``base-group`` are both
    already spoken for, and all three emit ``'name' in dependency_groups``.
    """
    name: str | None = build_group.value
    if name is None:
        return

    if name == base_group.value:
        build_label = _option_label(build_group)
        base_label = _option_label(base_group)
        msg = (
            f"{build_label} and {base_label} are both {name!r};"
            " one marker cannot mean both"
        )
        raise ConfigError(msg)

    _reject_declared_group_collision(build_group, declared)


def _read_project_requires_python(document: Mapping[str, Any]) -> str | None:
    """Read ``[project].requires-python``, the fallback declaration source.

    ``[tool.nab].requires-python`` (and ``--project-requires-python``) wins
    when set; otherwise the project's own declaration is what the lock
    records.
    """
    project = document.get("project")
    if not isinstance(project, dict) or "requires-python" not in project:
        return None
    return parse_requires_python(project["requires-python"], _PROJECT_REQUIRES_PYTHON)


# The deprecated marker-environment keys, per environment axis, in the
# order they are read: the more precise key wins.
_MARKER_PYTHON_KEYS = ("python_full_version", "python_version")
_MARKER_IMPLEMENTATION_KEYS = ("implementation_name", "platform_python_implementation")
_MARKER_PLATFORM_KEYS = ("sys_platform", "platform_machine")
# The platform id names these too, so an overlay may repeat them as long as it
# agrees with the machine the pair identifies.
_MARKER_PLATFORM_IMPLIED_KEYS = ("platform_system", "os_name")
# The kernel markers, which are knobs of the platform the pair names.
_MARKER_PLATFORM_KNOBS = {
    "platform_release": "platform-release",
    "platform_version": "platform-version",
}

# The platform id each (sys_platform, platform_machine) pair names.
_PLATFORM_ID_BY_MARKERS: dict[tuple[str, str], str] = {
    (markers["sys_platform"], markers["platform_machine"]): platform_id
    for platform_id, markers in PLATFORM_MARKERS.items()
}


def _environment_from_marker_environment(
    marker_environment: Mapping[str, str],
) -> dict[str, Any]:
    """Translate the deprecated ``[tool.nab.marker-environment]`` overlay.

    The overlay set PEP 508 marker variables one by one, which let a
    partial declaration (``sys_platform`` alone) leave the rest of the
    environment on the host and resolve for a machine that does not exist.
    The replacement declares whole axes, so every overlay key must name one:
    an unmappable ``(sys_platform, platform_machine)`` pair, or a key no
    axis can carry, is an error rather than a silently-wrong resolve.  The
    kernel markers name no axis of their own; they are knobs of the platform
    the pair names, and translate into its table.
    """
    _logger.warning(
        "[tool.nab.marker-environment] is deprecated and will be removed;"
        " declare [tool.nab.environment] with python/platform/implementation"
        " instead.  Translating the overlay for this run."
    )

    translatable = {
        *_MARKER_PYTHON_KEYS,
        *_MARKER_IMPLEMENTATION_KEYS,
        *_MARKER_PLATFORM_KEYS,
        *_MARKER_PLATFORM_IMPLIED_KEYS,
        *_MARKER_PLATFORM_KNOBS,
    }
    untranslatable = sorted(set(marker_environment) - translatable)
    if untranslatable:
        msg = (
            f"[tool.nab.marker-environment] variable(s) {untranslatable!r} cannot"
            " be translated to [tool.nab.environment], whose axes are"
            f" {sorted(ENVIRONMENT_KEYS)!r}.  The platform id carries the"
            " OS and machine markers; declare it as environment.platform."
        )
        raise ConfigError(msg)

    environment: dict[str, Any] = {}
    for key in _MARKER_PYTHON_KEYS:
        if key in marker_environment:
            environment["python"] = marker_environment[key]
            break
    for key in _MARKER_IMPLEMENTATION_KEYS:
        if key in marker_environment:
            # platform_python_implementation is title-cased ("CPython").
            environment["implementation"] = marker_environment[key].lower()
            break

    needs_platform = [
        k
        for k in (*_MARKER_PLATFORM_IMPLIED_KEYS, *_MARKER_PLATFORM_KNOBS)
        if k in marker_environment
    ]
    if needs_platform and not any(
        k in marker_environment for k in _MARKER_PLATFORM_KEYS
    ):
        msg = (
            f"[tool.nab.marker-environment] sets {needs_platform!r} without"
            " (sys_platform, platform_machine), which is the pair that names"
            " the machine.  Declare [tool.nab.environment] platform instead:"
            " half a machine would keep the other half of the host's."
        )
        raise ConfigError(msg)

    if any(key in marker_environment for key in _MARKER_PLATFORM_KEYS):
        pair = (
            marker_environment.get("sys_platform", ""),
            marker_environment.get("platform_machine", ""),
        )
        platform_id = _PLATFORM_ID_BY_MARKERS.get(pair)
        if platform_id is None:
            valid = sorted(PLATFORM_MARKERS)
            msg = (
                "[tool.nab.marker-environment] sets (sys_platform,"
                f" platform_machine) = {pair!r}, which names no platform nab"
                f" models.  Declare [tool.nab.environment] platform = one of"
                f" {valid!r}; both markers must name one machine, because half"
                " a machine would keep the other half of the host's."
            )
            raise ConfigError(msg)
        _check_implied_platform_markers(marker_environment, platform_id)
        environment["platform"] = {
            "id": platform_id,
            **{
                knob: marker_environment[key]
                for key, knob in _MARKER_PLATFORM_KNOBS.items()
                if key in marker_environment
            },
        }
    validate_environment_values(environment)
    return environment


def _check_implied_platform_markers(
    marker_environment: Mapping[str, str], platform_id: str
) -> None:
    """Reject an overlay whose implied markers contradict the platform it names.

    The platform id carries ``platform_system`` and ``os_name``, so an overlay
    that repeats them is translatable; one that disagrees with them names two
    machines at once.
    """
    declared = PLATFORM_MARKERS[platform_id]
    conflicting = {
        key: marker_environment[key]
        for key in _MARKER_PLATFORM_IMPLIED_KEYS
        if key in marker_environment and marker_environment[key] != declared[key]
    }
    if conflicting:
        expected = {key: declared[key] for key in conflicting}
        msg = (
            f"[tool.nab.marker-environment] sets {conflicting!r}, which"
            f" contradicts platform {platform_id!r}, where they are"
            f" {expected!r}.  One overlay names one machine."
        )
        raise ConfigError(msg)


def _environment_from_effective(
    effective: Mapping[str, EffectiveValue],
) -> EnvironmentConfig | None:
    """Fold the environment surfaces into the one declared environment.

    ``[tool.nab.environment]`` is the surface;
    ``[tool.nab.marker-environment]`` is its deprecated predecessor and is
    translated into it.  Declaring both in a file is an error: the two would
    have to agree, and a silent precedence between them is exactly the
    ambiguity the replacement removes.  The command line writes keys of the
    one environment rather than a second table, so its keys lay over the
    translation the way they lay over a declared table.  Returns ``None``
    when neither is declared, which is the host.
    """
    entry = effective["environment"]
    declared: Mapping[str, Any] = entry.value
    marker_environment: Mapping[str, str] = effective["marker-environment"].value
    if marker_environment:
        if _file_declares_environment(entry):
            msg = (
                "[tool.nab.environment] and the deprecated"
                " [tool.nab.marker-environment] are both set; drop the"
                " marker-environment table."
            )
            raise ConfigError(msg)
        declared = {
            **_environment_from_marker_environment(marker_environment),
            **declared,
        }
    if not declared:
        return None
    platform = declared.get("platform")
    environment = EnvironmentConfig(
        python=declared.get("python"),
        platform=None if platform is None else environment_platform_spec(platform),
        implementation=declared.get("implementation"),
    )
    if environment.implementation is not None and environment.platform is None:
        valid = sorted(PLATFORM_MARKERS)
        msg = (
            "[tool.nab.environment].implementation needs a platform: an"
            " interpreter is modelled on a declared machine, not on the host's."
            f"  Add platform = one of {valid!r}."
        )
        raise ConfigError(msg)
    return environment


def _file_declares_environment(entry: EffectiveValue) -> bool:
    """Whether a configuration file declared ``[tool.nab.environment]``.

    Read off the stack rather than the effective value: the command line
    folds its keys into the same key, and a flag narrowing the deprecated
    overlay is not a second declaration of the table.
    """
    return any(
        value for origin, value in entry.stack if origin.kind is not SourceKind.CLI
    )


def _reject_duplicate_source_names(
    local_sources: tuple[LocalSource, ...],
    vcs_sources: tuple[VcsSource, ...],
    archive_sources: tuple[ArchiveSource, ...],
) -> None:
    """Reject a canonical name claimed by more than one declared source.

    The provider enforces this while indexing, but as a bare ValueError raised
    after the resolve starts; raising ConfigError here surfaces it at parse
    time like every other config error.
    """
    seen: dict[str, str] = {}
    # The three types share only SlottedValue, which declares no name, so the
    # joined tuple needs the element type spelling out.
    declared: tuple[LocalSource | VcsSource | ArchiveSource, ...] = (
        *local_sources,
        *vcs_sources,
        *archive_sources,
    )
    for source in declared:
        canonical = canonicalize_name(source.name)
        if canonical in seen:
            msg = (
                "local-sources/vcs-sources/archive-sources declare duplicate"
                f" canonical name {canonical!r} via {seen[canonical]!r}"
                f" and {source.name!r}"
            )
            raise ConfigError(msg)
        seen[canonical] = source.name


def _project_key_path(key: str, kind: SourceKind) -> str:
    """Return the table path a project file of ``kind`` gives ``key``.

    A project-dir nab.toml takes nab's keys at the top level; a
    pyproject.toml takes them under ``[tool.nab]``.
    """
    return key if kind is SourceKind.PROJECT_TOML else f"tool.nab.{key}"


def _reject_vcs_sources_under_block(
    vcs_sources: EffectiveValue,
    vcs: EffectiveValue,
) -> None:
    """Reject vcs-sources declared while the VCS policy blocks cloning.

    Cloning is opt-in, so a declared source under the default
    ``policy = "block"`` is contradictory. Raising ConfigError here fails
    at parse time and names the token to set.

    Each table is named for the file that has to carry the repair: the
    sources for the file declaring them, the policy for the file that set
    it. The two project files share a precedence rank, so a policy written
    into the other one conflicts instead of overriding.

    ``policy = "allow"`` opens the gate but does not on its own admit a
    URL: ``allowed-schemes`` and ``allowed-repos`` are empty by default
    and each denies every URL until an entry is added, so the message
    points at the whole gate rather than promising that one key is enough.
    """
    sources: tuple[VcsSource, ...] = vcs_sources.value
    config: VcsConfig = vcs.value
    if not sources or config.policy is not VcsPolicy.BLOCK:
        return

    # The default policy sits in no file, so its repair goes in the one that
    # declared the sources.
    policy_kind = (
        vcs_sources.origin.kind
        if vcs.origin.kind is SourceKind.DEFAULT
        else vcs.origin.kind
    )
    sources_table = _project_key_path("vcs-sources", vcs_sources.origin.kind)
    vcs_table = _project_key_path("vcs", policy_kind)

    msg = (
        f"[[{sources_table}]] is declared but [{vcs_table}].policy is"
        f" {config.policy.value!r}, which refuses every clone; remove"
        f' the sources, or set [{vcs_table}].policy = "allow" and open the'
        " rest of the gate (vcs.allowed-schemes and vcs.allowed-repos are"
        " empty by default and each denies every URL)"
    )
    raise ConfigError(msg)


def _validate_default_groups_against_conflicts(
    default_groups: Sequence[str],
    conflicts: Sequence[ConflictSet],
    base_group: str | None = None,
) -> None:
    """Reject a default install that co-activates an exclusive conflict set.

    A default install activates every default group with no user
    selection, but the emit-time disjointness validator prunes any
    context that activates two members of an exclusive set, so it never
    enumerates that install.  Two members co-active by default would
    silently violate the declared conflict; catch it at parse time.

    ``base_group`` counts as a default: PEP 751 seeds ``dependency_groups``
    from ``default-groups``, and the lock puts that name there so an
    install asking for nothing still gets the project's own dependencies.
    """
    configured = None if base_group is None else canonicalize_name(base_group)
    active = {canonicalize_name(g) for g in default_groups}
    if configured is not None:
        active.add(configured)

    for group in conflict_exclusion_groups(conflicts):
        co_active = sorted(
            name
            for kind, name in group
            if kind == ConflictKind.GROUP.value and name in active
        )
        if len(co_active) < MIN_ENGAGED_MEMBERS:
            continue

        if configured in co_active:
            others = ", ".join(repr(name) for name in co_active if name != configured)
            msg = (
                f"default-groups activates {others}, and base-group"
                f" {base_group!r} names the project's own dependencies, which"
                " every default install activates too; they are declared"
                " mutually exclusive in [tool.nab].conflicts, so an installer"
                " given no selection would install neither"
            )
        else:
            joined = ", ".join(repr(name) for name in co_active)
            msg = (
                f"default-groups activates {joined}, which are declared"
                " mutually exclusive in [tool.nab].conflicts"
            )
        raise ConfigError(msg)


def _validate_configured_conflict_sets(
    conflicts: Sequence[ConflictSet],
    base_group: str | None,
    build_group: str | None,
) -> None:
    """Reject the conflict sets a configured group name cannot mean.

    A configured member is active on every run, so an at-least-one set
    holding one can never fail and decides nothing.  A declaration that
    decides nothing reads as one that took effect, so it is refused
    rather than left to sit.

    An exclusive set pairing ``base-group`` with an extra is worse than
    inert.  A PEP 621 extra installs on top of the project's own
    dependencies, and the extras axis never deselects a default group, so
    the fork that chooses the extra describes an install context no
    installer can produce.  ``build-group`` pairs with an extra fine: the
    project's dependencies stay in every fork of that set.
    """
    names = {
        canonicalize_name(name)
        for name in (base_group, build_group)
        if name is not None
    }
    for conflict_set in conflicts:
        members = conflict_set.members
        if conflict_set.policy is ConflictPolicy.AT_LEAST_ONE:
            inert = sorted(
                member.name
                for member in members
                if member.kind is ConflictKind.GROUP and member.name in names
            )
            if inert:
                joined = ", ".join(repr(name) for name in inert)
                msg = (
                    f"[tool.nab].conflicts declares {joined} in an at-least-one"
                    " set, but a group named by base-group or build-group is"
                    " active on every run, so the set can never fail and"
                    " decides nothing"
                )
                raise ConfigError(msg)
            continue

        if base_group is None:
            continue
        canonical_base = canonicalize_name(base_group)
        holds_main = any(
            member.kind is ConflictKind.GROUP and member.name == canonical_base
            for member in members
        )
        extras = sorted(
            member.name for member in members if member.kind is ConflictKind.EXTRA
        )
        if holds_main and extras:
            joined = ", ".join(repr(name) for name in extras)
            msg = (
                f"[tool.nab].conflicts declares base-group {base_group!r}"
                f" mutually exclusive with the extra {joined}, but an extra"
                " installs on top of the project's own dependencies and never"
                " deselects them, so nothing could install that extra"
            )
            raise ConfigError(msg)
