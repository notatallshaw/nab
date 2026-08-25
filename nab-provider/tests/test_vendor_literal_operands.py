"""Tests for the vendored patch's inline ``===`` literal test in the set operations.

``intersection``, ``union`` and ``difference`` each test both operands for
literals inline and take the bounds-only shortcut when neither carries one.
Omitting an operand from that test silently drops its literals, so each
operation is pinned with the literal on one side at a time.
"""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet

LITERAL = "foo"

# The two ways an operand carries the literal: admitting it, and excluding it
# from a range that would otherwise admit any arbitrary string.
ADMITS = SpecifierSet(f"==={LITERAL}").to_range()
REJECTS = VersionRange.full().difference(ADMITS)

# Operands with no literal of their own. ``ARBITRARY`` admits the string
# anyway, ``BOUNDED`` admits nothing but versions.
ARBITRARY = VersionRange.full()
BOUNDED = SpecifierSet(">=1.0,<2.0").to_range()


@pytest.mark.parametrize(
    ("left", "right", "admitted"),
    [
        pytest.param(ARBITRARY, REJECTS, False, id="right-rejects"),
        pytest.param(REJECTS, ARBITRARY, False, id="left-rejects"),
        pytest.param(ARBITRARY, ADMITS, True, id="right-admits"),
        pytest.param(ADMITS, ARBITRARY, True, id="left-admits"),
    ],
)
def test_intersection_reads_the_literals_of_both_operands(
    left: VersionRange, right: VersionRange, *, admitted: bool
) -> None:
    assert (LITERAL in left.intersection(right)) is admitted


@pytest.mark.parametrize(
    ("left", "right", "admitted"),
    [
        pytest.param(BOUNDED, REJECTS, False, id="right-rejects"),
        pytest.param(REJECTS, BOUNDED, False, id="left-rejects"),
        pytest.param(BOUNDED, ADMITS, True, id="right-admits"),
        pytest.param(ADMITS, BOUNDED, True, id="left-admits"),
    ],
)
def test_union_reads_the_literals_of_both_operands(
    left: VersionRange, right: VersionRange, *, admitted: bool
) -> None:
    assert (LITERAL in left.union(right)) is admitted


# No left-rejects case: reaching the literal test with only ``self`` carrying a
# reject needs a plain ``other`` with bounds, and subtracting one shrinks the
# bounds, dropping the arbitrary admission the reject exists to narrow.
@pytest.mark.parametrize(
    ("left", "right", "admitted"),
    [
        pytest.param(ARBITRARY, REJECTS, True, id="right-rejects"),
        pytest.param(ARBITRARY, ADMITS, False, id="right-admits"),
        pytest.param(ADMITS, BOUNDED, True, id="left-admits"),
    ],
)
def test_difference_reads_the_literals_of_both_operands(
    left: VersionRange, right: VersionRange, *, admitted: bool
) -> None:
    assert (LITERAL in left.difference(right)) is admitted
