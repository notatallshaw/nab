"""Every layered nab option, declared once.

One :class:`OptionSpec` row in :data:`OPTIONS` is the whole definition of an
option: the key it is written under, the scope that decides which sources may
set it, the hooks that parse and render its value, its default, its ``NAB_*``
name, and the CLI flag that overrides it for one run.

Nothing below reads a key by name.  :mod:`nab.config.layers` walks these rows
to read a source, gate it and merge the result, and :mod:`nab.config.inspect`
walks them again to print it, so adding an option is one new row here plus one
row in :mod:`nab.optiondefs`.  A conformance test holds the two together.

The value types the ladder passes around live here too, next to the rows they
describe: where a value came from (:class:`Origin`), what one source bound
(:class:`Layer`), what it was refused for (:class:`RejectedLayer`), what won
(:class:`EffectiveValue`) and where discovery looks (:class:`SourceRoots`).
"""

from __future__ import annotations

import enum
import types
from typing import TYPE_CHECKING, Any

from nab_project.value import ValueType
from nab_provider.policy import (
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    ResolutionStrategy,
    ResolveMode,
)
from nab_provider.records import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, IndexConfig
from nab_provider.vcs_admission import VcsConfig

from . import hooks

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

__all__ = [
    "BY_KEY",
    "OPTIONS",
    "PRECEDENCE",
    "EffectiveValue",
    "Layer",
    "OptionSpec",
    "Origin",
    "RejectedLayer",
    "Scope",
    "SourceKind",
    "SourceRoots",
    "build_cli_overrides",
    "pyproject_registry_keys",
    "scope_label",
]


class Scope(enum.Enum):
    """Whether an option configures the project or the user/environment."""

    PROJECT = "project"
    USER = "user"


class SourceKind(enum.Enum):
    """One discoverable configuration source, low precedence to high.

    The two project-level TOML sources (``PYPROJECT`` and
    ``PROJECT_TOML``) share a precedence rank; the rest are totally
    ordered by :data:`PRECEDENCE`.
    """

    DEFAULT = "default"
    SYSTEM_TOML = "system"
    USER_TOML = "user"
    PYPROJECT = "pyproject"
    PROJECT_TOML = "project"
    ENV = "env"
    CLI = "cli"


# Precedence rank, low -> high.  PYPROJECT and PROJECT_TOML share rank 3:
# they are the same (project) precedence level, and on a tie PROJECT_TOML
# (the project-dir nab.toml) sorts last (wins).
PRECEDENCE: dict[SourceKind, int] = {
    SourceKind.DEFAULT: 0,
    SourceKind.SYSTEM_TOML: 1,
    SourceKind.USER_TOML: 2,
    SourceKind.PYPROJECT: 3,
    SourceKind.PROJECT_TOML: 3,
    SourceKind.ENV: 4,
    SourceKind.CLI: 5,
}

# The category gate, derived once: for each option scope, the TOML
# sources that may set it.  A PROJECT option lives in the two
# project-level files (pyproject + project-dir nab.toml).  A USER option
# lives in the three nab.toml files (system/user/project) but not in
# pyproject, which is project-scope only.  The project-dir nab.toml is
# the shared file both scopes accept.
_ALLOWED_TOML_SOURCES: dict[Scope, frozenset[SourceKind]] = {
    Scope.PROJECT: frozenset({SourceKind.PYPROJECT, SourceKind.PROJECT_TOML}),
    Scope.USER: frozenset(
        {SourceKind.SYSTEM_TOML, SourceKind.USER_TOML, SourceKind.PROJECT_TOML}
    ),
}


