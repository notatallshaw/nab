"""Bind the copy of ``packaging`` this package runs on.

Two can be installed. nab vendors a fork at ``nab_provider._vendor.packaging``,
and released ``packaging`` is on PyPI; each is an extra of this distribution.
The fork wins when both are there, so a caller inside nab hands the algebra its
own ``Marker`` objects and catches its own exception classes. With neither,
importing this module raises rather than leaving it half bound.

The floor is checked here as well as declared, because an install that names
no extra declares no dependency at all, and an older copy answers differently
rather than failing. The ceiling is not: a release that moves the private names
below fails loudly on its own, and a hard refusal in code could not be waived.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

BACKENDS = ("nab_provider._vendor.packaging", "packaging")

#: The oldest packaging this suite has been run against; the ``packaging``
#: extra declares the same floor.
MINIMUM = "26.3"

_MISSING = (
    "nab-markersets needs a copy of packaging: install nab-markersets[packaging]"
    " for the released one, or nab-markersets[nab-vendored-packaging] for the"
    " fork nab vendors"
)


def _import_or_none(name: str) -> ModuleType | None:
    """Return the module ``name``, or ``None`` when it does not import."""
    try:
        return import_module(name)
    except ImportError:
        return None


def _import_backend(candidates: Sequence[str]) -> ModuleType:
    """Return the first candidate that imports.

    Only the package is named, so a submodule that fails for a reason of its
    own raises where it failed rather than reading as a missing backend.
    """
    for name in candidates:
        module = _import_or_none(name)
        if module is not None:
            return module
    raise ImportError(_MISSING)


_backend = _import_backend(BACKENDS)

#: Import path of the packaging copy every name below comes from.
BACKEND = _backend.__name__

# Re-exported, so the engine names one module rather than six.
__all__ = [
    "BACKEND",
    "InvalidMarker",
    "InvalidSpecifier",
    "InvalidVersion",
    "Marker",
    "Op",
    "ParserSyntaxError",
    "Specifier",
    "UndefinedComparison",
    "UndefinedEnvironmentName",
    "Value",
    "Variable",
    "Version",
    "_eval_op",
    "canonicalize_name",
    "parse_marker",
]

if TYPE_CHECKING:
    from packaging._parser import Op, Value, Variable, parse_marker
    from packaging._tokenizer import ParserSyntaxError
    from packaging.markers import (
        InvalidMarker,
        Marker,
        UndefinedComparison,
        UndefinedEnvironmentName,
        _eval_op,
    )
    from packaging.specifiers import InvalidSpecifier, Specifier
    from packaging.utils import canonicalize_name
    from packaging.version import InvalidVersion, Version
else:
    _parser = import_module(f"{BACKEND}._parser")
    _tokenizer = import_module(f"{BACKEND}._tokenizer")
    _markers = import_module(f"{BACKEND}.markers")
    _specifiers = import_module(f"{BACKEND}.specifiers")
    _utils = import_module(f"{BACKEND}.utils")
    _version = import_module(f"{BACKEND}.version")

    Op = _parser.Op
    Value = _parser.Value
    Variable = _parser.Variable
    parse_marker = _parser.parse_marker

    ParserSyntaxError = _tokenizer.ParserSyntaxError

    InvalidMarker = _markers.InvalidMarker
    Marker = _markers.Marker
    UndefinedComparison = _markers.UndefinedComparison
    UndefinedEnvironmentName = _markers.UndefinedEnvironmentName
    # packaging publishes no per-atom evaluator, and writing one here would let
    # a marker mean something it does not mean there. It is one of the six
    # private names pyproject.toml lists behind the version range.
    _eval_op = _markers._eval_op  # noqa: SLF001

    InvalidSpecifier = _specifiers.InvalidSpecifier
    Specifier = _specifiers.Specifier

    canonicalize_name = _utils.canonicalize_name

    InvalidVersion = _version.InvalidVersion
    Version = _version.Version


def _require_floor(backend: str, version: str) -> None:
    """Refuse a packaging copy older than :data:`MINIMUM`, or one with no version."""
    try:
        too_old = Version(version) < Version(MINIMUM)
    except InvalidVersion:
        too_old = True
    if too_old:
        msg = (
            f"{backend} is {version or 'unversioned'}; nab-markersets needs"
            f" packaging>={MINIMUM}, which nab-markersets[packaging] installs"
        )
        raise ImportError(msg)


_require_floor(BACKEND, getattr(_backend, "__version__", ""))
