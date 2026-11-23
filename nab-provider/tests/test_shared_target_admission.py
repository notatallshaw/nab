"""Shared listing preparation retains each target's admission and counters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

from nab_provider._vendor.packaging.version import Version
from nab_provider.overrides import IndexOverride
from nab_provider.policy import DistPolicy
from nab_provider.provider import ListingFilterCache, Provider
from nab_provider.records import DistFile, SdistFile
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator, make_sdist, make_wheel, pkg_override
from nab_provider.yanking import mark_yanked

if TYPE_CHECKING:
    from collections.abc import Iterator


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


def test_replacement_page_supplies_the_next_targets_candidates_and_diagnosis() -> None:
    old = make_wheel("1")
    replacement = make_wheel("2")
    coordinator = make_coordinator([old], auto_metadata=True)
    cache = ListingFilterCache(2, target_count=3)
    environment = ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("linux_x86_64")
    )
    first = Provider(coordinator, environment, listing_filter_cache=cache)
    assert first.fetch_versions("pkg") == [(Version("1"), old)]
    assert first.diagnose_listing("pkg") is not None

    coordinator.index.store_listing("pkg", [replacement])
    second = Provider(coordinator, environment, listing_filter_cache=cache)
    assert second.fetch_versions("pkg") == [(Version("2"), replacement)]
    diagnosis = second.diagnose_listing("pkg")
    assert diagnosis is not None
    assert diagnosis.kept == {Version("2")}


def test_serving_index_change_reapplies_the_index_policy() -> None:
    wheel = make_wheel("1")
    source = make_sdist("2")
    coordinator = make_coordinator([wheel, source], auto_metadata=True)
    cache = ListingFilterCache(2, target_count=2)
    policies = {"binary": IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)}

    coordinator.index.store_listing_index("pkg", "binary")
    first = Provider(coordinator, listing_filter_cache=cache, index_overrides=policies)
    assert first.fetch_versions("pkg") == [(Version("1"), wheel)]

    coordinator.index.store_listing_index("pkg", "default")
    second = Provider(coordinator, listing_filter_cache=cache, index_overrides=policies)
    assert second.fetch_versions("pkg") == [
        (Version("2"), source),
        (Version("1"), wheel),
    ]


@pytest.mark.parametrize("admit_first", [False, True])
def test_yanked_admission_is_local_to_each_target(*, admit_first: bool) -> None:
    wheel = mark_yanked(make_wheel("1"), reason="withdrawn")
    coordinator = make_coordinator([wheel], auto_metadata=True)
    cache = ListingFilterCache(target_count=2)
    for admit in (admit_first, not admit_first):
        provider = Provider(coordinator, listing_filter_cache=cache)
        if admit:
            provider.yank_admissions = {("pkg", Version("1")): ()}

        assert provider.fetch_versions("pkg") == (
            [(Version("1"), wheel)] if admit else []
        )


def test_interleaved_page_reads_keep_their_own_files_and_index_policy() -> None:
    old_wheel = make_wheel("1")
    old_sdist = make_sdist("2")
    replacement = make_sdist("3")
    coordinator = make_coordinator(auto_metadata=True)
    cache = ListingFilterCache(2, target_count=2)
    policies = {"binary": IndexOverride(dist_policy=DistPolicy.WHEEL_ONLY)}
    target = ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("linux_x86_64")
    )
    first = Provider(
        coordinator, target, listing_filter_cache=cache, index_overrides=policies
    )
    second = Provider(
        coordinator, target, listing_filter_cache=cache, index_overrides=policies
    )

    class Page(list[DistFile]):
        def __iter__(self) -> Iterator[DistFile]:
            # Complete the newer read while the first reader traverses its page.
            coordinator.index.store_listing_index("pkg", "default")
            assert second.serving_index("pkg") == "default"
            assert second.filter_distributions("pkg", [replacement]) == [
                (Version("3"), replacement)
            ]
            return super().__iter__()

    coordinator.index.store_listing_index("pkg", "binary")
    assert first.filter_distributions("pkg", Page([old_wheel, old_sdist])) == [
        (Version("1"), old_wheel)
    ]
