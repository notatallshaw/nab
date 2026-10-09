"""Supported resolver import paths and package import behavior."""

from __future__ import annotations

import importlib
import subprocess
import sys
from types import ModuleType

import nab_resolver

_PACKAGE_ROOT_PROBE = """
import sys

import nab_resolver

bound = sorted(name for name in vars(nab_resolver) if not name.startswith("_"))
assert bound == [], f"bound names: {bound}"

loaded = sorted(name for name in sys.modules if name.startswith("nab_resolver."))
assert loaded == [], f"imported submodules: {loaded}"
"""


SUPPORTED_API = {
    "nab_resolver.errors": {
        "ResolutionError",
        "ResolutionInvariantError",
        "ResolutionLimitError",
        "ResolutionStalledError",
        "ResolutionTerminatedError",
    },
    "nab_resolver.ranges": {"Range"},
    "nab_resolver.resolver": {
        "BaseProvider",
        "DEFAULT_MAX_ITERATIONS",
        "Resolver",
        "ResolverObserver",
        "ResolverProvider",
        "Solution",
    },
    "nab_resolver.root": {"ROOT"},
    "nab_resolver.types": {
        "Incompatibility",
        "IncompatibilityCause",
        "RangeProtocol",
        "RootRequirement",
        "Term",
    },
}

# Module exports beyond the supported embedding API.
EXTRA_EXPORTS = {
    "nab_resolver.ranges": {
        "NEGATIVE_INFINITY",
        "POSITIVE_INFINITY",
        "Bound",
        "Interval",
    },
    "nab_resolver.resolver": {"IncompatibilityState", "ResolverStats", "SetRelation"},
    "nab_resolver.types": {
        "IncompatibilityState",
        "PackageType",
        "RangeRelation",
        "RelationProtocol",
        "SetRelation",
        "VersionType",
    },
}


def test_supported_names_are_importable() -> None:
    missing: list[str] = []
    for module, names in sorted(SUPPORTED_API.items()):
        try:
            imported = importlib.import_module(module)
        except ModuleNotFoundError:
            missing.extend(f"{module}.{name}" for name in sorted(names))
            continue
        missing.extend(
            f"{module}.{name}" for name in sorted(names) if not hasattr(imported, name)
        )

    assert missing == [], f"supported but not importable: {missing}"


def test_public_exports_are_accounted_for() -> None:
    supported = set().union(*SUPPORTED_API.values())
    for module, names in SUPPORTED_API.items():
        declared = set(importlib.import_module(module).__all__)
        assert names <= declared
        assert declared - supported == EXTRA_EXPORTS.get(module, set())


def test_package_root_exports_nothing() -> None:
    """Submodules bind themselves onto the package, so only those are allowed."""
    bound = {
        name
        for name, value in vars(nab_resolver).items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    }

    assert bound == set(), f"the package root must re-export nothing, found: {bound}"


def test_package_root_import_binds_no_names_and_loads_no_submodules() -> None:
    """Inspect the root import in a fresh interpreter."""
    subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PACKAGE_ROOT_PROBE], check=True
    )
