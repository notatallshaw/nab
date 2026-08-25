"""Tests for the vendored patch's pre-release policy guard.

Every set operation and relation query tests policy compatibility inline and
calls ``_check_policy_compat`` only when that test fails, so each is checked
here against both errors the guard raises.
"""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet

OPERATIONS = [
    "difference",
    "intersection",
    "is_disjoint",
    "is_subset",
    "is_superset",
    "relation",
    "union",
]


@pytest.fixture
def unconfigured() -> VersionRange:
    """A plain range that carries no configured pre-release policy."""
    return SpecifierSet(">=1.0,<2.0").to_range()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_non_range_operand_is_refused(
    operation: str, unconfigured: VersionRange
) -> None:
    with pytest.raises(TypeError, match="expected VersionRange, got str"):
        getattr(unconfigured, operation)(">=1.5")


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_differently_configured_operand_is_refused(
    operation: str, unconfigured: VersionRange
) -> None:
    # Plain on both sides: an operation that stopped testing the policy would
    # answer from its bounds-only fast path rather than raise further down.
    configured = VersionRange.singleton("1.5", prereleases=True)
    with pytest.raises(ValueError, match="different pre-release policies"):
        getattr(unconfigured, operation)(configured)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_matching_policy_is_accepted(operation: str) -> None:
    """Matching policies reach the operation instead of raising."""
    configured = VersionRange.full(prereleases=True)
    other = VersionRange.singleton("1.5", prereleases=True)
    getattr(configured, operation)(other)
