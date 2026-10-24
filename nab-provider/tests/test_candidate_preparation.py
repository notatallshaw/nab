"""Shared preparation preserves each target's artifact and dependency answers."""

from __future__ import annotations

import gc
import weakref
from collections.abc import Callable, Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_provider._provider import candidate_views
from nab_provider._provider.candidate_views import CandidatePreparation
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import (
    MetadataHashMismatchError,
    SiblingMetadataDivergenceError,
)
from nab_provider.provider import ListingFilterCache, Provider, ProviderStats
from nab_provider.records import DistFile, SdistFile, WheelFile
from nab_provider.tags import PlatformSpec, TagSet, wheel_tag_set
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator, make_metadata, make_sdist, make_wheel


def target(python: str = "3.12", platform: str = "linux_x86_64") -> ResolveTarget:
    """Build a real declared target for shared-versus-fresh comparisons."""
    return ResolveTarget.for_declared(
        python_version=python, spec=PlatformSpec(platform)
    )


def version_range(text: str) -> VersionRange:
    return SpecifierSet(text).to_range()


def test_parsed_tag_groups_preserve_filename_admission() -> None:
    tags = target().tags
    assert tags is not None
    for filename in (
        "pkg-1-py3-none-any.whl",
        "pkg-1-cp312-cp312-manylinux_2_17_x86_64.whl",
        "pkg-1-cp312-cp312-win_amd64.whl",
    ):
        parsed = wheel_tag_set(filename)
        assert parsed is not None
        assert tags.accepts_tags(parsed) == tags.accepts(filename)
        assert tags.accepts_tags(parsed) == tags.accepts(filename)


def test_empty_and_invalid_wheel_views() -> None:
    tags = target().tags

    assert tags is not None
    assert CandidatePreparation([]).for_tags(tags).distributions == ()

    bad = make_wheel(tag="bad")
    view = CandidatePreparation([(Version("1"), bad)]).for_tags(tags)

    assert view.distributions == ()
    assert view.tag_exclusions == ((Version("1"), 1),)
    assert view.empty_versions == 1
    assert view.count_matching(VersionRange.full()) == 0
    assert view.count_matching(VersionRange.full()) == 0


def test_bulk_preparation_keeps_filename_lifetime_inside_the_snapshot() -> None:
    class Filename(str):
        """Make the filename's lifetime observable."""

        __slots__ = ("__weakref__",)

    filename = Filename("bulk_cache_lifetime-1-py3-none-any.whl")
    reference = weakref.ref(filename)
    wheel = WheelFile(
        filename,
        url="https://example.com/wheel.whl",
        version="1",
        requires_python=None,
        has_metadata=False,
        upload_time=None,
    )
    before = wheel_tag_set.cache_info()
    prepared = CandidatePreparation([(Version("1"), wheel)])
    after = wheel_tag_set.cache_info()

    assert (after.hits, after.misses) == (before.hits, before.misses)

    del prepared, wheel, filename
    gc.collect()

    assert reference() is None


def test_snapshot_matching_requires_row_identity_and_order() -> None:
    rows: list[tuple[Version, DistFile]] = [
        (Version("1"), make_wheel("1")),
        (Version("2"), make_sdist("2")),
    ]
    preparation = CandidatePreparation(rows)

    assert preparation.matches_records(rows.copy())
    assert not preparation.matches_records(rows[:-1])
    assert not preparation.matches_records(list(reversed(rows)))
    assert not preparation.matches_records([(version, dist) for version, dist in rows])
    assert CandidatePreparation([]).matches_records([])


@pytest.mark.parametrize("integer_fallback", [False, True])
def test_projection_preserves_unordered_identity_runs_and_equal_versions(
    monkeypatch: pytest.MonkeyPatch, integer_fallback: bool
) -> None:
    if integer_fallback:
        monkeypatch.setattr(candidate_views, "_GROUP_CODEPOINT_MAX", 0)
    first, second, equal_first, third = map(Version, ("1", "2", "1.0", "3"))
    refused = make_wheel("1", tag="cp312-cp312-win_amd64")
    accepted = make_wheel("2")
    rows: list[tuple[Version, DistFile]] = [
        (first, refused),
        (second, accepted),
        (first, refused),
        (equal_first, accepted),
        (third, refused),
        (Version("4"), make_sdist("4")),
    ]
    tags = target().tags
    assert tags is not None
    view = CandidatePreparation(rows).for_tags(tags)

    assert view.distributions == (rows[1], rows[3], rows[5])
    assert view.tag_exclusions == ((first, 2), (third, 1))
    assert view.empty_versions == 1

    separate = CandidatePreparation(
        [(first, refused), (equal_first, refused)]
    ).for_tags(tags)

    assert separate.tag_exclusions == ((first, 1), (equal_first, 1))
    assert separate.empty_versions == 1