class OptionSpec(ValueType):
    """One row of the registry: the full definition of a layered option.

    ``key`` is the TOML/`nab config` key.  ``scope`` gates which sources
    may set it.  ``parse`` turns a raw TOML/string value into the typed
    value (and raises :class:`SourceConfigError` on a bad value).
    ``env_var`` is the ``NAB_*`` name or ``None`` (not env-settable).
    ``cli_flag`` is the flag spelling the conformance test checks, or
    ``None`` for a file-only row (a structured PROJECT table with no bare
    CLI flag, e.g. ``vcs``/``workspace``/``environment``); ``cli_param``
    is the command parameter backing the flag, also ``None`` for a
    file-only row.  ``type_label`` is shown by
    ``nab config``.
    """

    __slots__ = __match_args__ = (
        "key",
        "scope",
        "type_label",
        "default",
        "env_var",
        "cli_flag",
        "cli_param",
        "parse",
        "render",
    )

    key: str
    scope: Scope
    type_label: str
    default: Any
    env_var: str | None
    cli_flag: str | None
    cli_param: str | None

    def __init__(  # noqa: PLR0913, PLR0917 - the type's own fields, in its own order
        self,
        key: str,
        scope: Scope,
        type_label: str,
        default: Any,
        env_var: str | None,
        cli_flag: str | None,
        cli_param: str | None,
        parse: Callable[[Any, str], Any],
        render: Callable[[Any], str],
    ) -> None:
        """Record one registry row."""
        self.key = key
        self.scope = scope
        self.type_label = type_label
        self.default = default
        self.env_var = env_var
        self.cli_flag = cli_flag
        self.cli_param = cli_param

        # Annotated here, not in the field block: zuban reads a class-level
        # ``Callable`` as a method and drops the first argument at every call.
        self.parse: Callable[[Any, str], Any] = parse
        self.render: Callable[[Any], str] = render

    def allowed_in_toml(self, kind: SourceKind) -> bool:
        """Whether a TOML source of ``kind`` may set this option.

        The category gate.  ``kind`` is always one of the four TOML
        source kinds; env (``NAB_*``) gating is the ``env_var`` field and
        is handled in :func:`read_env_layer`, CLI is always allowed.
        """
        return kind in _ALLOWED_TOML_SOURCES[self.scope]


# Immutable empty-mapping default shared by table rows, so an unset
# value never aliases (or lets a downstream mutation corrupt) the
# registry default for later resolves in the same process.
_EMPTY_MAPPING: Mapping[str, Any] = types.MappingProxyType({})

