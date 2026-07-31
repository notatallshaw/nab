"""Registry of layered nab options and the helpers that read them.

Every layered option is one :class:`OptionSpec` row in :data:`OPTIONS`.
A row names the option, its scope (``PROJECT`` or ``USER``), its value
type, its default, the env var (or ``None``), the CLI flag spelling,
and (via the scope) the set of source kinds it is allowed to come
from.

Everything else is derived by iterating :data:`OPTIONS`:

* :func:`discover_layers` reads each ``nab.toml`` / ``[tool.nab]``
  table against the registry, raising on any key that names an option
  not allowed in that source kind (the category gate).
* :func:`read_env_layer` reads ``NAB_*`` for the rows that declare an
  env var, and warns on any ``NAB_*`` name it does not recognize.
* :func:`resolve_config` merges the discovered layers low-to-high and
  attaches ``(scope, origin)`` provenance to each effective value.
* :func:`render_list` / :func:`render_get` / :func:`render_explain`
  back ``nab config``.

Adding an option is one new row in :data:`OPTIONS` plus, for the
CLI, one tyro flag.  A conformance test asserts the tyro surface matches
the registry so the one place the CLI surface is not registry-derived
(tyro reads flags from a function signature) cannot silently drift.
"""

from __future__ import annotations

import enum
import logging
import types
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli

from nab_index.multi_index import IndexConfig
from nab_index.serialization import SimpleSerialization

from ._toml import tool_nab_section
from .fetch import DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL
from .provider import (
    ArchiveSource,
    BuildPolicy,
    DistPolicy,
    LocalSource,
    ResolutionStrategy,
    ResolveMode,
    VcsConfig,
    VcsSource,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

_logger = logging.getLogger(__name__)

__all__ = [
    "OPTIONS",
    "ConfigError",
    "EffectiveValue",
    "Layer",
    "OptionSpec",
    "Origin",
    "RejectedLayer",
    "Scope",
    "SourceConfigError",
    "SourceKind",
    "SourceRoots",
    "build_cli_layer",
    "build_cli_overrides",
    "discover_layers",
    "inspector_anchor",
    "orphan_rejections",
    "project_cli_override_notice",
    "project_cli_override_records",
    "pyproject_registry_keys",
    "read_env_layer",
    "reject_user_keys_in_pyproject",
    "render_explain",
    "render_get",
    "render_list",
    "resolve_anchor",
    "resolve_config",
]


# The anchor that ``P<n>D`` relative durations resolve against during a
# real resolve.  ``nab config`` (the inspector) leaves it unset, so the
# display anchors a relative duration at the current time; the resolve
# path (``config.read_pyproject_config``) sets it to the lockfile-captured
# anchor for the duration of the merge so re-locks reproduce the same
# cutoff.  Carried on a ContextVar rather than threaded through every
# fixed-arity parse hook.
_RESOLVE_ANCHOR: ContextVar[datetime | None] = ContextVar(
    "_RESOLVE_ANCHOR", default=None
)

# The directory of the TOML file currently being parsed.  Relative
# ``local-sources`` paths resolve against the declaring file's directory;
# carried structurally on a ContextVar (set by :func:`_load_toml_layer`)
# rather than re-derived by splitting a human-facing ``where`` label.
_DECLARING_DIR: ContextVar[Path | None] = ContextVar("_DECLARING_DIR", default=None)

# A single ``now`` for one inspector pass, so override-body ``P<n>D``
# durations in the two project files anchor against the same instant.
# Unlike the top-level ``uploaded-prior-to`` row (which keeps a relative
# duration as its raw string when the resolve anchor is unset), an override
# body is resolved eagerly to an absolute datetime, so two identical
# durations across pyproject and the project nab.toml would otherwise anchor
# microseconds apart and read as conflicting values.  Bound by
# :func:`inspector_anchor` around the inspector's discover+merge.  The
# resolve path leaves it unset and uses :data:`_RESOLVE_ANCHOR` instead.
_INSPECTOR_ANCHOR: ContextVar[datetime | None] = ContextVar(
    "_INSPECTOR_ANCHOR", default=None
)


def _current_anchor() -> datetime:
    """Return the active ``P<n>D`` anchor: the resolve anchor or ``now``.

    The resolve path sets :data:`_RESOLVE_ANCHOR` so relative durations
    resolve against the lockfile anchor; the inspector leaves it unset and
    anchors at the current time (display only).
    """
    anchor = _RESOLVE_ANCHOR.get()
    return anchor if anchor is not None else datetime.now(timezone.utc)


@contextmanager
def resolve_anchor(anchor: datetime | None) -> Iterator[None]:
    """Bind the ``P<n>D`` resolve anchor for the duration of a merge.

    The resolve path wraps its :func:`resolve_config` call in this so the
    effective override / ``uploaded-prior-to`` values it consumes resolve
    relative durations against the lockfile anchor.  A ``None`` anchor is a
    no-op (the inspector's current-time behaviour).
    """
    token = _RESOLVE_ANCHOR.set(anchor)
    try:
        yield
    finally:
        _RESOLVE_ANCHOR.reset(token)


@contextmanager
def inspector_anchor() -> Iterator[None]:
    """Pin one ``now`` for an inspector discover+merge pass.

    The inspector (``nab config``) leaves :data:`_RESOLVE_ANCHOR` unset, so
    each override-body ``P<n>D`` duration would otherwise anchor against a
    fresh :func:`datetime.now` per layer.  Binding a single instant here
    makes an identical relative duration in pyproject ``[tool.nab]`` and the
    project ``nab.toml`` resolve to the same datetime, so the cross-file
    equality check does not see a spurious conflict.  A no-op on the resolve
    path (the resolve anchor takes precedence).
    """
    token = _INSPECTOR_ANCHOR.set(datetime.now(timezone.utc))
    try:
        yield
    finally:
        _INSPECTOR_ANCHOR.reset(token)


class ConfigError(ValueError):
    """Raised when ``[tool.nab]`` configuration is invalid.

    The base for every config-parse error.  Lives here (the lowest config
    layer) so :class:`SourceConfigError` can subclass it without an import
    cycle; :mod:`nab_python.config` re-exports it as its public name and
    hangs its own subclasses (``ConflictSelectionError``,
    ``OverrideConflictError``) off it.
    """


class SourceConfigError(ConfigError):
    """A layered source set a value it is not allowed to set.

    Raised by the category gate: e.g. a PROJECT option appearing in a
    user ``nab.toml`` or env var, or a USER option appearing in
    ``pyproject.toml`` ``[tool.nab]``.  A subclass of :class:`ConfigError`
    so a caller catching the broad config error also catches a layered
    gate or cross-file conflict failure, while ``except SourceConfigError``
    still narrows to the layered cases.
    """


class Scope(enum.Enum):
    """Whether an option configures the project or the user/environment."""

    PROJECT = "project"
    USER = "user"


class SourceKind(enum.Enum):
    """One discoverable configuration source, low precedence to high.

    The two project-level TOML sources (``PYPROJECT`` and
    ``PROJECT_TOML``) share a precedence rank; the rest are totally
    ordered by :data:`_PRECEDENCE`.
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
_PRECEDENCE: dict[SourceKind, int] = {
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


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """One row of the registry: the full definition of a layered option.

    ``key`` is the TOML/`nab config` key.  ``scope`` gates which sources
    may set it.  ``parse`` turns a raw TOML/string value into the typed
    value (and raises :class:`SourceConfigError` on a bad value).
    ``env_var`` is the ``NAB_*`` name or ``None`` (not env-settable).
    ``cli_flag`` is the tyro flag spelling the conformance test checks,
    or ``None`` for a file-only row (a structured PROJECT table with no
    bare CLI flag, e.g. ``vcs``/``workspace``/``environment``);
    ``cli_param`` is the tyro function-parameter name backing the flag,
    also ``None`` for a file-only row.  ``type_label`` is shown by
    ``nab config``.
    """

    key: str
    scope: Scope
    type_label: str
    default: Any
    env_var: str | None
    cli_flag: str | None
    cli_param: str | None
    parse: Callable[[Any, str], Any]
    render: Callable[[Any], str]
    # How bindings from different sources combine.  The default is scalar
    # last-wins: the highest-precedence source's value is the effective one,
    # and the same key set to different values in the two project files is a
    # conflict error.
    #
    # ``is_array`` concatenates every binding's items low-to-high into one
    # value, so the two project files contribute additively rather than
    # conflicting; the concatenated whole is then re-validated (see
    # ``merge_check``).
    is_array: bool = False
    # ``is_mapping`` is a name-keyed table (``index``, ``marker-environment``)
    # whose sub-keys merge across sources, folded low-to-high, so the two
    # project files contribute disjoint sub-keys additively and only the same
    # sub-key set differently is a conflict.  Mutually exclusive with
    # ``is_array``.
    is_mapping: bool = False
    # For an ``is_array`` row whose items are already-built objects (not
    # plain strings), the check to run over the concatenated whole; it
    # returns the validated tuple.  A plain-string array leaves this ``None``
    # and is re-validated by re-running ``parse`` over the concatenation.
    merge_check: Callable[[Sequence[Any]], tuple[Any, ...]] | None = None

    def allowed_in_toml(self, kind: SourceKind) -> bool:
        """Whether a TOML source of ``kind`` may set this option.

        The category gate.  ``kind`` is always one of the four TOML
        source kinds; env (``NAB_*``) gating is the ``env_var`` field and
        is handled in :func:`read_env_layer`, CLI is always allowed.
        """
        return kind in _ALLOWED_TOML_SOURCES[self.scope]


def _parse_bool(value: Any, where: str) -> bool:
    """Parse a TOML/env boolean.

    Accepts a real bool (TOML) or one of ``1/0/true/false`` (env,
    case-insensitive).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true"}:
            return True
        if lowered in {"0", "false"}:
            return False
        msg = f"{where} must be one of 1/0/true/false, got {value!r}"
        raise SourceConfigError(msg)
    msg = f"{where} must be a boolean, got {type(value).__name__}"
    raise SourceConfigError(msg)


def _parse_path(value: Any, where: str) -> Path:
    # A CLI layer already supplies a Path; TOML/env supply a string.
    if isinstance(value, Path):
        return value
    if not isinstance(value, str):
        msg = f"{where} must be a string path, got {type(value).__name__}"
        raise SourceConfigError(msg)
    if not value.strip():
        # An empty NAB_CACHE_DIR would resolve Path("") to the cwd; reject
        # it instead.
        msg = f"{where} must be a non-empty path"
        raise SourceConfigError(msg)
    return Path(value)


def _parse_resolution(value: Any, where: str) -> ResolutionStrategy:
    # TOML/env supply a string; the CLI layer also passes the string spelling.
    if not isinstance(value, str):
        msg = f"{where} must be a string, got {type(value).__name__}"
        raise SourceConfigError(msg)
    try:
        return ResolutionStrategy(value)
    except ValueError as exc:
        valid = sorted(s.value for s in ResolutionStrategy)
        msg = f"{where} must be one of {valid!r}, got {value!r}"
        raise SourceConfigError(msg) from exc


_HTTP_BACKENDS = ("httpx", "urllib3")


def _parse_http_backend(value: Any, where: str) -> str:
    if not isinstance(value, str):
        msg = f"{where} must be a string, got {type(value).__name__}"
        raise SourceConfigError(msg)
    lowered = value.strip()
    if lowered not in _HTTP_BACKENDS:
        msg = f"{where} must be one of {list(_HTTP_BACKENDS)!r}, got {value!r}"
        raise SourceConfigError(msg)
    return lowered


def _parse_max_concurrency(value: Any, where: str) -> int:
    # TOML supplies an int; env/CLI supply a string.  bool is an int
    # subclass, so reject it explicitly rather than read True as 1.
    if isinstance(value, bool):
        msg = f"{where} must be an integer, got {type(value).__name__}"
        raise SourceConfigError(msg)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value)
        except ValueError as exc:
            msg = f"{where} must be an integer, got {value!r}"
            raise SourceConfigError(msg) from exc
    else:
        msg = f"{where} must be an integer, got {type(value).__name__}"
        raise SourceConfigError(msg)
    if result < 1:
        msg = f"{where} must be at least 1, got {result}"
        raise SourceConfigError(msg)
    return result


