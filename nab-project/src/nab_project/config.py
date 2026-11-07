"""Read project configuration and plan its resolve targets."""

from __future__ import annotations

__lazy_modules__ = (
    "nab_resolver.diagnostics",
    "datetime",
    "nab_project._config_hooks",
    "nab_project._config_sources",
    "nab_project.config_values",
    "nab_project",
    "nab_project.build_policy",
    "nab_project.conflicts",
    "nab_project.inputs",
    "nab_project.paths",
    "nab_project.workspace",
    "nab_provider._vendor.packaging.specifiers",
    "nab_provider._vendor.packaging.utils",
    "nab_provider._vendor.packaging.version",
    "tomli",
)

import enum
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import tomli

from nab_project.build_policy import enforce_build_policy_for_targets
from nab_project.config_values import (
    ENVIRONMENT_KEYS,
    MatrixConfig,
    check_package_override_overlap,
    environment_platform_spec,
    matrix_from_config,
    parse_requires_python,
    validate_environment_values,
)
from nab_project.conflicts import (
    MIN_ENGAGED_MEMBERS,
    ConflictKind,
    ConflictPolicy,
    ConflictSet,
    conflict_exclusion_groups,
)
from nab_project.input_source import (
    InputFormat,
    ScriptMetadataError,
    read_input_document,
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
    LocalSource,
    ResolveMode,
    VcsSource,
)
from nab_provider.target import (
    PLATFORM_MARKERS,
    ResolveTarget,
    check_free_threaded,
    host_environment,
    names_a_micro,
    python_axis_environment,
)
from nab_provider.vcs_admission import VcsConfig, VcsPolicy
from nab_resolver.diagnostics import DiagnosticMessage, Suggestion

