"""Shared listing preparation retains each target's admission and counters."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nab_provider.provider import ListingFilterCache, Provider
from nab_provider.records import SdistFile
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator, make_sdist, make_wheel, pkg_override


@pytest.mark.parametrize("policy", ["default", "override", "cutoff"])
@pytest.mark.parametrize("allow_zip", [False, True])
def test_shared_admission_matches_independent_targets(
    policy: str, *, allow_zip: bool
) -> None:
    """Malformed bounds, repeated exclusions and ZIP policy keep their order."""
    files = [
        make_wheel("1", upload_time="2020-01-01T00:00:00Z"),
        make_wheel("2", requires_python=">=3.12", upload_time="2020-01-01T00:00:00Z"),
        make_wheel(
            "2",
            tag="py2.py3-none-any",
            requires_python=">=3.12",
            upload_time="2020-01-01T00:00:00Z",
        ),
        make_wheel("3", requires_python="invalid", upload_time="2020-01-01T00:00:00Z"),
        SdistFile(
            "pkg-4.zip",
            "https://example.com/pkg-4.zip",
            "4",
            None,
            "2025-01-01T00:00:00Z",
        ),
        make_sdist("5", requires_python=">=3.13", upload_time="2020-01-01T00:00:00Z"),
    ]
    coordinator = make_coordinator(files, auto_metadata=True)
    shared = ListingFilterCache(3, target_count=3)
    targets = [
        None,
        ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        ),
        ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("windows_amd64")
        ),
    ]
    for target in targets:
        overrides = (
            [pkg_override("pkg", requires_python=">=3.12")]
            if policy == "override"
            else []
        )
        cutoff = (
            datetime(2024, 1, 1, tzinfo=timezone.utc) if policy == "cutoff" else None
        )
        cached = Provider(
            coordinator,
            target=target,
            listing_filter_cache=shared,
            allow_zip_sdists=allow_zip,
            package_overrides=overrides,
            uploaded_prior_to=cutoff,
        )
        independent = Provider(
            coordinator,
            target=target,
            allow_zip_sdists=allow_zip,
            package_overrides=overrides,
            uploaded_prior_to=cutoff,
        )

        actual = cached.fetch_versions("pkg")
        assert actual == independent.fetch_versions("pkg")
        assert cached.stats == independent.stats
        assert (
            cached.tag_excluded_wheels_by_version
            == independent.tag_excluded_wheels_by_version
        )
        if policy == "default":
            assert "3" in {str(version) for version, _ in actual}
            assert ("4" in {str(version) for version, _ in actual}) is allow_zip
            if target is not None and target.python_version == "3.11":
                assert cached.stats.excluded_by_python == 3