def _delegate(call: Callable[[], Any]) -> Any:
    """Run a ``config.py`` parse helper, re-typing its error for the registry.

    The registry rows reuse the single-environment parsers in
    :mod:`nab_python.config` verbatim, so the value and every validation
    message are identical to the pyproject parse path.  Those helpers raise
    :class:`config.ConfigError`; the registry contract is
    :class:`SourceConfigError`, and the CLI and loader catch only the latter.
    Re-raise with the same message so behaviour is preserved and the error is
    caught by the ladder.
    """
    try:
        return call()
    except ConfigError as exc:
        raise SourceConfigError(str(exc)) from exc


def _parse_mode(value: Any, where: str) -> Any:
    # Delegates to config._parse_mode (enum specific|universal).  ``where``
    # is unused: the helper owns the (config-keyed) message wording, kept
    # identical to the pyproject path.
    del where
    from .config import _parse_mode as _impl  # noqa: PLC0415 (config import cycle)

    return _delegate(lambda: _impl(value))


def _parse_requires_python(value: Any, where: str) -> str | None:
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_requires_python as _impl,
    )

    return _delegate(lambda: _impl(value))


def _parse_dist_policy(value: Any, where: str) -> tuple[DistPolicy, bool]:
    # The scalar-or-table dist-policy folds the sdist-trust bool, so the
    # registry value is the (policy, trust) pair the global parser returns.
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_dist_policy_global as _impl,
    )

    return _delegate(lambda: _impl(value))


def _parse_build_policy(value: Any, where: str) -> BuildPolicy:
    # The plain scalar last-wins value only.  The host-build gate that forces
    # never for a declared target is a post-merge transform applied over the
    # merged config, not this row.
    del where
    from .config import _parse_enum as _impl  # noqa: PLC0415 (config import cycle)

    return _delegate(
        lambda: _impl("build-policy", value, BuildPolicy, BuildPolicy.BUILD_LOCAL)
    )


def _parse_uploaded_prior_to(value: Any, where: str) -> Any:
    """Parse ``uploaded-prior-to`` without re-anchoring relative durations.

    A ``P<n>D`` duration anchors to the lock at resolve time
    (config._parse_uploaded_prior_to threads the lockfile anchor); the
    registry merely gates/displays the key and must not silently
    re-anchor it to ``now``.  So a relative duration is carried as its
    raw string (the cross-file conflict check compares the raw strings, so
    identical durations match and different ones conflict), and only an
    absolute datetime is normalised through the shared parser.  The real
    resolve still parses the value with the proper anchor via
    ``read_pyproject_config``.
    """
    del where
    from .config import _DURATION_PATTERN  # noqa: PLC0415 (config import cycle)
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_uploaded_prior_to as _impl,
    )

    if isinstance(value, str) and _DURATION_PATTERN.match(value):
        # On the resolve path the active anchor is set, so resolve the
        # relative duration to its absolute cutoff now; the inspector
        # leaves the anchor unset and carries the raw string for display
        # (its now-anchor would be misleading in a stored value).
        if _RESOLVE_ANCHOR.get() is not None:
            return _delegate(lambda: _impl(value, anchor=_current_anchor()))
        return value
    # Absolute datetime / TOML offset-datetime: anchor is irrelevant, but
    # the helper requires one, so pass a placeholder it never reads.
    return _delegate(lambda: _impl(value, anchor=_current_anchor()))


