"""Read and validate PEP 723 metadata without executing the Python source."""

from __future__ import annotations

__lazy_modules__ = (
    "nab_provider.project_requirements",
    "nab_provider._vendor.packaging.specifiers",
    "tokenize",
    "tomli",
)

import re
import tokenize
from typing import TYPE_CHECKING, Any

import tomli

from nab_provider._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from nab_provider.project_requirements import (
    InvalidProjectRequirementError,
    parse_requirements,
    require_string_list,
)

from . import toml_io
from .input_source import ScriptMetadataError
from .value import ValueType

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from nab_provider._vendor.packaging.requirements import Requirement

__all__ = ["ScriptMetadata", "read_script_metadata"]

_BLOCK_HEADER = re.compile(r"/// ([A-Za-z0-9-]+)")


class ScriptMetadata(ValueType):
    """Validated script requirements and its tool configuration."""

    __slots__ = __match_args__ = ("dependencies", "requires_python", "tool")

    dependencies: tuple[Requirement, ...]
    requires_python: str | None
    tool: Mapping[str, Any]

    def __init__(
        self,
        dependencies: tuple[Requirement, ...],
        requires_python: str | None,
        tool: Mapping[str, Any],
    ) -> None:
        """Retain the validated declarations for one metadata read."""
        self.dependencies = dependencies
        self.requires_python = requires_python
        self.tool = tool

    def project_document(self) -> dict[str, Any]:
        """Expose the dependency declaration view shared with project inputs."""
        project: dict[str, Any] = {
            "dependencies": [str(requirement) for requirement in self.dependencies]
        }
        if self.requires_python is not None:
            project["requires-python"] = self.requires_python
        return {"project": project, "tool": dict(self.tool)}


def _comment_sections(text: str) -> Iterator[list[str]]:
    """Yield contiguous PEP 723 comment lines with their prefixes removed."""
    section: list[str] = []
    for line in text.split("\n"):
        if line == "#":
            section.append("")
        elif line.startswith("# "):
            section.append(line[2:])
        elif section:
            yield section
            section = []
    if section:
        yield section


def _script_blocks(text: str) -> Iterator[str]:
    """Extract script blocks, respecting the specification's greedy end marker."""
    for section in _comment_sections(text):
        for start, line in enumerate(section):
            header = _BLOCK_HEADER.fullmatch(line)
            if header is None:
                continue

            end = next(
                (
                    index
                    for index in range(len(section) - 1, start, -1)
                    if section[index] == "///"
                ),
                None,
            )
            if end is not None and header[1] == "script":
                yield "\n".join(section[start + 1 : end])
            break


def read_script_metadata(path: Path) -> ScriptMetadata:
    """Read and validate one PEP 723 block using Python's declared file encoding."""
    try:
        with tokenize.open(path) as stream:
            blocks = list(_script_blocks(stream.read()))
    except (UnicodeError, SyntaxError) as exc:
        message = f"{path}: cannot decode Python script: {exc}"
        raise ScriptMetadataError(message) from exc

    if not blocks:
        message = f"{path}: no complete PEP 723 script metadata block"
        raise ScriptMetadataError(message)
    if len(blocks) > 1:
        message = f"{path}: multiple PEP 723 script metadata blocks"
        raise ScriptMetadataError(message)

    try:
        document = toml_io.loads(blocks[0])
    except tomli.TOMLDecodeError as exc:
        message = f"{path}: invalid TOML in PEP 723 script metadata: {exc}"
        raise ScriptMetadataError(message) from exc

    requires_python = document.get("requires-python")
    if requires_python is not None and not isinstance(requires_python, str):
        message = f"{path}: PEP 723 requires-python must be a string"
        raise ScriptMetadataError(message)
    tool = document.get("tool", {})
    if not isinstance(tool, dict):
        message = f"{path}: PEP 723 tool must be a table"
        raise ScriptMetadataError(message)

    try:
        if requires_python is not None:
            SpecifierSet(requires_python)
        dependencies = parse_requirements(
            require_string_list(document.get("dependencies", []), "dependencies"),
            "dependencies",
        )
    except (
        InvalidSpecifier,
        InvalidProjectRequirementError,
    ) as exc:
        message = f"{path}: invalid PEP 723 script metadata: {exc}"
        raise ScriptMetadataError(message) from exc
    return ScriptMetadata(tuple(dependencies), requires_python, tool)
