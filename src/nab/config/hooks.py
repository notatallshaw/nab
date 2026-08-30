"""Turn one layered option's raw value into its typed form, and back.

Every row of :data:`nab.config.registry.OPTIONS` names one ``parse`` and one
``render`` hook here.  A hook takes the raw TOML, env or CLI value and the
location to name in an error, and delegates the value's own grammar to
:mod:`nab.config.values` so a key is rejected in the same words whichever
reader saw it.

The hooks take a fixed pair of arguments, so the three pieces of parse state
that vary per pass ride on context variables the ladder binds around a read:
the resolve anchor a ``P<n>D`` duration is measured from, the one ``now`` an
inspector pass shares, and the directory a relative path in the file being
read resolves against.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from nab_project.paths import is_usable_path_name
from nab_provider.errors import ConfigError
from nab_provider.policy import (
    BuildPolicy,
    DecisionOrder,
    ResolutionStrategy,
    ResolveMode,
)
from nab_provider.serialization import SimpleSerialization

from . import values

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from nab_provider.policy import (
        ArchiveSource,
        DistPolicy,
        LocalSource,
        VcsSource,
    )
    from nab_provider.records import IndexConfig

__all__ = [
    "HTTP_BACKENDS",
    "SourceConfigError",
    "declaring_dir",
    "inspector_anchor",
    "parse_archive_sources",
    "parse_base_group",
    "parse_bool",
    "parse_build_group",
    "parse_build_policy",
    "parse_build_requires_depth",
    "parse_conflicts",
    "parse_constraints",
    "parse_decision_order",
    "parse_default_groups",
    "parse_dist_policy",
    "parse_environment",
    "parse_http_backend",
    "parse_index_overrides",
    "parse_indexes",
    "parse_local_sources",
    "parse_marker_environment",
    "parse_matrix",
    "parse_max_concurrency",
    "parse_mode",
    "parse_package_rules",
    "parse_packages",
    "parse_path",
    "parse_requires_python",
    "parse_resolution",
    "parse_uploaded_prior_to",
    "parse_vcs",
    "parse_vcs_sources",
    "parse_workspace",
    "render_archive_sources",
    "render_bool",
    "render_cache_dir",
    "render_conflicts",
    "render_dist_policy",
    "render_enum_value",
    "render_environment",
    "render_index_list",
    "render_index_overrides",
    "render_local_sources",
    "render_marker_environment",
    "render_matrix",
    "render_optional_text",
    "render_package_overrides",
    "render_string_tuple",
    "render_text",
    "render_uploaded_prior_to",
    "render_vcs",
    "render_vcs_sources",
    "render_workspace",
    "resolve_anchor",
]


# The anchor that ``P<n>D`` relative durations resolve against during a
# real resolve.  ``nab config`` (the inspector) leaves it unset, so the
# display anchors a relative duration at the current time; the resolve
# path (``model.read_pyproject_config``) sets it to the lockfile-captured
# anchor for the duration of the merge so re-locks reproduce the same
# cutoff.  Carried on a ContextVar rather than threaded through every
# fixed-arity parse hook.
_RESOLVE_ANCHOR: ContextVar[datetime | None] = ContextVar(
    "_RESOLVE_ANCHOR", default=None
)

# The directory of the TOML file currently being parsed.  Relative
# ``local-sources`` paths resolve against the declaring file's directory;
# carried structurally on a ContextVar (bound by :func:`declaring_dir`)
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


@contextmanager
def declaring_dir(directory: Path) -> Iterator[None]:
    """Bind the directory a relative path in the file being read resolves against.

    The ladder wraps each TOML source's parse in this, so
    :func:`parse_local_sources` resolves a relative ``local-sources`` path
    against the declaring file's own directory rather than the cwd.
    """
    token = _DECLARING_DIR.set(directory)
    try:
        yield
    finally:
        _DECLARING_DIR.reset(token)


class SourceConfigError(ConfigError):
    """A layered source set a value it is not allowed to set.

    Raised by the category gate: e.g. a PROJECT option appearing in a
    user ``nab.toml`` or env var, or a USER option appearing in
    ``pyproject.toml`` ``[tool.nab]``.  A subclass of :class:`ConfigError`
    so a caller catching the broad config error also catches a layered
    gate or cross-file conflict failure, while ``except SourceConfigError``
    still narrows to the layered cases.
    """


def parse_bool(value: Any, where: str) -> bool:
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


def parse_path(value: Any, where: str) -> Path:
    """Return the ``cache-dir`` row's value as a path."""
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
    if not is_usable_path_name(value):
        msg = f"{where} {value!r} is not a usable filesystem path"
        raise SourceConfigError(msg)
    return Path(value)