def _parse_marker_environment(value: Any, where: str) -> Mapping[str, str]:
    # Name-keyed table (PEP 508 marker var -> str).  Delegates to the
    # single-environment parser so validation (string->string, known
    # marker vars) is identical to the pyproject path.
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_marker_environment as _impl,
    )

    return _delegate(lambda: _impl(value))


def _parse_environment(value: Any, where: str) -> Mapping[str, Any]:
    # Name-keyed table (python/platform/implementation), one cell of a
    # matrix; platform is an id or a table of tag knobs.  A mapping row, so
    # the axes merge sub-key by sub-key across the ladder and a ``--python``
    # override moves only the python axis.
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_environment as _impl,
    )

    return _delegate(lambda: _impl(value))


def _parse_vcs(value: Any, where: str) -> Any:
    # Nested table (policy, allowed-schemes, allowed-repos, require-pin).
    # Delegates to config._parse_vcs, returning the frozen VcsConfig.
    del where
    from .config import _parse_vcs as _impl  # noqa: PLC0415 (config import cycle)

    return _delegate(lambda: _impl(value))


def _parse_workspace(value: Any, where: str) -> Any:
    # Nested table (members).  Delegates to config._parse_workspace,
    # returning a WorkspaceConfig or None.  The discovery walk-up is a
    # post-merge transform applied by read_pyproject_config, never this
    # row, so the registry only folds the declared table.
    del where
    from .config import _parse_workspace as _impl  # noqa: PLC0415 (config import cycle)

    return _delegate(lambda: _impl(value))


def _parse_constraints(value: Any, where: str) -> tuple[str, ...]:
    # Array of PEP 508 strings.  Delegates to config._parse_constraints so
    # the list-of-strings shape check and the per-item PEP 508 validation
    # (and their messages) are identical to the pyproject parse path.  Runs
    # both per-layer (each file's own list) and again over the concatenated
    # whole (the array merge re-validates the result), which is why the
    # delegate must accept any tuple as well as a list.
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_constraints as _impl,
    )

    return _delegate(lambda: _impl(value))


def _parse_default_groups(value: Any, where: str) -> tuple[str, ...]:
    # Array of group names.  Delegates to config._parse_string_list so the
    # list-of-strings shape check and message match the pyproject path.
    # The default-groups-vs-conflicts cross-check stays in the single
    # environment parser (it needs both merged values), so this row only
    # folds the list itself.
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_string_list as _impl,
    )

    return _delegate(lambda: _impl("default-groups", value))


def _parse_indexes(value: Any, where: str) -> tuple[IndexConfig, ...]:
    # One file's array-of-tables: config._parse_indexes validates
    # shape, keys, and the within-file same-name check into IndexConfig
    # entries.  The across-file same-name check runs over the concatenation
    # via merge_check (_revalidate_index_names).
    del where
    from .config import _parse_indexes as _impl  # noqa: PLC0415 (config import cycle)

    return _delegate(lambda: _impl(value))


def _revalidate_index_names(indexes: Sequence[Any]) -> tuple[IndexConfig, ...]:
    # Re-run the same-name check over the concatenated indexes, re-typing the
    # error so its message matches the single-file path.
    from .config import (  # noqa: PLC0415 (config import cycle)
        _check_index_name_uniqueness as _impl,
    )

    merged = tuple(indexes)
    _delegate(lambda: _impl(merged))
    return merged


def _parse_local_sources(value: Any, where: str) -> tuple[LocalSource, ...]:
    # One file's array-of-tables (name, path, editable, subdirectory).  Paths
    # resolve relative to the declaring file's directory (both legal sources
    # share the project dir).  There is no within-key duplicate check (the
    # cross-source local/vcs/archive name check is a whole-config pass on the
    # resolve path), so the concatenation passes through unchanged
    # (merge_check=tuple).
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_local_sources as _impl,
    )

    base_dir = _declaring_dir()
    return _delegate(lambda: _impl(value, pyproject_dir=base_dir))


def _declaring_dir() -> Path:
    # local-sources is a file-only row (no CLI/env), so its per-layer parse
    # is only ever reached from a TOML layer, which sets _DECLARING_DIR to
    # the file's directory.  Paths in the file resolve relative to it.
    base = _DECLARING_DIR.get()
    if base is None:  # pragma: no cover - per-layer parse always sets it
        msg = "local-sources parsed without a declaring directory"
        raise SourceConfigError(msg)
    return base


def _parse_vcs_sources(value: Any, where: str) -> tuple[VcsSource, ...]:
    # One file's array-of-tables (name, url).  Like local-sources there is no
    # within-key duplicate check, so the concatenation passes through
    # (merge_check=tuple).
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_vcs_sources as _impl,
    )

    return _delegate(lambda: _impl(value))


def _parse_archive_sources(value: Any, where: str) -> tuple[ArchiveSource, ...]:
    # One file's array-of-tables (name, url); same passthrough shape as
    # vcs-sources (merge_check=tuple).
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_archive_sources as _impl,
    )

    return _delegate(lambda: _impl(value))


def _override_anchor() -> datetime:
    # An override body may carry an ``uploaded-prior-to`` whose ``P<n>D`` form
    # resolves against an anchor at parse time.  The resolve path binds the
    # lockfile anchor, so the duration anchors reproducibly.  The inspector
    # leaves it unset and, unlike the top-level uploaded-prior-to row, cannot
    # defer to a raw string (the body is an opaque object), so it anchors at
    # the inspector pass's single ``now`` (bound by inspector_anchor) rather
    # than a fresh now per layer, so an identical duration in both project
    # files compares equal.
    resolve = _RESOLVE_ANCHOR.get()
    if resolve is not None:
        return resolve
    inspector = _INSPECTOR_ANCHOR.get()
    if inspector is not None:
        return inspector
    return datetime.now(timezone.utc)


# ``packages`` (name-keyed sugar) and ``package-rules`` (array-of-tables) are
# two rows that both desugar into one PackageOverride tuple, so each parses
# only its own surface and config validates the body and the within-surface
# same-field overlap.  The across-file overlap runs over the concatenation
# via merge_check (_revalidate_override_overlap).  The cross-surface
# packages-vs-package-rules overlap and the route-names-a-declared-index
# check both need the merged whole, so they run on the resolve path in
# config._config_from_effective, not here.
def _parse_packages(value: Any, where: str) -> tuple[Any, ...]:
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_packages_sugar as _impl,
    )

    return _delegate(lambda: tuple(_impl(value, anchor=_override_anchor())))


def _parse_package_rules(value: Any, where: str) -> tuple[Any, ...]:
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_package_rules as _impl,
    )

    return _delegate(lambda: tuple(_impl(value, anchor=_override_anchor())))


def _revalidate_override_overlap(overrides: Sequence[Any]) -> tuple[Any, ...]:
    # Re-run the per-package same-field overlap check over the concatenation,
    # re-typing the error to the registry's SourceConfigError.
    from .config import (  # noqa: PLC0415 (config import cycle)
        _check_package_override_overlap as _impl,
    )

    merged = tuple(overrides)
    _delegate(lambda: _impl(merged))
    return merged


def _parse_index_overrides(value: Any, where: str) -> Mapping[str, Any]:
    # Name-keyed table (``[tool.nab.index.<name>]``).  The body (policy
    # fields) is validated as on the pyproject path; the
    # key-names-a-declared-index check needs the merged indexes and so runs on
    # the resolve path.  As a mapping row the two project files contribute
    # disjoint index names additively, and only the same index name set
    # differently across them is a conflict.
    del where
    from .config import (  # noqa: PLC0415 (config import cycle)
        _parse_index_overrides as _impl,
    )

    return _delegate(lambda: _impl(value, anchor=_override_anchor()))


