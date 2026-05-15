"""Minimal METADATA parser for nab-python.

Extracts only the fields needed for dependency resolution from PEP
566/643 METADATA files (RFC 822 format).  Lighter than
:class:`packaging.metadata.Metadata` (no validation pass) and reuses
:class:`packaging.requirements.Requirement` parsing through an
LRU cache so repeated dep strings parse once.
"""

from __future__ import annotations

import email.parser
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import tomli

from ._vendor.packaging.requirements import Requirement
from ._vendor.packaging.specifiers import SpecifierSet
from ._vendor.packaging.version import Version

__all__ = [
    "DEPENDENCY_FIELDS",
    "WheelMetadata",
    "intern_version",
    "load_static_project",
    "parse_metadata",
]


# ``[project].dynamic`` keys that disqualify the static reader.
# When either appears the build backend may override the declared
# values, so PEP 621 does not guarantee the table is authoritative.
_DYNAMIC_FIELD_BLOCKERS = frozenset({"dependencies", "optional-dependencies"})


def load_static_project(text: str) -> dict[str, Any] | None:
    """Return the ``[project]`` table when it can be trusted as static.

    Returns ``None`` when the TOML cannot be parsed, the
    ``[project]`` table is missing or malformed, or
    ``project.dynamic`` includes ``dependencies`` /
    ``optional-dependencies`` (in which case the static reader can
    not provide either).
    """
    try:
        data = tomli.loads(text)
    except tomli.TOMLDecodeError:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    dynamic_raw = project.get("dynamic")
    if isinstance(dynamic_raw, list):
        dynamic_set = {d for d in dynamic_raw if isinstance(d, str)}
        if _DYNAMIC_FIELD_BLOCKERS & dynamic_set:
            return None
    return project


# PEP 643 dependency-affecting METADATA fields, lowercased.
# Intersect with WheelMetadata.dynamic to detect wheels whose dep
# declarations may change at build time.
DEPENDENCY_FIELDS = frozenset({"requires-dist", "provides-extra"})


@lru_cache(maxsize=16384)
def _parse_requirement_cached(req_str: str) -> Requirement:
    """Cache ``Requirement(req_str)`` parsing across wheel metadata.

    The same dep strings (``numpy>=1.26``, ``pydantic<3``, etc.) recur
    across many wheels in a dependency graph.  ``Requirement`` exposes
    only read operations (specifier, marker, extras, name) so sharing
    parsed objects is safe.
    """
    return Requirement(req_str)


@lru_cache(maxsize=65536)
def intern_version(version_str: str) -> Version:
    """Return a shared :class:`Version` for ``version_str``.

    The same version string recurs across the per-platform wheels of a
    project (and across projects that publish the same number).  Sharing
    the parsed object saves the PEP 440 regex walk on every duplicate.
    ``Version`` is immutable in ``packaging``, so the shared instance
    is safe.
    """
    return Version(version_str)


@dataclass
class WheelMetadata:
    """Parsed fields from a wheel's METADATA file."""

    name: str
    version: Version
    requires_python: SpecifierSet | None = None
    requires_dist: list[Requirement] = field(default_factory=list)
    provides_extra: list[str] = field(default_factory=list)
    metadata_version: str | None = None
    dynamic: frozenset[str] = field(default_factory=frozenset)


def parse_metadata(data: str | bytes) -> WheelMetadata:
    """Parse a METADATA file and return the fields needed for resolution."""
    if isinstance(data, bytes):
        data = data.decode("utf-8")

    msg = email.parser.Parser().parsestr(data)

    name = msg.get("Name")
    if name is None:
        err = "METADATA missing required Name field"
        raise ValueError(err)

    version_str = msg.get("Version")
    if version_str is None:
        err = "METADATA missing required Version field"
        raise ValueError(err)

    requires_python_str = msg.get("Requires-Python")
    requires_python = SpecifierSet(requires_python_str) if requires_python_str else None

    requires_dist = [
        _parse_requirement_cached(r) for r in msg.get_all("Requires-Dist") or []
    ]

    provides_extra = list(msg.get_all("Provides-Extra") or [])

    metadata_version = msg.get("Metadata-Version")
    # PEP 643 field names are case-insensitive per RFC 822; normalise so
    # downstream lookups don't depend on the producer's capitalisation.
    dynamic = frozenset(d.lower() for d in msg.get_all("Dynamic") or [])

    return WheelMetadata(
        name=name,
        version=intern_version(version_str),
        requires_python=requires_python,
        requires_dist=requires_dist,
        provides_extra=provides_extra,
        metadata_version=metadata_version,
        dynamic=dynamic,
    )