def parse_resolution(value: Any, where: str) -> ResolutionStrategy:
    """Return the ``resolution`` row's strategy."""
    # Unlike the other enum rows, this message names the source location, not the key.
    return _delegate(
        lambda: values.parse_enum(
            where, value, ResolutionStrategy, ResolutionStrategy.HIGHEST
        )
    )


HTTP_BACKENDS = ("urllib3", "httpx")


def parse_http_backend(value: Any, where: str) -> str:
    """Return the ``http-backend`` row's backend name."""
    if not isinstance(value, str):
        msg = f"{where} must be a string, got {type(value).__name__}"
        raise SourceConfigError(msg)
    lowered = value.strip()
    if lowered not in HTTP_BACKENDS:
        msg = f"{where} must be one of {list(HTTP_BACKENDS)!r}, got {value!r}"
        raise SourceConfigError(msg)
    return lowered


def parse_max_concurrency(value: Any, where: str) -> int:
    """Return the ``max-concurrency`` row's positive count."""
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


_ParsedT = TypeVar("_ParsedT")


def _delegate(call: Callable[[], _ParsedT]) -> _ParsedT:
    """Run a :mod:`nab.config.values` parser, re-typing its error for the registry.

    The rows reuse those parsers so a value and its validation wording match
    the pyproject parse path where there is one.  They raise
    :class:`ConfigError` and the registry contract is
    :class:`SourceConfigError`, so re-raise with the same message.
    """
    try:
        return call()
    except ConfigError as exc:
        raise SourceConfigError(str(exc)) from exc


def parse_mode(value: Any, where: str) -> ResolveMode:
    """Return the ``mode`` row's resolve mode."""
    del where
    return _delegate(
        lambda: values.parse_enum("mode", value, ResolveMode, ResolveMode.SPECIFIC)
    )


def parse_requires_python(value: Any, where: str) -> str | None:
    """Return the ``requires-python`` row's specifier text."""
    del where
    return _delegate(lambda: values.parse_requires_python(value))


def parse_dist_policy(value: Any, where: str) -> tuple[DistPolicy, bool]:
    """Return the ``dist-policy`` row's policy and its sdist-trust flag."""
    # The scalar-or-table dist-policy folds the sdist-trust bool, so the
    # registry value is the (policy, trust) pair the global parser returns.
    del where
    return _delegate(lambda: values.parse_dist_policy_global(value))


def parse_build_requires_depth(value: Any, where: str) -> int:
    """Return the ``build-requires-depth`` row's nesting depth."""
    # bool is an int subclass, so reject it rather than read True as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = (
            f"{where} must be a non-negative integer number of nested build"
            f" environments, got {value!r}"
        )
        raise SourceConfigError(msg)
    return value


def parse_build_policy(value: Any, where: str) -> BuildPolicy:
    """Return the ``build-policy`` row's policy."""
    # The plain scalar last-wins value only.  The host-build gate that forces
    # never for a declared target is a post-merge transform applied over the
    # merged config, not this row.
    del where
    return _delegate(
        lambda: values.parse_enum(
            "build-policy", value, BuildPolicy, BuildPolicy.BUILD_LOCAL
        )
    )


def parse_decision_order(value: Any, where: str) -> DecisionOrder:
    """Return the ``decision-order`` row's order."""
    del where
    return _delegate(
        lambda: values.parse_enum(
            "decision-order", value, DecisionOrder, DecisionOrder.ARRIVAL
        )
    )