def _parse_conflicts(value: Any, where: str) -> tuple[Any, ...]:
    # One file's array-of-tables (member list/table + policy): config validates
    # shape, members, and policy and runs the within-file member-uniqueness
    # check.  The across-file member-uniqueness check runs over the
    # concatenation via merge_check (_revalidate_conflict_members).  The
    # default-groups-vs-conflicts check needs both merged values, so it runs
    # on the resolve path.
    del where
    from .config import _parse_conflicts as _impl  # noqa: PLC0415 (config import cycle)

    return _delegate(lambda: _impl(value))


def _revalidate_conflict_members(conflicts: Sequence[Any]) -> tuple[Any, ...]:
    # Re-run the member-uniqueness check over the concatenation, re-typing the
    # error so its message matches the single-file path.
    from .config import (  # noqa: PLC0415 (config import cycle)
        _check_conflict_member_uniqueness as _impl,
    )

    merged = tuple(conflicts)
    _delegate(lambda: _impl(merged))
    return merged


def _parse_matrix(value: Any, where: str) -> Any:
    # Nested table (python, platforms, python-order, python-patches,
    # implementations).  Scalar last-wins, compared by value (a frozen
    # MatrixConfig or None).  Delegates to config._parse_matrix so axis
    # validation and eager expansion are identical to the pyproject path.
    # The mode/matrix mutual-requirement check needs both merged values, so
    # it runs over the merged config rather than in this row.
    del where
    from .config import _parse_matrix as _impl  # noqa: PLC0415 (config import cycle)

    return _delegate(lambda: _impl(value))


def _render_conflicts(value: Sequence[Any]) -> str:
    if not value:
        return "<none>"
    return "; ".join(str(cs) for cs in value)


def _render_matrix(value: Any) -> str:
    if value is None:
        return "<none>"
    platforms = [p.label for p in value.platforms]
    return f"python={value.python}, platforms={platforms}"


def _render_package_overrides(value: Sequence[Any]) -> str:
    if not value:
        return "<none>"
    return ", ".join(str(o.requirement) for o in value)


def _render_index_overrides(value: Mapping[str, Any]) -> str:
    if not value:
        return "<none>"
    return ", ".join(sorted(value))


def _render_index_list(value: Sequence[IndexConfig]) -> str:
    if not value:
        return "<none>"
    return ", ".join(f"{i.name}={i.url}{_render_index_pin(i)}" for i in value)


def _render_index_pin(index: IndexConfig) -> str:
    if index.serialization is SimpleSerialization.NEGOTIATE:
        return ""
    return f" serialization={index.serialization.value}"


def _render_local_sources(value: Sequence[LocalSource]) -> str:
    if not value:
        return "<none>"
    return ", ".join(f"{s.name}@{s.path}" for s in value)


def _render_vcs_sources(value: Sequence[VcsSource]) -> str:
    if not value:
        return "<none>"
    return ", ".join(f"{s.name}@{s.url}" for s in value)


def _render_archive_sources(value: Sequence[ArchiveSource]) -> str:
    if not value:
        return "<none>"
    return ", ".join(f"{s.name}@{s.url}" for s in value)


def _render_string_tuple(value: Sequence[str]) -> str:
    return "<none>" if not value else ", ".join(value)


def _render_marker_environment(value: Mapping[str, str]) -> str:
    if not value:
        return "<none>"
    return ", ".join(f"{k}={v}" for k, v in sorted(value.items()))


def _render_environment(value: Mapping[str, Any]) -> str:
    if not value:
        return "<host>"
    return ", ".join(
        f"{k}={_render_environment_axis(v)}" for k, v in sorted(value.items())
    )


def _render_environment_axis(value: Any) -> str:
    """Render one axis, the platform table as ``id[knob=value, ...]``."""
    if not isinstance(value, dict):
        return str(value)
    knobs = ", ".join(f"{k}={v}" for k, v in sorted(value.items()) if k != "id")
    platform_id = value["id"]
    return f"{platform_id}[{knobs}]" if knobs else str(platform_id)


def _render_vcs(value: Any) -> str:
    parts = [f"policy={value.policy.value}"]
    if value.allowed_schemes:
        parts.append(f"allowed-schemes={sorted(value.allowed_schemes)}")
    if value.allowed_repos:
        parts.append(f"allowed-repos={list(value.allowed_repos)}")
    if not value.require_pin:
        parts.append("require-pin=false")
    return ", ".join(parts)


def _render_workspace(value: Any) -> str:
    if value is None:
        return "<none>"
    return f"members={list(value.members)}"


def _render_uploaded_prior_to(value: Any) -> str:
    return "<none>" if value is None else str(value)


def _render_dist_policy(value: Any) -> str:
    # value is the (policy, trust_unverified_deps) pair the global parser
    # returns; show the trust flag only when it diverges from the default.
    policy, trust = value
    return f"{policy.value} (trust-unverified-deps)" if trust else policy.value


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
        parse=_parse_resolution,
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
        parse=_parse_mode,
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
        parse=_parse_constraints,
        render=_render_string_tuple,
        is_array=True,
    ),
    OptionSpec(
        key="default-groups",
        scope=Scope.PROJECT,
        type_label="list(group)",
        default=(),
        env_var=None,
        cli_flag="--project-default-group",
        cli_param="project_default_group",
        parse=_parse_default_groups,
        render=_render_string_tuple,
        is_array=True,
    ),
    OptionSpec(
        key="requires-python",
        scope=Scope.PROJECT,
        type_label="specifier",
        default=None,
        env_var=None,
        cli_flag="--project-requires-python",
        cli_param="project_requires_python",
        parse=_parse_requires_python,
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
        parse=_parse_uploaded_prior_to,
        render=_render_uploaded_prior_to,
    ),
    OptionSpec(
        key="dist-policy",
        scope=Scope.PROJECT,
        type_label=f"enum({'|'.join(p.value for p in DistPolicy)})",
        default=(DistPolicy.WHEEL_OR_SDIST, False),
        env_var=None,
        cli_flag="--project-dist-policy",
        cli_param="project_dist_policy",
        parse=_parse_dist_policy,
        render=_render_dist_policy,
    ),
    OptionSpec(
        key="build-policy",
        scope=Scope.PROJECT,
        type_label=f"enum({'|'.join(p.value for p in BuildPolicy)})",
        default=BuildPolicy.BUILD_LOCAL,
        env_var=None,
        cli_flag="--project-build-policy",
        cli_param="project_build_policy",
        parse=_parse_build_policy,
        render=lambda v: v.value,
    ),
    OptionSpec(
        key="environment",
        scope=Scope.PROJECT,
        type_label="table(python,platform[,knobs],implementation)",
        default=_EMPTY_MAPPING,
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_environment,
        render=_render_environment,
        is_mapping=True,
    ),
    OptionSpec(
        key="marker-environment",
        scope=Scope.PROJECT,
        type_label="table(marker-var=str) [deprecated]",
        default=_EMPTY_MAPPING,
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_marker_environment,
        render=_render_marker_environment,
        is_mapping=True,
    ),
    OptionSpec(
        key="vcs",
        scope=Scope.PROJECT,
        type_label="table(vcs-policy)",
        default=VcsConfig(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_vcs,
        render=_render_vcs,
    ),
    OptionSpec(
        key="workspace",
        scope=Scope.PROJECT,
        type_label="table(members)",
        default=None,
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_workspace,
        render=_render_workspace,
    ),
    OptionSpec(
        key="indexes",
        scope=Scope.PROJECT,
        type_label="array-of-tables(name,url,serialization)",
        default=(IndexConfig(DEFAULT_INDEX_NAME, DEFAULT_INDEX_URL),),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_indexes,
        render=_render_index_list,
        is_array=True,
        merge_check=_revalidate_index_names,
    ),
    OptionSpec(
        key="local-sources",
        scope=Scope.PROJECT,
        type_label="array-of-tables(name,path)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_local_sources,
        render=_render_local_sources,
        is_array=True,
        merge_check=tuple,
    ),
    OptionSpec(
        key="vcs-sources",
        scope=Scope.PROJECT,
        type_label="array-of-tables(name,url)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_vcs_sources,
        render=_render_vcs_sources,
        is_array=True,
        merge_check=tuple,
    ),
    OptionSpec(
        key="archive-sources",
        scope=Scope.PROJECT,
        type_label="array-of-tables(name,url)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_archive_sources,
        render=_render_archive_sources,
        is_array=True,
        merge_check=tuple,
    ),
    OptionSpec(
        key="packages",
        scope=Scope.PROJECT,
        type_label="table(package-override)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_packages,
        render=_render_package_overrides,
        is_array=True,
        merge_check=_revalidate_override_overlap,
    ),
    OptionSpec(
        key="package-rules",
        scope=Scope.PROJECT,
        type_label="array-of-tables(match,policy)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_package_rules,
        render=_render_package_overrides,
        is_array=True,
        merge_check=_revalidate_override_overlap,
    ),
    OptionSpec(
        key="index",
        scope=Scope.PROJECT,
        type_label="table(index-override)",
        default=_EMPTY_MAPPING,
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_index_overrides,
        render=_render_index_overrides,
        is_mapping=True,
    ),
    OptionSpec(
        key="conflicts",
        scope=Scope.PROJECT,
        type_label="array-of-tables(members,policy)",
        default=(),
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_conflicts,
        render=_render_conflicts,
        is_array=True,
        merge_check=_revalidate_conflict_members,
    ),
    OptionSpec(
        key="matrix",
        scope=Scope.PROJECT,
        type_label="table(python,platforms)",
        default=None,
        env_var=None,
        cli_flag=None,
        cli_param=None,
        parse=_parse_matrix,
        render=_render_matrix,
    ),
    OptionSpec(
        key="offline",
        scope=Scope.USER,
        type_label="bool",
        default=False,
        env_var="NAB_OFFLINE",
        cli_flag="--offline",
        cli_param="offline",
        parse=_parse_bool,
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
        parse=_parse_path,
        render=lambda v: "<computed>" if v is None else str(v),
    ),
    OptionSpec(
        key="http-backend",
        scope=Scope.USER,
        type_label=f"enum({'|'.join(_HTTP_BACKENDS)})",
        default="urllib3",
        env_var="NAB_HTTP_BACKEND",
        cli_flag="--http-backend",
        cli_param="http_backend",
        parse=_parse_http_backend,
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
        parse=_parse_max_concurrency,
        render=str,
    ),
)

