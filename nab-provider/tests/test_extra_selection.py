"""Selecting versions for requested extras and backtracking across releases."""

from __future__ import annotations

import pytest
from packaging.ranges import VersionRange
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from nab_provider.errors import MetadataError, MissingExtraError
from nab_provider.policy import ExtrasMode, ResolutionStrategy
from nab_provider.provider import Provider
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_metadata,
    make_wheel,
)
from nab_resolver.resolver import Resolver

from .provider_fixtures import PYTHON_312, SECURITY_EXTRA_METADATA

METADATA_WITHOUT_EXTRAS = (
    "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=1.0\n"
)

METADATA_WITH_TRAILING_SPACE = (
    "Metadata-Version: 2.1\n"
    "Name: foo\n"
    "Version: 1.0\n"
    "Provides-Extra: security \n"
    'Requires-Dist: cryptography>=2.0; extra == "security"\n'
)


class TestExtraSelection:
    def test_choose_version_delegates_to_base(self) -> None:
        """choose_version for extras returns the base's best version."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={
                "2.0": SECURITY_EXTRA_METADATA.replace("Version: 1.0", "Version: 2.0"),
                "1.0": SECURITY_EXTRA_METADATA,
            },
            package="foo",
        )
        provider = Provider(coordinator)
        spec = SpecifierSet("")
        version = provider.choose_version("foo[security]", spec.to_range())
        assert version == Version("2.0")

    def test_choose_extra_version_honours_lowest_strategy(self) -> None:
        """``LOWEST`` flips the extras-proxy decision to the smallest version.

        The strategy is keyed off the *base* canonical name; an extras
        proxy never gets a different answer than its underlying package.
        """
        wheels = [make_wheel("3.0"), make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=SECURITY_EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator, resolution_strategy=ResolutionStrategy.LOWEST)
        chosen = provider.choose_version("foo[security]", VersionRange.full())
        assert chosen == Version("1.0")

    def test_get_dependencies_returns_extra_deps(self) -> None:
        """The security extra requires both the base package and cryptography."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=SECURITY_EXTRA_METADATA,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        deps = provider.get_dependencies("foo[security]", Version("1.0"))
        assert "cryptography" in deps
        assert "foo" in deps
        assert "bar" not in deps

    def test_get_dependencies_whitespace_provides_extra_keeps_deps(self) -> None:
        """A trailing space on Provides-Extra must not drop the extra's deps."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=METADATA_WITH_TRAILING_SPACE,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312)
        deps = provider.get_dependencies("foo[security]", Version("1.0"))
        assert "cryptography" in deps

    def test_choose_extra_version_filters_by_base_range(self) -> None:
        """foo[security] must use a version allowed by the requirement on foo."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=SECURITY_EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator)
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("<2.0").to_range()}, {}
        )
        spec = SpecifierSet("")
        assert provider.choose_version("foo[security]", spec.to_range()) == Version(
            "1.0"
        )

    def test_choose_extra_version_records_block_when_base_filter_empties(
        self,
    ) -> None:
        """When the base's decision excludes every proxy candidate in
        ``version_range``, the provider must push a binary clause linking
        the proxy's emptied range to the base's decision.  Without it, the
        resolver only sees a single-term NO_VERSIONS clause and cannot
        learn to revisit the base.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=SECURITY_EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator)
        # Base decided to 1.0; proxy version range excludes 1.0.  Both
        # 2.0 and 1.0 are filtered out (1.0 by version_range, 2.0 by
        # base's positive range), so the proxy has no viable candidate.
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("==1.0").to_range()},
            {"foo": Version("1.0")},
        )
        result = provider.choose_version(
            "foo[security]", SpecifierSet(">1.0").to_range()
        )
        assert result is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        terms = clauses[0].terms
        proxy_terms = [t for t in terms if t.package == "foo[security]"]
        base_terms = [t for t in terms if t.package == "foo"]
        assert len(proxy_terms) == 1
        assert len(base_terms) == 1
        # Proxy term names the excluded version 2.0; base term pins 1.0.
        assert proxy_terms[0].is_positive()
        assert Version("2.0") in proxy_terms[0].constraint
        assert Version("1.0") in base_terms[0].constraint

    def test_choose_extra_version_blocks_a_prerelease_excluded_by_the_base(
        self,
    ) -> None:
        """The pre-release enumeration reads the same descending listing.

        The proxy range's bounds are wider than its opt-in region, so the
        default policy buffers 2.0a1 behind 2.0 and drops it; only the
        ``prereleases=True`` pass reaches the block recorder with it.
        """
        wheels = [make_wheel(v) for v in ("2.0", "2.0a1", "1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=SECURITY_EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator)
        all_versions = provider.versions_only("foo", provider.fetch_versions("foo"))
        assert all_versions == sorted(all_versions, reverse=True)
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("<1.0").to_range()},
            {},
        )
        result = provider.choose_version(
            "foo[security]", SpecifierSet(">=1.0,<3.0").to_range()
        )
        assert result is None
        clauses = provider.consume_pending_clauses()
        proxy_terms = [
            t for c in clauses for t in c.terms if t.package == "foo[security]"
        ]
        assert any(Version("2.0a1") in t.constraint for t in proxy_terms)

    def test_choose_extra_version_records_range_block_when_base_undecided(
        self,
    ) -> None:
        """Range-block path: base has a positive range but no decision yet.

        The clause must use the base's range (not a singleton) so it
        stays sound across backjumps that widen the range.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=SECURITY_EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator)
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("<2.0").to_range()},
            {},  # base is constrained but not yet decided
        )
        # version_range demands 2.0+ but base_range forbids it.
        result = provider.choose_version(
            "foo[security]", SpecifierSet(">=2.0").to_range()
        )
        assert result is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        base_terms = [t for t in clauses[0].terms if t.package == "foo"]
        assert len(base_terms) == 1
        # Base term carries the range, not a singleton.
        assert Version("1.0") in base_terms[0].constraint
        assert Version("2.0") not in base_terms[0].constraint

    def test_choose_extra_version_records_block_in_backtrack_mode(self) -> None:
        """BACKTRACK mode also pushes block clauses when base filter empties.

        The fall-through path in ``choose_extra_version`` (after the
        per-version metadata check loop) must record the same block
        clauses as the non-BACKTRACK path so the resolver can revisit
        the base regardless of the configured extras handling.
        """
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels, metadata_text=SECURITY_EXTRA_METADATA, package="foo"
        )
        provider = Provider(coordinator, extras_mode=ExtrasMode.BACKTRACK)
        provider.receive_partial_solution_hint(
            {"foo": SpecifierSet("==1.0").to_range()},
            {"foo": Version("1.0")},
        )
        result = provider.choose_version(
            "foo[security]", SpecifierSet(">1.0").to_range()
        )
        assert result is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        terms = clauses[0].terms
        assert any(t.package == "foo[security]" for t in terms)
        assert any(t.package == "foo" for t in terms)

    def test_prioritize_extras_before_base(self) -> None:
        """Extras proxies sort before their base so they pin base versions."""
        wheels = [make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo")
        provider = Provider(coordinator)
        spec = SpecifierSet("")
        base = provider.prioritize("foo", spec.to_range(), {})
        extra = provider.prioritize("foo[security]", spec.to_range(), {})
        assert extra < base

    def test_warn_mode_missing_extra(self) -> None:
        """WARN mode logs and returns only the base pin for missing extra."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=METADATA_WITHOUT_EXTRAS,
            package="foo",
        )
        provider = Provider(coordinator, target=PYTHON_312, extras_mode=ExtrasMode.WARN)
        deps = provider.get_dependencies("foo[nonexistent]", Version("1.0"))
        assert deps == {"foo": VersionRange.singleton(Version("1.0"))}

    def test_error_user_mode_raises_for_root_extra(self) -> None:
        """ERROR_USER raises for user-provided missing extra."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=METADATA_WITHOUT_EXTRAS,
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            extras_mode=ExtrasMode.ERROR_USER,
            root_extras={("foo", "nonexistent")},
        )
        with pytest.raises(MissingExtraError):
            provider.get_dependencies("foo[nonexistent]", Version("1.0"))

    def test_error_user_mode_warns_for_transitive(self) -> None:
        """ERROR_USER warns (not errors) for transitive missing extra."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=METADATA_WITHOUT_EXTRAS,
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            extras_mode=ExtrasMode.ERROR_USER,
        )
        deps = provider.get_dependencies("foo[nonexistent]", Version("1.0"))
        assert deps == {"foo": VersionRange.singleton(Version("1.0"))}

    def test_missing_extra_proxy_and_base_converge(self) -> None:
        """A warn-and-drop extra still keeps proxy and base in lockstep.

        alpha 2.0 provides ext-one, whose dependency beta has no
        versions; alpha 1.0 does not provide it.  The proxy backtracks
        to 1.0 and must drag the base along, otherwise the resolution
        pins alpha 2.0 while silently dropping ext-one's dependencies.
        """
        listings = {
            "alpha": [make_wheel("2.0"), make_wheel("1.0")],
            "beta": [],
            "gamma": [make_wheel("1.0")],
        }
        coordinator = make_coordinator(listings=listings)
        coordinator.index.store_metadata(
            "alpha",
            "2.0",
            "Metadata-Version: 2.1\nName: alpha\nVersion: 2.0\n"
            "Provides-Extra: ext-one\n"
            'Requires-Dist: beta; extra == "ext-one"\n',
        )
        coordinator.index.store_metadata(
            "alpha", "1.0", "Metadata-Version: 2.1\nName: alpha\nVersion: 1.0\n"
        )
        coordinator.index.store_metadata(
            "gamma",
            "1.0",
            "Metadata-Version: 2.1\nName: gamma\nVersion: 1.0\n"
            "Requires-Dist: alpha[ext-one]\n",
        )
        roots = {"gamma": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(coordinator, target=PYTHON_312, root_requirements=roots)
        resolver: Resolver[str, Version] = Resolver(
            provider, range_type=VersionRange, root_version="0"
        )
        result = resolver.resolve(roots)
        assert result["alpha[ext-one]"] == result["alpha"] == Version("1.0")
        assert "beta" not in result

    def test_backtrack_mode_raises_for_root_extra(self) -> None:
        """BACKTRACK raises for user-provided missing extra."""
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=METADATA_WITHOUT_EXTRAS,
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            extras_mode=ExtrasMode.BACKTRACK,
            root_extras={("foo", "nonexistent")},
        )
        with pytest.raises(MissingExtraError):
            provider.get_dependencies("foo[nonexistent]", Version("1.0"))

    def test_backtrack_mode_skips_version_without_extra(self) -> None:
        """BACKTRACK skips versions where the extra is missing."""
        meta_v2 = "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\nRequires-Dist: bar\n"
        meta_v1 = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            "Provides-Extra: security\n"
            'Requires-Dist: cryptography; extra == "security"\n'
        )
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(
            wheels,
            metadata_by_version={"2.0": meta_v2, "1.0": meta_v1},
            package="foo",
        )

        provider = Provider(
            coordinator,
            target=PYTHON_312,
            extras_mode=ExtrasMode.BACKTRACK,
        )
        spec = SpecifierSet("")
        version = provider.choose_version("foo[security]", spec.to_range())
        assert version == Version("1.0")

    def test_no_metadata_raises(self) -> None:
        """Extras on a package with no metadata raises MetadataError."""
        coordinator = make_coordinator(
            [make_wheel("1.0", has_metadata=False)], package="foo"
        )
        provider = Provider(coordinator)
        with pytest.raises(MetadataError):
            provider.get_dependencies("foo[security]", Version("1.0"))

    def test_look_ahead_skipped_for_extras(self) -> None:
        """Look-ahead always passes for extras packages."""
        coordinator = make_coordinator([make_wheel("1.0")], package="foo")
        provider = Provider(coordinator)
        assert provider._look_ahead_ok("foo[security]", Version("1.0")) is True

    def test_warn_mode_choose_version_empty(self) -> None:
        """WARN mode choose_version returns None for empty candidates."""
        coordinator = make_coordinator([], package="foo")
        provider = Provider(coordinator, extras_mode=ExtrasMode.WARN)
        spec = SpecifierSet(">=99.0")
        assert provider.choose_version("foo[security]", spec.to_range()) is None

    def test_backtrack_mode_user_extra_returns_first(self) -> None:
        """A direct request can select a release with no declared extras."""
        wheels = [make_wheel("2.0"), make_wheel("1.0")]
        coordinator = make_coordinator(wheels, package="foo", auto_metadata=True)
        provider = Provider(
            coordinator,
            extras_mode=ExtrasMode.BACKTRACK,
            root_extras={("foo", "security")},
        )
        spec = SpecifierSet("")
        version = provider.choose_version("foo[security]", spec.to_range())
        assert version == Version("2.0")

    @staticmethod
    def _user_extra_provider(
        extras_mode: ExtrasMode,
        root_spec: str = "",
        extra: str = "security",
    ) -> Provider:
        """A provider whose ``foo`` declares ``security`` only at 2.0.

        ``root_spec`` is the specifier the root requirement put on ``foo``.
        """
        wheels = [make_wheel(v) for v in ("3.0", "2.1", "2.0", "1.0")]
        coordinator = make_coordinator(
            wheels,
            package="foo",
            metadata_by_version={
                "3.0": make_metadata("foo", "3.0"),
                "2.1": make_metadata("foo", "2.1"),
                "2.0": make_metadata("foo", "2.0") + "Provides-Extra: security\n",
                "1.0": make_metadata("foo", "1.0"),
            },
        )
        return Provider(
            coordinator,
            extras_mode=extras_mode,
            root_extras={("foo", extra)},
            root_requirements={"foo": SpecifierSet(root_spec).to_range()},
        )

    @pytest.mark.parametrize(
        "extras_mode", [ExtrasMode.ERROR_USER, ExtrasMode.BACKTRACK]
    )
    def test_user_extra_reports_no_version_when_range_drops_the_provider(
        self, extras_mode: ExtrasMode
    ) -> None:
        """A narrowed range holding no provider of a user extra has no version.

        Pinning one would commit the resolver to a version the extra
        cannot be expanded from, aborting the resolve instead of
        backjumping off whatever narrowed the range.
        """
        provider = self._user_extra_provider(extras_mode)
        spec = SpecifierSet(">=2.1")
        assert provider.choose_version("foo[security]", spec.to_range()) is None

    def test_user_extra_keeps_the_first_version_over_an_older_provider(self) -> None:
        """A user extra pins the first in-range version, not an older provider."""
        provider = self._user_extra_provider(ExtrasMode.ERROR_USER)
        spec = SpecifierSet(">=2.0")
        assert provider.choose_version("foo[security]", spec.to_range()) == Version(
            "3.0"
        )

    def test_user_extra_keeps_the_first_version_when_the_root_excludes_it(self) -> None:
        """A root range with no provider of its own stays the user's error.

        Pinning the first version reports the miss against it, rather
        than reporting no version and losing the error naming the extra.
        """
        provider = self._user_extra_provider(ExtrasMode.ERROR_USER, root_spec=">=2.1")
        spec = SpecifierSet(">=2.1")
        assert provider.choose_version("foo[security]", spec.to_range()) == Version(
            "3.0"
        )

    def test_user_extra_keeps_the_first_version_when_no_version_declares_it(
        self,
    ) -> None:
        """An extra no version of the package declares stays the user's error."""
        provider = self._user_extra_provider(ExtrasMode.ERROR_USER, extra="nonexistent")
        spec = SpecifierSet(">=2.1")
        assert provider.choose_version("foo[nonexistent]", spec.to_range()) == Version(
            "3.0"
        )

    def test_warn_mode_user_extra_keeps_the_first_version(self) -> None:
        """WARN mode pins the first in-range version whatever the range."""
        provider = self._user_extra_provider(ExtrasMode.WARN)
        spec = SpecifierSet(">=2.1")
        assert provider.choose_version("foo[security]", spec.to_range()) == Version(
            "3.0"
        )

    def test_backtrack_all_versions_rejected(self) -> None:
        """BACKTRACK returns None when no version provides the extra."""
        metadata = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar\n"
        )
        coordinator = make_coordinator(
            [make_wheel("1.0")],
            metadata_text=metadata,
            package="foo",
        )
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            extras_mode=ExtrasMode.BACKTRACK,
        )
        spec = SpecifierSet("")
        assert provider.choose_version("foo[missing]", spec.to_range()) is None