# The registry.  One row per layered option.
OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        key="resolution",
        scope=Scope.PROJECT,
        type_label=f"enum({'|'.join(s.value for s in ResolutionStrategy)})",
        default=ResolutionStrategy.HIGHEST,
        env_var=None,
        cli_flag="--project-resolution",
        cli_param="project_resolution",
        parse=hooks.parse_resolution,
        render=lambda v: v.value,
    ),
    OptionSpec(
        key="decision-order",
        scope=Scope.PROJECT,
        type_label=f"enum({'|'.join(o.value for o in DecisionOrder)})",
        default=DecisionOrder.ARRIVAL,
        env_var=None,
        cli_flag="--project-decision-order",
        cli_param="project_decision_order",
        parse=hooks.parse_decision_order,
        render=lambda v: v.value,
    ),
    OptionSpec(
        key="mode",
        scope=Scope.PROJECT,
        type_label=f"enum({'|'.join(m.value for m in ResolveMode)})",
        default=ResolveMode.SPECIFIC,
        env_var=None,
        cli_flag="--project-mode",
        cli_param="project_mode",
        parse=hooks.parse_mode,
        render=lambda v: v.value,
    ),
    OptionSpec(
        key="constraints",
        scope=Scope.PROJECT,
        type_label="list(requirement)",
        default=(),
        env_var=None,
        cli_flag="--project-constraint",
        cli_param="project_constraint",
        parse=hooks.parse_constraints,
        render=hooks.render_string_tuple,
    ),
    OptionSpec(
        key="default-groups",
        scope=Scope.PROJECT,
        type_label="list(group)",
        default=(),
        env_var=None,
        cli_flag="--project-default-group",
        cli_param="project_default_group",
        parse=hooks.parse_default_groups,
        render=hooks.render_string_tuple,
    ),
    OptionSpec(
        key="base-group",
        scope=Scope.PROJECT,
        type_label="group",
        default=None,
        env_var=None,
        cli_flag="--project-base-group",
        cli_param="project_base_group",
        parse=hooks.parse_base_group,
        render=lambda v: "<none>" if v is None else v,
    ),
    OptionSpec(
        key="build-group",
        scope=Scope.PROJECT,
        type_label="group",
        default=None,
        env_var=None,
        cli_flag="--project-build-group",
        cli_param="project_build_group",
        parse=hooks.parse_build_group,
        render=lambda v: "<none>" if v is None else v,
    ),
    OptionSpec(
        key="requires-python",
        scope=Scope.PROJECT,
        type_label="specifier",
        default=None,
        env_var=None,
        cli_flag="--project-requires-python",
        cli_param="project_requires_python",
        parse=hooks.parse_requires_python,
        render=lambda v: "<none>" if v is None else v,
    ),
    OptionSpec(
        key="uploaded-prior-to",
        scope=Scope.PROJECT,
        type_label="datetime|PnD",
        default=None,
        env_var=None,
        cli_flag="--project-uploaded-prior-to",
        cli_param="project_uploaded_prior_to",
        parse=hooks.parse_uploaded_prior_to,
        render=hooks.render_uploaded_prior_to,
    ),
    OptionSpec(
        key="dist-policy",
        scope=Scope.PROJECT,
        type_label=f"enum({'|'.join(p.value for p in DistPolicy)})",
        default=(DistPolicy.WHEEL_OR_SDIST, False),
        env_var=None,
        cli_flag="--project-dist-policy",
        cli_param="project_dist_policy",
        parse=hooks.parse_dist_policy,
        render=hooks.render_dist_policy,
    ),
    OptionSpec(
        key="build-policy",
        scope=Scope.PROJECT,
        type_label=f"enum({'|'.join(p.value for p in BuildPolicy)})",
        default=BuildPolicy.BUILD_LOCAL,
        env_var=None,
        cli_flag="--project-build-policy",
        cli_param="project_build_policy",
        parse=hooks.parse_build_policy,
        render=lambda v: v.value,
    ),
    OptionSpec(
        key="build-requires-depth",
        scope=Scope.PROJECT,
        type_label="int",
        default=0,
        env_var=None,
        cli_flag="--project-build-requires-depth",
        cli_param="project_build_requires_depth",
        parse=hooks.parse_build_requires_depth,
        render=str,
    ),
    OptionSpec(
        key="environment",
        scope=Scope.PROJECT,
        type_label="table(python,platform[,knobs],implementation)",
        default=_EMPTY_MAPPING,
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_environment,
        render=hooks.render_environment,
    ),
    OptionSpec(
        key="marker-environment",
        scope=Scope.PROJECT,
        type_label="table(marker-var=str) [deprecated]",
        default=_EMPTY_MAPPING,
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_marker_environment,
        render=hooks.render_marker_environment,
    ),
    OptionSpec(
        key="vcs",
        scope=Scope.PROJECT,
        type_label="table(vcs-policy)",
        default=VcsConfig(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_vcs,
        render=hooks.render_vcs,
    ),
    OptionSpec(
        key="workspace",
        scope=Scope.PROJECT,
        type_label="table(members)",
        default=None,
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_workspace,
        render=hooks.render_workspace,
    ),
    OptionSpec(
        key="indexes",
        scope=Scope.PROJECT,
        type_label="array-of-tables(name,url,serialization)",
        default=(IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_indexes,
        render=hooks.render_index_list,
    ),
    OptionSpec(
        key="local-sources",
        scope=Scope.PROJECT,
        type_label="array-of-tables(name,path)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_local_sources,
        render=hooks.render_local_sources,
    ),
    OptionSpec(
        key="vcs-sources",
        scope=Scope.PROJECT,
        type_label="array-of-tables(name,url)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_vcs_sources,
        render=hooks.render_vcs_sources,
    ),
    OptionSpec(
        key="archive-sources",
        scope=Scope.PROJECT,
        type_label="array-of-tables(name,url)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_archive_sources,
        render=hooks.render_archive_sources,
    ),
    OptionSpec(
        key="packages",
        scope=Scope.PROJECT,
        type_label="table(package-override)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_packages,
        render=hooks.render_package_overrides,
    ),
    OptionSpec(
        key="package-rules",
        scope=Scope.PROJECT,
        type_label="array-of-tables(match,policy)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_package_rules,
        render=hooks.render_package_overrides,
    ),
    OptionSpec(
        key="index",
        scope=Scope.PROJECT,
        type_label="table(index-override)",
        default=_EMPTY_MAPPING,
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_index_overrides,
        render=hooks.render_index_overrides,
    ),
    OptionSpec(
        key="conflicts",
        scope=Scope.PROJECT,
        type_label="array-of-tables(members,policy)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_conflicts,
        render=hooks.render_conflicts,
    ),
    OptionSpec(
        key="matrix",
        scope=Scope.PROJECT,
        type_label="table(python,platforms)",
        default=None,
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=hooks.parse_matrix,
        render=hooks.render_matrix,
    ),
    OptionSpec(
        key="offline",
        scope=Scope.USER,
        type_label="bool",
        default=False,
        env_var="NAB_OFFLINE",
        cli_flag="--offline",
        cli_param="offline",
        parse=hooks.parse_bool,
        render=lambda v: "true" if v else "false",
    ),
    OptionSpec(
        key="cache-dir",
        scope=Scope.USER,
        type_label="path",
        default=None,
        env_var="NAB_CACHE_DIR",
        cli_flag="--cache-dir",
        cli_param="cache_dir",
        parse=hooks.parse_path,
        render=lambda v: "<computed>" if v is None else str(v),
    ),
    OptionSpec(
        key="http-backend",
        scope=Scope.USER,
        type_label=f"enum({'|'.join(hooks.HTTP_BACKENDS)})",
        default="urllib3",
        env_var="NAB_HTTP_BACKEND",
        cli_flag="--http-backend",
        cli_param="http_backend",
        parse=hooks.parse_http_backend,
        render=str,
    ),
    OptionSpec(
        key="max-concurrency",
        scope=Scope.USER,
        type_label="int",
        default=8,
        env_var="NAB_MAX_CONCURRENCY",
        cli_flag="--max-concurrency",
        cli_param="max_concurrency",
        parse=hooks.parse_max_concurrency,
        render=str,
    ),
)

BY_KEY: dict[str, OptionSpec] = {spec.key: spec for spec in OPTIONS}


def pyproject_registry_keys() -> frozenset[str]:
    """Registry keys a pyproject ``[tool.nab]`` table may legitimately carry.

    Only PROJECT-scope registry options are allowed in pyproject; the
    single-environment parser (:mod:`nab.config.model`) folds this set
    into its own known-keys so a registry key is not double-reported as
    an unknown ``[tool.nab]`` key.
    """
    return frozenset(
        spec.key for spec in OPTIONS if spec.allowed_in_toml(SourceKind.PYPROJECT)
    )


class Origin(ValueType):
    """Where a value came from: a source kind plus a display label."""

    __slots__ = __match_args__ = ("kind", "label")

    kind: SourceKind
    label: str

    def __init__(self, kind: SourceKind, label: str) -> None:
        """Record the source a value came from."""
        self.kind = kind
        self.label = label

    @property
    def scope(self) -> str:
        """The provenance scope name shown by ``nab config``.

        Mirrors the source kind's value for every kind except
        ``PYPROJECT``, which reports "project" (it sits at the project
        precedence level alongside the project-dir nab.toml).
        """
        return scope_label(self.kind)

    def outranks(self, other: Origin) -> bool:
        """Whether this origin sits at a strictly higher precedence level.

        A tie is not an outranking: ``PYPROJECT`` and ``PROJECT_TOML``
        share a rank, so neither overrides the other here.
        """
        return PRECEDENCE[self.kind] > PRECEDENCE[other.kind]


def scope_label(kind: SourceKind) -> str:
    """Return the scope name ``nab config`` reports for a source kind.

    Distinct from Scope (PROJECT/USER, the gate axis): provenance reports
    the source, so a project nab.toml reports "project", env reports
    "env", etc.  Every kind reports its own value except PYPROJECT, which
    shares the project precedence level and so reports "project".
    """
    return "project" if kind is SourceKind.PYPROJECT else kind.value


class Layer(ValueType):
    """A set of (key -> value) bindings discovered from one source."""

    __slots__ = __match_args__ = ("origin", "values")

    origin: Origin
    values: Mapping[str, Any]

    def __init__(self, origin: Origin, values: Mapping[str, Any]) -> None:
        """Record the bindings ``origin`` supplied."""
        self.origin = origin
        self.values = values


class RejectedLayer(ValueType):
    """A source refused by the registry: a key outside its scope, or unknown.

    Captured (not raised) by :func:`discover_layers` for the TOML sources and
    :func:`read_env_layer` for the ``NAB_*`` ones, only when the caller asks
    to collect rejections for ``nab config --include-rejected``.  The normal
    load path raises :class:`SourceConfigError` for TOML and warns for env.
    """

    __slots__ = __match_args__ = ("origin", "key", "reason")

    origin: Origin
    key: str
    reason: str

    def __init__(self, origin: Origin, key: str, reason: str) -> None:
        """Record why ``origin``'s ``key`` was refused."""
        self.origin = origin
        self.key = key
        self.reason = reason


class EffectiveValue(ValueType):
    """One option's winning value plus its full shadowed stack."""

    __slots__ = __match_args__ = ("spec", "value", "origin", "stack", "rejected")

    spec: OptionSpec
    value: Any
    origin: Origin
    # Every binding for this key in precedence order (low -> high),
    # the last of which is the winner.
    stack: tuple[tuple[Origin, Any], ...]
    rejected: tuple[RejectedLayer, ...]

    def __init__(
        self,
        spec: OptionSpec,
        value: Any,
        origin: Origin,
        stack: tuple[tuple[Origin, Any], ...],
        rejected: tuple[RejectedLayer, ...] = (),
    ) -> None:
        """Record the value ``origin`` bound for ``spec``."""
        self.spec = spec
        self.value = value
        self.origin = origin
        self.stack = stack
        self.rejected = rejected


class SourceRoots(ValueType):
    """Injectable search roots so config discovery is hermetic in tests.

    ``system_toml`` and ``user_toml`` point at the system/user
    ``nab.toml`` files.  ``project_dir`` is the directory holding the
    pyproject; the project ``nab.toml`` is looked up beside it.
    ``pyproject`` names the pyproject file itself when the user pointed at
    a non-default name; left ``None`` it defaults to
    ``project_dir / "pyproject.toml"`` so the registry reads the same file
    the rest of nab does.  Any field may be ``None`` to skip that source.
    There is no walk-up: the project source is the directory of the
    pyproject only.
    """

    __slots__ = __match_args__ = (
        "system_toml",
        "user_toml",
        "project_dir",
        "pyproject",
    )

    system_toml: Path | None
    user_toml: Path | None
    project_dir: Path | None
    pyproject: Path | None

    def __init__(
        self,
        system_toml: Path | None = None,
        user_toml: Path | None = None,
        project_dir: Path | None = None,
        pyproject: Path | None = None,
    ) -> None:
        """Record the roots config discovery may read."""
        self.system_toml = system_toml
        self.user_toml = user_toml
        self.project_dir = project_dir
        self.pyproject = pyproject


def build_cli_overrides(locals_by_param: Mapping[str, Any]) -> dict[str, Any]:
    """Map ``{cli_param: value}`` to a registry-keyed override dict.

    Iterates :data:`OPTIONS`, reads each row's ``cli_param`` out of
    ``locals_by_param``, and keeps only the keys the user actually set.
    An unset scalar flag is ``None`` and an unset repeatable flag is an
    empty tuple (the append-action default); both are omitted so they do not
    shadow the file ladder.  A file-only row (``cli_param`` is ``None``)
    has no CLI flag, so it is skipped entirely.  Both the run subcommands
    and ``nab config`` build their override dict through this single
    helper, keyed off the registry rather than a per-option if-chain.
    """
    out: dict[str, Any] = {}
    for spec in OPTIONS:
        if spec.cli_param is None:
            continue
        value = locals_by_param[spec.cli_param]
        if value is None or (isinstance(value, tuple) and not value):
            continue
        out[spec.key] = value
    return out
