"""Independent keyed ranges obey the scalar set contract for generated inputs."""

from itertools import product

from hypothesis import given
from hypothesis import strategies as st

from nab_resolver.keyed_ranges import KeyedRange
from nab_resolver.ranges import Range


def finite_range(values: set[int]) -> Range[int]:
    """Build a real scalar range from independently generated members."""
    result = Range[int].empty()
    for value in values:
        result |= Range.singleton(value)
    return result


scalar_ranges = st.sets(st.integers(-3, 3)).map(finite_range)
keyed_ranges = st.builds(
    KeyedRange[str, int],
    scalar_ranges,
    st.dictionaries(st.sampled_from(("first", "second")), scalar_ranges),
)


@given(left=keyed_ranges, right=keyed_ranges)
def test_generated_membership_and_resolution_set_laws(
    left: KeyedRange[str, int], right: KeyedRange[str, int]
) -> None:
    assert left.is_subset((left - right) | right)
    assert (left - right).is_disjoint(right)
    assert ~~left == left

    for key, value in product(("first", "second", "undiscovered"), range(-4, 5)):
        first = left.contains(key, value)
        second = right.contains(key, value)
        assert (left & right).contains(key, value) == (first and second)
        assert (left | right).contains(key, value) == (first or second)
        assert (left - right).contains(key, value) == (first and not second)
        assert (~left).contains(key, value) != first