def test_identical_python_snapshots_share_views_and_replay_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparations: list[CandidatePreparation] = []
    original = CandidatePreparation.__init__

    def initialize(
        prepared: CandidatePreparation,
        distributions: Sequence[tuple[Version, DistFile]],
    ) -> None:
        preparations.append(prepared)
        original(prepared, distributions)

    monkeypatch.setattr(CandidatePreparation, "__init__", initialize)
    files: list[WheelFile | SdistFile] = [make_wheel("1"), make_sdist("2")]
    port = make_coordinator(files)
    cache = ListingFilterCache(3, target_count=3)
    providers = [
        Provider(port, target(python), listing_filter_cache=cache)
        for python in ("3.12", "3.13", "3.14")
    ]
    for provider in providers:
        result = provider.filter_distributions("pkg", files)
        assert [str(version) for version, _ in result] == ["2", "1"]
        assert provider.stats.distributions_seen == 2
    first, second, third = [provider.prepared_listings["pkg"] for provider in providers]

    assert first is second is third
    assert len(preparations) == 1


def test_source_records_and_distinct_equal_version_objects() -> None:
    tags = target(platform="windows_amd64").tags
    assert tags is not None
    rows: list[tuple[Version, DistFile]] = [
        (Version("2"), make_wheel("2", tag="cp312-cp312-manylinux_2_17_x86_64")),
        (Version("1"), make_wheel("1", tag="cp312-cp312-manylinux_2_17_x86_64")),
        (Version("1.0"), make_sdist("1.0")),
        (Version("1.0"), make_wheel("1.0", tag="cp312-cp312-manylinux_2_17_x86_64")),
    ]
    view = CandidatePreparation(rows).for_tags(tags)

    assert view.distributions == (rows[2],)
    assert sum(count for _, count in view.tag_exclusions) == 3
    assert view.empty_versions == 1
    assert view.count_matching(VersionRange.full()) == 1
    assert view.count_matching(version_range(">=2")) == 0
    assert view.count_matching(VersionRange.full()) == 1


@given(
    st.lists(
        st.tuples(
            st.sampled_from(("1", "1.0", "2rc1", "2", "3")),
            st.sampled_from(
                (
                    "py3-none-any",
                    "py2-none-any",
                    "cp312-cp312-manylinux_2_17_x86_64",
                    "cp312-cp312-win_amd64",
                    "bad",
                    "sdist",
                )
            ),
            st.sampled_from((None, ">=3.12", "<3.12", ">=3.12.5")),
        ),
        max_size=12,
    )
)
def test_shared_views_match_uncached_filters_and_range_counts(
    declarations: list[tuple[str, str, str | None]],
) -> None:
    files: list[WheelFile | SdistFile] = [
        make_sdist(version, requires_python=python)
        if tag == "sdist"
        else make_wheel(version, tag=tag, requires_python=python)
        for version, tag, python in declarations
    ]
    port = make_coordinator(files, auto_metadata=True)
    cache = ListingFilterCache(2, target_count=4)
    targets = [target("3.11"), target(), target(platform="windows_amd64"), target()]
    for environment in targets:
        shared = Provider(port, environment, listing_filter_cache=cache)
        fresh = Provider(port, environment)
        actual = shared.filter_distributions("pkg", files)
        expected = fresh.filter_distributions("pkg", files)
        assert actual == expected
        assert shared.stats == fresh.stats
        assert (
            shared.tag_excluded_wheels_by_version
            == fresh.tag_excluded_wheels_by_version
        )
        for text in ("", ">=2", "<2", "==1.*"):
            wanted = version_range(text)
            assert shared.count_matching_records(
                "pkg", wanted, actual
            ) == fresh.count_matching_records("pkg", wanted, expected)


def test_cache_hit_does_not_walk_every_file_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = [make_wheel(str(i)) for i in range(1, 9)]
    port = make_coordinator(files)
    cache = ListingFilterCache(target_count=2)
    first = Provider(port, target(), listing_filter_cache=cache)
    first.filter_distributions("pkg", files)
    calls = 0
    original = TagSet.accepts_tags

    def count(tags: TagSet, group: frozenset) -> bool:
        nonlocal calls
        calls += 1
        return original(tags, group)

    monkeypatch.setattr(TagSet, "accepts_tags", count)
    second = Provider(
        port, target(platform="windows_amd64"), listing_filter_cache=cache
    )
    result = second.filter_distributions("pkg", files)

    assert len(result) == len(files)
    assert calls == 1

    result.clear()

    assert len(second.filter_distributions("pkg", files)) == len(files)


