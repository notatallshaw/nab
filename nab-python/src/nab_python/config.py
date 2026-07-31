"""Read ``[tool.nab]`` from a ``pyproject.toml`` into a typed config.

The CLI is intentionally narrow: anything that defines *what* gets
resolved lives in ``[tool.nab]``; anything about *how this run executes*
lives on the CLI.  This module owns the project side.
"""

from __future__ import annotations

import enum
import itertools
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import tomli
from typing_extensions import override

from nab_index.archive import ArchiveRequest, ArchiveRequestError
from nab_index.local_index import is_file_url
from nab_index.multi_index import IndexConfig
from nab_index.serialization import SimpleSerialization
from nab_index.subdir import subdirectory_escapes

from ._conflict_kind import KIND_EXTRA, KIND_GROUP
from ._iso8601 import parse_iso_datetime
from ._toml import tool_nab_section
from ._vcs_admission import known_vcs_schemes
from ._vendor.packaging.requirements import InvalidRequirement, Requirement
from ._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from ._vendor.packaging.utils import InvalidName, canonicalize_name
from ._vendor.packaging.version import InvalidVersion, Version
from .config_sources import (
    ConfigError,
    EffectiveValue,
    SourceKind,
    SourceRoots,
    build_cli_layer,
    discover_layers,
    pyproject_registry_keys,
    read_env_layer,
    reject_user_keys_in_pyproject,
    resolve_anchor,
    resolve_config,
)
from .fetch import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, IndexRoute
from .provider import (
    ArchiveSource,
    BuildPolicy,
    DistPolicy,
    LocalSource,
    ResolutionStrategy,
    ResolveMode,
    VcsConfig,
    VcsPolicy,
    VcsSource,
    _normalize_extra,
)
from .tags import DEFAULT_LIBC, LIBC_MAJOR, Libc, PlatformSpec, platform_kind
from .target import (
    PLATFORM_MARKERS,
    Matrix,
    ResolveTarget,
    check_free_threaded,
    host_environment,
    python_axis_environment,
)
from .workspace import (
    WorkspaceConfig,
    discover_workspace_root,
    merge_workspace_local_sources,
    read_workspace_members,
    workspace_local_sources,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from collections.abc import Set as AbstractSet
    from pathlib import Path

    from ._vendor.packaging.ranges import VersionRange

__all__ = [
    "ConfigError",
    "ConflictFork",
    "ConflictKind",
    "ConflictMember",
    "ConflictPolicy",
    "ConflictSelectionError",
    "ConflictSet",
    "EnvironmentConfig",
    "IndexOverride",
    "MatrixConfig",
    "NabProjectConfig",
    "OverrideConflictError",
    "PackageOverride",
    "ResolveMode",
    "conflict_exclusion_groups",
    "conflict_forks",
    "conflict_member_groups",
    "enforce_build_policy_for_targets",
    "index_routes_from_config",
    "matrix_from_config",
    "plan_targets",
    "read_pyproject_config",
    "validate_conflict_exclusions",
    "validate_conflict_minimums",
    "with_python_override",
]


_DURATION_PATTERN = re.compile(r"^P(\d+)D$")

# PEP 508 environment-marker variables; reject a misspelled
# [tool.nab.marker-environment] key (e.g. ``python-version``).
_PEP508_MARKER_VARIABLES = frozenset(
    {
        "os_name",
        "sys_platform",
        "platform_machine",
        "platform_python_implementation",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_version",
        "python_full_version",
        "implementation_name",
        "implementation_version",
    },
)

# Marker variables whose values must parse as PEP 440 versions.
_VERSION_MARKER_VARIABLES = frozenset({"python_version", "python_full_version"})

# How a ``requires-python`` declaration is named back to the user.  The
# [tool.nab] key stays bare because the CLI's error prefix already names that
# table; the [project] fallback has to name its own.
_TOOL_NAB_REQUIRES_PYTHON = "requires-python"
_PROJECT_REQUIRES_PYTHON = "[project] requires-python"


@dataclass(frozen=True, slots=True)
class MatrixConfig:
    """User-declared matrix axes for universal resolution."""

    python: str
    platforms: tuple[PlatformSpec, ...]
    python_order: str = "asc"
    python_patches: Mapping[str, str] | None = None
    implementations: tuple[str, ...] = ("cpython",)


@dataclass(frozen=True, slots=True)
class EnvironmentConfig:
    """The single environment ``[tool.nab.environment]`` declares.

    The axes a target is made of, the same ones a matrix entry carries.
    An unset axis takes the host's value, so an empty table is the host
    and a table naming only ``python`` is the host machine running
    another Python.

    ``platform`` is the same :class:`~nab_python.tags.PlatformSpec` a
    ``matrix.platforms`` entry parses to, so the wheel-tag knobs (the libc
    family, the libc and macOS the lock must run on, the kernel
    marker values, the free-threaded build) are declarable here too.
    """

    python: str | None = None
    platform: PlatformSpec | None = None
    implementation: str | None = None


class ConflictPolicy(enum.Enum):
    """How exclusive the members of a :class:`ConflictSet` are.

    Mirrors Gentoo's ``REQUIRED_USE`` group operators.  ``AT_MOST_ONE``
    (``??``) is the default for a bare uv-style set: the members are
    mutually exclusive but selecting none is fine, which suits opt-in
    extras.  ``EXACTLY_ONE`` (``^^``) additionally requires one to be
    chosen.  ``AT_LEAST_ONE`` (``||``) only forbids the empty
    selection; it is rarely useful for extras and is included for
    completeness.
    """

    AT_MOST_ONE = "at-most-one"
    EXACTLY_ONE = "exactly-one"
    AT_LEAST_ONE = "at-least-one"


class ConflictKind(enum.Enum):
    """Whether a :class:`ConflictMember` names an extra or a group."""

    EXTRA = KIND_EXTRA
    GROUP = KIND_GROUP


@dataclass(frozen=True, slots=True)
class ConflictMember:
    """One side of a conflict: a named extra or dependency group.

    ``name`` is stored canonicalised (PEP 685 for extras, PEP 735 for
    groups) so a selection compares equal regardless of how the user
    spelled it.  An extra and a group sharing a name are distinct
    members, matching uv's package-qualified model.
    """

    kind: ConflictKind
    name: str

    @override
    def __str__(self) -> str:
        """Render as ``extra 'cpu'`` / ``group 'black22'`` for messages."""
        return f"{self.kind.value} {self.name!r}"


@dataclass(frozen=True, slots=True)
class ConflictSet:
    """A set of mutually-exclusive members with an exclusivity policy."""

    members: tuple[ConflictMember, ...]
    policy: ConflictPolicy = ConflictPolicy.AT_MOST_ONE

    @override
    def __str__(self) -> str:
        """Render as ``at-most-one (extra 'cpu', extra 'gpu')`` for messages."""
        joined = ", ".join(str(m) for m in self.members)
        return f"{self.policy.value} ({joined})"


def conflict_exclusion_groups(
    conflicts: Sequence[ConflictSet],
) -> tuple[frozenset[tuple[str, str]], ...]:
    """Project conflict sets to the neutral exclusion form the lockfile uses.

    The disjointness validator consumes a sequence of member sets, of
    which at most one member may be active in any install context.
    Only :attr:`ConflictPolicy.AT_MOST_ONE` and
    :attr:`ConflictPolicy.EXACTLY_ONE` forbid co-selection, so only
    those contribute; :attr:`ConflictPolicy.AT_LEAST_ONE` constrains the
    empty selection, not co-selection, and is omitted.  Each member
    becomes a ``(kind, canonical_name)`` pair.
    """
    return tuple(
        frozenset((m.kind.value, m.name) for m in cs.members)
        for cs in conflicts
        if cs.policy is not ConflictPolicy.AT_LEAST_ONE
    )


def conflict_member_groups(
    conflicts: Sequence[ConflictSet],
) -> tuple[frozenset[tuple[str, str]], ...]:
    """Project every conflict set (any policy) to ``(kind, name)`` member sets.

    Distinct from :func:`conflict_exclusion_groups`, which drops
    :attr:`ConflictPolicy.AT_LEAST_ONE` because that policy permits
    co-selection.  The disjointness validator uses this projection to
    tell already-declared collisions from undeclared ones when shaping
    the hint.
    """
    return tuple(
        frozenset((m.kind.value, m.name) for m in cs.members) for cs in conflicts
    )


@dataclass(frozen=True, slots=True)
class ConflictFork:
    """One fork of a conflict-driven universal resolve.

    ``selection`` is the active conflicting members as ``(kind, name)``
    pairs.  ``active_extras`` and ``active_groups`` are the full extra
    and group selections this fork resolves with: the non-conflicting
    selections plus this fork's chosen members.  An unforked resolve is
    a single fork with an empty ``selection``.
    """

    selection: tuple[tuple[str, str], ...]
    active_extras: tuple[str, ...]
    active_groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PackageOverride:
    """One per-package override: a requirement plus a body.

    Built from either ``[tool.nab.packages.<name>]`` (the name-keyed sugar
    table) or a ``[[tool.nab.package-rules]]`` entry (one body across the
    requirements in its ``match`` selector).  The selector is a single PEP
    508 ``requirement`` (name plus an optional version specifier; no
    extras, marker, or URL); ``name`` is its canonical package name and
    ``version_range`` its range, so a policy field applies only to
    candidate versions inside it.  The *body* sets any combination of
    ``dist_policy`` (with ``dist_trust_unverified_deps`` folding in the
    sdist-trust flag), ``build_policy``, the ``uploaded_prior_to`` cutoff
    (or ``uploaded_prior_to_disabled`` for the ``false`` form), the
    routing ``index``, and the metadata-override fields.  An entry that
    sets ``index`` must use a bare-name requirement (full range), because
    routing decides where to fetch a listing before any version is known.

    The metadata-override fields ``dependencies``, ``requires_python``, and
    ``provides_extra`` substitute for what nab would parse from the
    distribution, keyed to the matched version range (uv
    ``dependency-metadata`` parity).  Each replaces its field independently:
    ``dependencies`` becomes the whole runtime ``Requires-Dist`` list,
    ``requires_python`` the Python specifier, and ``provides_extra`` the
    declared extras.  For every one, ``None`` means the entry does not set
    it; a present-but-empty value (``()`` for the two tuples) is a distinct,
    first-class value meaning "replace with nothing".
    """

    requirement: Requirement
    name: str
    version_range: VersionRange
    dist_policy: DistPolicy | None = None
    dist_trust_unverified_deps: bool | None = None
    build_policy: BuildPolicy | None = None
    uploaded_prior_to: datetime | None = None
    uploaded_prior_to_disabled: bool = False
    index: str | None = None
    dependencies: tuple[Requirement, ...] | None = None
    requires_python: str | None = None
    provides_extra: tuple[str, ...] | None = None
    # The config surface this entry was declared on (e.g. "packages.'numpy'"
    # or "package-rules[0]").  Only used to name the source in an error that
    # is raised after the two project files merge, so it is excluded from
    # equality.
    source_label: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class IndexOverride:
    """One ``[tool.nab.index.<name>]`` entry: policy for an index.

    Keyed by a declared index name.  The body sets any combination of
    ``dist_policy`` (with ``dist_trust_unverified_deps``),
    ``build_policy``, and the ``uploaded_prior_to`` cutoff (or
    ``uploaded_prior_to_disabled`` for the ``false`` form).  It applies
    to every package served from that index; it carries no routing and
    no version scope.
    """

    dist_policy: DistPolicy | None = None
    dist_trust_unverified_deps: bool | None = None
    build_policy: BuildPolicy | None = None
    uploaded_prior_to: datetime | None = None
    uploaded_prior_to_disabled: bool = False


# Two active selections engage the set's exclusivity, forcing a fork.
# Distinct from ``_MIN_CONFLICT_MEMBERS`` (a structural check on the
# declaration), which happens to be the same number for unrelated reasons.
_MIN_ENGAGED_MEMBERS = 2


def conflict_forks(
    selected_extras: Sequence[str],
    selected_groups: Sequence[str],
    conflicts: Sequence[ConflictSet],
) -> list[ConflictFork]:
    """Split a selection into one fork per mutually-exclusive combination.

    A conflict set is *engaged* when the selection activates two or more
    of its members under an exclusivity policy (at-most-one or
    exactly-one); only an engaged set forces a fork.  Each engaged set
    contributes one chosen member per fork, and the forks are the
    cartesian product across engaged sets.  Members of engaged sets are
    dropped from the shared base; non-conflicting selections stay active
    in every fork.  With no engaged set the result is a single unforked
    fork carrying the whole selection.

    Names compare and emit canonicalised; the extra and group loaders
    normalise on lookup, so a canonical active set resolves the same
    requirements the user's spelling would.
    """
    base_extras = [canonicalize_name(e) for e in selected_extras]
    base_groups = [canonicalize_name(g) for g in selected_groups]
    extra_set = set(base_extras)
    group_set = set(base_groups)

    # Collect the engaged sets (2+ selected members) and the members to
    # drop from the shared base; each engaged set becomes a fork axis.
    engaged: list[list[ConflictMember]] = []
    drop_extras: set[str] = set()
    drop_groups: set[str] = set()
    for conflict_set in conflicts:
        if conflict_set.policy is ConflictPolicy.AT_LEAST_ONE:
            continue
        members = [
            m for m in conflict_set.members if _member_active(m, extra_set, group_set)
        ]
        if len(members) < _MIN_ENGAGED_MEMBERS:
            continue
        engaged.append(members)
        for member in members:
            target = drop_extras if member.kind is ConflictKind.EXTRA else drop_groups
            target.add(member.name)

    if not engaged:
        return [ConflictFork((), tuple(base_extras), tuple(base_groups))]

    # One fork per choice of a single member from each engaged set.
    rest_extras = [e for e in base_extras if e not in drop_extras]
    rest_groups = [g for g in base_groups if g not in drop_groups]
    forks: list[ConflictFork] = []
    for combo in itertools.product(*engaged):
        chosen_extras = [m.name for m in combo if m.kind is ConflictKind.EXTRA]
        chosen_groups = [m.name for m in combo if m.kind is ConflictKind.GROUP]
        forks.append(
            ConflictFork(
                selection=tuple(sorted((m.kind.value, m.name) for m in combo)),
                active_extras=tuple(rest_extras + chosen_extras),
                active_groups=tuple(rest_groups + chosen_groups),
            )
        )
    return forks


@dataclass(frozen=True, slots=True)
class NabProjectConfig:
    """Everything ``[tool.nab]`` says about how to resolve this project."""

    mode: ResolveMode = ResolveMode.SPECIFIC
    constraints: tuple[str, ...] = ()
    default_groups: tuple[str, ...] = ()
    # The project's declared Python support range: recorded as the lock's
    # top-level ``requires-python`` and checked against the resolve target.
    # It does not choose the target; ``environment`` does.
    requires_python: str | None = None
    # The surface ``requires_python`` was read from, named by the error when
    # the declaration excludes a target.
    requires_python_source: str = _TOOL_NAB_REQUIRES_PYTHON
    uploaded_prior_to: datetime | None = None
    dist_policy: DistPolicy = DistPolicy.WHEEL_OR_SDIST
    build_policy: BuildPolicy = BuildPolicy.BUILD_LOCAL
    trust_unverified_sdist_deps: bool = False
    # The declared resolve environment from ``[tool.nab.environment]``, or
    # ``None`` for the host.  Mutually exclusive with ``matrix``.
    environment: EnvironmentConfig | None = None
    indexes: tuple[IndexConfig, ...] = (
        IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),
    )
    vcs: VcsConfig = field(default_factory=VcsConfig)
    local_sources: tuple[LocalSource, ...] = ()
    vcs_sources: tuple[VcsSource, ...] = ()
    archive_sources: tuple[ArchiveSource, ...] = ()
    matrix: MatrixConfig | None = None
    resolution: ResolutionStrategy = ResolutionStrategy.HIGHEST
    workspace: WorkspaceConfig | None = None
    conflicts: tuple[ConflictSet, ...] = ()
    # Per-package overrides from ``[tool.nab.packages.<name>]`` and
    # ``[[tool.nab.package-rules]]``, one per requirement, in declared
    # order.  Version-scoped: a policy field applies only to candidate
    # versions inside its requirement's range.  Routing entries (those
    # that set ``index``) are also projected into coordinator routes by
    # ``index_routes_from_config``.
    package_overrides: tuple[PackageOverride, ...] = ()
    # Per-index overrides from ``[tool.nab.index.<name>]``, keyed by
    # declared index name.  Each applies to every package served from
    # that index; no routing, no version scope.
    index_overrides: Mapping[str, IndexOverride] = field(default_factory=dict)
    # Canonical names of workspace members. Populated by
    # _apply_workspace_discovery; empty otherwise. Distinct from
    # ``local_sources``, which also carries explicit
    # ``[[tool.nab.local-sources]]`` entries.
    workspace_member_names: frozenset[str] = field(default_factory=frozenset)


