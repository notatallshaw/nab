"""Property tests guarding the pre-release admission bug class.

The resolver must not admit a pre-release that no active requirement asked
for when a final release would have served. The leak that motivated these
tests was an exclusion (negative) range leaking its pre-release policy into
selection; the fix uses set difference (``positive - negative``) so an
exclusion grants no admission.

The drop-in-final invariant below catches that whole class while allowing
the intended "only a pre-release can satisfy the surviving constraints"
case: a pre-release is a leak only when a *valid final substitute* exists
(one that satisfies the active requirements and whose own dependencies the
rest of the solution already meets). When the only spec-compatible final is
unusable downstream, no such substitute exists and the pre-release stands.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_project._packaging_provider import PackagingProvider
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_resolver.errors import ResolutionError
from nab_resolver.resolver import Resolver

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

_PKGS = ["a", "b", "c", "d"]
_VERSIONS = ["0.9", "1.0", "1.5a1", "1.5", "2.0b1", "2.0", "3.0a1", "3.0"]
_SPECS = ["", ">=1.0", ">=2.0", "==1.0", "==2.0", ">=2.0b1", "<2.0", "!=1.5", "~=1.0"]
_RANGE_SPECS = ["", ">=1.0", "<2.0", ">=1.0,<2.0", "==1.5", "!=1.5", ">=2.0b1"]


class _FilterProvider(PackagingProvider):
    """Selects via ``version_range.filter`` like the real PyPI provider."""

    def choose_version(
        self, package: str, version_range: VersionRange
    ) -> Version | None:
        return next(iter(version_range.filter(self._get_versions(package))), None)

    def has_satisfying_version(self, package: str, version_range: VersionRange) -> bool:
        return self.choose_version(package, version_range) is not None


@st.composite
def _prerelease_graphs(
    draw: st.DrawFn,
) -> tuple[dict[str, dict[Version, dict[str, SpecifierSet]]], dict[str, SpecifierSet]]:
    """Small graphs over a/b/c/d whose version pool mixes finals and pre-releases."""
    graph: dict[str, dict[Version, dict[str, SpecifierSet]]] = {}
    for pkg in _PKGS:
        versions = draw(
            st.lists(st.sampled_from(_VERSIONS), min_size=1, max_size=3, unique=True)
        )
        graph[pkg] = {}
        for raw in versions:
            deps: dict[str, SpecifierSet] = {}
            for dep in _PKGS:
                if dep != pkg and draw(st.booleans()):
                    deps[dep] = SpecifierSet(draw(st.sampled_from(_SPECS)))
            graph[pkg][Version(raw)] = deps
    root_deps = {
        dep: SpecifierSet(draw(st.sampled_from(_SPECS)))
        for dep in draw(st.lists(st.sampled_from(_PKGS), min_size=1, unique=True))
    }
    graph["root"] = {Version("1.0"): root_deps}
    return graph, root_deps


def _active_specs(
    package: str,
    solution: dict[str, Version],
    graph: dict[str, dict[Version, dict[str, SpecifierSet]]],
) -> list[SpecifierSet]:
    specs: list[SpecifierSet] = []
    for parent, version in solution.items():
        spec = graph.get(parent, {}).get(version, {}).get(package)
        if spec is not None:
            specs.append(spec)
    return specs


def _has_valid_final_dropin(
    package: str,
    solution: dict[str, Version],
    graph: dict[str, dict[Version, dict[str, SpecifierSet]]],
    specs: list[SpecifierSet],
) -> bool:
    """A final of ``package`` that satisfies the active specs and whose own
    dependencies the rest of the solution already meets."""
    for candidate in graph.get(package, {}):
        if candidate.is_prerelease or candidate == solution[package]:
            continue
        if not all(spec.contains(candidate, prereleases=True) for spec in specs):
            continue
        deps = graph[package][candidate]
        if all(
            dep in solution and spec.contains(solution[dep], prereleases=True)
            for dep, spec in deps.items()
        ):
            return True
    return False


class TestNoUnauthorizedPrereleaseDropIn:
    """No package resolves to a pre-release when an unrequested final fits."""

    @given(graph_and_roots=_prerelease_graphs())
    @PROPERTY_SETTINGS
    def test_no_unauthorized_prerelease_dropin(
        self,
        graph_and_roots: tuple[
            dict[str, dict[Version, dict[str, SpecifierSet]]], dict[str, SpecifierSet]
        ],
    ) -> None:
        graph, _root_deps = graph_and_roots
        resolver = Resolver(
            _FilterProvider(graph),
            range_type=VersionRange,
            root_version="0",
            max_iterations=2000,
        )
        try:
            solution = resolver.resolve(
                {"root": VersionRange.singleton(Version("1.0"))}
            )
        except ResolutionError:
            return

        for package, version in solution.items():
            if package == "root" or not version.is_prerelease:
                continue
            specs = _active_specs(package, solution, graph)
            if any(spec.prereleases is True for spec in specs):
                continue  # a requirement legitimately admits the pre-release
            assert not _has_valid_final_dropin(package, solution, graph, specs), (
                f"{package}=={version} is a pre-release no requirement asked for, "
                f"yet a final release would satisfy the same solution"
            )


class TestIsEmptyPolicyIndependent:
    """``(a - b).is_empty == (a & ~b).is_empty`` for nab-built ranges.

    Justifies expressing the resolver's subset tests as set difference: the
    two agree on emptiness because nab never sets an explicit pre-release
    policy, so ``is_empty`` does not depend on the carried policy.
    """

    @given(
        left=st.sampled_from(_RANGE_SPECS),
        right=st.sampled_from(_RANGE_SPECS),
    )
    @PROPERTY_SETTINGS
    def test_difference_emptiness_matches_complement(
        self, left: str, right: str
    ) -> None:
        a = SpecifierSet(left).to_range()
        b = SpecifierSet(right).to_range()
        assert (a - b).is_empty == (a & ~b).is_empty
