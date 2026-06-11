"""Metamorphic resolver properties with exact-equality oracles.

Each property applies a solution-preserving transformation to a graph
and asserts the resolver's output is unchanged modulo the mapping:

1. Adding an unreferenced package must not change the exact solution.
2. Order-preserving package renaming must preserve the outcome modulo
   the rename (tiebreaks compare ``str(package)``, and a common prefix
   keeps relative lexicographic order).
3. Strictly monotone version relabeling (``v -> v + offset`` with range
   bounds shifted identically) must preserve the outcome modulo the
   shift.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.ranges import NEGATIVE_INFINITY, POSITIVE_INFINITY, Range

from .graph_oracles import Graph, solve
from .providers import verify_solution
from .strategies import PROPERTY_SETTINGS, small_exhaustive_graphs

pytestmark = pytest.mark.property

METAMORPHIC_TIMEOUT_SECONDS = 120


def _shift_range(r: Range[int], offset: int) -> Range[int]:
    """Shift every finite interval bound by ``offset`` (strictly monotone)."""
    intervals = []
    for lower, lower_inc, upper, upper_inc in r._intervals:
        new_lower = lower if lower is NEGATIVE_INFINITY else lower + offset
        new_upper = upper if upper is POSITIVE_INFINITY else upper + offset
        intervals.append((new_lower, lower_inc, new_upper, upper_inc))
    return Range(tuple(intervals))


def _shift_graph(graph: Graph, offset: int) -> Graph:
    """Apply the version shift to every version key and range bound."""
    shifted: Graph = {}
    for package, versions in graph.items():
        if package == "root":
            shifted["root"] = {
                1: {d: _shift_range(r, offset) for d, r in versions[1].items()}
            }
            continue
        shifted[package] = {
            version + offset: {d: _shift_range(r, offset) for d, r in deps.items()}
            for version, deps in versions.items()
        }
    return shifted


def _rename_graph(graph: Graph, prefix: str) -> Graph:
    """Prefix every package name except the root entry."""

    def rename(name: str) -> str:
        return name if name == "root" else prefix + name

    return {
        rename(package): {
            version: {rename(d): r for d, r in deps.items()}
            for version, deps in versions.items()
        }
        for package, versions in graph.items()
    }


class TestIrrelevantAddition:
    """Adding a package nothing references changes nothing."""

    @pytest.mark.timeout(METAMORPHIC_TIMEOUT_SECONDS)
    @given(
        graph=small_exhaustive_graphs(),
        extra_versions=st.integers(min_value=1, max_value=3),
    )
    @PROPERTY_SETTINGS
    def test_unreferenced_package_changes_nothing(
        self, graph: Graph, extra_versions: int
    ) -> None:
        """An unreferenced package leaves the exact solution unchanged."""
        base_solution, base_error = solve(graph)
        if base_solution is None and base_error is None:
            return

        extended = dict(graph)
        referenced = [p for p in graph if p != "root"]
        extended["zzz-unreferenced"] = {
            v: {referenced[0]: Range.full()} for v in range(1, extra_versions + 1)
        }
        ext_solution, ext_error = solve(extended)
        if ext_solution is None and ext_error is None:
            return

        assert (base_solution is None) == (ext_solution is None), (
            "adding an unreferenced package flipped solvability"
        )
        if base_solution is not None:
            assert base_solution == ext_solution, (
                f"adding an unreferenced package changed the solution: "
                f"{base_solution} -> {ext_solution}"
            )


class TestRenaming:
    """Order-preserving renames preserve the outcome modulo the mapping."""

    @pytest.mark.timeout(METAMORPHIC_TIMEOUT_SECONDS)
    @given(graph=small_exhaustive_graphs())
    @PROPERTY_SETTINGS
    def test_order_preserving_rename(self, graph: Graph) -> None:
        """Renaming with a common prefix maps the solution one-to-one."""
        base_solution, base_error = solve(graph)
        if base_solution is None and base_error is None:
            return
        renamed = _rename_graph(graph, "z")
        renamed_solution, renamed_error = solve(renamed)
        if renamed_solution is None and renamed_error is None:
            return

        assert (base_solution is None) == (renamed_solution is None), (
            "order-preserving rename flipped solvability"
        )
        if base_solution is not None:
            assert renamed_solution == {"z" + p: v for p, v in base_solution.items()}, (
                f"rename changed solution: {base_solution} vs {renamed_solution}"
            )
        if renamed_solution is not None:
            verify_solution(renamed_solution, renamed["root"][1], renamed)


class TestVersionRelabeling:
    """Strictly monotone version maps preserve the outcome modulo the map."""

    @pytest.mark.timeout(METAMORPHIC_TIMEOUT_SECONDS)
    @given(
        graph=small_exhaustive_graphs(), offset=st.integers(min_value=1, max_value=50)
    )
    @PROPERTY_SETTINGS
    def test_version_shift(self, graph: Graph, offset: int) -> None:
        """Shifting all versions and bounds by an offset maps the solution."""
        base_solution, base_error = solve(graph)
        if base_solution is None and base_error is None:
            return
        shifted = _shift_graph(graph, offset)
        shifted_solution, shifted_error = solve(shifted)
        if shifted_solution is None and shifted_error is None:
            return

        assert (base_solution is None) == (shifted_solution is None), (
            f"version shift by {offset} flipped solvability"
        )
        if base_solution is not None:
            assert shifted_solution == {
                p: v + offset for p, v in base_solution.items()
            }, (
                f"shift by {offset} changed solution: "
                f"{base_solution} vs {shifted_solution}"
            )
        if shifted_solution is not None:
            verify_solution(shifted_solution, shifted["root"][1], shifted)
