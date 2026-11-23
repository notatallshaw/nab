"""Diagnose unavailable extras and releases with unreadable metadata."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nab_provider._provider.listing_diagnosis import MetadataBlock
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import MetadataError
from nab_provider.policy import ExtrasMode
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator, make_metadata, make_sdist, make_wheel
from nab_resolver.resolver import Resolver

from .provider_fixtures import PYTHON_312, SECURITY_EXTRA_METADATA


class TestExtrasInvalidMetadata:
    """Cover the invalid-metadata skip paths in extras pick mode."""

    BAD_META = (
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar (>=1.*)\n"
    )

    def test_skip_when_invalid_metadata_cached(self) -> None:
        """Cached invalid metadata short-circuits the candidate loop."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={
                "2.0": SECURITY_EXTRA_METADATA,
                "1.0": SECURITY_EXTRA_METADATA,
            },
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312, extras_mode=ExtrasMode.WARN)
        provider._invalid_metadata[("foo", Version("2.0"))] = MetadataBlock("stub")
        version = provider.choose_version("foo[security]", VersionRange.full())

        assert version == Version("1.0")

    def test_skip_when_get_dependencies_marks_invalid(self) -> None:
        """Parseable but rejected metadata fetched mid-loop skips the candidate."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": self.BAD_META, "1.0": SECURITY_EXTRA_METADATA},
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312, extras_mode=ExtrasMode.WARN)
        version = provider.choose_version("foo[security]", VersionRange.full())
        assert version == Version("1.0")

    def test_user_extra_skips_invalid_metadata(self) -> None:
        """A user-requested extra must not pick a known-invalid version.

        get_dependencies raises MetadataError for a version in
        _invalid_metadata, so returning 2.0 here crashes when the proxy
        later fetches its dependencies.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={
                "2.0": SECURITY_EXTRA_METADATA,
                "1.0": SECURITY_EXTRA_METADATA,
            },
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            extras_mode=ExtrasMode.WARN,
            root_extras={("foo", "security")},
        )
        provider._invalid_metadata[("foo", Version("2.0"))] = MetadataBlock("stub")
        version = provider.choose_version("foo[security]", VersionRange.full())

        assert version == Version("1.0")

    def test_user_extra_skips_unsupported_sdist(self) -> None:
        """Only the 1.0 wheel has usable metadata for the requested security extra."""
        dists = [make_sdist("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            dists,
            metadata_by_version={"1.0": SECURITY_EXTRA_METADATA},
            sdist_pkg_info=make_metadata("foo", "2.0", "dep-a>=1.0", "dep-b"),
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            root_extras={("foo", "security")},
        )
        version = provider.choose_version("foo[security]", VersionRange.full())
        assert version == Version("1.0")

    def test_user_extra_skips_when_get_dependencies_marks_invalid(self) -> None:
        """A user-requested extra skips metadata rejected mid-loop.

        The invalid metadata for 2.0 is not cached before the pick, so
        only the fetch inside the candidate loop can reveal it.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": self.BAD_META, "1.0": SECURITY_EXTRA_METADATA},
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            extras_mode=ExtrasMode.WARN,
            root_extras={("foo", "security")},
        )
        version = provider.choose_version("foo[security]", VersionRange.full())
        assert version == Version("1.0")

    def test_user_extra_skips_version_with_no_metadata_source(self) -> None:
        """A user extra skips a version lacking both PEP 658 metadata and an sdist.

        resolve_metadata raises a generic MetadataError before
        get_dependencies records the version, so has_invalid_metadata stays
        False. The pick must still skip 2.0 and choose 1.0.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"1.0": SECURITY_EXTRA_METADATA},
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            root_extras={("foo", "security")},
        )
        version = provider.choose_version("foo[security]", VersionRange.full())
        assert version == Version("1.0")

    def test_user_extra_no_metadata_resolves_end_to_end(self) -> None:
        """A user extra whose top version lacks metadata still resolves.

        The extras proxy sorts before its base, so pkg[feature] is decided
        first. With 2.0 unreadable, the proxy must fall to 1.0.
        """
        metadata = (
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nProvides-Extra: feature\n"
        )
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={"1.0": metadata},
            package="pkg",
        )
        root_reqs = {
            "pkg": VersionRange.full(admit_arbitrary=False),
            "pkg[feature]": VersionRange.full(admit_arbitrary=False),
        }
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            root_requirements=root_reqs,
            root_extras={("pkg", "feature")},
        )
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        pins = resolver.resolve(root_reqs)
        assert pins["pkg"] == Version("1.0")


