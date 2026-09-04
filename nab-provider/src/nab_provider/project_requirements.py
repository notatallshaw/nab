"""Validate dependency strings and arrays from project metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_provider._vendor.packaging.utils import canonicalize_name

from .metadata import validate_specifier_versions
from .pep508 import parse_requirement

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_provider._vendor.packaging.requirements import Requirement

__all__ = [
    "InvalidProjectRequirementError",
    "add_extra_marker",
    "parse_project_requirement",
    "parse_requirements",
    "require_string_list",
]


class InvalidProjectRequirementError(ValueError):
    """A pyproject.toml dependency or metadata value is invalid or unresolvable."""


def add_extra_marker(dep_str: str, extra_name: str) -> str:
    """Append an extra marker, preserving semicolons inside direct-reference URLs.

    Validate and normalize the extra name according to PEP 685.
    """
    req = parse_requirement(dep_str)
    canonical_extra = canonicalize_name(extra_name, validate=True)
    extra_marker = f'extra == "{canonical_extra}"'
    if req.marker is not None:
        marker = f"({req.marker}) and {extra_marker}"
    else:
        marker = extra_marker
    req.marker = None
    return f"{req} ; {marker}"


def parse_project_requirement(
    dep_str: str, source: str, *, extra: str | None = None
) -> Requirement:
    """Parse a dependency and its optional extra marker, rejecting invalid versions."""
    try:
        text = add_extra_marker(dep_str, extra) if extra is not None else dep_str
        req = parse_requirement(text)
        validate_specifier_versions(req.specifier)
    except ValueError as exc:
        msg = f"invalid requirement in {source}: {exc}"
        raise InvalidProjectRequirementError(msg) from exc
    return req


def parse_requirements(strings: Sequence[str], source: str) -> list[Requirement]:
    """Parse PEP 508 strings, naming ``source`` if one is malformed."""
    return [parse_project_requirement(s, source) for s in strings]


def require_string_list(value: object, source: str) -> list[str]:
    """Return a list of dependency strings, rejecting scalar and non-string values."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{source} must be an array of strings"
        raise InvalidProjectRequirementError(msg)
    return value
