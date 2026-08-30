"""Shared helpers for reading a pyproject.toml."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import tomli

from . import toml_io

if TYPE_CHECKING:
    from collections.abc import Mapping


def tool_nab_section(data: Mapping[str, Any]) -> Any:
    """Return the raw ``[tool.nab]`` value from parsed TOML ``data``.

    Returns ``{}`` when ``[tool]`` is absent or is not a table, so callers
    can chain ``.get`` safely. The value may itself be a non-table when
    ``[tool.nab]`` is malformed.
    """
    tool = data.get("tool", {})
    return tool.get("nab", {}) if isinstance(tool, dict) else {}


def parse_pyproject_table(text: str) -> dict[str, Any] | None:
    """Return ``text`` parsed as TOML, or ``None`` when it will not parse.

    A pyproject that does not parse carries no metadata, the same answer as
    one that was never fetched, so the failure is a value not an exception.
    """
    try:
        return toml_io.loads(text)
    except tomli.TOMLDecodeError:
        return None
