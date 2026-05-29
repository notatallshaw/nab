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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import tomli

from nab_index.multi_index import IndexConfig

from ._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from ._vendor.packaging.utils import canonicalize_name
from ._vendor.packaging.version import InvalidVersion, Version
from .fetch import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL, IndexOverride
from .provider import (
    BuildPolicy,
    DistPolicy,
    LocalSource,
    ResolutionStrategy,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from .universal.matrix import Matrix
from .workspace import (
    WorkspaceConfig,
    auto_promote_build_policy_for_workspace,
    discover_workspace_root,
    merge_workspace_local_sources,
    read_workspace_members,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = [
    "ConfigError",
    "ConflictFork",
    "ConflictKind",
    "ConflictMember",
    "ConflictPolicy",
    "ConflictSelectionError",
    "ConflictSet",
    "MatrixConfig",
    "NabProjectConfig",
    "ResolveMode",
    "conflict_exclusion_groups",
    "conflict_forks",
    "read_pyproject_config",
    "validate_conflict_selection",
]


_TOP_LEVEL_KEYS = frozenset(
    {
        "mode",
        "constraints",
        "default-groups",
        "requires-python",
        "uploaded-prior-to",
        "uploaded-prior-to-package",
        "dist-policy",
        "dist-policy-package",
        "build-policy",
        "build-policy-package",
        "marker-environment",
        "indexes",
        "index-overrides",
        "vcs",
        "local-sources",
        "vcs-sources",
        "matrix",
        "resolution",
        "workspace",
        "conflicts",
    },
)

_DURATION_PATTERN = re.compile(r"^P(\d+)D$")


class ResolveMode(enum.Enum):
    """How the resolver interprets the project.

    ``SPECIFIC`` runs the single-environment resolver against one
    marker environment (host or impersonated).  ``UNIVERSAL`` runs the
    matrix-based per-tuple resolver and is *experimental*: users
    must opt in by setting ``[tool.nab].mode = "universal"`` and
    declaring ``[tool.nab.matrix]``.
    """

    SPECIFIC = "specific"
    UNIVERSAL = "universal"


@dataclass(frozen=True, slots=True)
class MatrixConfig:
    """User-declared matrix axes for universal resolution."""

    python: str
    platforms: tuple[str, ...]
    python_order: str = "asc"
    python_patches: Mapping[str, str] | None = None
    implementations: tuple[str, ...] = ("cpython",)


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

    AT_MOST_ONE = "at_most_one"
    EXACTLY_ONE = "exactly_one"
    AT_LEAST_ONE = "at_least_one"


class ConflictKind(enum.Enum):
    """Whether a :class:`ConflictMember` names an extra or a group."""

    EXTRA = "extra"
    GROUP = "group"


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

    def __str__(self) -> str:
        """Render as ``extra 'cpu'`` / ``group 'black22'`` for messages."""
        return f"{self.kind.value} {self.name!r}"


@dataclass(frozen=True, slots=True)
class ConflictSet:
    """A set of mutually-exclusive members with an exclusivity policy."""

    members: tuple[ConflictMember, ...]
    policy: ConflictPolicy = ConflictPolicy.AT_MOST_ONE

    def __str__(self) -> str:
        """Render as ``at_most_one (extra 'cpu', extra 'gpu')`` for messages."""
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
    requires_python: str | None = None
    uploaded_prior_to: datetime | None = None
    uploaded_prior_to_overrides: Mapping[str, datetime | None] = field(
        default_factory=dict
    )
    dist_policy: DistPolicy = DistPolicy.WHEEL_OR_SDIST
    dist_policy_overrides: Mapping[str, DistPolicy] = field(default_factory=dict)
    build_policy: BuildPolicy = BuildPolicy.BUILD_LOCAL
    build_policy_overrides: Mapping[str, BuildPolicy] = field(default_factory=dict)
    marker_environment: Mapping[str, str] = field(default_factory=dict)
    indexes: tuple[IndexConfig, ...] = (
        IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),
    )
    index_overrides: tuple[IndexOverride, ...] = ()
    vcs: VcsConfig = field(default_factory=VcsConfig)
    local_sources: tuple[LocalSource, ...] = ()
    vcs_sources: tuple[VcsSource, ...] = ()
    matrix: MatrixConfig | None = None
    resolution: ResolutionStrategy = ResolutionStrategy.HIGHEST
    workspace: WorkspaceConfig | None = None
    conflicts: tuple[ConflictSet, ...] = ()
    # Canonical names of workspace members. Populated by
    # _apply_workspace_discovery; empty otherwise. Distinct from
    # ``local_sources``, which also carries explicit
    # ``[[tool.nab.local-sources]]`` entries.
    workspace_member_names: frozenset[str] = field(default_factory=frozenset)


class ConfigError(ValueError):
    """Raised when ``[tool.nab]`` is structurally invalid."""


class ConflictSelectionError(ConfigError):
    """A requested extra/group selection violates a declared conflict.

    Raised on the single-environment path, where one resolution cannot
    serve two mutually-exclusive members at once.  Universal mode forks
    the resolve instead of raising.
    """


def _member_active(
    member: ConflictMember,
    active_extras: set[str],
    active_groups: set[str],
) -> bool:
    """Return True when ``member`` is in the selected extras/groups."""
    if member.kind is ConflictKind.EXTRA:
        return member.name in active_extras
    return member.name in active_groups


def validate_conflict_selection(
    conflicts: Sequence[ConflictSet],
    selected_extras: Sequence[str],
    selected_groups: Sequence[str],
) -> None:
    """Raise when a selection breaks a declared conflict's policy.

    For a single-environment resolve, two members of an at-most-one or
    exactly-one set cannot both be active, an exactly-one set must have
    one active member, and an at-least-one set must have at least one.
    Names compare under canonicalisation.  Universal mode does not call
    this for the co-selection case: it forks the matrix so each member
    resolves on its own.
    """
    active_extras = {canonicalize_name(e) for e in selected_extras}
    active_groups = {canonicalize_name(g) for g in selected_groups}
    for conflict_set in conflicts:
        active = [
            m
            for m in conflict_set.members
            if _member_active(m, active_extras, active_groups)
        ]
        policy = conflict_set.policy
        exclusive = {ConflictPolicy.AT_MOST_ONE, ConflictPolicy.EXACTLY_ONE}
        if len(active) > 1 and policy in exclusive:
            chosen = ", ".join(str(m) for m in active)
            msg = (
                f"{chosen} cannot be selected together: declared mutually"
                f" exclusive ({policy.value}) in [tool.nab].conflicts"
            )
            raise ConflictSelectionError(msg)
        if not active and policy is ConflictPolicy.EXACTLY_ONE:
            msg = f"exactly one of {conflict_set} must be selected"
            raise ConflictSelectionError(msg)
        if not active and policy is ConflictPolicy.AT_LEAST_ONE:
            msg = f"at least one of {conflict_set} must be selected"
            raise ConflictSelectionError(msg)


def read_pyproject_config(
    path: Path,
    *,
    discover_workspace: bool = True,
    anchor: datetime | None = None,
) -> NabProjectConfig:
    """Parse ``[tool.nab]`` from ``path`` into :class:`NabProjectConfig`.

    Returns the default ``NabProjectConfig`` when the table is absent.
    Unknown keys at the top level are rejected so typos fail loud.

    When ``discover_workspace`` is true (the default), this function
    also walks up from ``path`` looking for a ``pyproject.toml`` whose
    ``[tool.nab.workspace]`` table is present.  If found, every member
    is materialised as an additional :class:`LocalSource` (explicit
    ``[[tool.nab.local-sources]]`` entries win on collision) and the
    effective ``build-policy`` is floored at
    :attr:`BuildPolicy.BUILD_LOCAL`.  Pass ``discover_workspace=False``
    to skip the walk; useful for tests or for callers that layer their
    own workspace logic on top of a base config.

    ``anchor`` is the timestamp ``P<n>D`` durations resolve against.
    Defaults to ``datetime.now(UTC)`` when not supplied, which gives
    fresh-resolve semantics.  The ``nab lock`` CLI passes the anchor
    captured in any existing lockfile so re-locks reproduce the same
    cutoff for relative durations.
    """
    if anchor is None:
        anchor = datetime.now(timezone.utc)
    with path.open("rb") as f:
        data = tomli.load(f)
    raw = data.get("tool", {}).get("nab", {})
    if not isinstance(raw, dict):
        msg = f"[tool.nab] must be a table, got {type(raw).__name__}"
        raise ConfigError(msg)
    config = _parse_nab_table(raw, anchor=anchor, pyproject_dir=path.parent.resolve())
    if discover_workspace:
        config = _apply_workspace_discovery(path, config)
    return config


_logger = logging.getLogger(__name__)


def _apply_workspace_discovery(
    path: Path, config: NabProjectConfig
) -> NabProjectConfig:
    root_pyproject = discover_workspace_root(path)
    if root_pyproject is None:
        return config
    discovered = read_workspace_members(root_pyproject)
    if not discovered:
        return config
    merged = merge_workspace_local_sources(config.local_sources, discovered)
    promoted_policy = auto_promote_build_policy_for_workspace(config.build_policy)
    if promoted_policy is not config.build_policy:
        _logger.info(
            "workspace discovery promoted build-policy from %r to %r"
            " (workspace root: %s)",
            config.build_policy.value,
            promoted_policy.value,
            root_pyproject,
        )
    return replace(
        config,
        local_sources=merged,
        build_policy=promoted_policy,
        workspace_member_names=frozenset(
            canonicalize_name(src.name) for src in discovered
        ),
    )


def _parse_nab_table(
    raw: dict[str, Any], *, anchor: datetime, pyproject_dir: Path
) -> NabProjectConfig:
    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        msg = (
            f"unknown [tool.nab] keys: {unknown!r}; expected one of"
            f" {sorted(_TOP_LEVEL_KEYS)!r}"
        )
        raise ConfigError(msg)

    mode = _parse_mode(raw.get("mode"))
    matrix = _parse_matrix(raw.get("matrix"))
    if mode is ResolveMode.UNIVERSAL and matrix is None:
        msg = (
            "mode = 'universal' requires a [tool.nab.matrix] table"
            " declaring python and platforms"
        )
        raise ConfigError(msg)
    if mode is ResolveMode.SPECIFIC and matrix is not None:
        msg = (
            "[tool.nab.matrix] is set but mode is 'specific'; set"
            " mode = 'universal' to opt in to the experimental"
            " matrix-based resolver"
        )
        raise ConfigError(msg)

    index_overrides = _parse_index_overrides(raw.get("index-overrides", []))
    if mode is ResolveMode.UNIVERSAL:
        marker_gated = sorted(o.name for o in index_overrides if o.marker)
        if marker_gated:
            joined = ", ".join(marker_gated)
            msg = (
                "mode = 'universal' does not support marker-gated"
                f" [[tool.nab.index-overrides]] ({joined}): a universal lock"
                " fetches each package once and cannot route it to a"
                " different index per environment. Drop the marker or use"
                " mode = 'specific'."
            )
            raise ConfigError(msg)

    return NabProjectConfig(
        mode=mode,
        constraints=_parse_string_list("constraints", raw.get("constraints", [])),
        default_groups=_parse_string_list(
            "default-groups", raw.get("default-groups", [])
        ),
        requires_python=_parse_requires_python(raw.get("requires-python")),
        uploaded_prior_to=_parse_uploaded_prior_to(
            raw.get("uploaded-prior-to"), anchor=anchor
        ),
        uploaded_prior_to_overrides=_parse_uploaded_prior_to_package(
            raw.get("uploaded-prior-to-package"), anchor=anchor
        ),
        dist_policy=_parse_enum(
            "dist-policy", raw.get("dist-policy"), DistPolicy, DistPolicy.WHEEL_OR_SDIST
        ),
        dist_policy_overrides=_parse_dist_policy_package(
            raw.get("dist-policy-package")
        ),
        build_policy=_parse_enum(
            "build-policy",
            raw.get("build-policy"),
            BuildPolicy,
            BuildPolicy.BUILD_LOCAL,
        ),
        build_policy_overrides=_parse_build_policy_package(
            raw.get("build-policy-package")
        ),
        marker_environment=_parse_marker_environment(raw.get("marker-environment", {})),
        indexes=_parse_indexes(raw.get("indexes")),
        index_overrides=index_overrides,
        vcs=_parse_vcs(raw.get("vcs", {})),
        local_sources=_parse_local_sources(
            raw.get("local-sources", []), pyproject_dir=pyproject_dir
        ),
        vcs_sources=_parse_vcs_sources(raw.get("vcs-sources", [])),
        matrix=matrix,
        resolution=_parse_enum(
            "resolution",
            raw.get("resolution"),
            ResolutionStrategy,
            ResolutionStrategy.HIGHEST,
        ),
        workspace=_parse_workspace(raw.get("workspace")),
        conflicts=_parse_conflicts(raw.get("conflicts")),
    )


def _parse_mode(value: object) -> ResolveMode:
    if value is None:
        return ResolveMode.SPECIFIC
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


def _parse_optional_string(key: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{key} must be a string, got {type(value).__name__}"
        raise ConfigError(msg)
    return value


def _parse_requires_python(value: object) -> str | None:
    """Parse ``[tool.nab].requires-python`` as a PEP 440 specifier.

    Stored as the raw specifier string so the lockfile writer can pass
    it straight to :class:`SpecifierSet`.  The resolve path later
    derives a concrete Python version target from the same specifier.
    Raises :class:`ConfigError` for invalid specifiers and for
    well-meaning bare versions like ``"3.13"``; those are not valid
    specifiers and must be written ``"==3.13"`` or ``">=3.13,<3.14"``.
    """
    raw = _parse_optional_string("requires-python", value)
    if raw is None:
        return None
    try:
        SpecifierSet(raw)
    except InvalidSpecifier as exc:
        msg = (
            f"requires-python must be a PEP 440 specifier, got {raw!r}."
            f"  Did you mean ==X.Y or >=X.Y,<X.{{Y+1}}?"
        )
        raise ConfigError(msg) from exc
    return raw


def _parse_uploaded_prior_to(value: object, *, anchor: datetime) -> datetime | None:
    """Parse ``uploaded-prior-to`` (ISO datetime, TOML datetime, or ``P<n>D``).

    Naive datetimes are rejected so lockfiles read identically across
    timezones. ``P<n>D`` (a nab extension) is resolved against
    ``anchor`` so re-locks reproduce the same cutoff.
    """
    if value is None:
        return None

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
    # Python 3.10's fromisoformat rejects a trailing 'Z'; 3.11+ accept it.
    iso_value = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(iso_value)
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


def read_pyproject_lock_anchor(path: Path) -> datetime | None:
    """Return the absolute ``uploaded-prior-to`` timestamp, if one is set.

    A ``P<n>D`` duration is anchored to run time, so it is not reproducible
    and returns ``None``. ``nab lock`` uses an absolute cutoff as the lock
    anchor so re-locks from identical inputs produce identical bytes.
    Absent or invalid values return ``None``; the full config parse reports
    the error, including a missing file.
    """
    try:
        with path.open("rb") as f:
            data = tomli.load(f)
    except (FileNotFoundError, tomli.TOMLDecodeError):
        return None
    raw = data.get("tool", {}).get("nab", {})
    value = raw.get("uploaded-prior-to") if isinstance(raw, dict) else None
    if isinstance(value, str) and _DURATION_PATTERN.match(value):
        return None
    try:
        return _parse_uploaded_prior_to(value, anchor=datetime.now(timezone.utc))
    except ConfigError:
        return None


def _parse_uploaded_prior_to_package(
    value: object, *, anchor: datetime
) -> Mapping[str, datetime | None]:
    """Parse the optional ``[tool.nab.uploaded-prior-to-package]`` table.

    Each entry maps a package name to either:

    - ``false``: disable the cooldown for that package entirely.
    - any value :func:`_parse_uploaded_prior_to` accepts (ISO datetime
      with timezone, TOML offset-date-time, or ``"P<n>D"`` duration);
      use that cutoff for the package, overriding the global value.

    ``true`` is rejected: the only meaningful boolean is ``false``
    ("no cooldown").  Package names are canonicalised so that
    ``Foo-Bar`` and ``foo_bar`` collapse to the same entry; a
    duplicate raises :class:`ConfigError` with the original keys
    in the message.

    ``anchor`` is forwarded to :func:`_parse_uploaded_prior_to` so
    per-package ``P<n>D`` overrides resolve against the same reference
    timestamp as the global value.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        msg = (
            "[tool.nab.uploaded-prior-to-package] must be a table,"
            f" got {type(value).__name__}"
        )
        raise ConfigError(msg)
    out: dict[str, datetime | None] = {}
    seen: dict[str, str] = {}
    for raw_key, raw_val in value.items():
        if raw_val is True:
            msg = (
                f"[tool.nab.uploaded-prior-to-package].{raw_key}: ``true`` is"
                " not a valid override; use ``false`` to disable the cooldown"
                " or an absolute datetime / 'PnD' duration to set a window"
            )
            raise ConfigError(msg)
        cutoff: datetime | None
        if raw_val is False:
            cutoff = None
        else:
            try:
                cutoff = _parse_uploaded_prior_to(raw_val, anchor=anchor)
            except ConfigError as exc:
                msg = f"[tool.nab.uploaded-prior-to-package].{raw_key}: {exc}"
                raise ConfigError(msg) from exc
        canonical = canonicalize_name(raw_key)
        if canonical in seen:
            msg = (
                "[tool.nab.uploaded-prior-to-package] declares duplicate"
                f" canonical name {canonical!r} via {seen[canonical]!r}"
                f" and {raw_key!r}"
            )
            raise ConfigError(msg)
        seen[canonical] = raw_key
        out[canonical] = cutoff
    return out


def _parse_dist_policy_package(value: object) -> Mapping[str, DistPolicy]:
    """Parse the optional ``[tool.nab.dist-policy-package]`` table.

    Maps a package name to one of the :class:`DistPolicy` values
    ("no-sdist", "prefer-binary", "allow", "sdist-only").  The
    per-package value overrides the global ``dist-policy`` for that
    package; mirrors pip's ``--no-binary-package <pkg>`` shape by
    mapping ``<pkg> = "sdist-only"``.

    Package names are canonicalised; duplicates raise
    :class:`ConfigError` with the original keys named.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        msg = (
            "[tool.nab.dist-policy-package] must be a table,"
            f" got {type(value).__name__}"
        )
        raise ConfigError(msg)
    out: dict[str, DistPolicy] = {}
    seen: dict[str, str] = {}
    valid = sorted(p.value for p in DistPolicy)
    for raw_key, raw_val in value.items():
        if not isinstance(raw_val, str):
            msg = (
                f"[tool.nab.dist-policy-package].{raw_key} must be a string,"
                f" got {type(raw_val).__name__}"
            )
            raise ConfigError(msg)
        try:
            policy = DistPolicy(raw_val)
        except ValueError as exc:
            msg = (
                f"[tool.nab.dist-policy-package].{raw_key} must be one of"
                f" {valid!r}, got {raw_val!r}"
            )
            raise ConfigError(msg) from exc
        canonical = canonicalize_name(raw_key)
        if canonical in seen:
            msg = (
                "[tool.nab.dist-policy-package] declares duplicate canonical"
                f" name {canonical!r} via {seen[canonical]!r} and {raw_key!r}"
            )
            raise ConfigError(msg)
        seen[canonical] = raw_key
        out[canonical] = policy
    return out


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
        out[k] = v
    return out


def _parse_indexes(value: object) -> tuple[IndexConfig, ...]:
    if value is None:
        return (IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),)

    if not isinstance(value, list):
        msg = f"indexes must be an array of tables, got {type(value).__name__}"
        raise ConfigError(msg)

    if not value:
        msg = "indexes must contain at least one entry when present"
        raise ConfigError(msg)

    out: list[IndexConfig] = []
    seen: set[str] = set()
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            msg = f"indexes[{i}] must be a table, got {type(entry).__name__}"
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
        if name in seen:
            msg = f"duplicate index name: {name!r}"
            raise ConfigError(msg)
        seen.add(name)
        out.append(IndexConfig(name=name, url=url))
    return tuple(out)


def _parse_index_overrides(value: object) -> tuple[IndexOverride, ...]:
    if not isinstance(value, list):
        msg = f"index-overrides must be an array of tables, got {type(value).__name__}"
        raise ConfigError(msg)
    out: list[IndexOverride] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            msg = f"index-overrides[{i}] must be a table, got {type(entry).__name__}"
            raise ConfigError(msg)
        try:
            name = entry["name"]
            index = entry["index"]
        except KeyError as missing:
            msg = f"index-overrides[{i}] missing required key {missing!s}"
            raise ConfigError(msg) from None
        marker = entry.get("marker")
        if not isinstance(name, str) or not isinstance(index, str):
            msg = f"index-overrides[{i}] name and index must be strings"
            raise ConfigError(msg)
        if marker is not None and not isinstance(marker, str):
            msg = f"index-overrides[{i}] marker must be a string when set"
            raise ConfigError(msg)
        out.append(IndexOverride(name=name, index=index, marker=marker))
    return tuple(out)


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


def _parse_build_policy_package(value: object) -> Mapping[str, BuildPolicy]:
    """Parse the optional ``[tool.nab.build-policy-package]`` table.

    Maps a package name to one of the :class:`BuildPolicy` values
    ("never", "build-local", "build-remote").  The per-package value
    overrides the global ``build-policy`` for that package.

    Package names are canonicalised; duplicates raise
    :class:`ConfigError` with the original keys named.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        msg = (
            "[tool.nab.build-policy-package] must be a table,"
            f" got {type(value).__name__}"
        )
        raise ConfigError(msg)
    out: dict[str, BuildPolicy] = {}
    seen: dict[str, str] = {}
    valid = sorted(p.value for p in BuildPolicy)
    for raw_key, raw_val in value.items():
        if not isinstance(raw_val, str):
            msg = (
                f"[tool.nab.build-policy-package].{raw_key} must be a string,"
                f" got {type(raw_val).__name__}"
            )
            raise ConfigError(msg)
        try:
            policy = BuildPolicy(raw_val)
        except ValueError as exc:
            msg = (
                f"[tool.nab.build-policy-package].{raw_key} must be one of"
                f" {valid!r}, got {raw_val!r}"
            )
            raise ConfigError(msg) from exc
        canonical = canonicalize_name(raw_key)
        if canonical in seen:
            msg = (
                "[tool.nab.build-policy-package] declares duplicate canonical"
                f" name {canonical!r} via {seen[canonical]!r} and {raw_key!r}"
            )
            raise ConfigError(msg)
        seen[canonical] = raw_key
        out[canonical] = policy
    return out


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


def _parse_vcs_sources(value: object) -> tuple[VcsSource, ...]:
    if not isinstance(value, list):
        msg = f"vcs-sources must be an array of tables, got {type(value).__name__}"
        raise ConfigError(msg)
    out: list[VcsSource] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            msg = f"vcs-sources[{i}] must be a table, got {type(entry).__name__}"
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
        out[k] = v
    return out


def _parse_workspace(value: object) -> WorkspaceConfig | None:
    """Parse the optional ``[tool.nab.workspace]`` table.

    Schema today is a single ``members`` field listing literal paths.
    Globs and member-existence checks happen in
    :func:`nab_python.workspace.read_workspace_members`; this layer only
    validates the table shape so typos like ``member = ...`` (missing
    the ``s``) fail loud at config-parse time.
    """
    if value is None:
        return None
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


_MIN_CONFLICT_MEMBERS = 2
_CONFLICT_POLICY_KEYS = {p.value: p for p in ConflictPolicy}


def _parse_conflicts(value: object) -> tuple[ConflictSet, ...]:
    """Parse the optional ``[tool.nab].conflicts`` array.

    Each item is either a bare array of members (uv-compatible; the
    members are mutually exclusive under the default at-most-one
    policy) or a single-key table naming the policy
    (``at_most_one``/``exactly_one``/``at_least_one``) whose value is
    the member array.  A member is ``{ extra = "NAME" }`` or
    ``{ group = "NAME" }``.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        msg = f"conflicts must be an array of conflict sets, got {type(value).__name__}"
        raise ConfigError(msg)
    return tuple(_parse_conflict_set(item, i) for i, item in enumerate(value))


def _parse_conflict_set(item: object, index: int) -> ConflictSet:
    where = f"conflicts[{index}]"
    if isinstance(item, list):
        return ConflictSet(
            members=_parse_conflict_members(item, where),
            policy=ConflictPolicy.AT_MOST_ONE,
        )
    if isinstance(item, dict):
        keys = set(item)
        unknown = sorted(keys - _CONFLICT_POLICY_KEYS.keys())
        if unknown:
            msg = (
                f"{where}: unknown conflict-set key(s) {unknown!r}; expected a"
                f" policy table with one of {sorted(_CONFLICT_POLICY_KEYS)!r}"
                " or a bare array of members"
            )
            raise ConfigError(msg)
        if len(keys) != 1:
            msg = (
                f"{where}: a policy conflict set must name exactly one policy"
                f" of {sorted(_CONFLICT_POLICY_KEYS)!r}, got {sorted(keys)!r}"
            )
            raise ConfigError(msg)
        policy_key = next(iter(keys))
        return ConflictSet(
            members=_parse_conflict_members(item[policy_key], f"{where}.{policy_key}"),
            policy=_CONFLICT_POLICY_KEYS[policy_key],
        )
    msg = (
        f"{where} must be an array of members or a policy table, got"
        f" {type(item).__name__}"
    )
    raise ConfigError(msg)


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
    return ConflictMember(kind=kind, name=canonicalize_name(name))


_MINOR_RELEASE_PARTS = 2


def _matrix_python_patch_clause(spec_set: SpecifierSet) -> str | None:
    """Return the first clause pinning a patch (micro) version, else None.

    The matrix python axis is minor-granular, so ``>=3.11.5`` would
    silently exclude 3.11 entirely. A trailing ``.*`` wildcard targets a
    minor and is allowed.
    """
    for clause in spec_set:
        try:
            release = Version(clause.version.removesuffix(".*")).release
        except InvalidVersion:
            continue
        if len(release) > _MINOR_RELEASE_PARTS:
            return str(clause)
    return None


def _parse_matrix(value: object) -> MatrixConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        msg = f"[tool.nab.matrix] must be a table, got {type(value).__name__}"
        raise ConfigError(msg)
    allowed = {
        "python",
        "platforms",
        "python-order",
        "python-patches",
        "implementations",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        msg = (
            f"unknown [tool.nab.matrix] keys: {unknown!r}; expected {sorted(allowed)!r}"
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
    try:
        spec_set = SpecifierSet(python)
    except InvalidSpecifier as exc:
        msg = f"matrix.python must be a PEP 440 specifier, got {python!r}"
        raise ConfigError(msg) from exc
    patched = _matrix_python_patch_clause(spec_set)
    if patched is not None:
        msg = (
            f"matrix.python takes minor (language) versions only, got {patched!r}."
            " The python axis is minor-granular; use 3.X, not a patch like 3.X.Y."
        )
        raise ConfigError(msg)
    platforms = _parse_string_list("matrix.platforms", platforms_raw)
    if not platforms:
        msg = "matrix.platforms must list at least one platform id"
        raise ConfigError(msg)
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
    matrix = Matrix(
        python=config.python,
        platforms=config.platforms,
        python_order=config.python_order,
        python_patches=dict(config.python_patches)
        if config.python_patches is not None
        else None,
        implementations=config.implementations,
    )
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
    unknown = sorted(set(impls) - set(_KNOWN_IMPLEMENTATIONS))
    if unknown:
        msg = (
            f"unknown matrix.implementations: {unknown!r}; "
            f"expected {list(_KNOWN_IMPLEMENTATIONS)!r}"
        )
        raise ConfigError(msg)
    return impls
