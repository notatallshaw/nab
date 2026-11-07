"""Parse user settings and command-line table errors."""

from __future__ import annotations

from pathlib import Path

from nab_project.config_values import SourceConfigError, parse_bool
from nab_project.paths import is_usable_path_name

HTTP_BACKENDS = ("urllib3", "httpx", "httpx2")


__all__ = [
    "CliTableError",
    "SourceConfigError",
    "parse_bool",
    "parse_http_backend",
    "parse_max_concurrency",
    "parse_path",
]


class CliTableError(SourceConfigError):
    """A CLI table-key override that omits the ``config error:`` prefix."""


def parse_path(value: object, where: str) -> Path:
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


def parse_http_backend(value: object, where: str) -> str:
    """Return the ``http-backend`` row's backend name."""
    if not isinstance(value, str):
        msg = f"{where} must be a string, got {type(value).__name__}"
        raise SourceConfigError(msg)
    lowered = value.strip()
    if lowered not in HTTP_BACKENDS:
        msg = f"{where} must be one of {list(HTTP_BACKENDS)!r}, got {value!r}"
        raise SourceConfigError(msg)
    return lowered


def parse_max_concurrency(value: object, where: str) -> int:
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
