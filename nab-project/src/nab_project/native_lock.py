"""Project lockfile writers for native package managers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._lockfile.native_saved import uv_project_identity
from ._lockfile.pdm import pdm_content_hash, write_pdm_lock
from ._lockfile.poetry import poetry_content_hash, write_poetry_lock
from ._lockfile.uv import write_uv_lock

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "native_project_identity",
    "write_pdm_lock",
    "write_poetry_lock",
    "write_uv_lock",
]


def native_project_identity(format_name: str, document: Mapping[str, Any]) -> str:
    """Return the fingerprint the native writer records for its project."""
    identity = {
        "pdm": pdm_content_hash,
        "poetry": poetry_content_hash,
        "uv": uv_project_identity,
    }
    return identity[format_name](document)
