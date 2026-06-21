"""Property test: per-package overlap errors iff the same field's ranges overlap.

The parse-time non-overlap check rejects two per-package overrides that
set the same field for one package when their version ranges overlap.
The oracle pairs the ``packaging.ranges.VersionRange`` algebra (two
ranges overlap exactly when ``not (a & b).is_empty``) with the parser's
field bucketing: a datetime cutoff and a ``false`` disable share the one
uploaded-prior-to field, and distinct fields never conflict.

Invariants:

* the config parser raises ``ConfigError`` for two same-package entries
  iff they set the same field and their ranges overlap;
* two entries that set different fields never conflict, even when their
  ranges overlap;
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


def _fields(override: PackageOverride) -> set[str]:
    """The field buckets this override sets (mirrors the parser bucketing)."""
    buckets: set[str] = set()
    if override.dist_policy is not None:
        buckets.add("dist")
    if override.build_policy is not None:
        buckets.add("build")
    if override.uploaded_prior_to is not None or override.uploaded_prior_to_disabled:
        buckets.add("upload")
    return buckets


def _conflicts(left: PackageOverride, right: PackageOverride) -> bool:
    """Oracle: a shared field bucket whose ranges share at least one version."""
    if not (_fields(left) & _fields(right)):
        return False
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
def test_errors_iff_same_field_ranges_overlap(
    left: PackageOverride,
    right: PackageOverride,
) -> None:
    expected = _conflicts(left, right)
    assert _raises(left, right) is expected
    # Symmetric in declared order: swapping the two entries cannot change
    # whether they conflict.
    assert _raises(right, left) is expected