_BY_KEY: dict[str, OptionSpec] = {spec.key: spec for spec in OPTIONS}


def pyproject_registry_keys() -> frozenset[str]:
    """Registry keys a pyproject ``[tool.nab]`` table may legitimately carry.

    Only PROJECT-scope registry options are allowed in pyproject; the
    single-environment parser (:mod:`nab_python.config`) folds this set
    into its own known-keys so a registry key is not double-reported as
    an unknown ``[tool.nab]`` key.
    """
    return frozenset(
        spec.key for spec in OPTIONS if spec.allowed_in_toml(SourceKind.PYPROJECT)
    )


def reject_user_keys_in_pyproject(raw: Mapping[str, Any]) -> None:
    """Raise the category error for any USER registry key in ``[tool.nab]``.

    The parser fold: a USER-scope option (e.g. ``offline``,
    ``cache-dir``) set in pyproject ``[tool.nab]`` must surface the
    registry category error (``pyproject [tool.nab]`` is project-scope
    only) rather than the generic unknown-key error the pyproject
    parser would otherwise raise.  PROJECT keys and keys the registry
    does not own are left for the pyproject parser to handle.

    The message carries no ``[tool.nab]:`` prefix: the only caller is the
    pyproject parser, whose ``error: in [tool.nab]:`` wrapper already
    supplies it, so prefixing here would double it.
    """
    for key in raw:
        spec = _BY_KEY.get(key)
        if spec is None:
            continue
        if not spec.allowed_in_toml(SourceKind.PYPROJECT):
            reason = _gate_reason(spec, SourceKind.PYPROJECT)
            msg = f"{key}: {reason}"
            raise SourceConfigError(msg)


@dataclass(frozen=True, slots=True)
class Origin:
    """Where a value came from: a source kind plus a display label."""

    kind: SourceKind
    label: str

    @property
    def scope(self) -> str:
        """The provenance scope name shown by ``nab config``.

        Mirrors the source kind's value for every kind except
        ``PYPROJECT``, which reports "project" (it sits at the project
        precedence level alongside the project-dir nab.toml).
        """
        return _scope_label(self.kind)

    def outranks(self, other: Origin) -> bool:
        """Whether this origin sits at a strictly higher precedence level.

        A tie is not an outranking: ``PYPROJECT`` and ``PROJECT_TOML``
        share a rank, so neither overrides the other here.
        """
        return _PRECEDENCE[self.kind] > _PRECEDENCE[other.kind]


def _scope_label(kind: SourceKind) -> str:
    """Return the scope name ``nab config`` reports for a source kind.

    Distinct from Scope (PROJECT/USER, the gate axis): provenance reports
    the source, so a project nab.toml reports "project", env reports
    "env", etc.  Every kind reports its own value except PYPROJECT, which
    shares the project precedence level and so reports "project".
    """
    return "project" if kind is SourceKind.PYPROJECT else kind.value


@dataclass(frozen=True, slots=True)
class Layer:
    """A set of (key -> value) bindings discovered from one source."""

    origin: Origin
    values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RejectedLayer:
    """A source that tried to set a key it is not allowed to set.

    Captured (not raised) by :func:`discover_layers` only when the caller
    asks to collect rejections for ``nab config explain
    --include-rejected``.  The normal load path raises a
    :class:`SourceConfigError` instead.
    """

    origin: Origin
    key: str
    reason: str


