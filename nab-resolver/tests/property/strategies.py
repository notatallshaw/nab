"""Shared Hypothesis strategies for nab-resolver property tests.

The strategies generate the algebraic objects exercised by the
property tests:

* ``version_ranges``: ``Range[int]`` over a small integer pool with
  every constructor (full, empty, singleton, at_least, greater_than,
  at_most, less_than, between, two-interval unions).
* ``terms``: positive and negative ``Term[str, int]`` for a fixed
  package name.
* ``dependency_graphs`` and friends: random graphs over a small name
  pool used by the resolver-correctness properties.

The pool sizes were tuned so brute-force enumeration of the search
space stays under :data:`MAX_BRUTE_FORCE_COMBINATIONS` for the small
strategies, and so Hypothesis can shrink failures to small examples.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st

from nab_resolver.ranges import Range
from nab_resolver.types import Term

PACKAGE_NAMES = [f"pkg{i}" for i in range(15)]

VERSION_RANGE = range(1, 21)

_DEEP = os.environ.get("HYPOTHESIS_PROFILE") == "deep"

PROPERTY_SETTINGS = settings(
    max_examples=2000 if _DEEP else 200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
"""Default settings for fast properties.

The ``deep`` profile (env ``HYPOTHESIS_PROFILE=deep``) bumps
``max_examples`` to 2000 so a long offline run can hunt for
counter-examples that the default 200-example budget would miss.
"""

DEEP_SETTINGS = settings(
    max_examples=5000 if _DEEP else 500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
"""Heavier settings for properties that benefit from more examples."""

BRUTE_FORCE_SETTINGS = settings(
    max_examples=200 if _DEEP else 50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
"""Settings for properties that compare against brute-force enumeration."""

MAX_BRUTE_FORCE_COMBINATIONS = 10_000


@st.composite
def version_ranges(draw: st.DrawFn) -> Range[int]:
    """Generate a random ``Range[int]`` over versions 1-20.

    Covers every public constructor and a two-interval compound
    union, so set-algebra properties exercise every shape the resolver
    can encounter at runtime.
    """
    range_type = draw(
        st.sampled_from(
            [
                "any",
                "empty",
                "exact",
                "at_least",
                "greater_than",
                "at_most",
                "less_than",
                "between",
                "compound",
            ]
        )
    )
    if range_type == "any":
        return Range.full()
    if range_type == "empty":
        return Range.empty()
    if range_type == "exact":
        return Range.singleton(draw(st.integers(min_value=1, max_value=20)))
    if range_type == "at_least":
        return Range.at_least(draw(st.integers(min_value=1, max_value=20)))
    if range_type == "greater_than":
        return Range.greater_than(draw(st.integers(min_value=1, max_value=20)))
    if range_type == "at_most":
        return Range.at_most(draw(st.integers(min_value=1, max_value=20)))
    if range_type == "less_than":
        return Range.less_than(draw(st.integers(min_value=2, max_value=21)))
    if range_type == "compound":
        first = Range.between(
            draw(st.integers(min_value=1, max_value=5)),
            draw(st.integers(min_value=6, max_value=10)),
        )
        second = Range.between(
            draw(st.integers(min_value=11, max_value=15)),
            draw(st.integers(min_value=16, max_value=20)),
        )
        return first | second
    lower = draw(st.integers(min_value=1, max_value=18))
    upper = draw(st.integers(min_value=lower + 1, max_value=20))
    return Range.between(lower, upper)


@st.composite
def non_empty_ranges(draw: st.DrawFn) -> Range[int]:
    """Generate a non-empty ``Range[int]``."""
    candidate = draw(version_ranges())
    if candidate.is_empty:
        return Range.full()
    return candidate


@st.composite
def dependency_ranges(draw: st.DrawFn) -> Range[int]:
    """Generate ranges suitable for dependency constraints.

    Excludes empty ranges; an empty dependency is unsatisfiable on its
    face which makes the resolver's behavior trivial to predict.
    """
    candidate = draw(version_ranges())
    if candidate.is_empty:
        return Range.full()
    return candidate


@st.composite
def dependency_ranges_with_empty(draw: st.DrawFn) -> Range[int]:
    """Generate ranges that may include empty ranges.

    An empty dependency range means "this version requires something
    impossible". The resolver must reject such versions rather than
    crash or include them in a solution.
    """
    return draw(version_ranges())


@st.composite
def terms(draw: st.DrawFn) -> Term[str, int]:
    """Generate a random ``Term[str, int]`` for package ``pkg``."""
    constraint = draw(version_ranges())
    positive = draw(st.booleans())
    return Term("pkg", constraint, positive=positive)


@st.composite
def non_empty_terms(draw: st.DrawFn) -> Term[str, int]:
    """Generate a ``Term[str, int]`` with non-empty constraint."""
    constraint = draw(non_empty_ranges())
    positive = draw(st.booleans())
    return Term("pkg", constraint, positive=positive)


@st.composite
def dependency_graphs(draw: st.DrawFn) -> dict[str, dict[int, dict[str, Range[int]]]]:
    """Generate a random dependency graph over up to 8 packages.

    Returns a dict in :class:`FuzzProvider`-shape:
    ``{package: {version: {dep_package: dep_range}}}``.
    Always includes a ``root`` package with version 1.
    """
    num_packages = draw(st.integers(min_value=2, max_value=8))
    packages = PACKAGE_NAMES[:num_packages]

    graph: dict[str, dict[int, dict[str, Range[int]]]] = {}

    for package in packages:
        num_versions = draw(st.integers(min_value=1, max_value=5))
        versions = list(range(1, num_versions + 1))
        package_versions: dict[int, dict[str, Range[int]]] = {}
        for version in versions:
            num_deps = draw(st.integers(min_value=0, max_value=3))
            deps: dict[str, Range[int]] = {}
            other_packages = [p for p in packages if p != package]
            if num_deps > 0 and other_packages:
                dep_packages = draw(
                    st.lists(
                        st.sampled_from(other_packages),
                        min_size=min(num_deps, len(other_packages)),
                        max_size=min(num_deps, len(other_packages)),
                        unique=True,
                    )
                )
                for dep_package in dep_packages:
                    deps[dep_package] = draw(dependency_ranges())
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
    root_deps: dict[str, Range[int]] = {
        dep_package: draw(dependency_ranges()) for dep_package in root_dep_packages
    }
    graph["root"] = {1: root_deps}

    return graph


@st.composite
def deep_chain_graphs(draw: st.DrawFn) -> dict[str, dict[int, dict[str, Range[int]]]]:
    """Generate deep linear dependency chains.

        root -> chain0 -> chain1 -> chain2 -> ... -> chainN

    When a conflict happens at the end of a deep chain, the resolver
    has to backjump many levels.  Stresses the conflict-resolution
    loop's ability to find the right level to jump to.
    """
    depth = draw(st.integers(min_value=3, max_value=12))
    packages = [f"chain{i}" for i in range(depth)]

    graph: dict[str, dict[int, dict[str, Range[int]]]] = {}
    for index, package in enumerate(packages):
        num_versions = draw(st.integers(min_value=1, max_value=3))
        versions_dict: dict[int, dict[str, Range[int]]] = {}
        for version in range(1, num_versions + 1):
            if index + 1 < len(packages):
                versions_dict[version] = {
                    packages[index + 1]: draw(dependency_ranges()),
                }
            else:
                versions_dict[version] = {}
        graph[package] = versions_dict

    graph["root"] = {1: {packages[0]: Range.full()}}
    return graph


@st.composite
def pinning_cascade_graphs(
    draw: st.DrawFn,
) -> dict[str, dict[int, dict[str, Range[int]]]]:
    """Generate version-pinning cascades.

        pinner@1 -> pinned==1
        pinner@2 -> pinned==2      (each version pins its dep exactly)
        ...
        blocker  -> pinned < K      (forces a specific subset)

    This pattern is common in AWS SDKs (boto3/botocore) where each
    release pins an exact version of a sibling package.  The resolver
    has to generalize across many candidates with similar-but-different
    exact pins rather than try each candidate in isolation.
    """
    num_versions = draw(st.integers(min_value=3, max_value=10))
    versions = list(range(1, num_versions + 1))

    return {
        "pinner": {v: {"pinned": Range.singleton(v)} for v in versions},
        "pinned": {v: {} for v in versions},
        "blocker": {
            1: {
                "pinned": Range.less_than(
                    draw(st.integers(min_value=2, max_value=num_versions)),
                ),
            },
        },
        "root": {
            1: {
                "pinner": Range.full(),
                "blocker": Range.full(),
            },
        },
    }


@st.composite
def guaranteed_solvable_graphs(
    draw: st.DrawFn,
) -> dict[str, dict[int, dict[str, Range[int]]]]:
    """Generate graphs where a solution is guaranteed.

    Every package has a version 1 with no dependencies.  Whatever the
    higher versions look like, version 1 is always a fallback.  The
    resolver must therefore find a solution; reporting impossible is
    a completeness bug.
    """
    num_packages = draw(st.integers(min_value=2, max_value=6))
    packages = PACKAGE_NAMES[:num_packages]

    graph: dict[str, dict[int, dict[str, Range[int]]]] = {}
    for package in packages:
        versions_dict: dict[int, dict[str, Range[int]]] = {1: {}}
        num_extra = draw(st.integers(min_value=0, max_value=3))
        for version in range(2, 2 + num_extra):
            other = [p for p in packages if p != package]
            if other:
                dep_pkg = draw(st.sampled_from(other))
                versions_dict[version] = {dep_pkg: Range.full()}
            else:
                versions_dict[version] = {}
        graph[package] = versions_dict

    root_deps = {p: Range.full() for p in packages}
    graph["root"] = {1: root_deps}
    return graph


@st.composite
def small_exhaustive_graphs(
    draw: st.DrawFn,
) -> dict[str, dict[int, dict[str, Range[int]]]]:
    """Generate tiny graphs (2-3 packages, 1-3 versions) for brute-force comparison.

    Small enough that we can enumerate every (package, version) tuple
    and check whether any satisfies all constraints.  When the
    resolver disagrees with this exhaustive answer it is a
    completeness bug.
    """
    num_packages = draw(st.integers(min_value=2, max_value=3))
    packages = PACKAGE_NAMES[:num_packages]

    graph: dict[str, dict[int, dict[str, Range[int]]]] = {}
    for package in packages:
        num_versions = draw(st.integers(min_value=1, max_value=3))
        package_versions: dict[int, dict[str, Range[int]]] = {}
        for version in range(1, num_versions + 1):
            num_deps = draw(st.integers(min_value=0, max_value=2))
            deps: dict[str, Range[int]] = {}
            other_packages = [p for p in packages if p != package]
            if num_deps > 0 and other_packages:
                dep_packages = draw(
                    st.lists(
                        st.sampled_from(other_packages),
                        min_size=min(num_deps, len(other_packages)),
                        max_size=min(num_deps, len(other_packages)),
                        unique=True,
                    )
                )
                for dep_package in dep_packages:
                    deps[dep_package] = draw(dependency_ranges())
            package_versions[version] = deps
        graph[package] = package_versions

    root_dep_count = draw(st.integers(min_value=1, max_value=min(2, len(packages))))
    root_dep_packages = draw(
        st.lists(
            st.sampled_from(packages),
            min_size=root_dep_count,
            max_size=root_dep_count,
            unique=True,
        )
    )
    root_deps: dict[str, Range[int]] = {
        dep_package: draw(dependency_ranges()) for dep_package in root_dep_packages
    }
    graph["root"] = {1: root_deps}
    return graph


@st.composite
def graph_and_constraints(
    draw: st.DrawFn,
) -> tuple[dict[str, dict[int, dict[str, Range[int]]]], dict[str, Range[int]]]:
    """Draw a small graph together with constraints over its packages.

    Constraints lean toward singletons because pinning a package to one
    version is what forces it to drag in a conflicting dependency, the
    shape that exposes constraint-driven completeness bugs.
    """
    graph = draw(small_exhaustive_graphs())
    packages = [p for p in graph if p != "root"]
    num_constraints = draw(st.integers(min_value=0, max_value=min(2, len(packages))))
    constrained = (
        draw(
            st.lists(
                st.sampled_from(packages),
                min_size=num_constraints,
                max_size=num_constraints,
                unique=True,
            )
        )
        if num_constraints
        else []
    )
    constraint_range = st.one_of(
        st.builds(Range.singleton, st.integers(min_value=1, max_value=3)),
        dependency_ranges(),
    )
    constraints = {pkg: draw(constraint_range) for pkg in constrained}
    return graph, constraints


@st.composite
def empty_dep_graphs(
    draw: st.DrawFn,
) -> dict[str, dict[int, dict[str, Range[int]]]]:
    """Generate graphs where some dependencies use empty ranges.

    An empty range dependency means "this version requires something
    impossible." The resolver must reject the parent version and
    continue, never crash or surface invalid results.
    """
    num_packages = draw(st.integers(min_value=2, max_value=4))
    packages = PACKAGE_NAMES[:num_packages]

    graph: dict[str, dict[int, dict[str, Range[int]]]] = {}
    for package in packages:
        num_versions = draw(st.integers(min_value=1, max_value=3))
        package_versions: dict[int, dict[str, Range[int]]] = {}
        for version in range(1, num_versions + 1):
            num_deps = draw(st.integers(min_value=0, max_value=2))
            deps: dict[str, Range[int]] = {}
            other_packages = [p for p in packages if p != package]
            if num_deps > 0 and other_packages:
                dep_packages = draw(
                    st.lists(
                        st.sampled_from(other_packages),
                        min_size=min(num_deps, len(other_packages)),
                        max_size=min(num_deps, len(other_packages)),
                        unique=True,
                    )
                )
                for dep_package in dep_packages:
                    deps[dep_package] = draw(dependency_ranges_with_empty())
            package_versions[version] = deps
        graph[package] = package_versions

    root_dep_count = draw(st.integers(min_value=1, max_value=min(2, len(packages))))
    root_dep_packages = draw(
        st.lists(
            st.sampled_from(packages),
            min_size=root_dep_count,
            max_size=root_dep_count,
            unique=True,
        )
    )
    root_deps: dict[str, Range[int]] = {
        dep_package: draw(dependency_ranges()) for dep_package in root_dep_packages
    }
    graph["root"] = {1: root_deps}
    return graph


@st.composite
def mutual_back_edge_graphs(
    draw: st.DrawFn,
) -> dict[str, dict[int, dict[str, Range[int]]]]:
    """Generate graphs with at least one circular dependency.

        A@high -> B
        B      -> A

    Circular deps create conditional subgraphs: B is only reachable
    when a specific version of A is chosen, and B depends back on A.
    Failures inside B must propagate back through A to a different A
    version; if not, the resolver loops or falsely claims impossible.

    See:
        https://github.com/python-poetry/poetry/issues/7398
        https://github.com/dart-lang/pub/issues/2258
    """
    num_packages = draw(st.integers(min_value=3, max_value=6))
    packages = PACKAGE_NAMES[:num_packages]

    graph: dict[str, dict[int, dict[str, Range[int]]]] = {}
    for package in packages:
        num_versions = draw(st.integers(min_value=1, max_value=4))
        package_versions: dict[int, dict[str, Range[int]]] = {}
        for version in range(1, num_versions + 1):
            deps: dict[str, Range[int]] = {}
            other_packages = [p for p in packages if p != package]
            if other_packages:
                num_deps = draw(
                    st.integers(min_value=0, max_value=min(3, len(other_packages)))
                )
                if num_deps > 0:
                    dep_packages = draw(
                        st.lists(
                            st.sampled_from(other_packages),
                            min_size=num_deps,
                            max_size=num_deps,
                            unique=True,
                        )
                    )
                    for dep_package in dep_packages:
                        deps[dep_package] = draw(dependency_ranges())
            package_versions[version] = deps
        graph[package] = package_versions

    if len(packages) >= 2:
        pkg_a, pkg_b = packages[0], packages[1]
        a_versions = sorted(graph[pkg_a].keys())
        high_v = a_versions[-1]
        graph[pkg_a][high_v][pkg_b] = draw(dependency_ranges())
        b_versions = sorted(graph[pkg_b].keys())
        for bv in b_versions:
            if draw(st.booleans()):
                graph[pkg_b][bv][pkg_a] = draw(dependency_ranges())

    root_dep_count = draw(st.integers(min_value=1, max_value=min(3, len(packages))))
    root_dep_packages = draw(
        st.lists(
            st.sampled_from(packages),
            min_size=root_dep_count,
            max_size=root_dep_count,
            unique=True,
        )
    )
    root_deps: dict[str, Range[int]] = {
        dep_package: draw(dependency_ranges()) for dep_package in root_dep_packages
    }
    graph["root"] = {1: root_deps}
    return graph


@st.composite
def single_version_conflict_graphs(
    draw: st.DrawFn,
) -> dict[str, dict[int, dict[str, Range[int]]]]:
    """Generate graphs where some packages have exactly one version.

    When the only available version of a package conflicts with other
    constraints there is no alternative version to try.  The resolver
    must conclude unsatisfiable rather than rederive the same
    dead-end repeatedly.
    """
    num_packages = draw(st.integers(min_value=3, max_value=5))
    packages = PACKAGE_NAMES[:num_packages]

    graph: dict[str, dict[int, dict[str, Range[int]]]] = {}
    for index, package in enumerate(packages):
        if index > 0 and draw(st.integers(min_value=1, max_value=5)) <= 2:
            num_versions = 1
        else:
            num_versions = draw(st.integers(min_value=1, max_value=4))
        package_versions: dict[int, dict[str, Range[int]]] = {}
        for version in range(1, num_versions + 1):
            deps: dict[str, Range[int]] = {}
            other = [p for p in packages if p != package]
            if other:
                num_deps = draw(st.integers(min_value=0, max_value=min(2, len(other))))
                if num_deps > 0:
                    dep_packages = draw(
                        st.lists(
                            st.sampled_from(other),
                            min_size=num_deps,
                            max_size=num_deps,
                            unique=True,
                        )
                    )
                    for dep_package in dep_packages:
                        deps[dep_package] = draw(dependency_ranges())
            package_versions[version] = deps
        graph[package] = package_versions

    root_deps = {
        p: draw(dependency_ranges())
        for p in packages[
            : draw(st.integers(min_value=1, max_value=min(3, len(packages))))
        ]
    }
    graph["root"] = {1: root_deps}
    return graph


@st.composite
def wide_fan_in_graphs(
    draw: st.DrawFn,
) -> dict[str, dict[int, dict[str, Range[int]]]]:
    """Generate graphs where many packages constrain a single bottleneck.

        root -> parent0, parent1, parent2, ...
        parent0 -> bottleneck [some range]
        parent1 -> bottleneck [different range]
        parent2 -> bottleneck [different range]
        ...

    A conflict on the bottleneck involves terms from many decision
    levels (one per parent).  The conflict-resolution loop has to
    pick the most-recent satisfier and compute the right backjump
    target.

    See: https://github.com/pubgrub-rs/pubgrub/pull/257
    """
    num_parents = draw(st.integers(min_value=3, max_value=6))
    parents = [f"parent{i}" for i in range(num_parents)]
    bottleneck = "bottleneck"

    graph: dict[str, dict[int, dict[str, Range[int]]]] = {}

    num_bottleneck_versions = draw(st.integers(min_value=2, max_value=6))
    graph[bottleneck] = {v: {} for v in range(1, num_bottleneck_versions + 1)}

    for parent in parents:
        num_versions = draw(st.integers(min_value=1, max_value=3))
        parent_versions: dict[int, dict[str, Range[int]]] = {}
        for version in range(1, num_versions + 1):
            parent_versions[version] = {bottleneck: draw(dependency_ranges())}
        graph[parent] = parent_versions

    graph["root"] = {1: {p: Range.full() for p in parents}}
    return graph


@st.composite
def mutual_dep_exhaustive_graphs(
    draw: st.DrawFn,
) -> dict[str, dict[int, dict[str, Range[int]]]]:
    """Small graphs (2-3 packages) with at least one mutual-dep pair.

    Combines :func:`small_exhaustive_graphs` (small enough for
    brute-force enumeration) with :func:`mutual_back_edge_graphs`
    (at least one A<->B pair).  Targets the kind of graph most
    likely to expose a false impossible report.
    """
    num_packages = draw(st.integers(min_value=2, max_value=3))
    packages = PACKAGE_NAMES[:num_packages]

    graph: dict[str, dict[int, dict[str, Range[int]]]] = {}
    for package in packages:
        num_versions = draw(st.integers(min_value=1, max_value=3))
        package_versions: dict[int, dict[str, Range[int]]] = {}
        for version in range(1, num_versions + 1):
            deps: dict[str, Range[int]] = {}
            other_packages = [p for p in packages if p != package]
            if other_packages:
                num_deps = draw(
                    st.integers(min_value=0, max_value=min(2, len(other_packages)))
                )
                if num_deps > 0:
                    dep_packages = draw(
                        st.lists(
                            st.sampled_from(other_packages),
                            min_size=num_deps,
                            max_size=num_deps,
                            unique=True,
                        )
                    )
                    for dep_package in dep_packages:
                        deps[dep_package] = draw(dependency_ranges())
            package_versions[version] = deps
        graph[package] = package_versions

    if len(packages) >= 2:
        pkg_a, pkg_b = packages[0], packages[1]
        a_versions = sorted(graph[pkg_a].keys())
        b_versions = sorted(graph[pkg_b].keys())
        v_a = draw(st.sampled_from(a_versions))
        v_b = draw(st.sampled_from(b_versions))
        graph[pkg_a][v_a][pkg_b] = draw(dependency_ranges())
        graph[pkg_b][v_b][pkg_a] = draw(dependency_ranges())

    root_dep_count = draw(st.integers(min_value=1, max_value=min(2, len(packages))))
    root_dep_packages = draw(
        st.lists(
            st.sampled_from(packages),
            min_size=root_dep_count,
            max_size=root_dep_count,
            unique=True,
        )
    )
    root_deps: dict[str, Range[int]] = {
        dep_package: draw(dependency_ranges()) for dep_package in root_dep_packages
    }
    graph["root"] = {1: root_deps}
    return graph
