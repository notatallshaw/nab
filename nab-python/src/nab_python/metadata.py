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
from typing import TYPE_CHECKING, Any

import tomli

from ._vendor.packaging.requirements import Requirement
from ._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from ._vendor.packaging.version import Version

if TYPE_CHECKING:
    from ._vendor.packaging.markers import Marker

__all__ = [
    "DEPENDENCY_FIELDS",
    "WheelMetadata",
    "intern_version",
    "load_static_project",
    "metadata_deps_are_static",
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

# Metadata-Version 2.2 introduced PEP 643's Dynamic field. Earlier
# formats give no static-deps guarantee.
_MIN_STATIC_METADATA_VERSION = (2, 2)


def metadata_deps_are_static(metadata: WheelMetadata) -> bool:
    """Return True when a distribution's dependency fields are final.

    Per :pep:`643` the values are trustworthy only at Metadata-Version
    2.2 or later with no dependency field marked ``Dynamic``. Below 2.2
    an sdist's declared dependencies may change when it is built.
    """
    if metadata.metadata_version is None:
        return False
    try:
        major, minor = (int(p) for p in metadata.metadata_version.split(".")[:2])
    except ValueError:
        return False
    if (major, minor) < _MIN_STATIC_METADATA_VERSION:
        return False
    return not (DEPENDENCY_FIELDS & metadata.dynamic)


@lru_cache(maxsize=8192)
def _intern_marker(marker: Marker) -> Marker:
    """Return a shared :class:`Marker` for an equal marker expression.

    ``Marker`` hashes and compares by its text, so a single marker like
    ``extra == "test"`` recurs across hundreds of distinct dep strings
    (``pytest; extra == "test"``, ``coverage; extra == "test"``, ...),
    each parsing to its own object.  The provider caches marker
    evaluation by ``id(marker)``, so sharing one object per distinct
    expression lets that cache hit across every candidate instead of
    re-evaluating the same expression per dep.  Markers are read-only,
    so sharing is safe.
    """
    return marker


@lru_cache(maxsize=16384)
def _parse_requirement_cached(req_str: str) -> Requirement:
    """Cache ``Requirement(req_str)`` parsing across wheel metadata.

    The same dep strings (``numpy>=1.26``, ``pydantic<3``, etc.) recur
    across many wheels in a dependency graph.  ``Requirement`` exposes
    only read operations (specifier, marker, extras, name) so sharing
    parsed objects is safe.
    """
    req = Requirement(req_str)
    if req.marker is not None:
        req.marker = _intern_marker(req.marker)
    return req


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
    # RFC 822 makes the whitespace around a header value insignificant.
    name = name.strip()

    version_str = msg.get("Version")
    if version_str is None:
        err = "METADATA missing required Version field"
        raise ValueError(err)

    requires_python_str = msg.get("Requires-Python")
    requires_python = None
    if requires_python_str:
        try:
            requires_python = SpecifierSet(requires_python_str)
        except InvalidSpecifier as exc:
            # A malformed Requires-Python is invalid metadata; raise rather
            # than silently drop the field, matching the Name/Version checks
            # above. The resolve boundary turns this into a rejected candidate.
            err = (
                f"METADATA for {name}=={version_str} has an invalid "
                f"Requires-Python: {requires_python_str!r}"
            )
            raise ValueError(err) from exc

    requires_dist = [
        _parse_requirement_cached(r) for r in msg.get_all("Requires-Dist") or []
    ]

    provides_extra = [e.strip() for e in msg.get_all("Provides-Extra") or []]

    metadata_version = msg.get("Metadata-Version")
    # PEP 643 field names are case-insensitive and, per RFC 822, surrounding
    # whitespace is insignificant; normalise both so downstream membership
    # tests don't depend on the producer's capitalisation or stray spacing.
    dynamic = frozenset(d.strip().lower() for d in msg.get_all("Dynamic") or [])

    return WheelMetadata(
        name=name,
        version=intern_version(version_str),
        requires_python=requires_python,
        requires_dist=requires_dist,
        provides_extra=provides_extra,
        metadata_version=metadata_version,
        dynamic=dynamic,
    )