from ._config_sources import (
    ConfigValue,
    Origin,
    SourceKind,
    project_key_path,
    read_project_values,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from nab_provider.overrides import IndexOverride, PackageOverride
    from nab_provider.records import IndexConfig
    from nab_provider.tags import PlatformSpec

    from .config_overrides import ProjectOverrides


__all__ = [
    "ConfigConflictError",
    "ConfigConflictKind",
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


class ConfigConflictKind(enum.Enum):
    """Project conflicts for which a host can suggest its own override syntax."""

    MISSING_MATRIX = "missing-matrix"
    SPECIFIC_MATRIX = "specific-matrix"


class ConfigConflictError(ConfigError):
    """A project conflict whose host may add an override-specific remedy."""

    def __init__(
        self, kind: ConfigConflictKind, message: DiagnosticMessage | str
    ) -> None:
        """Retain the conflict kind without coupling validation to host flags."""
        super().__init__(message)
        self.kind = kind


class EnvironmentConfig(ValueType):
    """The axes declared by ``[tool.nab.environment]``.

    Unset axes use the host. ``platform`` accepts a matrix ``PlatformSpec``.
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
    """Resolution inputs with project planning and diagnostic provenance."""

    __slots__ = __match_args__ = (
        "inputs",
        "mode",
        "environment",
        "matrix",
        "workspace",
        "workspace_member_names",
        "requires_python_source",
        "requires_python_declarations",
    )

    inputs: ResolveInputs
    mode: ResolveMode
    environment: EnvironmentConfig | None
    matrix: MatrixConfig | None
    workspace: WorkspaceConfig | None
    workspace_member_names: frozenset[str]
    requires_python_source: str
    requires_python_declarations: tuple[tuple[str, str], ...]

    def __init__(
        self,
        *,
        inputs: ResolveInputs | None = None,
        mode: ResolveMode = ResolveMode.SPECIFIC,
        environment: EnvironmentConfig | None = None,
        matrix: MatrixConfig | None = None,
        workspace: WorkspaceConfig | None = None,
        workspace_member_names: frozenset[str] = frozenset(),
        requires_python_source: str = _TOOL_NAB_REQUIRES_PYTHON,
        requires_python_declarations: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Retain operation settings separately from project planning."""
        self.inputs = ResolveInputs() if inputs is None else inputs
        self.mode = mode
        self.environment = environment
        self.matrix = matrix
        self.workspace = workspace
        self.workspace_member_names = workspace_member_names
        self.requires_python_source = requires_python_source
        self.requires_python_declarations = requires_python_declarations

    # Deprecate flat replace() updates and resolve_inputs() after upstream
    # consumers migrate to inputs and inputs.replace(). Add warnings then;
    # remove those compatibility paths in 0.1.
    def replace(self, **changes: object) -> NabProjectConfig:
        """Copy planning fields and named operation settings."""
        input_changes = {
            name: value
            for name, value in changes.items()
            if name in ResolveInputs.__match_args__
        }
        project_changes = {
            name: value
            for name, value in changes.items()
            if name not in ResolveInputs.__match_args__
        }
        if input_changes:
            if "inputs" in project_changes:
                message = (
                    "inputs cannot be combined with individual "
                    "resolution setting changes"
                )
                raise TypeError(message)
            project_changes["inputs"] = self.inputs.replace(**input_changes)
        kept: dict[str, Any] = {
            name: getattr(self, name) for name in self.__match_args__
        }
        kept.update(project_changes)
        return NabProjectConfig(**kept)

    def resolve_inputs(self) -> ResolveInputs:
        """Return the operation settings supplied by this project configuration."""
        return self.inputs


def read_pyproject_config(
    path: Path,
    *,
    discover_workspace: bool = True,
    anchor: datetime | None = None,
    input_format: InputFormat = InputFormat.PROJECT,
    overrides: ProjectOverrides | None = None,
    validate_host: Callable[[str, object, str], None] | None = None,
) -> NabProjectConfig:
    """Read project configuration without CLI, environment or user settings.

    The input's tool.nab table and adjacent nab.toml share one precedence
    level. Relative paths use the declaring file's directory, and relative
    cutoffs use one anchor for the complete read.
    """
    anchor = datetime.now(timezone.utc) if anchor is None else anchor
    document = _read_project_document(path, input_format=input_format)
    effective = read_project_values(
        realpath(path.parent) / path.name,
        document,
        input_format=input_format,
        overrides=overrides,
        validate_host=validate_host,
        anchor=anchor,
    )
    return _build_project_config(
        path,
        document,
        effective,
        discover_workspace=discover_workspace,
        input_format=input_format,
    )


def _build_project_config(
    path: Path,
    document: Mapping[str, Any],
    effective: Mapping[str, ConfigValue],
    *,
    discover_workspace: bool = True,
    input_format: InputFormat = InputFormat.PROJECT,
) -> NabProjectConfig:
    """Validate parsed host settings and assemble their project configuration."""
    if input_format is InputFormat.SCRIPT and any(
        effective[key].value
        for key in (
            "default-groups",
            "base-group",
            "build-group",
            "conflicts",
            "workspace",
        )
    ):
        msg = (
            "script inputs have no project groups, extras, build requirements "
            "or workspace"
        )
        raise ConfigError(msg)
    project_requires_python = _read_project_requires_python(document)
    config = _config_from_effective(
        effective,
        project_requires_python=project_requires_python,
        input_format=input_format,
    )
    _validate_configured_groups(
        effective["base-group"], effective["build-group"], document
    )
    if input_format is InputFormat.SCRIPT:
        config = _script_python_bounds(
            config, path, project_requires_python, effective["requires-python"]
        )
    elif discover_workspace:
        config = _apply_workspace_discovery(
            path, config, declared_in=effective["workspace"].origin.label
        )
    return config


def _script_python_bounds(
    config: NabProjectConfig,
    path: Path,
    script_requires_python: str | None,
    tool_requires_python: ConfigValue,
) -> NabProjectConfig:
    """Combine script/tool Python bounds while retaining their diagnostic origins."""
    if script_requires_python is None:
        return config

    declarations: tuple[tuple[str, str], ...] = (
        (script_requires_python, f"{path}: PEP 723 requires-python"),
    )
    if tool_requires_python.value is not None:
        origin = tool_requires_python.origin
        if origin.kind is SourceKind.OVERRIDE:
            tool_source = tool_requires_python.display_name
        else:
            key = project_key_path("requires-python", origin.kind)
            tool_source = f"{origin.label}: {key}"
        declarations += ((tool_requires_python.value, tool_source),)

    combined = str(SpecifierSet(",".join(value for value, _source in declarations)))
    return config.replace(
        requires_python=combined,
        requires_python_source="combined requires-python",
        requires_python_declarations=declarations,
    )


def _config_from_effective(
    effective: Mapping[str, ConfigValue],
    *,
    project_requires_python: str | None = None,
    input_format: InputFormat = InputFormat.PROJECT,
) -> NabProjectConfig:
    """Build a project config after single-option precedence is resolved.

    This applies cross-option validation and transformations. The caller
    applies workspace discovery afterwards.

    ``project_requires_python`` is ``[project].requires-python``, the
    fallback source for the declaration when ``[tool.nab]`` sets none.
    """
    mode_value = effective["mode"]
    matrix_value = effective["matrix"]
    mode: ResolveMode = mode_value.value
    matrix: MatrixConfig | None = matrix_value.value
    if mode is ResolveMode.UNIVERSAL and matrix is None:
        raise ConfigConflictError(
            ConfigConflictKind.MISSING_MATRIX,
            _missing_matrix_message(mode_value, input_format=input_format),
        )

    if mode is ResolveMode.SPECIFIC and matrix is not None:
        if mode_value.origin.outranks(matrix_value.origin):
            matrix = None
        elif len(matrix_from_config(matrix).expand()) > 1:
            raise ConfigConflictError(
                ConfigConflictKind.SPECIFIC_MATRIX,
                _specific_mode_message(matrix_value),
            )

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
            _matrix_environment_message(matrix_value, _environment_source(effective))
        )

    targets = _plan_targets(matrix, environment)
    build_policy = enforce_build_policy_for_targets(
        targets=targets,
        assume_universal_deps=effective["assume-universal-deps"].value,
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

    return NabProjectConfig(
        inputs=ResolveInputs(
            constraints=effective["constraints"].value,
            default_groups=default_groups,
            base_group=effective["base-group"].value,
            build_group=effective["build-group"].value,
            requires_python=requires_python,
            uploaded_prior_to=effective["uploaded-prior-to"].value,
            dist_policy=dist_policy,
            metadata_policy=effective["metadata-policy"].value,
            allow_zip_sdists=effective["allow-zip-sdists"].value,
            build_policy=build_policy,
            build_requires_depth=effective["build-requires-depth"].value,
            trust_unverified_sdist_deps=trust_unverified,
            assume_universal_deps=effective["assume-universal-deps"].value,
            indexes=indexes,
            vcs=vcs_config,
            local_sources=local_sources,
            vcs_sources=vcs_sources,
            archive_sources=archive_sources,
            resolution=effective["resolution"].value,
            decision_order=effective["decision-order"].value,
            conflicts=conflicts,
            package_overrides=package_overrides,
            index_overrides=index_overrides,
        ),
        mode=mode,
        requires_python_source=requires_python_source,
        environment=environment,
        matrix=matrix,
        workspace=effective["workspace"].value,
    )


def _declared_by(ev: ConfigValue) -> tuple[str, ...]:
    """Return the host's declaration labels for this setting."""
    return ev.declarations


def _declares(declarations: tuple[str, ...]) -> str:
    """Agree the verb with the number of declarations named by the host."""
    return "declare" if len(declarations) > 1 else "declares"


def _missing_matrix_message(
    mode_value: ConfigValue, *, input_format: InputFormat
) -> str:
    """Explain where to declare a missing universal target matrix."""
    if mode_value.origin.kind is SourceKind.PROJECT_TOML:
        table = "top-level [matrix] table in nab.toml"
    elif input_format is InputFormat.SCRIPT:
        table = "[tool.nab.matrix] table in the script's metadata"
    else:
        table = "[tool.nab.matrix] table in pyproject.toml"
    return f"mode = 'universal' requires a {table} declaring python and platforms"


def _specific_mode_message(matrix_value: ConfigValue) -> DiagnosticMessage:
    """Explain why a multi-target matrix conflicts with explicit specific mode."""
    if matrix_value.origin.kind is SourceKind.PROJECT_TOML:
        declared = "[matrix] in nab.toml"
    elif matrix_value.origin.kind is SourceKind.SCRIPT:
        declared = "[tool.nab.matrix] in the script's metadata"
    else:
        declared = "[tool.nab.matrix] in pyproject.toml"
    return DiagnosticMessage(
        (f"{declared} declares multiple targets but mode is 'specific'"),
        suggestions=(
            Suggestion(
                "set mode = 'universal' to resolve for every target the matrix "
                "declares, or remove the table. The multi-target lockfile format "
                "universal mode produces is experimental."
            ),
        ),
    )


def _matrix_environment_message(
    matrix_value: ConfigValue, environment_value: ConfigValue
) -> DiagnosticMessage:
    """Explain why matrix and environment declarations conflict."""
    matrix_declarations = _declared_by(matrix_value)
    environment_declarations = _declared_by(environment_value)
    matrix = " and ".join(matrix_declarations) or f"[{_declared_table(matrix_value)}]"
    environment = (
        " and ".join(environment_declarations)
        or f"[{_declared_table(environment_value)}]"
    )
    if not matrix_declarations and not environment_declarations:
        return DiagnosticMessage(
            (
                f"{matrix} and {environment} cannot both be set: the matrix declares "
                "one environment per tuple, so a single declared environment would "
                "contradict it"
            ),
            suggestions=(Suggestion("drop one."),),
        )
    return DiagnosticMessage(
        (
            f"{matrix} {_declares(matrix_declarations)} a matrix and {environment} "
            f"{_declares(environment_declarations)} one environment; the matrix "
            "declares one environment per tuple, so the two contradict"
        ),
        suggestions=(Suggestion("drop one."),),
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

    merged = merge_workspace_local_sources(config.inputs.local_sources, discovered)
    _reject_duplicate_source_names(
        merged, config.inputs.vcs_sources, config.inputs.archive_sources
    )
    explicit_names = {
        canonicalize_name(src.name) for src in config.inputs.local_sources
    }
    return config.replace(
        local_sources=merged,
        workspace_member_names=frozenset(
            canonicalize_name(src.name)
            for src in discovered
            if canonicalize_name(src.name) not in explicit_names
        ),
    )


def _read_project_document(
    path: Path, *, input_format: InputFormat = InputFormat.PROJECT
) -> dict[str, Any]:
    """Parse ``path``, reporting an unreadable or malformed file as ``ConfigError``."""
    try:
        return read_input_document(path, input_format=input_format)
    except ScriptMetadataError as exc:
        raise ConfigError(str(exc)) from exc
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise ConfigError(msg) from exc


def plan_targets(config: NabProjectConfig) -> tuple[ResolveTarget, ...]:
    """Return the configured resolve targets in matrix order.

    Validate Python support after target selection, which can override the host.
    """
    targets = _plan_targets(config.matrix, config.environment)

    if config.environment is not None:
        _check_environment_build(config.environment, (targets[0].python_version,))

    for target in targets:
        _check_requires_python_admits_target(
            config.inputs.requires_python,
            target,
            source=config.requires_python_source,
            matrix=config.matrix,
            declarations=config.requires_python_declarations,
        )
    return targets


def _plan_targets(
    matrix: MatrixConfig | None, environment: EnvironmentConfig | None
) -> tuple[ResolveTarget, ...]:
    """Plan targets before assembling the complete project configuration."""
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
    """Build markers and wheel tags for the declared platform.

    An unset Python version uses the host release; implementation defaults to CPython.
    Micro and prerelease versions select a point; minor versions select an interval.
    """
    assert environment.platform is not None  # the caller checked
    python = environment.python or host_environment()["python_full_version"]
    axis = python_axis_environment(python)
    implementation = environment.implementation or "cpython"

    _check_environment_build(
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


def _check_environment_build(
    environment: EnvironmentConfig, python_versions: Sequence[str]
) -> None:
    """Reject interpreter build knobs the environment cannot satisfy.

    ``python_versions`` is empty for a table that names no python, so the
    parse checks only the implementation and :func:`plan_targets` checks
    the python the target ended up on.
    """
    if environment.platform is None:
        return
    try:
        environment.platform.check_implementation(
            environment.implementation or "cpython"
        )
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
    matrix: MatrixConfig | None,
    declarations: tuple[tuple[str, str], ...] = (),
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
    target. The error names the shared Python setting or the implementation
    override that selected the target.
    """
    if requires_python is None:
        return

    if target.admits_requires_python(SpecifierSet(requires_python)):
        return

    for declared, declared_in in declarations:
        if not target.admits_requires_python(SpecifierSet(declared)):
            requires_python, source = declared, declared_in
            break

    excludes = (
        f"{source} = {requires_python!r} excludes the resolve target"
        f" Python {target.python_version} ({target.label})."
    )
    if matrix is not None:
        key = "matrix.python"
        override = (matrix.implementation_overrides or {}).get(target.implementation)
        if not matrix.include and override is not None and override.python is not None:
            key = f"matrix.overrides.implementations.{target.implementation}.python"
        repair = (
            f"change {key} or an include entry to drop the version, "
            "or widen requires-python."
        )
        if matrix.include:
            repair = (
                "change matrix.python, an implementation override or include entries "
                "to drop the version, or widen requires-python."
            )
        report = DiagnosticMessage(
            f"{excludes}  the matrix selects this target",
            suggestions=(Suggestion(repair),),
        )
    else:
        report = DiagnosticMessage(
            (
                f"{excludes}  nab resolves for the host interpreter unless told "
                "otherwise"
            ),
            suggestions=(
                Suggestion(
                    "pass --python with a version the declaration admits, set "
                    "[tool.nab.environment] python to one, or widen "
                    "requires-python."
                ),
            ),
        )

    raise ConfigError(report)


def _option_label(value: ConfigValue) -> str:
    """Return the setting label supplied by its host."""
    return value.display_name


def _declared_group_names(document: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the group names the project declares in ``[dependency-groups]``."""
    groups = document.get("dependency-groups")
    if not isinstance(groups, dict):
        return ()
    return tuple(groups)


def _reject_declared_group_collision(
    option: ConfigValue, declared: Sequence[str]
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
    base_group: ConfigValue,
    build_group: ConfigValue,
    document: Mapping[str, Any],
) -> None:
    """Validate configured group names against each other and project groups."""
    _validate_build_group_has_a_base_group(build_group, base_group)
    if base_group.value is None:
        return

    declared = _declared_group_names(document)
    _reject_declared_group_collision(base_group, declared)
    _validate_build_group_is_free(build_group, base_group, declared)


def _validate_build_group_has_a_base_group(
    build_group: ConfigValue, base_group: ConfigValue
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

    report = DiagnosticMessage(
        (
            f"{_option_label(build_group)} is {build_group.value!r}, but base-group is "
            "unset, so the project's own dependencies carry no marker and install "
            "alongside every group"
        ),
        suggestions=(
            Suggestion(
                "set base-group to name them, or drop build-group and use nab lock "
                "--build-requirements for a separate lock"
            ),
        ),
    )
    raise ConfigError(report)


def _validate_build_group_is_free(
    build_group: ConfigValue, base_group: ConfigValue, declared: Sequence[str]
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
    """Translate the deprecated marker overlay to declared environment axes."""
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
    effective: Mapping[str, ConfigValue],
) -> EnvironmentConfig | None:
    """Combine environment overrides with a deprecated marker overlay.

    Files cannot declare both forms. Host overrides amend the selected table.
    Return None for the host environment when both forms are absent.
    """
    entry = effective["environment"]
    declared: Mapping[str, Any] = entry.value
    marker_environment: Mapping[str, str] = effective["marker-environment"].value
    if marker_environment:
        file_origin = _environment_file_origin(entry)
        if file_origin is not None:
            environment_table = project_key_path("environment", file_origin.kind)
            marker_table = _declared_table(effective["marker-environment"])
            report = DiagnosticMessage(
                (
                    f"[{environment_table}] and the deprecated [{marker_table}] are "
                    "both set"
                ),
                suggestions=(Suggestion("drop the marker-environment table"),),
            )
            raise ConfigError(report)
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
        # The message asks for environment.platform, so name that table even
        # when the deprecated overlay is what declared it.
        origin = _environment_file_origin(entry)
        if origin is None:
            origin = _environment_source(effective).origin
        table = project_key_path("environment", origin.kind)

        valid = sorted(PLATFORM_MARKERS)
        suggestion = f"add platform = one of {valid!r}."
        source_labels = {source.kind: source.label for source, _value in entry.stack}
        input_kind = (
            SourceKind.SCRIPT
            if SourceKind.SCRIPT in source_labels
            else SourceKind.PYPROJECT
        )
        if {input_kind, SourceKind.PROJECT_TOML} <= source_labels.keys():
            suggestion += (
                " Set the same platform in [tool.nab.environment] in"
                f" {source_labels[input_kind]} and [environment] in"
                f" {source_labels[SourceKind.PROJECT_TOML]}."
            )

        report = DiagnosticMessage(
            (
                f"[{table}].implementation needs a platform: an interpreter is "
                "modelled on a declared machine, not on the host's"
            ),
            suggestions=(Suggestion(suggestion),),
        )
        raise ConfigError(report)

    return environment


def _environment_file_origin(entry: ConfigValue) -> Origin | None:
    """Return the last nonempty environment origin outside host overrides.

    An override can amend an overlay without adding a file declaration.
    """
    stack: Sequence[tuple[Origin, object]] = entry.stack
    for origin, value in reversed(stack):
        if value and origin.kind is not SourceKind.OVERRIDE:
            return origin
    return None


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
    # joined tuple needs an explicit element union.
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


def _environment_source(
    effective: Mapping[str, ConfigValue],
) -> ConfigValue:
    """Return the surface the environment came from, table or deprecated overlay."""
    environment = effective["environment"]
    return environment if environment.value else effective["marker-environment"]


def _declared_table(value: ConfigValue) -> str:
    """Return the table path the file that declared ``value`` writes for its key."""
    return project_key_path(value.name, value.origin.kind)


def _reject_vcs_sources_under_block(
    vcs_sources: ConfigValue,
    vcs: ConfigValue,
) -> None:
    """Reject declared VCS sources when their policy blocks cloning.

    Project files have equal precedence, so identical policies need matching edits.
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
    sources_table = project_key_path("vcs-sources", vcs_sources.origin.kind)
    vcs_table = project_key_path("vcs", policy_kind)
    policy_tables = [
        project_key_path("vcs", origin.kind)
        for origin, _value in vcs.stack
        if origin.kind
        in (
            SourceKind.PYPROJECT,
            SourceKind.SCRIPT,
            SourceKind.PROJECT_TOML,
        )
    ] or [vcs_table]
    policy_repair = " and ".join(
        f'[{table}].policy = "allow"' for table in policy_tables
    )

    report = DiagnosticMessage(
        (
            f"[[{sources_table}]] is declared but [{vcs_table}].policy is "
            f"{config.policy.value!r}, which refuses every clone"
        ),
        suggestions=(
            Suggestion(
                f"remove the sources, or set {policy_repair} and "
                "configure the remaining VCS filters (vcs.allowed-schemes and "
                "vcs.allowed-repos are empty by default and each denies every URL)"
            ),
        ),
    )
    raise ConfigError(report)


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
    """Reject conflict sets incompatible with configured project groups.

    An at-least-one set containing an always-active group is ineffective.
    An extra cannot deselect base-group dependencies.
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
