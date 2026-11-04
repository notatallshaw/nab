"""Select the declaration format read by resolution and lock validation."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from . import toml_io

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["InputFormat", "ScriptMetadataError", "read_input_document"]


class InputFormat(Enum):
    """The declaration format of a local resolution input."""

    PROJECT = "project"
    SCRIPT = "script"


class ScriptMetadataError(ValueError):
    """A script has no usable PEP 723 metadata."""


def read_input_document(
    path: Path, *, input_format: InputFormat = InputFormat.PROJECT
) -> dict[str, Any]:
    """Read declarations in the project-shaped view used by the lock pipeline."""
    if input_format is InputFormat.SCRIPT:
        # The parser imports this module's error type and is needed only for scripts.
        from .script_metadata import read_script_metadata  # noqa: PLC0415

        return read_script_metadata(path).project_document()
    return toml_io.load_path(path)
