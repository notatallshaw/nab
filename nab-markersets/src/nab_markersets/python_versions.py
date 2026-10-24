"""The Python language versions supported by marker environments and targets."""

from __future__ import annotations

__lazy_modules__ = ("nab_markersets._packaging",)

from typing import TYPE_CHECKING

from ._packaging import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Mapping

_MINOR_PARTS = 2
_FULL_PARTS = 3


def supports_python_version(version: Version, *, variable: str) -> bool:
    """Whether a parsed value belongs to the named Python marker's domain."""
    if version.epoch or version.post is not None:
        return False
    if variable == "python_version":
        return (
            len(version.release) <= _MINOR_PARTS
            and version.pre is None
            and version.dev is None
            and version.local is None
        )
    return len(version.release) <= _FULL_PARTS


def validate_python_version(
    value: str, *, variable: str = "python_full_version"
) -> None:
    """Reject unsupported Python values while accepting abbreviated releases."""
    if supports_python_version(Version(value), variable=variable):
        return
    shape = (
        "major.minor"
        if variable == "python_version"
        else "major.minor.micro with optional prerelease, dev or local suffixes"
    )
    msg = f"{variable} {value!r} must be {shape}, without an epoch or post release"
    raise InvalidVersion(msg)


def validate_python_environment(env: Mapping[str, object]) -> None:
    """Check supplied Python values before substituting them into the algebra."""
    for variable in ("python_version", "python_full_version"):
        value = env.get(variable)
        if isinstance(value, str):
            validate_python_version(value, variable=variable)