class TestExtrasPrereleaseBaseRangeBlocks:
    """Only the pre-release of c provides the extra x.

    Switching from r 2.0 to r 1.0 changes the constraint on c and makes
    that pre-release eligible. The resolver must reconsider c[x] after the switch.
    """

    C_META_WITH_X = (
        "Metadata-Version: 2.1\nName: c\nVersion: {v}\nProvides-Extra: x\n\n"
    )
    C_META_NO_X = "Metadata-Version: 2.1\nName: c\nVersion: {v}\n\n"

    def _prerelease_coordinator(self, *, include_older_release: bool) -> FakeFetchPort:
        r_wheels = [make_wheel("2.0"), make_wheel("1.0")]
        if include_older_release:
            r_wheels.append(make_wheel("0.5"))
        listings = {
            "r": r_wheels,
            "c": [make_wheel("2.0.0a1"), make_wheel("1.0.0")],
        }
        metadata = {
            "2.0": (
                "Metadata-Version: 2.1\nName: r\nVersion: 2.0\n"
                "Requires-Dist: c[x]>=1.0,<1.5\n\n"
            ),
            "1.0": (
                "Metadata-Version: 2.1\nName: r\nVersion: 1.0\n"
                "Requires-Dist: c[x]>=1.5a0\n\n"
            ),
            "0.5": "Metadata-Version: 2.1\nName: r\nVersion: 0.5\n\n",
            "1.0.0": self.C_META_NO_X.format(v="1.0.0"),
            "2.0.0a1": self.C_META_WITH_X.format(v="2.0.0a1"),
        }
        return make_coordinator(listings=listings, metadata_by_version=metadata)

    def _resolve_r(
        self, coordinator: FakeFetchPort
    ) -> tuple[dict[str, Version], Resolver[str, Version]]:
        root_reqs = {"r": VersionRange.full(admit_arbitrary=False)}
        provider = Provider(
            coordinator,
            target=PYTHON_312,
            extras_mode=ExtrasMode.BACKTRACK,
            root_requirements=root_reqs,
        )
        resolver = Resolver(provider, range_type=VersionRange, root_version="0")
        return resolver.resolve(root_reqs), resolver

    def test_backtracking_prefers_release_that_can_use_the_extra(self) -> None:
        """Backtracking selects r 1.0 even when the dependency-free r 0.5 exists."""
        pins, resolver = self._resolve_r(
            self._prerelease_coordinator(include_older_release=True)
        )
        assert pins["r"] == Version("1.0")
        assert pins["c"] == Version("2.0.0a1")
        assert pins["c[x]"] == Version("2.0.0a1")
        assert resolver.stats.backjumps > 0

    def test_backtracking_finds_the_only_release_that_can_use_the_extra(self) -> None:
        """Backtracking still finds r 1.0 when it is the oldest release."""
        pins, _ = self._resolve_r(
            self._prerelease_coordinator(include_older_release=False)
        )
        assert pins["r"] == Version("1.0")
        assert pins["c"] == Version("2.0.0a1")

    def test_recorded_block_includes_bounds_excluded_prerelease(self) -> None:
        """A recorded block names the bounds-excluded pre-release.

        Default filtering buffers and drops it once the final 1.0.0
        matches, so the block naming c 2.0.0a1 appears only when the
        recompute admits pre-releases.
        """
        listings = {"c": [make_wheel("2.0.0a1"), make_wheel("1.0.0")]}
        metadata = {
            "2.0.0a1": self.C_META_WITH_X.format(v="2.0.0a1"),
            "1.0.0": self.C_META_NO_X.format(v="1.0.0"),
        }
        coordinator = make_coordinator(listings=listings, metadata_by_version=metadata)
        provider = Provider(
            coordinator, target=PYTHON_312, extras_mode=ExtrasMode.BACKTRACK
        )
        # Base decided to the final 1.0.0, which does not provide x, so the
        # proxy has no candidate; the pre-release 2.0.0a1 is excluded only
        # by the base's bounds.
        provider.receive_partial_solution_hint(
            {"c": SpecifierSet("==1.0.0").to_range()},
            {"c": Version("1.0.0")},
        )
        result = provider.choose_version("c[x]", VersionRange.full())
        assert result is None
        clauses = provider.consume_pending_clauses()
        assert len(clauses) == 1
        terms = clauses[0].terms
        proxy_terms = [t for t in terms if t.package == "c[x]"]
        base_terms = [t for t in terms if t.package == "c"]
        assert len(proxy_terms) == 1
        assert len(base_terms) == 1
        assert proxy_terms[0].is_positive()
        assert Version("2.0.0a1") in proxy_terms[0].constraint
        assert Version("1.0.0") in base_terms[0].constraint