def parse_uploaded_prior_to(value: Any, where: str) -> Any:
    """Parse ``uploaded-prior-to`` without re-anchoring relative durations.

    A ``P<n>D`` duration anchors to the lockfile at resolve time, and the
    registry merely gates and displays the key, so it must not silently
    re-anchor one to ``now``.  A relative duration is carried as its raw
    string (the cross-file conflict check compares raw strings, so identical
    durations match and different ones conflict), and only an absolute
    datetime is normalised through the shared parser.
    """
    del where
    if isinstance(value, str) and values.DURATION_PATTERN.match(value):
        # On the resolve path the active anchor is set, so resolve the
        # relative duration to its absolute cutoff now; the inspector
        # leaves the anchor unset and carries the raw string for display
        # (its now-anchor would be misleading in a stored value).
        if _RESOLVE_ANCHOR.get() is not None:
            return _delegate(
                lambda: values.parse_uploaded_prior_to(value, anchor=_current_anchor())
            )
        return value
    # Absolute datetime / TOML offset-datetime: anchor is irrelevant, but
    # the helper requires one, so pass a placeholder it never reads.
    return _delegate(
        lambda: values.parse_uploaded_prior_to(value, anchor=_current_anchor())
    )


def parse_marker_environment(value: Any, where: str) -> Mapping[str, str]:
    """Return the ``marker-environment`` row's marker table."""
    # Name-keyed table (PEP 508 marker var -> str), validated
    # (string->string, known marker vars) as on the pyproject path.
    del where
    return _delegate(lambda: values.parse_marker_environment(value))


def parse_environment(value: Any, where: str) -> Mapping[str, Any]:
    """Return the ``environment`` row's axis table."""
    # Name-keyed table (python/platform/implementation), one cell of a
    # matrix; platform is an id or a table of tag knobs.  A mapping row, so
    # the axes merge sub-key by sub-key across the ladder and a ``--python``
    # override moves only the python axis.
    del where
    return _delegate(lambda: values.parse_environment(value))


def parse_vcs(value: Any, where: str) -> Any:
    """Return the ``vcs`` row's admission policy."""
    # Nested table (policy, allowed-schemes, allowed-repos, require-pin),
    # folded into the frozen VcsConfig.
    del where
    return _delegate(lambda: values.parse_vcs(value))


def parse_workspace(value: Any, where: str) -> Any:
    """Return the ``workspace`` row's declared member table."""
    # Nested table (members), folded into a WorkspaceConfig or None.  The
    # discovery walk-up is a post-merge transform, never this row, so the
    # registry only folds the declared table.
    del where
    return _delegate(lambda: values.parse_workspace(value))


def parse_constraints(value: Any, where: str) -> tuple[str, ...]:
    """Return the ``constraints`` row's requirement strings."""
    # Array of PEP 508 strings, shape-checked and validated per item with
    # the same messages as the pyproject parse path.
    del where
    return _delegate(lambda: values.parse_constraints(value))


def parse_default_groups(value: Any, where: str) -> tuple[str, ...]:
    """Return the ``default-groups`` row's group names."""
    # Array of group names, shape-checked with the same message as the
    # pyproject path.  The default-groups-vs-conflicts cross-check needs
    # both merged values, so this row only folds the list itself.
    del where
    return _delegate(lambda: values.parse_string_list("default-groups", value))


def parse_base_group(value: Any, where: str) -> str | None:
    """Return the ``base-group`` row's group name."""
    del where
    return _delegate(lambda: values.parse_base_group(value))


def parse_build_group(value: Any, where: str) -> str | None:
    """Return the ``build-group`` row's group name."""
    del where
    return _delegate(lambda: values.parse_build_group(value))


def parse_indexes(value: Any, where: str) -> tuple[IndexConfig, ...]:
    """Return the ``indexes`` row's index list."""
    # One file's array-of-tables: shape, keys and the same-name check,
    # folded into IndexConfig entries.
    del where
    return _delegate(lambda: values.parse_indexes(value))


