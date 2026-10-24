"""Selected-release preparation preserves actual target proof boundaries."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import (
    MetadataHashMismatchError,
    SiblingMetadataDivergenceError,
)
from nab_provider.provider import ListingFilterCache, Provider
from nab_provider.records import DistFile
from nab_provider.tags import PlatformSpec, TagSet
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator, make_metadata, make_sdist, make_wheel
from nab_provider.yanking import append_yanked_file


def target(python: str = "3.12", platform: str = "linux_x86_64") -> ResolveTarget:
    """Return a declared CPython target for the provider tests."""
    return ResolveTarget.for_declared(
        python_version=python, spec=PlatformSpec(platform)
    )


def test_release_groups_keep_aliases_and_reject_different_local_versions() -> None:
    files = [
        make_wheel("1"),
        make_sdist("1.0"),
        make_wheel("1+local"),
        make_wheel("bad"),
    ]
    cache = ListingFilterCache(3, target_count=48)
    selected, filters = cache.selected_release("pkg", files, Version("1"))

    assert list(selected) == files[:2]

    # Version 1.0 reuses the complete view prepared for version 1.
    again, same_filters = cache.selected_release("pkg", files, Version("1.0"))

    assert again is selected
    assert same_filters is filters
    assert filters.shares_pythons
    assert filters.shares_targets


def test_replaced_listing_rebuilds_release_groups_and_prepared_filters() -> None:
    old = [make_wheel("1", url="https://old.example/pkg.whl")]
    replacement = [make_wheel("1", url="https://new.example/pkg.whl")]
    port = make_coordinator(old)
    cache = ListingFilterCache(3, target_count=48)
    first = Provider(port, target(), listing_filter_cache=cache, defer_yanked=True)
    first.prepare_preferred_releases({"pkg": Version("1")})

    assert first.filter_distributions("pkg", old)[0][1] is old[0]

    # A replacement source must discard the first artifact's preparation.
    second = Provider(port, target(), listing_filter_cache=cache, defer_yanked=True)
    second.prepare_preferred_releases({"pkg": Version("1")})

    assert second.filter_distributions("pkg", replacement)[0][1] is replacement[0]


def test_restricted_filter_cannot_poison_normal_search_or_another_release() -> None:
    files = [make_wheel("1"), make_wheel("2")]
    port = make_coordinator(files)
    cache = ListingFilterCache(3, target_count=48)
    selected = Provider(port, target(), listing_filter_cache=cache, defer_yanked=True)
    selected.prepare_preferred_releases({"pkg": Version("1")})

    assert [version for version, _ in selected.fetch_versions("pkg")] == [Version("1")]

    # Ordinary search must still see every release after a restricted trial.
    normal = Provider(port, target(), listing_filter_cache=cache, defer_yanked=True)

    assert [version for version, _ in normal.fetch_versions("pkg")] == [
        Version("2"),
        Version("1"),
    ]

    other = Provider(port, target(), listing_filter_cache=cache, defer_yanked=True)
    other.prepare_preferred_releases({"pkg": Version("2")})

    assert [version for version, _ in other.fetch_versions("pkg")] == [Version("2")]


def test_fresh_provider_requirement_and_extra_version_agreement() -> None:
    port = make_coordinator([make_wheel("1")])
    # Fetching versions makes the provider ineligible for a new trial.
    provider = Provider(port, listing_filter_cache=ListingFilterCache())
    provider.fetch_versions("pkg")

    with pytest.raises(ValueError, match="fresh provider"):
        provider.prepare_preferred_releases({"pkg": Version("1")})

    # Equivalent base and extra hints configure one trial, not two.
    provider = Provider(port, listing_filter_cache=ListingFilterCache())
    provider.prepare_preferred_releases(
        {"Pkg": Version("1"), "pkg[speed]": Version("1.0")}
    )

    with pytest.raises(ValueError, match="fresh provider"):
        provider.prepare_preferred_releases({"pkg": Version("1")})

    # Conflicting extra hints fail before a selection map is published.
    provider = Provider(port, listing_filter_cache=ListingFilterCache())

    with pytest.raises(ValueError, match="base and extra"):
        provider.prepare_preferred_releases(
            {"pkg": Version("1"), "pkg[speed]": Version("2")}
        )


@pytest.mark.parametrize("reason", ["prerelease", "cutoff", "no-cache"])
def test_global_admission_contexts_keep_full_listing(reason: str) -> None:
    files = [make_wheel("1"), make_wheel("2rc1")]
    cache = None if reason == "no-cache" else ListingFilterCache()
    cutoff = datetime(2021, 1, 1, tzinfo=timezone.utc) if reason == "cutoff" else None

    provider = Provider(
        make_coordinator(files),
        listing_filter_cache=cache,
        uploaded_prior_to=cutoff,
    )
    preferred = Version("2rc1") if reason == "prerelease" else Version("1")
    provider.prepare_preferred_releases({"pkg": preferred})
    provider.fetch_versions("pkg")

    assert provider.stats.distributions_seen == len(files)


def test_withdrawal_evidence_keeps_live_alternatives_complete() -> None:
    files = append_yanked_file([make_wheel("1")], make_wheel("2"), reason="withdrawn")
    provider = Provider(
        make_coordinator(files),
        target(),
        listing_filter_cache=ListingFilterCache(3, target_count=48),
        defer_yanked=True,
    )
    provider.prepare_preferred_releases({"pkg": Version("2")})

    assert {version for version, _ in provider.fetch_versions("pkg")} == {
        Version("1"),
        Version("2"),
    }


def test_unknown_hint_package_refuses_instead_of_preparing_history() -> None:
    files = [make_wheel("1")]
    provider = Provider(
        make_coordinator(files), listing_filter_cache=ListingFilterCache()
    )
    provider.prepare_preferred_releases({"other": Version("1")})

    assert provider.choose_version("pkg", VersionRange.full()) is None


@pytest.mark.parametrize("package", ["pkg", "pkg[speed]"])
def test_forced_change_uses_complete_view_before_preparation(package: str) -> None:
    files = [make_wheel("1"), make_wheel("2")]
    port = make_coordinator(files)
    # Both releases are valid; only the incoming range forces the change.
    for file in files:
        assert file.metadata_url is not None
        port.index.store_metadata(
            "pkg",
            file.version,
            make_metadata("pkg", file.version, provides_extra=("speed",)),
            metadata_url=file.metadata_url,
        )

    provider = Provider(
        port,
        target(),
        preferences={"pkg": Version("1")},
        listing_filter_cache=ListingFilterCache(),
    )
    provider.prepare_preferred_releases({"pkg": Version("1")})
    required = SpecifierSet(">=2").to_range()
    provider.receive_partial_solution_hint({"pkg": required}, {})
    query = VersionRange.full() if package == "pkg[speed]" else required

    assert provider.choose_version(package, query) == Version("2")


def test_late_forced_change_refuses_without_replacing_prepared_state() -> None:
    files = [make_wheel("1"), make_wheel("2")]
    provider = Provider(
        make_coordinator(files, auto_metadata=True),
        target(),
        listing_filter_cache=ListingFilterCache(),
    )
    provider.prepare_preferred_releases({"pkg": Version("1")})
    snapshot = provider.fetch_versions("pkg")

    # Expanding an existing view would invalidate the trial's prepared state.
    assert provider.choose_version("pkg", SpecifierSet(">=2").to_range()) is None
    assert provider.fetch_versions("pkg") is snapshot


def test_extra_without_a_partial_solution_hint_keeps_preferred_release() -> None:
    provider = Provider(
        make_coordinator(
            [make_wheel("1")],
            metadata_text=make_metadata("pkg", "1", provides_extra=("speed",)),
        ),
        target(),
        preferences={"pkg": Version("1")},
        listing_filter_cache=ListingFilterCache(),
    )
    provider.prepare_preferred_releases({"pkg[speed]": Version("1")})

    assert provider.choose_version("pkg[speed]", VersionRange.full()) == Version("1")


@pytest.mark.parametrize("fails", [False, True])
def test_selected_filter_restores_shared_context_on_success_and_error(
    fails: bool,
) -> None:
    class RejectingTags(TagSet):
        def accepts(self, wheel_filename: str) -> bool:
            raise RuntimeError("tag backend failed")

    files = [make_wheel("1"), make_wheel("2")]
    cache = ListingFilterCache(target_count=48)
    provider = Provider(make_coordinator(files), target(), listing_filter_cache=cache)
    provider.prepare_preferred_releases({"pkg": Version("1")})

    if fails:
        provider.wheel_tags = RejectingTags((("py3", "none", "any"),))
        with pytest.raises(RuntimeError, match="tag backend failed"):
            provider.filter_distributions("pkg", files)
    else:
        assert len(provider.filter_distributions("pkg", files)) == 1

    assert provider.listing_filter_cache is cache


@pytest.mark.parametrize("fault", ["sibling-deps", "metadata-hash"])
def test_selected_release_preserves_metadata_checks(fault: str) -> None:
    first = make_wheel("1")
    sibling = make_wheel("1.0")

    assert first.metadata_url is not None
    assert sibling.metadata_url is not None

    files: list[DistFile] = [make_wheel("0"), first, sibling]
    port = make_coordinator(files)
    provider = Provider(port, target(), listing_filter_cache=ListingFilterCache())
    provider.prepare_preferred_releases({"pkg": Version("1")})

    # The restricted view must preserve both sidecar failure paths.
    if fault == "metadata-hash":
        port.index.store_metadata_error(
            "pkg",
            "1",
            MetadataHashMismatchError("mismatch"),
            metadata_url=first.metadata_url,
        )
        with pytest.raises(MetadataHashMismatchError):
            provider.get_dependencies("pkg", Version("1"))
    else:
        port.index.store_metadata(
            "pkg", "1", make_metadata("pkg", "1"), metadata_url=first.metadata_url
        )
        port.index.store_metadata(
            "pkg",
            "1",
            make_metadata("pkg", "1", "other"),
            metadata_url=sibling.metadata_url,
        )
        with pytest.raises(SiblingMetadataDivergenceError):
            provider.get_dependencies("pkg", Version("1"))
