"""Parse project settings that depend on a declaring file or cutoff anchor."""

from __future__ import annotations

__lazy_modules__ = (
    "datetime",
    "nab_project.config_values",
    "nab_provider.serialization",
)

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from nab_index.file_urls import parse_file_url
from nab_provider.records import IndexConfig

from . import config_values as values
from .config_values import SourceConfigError

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Mapping
    from pathlib import Path

    from nab_provider.policy import (
        LocalSource,
    )


__all__ = [
    "declaring_dir",
    "inspector_anchor",
    "key_prefix",
    "parse_index_overrides",
    "parse_indexes",
    "parse_local_sources",
    "parse_matrix",
    "parse_package_rules",
    "parse_packages",
    "parse_uploaded_prior_to",
    "resolve_anchor",
]

# The resolve anchor for ``P<n>D`` values, kept out of fixed-arity parsers.
_RESOLVE_ANCHOR: ContextVar[datetime | None] = ContextVar(
    "_RESOLVE_ANCHOR", default=None
)

# Keep the declaring directory separate from diagnostic source labels.
_DECLARING_DIR: ContextVar[Path | None] = ContextVar("_DECLARING_DIR", default=None)

# Standalone nab.toml tables have no tool.nab prefix.
_KEY_PREFIX: ContextVar[str] = ContextVar("_KEY_PREFIX", default="tool.nab.")

# One timestamp per inspector pass keeps equal relative values equal across files.
_INSPECTOR_ANCHOR: ContextVar[datetime] = ContextVar("_INSPECTOR_ANCHOR")


def _current_anchor() -> datetime:
    """Return the resolve anchor, or the current time for inspection."""
    anchor = _RESOLVE_ANCHOR.get()
    return anchor if anchor is not None else datetime.now(timezone.utc)


@contextmanager
def resolve_anchor(anchor: datetime | None) -> Generator[None, None, None]:
    """Bind the ``P<n>D`` resolve anchor for one config merge."""
    token = _RESOLVE_ANCHOR.set(anchor)
    try:
        yield
    finally:
        _RESOLVE_ANCHOR.reset(token)


@contextmanager
def inspector_anchor(anchor: datetime | None = None) -> Generator[None, None, None]:
    """Pin one current-time anchor for an inspector merge.

    Override bodies resolve relative durations eagerly. Sharing one instant
    keeps equal values in both project files from becoming a false conflict.
    """
    token = _INSPECTOR_ANCHOR.set(
        datetime.now(timezone.utc) if anchor is None else anchor
    )
    try:
        yield
    finally:
        _INSPECTOR_ANCHOR.reset(token)


@contextmanager
def declaring_dir(directory: Path) -> Generator[None, None, None]:
    """Bind the declaring file's directory for resolving relative paths."""
    token = _DECLARING_DIR.set(directory)
    try:
        yield
    finally:
        _DECLARING_DIR.reset(token)


@contextmanager
def key_prefix(prefix: str) -> Generator[None, None, None]:
    """Bind the declaring file's TOML key prefix for parser hints."""
    token = _KEY_PREFIX.set(prefix)
    try:
        yield
    finally:
        _KEY_PREFIX.reset(token)


def parse_uploaded_prior_to(value: Any, where: str) -> Any:
    """Parse ``uploaded-prior-to`` without re-anchoring relative durations.

    A ``P<n>D`` duration anchors to the lockfile at resolve time, and the
    registry only validates and displays the key. It must not silently
    re-anchor one to ``now``. A relative duration is carried as its raw
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


def parse_indexes(value: object, where: str) -> tuple[IndexConfig, ...]:
    """Validate file-index URLs and anchor relative paths at their declaring file."""
    indexes = values.parse_indexes(value, where)
    result = []
    for index in indexes:
        try:
            parsed = urlsplit(index.url)
        except ValueError:
            result.append(index)
            continue
        configured = index
        if parsed.scheme == "file":
            try:
                path = parse_file_url(index.url)
            except ValueError as exc:
                message = f"{where}: invalid file-index URL: {exc}"
                raise SourceConfigError(message) from exc
            if not parsed.netloc and not path.is_absolute():
                url = (_current_declaring_dir() / path).absolute().as_uri()
                configured = IndexConfig(index.name, url, index.serialization)
        result.append(configured)
    return tuple(result)


def _current_declaring_dir() -> Path:
    """Return the directory set while parsing a file's relative locations."""
    base = _DECLARING_DIR.get()
    if base is None:  # pragma: no cover - per-layer parse always sets it
        msg = "relative location parsed without a declaring directory"
        raise SourceConfigError(msg)
    return base


def parse_matrix(value: Any, where: str) -> values.MatrixConfig:
    """Return the ``matrix`` row's config, its table named for the declaring file."""
    return values.parse_matrix(value, where, table=f"{_KEY_PREFIX.get()}matrix")


def _override_anchor() -> datetime:
    """Return the resolve anchor or the bound comparison time."""
    resolve = _RESOLVE_ANCHOR.get()
    if resolve is not None:
        return resolve
    return _INSPECTOR_ANCHOR.get()


def parse_packages(value: Any, where: str) -> tuple[Any, ...]:
    """Return the ``packages`` row's overrides, desugared."""
    return _checked_overrides(
        values.parse_packages_sugar(
            value, where, anchor=_override_anchor(), key_prefix=_KEY_PREFIX.get()
        )
    )


def parse_package_rules(value: Any, where: str) -> tuple[Any, ...]:
    """Return the ``package-rules`` row's overrides."""
    return _checked_overrides(
        values.parse_package_rules(
            value, where, anchor=_override_anchor(), key_prefix=_KEY_PREFIX.get()
        )
    )


def _checked_overrides(overrides: Iterable[Any]) -> tuple[Any, ...]:
    """Return one table's overrides after rejecting same-field overlaps."""
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
