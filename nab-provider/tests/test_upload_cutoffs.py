"""Upload date filtering and scoped cutoff overrides in the provider."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from packaging.version import Version

from nab_provider.errors import InvalidUploadTimeError, MetadataError
from nab_provider.overrides import IndexOverride
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator, make_wheel, pkg_override

_MARCH_CUTOFF = datetime(2024, 3, 1, tzinfo=timezone.utc)


class TestUploadedPriorTo:
    def test_filters_newer_wheels(self) -> None:
        """Wheels uploaded after the cutoff are excluded."""
        wheels = [
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [Version("1.0")]

    def test_keeps_older_wheels(self) -> None:
        """Wheels uploaded before the cutoff are kept."""
        wheels = [
            make_wheel("2.0", upload_time="2024-01-15T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [Version("2.0"), Version("1.0")]

    def test_excludes_wheels_without_upload_time(self) -> None:
        """Wheels missing upload-time are excluded when cutoff is set."""
        wheels = [
            make_wheel("2.0", upload_time=None),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [Version("1.0")]

    def test_keeps_local_artifact_without_upload_time(self) -> None:
        """A local file:// artifact carries no upload time and is kept."""
        wheels = [
            make_wheel(
                "1.0",
                upload_time=None,
                local_path=Path("/wheelhouse/pkg-1.0-py3-none-any.whl"),
            ),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [Version("1.0")]

    def test_excludes_remote_but_keeps_local_without_upload_time(self) -> None:
        """The cutoff drops a remote no-upload-time wheel but keeps a local one."""
        wheels = [
            make_wheel("2.0", upload_time=None),
            make_wheel(
                "1.0",
                upload_time=None,
                local_path=Path("/wheelhouse/pkg-1.0-py3-none-any.whl"),
            ),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [Version("1.0")]

    def test_no_cutoff_ignores_upload_time(self) -> None:
        """Without uploaded_prior_to, upload_time is irrelevant."""
        wheels = [
            make_wheel("2.0", upload_time=None),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [Version("2.0"), Version("1.0")]

    def test_exact_cutoff_excludes(self) -> None:
        """A wheel uploaded at exactly the cutoff time is excluded."""
        wheels = [
            make_wheel("1.0", upload_time="2024-03-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        assert provider.fetch_versions("foo") == []

    @pytest.mark.parametrize(
        "fraction", ["", ".1", ".12", ".123", ".1234", ".12345", ".123456"]
    )
    def test_fractional_seconds(self, fraction: str) -> None:
        """PEP 700 permits 0 through 6 fractional digits, and all of them parse."""
        wheels = [
            make_wheel("1.0", upload_time=f"2024-01-15T12:30:45{fraction}Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [Version("1.0")]
        assert provider.stats.excluded_by_time == 0

    def test_invalid_upload_time_excluded(self) -> None:
        """Wheels with unparseable upload-time are excluded."""
        wheels = [
            make_wheel("1.0", upload_time="not-a-date"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        assert provider.fetch_versions("foo") == []

    def test_malformed_fraction_excluded_not_fatal(self) -> None:
        """An empty fraction is malformed, so the wheel is skipped, not fatal."""
        wheels = [make_wheel("1.0", upload_time="2024-01-15T12:30:45.")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        assert provider.fetch_versions("foo") == []
        assert provider.stats.excluded_by_time == 1

    def test_naive_upload_time_raises_when_cutoff_active(self) -> None:
        """A timezone-naive upload-time violates PEP 700, so it is a hard error."""
        wheels = [
            make_wheel("1.0", upload_time="2024-01-01T00:00:00"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        with pytest.raises(InvalidUploadTimeError, match="2024-01-01T00:00:00"):
            provider.fetch_versions("foo")

    def test_date_only_upload_time_reaches_the_rewriting_parser(self) -> None:
        """A date-only ``Z`` time still parses, through the fallback, and is naive."""
        wheels = [
            make_wheel("1.0", upload_time="2024-01-01Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator, uploaded_prior_to=_MARCH_CUTOFF)
        with pytest.raises(InvalidUploadTimeError, match="2024-01-01Z"):
            provider.fetch_versions("foo")

    def test_naive_upload_time_not_metadata_error(self) -> None:
        """The error is not a MetadataError, so look-ahead cannot swallow it."""
        assert not issubclass(InvalidUploadTimeError, MetadataError)

    def test_naive_upload_time_ignored_without_cutoff(self) -> None:
        """With no cutoff active the naive upload-time is never inspected."""
        wheels = [
            make_wheel("1.0", upload_time="2024-01-01T00:00:00"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [Version("1.0")]


class TestUploadedPriorToOverrides:
    """Per-package / per-index overrides bypass or replace the global cutoff."""

    def test_override_disables_global_cutoff(self) -> None:
        """An override with the ``false`` form keeps every wheel even when
        the global cutoff would exclude them."""
        wheels = [
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(
            coordinator,
            uploaded_prior_to=_MARCH_CUTOFF,
            package_overrides=(pkg_override("foo", uploaded_prior_to_disabled=True),),
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [Version("2.0"), Version("1.0")]

    def test_override_applies_per_package_cutoff(self) -> None:
        """A per-package datetime override replaces the global cutoff
        for that package only."""
        wheels = [
            make_wheel("3.0", upload_time="2024-08-01T00:00:00Z"),
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        package_cutoff = datetime(2024, 7, 1, tzinfo=timezone.utc)
        provider = Provider(
            coordinator,
            uploaded_prior_to=_MARCH_CUTOFF,
            package_overrides=(pkg_override("foo", uploaded_prior_to=package_cutoff),),
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        # 3.0 (Aug) excluded by package cutoff (Jul); 2.0 (Jun) kept
        # because the package override allows up to Jul; 1.0 (Jan) kept.
        assert versions == [Version("2.0"), Version("1.0")]

    def test_version_scoped_cutoff(self) -> None:
        """A version-scoped override applies only inside its range."""
        wheels = [
            make_wheel("3.0", upload_time="2024-08-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-08-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(
            coordinator,
            uploaded_prior_to=_MARCH_CUTOFF,
            package_overrides=(
                pkg_override("foo <= 2", uploaded_prior_to_disabled=True),
            ),
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        # 1.0 is inside the override range (cutoff disabled) so it stays;
        # 3.0 is outside it and the global cutoff (Mar) drops its Aug upload.
        assert versions == [Version("1.0")]

    def test_override_only_applies_to_named_package(self) -> None:
        """Other packages keep using the global cutoff."""
        wheels = [
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="bar")
        provider = Provider(
            coordinator,
            uploaded_prior_to=_MARCH_CUTOFF,
            package_overrides=(pkg_override("foo", uploaded_prior_to_disabled=True),),
        )
        versions = [v for v, _ in provider.fetch_versions("bar")]
        # ``bar`` has no override, so the global cutoff still applies.
        assert versions == [Version("1.0")]

    def test_per_index_cutoff_takes_effect(self) -> None:
        """A per-index override changes behaviour vs the global default."""
        wheels = [
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        coordinator.index.store_listing_index("foo", "internal")
        provider = Provider(
            coordinator,
            index_overrides={
                "internal": IndexOverride(uploaded_prior_to=_MARCH_CUTOFF)
            },
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        # The per-index cutoff (Mar) drops 2.0 (Jun) even though the global
        # cutoff is unset.
        assert versions == [Version("1.0")]

    def test_per_index_applies_to_every_package(self) -> None:
        """A per-index override governs every package served from it."""
        index = IndexOverride(uploaded_prior_to=_MARCH_CUTOFF)
        for pkg in ("foo", "bar"):
            wheels = [
                make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
                make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
            ]
            coordinator = make_coordinator(wheels, package=pkg)
            coordinator.index.store_listing_index(pkg, "internal")
            provider = Provider(coordinator, index_overrides={"internal": index})
            versions = [v for v, _ in provider.fetch_versions(pkg)]
            assert versions == [Version("1.0")]

    def test_override_with_no_global_cutoff(self) -> None:
        """A per-package cutoff with no global cutoff still filters."""
        wheels = [
            make_wheel("2.0", upload_time="2024-06-01T00:00:00Z"),
            make_wheel("1.0", upload_time="2024-01-01T00:00:00Z"),
        ]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(
            coordinator,
            uploaded_prior_to=None,
            package_overrides=(pkg_override("foo", uploaded_prior_to=_MARCH_CUTOFF),),
        )
        versions = [v for v, _ in provider.fetch_versions("foo")]
        assert versions == [Version("1.0")]