@dataclass(frozen=True, slots=True)
class EffectiveValue:
    """One option's winning value plus its full shadowed stack."""

    spec: OptionSpec
    value: Any
    origin: Origin
    # Every binding for this key in precedence order (low -> high),
    # the last of which is the winner.
    stack: tuple[tuple[Origin, Any], ...]
    rejected: tuple[RejectedLayer, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceRoots:
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

    system_toml: Path | None = None
    user_toml: Path | None = None
    project_dir: Path | None = None
    pyproject: Path | None = None


def _load_toml_layer(
    path: Path,
    kind: SourceKind,
    *,
    rejections: list[RejectedLayer] | None = None,
) -> Layer:
    """Read one TOML source into a :class:`Layer`, gating by category.

    ``kind`` selects how the file is read: a ``PYPROJECT`` source reads
    ``[tool.nab]``; the standalone ``nab.toml`` sources read top-level
    keys.  Every registry key is parsed by its row.  A key that names an
    option not allowed in ``kind`` (the category gate) raises
    :class:`SourceConfigError`, unless ``rejections`` is supplied, in
    which case it is appended there and skipped (for explain).
    """
    raw = _read_raw_table(path, kind)
    origin = Origin(kind, str(path))
    values: dict[str, Any] = {}
    # Carry the declaring file's directory structurally so relative
    # local-source paths resolve against it (see _declaring_dir).
    token = _DECLARING_DIR.set(path.parent)
    try:
        for key, value in raw.items():
            spec = _BY_KEY.get(key)
            if spec is None:
                # An unknown key (a typo) crashes naming the file rather than
                # being dropped, the same way an unknown NAB_* var does.  On
                # the resolve path config.read_pyproject_config rejects an
                # unknown pyproject [tool.nab] key before this loader runs;
                # the inspector reaches here, so it reports the typo too
                # instead of silently ignoring it.
                valid = sorted(_BY_KEY)
                msg = (
                    f"{path}: {key!r} is not a valid nab setting; the known"
                    f" keys are {valid!r}."
                )
                if rejections is not None:
                    rejections.append(RejectedLayer(origin, key, msg))
                    continue
                raise SourceConfigError(msg)
            where = f"{path}: {key}"
            if not spec.allowed_in_toml(kind):
                reason = _gate_reason(spec, kind)
                if rejections is not None:
                    rejections.append(RejectedLayer(origin, key, reason))
                    continue
                msg = f"{where}: {reason}"
                raise SourceConfigError(msg)
            values[key] = spec.parse(value, where)
    finally:
        _DECLARING_DIR.reset(token)
    return Layer(origin, values)


def _gate_reason(spec: OptionSpec, kind: SourceKind) -> str:
    if kind is SourceKind.PYPROJECT:
        where = "pyproject [tool.nab] (project-scope only)"
    else:
        where = f"a {_scope_label(kind)} nab.toml"
    return (
        f"{spec.key!r} is a {spec.scope.value}-scope option and cannot be set"
        f" in {where}"
    )


def _read_raw_table(path: Path, kind: SourceKind) -> Mapping[str, Any]:
    # TOML is UTF-8, so a file that will not decode is invalid TOML.
    try:
        with path.open("rb") as f:
            data = tomli.load(f)
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise SourceConfigError(msg) from exc
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise SourceConfigError(msg) from exc
    if kind is SourceKind.PYPROJECT:
        section = tool_nab_section(data)
        # A non-table [tool.nab] is malformed, not an empty config.
        if not isinstance(section, dict):
            msg = f"{path}: [tool.nab] must be a table, got {type(section).__name__}"
            raise SourceConfigError(msg)
        return section
    return data


def read_env_layer(
    environ: Mapping[str, str],
    *,
    reserved_env: Iterable[str] = (),
    rejections: list[RejectedLayer] | None = None,
) -> Layer:
    """Read ``NAB_*`` for every registry row that declares an env var.

    PROJECT options never declare an env var, so the env layer carries
    USER options only.  A ``NAB_<KEY>`` naming a PROJECT option (e.g.
    ``NAB_RESOLUTION``) draws a warning from :func:`_warn_renamed_env`;
    any other unknown ``NAB_*`` name (e.g. a typo) one from
    :func:`_warn_unknown_env`.  Neither is applied, and neither is fatal.
    ``reserved_env`` names the ``NAB_*`` vars other layers own (nab's
    output layer consumes ``NAB_VERBOSITY`` and ``NAB_NO_PROGRESS``), so
    the guard skips them silently.  When ``rejections`` is supplied
    (``nab config explain --include-rejected``) those env casualties are
    recorded there instead of warned, mirroring the TOML loader.
    """
    _warn_renamed_env(environ, rejections=rejections)
    _warn_unknown_env(environ, reserved_env=reserved_env, rejections=rejections)
    values: dict[str, Any] = {}
    for spec in OPTIONS:
        if spec.env_var is None:
            continue
        if spec.env_var not in environ:
            continue
        where = f"env:{spec.env_var}"
        values[spec.key] = spec.parse(environ[spec.env_var], where)
    return Layer(Origin(SourceKind.ENV, "env"), values)


def _warn_unknown_env(
    environ: Mapping[str, str],
    *,
    reserved_env: Iterable[str] = (),
    rejections: list[RejectedLayer] | None = None,
) -> None:
    """Warn about any ``NAB_*`` var that is neither a known nor renamed name.

    The env half of the category gate: a typo'd or made-up ``NAB_<KEY>``
    (e.g. ``NAB_OFLINE``) is ignored with a warning naming the variable,
    never applied and never fatal.  Renamed PROJECT names are left for
    :func:`_warn_renamed_env`, which gives the more specific
    not-env-settable message.  ``reserved_env`` names ``NAB_*`` vars owned
    by other layers (the output layer's ``NAB_VERBOSITY`` and
    ``NAB_NO_PROGRESS``); those are skipped silently.  When ``rejections``
    is supplied the var is recorded there instead of warned.
    """
    known = {spec.env_var for spec in OPTIONS if spec.env_var is not None}
    renamed = set(_renamed_env_names())
    reserved = set(reserved_env)
    for name in environ:
        if (
            not name.startswith("NAB_")
            or name in known
            or name in renamed
            or name in reserved
        ):
            continue
        valid = sorted(known)
        msg = (
            f"{name} is not a recognized nab setting and was ignored; the"
            f" known NAB_* variables are {valid!r}."
        )
        if rejections is not None:
            rejections.append(RejectedLayer(Origin(SourceKind.ENV, name), name, msg))
            continue
        _logger.warning("%s", msg)


def _renamed_env_names() -> dict[str, OptionSpec]:
    """Map every PROJECT row's would-be ``NAB_<KEY>`` name to its spec."""
    names: dict[str, OptionSpec] = {}
    for spec in OPTIONS:
        if spec.scope is Scope.PROJECT:
            env_name = "NAB_" + spec.key.upper().replace("-", "_")
            names[env_name] = spec
    return names


def _warn_renamed_env(
    environ: Mapping[str, str],
    *,
    rejections: list[RejectedLayer] | None = None,
) -> None:
    """Warn about a ``NAB_<KEY>`` naming a PROJECT (non-env-settable) option.

    The variable is ignored, never applied and never fatal.  When
    ``rejections`` is supplied the var is recorded there under the spec's
    key (so ``explain <key> --include-rejected`` lists it) instead of
    warned.
    """
    for env_name, spec in _renamed_env_names().items():
        if env_name in environ:
            # A file-only row (cli_flag is None) carries no per-run flag,
            # so the message names only the two project files for it.
            override = (
                ""
                if spec.cli_flag is None
                else f", or override per-run with {spec.cli_flag}"
            )
            msg = (
                f"{env_name} was ignored: {spec.key!r} is a"
                f" {spec.scope.value}-scope option and is not env-settable."
                f"  Set it in pyproject [tool.nab].{spec.key} or a project-dir"
                f" nab.toml{override}."
            )
            if rejections is not None:
                rejections.append(
                    RejectedLayer(Origin(SourceKind.ENV, env_name), spec.key, msg)
                )
                continue
            _logger.warning("%s", msg)


def discover_layers(
    roots: SourceRoots,
    *,
    rejections: list[RejectedLayer] | None = None,
    read_pyproject: bool = True,
) -> list[Layer]:
    """Read every present TOML source into ordered layers (low -> high).

    Reads system, user, pyproject, and project-dir ``nab.toml`` in
    precedence order, skipping any root that is ``None`` or whose file is
    absent.  Hermetic: the caller supplies the roots, so nothing touches
    the real ``~/.config``.  There is no walk-up: the project source is
    the pyproject directory only.

    ``read_pyproject=False`` drops the pyproject layer while keeping the
    project-dir ``nab.toml``, for a caller reading a USER-scope option
    the category gate bars pyproject from setting.
    """
    if roots.project_dir is None or not read_pyproject:
        pyproject_path = None
    elif roots.pyproject is not None:
        pyproject_path = roots.pyproject
    else:
        pyproject_path = roots.project_dir / "pyproject.toml"
    layers: list[Layer] = []
    plan: list[tuple[Path | None, SourceKind]] = [
        (roots.system_toml, SourceKind.SYSTEM_TOML),
        (roots.user_toml, SourceKind.USER_TOML),
        (pyproject_path, SourceKind.PYPROJECT),
        (
            None if roots.project_dir is None else roots.project_dir / "nab.toml",
            SourceKind.PROJECT_TOML,
        ),
    ]
    for path, kind in plan:
        if path is None or not path.exists():
            continue
        if not path.is_file():
            # A genuinely-absent file is skipped above; a path that exists
            # but is not a regular file (e.g. an accidental `mkdir
            # nab.toml`) would be silently ignored by an is_file() filter,
            # so crash naming it rather than dropping the config source.
            msg = f"{path} exists but is not a regular file"
            raise SourceConfigError(msg)
        layers.append(_load_toml_layer(path, kind, rejections=rejections))
    return layers


def resolve_config(
    layers: Sequence[Layer],
    env_layer: Layer,
    cli_layer: Layer,
    *,
    rejected: Sequence[RejectedLayer] = (),
) -> dict[str, EffectiveValue]:
    """Merge all layers into one effective value per registry option.

    ``layers`` are the discovered TOML layers (any order; ranked by
    :data:`_PRECEDENCE`).  ``env_layer`` and ``cli_layer`` are the env
    and CLI bindings.  Returns a ``key -> EffectiveValue`` map covering
    every registry row, each carrying its winner ``(scope, origin)`` and
    the full shadowed stack.  ``rejected`` (category-gate casualties) is
    attached per key for ``explain --include-rejected``.
    """
    all_layers = [*layers, env_layer, cli_layer]
    out: dict[str, EffectiveValue] = {}
    for spec in OPTIONS:
        stack = _stack_for(spec, all_layers)
        rejected_for_key = tuple(r for r in rejected if r.key == spec.key)
        if not stack:
            origin, value = Origin(SourceKind.DEFAULT, "builtin-default"), spec.default
        elif spec.is_array:
            origin, value = stack[-1][0], _merge_array(spec, stack)
        elif spec.is_mapping:
            origin, value = stack[-1][0], _merge_mapping(stack)
        else:
            origin, value = stack[-1]
        out[spec.key] = EffectiveValue(
            spec=spec,
            value=value,
            origin=origin,
            stack=tuple(stack) if stack else ((origin, value),),
            rejected=rejected_for_key,
        )
    return out


def _stack_for(
    spec: OptionSpec, all_layers: Iterable[Layer]
) -> list[tuple[Origin, Any]]:
    """Bindings for one option across layers, sorted low -> high.

    Sorted by source precedence; the pyproject/project-dir tie is broken
    so the project-dir nab.toml sorts last (wins).  Co-presence in both
    project files with conflicting values is a hard error; identical
    values pass.
    """
    found = [
        (layer.origin, layer.values[spec.key])
        for layer in all_layers
        if spec.key in layer.values
    ]
    # Sort by precedence; break the pyproject/project-dir rank-3 tie so
    # the project-dir nab.toml (False < True) sorts last and wins.
    found.sort(
        key=lambda item: (
            _PRECEDENCE[item[0].kind],
            item[0].kind is SourceKind.PROJECT_TOML,
        )
    )
    _check_project_file_conflict(spec, found)
    return found


def _merge_array(
    spec: OptionSpec, stack: Sequence[tuple[Origin, Any]]
) -> tuple[Any, ...]:
    """Concatenate an array option's bindings low-to-high, then re-validate.

    Bindings concatenate in precedence order, so the two project files
    contribute additively and a higher source appends rather than replaces.
    A row whose items are already-built objects re-validates the whole
    through ``merge_check`` (the index-name, override-overlap, and
    conflict-member checks); a plain-string row re-runs ``parse`` over the
    concatenation, so ``constraints`` is re-checked as PEP 508 as a whole.
    """
    merged: list[Any] = []
    for _origin, value in stack:
        merged.extend(value)
    if spec.merge_check is not None:
        return spec.merge_check(merged)
    return spec.parse(merged, f"config {spec.key!r}")


def _merge_mapping(stack: Sequence[tuple[Origin, Any]]) -> Mapping[str, Any]:
    """Fold a name-keyed table's bindings sub-key by sub-key, low-to-high.

    Each layer in ``stack`` already holds a per-layer-validated mapping (its
    own file's ``[tool.nab.index.<name>]`` / ``[marker-environment]`` table
    parsed by the row).  The two project files contribute disjoint sub-keys
    additively; a sub-key present in more than one layer takes the
    highest-precedence binding (and across the two same-rung project files a
    differing sub-key is already an error, caught by
    :func:`_check_project_file_conflict`).  Returned as a plain dict; the
    per-entry body is already validated, and the check that an index name is
    declared runs over the merged config.
    """
    merged: dict[str, Any] = {}
    for _origin, value in stack:
        merged.update(value)
    return merged


def _check_project_file_conflict(
    spec: OptionSpec, found: Sequence[tuple[Origin, Any]]
) -> None:
    """Reject one key set differently in pyproject and the project nab.toml.

    Co-presence is allowed; setting the same key to different values
    across the two same-precedence project files is a hard
    :class:`SourceConfigError`, not a silent last-wins.  Identical values
    are fine.

    An array option (``is_array``) is exempt: its two project-file
    bindings concatenate additively, so differing lists are a merge, not a
    conflict.  The concat is order-stable (pyproject before project-dir
    nab.toml) so the result is deterministic.

    A name-keyed table (``is_mapping``) merges sub-key by sub-key, so the
    two project files contribute disjoint sub-keys additively; only the
    same sub-key set to different values across them is a conflict
    (handled below), mirroring the additive array exemption.
    """
    if spec.is_array:
        return
    by_kind = {origin.kind: value for origin, value in found}
    if SourceKind.PYPROJECT not in by_kind or SourceKind.PROJECT_TOML not in by_kind:
        return
    pyproject_value = by_kind[SourceKind.PYPROJECT]
    project_value = by_kind[SourceKind.PROJECT_TOML]
    if spec.is_mapping:
        _check_mapping_subkey_conflict(spec, pyproject_value, project_value)
        return
    if pyproject_value == project_value:
        return
    msg = (
        f"config {spec.key!r} is set to conflicting values in pyproject"
        f" [tool.nab] ({spec.render(pyproject_value)!r}) and project-dir"
        f" nab.toml ({spec.render(project_value)!r}).  Both files sit at the"
        " same precedence level; set the key in only one, or set them to the"
        " same value."
    )
    raise SourceConfigError(msg)


def _check_mapping_subkey_conflict(
    spec: OptionSpec,
    pyproject_value: Mapping[str, Any],
    project_value: Mapping[str, Any],
) -> None:
    """For a name-keyed table, only a shared sub-key set differently errors.

    Disjoint sub-keys across the two project files merge (additive, like an
    array row); a sub-key present in both with a differing value is the hard
    conflict.  Sorted so the message is deterministic.
    """
    for sub_key in sorted(set(pyproject_value) & set(project_value)):
        if pyproject_value[sub_key] != project_value[sub_key]:
            msg = (
                f"config {spec.key!r}.{sub_key} is set to conflicting values in"
                " pyproject [tool.nab] and project-dir nab.toml.  Both files sit"
                " at the same precedence level; set the sub-key in only one, or"
                " set them to the same value."
            )
            raise SourceConfigError(msg)


# ----- nab config renderers (all derived from the effective map) -----


def _ordered(effective: Mapping[str, EffectiveValue]) -> list[EffectiveValue]:
    return [effective[spec.key] for spec in OPTIONS]


# Column widths for the ``nab config list`` table, shared by the header
# and every row so the two cannot drift.  The trailing ``origin`` column
# is unpadded (last field).
_LIST_KEY_W = 20
_LIST_VALUE_W = 20
_LIST_SCOPE_W = 9
# Status column width for ``nab config explain`` (winner/shadowed/rejected).
_EXPLAIN_STATUS_W = 9


def orphan_rejections(
    rejected: Iterable[RejectedLayer],
) -> tuple[RejectedLayer, ...]:
    """Rejections that name no registry option, so attach to no key.

    An unknown standalone ``nab.toml`` key or an unknown ``NAB_*`` var is
    recorded with ``key`` set to the offending name, which matches no
    registry key, so :func:`resolve_config` attaches it to no
    :class:`EffectiveValue` and no ``explain <key>`` reaches it.  These
    orphans are surfaced by :func:`render_list` instead.
    """
    return tuple(rej for rej in rejected if rej.key not in _BY_KEY)


def render_list(
    effective: Mapping[str, EffectiveValue],
    *,
    rejected: Iterable[RejectedLayer] = (),
) -> str:
    """Render every effective option: value, scope, origin.

    ``rejected`` (when collecting for ``--include-rejected``) adds a
    trailing section listing every rejected source: a key set outside its
    scope, and an unknown key or ``NAB_*`` var.  ``explain`` reaches the
    former (it attaches to the named option) but not the latter (it names
    no option), so ``list`` is the one place that shows both together.
    """
    header = (
        f"{'key':<{_LIST_KEY_W}} {'value':<{_LIST_VALUE_W}}"
        f" {'scope':<{_LIST_SCOPE_W}} origin"
    )
    lines = [header]
    for ev in _ordered(effective):
        rendered = ev.spec.render(ev.value)
        lines.append(
            f"{ev.spec.key:<{_LIST_KEY_W}} {rendered:<{_LIST_VALUE_W}}"
            f" {ev.origin.scope:<{_LIST_SCOPE_W}} {ev.origin.label}"
        )
    rejected = tuple(rejected)
    if rejected:
        lines.append("")
        lines.append("rejected:")
        lines.extend(
            f"  {rej.origin.scope:<{_LIST_SCOPE_W}} {rej.origin.label}"
            f"  {rej.key}: {rej.reason}"
            for rej in rejected
        )
    return "\n".join(lines) + "\n"


def render_get(effective: Mapping[str, EffectiveValue], key: str) -> str:
    """Render only the effective value of ``key``."""
    ev = _require_key(effective, key)
    return ev.spec.render(ev.value) + "\n"


def render_explain(
    effective: Mapping[str, EffectiveValue],
    key: str,
    *,
    include_rejected: bool = False,
) -> str:
    """Render the full shadowed stack for ``key``.

    The winner is marked with a ``>`` gutter, every other source is
    ``shadowed``.  With ``include_rejected`` the category-rejected
    sources (a source that tried to set ``key`` but was not allowed) are
    listed too, labelled ``rejected``.
    """
    ev = _require_key(effective, key)
    lines = [f"{key} ({ev.spec.scope.value}, {ev.spec.type_label})"]
    winner_index = len(ev.stack) - 1
    merged = ev.spec.is_array or ev.spec.is_mapping
    for i, (origin, value) in enumerate(ev.stack):
        # Array / name-keyed-table options merge every layer low-to-high
        # (concat for arrays, sub-key union for mappings), so no single
        # layer's binding is the effective value; the per-layer rows are
        # contributions and the merged value is rendered separately below.
        if merged:
            gutter, status = " ", "contributes"
        else:
            gutter = ">" if i == winner_index else " "
            status = "winner" if i == winner_index else "shadowed"
        rendered = ev.spec.render(value)
        lines.append(
            f"{gutter} {origin.scope:<{_LIST_SCOPE_W}} {rendered:<{_LIST_VALUE_W}}"
            f" {status:<{_EXPLAIN_STATUS_W}} {origin.label}"
        )
    if merged:
        lines.append(f"= effective: {ev.spec.render(ev.value)}")
    if include_rejected:
        lines.extend(
            f"  {rej.origin.scope:<{_LIST_SCOPE_W}} {'-':<{_LIST_VALUE_W}}"
            f" {'rejected':<{_EXPLAIN_STATUS_W}} {rej.origin.label} ({rej.reason})"
            for rej in ev.rejected
        )
    return "\n".join(lines) + "\n"


def _require_key(effective: Mapping[str, EffectiveValue], key: str) -> EffectiveValue:
    ev = effective.get(key)
    if ev is None:
        valid = sorted(_BY_KEY)
        msg = f"unknown config key {key!r}; known keys are {valid!r}"
        raise SourceConfigError(msg)
    return ev


def build_cli_overrides(locals_by_param: Mapping[str, Any]) -> dict[str, Any]:
    """Map ``{cli_param: value}`` to a registry-keyed override dict.

    Iterates :data:`OPTIONS`, reads each row's ``cli_param`` out of
    ``locals_by_param``, and keeps only the keys the user actually set.
    An unset scalar flag is ``None`` and an unset array flag is an empty
    tuple (the append-action default); both are omitted so they do not
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


def build_cli_layer(values: Mapping[str, Any]) -> Layer:
    """Build the CLI layer from a ``{key: value}`` map of set overrides.

    ``values`` holds only keys the user actually set on the CLI (an
    unset flag is omitted, so it does not shadow lower layers).  Each
    value is normalised through its registry row so the effective value
    carries the typed form regardless of how the flag was spelled.
    """
    parsed: dict[str, Any] = {}
    for key, value in values.items():
        spec = _BY_KEY[key]
        # An array flag arrives as a tuple from tyro's append action; the
        # parse hooks expect a TOML list, so normalise it here.
        raw = list(value) if spec.is_array and isinstance(value, tuple) else value
        parsed[key] = spec.parse(raw, f"cli:{spec.cli_flag}")
    return Layer(Origin(SourceKind.CLI, "cli"), parsed)


def project_cli_override_records(
    effective: Mapping[str, EffectiveValue],
) -> tuple[tuple[str, str], ...]:
    """Return the ``(flag, value)`` pairs for PROJECT options set on the CLI.

    A PROJECT option changes the resolved set, so a CLI override means the
    result no longer derives from the committed files alone.  These pairs
    drive both the reproducibility notice and the auditable record written
    into the lockfile provenance.  A file-only row (``cli_flag`` is ``None``)
    is never CLI-settable, so it cannot appear.
    """
    records: list[tuple[str, str]] = []
    for spec in OPTIONS:
        if spec.scope is not Scope.PROJECT:
            continue
        ev = effective[spec.key]
        if ev.origin.kind is not SourceKind.CLI or spec.cli_flag is None:
            continue
        records.append((spec.cli_flag, spec.render(ev.value)))
    return tuple(records)


def project_cli_override_notice(
    effective: Mapping[str, EffectiveValue],
    *,
    produces_lock: bool = True,
) -> str | None:
    """Reproducibility notice for any PROJECT option set on the CLI.

    Returns a notice listing every PROJECT override that came from the CLI
    rung; ``None`` when no PROJECT option was set on the CLI.

    ``produces_lock`` tailors the wording: ``nab lock`` produces a lock, so
    the notice warns the lock will not derive from the committed files; the
    read-only ``nab config`` inspector produces no lock, so it warns only
    that the displayed values reflect a CLI override.
    """
    records = project_cli_override_records(effective)
    if not records:
        return None
    if produces_lock:
        header = (
            "notice: project-scope overrides were applied from the CLI; the lock"
            " they produce does not derive from the committed pyproject/nab.toml"
            " alone:"
        )
    else:
        header = (
            "notice: project-scope overrides were applied from the CLI; the"
            " values below reflect that override, not the committed"
            " pyproject/nab.toml alone:"
        )
    lines = [header]
    lines.extend(f"  {flag} -> {rendered}" for flag, rendered in records)
    return "\n".join(lines) + "\n"