def test_identical_admission_keeps_each_targets_wheel_ranking() -> None:
    specific = make_wheel("1", tag="cp312-none-any")
    generic = make_wheel("1")

    assert specific.metadata_url is not None
    assert generic.metadata_url is not None
    port = make_coordinator(
        [specific, generic],
        metadata_by_url={
            specific.metadata_url: make_metadata("pkg", "1", "specific-dep"),
            generic.metadata_url: make_metadata("pkg", "1", "generic-dep"),
        },
    )
    cache = ListingFilterCache(target_count=2)
    first = Provider(port, target(), listing_filter_cache=cache)
    second = Provider(port, target(), listing_filter_cache=cache)
    first.wheel_tags = TagSet((("cp312", "none", "any"), ("py3", "none", "any")))
    second.wheel_tags = TagSet((("py3", "none", "any"), ("cp312", "none", "any")))

    assert set(first.get_dependencies("pkg", Version("1"))) == {"specific-dep"}
    assert set(second.get_dependencies("pkg", Version("1"))) == {"generic-dep"}


def test_same_wheel_metadata_is_evaluated_for_each_target() -> None:
    wheel = make_wheel("1")
    port = make_coordinator(
        [wheel],
        metadata_text=make_metadata(
            "pkg",
            "1",
            "linux-dep; sys_platform == 'linux'",
            "win-dep; sys_platform == 'win32'",
        ),
    )
    cache = ListingFilterCache(target_count=2)
    linux = Provider(port, target(), listing_filter_cache=cache)
    windows = Provider(
        port, target(platform="windows_amd64"), listing_filter_cache=cache
    )

    assert set(linux.get_dependencies("pkg", Version("1"))) == {"linux-dep"}
    assert set(windows.get_dependencies("pkg", Version("1"))) == {"win-dep"}


def test_custom_provider_filter_keeps_its_matching_semantics() -> None:
    class FirstOnly(Provider):
        def filter_distributions(
            self, normalized: str, files: Sequence[WheelFile | SdistFile]
        ) -> list[tuple[Version, DistFile]]:
            return super().filter_distributions(normalized, files)[:1]

    files = [make_wheel("1"), make_wheel("2")]
    provider = FirstOnly(
        make_coordinator(files),
        target(),
        listing_filter_cache=ListingFilterCache(target_count=2),
    )
    filtered = provider.filter_distributions("pkg", files)

    assert provider.count_matching_records("pkg", VersionRange.full(), filtered) == 1


def test_custom_cache_can_filter_without_populating_native_entries() -> None:
    class Uncached(ListingFilterCache):
        def filtered(
            self,
            package: str,
            python_version: str | None,
            stats: ProviderStats,
            compute: Callable[[], list[tuple[Version, DistFile]]],
        ) -> list[tuple[Version, DistFile]]:
            return compute()

    files = [make_wheel("1")]
    provider = Provider(
        make_coordinator(files), target(), listing_filter_cache=Uncached(target_count=2)
    )

    assert len(provider.filter_distributions("pkg", files)) == 1


def test_custom_tag_admission_and_payload_release_use_normal_filter() -> None:
    class RejectingTags(TagSet):
        def accepts(self, wheel_filename: str) -> bool:
            return False

    files = [make_wheel("1")]
    provider = Provider(
        make_coordinator(files),
        target(),
        listing_filter_cache=ListingFilterCache(target_count=2),
    )
    provider.wheel_tags = RejectingTags((("py3", "none", "any"),))

    assert provider.filter_distributions("pkg", files) == []
    assert provider.stats.excluded_by_wheel_tags == 1

    refused = make_wheel("1", tag="cp312-cp312-win_amd64")
    releasing = Provider(
        make_coordinator([refused]),
        target(),
        listing_filter_cache=ListingFilterCache(target_count=2),
        release_refused_wheels=True,
    )

    assert releasing.filter_distributions("pkg", [refused]) == []
    assert refused.url == ""


