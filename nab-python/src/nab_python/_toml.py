"""Shared helpers for reading the ``[tool.nab]`` section of a pyproject."""

from __future__ import annotations

from typing import Any


def tool_nab_section(data: dict[str, Any]) -> Any:
    """Return the raw ``[tool.nab]`` value from parsed TOML ``data``.

    Returns ``{}`` when ``[tool]`` is absent or is not a table, so callers
    can chain ``.get`` safely. The value may itself be a non-table when
    ``[tool.nab]`` is malformed; callers that require a table check.
    """
    tool = data.get("tool", {})
    return tool.get("nab", {}) if isinstance(tool, dict) else {}
