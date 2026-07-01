"""Shared :class:`~nab_python.config.PackageOverride` builder for tests."""

from __future__ import annotations

from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.utils import canonicalize_name
from nab_python.config import PackageOverride


def pkg_override(req_str: str, **body: object) -> PackageOverride:
    """Build a :class:`PackageOverride` from a PEP 508 requirement string."""
    requirement = Requirement(req_str)
    return PackageOverride(
        requirement=requirement,
        name=canonicalize_name(requirement.name),
        version_range=requirement.specifier.to_range(),
        **body,  # type: ignore[arg-type]
    )
