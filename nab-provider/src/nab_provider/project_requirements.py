"""Validate dependency strings and arrays from project metadata."""

from __future__ import annotations

__lazy_modules__ = (
    "nab_provider._vendor.packaging.specifiers",
    "nab_provider._vendor.packaging.utils",
    "nab_provider.metadata",
    "nab_provider.pep508",
)

from typing import TYPE_CHECKING

from ._vendor.packaging.specifiers import SpecifierSet
from ._vendor.packaging.utils import canonicalize_name
from .metadata import validate_specifier_versions
from .pep508 import parse_requirement

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ._vendor.packaging.requirements import Requirement

__all__ = [
    "InvalidProjectRequirementError",
    "add_extra_marker",
    "parse_project_requirement",
    "parse_project_requires_python",
    "parse_requirements",
    "project_with_static_requires_python",
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


def project_with_static_requires_python(
    data: Mapping[str, object] | None, package: str
) -> Mapping[str, object] | None:
    """Return the project table if it fixes the package's Python requirement.

    An omitted requires-python means any Python only when the backend is not
    responsible for that field.
    """
    project = data.get("project") if data is not None else None
    if not isinstance(project, dict):
        return None
    name = project.get("name")
    if not isinstance(name, str) or canonicalize_name(name) != canonicalize_name(
        package
    ):
        return None

    dynamic = project.get("dynamic", [])
    if (
        not isinstance(dynamic, list)
        or not all(isinstance(field, str) for field in dynamic)
        or "requires-python" in dynamic
    ):
        return None
    return project


def parse_project_requires_python(raw: object) -> SpecifierSet | None:
    """Return the declared Python requirement, or None when the field is absent.

    Invalid specifiers raise InvalidProjectRequirementError.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        msg = f"[project].requires-python must be a string, got {type(raw).__name__}"
        raise InvalidProjectRequirementError(msg)
    try:
        specifier_set = SpecifierSet(raw)
        validate_specifier_versions(specifier_set)
    except ValueError as exc:
        msg = f"invalid [project].requires-python {raw!r}: {exc}"
        raise InvalidProjectRequirementError(msg) from exc
    return specifier_set


def parse_requirements(strings: Sequence[str], source: str) -> list[Requirement]:
    """Parse PEP 508 strings, naming ``source`` if one is malformed."""
    return [parse_project_requirement(s, source) for s in strings]


def require_string_list(value: object, source: str) -> list[str]:
    """Return a list of dependency strings, rejecting scalar and non-string values."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{source} must be an array of strings"
        raise InvalidProjectRequirementError(msg)
    return value
