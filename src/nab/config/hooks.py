"""Parse and render configuration values that need ladder-owned state.

Context variables supply time, path, and table-name state to fixed-arity
parser hooks.
"""

from __future__ import annotations

__lazy_modules__ = (
    "datetime",
    "nab.config",
    "nab.config.values",
    "nab_provider.serialization",
)

from typing import TYPE_CHECKING, Any

from nab_provider.serialization import SimpleSerialization

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

    from nab_project.config_values import MatrixConfig
    from nab_provider.policy import (
        ArchiveSource,
        LocalSource,
        VcsSource,
    )
    from nab_provider.records import IndexConfig
    from nab_provider.target import ImplementationOverride, MatrixInclude

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from nab_project.config_schema import ProjectValueContext

_DECLARING_DIR: ContextVar[Path | None] = ContextVar("cli_declaring_dir", default=None)
_KEY_PREFIX: ContextVar[str] = ContextVar("cli_key_prefix", default="tool.nab.")
_INSPECTOR_ANCHOR: ContextVar[datetime | None] = ContextVar(
    "cli_inspector_anchor", default=None
)


@contextmanager
def declaring_dir(directory: Path) -> Generator[None, None, None]:
    """Bind the declaring location used by CLI configuration inspection."""
    token = _DECLARING_DIR.set(directory)
    try:
        yield
    finally:
        _DECLARING_DIR.reset(token)


@contextmanager
def key_prefix(prefix: str) -> Generator[None, None, None]:
    """Bind the project-table path used by the inspector's parser calls."""
    token = _KEY_PREFIX.set(prefix)
    try:
        yield
    finally:
        _KEY_PREFIX.reset(token)


@contextmanager
def inspector_anchor() -> Generator[None, None, None]:
    """Share one comparison instant across a configuration inspection."""
    token = _INSPECTOR_ANCHOR.set(datetime.now(timezone.utc))
    try:
        yield
    finally:
        _INSPECTOR_ANCHOR.reset(token)


def project_value_context() -> ProjectValueContext:
    """Pass the inspector's source location to the project value parser."""
    directory = _DECLARING_DIR.get()
    return ProjectValueContext(
        Path.cwd() if directory is None else directory,
        prefix=_KEY_PREFIX.get(),
        comparison_anchor=_INSPECTOR_ANCHOR.get(),
    )


__all__ = [
    "declaring_dir",
    "inspector_anchor",
    "key_prefix",
    "project_value_context",
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
]


def render_conflicts(value: Sequence[Any]) -> str:
    """Render every conflict set, or ``<none>``."""
    if not value:
        return "<none>"
    return "; ".join(str(cs) for cs in value)


def render_matrix(value: MatrixConfig | None) -> str:
    """Render shared matrix axes and the values explicitly supplied by each entry."""
    if value is None:
        return "<none>"

    platforms = [platform.label for platform in value.platforms]
    rendered = f"python={value.python}, platforms={platforms}"
    if value.free_threaded is not None:
        rendered += f", free-threaded={_feature_value(value=value.free_threaded)}"
    if bool(value.implementation_overrides) or value.include:
        rendered += f", implementations={list(value.implementations)}"

    if value.implementation_overrides is not None and bool(
        value.implementation_overrides
    ):
        overrides = _implementation_values(value.implementation_overrides)
        rendered += f", overrides={{'implementations': {overrides}}}"
    if value.include:
        rendered += f", include={_matrix_include_values(value.include)}"

    return rendered


def _feature_value(*, value: bool | tuple[bool, ...]) -> bool | list[bool]:
    """Render multiple feature selections as a list."""
    return list(value) if isinstance(value, tuple) else value


def _implementation_values(
    overrides: Mapping[str, ImplementationOverride],
) -> dict[str, dict[str, str | bool | list[str] | list[bool]]]:
    """Render only the axes each implementation explicitly overrides."""
    bodies: dict[str, dict[str, str | bool | list[str] | list[bool]]] = {}
    for implementation, override in overrides.items():
        body: dict[str, str | bool | list[str] | list[bool]] = {}
        if override.python is not None:
            body["python"] = override.python
        if override.platforms is not None:
            body["platforms"] = [platform.label for platform in override.platforms]
        if override.free_threaded is not None:
            body["free-threaded"] = _feature_value(value=override.free_threaded)
        bodies[implementation] = body
    return bodies


def _matrix_include_values(
    entries: Sequence[MatrixInclude],
) -> list[dict[str, str | bool | list[str] | list[bool]]]:
    """Keep omitted axes distinct from explicit values in configuration output."""
    bodies: list[dict[str, str | bool | list[str] | list[bool]]] = []
    for entry in entries:
        body: dict[str, str | bool | list[str] | list[bool]] = {}
        if entry.python is not None:
            body["python"] = entry.python
        if entry.platforms is not None:
            body["platforms"] = [platform.label for platform in entry.platforms]
        if entry.implementations is not None:
            body["implementations"] = list(entry.implementations)
        if entry.free_threaded is not None:
            body["free-threaded"] = _feature_value(value=entry.free_threaded)
        bodies.append(body)
    return bodies


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
    """Render an enum-valued row as its member value."""
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