class ConflictSelectionError(ConfigError):
    """A requested extra/group selection violates a declared conflict.

    Raised when one resolve cannot serve the selection: a project
    resolving for a single environment cannot install two
    mutually-exclusive members at once.  A declared matrix forks the
    resolve instead of raising, and only raises when one fork still
    reaches two members (through an umbrella extra, say).
    """


class OverrideConflictError(ConfigError):
    """A per-package and a per-index override set the same field for one candidate.

    Raised at resolve time when a candidate ``(package, version)`` served
    from an index is governed by both a per-package override (whose range
    contains the version) and a per-index override that each set the same
    policy field.  The two surfaces are deliberately not ranked, so an
    overlap is an error rather than a precedence call.
    """


def _member_active(
    member: ConflictMember,
    active_extras: AbstractSet[str],
    active_groups: AbstractSet[str],
) -> bool:
    """Return True when ``member`` is in the selected extras/groups."""
    if member.kind is ConflictKind.EXTRA:
        return member.name in active_extras
    return member.name in active_groups


def validate_conflict_minimums(
    conflicts: Sequence[ConflictSet],
    selected_extras: Sequence[str],
    selected_groups: Sequence[str],
) -> None:
    """Raise when a require-one set has no active member.

    Enforces only the "must select one" policies: an exactly-one set
    and an at-least-one set each require at least one active member.
    Names compare under canonicalisation.  Universal mode calls this to
    apply the minimums without the co-selection rejection, which it
    handles by forking instead.
    """
    active_extras = {canonicalize_name(e) for e in selected_extras}
    active_groups = {canonicalize_name(g) for g in selected_groups}
    for conflict_set in conflicts:
        any_active = any(
            _member_active(m, active_extras, active_groups)
            for m in conflict_set.members
        )
        if any_active:
            continue
        if conflict_set.policy is ConflictPolicy.AT_MOST_ONE:
            continue
        members = ", ".join(str(m) for m in conflict_set.members)
        quantifier = (
            "exactly one"
            if conflict_set.policy is ConflictPolicy.EXACTLY_ONE
            else "at least one"
        )
        msg = (
            f"{quantifier} of {members} must be selected: declared"
            f" {conflict_set.policy.value} in [tool.nab].conflicts"
        )
        raise ConflictSelectionError(msg)


