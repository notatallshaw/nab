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

import os
from datetime import datetime, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nab.config.values import check_package_override_overlap
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider.errors import ConfigError
from nab_provider.overrides import PackageOverride
from nab_provider.provider import BuildPolicy, DistPolicy

pytestmark = pytest.mark.property

NAME = "foo"

# Specifier clauses over a small version grid so two random requirements
# overlap roughly half the time; a bare name (full range) is included so
# the always-overlaps case is exercised.
SPECIFIERS = ["", "<=2", ">=2", "<3", ">=3", ">1,<4", "==2"]

_DEEP = os.environ.get("HYPOTHESIS_PROFILE") == "deep"

PROPERTY_SETTINGS = settings(
    max_examples=1000 if _DEEP else 100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@st.composite
def package_overrides(draw: st.DrawFn, *, name: str) -> PackageOverride:
    """Draw one per-package ``PackageOverride`` for ``name``.

    The requirement is ``name`` plus a specifier drawn from a small grid
    so two draws overlap roughly half the time.  The body sets exactly one
    field, drawn across the policy surfaces, so the overlap property
    exercises per-field discrimination and the uploaded-prior-to
    cutoff/disable bucketing (a datetime cutoff and a ``false`` disable
    count as the same field).
    """
    specifier = draw(st.sampled_from(SPECIFIERS))
    requirement = Requirement(f"{name} {specifier}".strip())
    field = draw(st.sampled_from(["dist", "build", "upload_cutoff", "upload_off"]))
    dist_policy: DistPolicy | None = None
    build_policy: BuildPolicy | None = None
    uploaded_prior_to: datetime | None = None
    uploaded_prior_to_disabled = False
    if field == "dist":
        dist_policy = draw(st.sampled_from(list(DistPolicy)))
    elif field == "build":
        build_policy = draw(st.sampled_from(list(BuildPolicy)))
    elif field == "upload_cutoff":
        uploaded_prior_to = datetime(2024, 1, 1, tzinfo=timezone.utc)
    else:
        uploaded_prior_to_disabled = True
    return PackageOverride(
        requirement=requirement,
        name=canonicalize_name(name),
        version_range=requirement.specifier.to_range(),
        dist_policy=dist_policy,
        build_policy=build_policy,
        uploaded_prior_to=uploaded_prior_to,
        uploaded_prior_to_disabled=uploaded_prior_to_disabled,
    )


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
        check_package_override_overlap((left, right))
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