class TestExtrasProxyDiagnostics:
    """An extras proxy left with no version says why, in its own name.

    ``choose_version`` hands ``foo[bar]`` to the extras chooser before either
    listing-level record is made, so without a record of its own the tree
    names a package the ``Diagnostics:`` section cannot.
    """

    def _backtracking(self, metadata_by_version: dict[str, str]) -> Provider:
        """A BACKTRACK-mode provider over the versions ``metadata`` describes."""
        coordinator = make_coordinator(
            [make_wheel(version) for version in metadata_by_version],
            metadata_by_version=metadata_by_version,
            package="foo",
        )
        return Provider(
            coordinator, target=PYTHON_312, extras_mode=ExtrasMode.BACKTRACK
        )

    def test_no_version_declares_the_extra(self) -> None:
        """The base publishes versions, and none of them provides the extra."""
        provider = self._backtracking(
            {
                "2.0": make_metadata("foo", "2.0", "bar>=1.0"),
                "1.0": make_metadata("foo", "1.0", "bar>=1.0"),
            }
        )
        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        diagnostic = provider.get_no_versions_reason("foo[security]")
        assert diagnostic is not None
        assert diagnostic.short == "no version of foo declares this extra"
        assert diagnostic.detail[0] == (
            "no version of foo in the range the resolve considered declares"
            " Provides-Extra: security"
        )
        assert diagnostic.detail[1].startswith("the range considered: ")

    def test_every_declaring_version_has_unreadable_metadata(self) -> None:
        """Metadata nothing could read is told apart from an absent extra."""
        provider = self._backtracking({"2.0": "not: metadata\n"})
        with pytest.raises(MetadataError):
            provider.get_dependencies("foo", Version("2.0"))

        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        diagnostic = provider.get_no_versions_reason("foo[security]")
        assert diagnostic is not None
        assert diagnostic.short == (
            "every version in range was rejected on its metadata"
        )
        assert "foo==2.0" in diagnostic.detail[0]

    def test_a_metadata_error_raised_by_the_chooser_is_recorded(self) -> None:
        """The chooser's own failed read is the record, with no earlier ask."""
        provider = self._backtracking({"2.0": "not: metadata\n"})
        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        diagnostic = provider.get_no_versions_reason("foo[security]")
        assert diagnostic is not None
        assert diagnostic.short == (
            "every version in range was rejected on its metadata"
        )

    def test_the_base_was_narrowed_off_every_declaring_version(self) -> None:
        """The diagnostic identifies a release that provides the extra outside the current range."""
        coordinator = make_coordinator(
            [make_wheel("2.0"), make_wheel("1.0")],
            metadata_by_version={
                "2.0": make_metadata("foo", "2.0", "bar>=1.0"),
                "1.0": SECURITY_EXTRA_METADATA,
            },
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            root_requirements={"foo": VersionRange.full(admit_arbitrary=False)},
            root_extras={("foo", "security")},
        )
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet(">=2").to_range()}, {}
        )
        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        diagnostic = provider.get_no_versions_reason("foo[security]")
        assert diagnostic is not None
        assert diagnostic.short == (
            "another requirement holds foo where this extra is undeclared"
        )
        assert diagnostic.detail == (
            "foo 1.0 declares the extra, and the resolve cannot choose that version",
        )

    def test_a_filter_that_emptied_the_base_listing_is_named(self) -> None:
        """The upload cutoff remedy applies to the base package."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            uploaded_prior_to=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        diagnostic = provider.get_no_versions_reason("foo[security]")
        assert diagnostic is not None
        assert (
            diagnostic.short == "uploaded-prior-to excluded every file; none is dated"
        )
        assert diagnostic.remedy == 'set packages."foo".uploaded-prior-to = false'

    def test_a_base_no_index_serves_is_reported_absent(self) -> None:
        """A missing base package also explains why its extra is unavailable."""
        provider = Provider(make_coordinator([], package="foo"), target=PYTHON_312)
        assert provider.choose_version("foo[security]", VersionRange.full()) is None

        reason = provider.get_no_versions_reason("foo[security]")
        assert reason is not None
        assert reason.short == "package not found on any configured index"

    def test_a_base_whose_versions_are_all_out_of_range_records_nothing(self) -> None:
        """A version constraint that excludes every release produces no index diagnostic."""
        provider = self._backtracking({"1.0": SECURITY_EXTRA_METADATA})
        assert (
            provider.choose_version("foo[security]", SpecifierSet(">=9").to_range())
            is None
        )
        assert provider.get_no_versions_reason("foo[security]") is None
