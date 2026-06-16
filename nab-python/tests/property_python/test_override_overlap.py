"""Property test: per-package overlap is an error iff the ranges overlap.

The parse-time non-overlap check rejects two per-package overrides that
set the same field for one package when their version ranges overlap.
The oracle is the ``packaging.ranges.VersionRange`` algebra:
two ranges overlap exactly when ``not (a & b).is_empty``.

Invariants:

* the config parser raises ``ConfigError`` for two same-package
  same-field entries iff their ranges overlap;
* a disjoint pair always parses;
* the result is symmetric in the two entries' declared order.
"""

from __future__ import annotations

import pytest
from hypothesis import given

from nab_python.config import (
    ConfigError,
    PackageOverride,
    _check_package_override_overlap,
)

from .strategies import PROPERTY_SETTINGS, package_overrides

pytestmark = pytest.mark.property

NAME = "foo"


def _overlaps(left: PackageOverride, right: PackageOverride) -> bool:
    """Oracle: the two ranges share at least one version."""
    return not (left.version_range & right.version_range).is_empty


def _raises(left: PackageOverride, right: PackageOverride) -> bool:
    """Whether the parse-time non-overlap check rejects the pair."""
    try:
        _check_package_override_overlap((left, right))
    except ConfigError:
        return True
    return False


@PROPERTY_SETTINGS
@given(
    left=package_overrides(name=NAME),
    right=package_overrides(name=NAME),
)
def test_errors_iff_ranges_overlap(
    left: PackageOverride,
    right: PackageOverride,
) -> None:
    expected = _overlaps(left, right)
    assert _raises(left, right) is expected
    # Symmetric in declared order: swapping the two entries cannot change
    # whether they conflict.
    assert _raises(right, left) is expected