def test_tied_sibling_metadata_is_still_checked_after_view_reuse() -> None:
    first = make_wheel("1")
    sibling = make_wheel("1.0")
    port = make_coordinator([first, sibling])

    assert first.metadata_url is not None
    assert sibling.metadata_url is not None
    port.index.store_metadata(
        "pkg", "1", make_metadata("pkg", "1", "a"), metadata_url=first.metadata_url
    )
    port.index.store_metadata(
        "pkg", "1", make_metadata("pkg", "1", "b"), metadata_url=sibling.metadata_url
    )
    cache = ListingFilterCache(target_count=2)
    Provider(port, target(), listing_filter_cache=cache).filter_distributions(
        "pkg", [first, sibling]
    )
    provider = Provider(
        port, target(platform="windows_amd64"), listing_filter_cache=cache
    )
    with pytest.raises(SiblingMetadataDivergenceError):
        provider.get_dependencies("pkg", Version("1"))


def test_metadata_integrity_error_is_not_hidden_by_preparation() -> None:
    wheel = make_wheel("1")
    port = make_coordinator([wheel])
    cache = ListingFilterCache(target_count=2)
    Provider(port, target(), listing_filter_cache=cache).filter_distributions(
        "pkg", [wheel]
    )
    assert wheel.metadata_url is not None
    port.index.store_metadata_error(
        "pkg", "1", MetadataHashMismatchError("wrong digest"), wheel.metadata_url
    )
    provider = Provider(
        port, target(platform="windows_amd64"), listing_filter_cache=cache
    )
    with pytest.raises(MetadataHashMismatchError):
        provider.get_dependencies("pkg", Version("1"))


def test_single_target_does_not_prepare_candidate_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = CandidatePreparation.__init__

    def count(
        prepared: CandidatePreparation,
        distributions: Sequence[tuple[Version, DistFile]],
    ) -> None:
        nonlocal calls
        calls += 1
        original(prepared, distributions)

    monkeypatch.setattr(CandidatePreparation, "__init__", count)
    files = [make_wheel("1")]
    provider = Provider(
        make_coordinator(files),
        target(),
        listing_filter_cache=ListingFilterCache(target_count=1),
    )
    result = provider.filter_distributions("pkg", files)

    assert len(result) == 1
    assert provider.count_matching_records("pkg", VersionRange.full(), result) == 1
    assert calls == 0


def test_python_admission_precedes_shared_tag_projection() -> None:
    files = [
        make_wheel("1", requires_python=">=3.12"),
        make_wheel("2", requires_python="<3.12"),
    ]
    port = make_coordinator(files)
    cache = ListingFilterCache(2, target_count=2)
    first = Provider(port, target("3.11"), listing_filter_cache=cache)
    second = Provider(port, target("3.12"), listing_filter_cache=cache)

    assert [
        str(version) for version, _ in first.filter_distributions("pkg", files)
    ] == ["2"]
    assert [
        str(version) for version, _ in second.filter_distributions("pkg", files)
    ] == ["1"]


@pytest.mark.parametrize(
    ("python", "implementation", "spec"),
    [
        ("3.12", "cpython", PlatformSpec("linux_x86_64", runs_on_libc=(2, 17))),
        (
            "3.12",
            "cpython",
            PlatformSpec("linux_x86_64", libc="musl", runs_on_libc=(1, 2)),
        ),
        ("3.12", "pypy", PlatformSpec("linux_x86_64")),
        ("3.12", "pypy", PlatformSpec("linux_x86_64", pypy_abi="pp80")),
        ("3.13", "cpython", PlatformSpec("linux_x86_64", free_threaded=True)),
        ("3.12", "cpython", PlatformSpec("macos_arm64", runs_on_macos=(14, 0))),
        ("3.12", "cpython", PlatformSpec("windows_arm64")),
    ],
)
def test_preparation_preserves_declared_tag_rules(
    python: str, implementation: str, spec: PlatformSpec
) -> None:
    environment = ResolveTarget.for_declared(
        python_version=python, spec=spec, implementation=implementation
    )
    files = [
        make_wheel("1", tag=tag)
        for tag in (
            "py3-none-any",
            "cp312-cp312-manylinux_2_17_x86_64",
            "cp312-cp312-musllinux_1_2_x86_64",
            "pp312-pypy312_pp73-manylinux_2_17_x86_64",
            "pp312-pypy312_pp80-manylinux_2_17_x86_64",
            "cp313-cp313t-manylinux_2_17_x86_64",
            "cp312-cp312-macosx_14_0_arm64",
            "cp312-cp312-win_arm64",
        )
    ]
    port = make_coordinator(files)
    cached = Provider(
        port, environment, listing_filter_cache=ListingFilterCache(target_count=2)
    )
    fresh = Provider(port, environment)

    assert cached.filter_distributions("pkg", files) == fresh.filter_distributions(
        "pkg", files
    )
    assert cached.stats == fresh.stats
    assert cached.tag_excluded_wheels_by_version == fresh.tag_excluded_wheels_by_version
