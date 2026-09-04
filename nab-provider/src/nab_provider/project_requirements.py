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
    """Append ``extra == "name"`` to a :pep:`508` dep string.

    Parses with :class:`Requirement` rather than splitting on the first
    ``;`` so a semicolon inside a direct-reference URL is not mistaken
    for the marker separator; an existing marker is combined with ``and``.

    ``extra_name`` is a table key interpolated into the quoted marker, so
    it is canonicalised with ``validate=True`` (PEP 685). A key that is
    not a valid name (say one containing a quote) then raises
    :class:`InvalidName` instead of producing a marker for the wrong
    dependency.
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
    """Parse one PEP 508 dependency string, raising if it is malformed.

    An ``extra`` name is folded in as an ``extra == "name"`` marker. A string
    that is not valid PEP 508, or one whose specifier carries a version that
    will not convert, raises :class:`InvalidProjectRequirementError`, so a
    candidate declaring one malformed dependency is rejected whole rather
    than resolved with the dependency silently dropped.
    """
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
    """Validate that a PEP 621 dependency value is an array of strings.

    A bare string passes the type checker as ``Sequence[str]`` but
    iterates character by character, so ``dependencies = "requests"``
    would parse as eight single-character requirements rather than fail.
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{source} must be an array of strings"
        raise InvalidProjectRequirementError(msg)
    return value
