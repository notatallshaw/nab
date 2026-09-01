"""Shared Hypothesis strategies for nab-project property tests.

These strategies generate the artifacts the PEP 440-flavored property
tests exercise:

* ``pep440_specifiers``: random ``SpecifierSet`` from operator/version
  pairs.
* ``packaging_graphs``: random dependency graph using
  ``packaging.version.Version`` and ``packaging.specifiers.SpecifierSet``.
* ``small_packaging_graphs``: a smaller variant for brute-force
  comparison.
* ``canonical_names``/``versions``/``sha256s``: primitives for the
  PEP 751 lockfile property tests.
* ``range_clauses``/``range_specs``: specifier text over a version pool
  spanning pre-release, post, local, and epoch shapes, for the
  ``VersionRange`` differential tests.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st

from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

PACKAGE_NAMES = [f"pkg{i}" for i in range(10)]

VERSION_POOL = [
    Version(f"{major}.{minor}") for major in range(1, 6) for minor in range(3)
]

_DEEP = os.environ.get("HYPOTHESIS_PROFILE") == "deep"

PROPERTY_SETTINGS = settings(
    max_examples=1000 if _DEEP else 100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
"""Default settings for fast properties.

The ``deep`` profile (env ``HYPOTHESIS_PROFILE=deep``) bumps
``max_examples`` to 1000 so a long offline run can hunt for
counter-examples that the default 100-example budget would miss.
"""

DEEP_SETTINGS = settings(
    max_examples=3000 if _DEEP else 300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
"""Heavier settings for properties that benefit from more examples."""

BRUTE_FORCE_SETTINGS = settings(
    max_examples=200 if _DEEP else 30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
"""Settings for properties that compare against brute-force enumeration."""

MAX_BRUTE_FORCE_COMBINATIONS = 10_000

LINUX_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)
"""The CPython 3.11 linux_x86_64 target the property fixtures resolve against."""


# PEP 503 forbids a trailing separator in canonical names; the regex
# enforces that names start with a letter and end alphanumeric.
canonical_names = st.from_regex(
    r"[a-z][a-z0-9]{0,3}(-[a-z0-9]+)*",
    fullmatch=True,
)
versions = st.sampled_from(["0.1", "1.0", "1.0.0", "1.2.3", "2.0.0", "9.9"])
sha256s = st.from_regex(r"[0-9a-f]{64}", fullmatch=True)


@st.composite
def pep440_specifiers(draw: st.DrawFn) -> SpecifierSet:
    """Generate a random PEP 440 specifier set.

    Picks from operators that the intervals API handles; avoids
    ``===`` (arbitrary equality) since it has no interval
    representation.
    """
    num_specs = draw(st.integers(min_value=0, max_value=2))
    if num_specs == 0:
        return SpecifierSet()

    parts: list[str] = []
    for _ in range(num_specs):
        op = draw(st.sampled_from([">=", "<=", ">", "<", "==", "!="]))
        version = draw(st.sampled_from(VERSION_POOL))
        parts.append(f"{op}{version}")
    try:
        return SpecifierSet(",".join(parts))
    except Exception:  # noqa: BLE001
        return SpecifierSet()


@st.composite
def non_empty_specifiers(draw: st.DrawFn) -> SpecifierSet:
    """Generate a ``SpecifierSet`` that admits at least one version.

    Falls back to the empty ``SpecifierSet`` (which admits everything)
    if Hypothesis happens to draw an obviously-unsatisfiable
    combination.
    """
    spec = draw(pep440_specifiers())
    if spec.is_unsatisfiable():
        return SpecifierSet()
    return spec


@st.composite
def packaging_graphs(
    draw: st.DrawFn,
) -> dict[str, dict[Version, dict[str, SpecifierSet]]]:
    """Generate a random dependency graph with PEP 440 types."""
    num_packages = draw(st.integers(min_value=2, max_value=6))
    packages = PACKAGE_NAMES[:num_packages]

    graph: dict[str, dict[Version, dict[str, SpecifierSet]]] = {}

    for package in packages:
        num_versions = draw(st.integers(min_value=1, max_value=4))
        package_versions_list = draw(
            st.lists(
                st.sampled_from(VERSION_POOL),
                min_size=num_versions,
                max_size=num_versions,
                unique=True,
            )
        )
        package_versions: dict[Version, dict[str, SpecifierSet]] = {}
        for version in package_versions_list:
            num_deps = draw(st.integers(min_value=0, max_value=2))
            deps: dict[str, SpecifierSet] = {}
            other = [p for p in packages if p != package]
            if num_deps > 0 and other:
                dep_packages = draw(
                    st.lists(
                        st.sampled_from(other),
                        min_size=min(num_deps, len(other)),
                        max_size=min(num_deps, len(other)),
                        unique=True,
                    )
                )
                for dep in dep_packages:
                    deps[dep] = draw(non_empty_specifiers())
            package_versions[version] = deps
        graph[package] = package_versions

    root_dep_count = draw(st.integers(min_value=1, max_value=min(3, len(packages))))
    root_dep_packages = draw(
        st.lists(
            st.sampled_from(packages),
            min_size=root_dep_count,
            max_size=root_dep_count,
            unique=True,
        )
    )
    root_deps: dict[str, SpecifierSet] = {
        dep: draw(non_empty_specifiers()) for dep in root_dep_packages
    }
    graph["root"] = {Version("1.0"): root_deps}

    return graph


@st.composite
def small_packaging_graphs(
    draw: st.DrawFn,
) -> dict[str, dict[Version, dict[str, SpecifierSet]]]:
    """Generate tiny PEP 440 graphs (2-3 packages) for brute-force comparison."""
    num_packages = draw(st.integers(min_value=2, max_value=3))
    packages = PACKAGE_NAMES[:num_packages]
    small_pool = VERSION_POOL[:6]

    graph: dict[str, dict[Version, dict[str, SpecifierSet]]] = {}
    for package in packages:
        num_versions = draw(st.integers(min_value=1, max_value=3))
        package_versions_list = draw(
            st.lists(
                st.sampled_from(small_pool),
                min_size=num_versions,
                max_size=num_versions,
                unique=True,
            )
        )
        package_versions: dict[Version, dict[str, SpecifierSet]] = {}
        for version in package_versions_list:
            num_deps = draw(st.integers(min_value=0, max_value=1))
            deps: dict[str, SpecifierSet] = {}
            other = [p for p in packages if p != package]
            if num_deps > 0 and other:
                dep = draw(st.sampled_from(other))
                deps[dep] = draw(non_empty_specifiers())
            package_versions[version] = deps
        graph[package] = package_versions

    root_deps: dict[str, SpecifierSet] = {
        dep: draw(non_empty_specifiers())
        for dep in packages[
            : draw(st.integers(min_value=1, max_value=min(2, len(packages))))
        ]
    }
    graph["root"] = {Version("1.0"): root_deps}

    return graph


# A version pool wide enough that random bounds over it land on every
# interesting shape: pre-release, dev, post, local, and a second epoch.
RANGE_VERSION_POOL = [
    "0.9",
    "1.0.dev1",
    "1.0a1",
    "1.0rc1",
    "1.0",
    "1.0+local",
    "1.0.post1",
    "1.4",
    "1.5",
    "1.8",
    "1.9.9",
    "2.0",
    "3.0",
    "1!0.5",
    "1!1.0",
]

_ORDERED_OPERATORS = [">=", "<=", ">", "<"]
_EQUALITY_OPERATORS = ["==", "!="]


@st.composite
def range_clauses(draw: st.DrawFn) -> str:
    """One specifier clause over ``RANGE_VERSION_POOL``.

    Local versions and ``.*`` wildcards are only legal on some operators,
    so each operator is paired with versions it accepts.
    """
    kind = draw(st.integers(min_value=0, max_value=3))
    if kind == 0:
        operator = draw(st.sampled_from(_ORDERED_OPERATORS))
        version = draw(st.sampled_from([v for v in RANGE_VERSION_POOL if "+" not in v]))
        return f"{operator}{version}"
    if kind == 1:
        operator = draw(st.sampled_from(_EQUALITY_OPERATORS))
        return f"{operator}{draw(st.sampled_from(RANGE_VERSION_POOL))}"
    if kind == 2:
        operator = draw(st.sampled_from(_EQUALITY_OPERATORS))
        base = draw(st.sampled_from(["1", "1.0", "2", "1!1"]))
        return f"{operator}{base}.*"
    return f"~={draw(st.sampled_from(['1.4', '1.0.0', '2.0']))}"


@st.composite
def range_specs(draw: st.DrawFn) -> str:
    """A specifier set of zero to three clauses, or a ``===`` literal."""
    if draw(st.integers(min_value=0, max_value=7)) == 0:
        literal = draw(st.sampled_from([*RANGE_VERSION_POOL, "frobnicate"]))
        return f"==={literal}"
    return ",".join(draw(st.lists(range_clauses(), min_size=0, max_size=3)))
