"""The parse and render hooks a row needs the ladder's own state for.

Every row of :data:`nab.config.ladder.OPTIONS` names one ``parse`` and one
``render``.  Most rows name a parser in :mod:`nab.config.values` directly;
the ones here either render a merged value back to a line of ``nab config``,
or need a piece of parse state that varies per pass and cannot travel in the
fixed ``(value, where)`` pair.  Four such pieces ride on context variables
the ladder binds around a read: the resolve anchor a ``P<n>D`` duration is
measured from, the one ``now`` an inspector pass shares, the directory a
relative path in the file being read resolves against, and the matrix
header that file writes.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from nab_provider.serialization import SimpleSerialization

from . import values
from .values import SourceConfigError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from nab_provider.policy import (
        ArchiveSource,
        LocalSource,
        VcsSource,
    )
    from nab_provider.records import IndexConfig

__all__ = [
    "declaring_dir",
    "inspector_anchor",
    "matrix_table",
    "parse_index_overrides",
    "parse_local_sources",
    "parse_matrix",
    "parse_package_rules",
    "parse_packages",
    "parse_uploaded_prior_to",
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

# The resolve anchor for ``P<n>D`` values, kept out of fixed-arity parsers.
_RESOLVE_ANCHOR: ContextVar[datetime | None] = ContextVar(
    "_RESOLVE_ANCHOR", default=None
)

# The directory of the TOML file currently being parsed.  Relative
# ``local-sources`` paths resolve against the declaring file's directory;
# carried structurally on a ContextVar (bound by :func:`declaring_dir`)
# rather than re-derived by splitting a human-facing ``where`` label.
_DECLARING_DIR: ContextVar[Path | None] = ContextVar("_DECLARING_DIR", default=None)

# The matrix header spelling for the file being parsed.
_MATRIX_TABLE: ContextVar[str] = ContextVar("_MATRIX_TABLE", default="tool.nab.matrix")

# One timestamp per inspector pass keeps equal relative values equal across files.
_INSPECTOR_ANCHOR: ContextVar[datetime | None] = ContextVar(
    "_INSPECTOR_ANCHOR", default=None
)


def _current_anchor() -> datetime:
    """Return the resolve anchor, or the current time for inspection."""
    anchor = _RESOLVE_ANCHOR.get()
    return anchor if anchor is not None else datetime.now(timezone.utc)


@contextmanager
def resolve_anchor(anchor: datetime | None) -> Iterator[None]:
    """Bind the ``P<n>D`` resolve anchor for one config merge."""
    token = _RESOLVE_ANCHOR.set(anchor)
    try:
        yield
    finally:
        _RESOLVE_ANCHOR.reset(token)


@contextmanager
def inspector_anchor() -> Iterator[None]:
    """Pin one current-time anchor for an inspector merge.

    Override bodies resolve relative durations eagerly. Sharing one instant
    keeps equal values in both project files from becoming a false conflict.
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


@contextmanager
def matrix_table(table: str) -> Iterator[None]:
    """Bind the matrix header of the file being read.

    The ladder wraps each TOML source's parse in this, so
    :func:`parse_matrix` names the python-patches table the way the
    declaring file writes it: ``[tool.nab.matrix.python-patches]`` in a
    pyproject.toml, ``[matrix.python-patches]`` in a nab.toml.
    """
    token = _MATRIX_TABLE.set(table)
    try:
        yield
    finally:
        _MATRIX_TABLE.reset(token)


def parse_uploaded_prior_to(value: Any, where: str) -> Any:
    """Parse ``uploaded-prior-to`` without re-anchoring relative durations.

    A ``P<n>D`` duration anchors to the lockfile at resolve time, and the
    registry merely gates and displays the key, so it must not silently
    re-anchor one to ``now``.  A relative duration is carried as its raw
    string (the cross-file conflict check compares raw strings, so identical
    durations match and different ones conflict), and only an absolute
    datetime is normalised through the shared parser.
    """
    if (
        isinstance(value, str)
        and values.DURATION_PATTERN.match(value)
        and _RESOLVE_ANCHOR.get() is None
    ):
        # The inspector leaves the anchor unset, so a relative duration is
        # carried as its raw string; a now-anchor would be misleading in a
        # stored value.  The resolve path sets it and falls through, so the
        # duration resolves to the lockfile's cutoff.
        return value
    # An absolute datetime ignores the anchor, but the parser requires one.
    return values.parse_uploaded_prior_to(value, where, anchor=_current_anchor())


def parse_local_sources(value: Any, where: str) -> tuple[LocalSource, ...]:
    """Return the ``local-sources`` row's sources."""
    # One file's array-of-tables (name, path, editable, subdirectory).  Paths
    # resolve relative to the declaring file's directory (both legal sources
    # share the project dir).  The cross-source local/vcs/archive name check
    # is a whole-config pass on the resolve path.
    return values.parse_local_sources(
        value, where, pyproject_dir=_current_declaring_dir()
    )


def _current_declaring_dir() -> Path:
    # local-sources is a file-only row (no CLI/env), so its per-layer parse
    # is only ever reached from a TOML layer, which sets _DECLARING_DIR to
    # the file's directory.  Paths in the file resolve relative to it.
    base = _DECLARING_DIR.get()
    if base is None:  # pragma: no cover - per-layer parse always sets it
        msg = "local-sources parsed without a declaring directory"
        raise SourceConfigError(msg)
    return base


def parse_matrix(value: Any, where: str) -> values.MatrixConfig:
    """Return the ``matrix`` row's config, its table named for the declaring file."""
    return values.parse_matrix(value, where, table=_MATRIX_TABLE.get())


def _override_anchor() -> datetime:
    """Return the resolve anchor, inspector anchor, or current time."""
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
    return _checked_overrides(
        values.parse_packages_sugar(value, where, anchor=_override_anchor())
    )


def parse_package_rules(value: Any, where: str) -> tuple[Any, ...]:
    """Return the ``package-rules`` row's overrides."""
    return _checked_overrides(
        values.parse_package_rules(value, where, anchor=_override_anchor())
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
    return values.parse_index_overrides(value, where, anchor=_override_anchor())


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
