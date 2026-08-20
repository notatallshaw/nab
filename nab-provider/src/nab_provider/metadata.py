"""Minimal METADATA parser.

Extracts only the fields needed for dependency resolution from PEP
566/643 METADATA files (RFC 822 format).  Lighter than
:class:`packaging.metadata.Metadata` (no validation pass) and reuses
:class:`packaging.requirements.Requirement` parsing through an
LRU cache so repeated dep strings parse once.
"""

from __future__ import annotations

import email.parser
from contextlib import suppress
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nab_provider._vendor.packaging.markers import Marker

__all__ = [
    "DEPENDENCY_FIELDS",
    "WheelMetadata",
    "intern_version",
    "metadata_deps_are_static",
    "metadata_header_block",
    "parse_metadata",
    "static_project_from_table",
    "validate_specifier_versions",
]


# ``[project].dynamic`` keys that disqualify the static reader.
# When either appears the build backend may override the declared
# values, so PEP 621 does not guarantee the table is authoritative.
_DYNAMIC_FIELD_BLOCKERS = frozenset({"dependencies", "optional-dependencies"})


def static_project_from_table(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return ``data``'s ``[project]`` table when it can be trusted as static.

    Returns ``None`` when the ``[project]`` table is missing or
    malformed, or ``project.dynamic`` includes ``dependencies`` /
    ``optional-dependencies`` (in which case the static reader can
    not provide either).
    """
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    dynamic_raw = project.get("dynamic")
    if isinstance(dynamic_raw, list):
        dynamic_set = {d for d in dynamic_raw if isinstance(d, str)}
        if _DYNAMIC_FIELD_BLOCKERS & dynamic_set:
            return None
    return project


def validate_specifier_versions(specifier_set: SpecifierSet) -> None:
    """Convert every clause's version, raising when one will not parse.

    A :class:`SpecifierSet` keeps its clause versions as strings and
    converts them only when something compares against it, so a digit run
    past CPython's int-from-string limit is accepted here and raises a
    bare ``ValueError`` from the comparison much later.  An arbitrary
    equality (``===``) literal need not be a PEP 440 version, so only a
    failure other than :class:`InvalidVersion` rejects one.
    """
    for clause in specifier_set:
        if clause.operator == "===":
            with suppress(InvalidVersion):
                Version(clause.version)
        else:
            Version(clause.version.removesuffix(".*"))


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

    ``Marker`` hashes and compares by its text, so one expression like
    ``extra == "test"`` parses to a separate object in each of the
    hundreds of dep strings that carry it.  Sharing one object per
    expression lets a cache keyed on ``id(marker)`` hit across
    candidates.  Markers are read-only, so sharing is safe.
    """
    return marker


@lru_cache(maxsize=16384)
def _parse_requirement_cached(req_str: str) -> Requirement:
    """Cache ``Requirement(req_str)`` parsing across wheel metadata.

    The same dep strings (``numpy>=1.26``, ``pydantic<3``, etc.) recur
    across many wheels in a dependency graph.  ``Requirement`` exposes
    only read operations, so sharing parsed objects is safe.

    Raises ``ValueError`` when the string does not parse or a clause
    version will not convert.
    """
    req = Requirement(req_str)
    validate_specifier_versions(req.specifier)
    if req.marker is not None:
        req.marker = _intern_marker(req.marker)
    return req


@lru_cache(maxsize=65536)
def intern_version(version_str: str) -> Version:
    """Return a shared :class:`Version` for ``version_str``.

    The same version string recurs across a project's per-platform
    wheels, so sharing the parsed object saves the PEP 440 regex walk on
    every duplicate.  ``Version`` is immutable in ``packaging``, so the
    shared instance is safe.
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


def metadata_header_block(text: str) -> str:
    r"""Return ``text`` cut after the earlier of its first ``\n\n`` and ``\r\n\r\n``.

    ``email.parser`` closes the RFC 822 headers at the first blank line at the
    latest, so the prefix holds every field :func:`parse_metadata` reads and
    parses to the same :class:`WheelMetadata` as the whole document.  A
    document with neither sequence comes back whole.

    ``lf + 3`` bounds the second search: it is as far as a ``\r\n\r\n``
    starting before the ``\n\n`` hit can reach.
    """
    lf = text.find("\n\n")
    crlf = text.find("\r\n\r\n", 0, None if lf == -1 else lf + 3)
    if crlf != -1:
        return text[: crlf + 4]
    if lf != -1:
        return text[: lf + 2]
    return text


def parse_metadata(data: str | bytes) -> WheelMetadata:
    """Parse a METADATA file and return the fields needed for resolution."""
    if isinstance(data, bytes):
        data = data.decode("utf-8")

    # Nothing here reads the long description.
    msg = email.parser.Parser().parsestr(metadata_header_block(data), headersonly=True)

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
            validate_specifier_versions(requires_python)
        except ValueError as exc:
            # A malformed Requires-Python is invalid metadata; raise rather
            # than silently drop the field.
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
    # PEP 643 field names are case-insensitive, and RFC 822 makes surrounding
    # whitespace insignificant.
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
