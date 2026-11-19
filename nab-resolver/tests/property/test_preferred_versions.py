"""Check preferred versions against an ordinary solve of the same graph."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver

from ..preferred_provider import Graph, GraphProvider

pytestmark = pytest.mark.property


@st.composite
def registries(draw: st.DrawFn) -> Graph:
    """Generate finite forward dependency graphs with optional version bounds."""
    names = ("a", "b", "c", "d")
    result: Graph = {}
    for index, name in enumerate(names):
        versions = {}
        for version in (1, 2):
            dependencies = {}
            for dependency in names[index + 1 :]:
                if draw(st.booleans()):
                    dependencies[dependency] = Range.at_least(draw(st.integers(1, 2)))
            versions[version] = dependencies
        result[name] = versions
    return result


@given(registries())
def test_preferred_verification_matches_an_ordinary_success(packages: Graph) -> None:
    roots = {"a": Range.full()}
    expected = Resolver(GraphProvider(packages)).resolve(roots)
    provider = GraphProvider(packages)
    provider.preferences = expected

    assert Resolver(provider).try_resolve_preferred(roots, expected) == expected