def parse_local_sources(value: Any, where: str) -> tuple[LocalSource, ...]:
    """Return the ``local-sources`` row's sources."""
    # One file's array-of-tables (name, path, editable, subdirectory).  Paths
    # resolve relative to the declaring file's directory (both legal sources
    # share the project dir).  The cross-source local/vcs/archive name check
    # is a whole-config pass on the resolve path.
    del where
    base_dir = _current_declaring_dir()
    return _delegate(lambda: values.parse_local_sources(value, pyproject_dir=base_dir))


def _current_declaring_dir() -> Path:
    # local-sources is a file-only row (no CLI/env), so its per-layer parse
    # is only ever reached from a TOML layer, which sets _DECLARING_DIR to
    # the file's directory.  Paths in the file resolve relative to it.
    base = _DECLARING_DIR.get()
    if base is None:  # pragma: no cover - per-layer parse always sets it
        msg = "local-sources parsed without a declaring directory"
        raise SourceConfigError(msg)
    return base


def parse_vcs_sources(value: Any, where: str) -> tuple[VcsSource, ...]:
    """Return the ``vcs-sources`` row's sources."""
    # One file's array-of-tables (name, url); same shape as local-sources.
    del where
    return _delegate(lambda: values.parse_vcs_sources(value))


def parse_archive_sources(value: Any, where: str) -> tuple[ArchiveSource, ...]:
    """Return the ``archive-sources`` row's sources."""
    # One file's array-of-tables (name, url); same shape as vcs-sources.
    del where
    return _delegate(lambda: values.parse_archive_sources(value))


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
# only its own surface, with the body and the within-surface same-field
# overlap validated by the shared parsers.  The cross-surface
# packages-vs-package-rules overlap and the route-names-a-declared-index
# check both need the merged whole, so they run on the resolve path in
# config._config_from_effective, not here.
def parse_packages(value: Any, where: str) -> tuple[Any, ...]:
    """Return the ``packages`` row's overrides, desugared."""
    del where
    return _delegate(
        lambda: _checked_overrides(
            values.parse_packages_sugar(value, anchor=_override_anchor())
        )
    )


def parse_package_rules(value: Any, where: str) -> tuple[Any, ...]:
    """Return the ``package-rules`` row's overrides."""
    del where
    return _delegate(
        lambda: _checked_overrides(
            values.parse_package_rules(value, anchor=_override_anchor())
        )
    )


def _checked_overrides(overrides: Iterable[Any]) -> tuple[Any, ...]:
    """Reject a same-field overlap among one surface's desugared overrides.

    The cross-surface packages-vs-package-rules overlap needs both rows
    merged, so it runs on the resolve path; this is the within-surface half,
    run here so ``nab config`` refuses the same file the resolve refuses.
    """
    built = tuple(overrides)
    values.check_package_override_overlap(built)
    return built


def parse_index_overrides(value: Any, where: str) -> Mapping[str, Any]:
    """Return the ``index`` row's per-index overrides."""
    # Name-keyed table (``[tool.nab.index.<name>]``).  The body (policy
    # fields) is validated as on the pyproject path; the
    # key-names-a-declared-index check needs the merged indexes and so runs on
    # the resolve path.
    del where
    return _delegate(
        lambda: values.parse_index_overrides(value, anchor=_override_anchor())
    )


def parse_conflicts(value: Any, where: str) -> tuple[Any, ...]:
    """Return the ``conflicts`` row's conflict sets."""
    # One file's array-of-tables (member list/table + policy): shape,
    # members and policy are validated and the member-uniqueness check runs
    # here.  The default-groups-vs-conflicts check needs both merged values,
    # so it runs on the resolve path.
    del where
    return _delegate(lambda: values.parse_conflicts(value))


def parse_matrix(value: Any, where: str) -> Any:
    """Return the ``matrix`` row's expanded matrix."""
    # Nested table (python, platforms, python-order, python-patches,
    # implementations), axis-validated and eagerly expanded as on the
    # pyproject path.  The mode/matrix mutual-requirement check needs both
    # merged values, so it runs over the merged config rather than in this
    # row.
    del where
    return _delegate(lambda: values.parse_matrix(value))


def render_conflicts(value: Sequence[Any]) -> str:
    """Render every conflict set, or ``<none>``."""
    if not value:
        return "<none>"
    return "; ".join(str(cs) for cs in value)


