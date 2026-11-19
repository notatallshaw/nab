"""Exercise provider conformance across generated backtracking fixtures."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.ranges import Range

from ..conformance_fixture import Graph, check_fixture

pytestmark = pytest.mark.property


@given(
    st.lists(st.tuples(st.integers(0, 3), st.integers(0, 3)), min_size=2, max_size=2)
)
def test_generated_backtracking_fixtures(bounds: list[tuple[int, int]]) -> None:
    graph: Graph = {"app": {}, "lib": {1: {}, 2: {}}}

    for version, interval_bounds in enumerate(bounds, start=1):
        lower, upper = sorted(interval_bounds)
        graph["app"][version] = {"lib": Range.between(lower, upper)}

    check_fixture(graph)