def validate_conflict_exclusions(
    conflicts: Sequence[ConflictSet],
    selected_extras: Sequence[str],
    selected_groups: Sequence[str],
) -> None:
    """Raise when a selection co-activates two members of an exclusive set.

    An at-most-one or exactly-one set cannot have two active members at
    once.  Names compare under canonicalisation.  Universal mode applies
    this per fork, against the self-reference- and include-expanded
    active set, to catch members an umbrella selection reaches only
    transitively (one fork cannot serve two of them disjointly).
    """
    active_extras = {canonicalize_name(e) for e in selected_extras}
    active_groups = {canonicalize_name(g) for g in selected_groups}
    exclusive = {ConflictPolicy.AT_MOST_ONE, ConflictPolicy.EXACTLY_ONE}
    for conflict_set in conflicts:
        active = [
            m
            for m in conflict_set.members
            if _member_active(m, active_extras, active_groups)
        ]
        if len(active) > 1 and conflict_set.policy in exclusive:
            chosen = ", ".join(str(m) for m in active)
            msg = (
                f"{chosen} cannot be selected together: declared mutually"
                f" exclusive ({conflict_set.policy.value}) in [tool.nab].conflicts"
            )
            raise ConflictSelectionError(msg)


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
    merged by :func:`config_sources.resolve_config` with its per-key merge,
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
    project files and an array flag appends after them.  ``None`` (the
    default) is a file-only resolve, byte-identical to before.
    """
    if anchor is None:
        anchor = datetime.now(timezone.utc)
    pyproject_dir = path.parent.resolve()
    _reject_unknown_pyproject_keys(path)
    project_requires_python = _read_project_requires_python(path)
    # Point the pyproject root at ``pyproject_dir / path.name`` (not
    # ``path.resolve()``) so the registry's declaring directory is the
    # symlink's own directory, matching the historical local-sources base
    # and the project-dir nab.toml lookup.  ``open`` still follows the
    # symlink, so the same file is read.
    roots = SourceRoots(project_dir=pyproject_dir, pyproject=pyproject_dir / path.name)
    # Bind the lock anchor so the registry resolves ``P<n>D`` durations
    # (top-level and override-body) against it, exactly as the old direct
    # parse did.  System/user nab.toml and env/CLI carry no PROJECT key, so
    # they are excluded here: this is the file-only project config.
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
                "--project-mode universal needs a [tool.nab.matrix] table, but"
                " a matrix can only be declared in the project file (there is"
                " no --project-matrix flag). Add [tool.nab.matrix] to the"
                " project's pyproject.toml or nab.toml, or drop --project-mode"
                " universal."
            )
        else:
            msg = (
                "mode = 'universal' requires a [tool.nab.matrix] table"
                " declaring python and platforms"
            )
        raise ConfigError(msg)
    if mode is ResolveMode.SPECIFIC and matrix is not None:
        if not mode_value.origin.outranks(matrix_value.origin):
            msg = (
                "[tool.nab.matrix] is set but mode is 'specific'; set"
                " mode = 'universal' to resolve for every target the matrix"
                " declares, or remove the table. The multi-target lockfile"
                " format universal mode produces is experimental."
            )
            raise ConfigError(msg)
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
    _check_package_override_overlap(package_overrides)
    _validate_routes_declared(package_overrides, declared_index_names)

    index_overrides: Mapping[str, IndexOverride] = effective["index"].value
    _validate_index_overrides_declared(index_overrides, declared_index_names)

    environment = _environment_from_effective(effective)
    if matrix is not None and environment is not None:
        msg = (
            "[tool.nab.matrix] and [tool.nab.environment] cannot both be set:"
            " the matrix declares one environment per tuple, so a single"
            " declared environment would contradict it.  Drop one."
        )
        raise ConfigError(msg)

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
    _validate_default_groups_against_conflicts(default_groups, conflicts)

    local_sources = effective["local-sources"].value
    vcs_sources = effective["vcs-sources"].value
    archive_sources = effective["archive-sources"].value
    _reject_duplicate_source_names(local_sources, vcs_sources, archive_sources)
    vcs_config = effective["vcs"].value
    _reject_vcs_sources_under_block(vcs_sources, vcs_config)

    del pyproject_dir  # paths were resolved per-layer by the registry.
    return NabProjectConfig(
        mode=mode,
        constraints=effective["constraints"].value,
        default_groups=default_groups,
        requires_python=requires_python,
        requires_python_source=requires_python_source,
        uploaded_prior_to=effective["uploaded-prior-to"].value,
        dist_policy=dist_policy,
        build_policy=build_policy,
        trust_unverified_sdist_deps=trust_unverified,
        environment=environment,
        indexes=indexes,
        vcs=vcs_config,
        local_sources=local_sources,
        vcs_sources=vcs_sources,
        archive_sources=archive_sources,
        matrix=matrix,
        resolution=effective["resolution"].value,
        workspace=effective["workspace"].value,
        conflicts=conflicts,
        package_overrides=package_overrides,
        index_overrides=index_overrides,
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
            root_dir=path.parent.resolve(),
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
    return replace(
        config,
        local_sources=merged,
        workspace_member_names=frozenset(
            canonicalize_name(src.name)
            for src in discovered
            if canonicalize_name(src.name) not in explicit_names
        ),
    )


def _reject_unknown_pyproject_keys(path: Path) -> None:
    """Reject a USER-scope or unknown key in pyproject ``[tool.nab]``.

    Run before the registry merge so the resolve reports a typo'd
    ``[tool.nab]`` key and a USER-scope key set in pyproject with the
    established messages.  A USER-scope option (``offline``, ``cache-dir``)
    surfaces the category error; an unknown key fails loud rather than being
    silently dropped.  Reads the pyproject raw directly; the registry merge
    reads the same file again, and this keeps the unknown-key error a
    ``ConfigError`` on the pyproject surface for everything that is a known
    key.
    """
    try:
        with path.open("rb") as f:
            data = tomli.load(f)
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise ConfigError(msg) from exc
    raw = tool_nab_section(data)
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

    The ``requires-python`` declaration is checked here rather than at parse
    time because ``--python`` moves the target after the config is read, and
    it is the flag that rescues a project whose declaration excludes the host.

    Every target is checked, matrix included: the lock records the
    declaration at top level and the targets in ``environments``, so a
    target the declaration excludes would be a lock that contradicts itself
    and that a PEP 751 installer refuses.
    """
    targets = _plan_targets(config.matrix, config.environment)
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
    matching the matrix default.
    """
    assert environment.platform is not None  # the caller checked
    python = environment.python or host_environment()["python_full_version"]
    axis = python_axis_environment(python)
    implementation = environment.implementation or "cpython"
    try:
        check_free_threaded(
            platforms=(environment.platform,),
            implementations=(implementation,),
            python_versions=(axis["python_version"],),
        )
    except ValueError as exc:
        msg = f"invalid [tool.nab.environment]: {exc}"
        raise ConfigError(msg) from exc
    return ResolveTarget.for_declared(
        python_version=axis["python_version"],
        spec=environment.platform,
        implementation=implementation,
        python_full_version=axis["python_full_version"],
    )


def matrix_from_config(matrix: MatrixConfig) -> Matrix:
    """Build the expandable :class:`Matrix` from its parsed config table."""
    return Matrix(
        python=matrix.python,
        platforms=matrix.platforms,
        python_order=matrix.python_order,
        python_patches=(
            dict(matrix.python_patches) if matrix.python_patches is not None else None
        ),
        implementations=matrix.implementations,
    )


def _forbids_host_builds(targets: Sequence[ResolveTarget]) -> bool:
    """Whether any target impersonates a machine other than the host's.

    A declared target (a matrix tuple, or an environment naming a platform
    or implementation) carries a :class:`PlatformSpec`; the host and a
    host-python retarget do not.
    """
    return any(target.platform_spec is not None for target in targets)


def enforce_build_policy_for_targets(
    *,
    targets: Sequence[ResolveTarget],
    build_policy: BuildPolicy,
    build_policy_set: bool,
    package_overrides: Sequence[PackageOverride],
    index_overrides: Mapping[str, IndexOverride],
) -> BuildPolicy:
    """Return the build policy the planned targets permit, or raise.

    A PEP 517 backend only ever runs on the host interpreter, so what a
    build reports is the host's metadata.  Two tiers follow:

    * A target that moves the platform axis (a matrix, or an environment
      naming a ``platform`` or ``implementation``) forbids host builds:
      ``build-policy`` is forced to ``never`` and an explicit non-``never``
      value, global or in any override, is an error.  This matches pip,
      which requires ``--only-binary=:all:`` under ``--platform``.
    * A python-axis-only retarget on the host machine warns and permits:
      the machine is still the host, so a build can run at all, and
      refusing every one of them would take the default case with it.  A
      deliberate deviation from pip.  Set ``build-policy = "never"`` to
      forbid it.

    The host target permits, so the default case builds freely.
    """
    if _forbids_host_builds(targets):
        offending = _explicit_host_builds(
            build_policy_set=build_policy_set,
            build_policy=build_policy,
            package_overrides=package_overrides,
            index_overrides=index_overrides,
        )
        if offending:
            msg = (
                "a declared target cannot build on the host, so build-policy"
                f" must be 'never'; got {', '.join(offending)}.  A PEP 517"
                " backend runs on the host and reports the host's metadata,"
                " not the target's.  Remove the setting (it defaults to"
                " 'never' for a declared target) or set it to 'never'."
            )
            raise ConfigError(msg)
        return BuildPolicy.NEVER
    if not all(target.host_faithful for target in targets):
        _logger.warning(
            "the resolve targets Python %s but a build would run on the host"
            " interpreter and report its metadata; set build-policy = 'never'"
            " to forbid builds",
            targets[0].python_full_version,
        )
    return build_policy


def _explicit_host_builds(
    *,
    build_policy_set: bool,
    build_policy: BuildPolicy,
    package_overrides: Sequence[PackageOverride],
    index_overrides: Mapping[str, IndexOverride],
) -> list[str]:
    """Name every surface that explicitly asks for a non-``never`` build.

    An unset global is not offending: ``build-policy`` defaults to
    ``never`` for a target that forbids host builds rather than failing a
    project that never mentioned it.
    """
    offending: list[str] = []
    if build_policy_set and build_policy is not BuildPolicy.NEVER:
        offending.append(f"build-policy = {build_policy.value!r}")
    for pkg in package_overrides:
        bp = pkg.build_policy
        if bp is not None and bp is not BuildPolicy.NEVER:
            offending.append(f"packages.{pkg.requirement} build-policy = {bp.value!r}")
    for name, index_override in index_overrides.items():
        bp = index_override.build_policy
        if bp is not None and bp is not BuildPolicy.NEVER:
            offending.append(f"index.{name} build-policy = {bp.value!r}")
    return offending


def with_python_override(
    config: NabProjectConfig, python: str | None
) -> NabProjectConfig:
    """Return ``config`` with its resolve target moved onto ``python``.

    The ``--python`` flag (and the ``python_version`` argument of
    :func:`~nab_python.resolve.resolve_for_targets`) retargets the python
    axis for one run, leaving any declared platform in place.  The
    build-policy guard runs again over the new plan, so a runtime retarget
    is held to the same rule as a declared one.  ``None`` is a no-op.

    A matrix already declares the python axis for every target it names, so
    retargeting one of them would resolve for a python the matrix does not
    model and record it under that target's label.
    """
    if python is None:
        return config
    if config.matrix is not None:
        msg = (
            "--python cannot retarget a resolve that declares"
            " [tool.nab.matrix]: the matrix names the python axis of every"
            " target.  Narrow matrix.python instead."
        )
        raise ConfigError(msg)

    try:
        Version(python)
    except InvalidVersion as exc:
        msg = f"--python must be a version like '3.12' or '3.12.4', got {python!r}"
        raise ConfigError(msg) from exc

    environment = (
        EnvironmentConfig(python=python)
        if config.environment is None
        else replace(config.environment, python=python)
    )

    build_policy = enforce_build_policy_for_targets(
        targets=_plan_targets(config.matrix, environment),
        build_policy=config.build_policy,
        # The policy here is the effective one, so any non-``never`` value
        # that survived the parse is an explicit host-build request.
        build_policy_set=True,
        package_overrides=config.package_overrides,
        index_overrides=config.index_overrides,
    )

    retargeted = replace(config, environment=environment, build_policy=build_policy)
    # Called for the check it runs: the declaration must admit the moved target.
    plan_targets(retargeted)
    return retargeted


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

    A minor-interval target is admitted when ``requires-python`` overlaps its
    whole minor, so a micro floor like ``>= "3.11.4"`` admits the 3.11 minor
    rather than excluding it at the synthetic ``.0`` floor.  Which knob the
    error names depends on the target.  A matrix declares the python axis of
    every target it expands, and both ``--python`` and ``[tool.nab.environment]``
    are themselves errors alongside one, so a matrix target is moved by
    ``matrix.python`` instead.
    """
    if requires_python is None:
        return
    # A minor-interval target is admitted when the specifier overlaps the whole
    # minor; a whole target when its single release satisfies it.  A specifier
    # admits no prerelease unless it names one, so ">=3.14" has to admit a 3.14
    # candidate host.  This is the comparison every candidate's Requires-Python
    # takes too (see ResolveTarget.admits_requires_python).
    if target.admits_requires_python(SpecifierSet(requires_python)):
        return
    excludes = (
        f"{source} = {requires_python!r} excludes the resolve target"
        f" Python {target.python_full_version} ({target.label})."
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


def _parse_mode(value: object) -> ResolveMode:
    if not isinstance(value, str):
        msg = f"mode must be a string, got {type(value).__name__}"
        raise ConfigError(msg)
    try:
        return ResolveMode(value)
    except ValueError as exc:
        valid = sorted(m.value for m in ResolveMode)
        msg = f"mode must be one of {valid!r}, got {value!r}"
        raise ConfigError(msg) from exc


def _parse_string_list(key: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        msg = f"{key} must be a list of strings, got {type(value).__name__}"
        raise ConfigError(msg)
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            msg = f"{key}[{i}] must be a string, got {type(item).__name__}"
            raise ConfigError(msg)
        out.append(item)
    return tuple(out)


def _require_constraint(key: str, item: str) -> None:
    """Validate one ``constraints`` entry's shape.

    A constraint is a name with an optional specifier and marker; it bounds
    versions but never pulls a package in, so extras and direct-reference
    URLs are rejected here rather than only at resolve.
    """
    try:
        req = Requirement(item)
    except InvalidRequirement as exc:
        msg = f"{key} is not a valid requirement: {exc}"
        raise ConfigError(msg) from exc

    if req.extras:
        msg = f"{key} cannot have extras: {item}"
        raise ConfigError(msg)

    if req.url is not None:
        msg = f"{key} cannot be a direct reference (URL): {item}"
        raise ConfigError(msg)


def _parse_constraints(value: object) -> tuple[str, ...]:
    items = _parse_string_list("constraints", value)
    for i, item in enumerate(items):
        _require_constraint(f"constraints[{i}]", item)
    return items


def _reject_duplicates(key: str, items: tuple[str, ...]) -> None:
    seen: set[str] = set()
    for item in items:
        if item in seen:
            msg = f"{key} has duplicate entry: {item!r}"
            raise ConfigError(msg)
        seen.add(item)


def _parse_string_value(key: str, value: object) -> str:
    if not isinstance(value, str):
        msg = f"{key} must be a string, got {type(value).__name__}"
        raise ConfigError(msg)
    return value


def _parse_requires_python(value: object) -> str | None:
    """Parse ``[tool.nab].requires-python`` as a PEP 440 specifier.

    A declaration, not a target: it is recorded as the lock's top-level
    ``requires-python`` and checked against the resolve target, and the
    target itself comes from ``[tool.nab.environment]`` (the host by
    default).  Stored as the raw specifier string so the lockfile writer
    can pass it straight to :class:`SpecifierSet`.  Raises
    :class:`ConfigError` for invalid specifiers and for well-meaning bare
    versions like ``"3.13"``; those are not valid specifiers and must be
    written ``"==3.13"`` or ``">=3.13,<3.14"``.
    """
    raw = _parse_string_value("requires-python", value)
    try:
        SpecifierSet(raw)
    except InvalidSpecifier as exc:
        msg = (
            f"requires-python must be a PEP 440 specifier, got {raw!r}."
            f"  Did you mean ==X.Y or >=X.Y,<X.{{Y+1}}?"
        )
        raise ConfigError(msg) from exc
    return raw


def _read_project_requires_python(path: Path) -> str | None:
    """Read ``[project].requires-python``, the fallback declaration source.

    ``[tool.nab].requires-python`` (and ``--project-requires-python``) wins
    when set; otherwise the project's own declaration is what the lock
    records.  The file has already been parsed as TOML by
    :func:`_reject_unknown_pyproject_keys`, so a decode error cannot reach
    here.
    """
    with path.open("rb") as f:
        data = tomli.load(f)
    project = data.get("project")
    if not isinstance(project, dict) or "requires-python" not in project:
        return None
    return _parse_requires_python(project["requires-python"])


def index_routes_from_config(config: NabProjectConfig) -> list[IndexRoute]:
    """Project the routing package overrides into coordinator :class:`IndexRoute`s.

    Each per-package override that sets ``index`` contributes one route,
    keyed by its bare package name.  A routing entry always uses a
    bare-name requirement (parse-time guarantee), and the parse-time
    non-overlap check forbids two routes for one package, so the resulting
    route map has at most one entry per name.
    """
    return [
        IndexRoute(name=override.name, index=override.index)
        for override in config.package_overrides
        if override.index is not None
    ]


def _parse_uploaded_prior_to(value: object, *, anchor: datetime) -> datetime:
    """Parse ``uploaded-prior-to`` (ISO datetime, TOML datetime, or ``P<n>D``).

    Naive datetimes are rejected so lockfiles read identically across
    timezones. ``P<n>D`` (a nab extension) is resolved against
    ``anchor`` so re-locks reproduce the same cutoff.  Callers only reach
    here with a present value (the absent case is handled upstream).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            msg = (
                "uploaded-prior-to TOML datetime must have an explicit"
                " timezone offset (e.g. ``Z`` or ``+00:00``); got"
                f" {value!r}"
            )
            raise ConfigError(msg)
        return value

    if not isinstance(value, str):
        msg = (
            "uploaded-prior-to must be a TOML offset-date-time, an ISO"
            " 8601 datetime string with timezone, or a 'PnD' duration;"
            f" got {type(value).__name__}"
        )
        raise ConfigError(msg)

    duration_match = _DURATION_PATTERN.match(value)
    if duration_match is not None:
        days = int(duration_match.group(1))
        try:
            return anchor - timedelta(days=days)
        except OverflowError:
            msg = f"uploaded-prior-to duration is too large: {value!r}"
            raise ConfigError(msg) from None
    try:
        dt = parse_iso_datetime(value)
    except ValueError as exc:
        msg = (
            "uploaded-prior-to must be an ISO 8601 datetime with"
            " timezone (e.g. '2026-05-01T00:00:00Z') or a 'PnD'"
            f" duration (e.g. 'P4D'); got {value!r}"
        )
        raise ConfigError(msg) from exc
    if dt.tzinfo is None:
        msg = (
            "uploaded-prior-to ISO datetime must include an explicit"
            " timezone offset (e.g. 'Z' or '+00:00'); got"
            f" {value!r}"
        )
        raise ConfigError(msg)
    return dt


_DIST_POLICY_TABLE_KEYS = frozenset({"policy", "trust-unverified-deps"})


def _parse_dist_policy_global(value: object) -> tuple[DistPolicy, bool]:
    """Parse the global ``dist-policy``: an enum string or a policy table.

    The table form ``{ policy = "...", trust-unverified-deps = bool }``
    folds the sdist-trust flag into the dist body.  Returns
    ``(policy, trust_unverified)``.
    """
    if not isinstance(value, dict):
        return (
            _parse_enum("dist-policy", value, DistPolicy, DistPolicy.WHEEL_OR_SDIST),
            False,
        )
    unknown = sorted(set(value) - _DIST_POLICY_TABLE_KEYS)
    if unknown:
        msg = (
            f"dist-policy table has unknown key(s) {unknown!r};"
            f" expected {sorted(_DIST_POLICY_TABLE_KEYS)!r}"
        )
        raise ConfigError(msg)
    if "policy" not in value:
        msg = "dist-policy table must set 'policy'"
        raise ConfigError(msg)
    policy = _parse_enum(
        "dist-policy.policy", value["policy"], DistPolicy, DistPolicy.WHEEL_OR_SDIST
    )
    trust = _parse_bool(
        "dist-policy.trust-unverified-deps",
        value.get("trust-unverified-deps"),
        default=False,
    )
    return (policy, trust)


def _parse_bool(key: str, value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        msg = f"{key} must be a boolean, got {type(value).__name__}"
        raise ConfigError(msg)
    return value


def _parse_enum(
    key: str,
    value: object,
    enum_cls: type[enum.Enum],
    default: enum.Enum,
) -> Any:
    if value is None:
        return default
    if not isinstance(value, str):
        msg = f"{key} must be a string, got {type(value).__name__}"
        raise ConfigError(msg)
    try:
        return enum_cls(value)
    except ValueError as exc:
        valid = sorted(m.value for m in enum_cls)
        msg = f"{key} must be one of {valid!r}, got {value!r}"
        raise ConfigError(msg) from exc


def _parse_marker_environment(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        msg = (
            "marker-environment must be a table of string -> string,"
            f" got {type(value).__name__}"
        )
        raise ConfigError(msg)
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            msg = (
                f"marker-environment entries must be string -> string, got {k!r}: {v!r}"
            )
            raise ConfigError(msg)
        if k not in _PEP508_MARKER_VARIABLES:
            valid = sorted(_PEP508_MARKER_VARIABLES)
            msg = (
                f"unknown marker-environment variable {k!r}; expected a PEP 508"
                f" marker variable, one of {valid!r}"
            )
            raise ConfigError(msg)
        if k in _VERSION_MARKER_VARIABLES:
            try:
                Version(v)
            except InvalidVersion as exc:
                msg = f"marker-environment.{k} must be a PEP 440 version, got {v!r}"
                raise ConfigError(msg) from exc
        out[k] = v
    return out


_ENVIRONMENT_KEYS = frozenset({"python", "platform", "implementation"})


def _parse_environment(value: object) -> dict[str, Any]:
    """Parse ``[tool.nab.environment]``: the one environment to resolve for.

    Kept as the raw table so the registry merges it sub-key by sub-key
    across the config sources; :func:`_environment_from_effective` turns the
    merged whole into an :class:`EnvironmentConfig`.  ``platform`` takes the
    two shapes a ``matrix.platforms`` entry takes, a bare id or a table of
    the wheel-tag knobs, so a dict value passes through here.
    """
    if not isinstance(value, dict):
        msg = f"[tool.nab.environment] must be a table, got {type(value).__name__}"
        raise ConfigError(msg)
    unknown = sorted(set(value) - _ENVIRONMENT_KEYS)
    if unknown:
        msg = (
            f"unknown [tool.nab.environment] keys: {unknown!r};"
            f" expected {sorted(_ENVIRONMENT_KEYS)!r}"
        )
        raise ConfigError(msg)
    out: dict[str, Any] = {
        key: item
        if key == "platform"
        else _parse_string_value(f"environment.{key}", item)
        for key, item in value.items()
    }
    _validate_environment_values(out)
    return out


def _environment_platform_spec(value: object) -> PlatformSpec:
    """Build the :class:`PlatformSpec` ``[tool.nab.environment].platform`` names.

    The same two shapes ``matrix.platforms`` entries take, parsed by the same
    code: a bare id at the platform's default tag knobs, or a table declaring
    them.
    """
    where = "environment.platform"
    if isinstance(value, str):
        return _platform_spec(where, platform_id=value)
    if isinstance(value, dict):
        return _parse_platform_table(where, cast("dict[str, Any]", value))
    msg = f"{where} must be a platform id or a table, got {type(value).__name__}"
    raise ConfigError(msg)


def _validate_environment_values(environment: Mapping[str, Any]) -> None:
    """Validate the value of every environment axis the table names.

    Shared by the ``[tool.nab.environment]`` parse and the
    ``[tool.nab.marker-environment]`` translation, so both reject the
    same bad values with one message.
    """
    python = environment.get("python")
    if python is not None:
        try:
            Version(python)
        except InvalidVersion as exc:
            msg = (
                "environment.python must be a version like '3.12' or"
                f" '3.12.4', got {python!r}"
            )
            raise ConfigError(msg) from exc
    platform = environment.get("platform")
    if platform is not None:
        platform_id = _environment_platform_spec(platform).platform_id
        if platform_id not in PLATFORM_MARKERS:
            valid = sorted(PLATFORM_MARKERS)
            msg = (
                f"unknown environment.platform {platform_id!r};"
                f" expected one of {valid!r}"
            )
            raise ConfigError(msg)
    implementation = environment.get("implementation")
    if implementation is not None and implementation not in _KNOWN_IMPLEMENTATIONS:
        valid = list(_KNOWN_IMPLEMENTATIONS)
        msg = (
            f"unknown environment.implementation {implementation!r};"
            f" expected one of {valid!r}"
        )
        raise ConfigError(msg)


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
            f" {sorted(_ENVIRONMENT_KEYS)!r}.  The platform id carries the"
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
    _validate_environment_values(environment)
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
    translated into it.  Declaring both is an error: the two would have to
    agree, and a silent precedence between them is exactly the ambiguity the
    replacement removes.  Returns ``None`` when neither is declared, which
    is the host.
    """
    declared: Mapping[str, Any] = effective["environment"].value
    marker_environment: Mapping[str, str] = effective["marker-environment"].value
    if marker_environment:
        if declared:
            msg = (
                "[tool.nab.environment] and the deprecated"
                " [tool.nab.marker-environment] are both set; drop the"
                " marker-environment table."
            )
            raise ConfigError(msg)
        declared = _environment_from_marker_environment(marker_environment)
    if not declared:
        return None
    platform = declared.get("platform")
    environment = EnvironmentConfig(
        python=declared.get("python"),
        platform=None if platform is None else _environment_platform_spec(platform),
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


_INDEX_KEYS = frozenset({"name", "url", "serialization"})


def _parse_indexes(value: object) -> tuple[IndexConfig, ...]:
    if not isinstance(value, list):
        msg = f"indexes must be an array of tables, got {type(value).__name__}"
        raise ConfigError(msg)

    if not value:
        msg = "indexes must contain at least one entry when present"
        raise ConfigError(msg)

    out: list[IndexConfig] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            msg = f"indexes[{i}] must be a table, got {type(entry).__name__}"
            raise ConfigError(msg)
        unknown = sorted(set(entry) - _INDEX_KEYS)
        if unknown:
            msg = (
                f"unknown indexes[{i}] keys: {unknown!r};"
                f" expected {sorted(_INDEX_KEYS)!r}"
            )
            raise ConfigError(msg)
        try:
            name = entry["name"]
            url = entry["url"]
        except KeyError as missing:
            msg = f"indexes[{i}] missing required key {missing!s}"
            raise ConfigError(msg) from None
        if not isinstance(name, str) or not isinstance(url, str):
            msg = f"indexes[{i}] name and url must be strings"
            raise ConfigError(msg)
        if "serialization" in entry and is_file_url(url):
            msg = (
                f"indexes[{i}].serialization is not settable on a file:// index:"
                " a local index is read from disk with no Accept negotiation,"
                f" so the pin would do nothing.  Drop it from index {name!r}."
            )
            raise ConfigError(msg)
        serialization = _parse_enum(
            f"indexes[{i}].serialization",
            entry.get("serialization"),
            SimpleSerialization,
            SimpleSerialization.NEGOTIATE,
        )
        out.append(IndexConfig(name=name, url=url, serialization=serialization))
    _check_index_name_uniqueness(out)
    return tuple(out)


def _check_index_name_uniqueness(indexes: Sequence[IndexConfig]) -> None:
    """Reject two indexes declared with the same name.

    Shared by the single-file parse and the registry's across-file merge
    re-validation (config_sources): a name may appear at most once, whether
    the duplicate is in one file or split across the two project files.
    """
    seen: set[str] = set()
    for index in indexes:
        if index.name in seen:
            msg = f"duplicate index name: {index.name!r}"
            raise ConfigError(msg)
        seen.add(index.name)


_PACKAGE_OVERRIDE_BODY_KEYS = frozenset(
    {
        "dist-policy",
        "build-policy",
        "uploaded-prior-to",
        "index",
        "strict",
        "dependencies",
        "requires-python",
        "provides-extra",
    }
)
# A [[tool.nab.package-rules]] entry carries a ``match`` selector plus body keys.
_PACKAGE_RULE_KEYS = frozenset({"match"}) | _PACKAGE_OVERRIDE_BODY_KEYS
_INDEX_OVERRIDE_KEYS = frozenset({"dist-policy", "build-policy", "uploaded-prior-to"})
# Override-body keys not supported yet; rejected so nothing inert ships.
# ``metadata`` is the nested-table form the flat body keys replace.
_OVERRIDE_DEFERRED_KEYS = frozenset(
    {"resolution", "prereleases", "source", "vcs", "metadata", "marker"}
)
# The policy fields a per-package override may carry per field name, mapping
# each to the offending-entry attribute used by the parse-time overlap check
# below.  uploaded-prior-to is one field set by either a cutoff datetime or
# the ``false`` disable form, so both forms share one row (see _override_sets).
_PACKAGE_POLICY_FIELDS = (
    ("dist-policy", "dist_policy"),
    ("dist-policy.trust-unverified-deps", "dist_trust_unverified_deps"),
    ("build-policy", "build_policy"),
    ("uploaded-prior-to", "uploaded_prior_to"),
    ("index", "index"),
    ("dependencies", "dependencies"),
    ("requires-python", "requires_python"),
    ("provides-extra", "provides_extra"),
)


def _parse_packages_sugar(
    value: object,
    *,
    anchor: datetime,
) -> list[PackageOverride]:
    """Parse ``[tool.nab.packages.<name>]`` into per-package overrides.

    Each key is a PEP 508 requirement (a bare name, or a name plus a
    version specifier in a quoted key such as ``"numpy <= 1.21"``) and the
    sub-table is the override body.  The key is the whole selector, so the
    sugar form carries no inner selector key.
    """
    if isinstance(value, list):
        msg = (
            "[tool.nab.packages] is the name-keyed table form"
            " ([tool.nab.packages.<name>]); for one body across several"
            " requirements use [[tool.nab.package-rules]] with match = [...]"
        )
        raise ConfigError(msg)
    if not isinstance(value, dict):
        msg = (
            "[tool.nab.packages] must be a table keyed by package name, got"
            f" {type(value).__name__}"
        )
        raise ConfigError(msg)
    out: list[PackageOverride] = []
    for key, body in value.items():
        where = f"packages.{key!r}"
        requirement = _requirement_from_selector(key, where)
        if not isinstance(body, dict):
            msg = f"{where} must be a table, got {type(body).__name__}"
            raise ConfigError(msg)
        _reject_deferred(body, where)
        unknown = sorted(set(body) - _PACKAGE_OVERRIDE_BODY_KEYS)
        if unknown:
            msg = (
                f"{where}: unknown override key(s) {unknown!r}; expected body"
                f" keys {sorted(_PACKAGE_OVERRIDE_BODY_KEYS)!r}"
            )
            raise ConfigError(msg)
        out.extend(
            _build_package_overrides(
                (requirement,),
                body,
                where,
                anchor=anchor,
            )
        )
    return out


def _parse_package_rules(
    value: object,
    *,
    anchor: datetime,
) -> list[PackageOverride]:
    """Parse ``[[tool.nab.package-rules]]`` into per-package overrides.

    Each entry's ``match`` selector lists PEP 508 requirements (name plus
    an optional version specifier); the body applies to every one, so a
    single rule can cover many packages (e.g. routing a namespace to one
    index).
    """
    if not isinstance(value, list):
        msg = (
            "[tool.nab.package-rules] must be an array of tables"
            " ([[tool.nab.package-rules]]); for per-package policy keyed by"
            f" name use [tool.nab.packages.<name>].  Got {type(value).__name__}"
        )
        raise ConfigError(msg)
    out: list[PackageOverride] = []
    for i, entry in enumerate(value):
        out.extend(_parse_package_rule_entry(entry, i, anchor=anchor))
    return out


def _parse_package_rule_entry(
    entry: object,
    index: int,
    *,
    anchor: datetime,
) -> list[PackageOverride]:
    where = f"package-rules[{index}]"
    if not isinstance(entry, dict):
        msg = f"{where} must be a table, got {type(entry).__name__}"
        raise ConfigError(msg)
    _reject_deferred(entry, where)
    unknown = sorted(set(entry) - _PACKAGE_RULE_KEYS)
    if unknown:
        msg = (
            f"{where}: unknown override key(s) {unknown!r}; expected 'match'"
            f" and body keys {sorted(_PACKAGE_OVERRIDE_BODY_KEYS)!r}"
        )
        raise ConfigError(msg)
    requirements = _parse_match(entry.get("match"), where)
    if not requirements:
        msg = (
            f"{where} must carry a 'match' selector listing at least one"
            " PEP 508 requirement"
        )
        raise ConfigError(msg)
    body = {key: val for key, val in entry.items() if key != "match"}
    return _build_package_overrides(requirements, body, where, anchor=anchor)


def _build_package_overrides(
    requirements: tuple[Requirement, ...],
    body: dict[str, Any],
    where: str,
    *,
    anchor: datetime,
) -> list[PackageOverride]:
    """Turn a validated selector and body into one override per requirement."""
    dist_policy, dist_trust = _parse_override_dist(body.get("dist-policy"), where)
    build_policy = (
        _parse_enum(
            f"{where}.build-policy",
            body["build-policy"],
            BuildPolicy,
            BuildPolicy.NEVER,
        )
        if "build-policy" in body
        else None
    )
    uploaded_prior_to, uploaded_disabled = _parse_override_uploaded_prior_to(
        body.get("uploaded-prior-to"),
        where,
        anchor=anchor,
        present="uploaded-prior-to" in body,
    )
    route = _parse_override_index(body, where)
    dependencies = _parse_override_dependencies(body.get("dependencies"), where)
    requires_python = _parse_override_requires_python(
        body.get("requires-python"), where
    )
    provides_extra = _parse_override_provides_extra(body.get("provides-extra"), where)
    has_body = (
        dist_policy is not None
        or dist_trust is not None
        or build_policy is not None
        or uploaded_prior_to is not None
        or uploaded_disabled
        or route is not None
        or dependencies is not None
        or requires_python is not None
        or provides_extra is not None
    )
    if not has_body:
        msg = (
            f"{where} sets no policy; an entry must set at least one of"
            f" {sorted(_PACKAGE_OVERRIDE_BODY_KEYS)!r}"
        )
        raise ConfigError(msg)
    if route is not None:
        for requirement in requirements:
            if str(requirement.specifier):
                msg = (
                    f"{where}.index routing requires bare-name requirements"
                    " (no version specifier); routing decides where to fetch a"
                    " listing before any version is known, but"
                    f" {str(requirement)!r} is version-scoped"
                )
                raise ConfigError(msg)

    return [
        PackageOverride(
            requirement=requirement,
            name=canonicalize_name(requirement.name),
            version_range=requirement.specifier.to_range(),
            dist_policy=dist_policy,
            dist_trust_unverified_deps=dist_trust,
            build_policy=build_policy,
            uploaded_prior_to=uploaded_prior_to,
            uploaded_prior_to_disabled=uploaded_disabled,
            index=route,
            dependencies=dependencies,
            requires_python=requires_python,
            provides_extra=provides_extra,
            source_label=where,
        )
        for requirement in requirements
    ]


def _requirement_from_selector(raw: str, where: str) -> Requirement:
    """Parse one selector into a name-plus-optional-specifier requirement.

    Extras, markers, and URLs are rejected: a selector carries only a
    package name and an optional version specifier.
    """
    try:
        requirement = Requirement(raw)
    except InvalidRequirement as exc:
        msg = f"{where} entry {raw!r} is not a valid PEP 508 requirement"
        raise ConfigError(msg) from exc
    if requirement.extras or requirement.marker is not None or requirement.url:
        msg = (
            f"{where} entry {raw!r} may carry only a name and an optional"
            " version specifier; extras, markers, and URLs are not supported"
            " on the override surface"
        )
        raise ConfigError(msg)
    return requirement


def _check_package_override_overlap(
    overrides: tuple[PackageOverride, ...],
) -> None:
    """Reject two per-package entries setting one field for overlapping ranges.

    For each (canonical name, policy field) the entries that set the field
    must have pairwise-disjoint version ranges.  Two ranges overlap when
    ``not (range_a & range_b).is_empty``.  A bare-name requirement is the
    full range, so it overlaps every range for that package; in
    particular two routing entries for one package always conflict.
    """
    for _field, attr in _PACKAGE_POLICY_FIELDS:
        by_name: defaultdict[str, list[PackageOverride]] = defaultdict(list)
        for entry in overrides:
            if _override_sets(entry, attr):
                by_name[entry.name].append(entry)
        for name, entries in by_name.items():
            for left, right in itertools.combinations(entries, 2):
                if not (left.version_range & right.version_range).is_empty:
                    msg = (
                        f"two per-package overrides for {name!r} both set"
                        f" {_field!r} for overlapping versions:"
                        f" {str(left.requirement)!r} and"
                        f" {str(right.requirement)!r}.  Per-package overrides for"
                        " one field must cover disjoint version ranges."
                    )
                    raise ConfigError(msg)


def _override_sets(override: PackageOverride, attr: str) -> bool:
    """Whether ``override`` carries the policy field tracked by ``attr``.

    uploaded-prior-to counts as set by either a cutoff datetime or the
    ``false`` disable form, so a cutoff entry and a disable entry for one
    package with overlapping ranges still conflict.
    """
    if attr == "uploaded_prior_to":
        return override.uploaded_prior_to is not None or (
            override.uploaded_prior_to_disabled
        )
    return getattr(override, attr) is not None


def _reject_deferred(
    entry: dict[str, Any], where: str, *, flat_metadata_advice: bool = True
) -> None:
    """Reject override-body keys that are not supported.

    ``flat_metadata_advice`` gates the package-surface hint to set metadata
    via the flat body keys; the index surface passes ``False`` since those
    keys are rejected there too.
    """
    deferred = sorted(set(entry) & _OVERRIDE_DEFERRED_KEYS)
    if deferred:
        msg = f"{where}: key(s) {deferred!r} are not supported"
        if flat_metadata_advice and "metadata" in deferred:
            msg += (
                "; set metadata as the flat body keys 'dependencies',"
                " 'requires-python', and 'provides-extra' instead"
            )
        raise ConfigError(msg)


def _parse_index_overrides(
    value: object,
    *,
    anchor: datetime,
) -> dict[str, IndexOverride]:
    """Parse ``[tool.nab.index.<name>]`` into a name-keyed policy map.

    Each key must name a declared ``[[tool.nab.indexes]]`` entry; that
    cross-key check is a resolve-path concern run post-merge by
    :func:`_validate_index_overrides_declared` (the surface is parsed in
    isolation from the ``indexes`` row, so this parser does not see the
    declared set).  The body sets policy fields only (no routing, no
    version scope); the override applies to every package served from that
    index.
    """
    if not isinstance(value, dict):
        msg = (
            "[tool.nab.index] must be a table keyed by index name, got"
            f" {type(value).__name__}"
        )
        raise ConfigError(msg)
    out: dict[str, IndexOverride] = {}
    for name, body in value.items():
        where = f"index.{name}"
        out[name] = _parse_index_override_body(body, where, anchor=anchor)
    return out


def _parse_index_override_body(
    body: object, where: str, *, anchor: datetime
) -> IndexOverride:
    if not isinstance(body, dict):
        msg = f"{where} must be a table, got {type(body).__name__}"
        raise ConfigError(msg)
    _reject_deferred(body, where, flat_metadata_advice=False)
    unknown = sorted(set(body) - _INDEX_OVERRIDE_KEYS)
    if unknown:
        msg = (
            f"{where}: unknown override key(s) {unknown!r}; expected body keys"
            f" {sorted(_INDEX_OVERRIDE_KEYS)!r} (per-index overrides carry no"
            " routing and no version scope)"
        )
        raise ConfigError(msg)
    dist_policy, dist_trust = _parse_override_dist(body.get("dist-policy"), where)
    build_policy = (
        _parse_enum(
            f"{where}.build-policy",
            body["build-policy"],
            BuildPolicy,
            BuildPolicy.NEVER,
        )
        if "build-policy" in body
        else None
    )
    uploaded_prior_to, uploaded_disabled = _parse_override_uploaded_prior_to(
        body.get("uploaded-prior-to"),
        where,
        anchor=anchor,
        present="uploaded-prior-to" in body,
    )
    has_body = (
        dist_policy is not None
        or dist_trust is not None
        or build_policy is not None
        or uploaded_prior_to is not None
        or uploaded_disabled
    )
    if not has_body:
        msg = (
            f"{where} sets no policy; an entry must set at least one of"
            f" {sorted(_INDEX_OVERRIDE_KEYS)!r}"
        )
        raise ConfigError(msg)
    return IndexOverride(
        dist_policy=dist_policy,
        dist_trust_unverified_deps=dist_trust,
        build_policy=build_policy,
        uploaded_prior_to=uploaded_prior_to,
        uploaded_prior_to_disabled=uploaded_disabled,
    )


def _parse_match(value: object, where: str) -> tuple[Requirement, ...]:
    """Parse a ``match`` selector into PEP 508 requirements.

    Each entry is a requirement of name plus an optional version
    specifier; extras, markers, and URLs are rejected.  A bare name means
    all versions; a version specifier scopes the entry to matching ones.
    """
    if value is None:
        return ()
    names = _parse_string_list(f"{where}.match", value)
    return tuple(_requirement_from_selector(raw, f"{where}.match") for raw in names)


def _parse_override_dist(
    value: object, where: str
) -> tuple[DistPolicy | None, bool | None]:
    """Parse the ``dist-policy`` body: an enum string or a policy table.

    The table form ``{ policy = ..., trust-unverified-deps = bool }``
    folds the sdist-trust flag into the dist body.
    """
    if value is None:
        return (None, None)
    if isinstance(value, str):
        return (
            _parse_enum(
                f"{where}.dist-policy", value, DistPolicy, DistPolicy.WHEEL_OR_SDIST
            ),
            None,
        )
    if not isinstance(value, dict):
        msg = (
            f"{where}.dist-policy must be a policy string or a table"
            f" {{ policy, trust-unverified-deps }}, got {type(value).__name__}"
        )
        raise ConfigError(msg)
    unknown = sorted(set(value) - _DIST_POLICY_TABLE_KEYS)
    if unknown:
        msg = (
            f"{where}.dist-policy has unknown key(s) {unknown!r};"
            f" expected {sorted(_DIST_POLICY_TABLE_KEYS)!r}"
        )
        raise ConfigError(msg)
    if "policy" not in value:
        msg = f"{where}.dist-policy table must set 'policy'"
        raise ConfigError(msg)
    policy = _parse_enum(
        f"{where}.dist-policy.policy",
        value["policy"],
        DistPolicy,
        DistPolicy.WHEEL_OR_SDIST,
    )
    trust = value.get("trust-unverified-deps")
    if trust is not None and not isinstance(trust, bool):
        msg = f"{where}.dist-policy.trust-unverified-deps must be a boolean"
        raise ConfigError(msg)
    return (policy, trust)


def _parse_override_uploaded_prior_to(
    value: object, where: str, *, anchor: datetime, present: bool
) -> tuple[datetime | None, bool]:
    """Parse the ``uploaded-prior-to`` body: ``false`` disables, else a cutoff."""
    if not present:
        return (None, False)
    if value is False:
        return (None, True)
    if value is True:
        msg = (
            f"{where}.uploaded-prior-to: ``true`` is not a valid value; use"
            " ``false`` to disable the cutoff or a datetime / 'PnD' duration"
            " to set a window"
        )
        raise ConfigError(msg)
    try:
        cutoff = _parse_uploaded_prior_to(value, anchor=anchor)
    except ConfigError as exc:
        msg = f"{where}.uploaded-prior-to: {exc}"
        raise ConfigError(msg) from exc
    return (cutoff, False)


def _parse_override_requires_python(value: object, where: str) -> str | None:
    """Parse a per-package ``requires-python`` override, naming the entry.

    An absent key (``None``) means no override; a present value delegates
    to :func:`_parse_requires_python` for PEP 440 validation, prefixing the
    ``where`` selector on failure so the message names the offending entry.
    """
    if value is None:
        return None

    try:
        return _parse_requires_python(value)
    except ConfigError as exc:
        msg = f"{where}.{exc}"
        raise ConfigError(msg) from exc


def _parse_override_dependencies(
    value: object, where: str
) -> tuple[Requirement, ...] | None:
    """Parse the ``dependencies`` body: PEP 508 strings that replace deps.

    The list replaces a package's declared runtime dependencies for the
    matched version range.  Each item is a full PEP 508 dependency
    *value*, so extras, markers, and version specifiers are all legal
    (unlike the override *key*, which :func:`_requirement_from_selector`
    restricts to a name plus specifier).  A present-but-empty list is
    stored as ``()`` (replace with zero deps), distinct from the key
    being absent (``None``).
    """
    if value is None:
        return None

    if not isinstance(value, list):
        msg = (
            f"{where}.dependencies must be a list of PEP 508 requirement"
            f" strings, got {type(value).__name__}"
        )
        raise ConfigError(msg)

    out: list[Requirement] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            msg = (
                f"{where}.dependencies[{i}] must be a string, got {type(item).__name__}"
            )
            raise ConfigError(msg)
        try:
            out.append(Requirement(item))
        except InvalidRequirement as exc:
            msg = (
                f"{where}.dependencies[{i}] is not a valid PEP 508"
                f" requirement: {item!r}"
            )
            raise ConfigError(msg) from exc
    return tuple(out)


def _parse_override_provides_extra(value: object, where: str) -> tuple[str, ...] | None:
    """Parse the ``provides-extra`` body: the extras the override declares.

    A TOML array of extra names, each normalised per PEP 685 (the same
    rule :func:`nab_python.provider._normalize_extra` applies to parsed
    ``Provides-Extra``), so an extra compares equal regardless of spelling.
    A present-but-empty list is stored as ``()`` (declares no extras),
    distinct from the key being absent (``None``).
    """
    if value is None:
        return None

    if not isinstance(value, list):
        msg = (
            f"{where}.provides-extra must be a list of extra names, got"
            f" {type(value).__name__}"
        )
        raise ConfigError(msg)

    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            msg = (
                f"{where}.provides-extra[{i}] must be a string, got"
                f" {type(item).__name__}"
            )
            raise ConfigError(msg)
        out.append(_normalize_extra(item))
    return tuple(out)


def _parse_override_index(entry: dict[str, Any], where: str) -> str | None:
    """Parse the routing ``index`` body and validate its ``strict`` flag.

    The route is always a strict pin to one index, so ``strict`` only
    accepts ``true``.  ``strict = false`` is rejected: fallthrough on a
    miss is not cleanly wireable through the single-index-pin router this
    release ships.

    The route-names-a-declared-index check is a resolve-path concern run
    post-merge by :func:`_validate_routes_declared`, since this parser sees
    the override surface in isolation from the ``indexes`` row.
    """
    route = entry.get("index")
    if route is not None and not isinstance(route, str):
        msg = f"{where}.index must be a string, got {type(route).__name__}"
        raise ConfigError(msg)
    if "strict" not in entry:
        return route
    if route is None:
        msg = f"{where}.strict is only meaningful alongside an 'index' route"
        raise ConfigError(msg)
    strict = entry["strict"]
    if not isinstance(strict, bool):
        msg = f"{where}.strict must be a boolean, got {type(strict).__name__}"
        raise ConfigError(msg)
    if not strict:
        msg = (
            f"{where}.strict = false (fallthrough routing) is not supported in"
            " this release; the index route is always a strict pin"
        )
        raise ConfigError(msg)
    return route


def _parse_vcs(value: object) -> VcsConfig:
    if not isinstance(value, dict):
        msg = f"[tool.nab.vcs] must be a table, got {type(value).__name__}"
        raise ConfigError(msg)
    allowed = sorted({"policy", "allowed-schemes", "allowed-repos", "require-pin"})
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        msg = f"unknown [tool.nab.vcs] keys: {unknown!r}; expected {allowed!r}"
        raise ConfigError(msg)
    policy = _parse_enum("vcs.policy", value.get("policy"), VcsPolicy, VcsPolicy.BLOCK)
    allowed_schemes = _parse_string_list(
        "vcs.allowed-schemes", value.get("allowed-schemes", [])
    )
    unknown_schemes = sorted(set(allowed_schemes) - known_vcs_schemes())
    if unknown_schemes:
        msg = (
            f"unknown vcs.allowed-schemes: {unknown_schemes!r}; nab recognises"
            f" {sorted(known_vcs_schemes())!r}"
        )
        raise ConfigError(msg)
    allowed_repos = _parse_string_list(
        "vcs.allowed-repos", value.get("allowed-repos", [])
    )
    require_pin_raw = value.get("require-pin", True)
    if not isinstance(require_pin_raw, bool):
        msg = f"vcs.require-pin must be a boolean, got {type(require_pin_raw).__name__}"
        raise ConfigError(msg)
    return VcsConfig(
        policy=policy,
        allowed_schemes=frozenset(allowed_schemes),
        allowed_repos=tuple(allowed_repos),
        require_pin=require_pin_raw,
    )


_LOCAL_SOURCE_KEYS = frozenset({"name", "path", "editable", "subdirectory"})


def _parse_local_sources(
    value: object, *, pyproject_dir: Path
) -> tuple[LocalSource, ...]:
    if not isinstance(value, list):
        msg = f"local-sources must be an array of tables, got {type(value).__name__}"
        raise ConfigError(msg)
    out: list[LocalSource] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            msg = f"local-sources[{i}] must be a table, got {type(entry).__name__}"
            raise ConfigError(msg)
        unknown = sorted(set(entry) - _LOCAL_SOURCE_KEYS)
        if unknown:
            msg = (
                f"unknown local-sources[{i}] keys: {unknown!r};"
                f" expected {sorted(_LOCAL_SOURCE_KEYS)!r}"
            )
            raise ConfigError(msg)
        try:
            name = entry["name"]
            path_value = entry["path"]
        except KeyError as missing:
            msg = f"local-sources[{i}] missing required key {missing!s}"
            raise ConfigError(msg) from None
        if not isinstance(name, str) or not isinstance(path_value, str):
            msg = f"local-sources[{i}] name and path must be strings"
            raise ConfigError(msg)
        editable = entry.get("editable", False)
        if not isinstance(editable, bool):
            msg = f"local-sources[{i}] editable must be a boolean"
            raise ConfigError(msg)
        subdirectory = entry.get("subdirectory")
        if subdirectory is not None and not isinstance(subdirectory, str):
            msg = f"local-sources[{i}] subdirectory must be a string"
            raise ConfigError(msg)
        if subdirectory is not None and subdirectory_escapes(subdirectory):
            msg = (
                f"local-sources[{i}] subdirectory {subdirectory!r}"
                " escapes the source tree"
            )
            raise ConfigError(msg)
        resolved = str((pyproject_dir / path_value).resolve())
        out.append(
            LocalSource(
                name=name,
                path=resolved,
                editable=editable,
                subdirectory=subdirectory,
            )
        )
    return tuple(out)


_VCS_SOURCE_KEYS = frozenset({"name", "url"})


def _parse_vcs_sources(value: object) -> tuple[VcsSource, ...]:
    if not isinstance(value, list):
        msg = f"vcs-sources must be an array of tables, got {type(value).__name__}"
        raise ConfigError(msg)
    out: list[VcsSource] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            msg = f"vcs-sources[{i}] must be a table, got {type(entry).__name__}"
            raise ConfigError(msg)
        unknown = sorted(set(entry) - _VCS_SOURCE_KEYS)
        if unknown:
            msg = (
                f"unknown vcs-sources[{i}] keys: {unknown!r};"
                f" expected {sorted(_VCS_SOURCE_KEYS)!r}"
            )
            raise ConfigError(msg)
        try:
            name = entry["name"]
            url = entry["url"]
        except KeyError as missing:
            msg = f"vcs-sources[{i}] missing required key {missing!s}"
            raise ConfigError(msg) from None
        if not isinstance(name, str) or not isinstance(url, str):
            msg = f"vcs-sources[{i}] name and url must be strings"
            raise ConfigError(msg)
        out.append(VcsSource(name=name, url=url))
    return tuple(out)


_ARCHIVE_SOURCE_KEYS = frozenset({"name", "url"})


def _parse_archive_sources(value: object) -> tuple[ArchiveSource, ...]:
    if not isinstance(value, list):
        msg = f"archive-sources must be an array of tables, got {type(value).__name__}"
        raise ConfigError(msg)
    out: list[ArchiveSource] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            msg = f"archive-sources[{i}] must be a table, got {type(entry).__name__}"
            raise ConfigError(msg)
        unknown = sorted(set(entry) - _ARCHIVE_SOURCE_KEYS)
        if unknown:
            msg = (
                f"unknown archive-sources[{i}] keys: {unknown!r};"
                f" expected {sorted(_ARCHIVE_SOURCE_KEYS)!r}"
            )
            raise ConfigError(msg)
        try:
            name = entry["name"]
            url = entry["url"]
        except KeyError as missing:
            msg = f"archive-sources[{i}] missing required key {missing!s}"
            raise ConfigError(msg) from None
        if not isinstance(name, str) or not isinstance(url, str):
            msg = f"archive-sources[{i}] name and url must be strings"
            raise ConfigError(msg)
        _validate_archive_url(i, url)
        out.append(ArchiveSource(name=name, url=url))
    return tuple(out)


def _validate_archive_url(index: int, url: str) -> None:
    """Reject an archive URL with no hash or an unsupported format.

    PEP 751 ``packages.archive.hashes`` is required, so nab requires the
    hash in the URL fragment and verifies the download against it.  Only
    ``.tar.gz`` source archives are supported today; wheels and zips are
    refused loudly rather than mis-handled.
    """
    try:
        request = ArchiveRequest.parse(url)
    except ArchiveRequestError as exc:
        msg = f"archive-sources[{index}] url: {exc}"
        raise ConfigError(msg) from exc

    if not request.has_usable_hash:
        msg = (
            f"archive-sources[{index}] url {url!r} has no hash; add a"
            " '#sha256=<hex>' fragment (PEP 751 requires an archive hash)"
        )
        raise ConfigError(msg)

    if not urlsplit(request.url).path.endswith(".tar.gz"):
        msg = (
            f"archive-sources[{index}] url {url!r} is not a .tar.gz archive;"
            " only .tar.gz source archives are supported"
        )
        raise ConfigError(msg)


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
    for source in (*local_sources, *vcs_sources, *archive_sources):
        canonical = canonicalize_name(source.name)
        if canonical in seen:
            msg = (
                "[tool.nab] local-sources/vcs-sources/archive-sources declare"
                f" duplicate canonical name {canonical!r} via {seen[canonical]!r}"
                f" and {source.name!r}"
            )
            raise ConfigError(msg)
        seen[canonical] = source.name


def _reject_vcs_sources_under_block(
    vcs_sources: tuple[VcsSource, ...],
    vcs_config: VcsConfig,
) -> None:
    """Reject vcs-sources declared while the VCS policy blocks cloning.

    Cloning is opt-in, so a ``[[tool.nab.vcs-sources]]`` entry under the
    default ``policy = "block"`` is contradictory. Raising ConfigError here
    fails at parse time and names the token to set.

    ``policy = "allow"`` opens the gate but does not on its own admit a
    URL: ``allowed-schemes`` and ``allowed-repos`` are empty by default
    and each denies every URL until an entry is added, so the message
    points at the whole gate rather than promising that one key is enough.
    """
    if vcs_sources and vcs_config.policy is VcsPolicy.BLOCK:
        msg = (
            "[[tool.nab.vcs-sources]] is declared but [tool.nab.vcs].policy is"
            f" {vcs_config.policy.value!r}, which refuses every clone; remove"
            ' the sources, or set [tool.nab.vcs].policy = "allow" and open the'
            " rest of the gate (vcs.allowed-schemes and vcs.allowed-repos are"
            " empty by default and each denies every URL)"
        )
        raise ConfigError(msg)


def _parse_python_patches(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        msg = (
            "matrix.python-patches must be a table of"
            " minor -> full version, got"
            f" {type(value).__name__}"
        )
        raise ConfigError(msg)
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            msg = (
                "matrix.python-patches entries must be"
                f" string -> string, got {k!r}: {v!r}"
            )
            raise ConfigError(msg)
        try:
            minor = Version(k)
            full = Version(v)
        except InvalidVersion as exc:
            msg = f"matrix.python-patches expects version strings, got {k!r}: {v!r}"
            raise ConfigError(msg) from exc

        if full.release[:2] != minor.release[:2]:
            msg = f"matrix.python-patches value {v!r} is not a patch release of {k!r}"
            raise ConfigError(msg)
        out[k] = v
    return out


def _parse_workspace(value: object) -> WorkspaceConfig | None:
    """Parse the optional ``[tool.nab.workspace]`` table.

    Schema today is a single ``members`` field listing literal paths.
    Globs and member-existence checks happen in
    :func:`nab_python.workspace.workspace_local_sources`; this layer only
    validates the table shape so typos like ``member = ...`` (missing
    the ``s``) fail loud at config-parse time.
    """
    if not isinstance(value, dict):
        msg = f"[tool.nab.workspace] must be a table, got {type(value).__name__}"
        raise ConfigError(msg)
    allowed = {"members"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        msg = (
            f"unknown [tool.nab.workspace] keys: {unknown!r};"
            f" expected {sorted(allowed)!r}"
        )
        raise ConfigError(msg)
    members = _parse_string_list("workspace.members", value.get("members", []))
    return WorkspaceConfig(members=members)


# A declared conflict set must list at least this many members to mean
# anything.  Distinct from ``_MIN_ENGAGED_MEMBERS`` (a runtime engagement
# threshold), which happens to be the same number for unrelated reasons.
_MIN_CONFLICT_MEMBERS = 2
_CONFLICT_POLICY_VALUES = {p.value: p for p in ConflictPolicy}
_CONFLICT_SET_KEYS = frozenset({"members", "policy"})


def _parse_conflicts(value: object) -> tuple[ConflictSet, ...]:
    """Parse the optional ``[tool.nab].conflicts`` array.

    Each item is either a bare array of members (uv-compatible; the
    members are mutually exclusive under the default at-most-one
    policy) or a table ``{ members = [...], policy = "..." }`` whose
    ``policy`` value is ``at-most-one`` / ``exactly-one`` /
    ``at-least-one``.  A member is ``{ extra = "NAME" }`` or
    ``{ group = "NAME" }``.
    """
    if not isinstance(value, list):
        msg = f"conflicts must be an array of conflict sets, got {type(value).__name__}"
        raise ConfigError(msg)
    sets = tuple(_parse_conflict_set(item, i) for i, item in enumerate(value))
    _check_conflict_member_uniqueness(sets)
    return sets


def _check_conflict_member_uniqueness(sets: Sequence[ConflictSet]) -> None:
    """Reject a member declared in more than one conflict set.

    Shared by the single-file parse and the registry's across-file merge
    re-validation (config_sources): a member may belong to at most one
    conflict set, whether the duplicate is in one file or split across the
    two project files.
    """
    seen: set[ConflictMember] = set()
    for conflict_set in sets:
        for member in conflict_set.members:
            if member in seen:
                msg = (
                    f"conflicts declares {member} in more than one set;"
                    " a member may belong to at most one conflict set"
                )
                raise ConfigError(msg)
            seen.add(member)


def _validate_default_groups_against_conflicts(
    default_groups: Sequence[str],
    conflicts: Sequence[ConflictSet],
) -> None:
    """Reject default-groups that co-activate an exclusive conflict set.

    A default install activates every default group with no user
    selection, but the emit-time disjointness validator prunes any
    context that activates two members of an exclusive set, so it never
    enumerates that install.  Two default groups in the same at-most-one
    or exactly-one set would silently violate the declared conflict;
    catch it at parse time.
    """
    active = {canonicalize_name(g) for g in default_groups}
    for group in conflict_exclusion_groups(conflicts):
        co_active = sorted(
            name
            for kind, name in group
            if kind == ConflictKind.GROUP.value and name in active
        )
        if len(co_active) >= _MIN_ENGAGED_MEMBERS:
            joined = ", ".join(repr(name) for name in co_active)
            msg = (
                f"default-groups activates {joined}, which are declared"
                " mutually exclusive in [tool.nab].conflicts"
            )
            raise ConfigError(msg)


def _parse_conflict_set(item: object, index: int) -> ConflictSet:
    where = f"conflicts[{index}]"
    if isinstance(item, list):
        return ConflictSet(
            members=_parse_conflict_members(item, where),
            policy=ConflictPolicy.AT_MOST_ONE,
        )
    if isinstance(item, dict):
        unknown = sorted(set(item) - _CONFLICT_SET_KEYS)
        if unknown:
            valid = sorted(_CONFLICT_POLICY_VALUES)
            msg = (
                f"{where}: unknown conflict-set key(s) {unknown!r}; expected a"
                f" table {{ members = [...], policy = '...' }} with policy one of"
                f" {valid!r}, or a bare array of members"
            )
            raise ConfigError(msg)
        if "members" not in item:
            msg = f"{where}: a conflict-set table must set 'members'"
            raise ConfigError(msg)
        policy = _parse_conflict_policy(item.get("policy"), where)
        return ConflictSet(
            members=_parse_conflict_members(item["members"], f"{where}.members"),
            policy=policy,
        )
    msg = (
        f"{where} must be an array of members or a conflict-set table, got"
        f" {type(item).__name__}"
    )
    raise ConfigError(msg)


def _parse_conflict_policy(value: object, where: str) -> ConflictPolicy:
    """Parse the ``policy`` value of a conflict-set table; default at-most-one."""
    if value is None:
        return ConflictPolicy.AT_MOST_ONE
    if not isinstance(value, str):
        msg = f"{where}.policy must be a string, got {type(value).__name__}"
        raise ConfigError(msg)
    policy = _CONFLICT_POLICY_VALUES.get(value)
    if policy is None:
        valid = sorted(_CONFLICT_POLICY_VALUES)
        msg = f"{where}.policy must be one of {valid!r}, got {value!r}"
        raise ConfigError(msg)
    return policy


def _parse_conflict_members(value: object, where: str) -> tuple[ConflictMember, ...]:
    if not isinstance(value, list):
        msg = f"{where} must be an array of members, got {type(value).__name__}"
        raise ConfigError(msg)
    members = tuple(
        _parse_conflict_member(item, f"{where}[{i}]") for i, item in enumerate(value)
    )
    if len(members) < _MIN_CONFLICT_MEMBERS:
        msg = (
            f"{where} must list at least {_MIN_CONFLICT_MEMBERS} members to be"
            f" a conflict; got {len(members)}"
        )
        raise ConfigError(msg)
    if len(set(members)) != len(members):
        msg = f"{where} lists a member more than once"
        raise ConfigError(msg)
    return members


def _parse_conflict_member(item: object, where: str) -> ConflictMember:
    if not isinstance(item, dict):
        msg = (
            f"{where} must be a table {{ extra = ... }} or {{ group = ... }},"
            f" got {type(item).__name__}"
        )
        raise ConfigError(msg)
    kinds = {k.value for k in ConflictKind}
    unknown = sorted(set(item) - kinds)
    if unknown:
        msg = f"{where}: unknown member key(s) {unknown!r}; expected {sorted(kinds)!r}"
        raise ConfigError(msg)
    present = sorted(set(item) & kinds)
    if len(present) != 1:
        msg = f"{where} must name exactly one of {sorted(kinds)!r}, got {present!r}"
        raise ConfigError(msg)
    kind = ConflictKind(present[0])
    name = item[present[0]]
    if not isinstance(name, str) or not name:
        msg = f"{where}.{kind.value} must be a non-empty string, got {name!r}"
        raise ConfigError(msg)
    try:
        canonical = canonicalize_name(name, validate=True)
    except InvalidName:
        canonical = canonicalize_name(name)
        msg = (
            f"{where}.{kind.value} is not a valid extra/group name: {name!r}"
            f" (canonicalises to {canonical!r})"
        )
        raise ConfigError(msg) from None
    return ConflictMember(kind=kind, name=canonical)


_MINOR_RELEASE_PARTS = 2


def _validate_matrix_python(spec: str) -> None:
    """Reject a matrix.python axis finer than major.minor.

    The axis lists language (minor) Python versions; patch pins belong in
    [tool.nab.matrix.python-patches].
    """
    try:
        specifier_set = SpecifierSet(spec)
    except InvalidSpecifier as exc:
        msg = f"matrix.python must be a PEP 440 specifier, got {spec!r}"
        raise ConfigError(msg) from exc
    for clause in specifier_set:
        try:
            version = Version(clause.version.removesuffix(".*"))
        except InvalidVersion as exc:
            msg = f"matrix.python clause {clause} is not a valid version"
            raise ConfigError(msg) from exc

        # Reject pre/post/dev/local qualifiers and patch-level release tuples.
        finer = (
            version.epoch != 0,
            version.pre is not None,
            version.post is not None,
            version.dev is not None,
            version.local is not None,
        )
        if len(version.release) > _MINOR_RELEASE_PARTS or any(finer):
            msg = (
                "matrix.python axis is a language (minor) version only; "
                f"{clause} is finer than major.minor. Put patch versions in "
                "[tool.nab.matrix.python-patches]."
            )
            raise ConfigError(msg)


_PLATFORM_TABLE_KEYS = frozenset(
    {
        "id",
        "libc",
        "runs-on-libc",
        "runs-on-macos",
        "platform-release",
        "platform-version",
        "free-threaded",
    }
)
# The platform kind that reads each knob key; any other kind rejects it.
_PLATFORM_KNOB_OWNER: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "linux": frozenset({"libc", "runs-on-libc"}),
        "macos": frozenset({"runs-on-macos"}),
    }
)


def _parse_matrix_platforms(value: object) -> tuple[PlatformSpec, ...]:
    """Parse ``matrix.platforms``: bare ids, tables, or a mix of both.

    A bare id takes the platform's default tag knobs; the table form declares
    them (libc family, the libc and macOS the lock must run on, kernel
    marker values, free-threaded build).  Both become a :class:`PlatformSpec`,
    so everything downstream reads one shape.
    """
    if not isinstance(value, list):
        msg = f"matrix.platforms must be a list, got {type(value).__name__}"
        raise ConfigError(msg)
    platforms: list[PlatformSpec] = []
    for i, item in enumerate(value):
        where = f"matrix.platforms[{i}]"
        if isinstance(item, str):
            platforms.append(_platform_spec(where, platform_id=item))
        elif isinstance(item, dict):
            platforms.append(_parse_platform_table(where, item))
        else:
            msg = f"{where} must be a platform id or a table, got {type(item).__name__}"
            raise ConfigError(msg)
    return tuple(platforms)


def _platform_spec(where: str, **knobs: Any) -> PlatformSpec:
    """Build a :class:`PlatformSpec`, reporting its knob check as a config error."""
    try:
        return PlatformSpec(**knobs)
    except ValueError as exc:
        msg = f"invalid {where}: {exc}"
        raise ConfigError(msg) from exc


def _parse_platform_table(where: str, value: dict[str, Any]) -> PlatformSpec:
    """Parse one ``matrix.platforms`` table entry into a :class:`PlatformSpec`."""
    unknown = sorted(set(value) - _PLATFORM_TABLE_KEYS)
    if unknown:
        msg = (
            f"unknown {where} keys: {unknown!r};"
            f" expected {sorted(_PLATFORM_TABLE_KEYS)!r}"
        )
        raise ConfigError(msg)
    if "id" not in value:
        msg = f"{where} missing required key 'id'"
        raise ConfigError(msg)

    platform_id = _parse_string_value(f"{where}.id", value["id"])
    _reject_foreign_knobs(where, value, platform_id)

    return _platform_spec(
        where,
        platform_id=platform_id,
        libc=_parse_libc(f"{where}.libc", value.get("libc")),
        runs_on_libc=_parse_major_minor(
            f"{where}.runs-on-libc", value.get("runs-on-libc")
        ),
        runs_on_macos=_parse_major_minor(
            f"{where}.runs-on-macos", value.get("runs-on-macos")
        ),
        platform_release=_parse_string_value(
            f"{where}.platform-release", value.get("platform-release", "")
        ),
        platform_version=_parse_string_value(
            f"{where}.platform-version", value.get("platform-version", "")
        ),
        free_threaded=_parse_bool(
            f"{where}.free-threaded", value.get("free-threaded"), default=False
        ),
    )


def _reject_foreign_knobs(where: str, value: dict[str, Any], platform_id: str) -> None:
    """Reject a knob key the declared platform's kind cannot read.

    :class:`PlatformSpec` refuses a knob whose *value* moves a platform that
    ignores it, but it cannot see a key written at its own default.  The
    table can, and a key that selects no wheel is a mistake either way.  An
    unknown ``platform_id`` is left to the matrix, which names the whole
    unknown set at once.
    """
    kind = platform_kind(platform_id)
    if kind is None:
        return
    for owner, keys in _PLATFORM_KNOB_OWNER.items():
        if kind == owner:
            continue
        foreign = sorted(keys & set(value))
        if foreign:
            msg = (
                f"{where} declares {foreign!r}, which only a {owner} platform"
                f" reads, but its id is {platform_id!r}"
            )
            raise ConfigError(msg)


def _parse_libc(key: str, value: object) -> Libc:
    """Parse a libc family name; an absent key takes the default family."""
    if value is None:
        return DEFAULT_LIBC
    text = _parse_string_value(key, value)
    if text not in LIBC_MAJOR:
        msg = f"{key} must be one of {sorted(LIBC_MAJOR)!r}, got {text!r}"
        raise ConfigError(msg)
    return cast("Libc", text)


def _parse_major_minor(key: str, value: object) -> tuple[int, int] | None:
    """Parse a ``major.minor`` string into a pair; ``None`` passes through."""
    if value is None:
        return None
    text = _parse_string_value(key, value)
    try:
        version = Version(text)
    except InvalidVersion as exc:
        msg = f"{key} must be a 'major.minor' version, got {text!r}"
        raise ConfigError(msg) from exc
    release = version.release
    two_part = len(release) == _MINOR_RELEASE_PARTS
    # str() renders the normalized version, so an epoch or a pre/post/dev/local
    # qualifier shows up as a mismatch here.
    if not two_part or str(version) != f"{release[0]}.{release[1]}":
        msg = f"{key} must be exactly 'major.minor', got {text!r}"
        raise ConfigError(msg)
    return (release[0], release[1])


_MATRIX_KEYS = frozenset(
    {
        "python",
        "platforms",
        "python-order",
        "python-patches",
        "implementations",
    }
)


def _parse_matrix(value: object) -> MatrixConfig | None:
    if not isinstance(value, dict):
        msg = f"[tool.nab.matrix] must be a table, got {type(value).__name__}"
        raise ConfigError(msg)
    unknown = sorted(set(value) - _MATRIX_KEYS)
    if unknown:
        msg = (
            f"unknown [tool.nab.matrix] keys: {unknown!r};"
            f" expected {sorted(_MATRIX_KEYS)!r}"
        )
        raise ConfigError(msg)
    try:
        python = value["python"]
        platforms_raw = value["platforms"]
    except KeyError as missing:
        msg = f"[tool.nab.matrix] missing required key {missing!s}"
        raise ConfigError(msg) from None
    if not isinstance(python, str):
        msg = "matrix.python must be a string PEP 440 specifier"
        raise ConfigError(msg)
    _validate_matrix_python(python)
    platforms = _parse_matrix_platforms(platforms_raw)
    if not platforms:
        msg = "matrix.platforms must list at least one platform id"
        raise ConfigError(msg)
    # One target per platform id.  A lockfile entry is selected by a PEP 508
    # marker, which has no libc or free-threading variable, so two targets
    # sharing an id would render the same marker.
    _reject_duplicates("matrix.platforms", tuple(p.platform_id for p in platforms))
    python_order = value.get("python-order", "asc")
    if python_order not in {"asc", "desc"}:
        msg = f"matrix.python-order must be 'asc' or 'desc', got {python_order!r}"
        raise ConfigError(msg)
    patches = _parse_python_patches(value.get("python-patches"))
    implementations = _parse_implementations(value.get("implementations"))
    config = MatrixConfig(
        python=python,
        platforms=platforms,
        python_order=python_order,
        python_patches=patches,
        implementations=implementations,
    )
    _validate_matrix_axes(config)
    return config


def _validate_matrix_axes(config: MatrixConfig) -> None:
    """Expand the matrix eagerly to catch bad axes at parse time."""
    matrix = matrix_from_config(config)
    try:
        matrix.expand()
    except ValueError as exc:
        msg = f"invalid [tool.nab.matrix]: {exc}"
        raise ConfigError(msg) from exc


_KNOWN_IMPLEMENTATIONS = ("cpython", "pypy")


def _parse_implementations(value: object) -> tuple[str, ...]:
    if value is None:
        return ("cpython",)
    impls = _parse_string_list("matrix.implementations", value)
    if not impls:
        msg = "matrix.implementations must list at least one implementation"
        raise ConfigError(msg)
    _reject_duplicates("matrix.implementations", impls)
    unknown = sorted(set(impls) - set(_KNOWN_IMPLEMENTATIONS))
    if unknown:
        msg = (
            f"unknown matrix.implementations: {unknown!r}; "
            f"expected {list(_KNOWN_IMPLEMENTATIONS)!r}"
        )
        raise ConfigError(msg)
    return impls