def render_matrix(value: Any) -> str:
    """Render the matrix as its python axis and platform labels."""
    if value is None:
        return "<none>"
    platforms = [p.label for p in value.platforms]
    return f"python={value.python}, platforms={platforms}"


def render_package_overrides(value: Sequence[Any]) -> str:
    """Render each override's requirement, or ``<none>``."""
    if not value:
        return "<none>"
    return ", ".join(str(o.requirement) for o in value)


def render_index_overrides(value: Mapping[str, Any]) -> str:
    """Render the overridden index names, or ``<none>``."""
    if not value:
        return "<none>"
    return ", ".join(sorted(value))


def render_index_list(value: Sequence[IndexConfig]) -> str:
    """Render each index as ``name=url``, or ``<none>``."""
    if not value:
        return "<none>"
    return ", ".join(f"{i.name}={i.url}{_render_index_pin(i)}" for i in value)


def _render_index_pin(index: IndexConfig) -> str:
    if index.serialization is SimpleSerialization.NEGOTIATE:
        return ""
    return f" serialization={index.serialization.value}"


def render_local_sources(value: Sequence[LocalSource]) -> str:
    """Render each source as ``name@path``, or ``<none>``."""
    if not value:
        return "<none>"
    return ", ".join(f"{s.name}@{s.path}" for s in value)


def render_vcs_sources(value: Sequence[VcsSource]) -> str:
    """Render each source as ``name@url``, or ``<none>``."""
    if not value:
        return "<none>"
    return ", ".join(f"{s.name}@{s.url}" for s in value)


def render_archive_sources(value: Sequence[ArchiveSource]) -> str:
    """Render each source as ``name@url``, or ``<none>``."""
    if not value:
        return "<none>"
    return ", ".join(f"{s.name}@{s.url}" for s in value)


def render_string_tuple(value: Sequence[str]) -> str:
    """Render a list row as a comma-separated line, or ``<none>``."""
    return "<none>" if not value else ", ".join(value)


def render_marker_environment(value: Mapping[str, str]) -> str:
    """Render each marker as ``name=value``, or ``<none>``."""
    if not value:
        return "<none>"
    return ", ".join(f"{k}={v}" for k, v in sorted(value.items()))


def render_environment(value: Mapping[str, Any]) -> str:
    """Render each axis as ``name=value``, or ``<host>`` when unset."""
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


def render_vcs(value: Any) -> str:
    """Render the policy and any allow-list or pin rule it carries."""
    parts = [f"policy={value.policy.value}"]
    if value.allowed_schemes:
        parts.append(f"allowed-schemes={sorted(value.allowed_schemes)}")
    if value.allowed_repos:
        parts.append(f"allowed-repos={list(value.allowed_repos)}")
    if not value.require_pin:
        parts.append("require-pin=false")
    return ", ".join(parts)


def render_workspace(value: Any) -> str:
    """Render the declared members, or ``<none>``."""
    if value is None:
        return "<none>"
    return f"members={list(value.members)}"


def render_uploaded_prior_to(value: Any) -> str:
    """Render the cutoff, or ``<none>``."""
    return "<none>" if value is None else str(value)


def render_dist_policy(value: Any) -> str:
    """Render the policy, naming the sdist-trust flag when it is set."""
    # value is the (policy, trust_unverified_deps) pair the global parser
    # returns; show the trust flag only when it diverges from the default.
    policy, trust = value
    return f"{policy.value} (trust-unverified-deps)" if trust else policy.value


def render_enum_value(value: Any) -> str:
    """Render an enum-valued row as the spelling its members carry."""
    return str(value.value)


def render_text(value: Any) -> str:
    """Render a scalar row's value as plain text."""
    return str(value)


def render_optional_text(value: Any) -> str:
    """Render a scalar row's value, or ``<none>`` where it is unset."""
    return "<none>" if value is None else str(value)


def render_bool(value: Any) -> str:
    """Render a boolean row the way TOML spells one."""
    return "true" if value else "false"


def render_cache_dir(value: Any) -> str:
    """Render the cache root, or ``<computed>`` where nab derives it."""
    return "<computed>" if value is None else str(value)
