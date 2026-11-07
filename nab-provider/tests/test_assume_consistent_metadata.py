"""An explicit release-wide assumption can replace unread sibling metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import (
    ForeignMetadataError,
    IncompatiblePythonError,
    OverrideConflictError,
)
from nab_provider.overrides import IndexOverride
from nab_provider.provider import Provider
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_sdist,
    make_wheel,
    pkg_override,
)
from nab_provider.yanking import mark_yanked

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_provider.records import SdistFile, WheelFile

VERSION = Version("1.0")
LINUX = make_wheel(tag="py3-none-manylinux_2_17_x86_64")
WINDOWS = make_wheel(tag="py3-none-win_amd64")


def target(platform: str, python: str = "3.11") -> ResolveTarget:
    """Return a concrete target with ordinary wheel admission."""
    return ResolveTarget.for_declared(
        python_version=python, spec=PlatformSpec(platform)
    )


def port(linux: str | None = None, windows: str | None = None) -> FakeFetchPort:
    """Publish two platform wheels whose declarations need not agree."""
    assert LINUX.metadata_url is not None
    assert WINDOWS.metadata_url is not None
    return make_coordinator(
        [LINUX, WINDOWS],
        metadata_by_url={
            LINUX.metadata_url: linux or make_metadata("pkg", "1.0", "linux-dep"),
            WINDOWS.metadata_url: windows or make_metadata("pkg", "1.0", "windows-dep"),
        },
    )


@pytest.mark.parametrize("assumed", [False, True])
def test_unread_sibling_dependencies_are_an_explicit_tradeoff(assumed: bool) -> None:
    fetch = port()
    linux = Provider(fetch, target("linux_x86_64"), assume_consistent_metadata=assumed)
    windows = Provider(
        fetch, target("windows_amd64"), assume_consistent_metadata=assumed
    )
    assert set(linux.get_dependencies("pkg", VERSION)) == {"linux-dep"}
    assert set(windows.get_dependencies("pkg", VERSION)) == (
        {"linux-dep"} if assumed else {"windows-dep"}
    )
    assert fetch.index.has_metadata("pkg", "1.0", WINDOWS.metadata_url) is not assumed


def test_reused_declarations_still_evaluate_markers_for_each_target() -> None:
    fetch = port(
        make_metadata(
            "pkg",
            "1.0",
            'linux-dep; sys_platform == "linux"',
            'windows-dep; sys_platform == "win32"',
        )
    )
    linux = Provider(fetch, target("linux_x86_64"), assume_consistent_metadata=True)
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(linux.get_dependencies("pkg", VERSION)) == {"linux-dep"}
    assert set(windows.get_dependencies("pkg", VERSION)) == {"windows-dep"}
    assert not fetch.index.has_metadata("pkg", "1.0", WINDOWS.metadata_url)


def test_representative_requires_python_is_checked_against_each_target() -> None:
    fetch = port(make_metadata("pkg", "1.0", requires_python=">=3.12"))
    linux = Provider(
        fetch, target("linux_x86_64", "3.12"), assume_consistent_metadata=True
    )
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    linux.get_dependencies("pkg", VERSION)
    with pytest.raises(IncompatiblePythonError):
        windows.get_dependencies("pkg", VERSION)


@pytest.mark.parametrize(
    "unread",
    [
        make_metadata("other", "1.0"),
        make_metadata("pkg", "1.0", requires_python=">=3.12"),
    ],
)
def test_unread_sibling_identity_and_python_are_not_independently_checked(
    unread: str,
) -> None:
    fetch = port(windows=unread)
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    assumed = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(assumed.get_dependencies("pkg", VERSION)) == {"linux-dep"}
    strict = Provider(fetch, target("windows_amd64"))
    with pytest.raises((ForeignMetadataError, IncompatiblePythonError)):
        strict.get_dependencies("pkg", VERSION)


def test_listing_python_requirement_remains_authoritative() -> None:
    wheel = make_wheel(tag="py3-none-win_amd64", requires_python=">=3.12")
    fetch = make_coordinator([LINUX, wheel], metadata_text=make_metadata("pkg", "1.0"))
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert windows.fetch_versions("pkg") == []


def test_a_representative_does_not_make_foreign_wheels_installable() -> None:
    fetch = make_coordinator([LINUX], metadata_text=make_metadata("pkg", "1.0"))
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert windows.fetch_versions("pkg") == []


@pytest.mark.parametrize(
    ("package_value", "index_value"), [(False, None), (None, False)]
)
def test_scoped_false_overrides_global_true(
    package_value: bool | None, index_value: bool | None
) -> None:
    fetch = port()
    fetch.index.store_listing_index("pkg", "private")
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    windows = Provider(
        fetch,
        target("windows_amd64"),
        assume_consistent_metadata=True,
        package_overrides=(
            pkg_override("pkg", assume_consistent_metadata=package_value),
        ),
        index_overrides={
            "private": IndexOverride(assume_consistent_metadata=index_value)
        },
    )
    assert set(windows.get_dependencies("pkg", VERSION)) == {"windows-dep"}


@pytest.mark.parametrize("package_value", [True, False])
def test_package_and_index_disagreement_is_not_silently_resolved(
    package_value: bool,
) -> None:
    fetch = port()
    fetch.index.store_listing_index("pkg", "private")
    provider = Provider(
        fetch,
        target("linux_x86_64"),
        package_overrides=(
            pkg_override("pkg", assume_consistent_metadata=package_value),
        ),
        index_overrides={"private": IndexOverride(assume_consistent_metadata=False)},
    )
    with pytest.raises(OverrideConflictError, match="assume-consistent-metadata"):
        provider.get_dependencies("pkg", VERSION)


@pytest.mark.parametrize("global_value", [False, True])
def test_unrelated_consistency_rule_keeps_the_global_policy(global_value: bool) -> None:
    fetch = port()
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    windows = Provider(
        fetch,
        target("windows_amd64"),
        assume_consistent_metadata=global_value,
        package_overrides=(pkg_override("other", assume_consistent_metadata=True),),
    )
    assert set(windows.get_dependencies("pkg", VERSION)) == (
        {"linux-dep"} if global_value else {"windows-dep"}
    )


def test_replaced_source_cannot_keep_answering_for_a_release() -> None:
    fetch = port()
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    fetch.index.store_metadata(
        "pkg",
        "1.0",
        make_metadata("pkg", "1.0", "changed"),
        metadata_url=LINUX.metadata_url,
    )
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(windows.get_dependencies("pkg", VERSION)) == {"windows-dep"}


def test_replaced_listing_discards_the_old_representative() -> None:
    fetch = port()
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    fetch.index.store_listing("pkg", [WINDOWS])
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(windows.get_dependencies("pkg", VERSION)) == {"windows-dep"}


def test_dependency_only_assumption_does_not_change_wheel_semantics() -> None:
    fetch = port()
    Provider(
        fetch, target("linux_x86_64"), assume_universal_deps=True
    ).get_dependencies("pkg", VERSION)
    windows = Provider(fetch, target("windows_amd64"), assume_universal_deps=True)
    assert set(windows.get_dependencies("pkg", VERSION)) == {"windows-dep"}


def test_source_metadata_does_not_borrow_the_wheel_representative() -> None:
    fetch = make_coordinator(
        [LINUX, make_sdist()],
        metadata_text=make_metadata("pkg", "1.0", "wheel-dep"),
        sdist_pkg_info=make_metadata(
            "pkg", "1.0", "source-dep", metadata_version="2.4"
        ),
    )
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(windows.get_dependencies("pkg", VERSION)) == {"source-dep"}


def test_direct_metadata_classification_does_not_require_a_listing() -> None:
    provider = Provider(
        make_coordinator(), target("linux_x86_64"), assume_consistent_metadata=True
    )
    provider.parse_and_cache_metadata(
        ("pkg", VERSION), make_metadata("pkg", "1.0", "dep")
    )
    assert set(provider.deps_cache["pkg", VERSION]) == {"dep"}


@pytest.mark.parametrize("url", [LINUX.metadata_url, None])
def test_failed_representative_source_is_not_reused(url: str | None) -> None:
    fetch = port()
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    fetch.index.store_metadata_error(
        "pkg", "1.0", ValueError("source unavailable"), metadata_url=url
    )
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(windows.get_dependencies("pkg", VERSION)) == {"windows-dep"}


def test_representative_from_a_replaced_index_is_not_reused() -> None:
    fetch = port()
    fetch.index.store_listing_index("pkg", "first")
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    fetch.index.store_listing_index("pkg", "second")
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(windows.get_dependencies("pkg", VERSION)) == {"windows-dep"}


def test_an_unrelated_listing_does_not_discard_the_representative() -> None:
    fetch = port()
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    fetch.index.store_listing("other", [])
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(windows.get_dependencies("pkg", VERSION)) == {"linux-dep"}
    assert not fetch.index.has_metadata("pkg", "1.0", WINDOWS.metadata_url)


def test_version_level_metadata_without_wheel_origin_is_not_a_representative() -> None:
    fetch = make_coordinator([make_sdist()])
    fetch.index.store_metadata("pkg", "1.0", make_metadata("pkg", "1.0", "source-dep"))
    provider = Provider(fetch, target("linux_x86_64"), assume_consistent_metadata=True)
    assert set(provider.get_dependencies("pkg", VERSION)) == {"source-dep"}
    files = fetch.index.get_listing("pkg")
    assert files is not None
    context = fetch.index.consistent_metadata_context("pkg", files)
    assert (
        fetch.index.get_consistent_wheel_metadata("pkg", "1.0", context=context) is None
    )


def test_different_releases_do_not_share_representatives() -> None:
    second = make_wheel("2.0", tag="py3-none-win_amd64")
    fetch = make_coordinator(
        [LINUX, second],
        metadata_by_version={
            "1.0": make_metadata("pkg", "1.0", "first-dep"),
            "2.0": make_metadata("pkg", "2.0", "second-dep"),
        },
    )
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(windows.get_dependencies("pkg", Version("2.0"))) == {"second-dep"}


def test_explicit_yank_admission_is_retained_when_reusing_metadata() -> None:
    withdrawn = mark_yanked(WINDOWS, reason="withdrawn")
    fetch = make_coordinator(
        [LINUX, withdrawn], metadata_text=make_metadata("pkg", "1.0", "dep")
    )
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    windows = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    windows.yank_admissions = frozenset({("pkg", VERSION)})
    assert set(windows.get_dependencies("pkg", VERSION)) == {"dep"}
    assert not fetch.index.has_metadata("pkg", "1.0", WINDOWS.metadata_url)


def test_listing_replacement_rejects_late_source_publication() -> None:
    fetch = make_coordinator(
        [LINUX],
        metadata_by_url={
            LINUX.metadata_url: make_metadata("pkg", "1.0", "old-dep"),
            WINDOWS.metadata_url: make_metadata("pkg", "1.0", "new-dep"),
        },
    )
    original_read = fetch.index.get_parsed_metadata

    def replace_after_read(package: str, version: str, source_text: str) -> None:
        assert original_read(package, version, source_text) is None
        fetch.index.store_listing("pkg", [WINDOWS])

    old = Provider(fetch, target("linux_x86_64"), assume_consistent_metadata=True)
    with patch.object(
        fetch.index, "get_parsed_metadata", side_effect=replace_after_read
    ):
        assert set(old.get_dependencies("pkg", VERSION)) == {"old-dep"}
    current = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(current.get_dependencies("pkg", VERSION)) == {"new-dep"}


def test_listing_replaced_before_filter_does_not_receive_a_new_context() -> None:
    fetch = make_coordinator(
        [LINUX],
        metadata_by_url={
            LINUX.metadata_url: make_metadata("pkg", "1.0", "old-dep"),
            WINDOWS.metadata_url: make_metadata("pkg", "1.0", "new-dep"),
        },
    )
    old = Provider(fetch, target("linux_x86_64"), assume_consistent_metadata=True)
    original_filter = old.filter_distributions

    def replace_before_filter(
        package: str, files: Sequence[WheelFile | SdistFile]
    ) -> list[tuple[Version, WheelFile | SdistFile]]:
        fetch.index.store_listing("pkg", [WINDOWS])
        return original_filter(package, files)

    with patch.object(old, "filter_distributions", side_effect=replace_before_filter):
        assert set(old.get_dependencies("pkg", VERSION)) == {"old-dep"}
    current = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(current.get_dependencies("pkg", VERSION)) == {"new-dep"}


def test_old_provider_does_not_borrow_a_new_listing_source() -> None:
    fetch = make_coordinator(
        [LINUX],
        metadata_by_url={
            LINUX.metadata_url: make_metadata("pkg", "1.0", "old-dep"),
            WINDOWS.metadata_url: make_metadata("pkg", "1.0", "new-dep"),
        },
    )
    old = Provider(fetch, target("linux_x86_64"), assume_consistent_metadata=True)
    old.fetch_versions("pkg")
    fetch.index.store_listing("pkg", [WINDOWS])
    current = Provider(fetch, target("windows_amd64"), assume_consistent_metadata=True)
    assert set(current.get_dependencies("pkg", VERSION)) == {"new-dep"}
    assert set(old.get_dependencies("pkg", VERSION)) == {"old-dep"}


def test_index_change_rejects_late_source_publication() -> None:
    fetch = make_coordinator([LINUX], metadata_text=make_metadata("pkg", "1.0", "dep"))
    Provider(
        fetch, target("linux_x86_64"), assume_consistent_metadata=True
    ).get_dependencies("pkg", VERSION)
    files = fetch.index.get_listing("pkg")
    assert files is not None
    old_context = fetch.index.consistent_metadata_context("pkg", files)
    text = fetch.index.get_metadata("pkg", "1.0", LINUX.metadata_url)
    assert text is not None
    fetch.index.store_listing_index("pkg", "replacement")
    fetch.index.store_consistent_wheel_metadata(
        "pkg", "1.0", LINUX.metadata_url, text, context=old_context
    )
    current_context = fetch.index.consistent_metadata_context("pkg", files)
    assert current_context is not old_context
    assert (
        fetch.index.get_consistent_wheel_metadata("pkg", "1.0", context=current_context)
        is None
    )
