"""Legacy ZIP admission respects scoped policies and explains relevant failures."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

from nab_provider._provider import metadata_resolver
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import OverrideConflictError, UnsupportedSdistError
from nab_provider.overrides import IndexOverride
from nab_provider.policy import DistPolicy
from nab_provider.provider import ListingFilterCache, Provider
from nab_provider.records import SdistFile
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    make_coordinator,
    make_metadata,
    make_sdist,
    make_wheel,
    pkg_override,
)

if TYPE_CHECKING:
    from nab_provider.diagnostics import Diagnostic
    from nab_provider.overrides import PackageOverride


def zip_sdist(
    version: str = "1.0",
    *,
    requires_python: str | None = None,
    upload_time: str | None = "2020-01-01T00:00:00Z",
) -> SdistFile:
    """Return one indexed ZIP sdist without replacing its metadata machinery."""
    filename = f"pkg-{version}.zip"
    return SdistFile(
        filename,
        f"https://example.com/{filename}",
        version,
        requires_python,
        upload_time,
    )


@pytest.mark.parametrize("pythons", [1, 2])
@pytest.mark.parametrize("allowed", [False, True])
def test_default_and_global_admission(pythons: int, *, allowed: bool) -> None:
    coordinator = make_coordinator([zip_sdist(), make_sdist("2.0")])
    provider = Provider(
        coordinator,
        allow_zip_sdists=allowed,
        listing_filter_cache=ListingFilterCache(pythons),
    )

    files = provider.fetch_versions("pkg")

    assert [version for version, _ in files] == (
        [Version("2.0"), Version("1.0")] if allowed else [Version("2.0")]
    )
    assert provider.stats.excluded_zip_sdists == (0 if allowed else 1)

    diagnosis = provider.diagnose_listing("pkg")

    assert diagnosis is not None
    assert diagnosis.kept == {version for version, _ in files}
    assert provider.stats.excluded_zip_sdists == (0 if allowed else 1)


@pytest.mark.parametrize("allowed", [False, True])
def test_shared_listing_keeps_target_python_verdicts_separate(*, allowed: bool) -> None:
    zipped = zip_sdist(requires_python=">=3.11")
    coordinator = make_coordinator([zipped])
    cache = ListingFilterCache(2)
    providers = [
        Provider(
            coordinator,
            target=ResolveTarget.for_declared(
                python_version=python, spec=PlatformSpec("linux_x86_64")
            ),
            allow_zip_sdists=allowed,
            listing_filter_cache=cache,
        )
        for python in ("3.10", "3.12")
    ]

    assert providers[0].fetch_versions("pkg") == []
    assert providers[0].stats.excluded_by_python == 1
    assert providers[0].stats.excluded_zip_sdists == 0

    assert providers[1].fetch_versions("pkg") == (
        [(Version("1.0"), zipped)] if allowed else []
    )
    assert providers[1].stats.excluded_by_python == 0
    assert providers[1].stats.excluded_zip_sdists == (0 if allowed else 1)


@pytest.mark.parametrize("global_allow", [False, True])
@pytest.mark.parametrize("override_allow", [False, True])
@pytest.mark.parametrize("surface", ["package", "index"])
def test_overrides_replace_the_global_default(
    surface: str, *, global_allow: bool, override_allow: bool
) -> None:
    coordinator = make_coordinator([zip_sdist("1.0"), zip_sdist("2.0")])
    packages = (
        [pkg_override("pkg<2", allow_zip_sdists=override_allow)]
        if surface == "package"
        else []
    )
    indexes = (
        {"pypi": IndexOverride(allow_zip_sdists=override_allow)}
        if surface == "index"
        else {}
    )
    provider = Provider(
        coordinator,
        allow_zip_sdists=global_allow,
        package_overrides=packages,
        index_overrides=indexes,
    )
    expected = []
    second_allowed = override_allow if surface == "index" else global_allow
    if second_allowed:
        expected.append(Version("2.0"))
    if override_allow:
        expected.append(Version("1.0"))

    assert [version for version, _ in provider.fetch_versions("pkg")] == expected


def test_package_and_index_settings_conflict() -> None:
    provider = Provider(
        make_coordinator([zip_sdist()]),
        package_overrides=[pkg_override("pkg", allow_zip_sdists=True)],
        index_overrides={"pypi": IndexOverride(allow_zip_sdists=False)},
    )
    with pytest.raises(OverrideConflictError, match="allow-zip-sdists"):
        provider.fetch_versions("pkg")


def test_unset_overrides_preserve_global_admission() -> None:
    provider = Provider(
        make_coordinator([zip_sdist()]),
        allow_zip_sdists=True,
        package_overrides=[pkg_override("pkg", requires_python="")],
        index_overrides={"other": IndexOverride(allow_zip_sdists=False)},
    )

    assert len(provider.fetch_versions("pkg")) == 1


def failure(provider: Provider, spec: str = "") -> Diagnostic:
    """Return the diagnostic after a real failed version selection."""
    assert provider.choose_version("pkg", SpecifierSet(spec).to_range()) is None

    diagnostic = provider.get_no_versions_reason("pkg")
    assert diagnostic is not None
    return diagnostic


def test_default_failure_suggests_a_scoped_exception() -> None:
    provider = Provider(make_coordinator([zip_sdist()]))
    diagnostic = failure(provider)

    assert diagnostic.short == "legacy ZIP sdists require allow-zip-sdists = true"
    assert diagnostic.remedy == 'set packages."pkg".allow-zip-sdists = true'
    assert provider.stats.sdist_pkg_info_fetched == 0


@pytest.mark.parametrize("allowed", [False, True])
def test_effective_default_without_overrides(*, allowed: bool) -> None:
    provider = Provider(make_coordinator(), allow_zip_sdists=allowed)

    assert provider.effective_allow_zip_sdists("pkg", Version("1.0")) is allowed


def test_unreadable_non_zip_page_keeps_its_format_diagnostic() -> None:
    coordinator = make_coordinator()
    coordinator.index.store_listing("pkg", [], unreadable_only=True)
    diagnostic = failure(Provider(coordinator))

    assert diagnostic.short == "no file the index served is one nab can read"
    assert diagnostic.remedy is None


@pytest.mark.parametrize("pythons", [1, 2])
@pytest.mark.parametrize("cause", ["range", "python", "cutoff", "dist-policy"])
def test_irrelevant_zip_has_no_permission_remedy(cause: str, pythons: int) -> None:
    provider = Provider(
        make_coordinator(
            [zip_sdist(requires_python=">=3.99" if cause == "python" else None)]
        ),
        target=ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_x86_64")
        ),
        uploaded_prior_to=(
            datetime(2010, 1, 1, tzinfo=timezone.utc) if cause == "cutoff" else None
        ),
        dist_policy=(
            DistPolicy.WHEEL_ONLY
            if cause == "dist-policy"
            else DistPolicy.WHEEL_OR_SDIST
        ),
        listing_filter_cache=ListingFilterCache(pythons),
    )
    diagnostic = failure(provider, "==2.0" if cause == "range" else "")

    assert "allow-zip-sdists" not in "\n".join(
        (diagnostic.short, *diagnostic.detail, diagnostic.remedy or "")
    )


def test_allowed_zip_has_no_permission_remedy_when_other_policy_refuses() -> None:
    provider = Provider(
        make_coordinator([zip_sdist()]),
        allow_zip_sdists=True,
        dist_policy=DistPolicy.WHEEL_ONLY,
    )
    diagnostic = failure(provider)

    assert diagnostic.remedy is not None
    assert "allow-zip-sdists" not in diagnostic.remedy


@pytest.mark.parametrize(
    ("packages", "indexes", "remedy"),
    [
        (
            [
                pkg_override(
                    "pkg", allow_zip_sdists=False, source_label='packages."pkg"'
                )
            ],
            {},
            'set allow-zip-sdists = true on packages."pkg"',
        ),
        (
            [],
            {"pypi": IndexOverride(allow_zip_sdists=False)},
            'set index."pypi".allow-zip-sdists = true',
        ),
        (
            [pkg_override("pkg>=2", allow_zip_sdists=False)],
            {},
            'add a package-rules entry with match = ["pkg==1.0"] and allow-zip-sdists = true',
        ),
        (
            [pkg_override("pkg", requires_python="", name_keyed=True)],
            {},
            "add allow-zip-sdists = true to pkg",
        ),
    ],
)
def test_failure_names_the_existing_override_surface(
    packages: list[PackageOverride], indexes: dict[str, IndexOverride], remedy: str
) -> None:
    provider = Provider(
        make_coordinator([zip_sdist()]),
        package_overrides=packages,
        index_overrides=indexes,
    )

    assert failure(provider).remedy == remedy


@pytest.mark.parametrize("zip_first", [False, True])
def test_gzip_tar_preferred_for_metadata(*, zip_first: bool) -> None:
    tar = make_sdist()
    zipped = zip_sdist()
    files = [zipped, tar] if zip_first else [tar, zipped]
    versions = [(Version("1.0"), dist) for dist in files]

    assert metadata_resolver.find_sdist(versions, Version("1.0")) is tar
    assert metadata_resolver.pick_dist(files, None) is tar

    provider = Provider(make_coordinator(files), allow_zip_sdists=True)
    listing = provider.fetch_versions("pkg")

    assert listing[0][1] is tar

    indexed = metadata_resolver.version_dists(provider, "pkg", listing)

    assert indexed.picked[Version("1.0")] is tar


def test_zip_only_and_wheel_metadata_selection() -> None:
    zipped = zip_sdist()
    versions = [(Version("1.0"), zipped)]

    assert metadata_resolver.find_sdist(versions, Version("1.0")) is zipped
    assert metadata_resolver.find_sdist(versions, Version("2.0")) is None

    wheel = make_wheel()

    assert metadata_resolver.pick_dist([zipped, wheel], None) is wheel


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("assumed", [False, True])
def test_zip_admission_and_foreign_dependency_metadata_require_separate_opt_ins(
    *, allowed: bool, assumed: bool
) -> None:
    wheel = make_wheel(tag="cp311-cp311-manylinux_2_17_x86_64")
    zipped = zip_sdist()
    coordinator = make_coordinator(
        [wheel, zipped],
        metadata_text=make_metadata("pkg", "1.0", "wheel-dep"),
        sdist_pkg_info=(
            make_metadata("pkg", "1.0", metadata_version="2.4")
            + "Dynamic: Requires-Dist\n"
        ),
    )
    provider = Provider(
        coordinator,
        ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("macos_arm64")
        ),
        allow_zip_sdists=allowed,
        assume_universal_deps=assumed,
    )

    assert provider.fetch_versions("pkg") == (
        [(Version("1.0"), zipped)] if allowed else []
    )

    if allowed and assumed:
        assert set(provider.get_dependencies("pkg", Version("1.0"))) == {"wheel-dep"}
        assert len(coordinator.calls_to("request_metadata")) == 1
    elif allowed:
        with pytest.raises(UnsupportedSdistError):
            provider.get_dependencies("pkg", Version("1.0"))
        assert coordinator.calls_to("request_metadata") == []
    else:
        assert provider.choose_version("pkg", SpecifierSet().to_range()) is None
        assert coordinator.calls_to("request_metadata") == []

    assert coordinator.calls_to("request_built_metadata") == []


def test_zip_siblings_keep_the_first_metadata_source() -> None:
    first, second = zip_sdist(), zip_sdist("1.0.0")
    versions = [(Version("1.0"), dist) for dist in (first, second)]

    assert metadata_resolver.find_sdist(versions, Version("1.0")) is first
