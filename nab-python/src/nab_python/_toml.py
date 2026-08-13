"""Shared helpers for reading a pyproject.toml.

Every TOML parse nab does outside the config ladder goes through here, so
the modules that only consume the parsed table never name a TOML library.
"""

from __future__ import annotations

from typing import Any

import tomli


def tool_nab_section(data: dict[str, Any]) -> Any:
    """Return the raw ``[tool.nab]`` value from parsed TOML ``data``.

    Returns ``{}`` when ``[tool]`` is absent or is not a table, so callers
    can chain ``.get`` safely. The value may itself be a non-table when
    ``[tool.nab]`` is malformed; callers that require a table check.
    """
    tool = data.get("tool", {})
    return tool.get("nab", {}) if isinstance(tool, dict) else {}


def parse_pyproject_table(text: str) -> dict[str, Any] | None:
    """Return ``text`` parsed as TOML, or ``None`` when it will not parse.

    A pyproject that does not parse carries no metadata, which is the same
    answer as one that was never fetched, so the failure is a value rather
    than an exception.
    """
    try:
        return tomli.loads(text)
    except tomli.TOMLDecodeError:
        return None
