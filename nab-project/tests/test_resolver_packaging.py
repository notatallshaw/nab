"""End-to-end resolver tests using PEP 440 versions.

Same test scenarios as nab-resolver's test_resolver.py but using
packaging.version.Version and packaging.specifiers.SpecifierSet
through the PackagingProvider.
"""

from __future__ import annotations

import pytest

from nab_project._packaging_provider import PackagingProvider
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_resolver.errors import ResolutionError
from nab_resolver.report import union_terms
from nab_resolver.resolver import Resolver
from nab_resolver.types import Term

V = Version


class TestSpecifierToRange:
    """Verify conversion from SpecifierSet to Range[Version]."""

    def test_simple_range(self) -> None:
        """Convert >=1.0,<2.0 to a range containing 1.5 but not 2.0."""
        r = SpecifierSet(">=1.0,<2.0").to_range()
        assert V("1.5") in r
        assert V("2.0") not in r
        assert V("0.9") not in r

    def test_exact(self) -> None:
        """Convert ==1.5 to a range containing only 1.5."""
        r = SpecifierSet("==1.5").to_range()
        assert V("1.5") in r
        assert V("1.4") not in r
        assert V("1.6") not in r

    def test_any(self) -> None:
        """Empty SpecifierSet matches everything."""
        r = SpecifierSet().to_range()
        assert V("1.0") in r
        assert V("999.0") in r

    def test_unsatisfiable(self) -> None:
        """Contradictory specifiers produce an empty range."""
        r = SpecifierSet(">=2.0,<1.0").to_range()
        assert r.is_empty

    def test_term_subset_respects_arbitrary_flag(self) -> None:
        """The arbitrary-string flag is load-bearing for subset and satisfaction.

        ``SpecifierSet("")`` produces the full range that also admits
        arbitrary ``===`` versions, while range unions built during conflict
        resolution produce the unflagged full range. The flagged range is a
        strict superset: it is not a subset of the plain range, and a term
        carrying the plain range is not satisfied by the flagged assignment.
        The reverse holds. Treating the two as mutual subsets (the old
        algebra) let conflict resolution loop on a clause it could never
        resolve away.
        """
        flagged_full = SpecifierSet("").to_range()
        exact = VersionRange.singleton(V("1.0"))
        plain_full = exact | ~exact
        assert plain_full != flagged_full
        assert not flagged_full.is_subset(plain_full)
        assert plain_full.is_subset(flagged_full)
        assert not Term("pkg", plain_full).satisfies(flagged_full)
        assert Term("pkg", flagged_full).satisfies(plain_full)

    def test_not_equal(self) -> None:
        """Convert !=1.5 to a range excluding only 1.5."""
        r = SpecifierSet("!=1.5").to_range()
        assert V("1.4") in r
        assert V("1.5") not in r
        assert V("1.6") in r

    def test_lte_includes_local(self) -> None:
        """<=1.0 must include 1.0+local but exclude 1.0.post1.

        PEP 440: local versions sort after the release but before
        any post-release.  The boundary version AFTER_LOCALS sits
        between 1.0+<anything> and 1.0.post0.  If the conversion
        dropped the boundary and stored plain Version('1.0') as an
        inclusive upper bound, 1.0+local would compare > 1.0 and
        be wrongly excluded.
        """
        r = SpecifierSet("<=1.0").to_range()
        assert V("1.0") in r
        assert V("1.0+local") in r
        assert V("1.0.post1") not in r

    def test_gt_excludes_post(self) -> None:
        """>1.0 must exclude 1.0.post1.

        PEP 440: >V excludes post-releases of V.  The boundary
        version AFTER_POSTS sits after all 1.0.postN releases.
        If the conversion used plain Version('1.0') as an exclusive
        lower bound, 1.0.post1 > 1.0 would be true and it would
        be wrongly included.
        """
        r = SpecifierSet(">1.0").to_range()
        assert V("1.0") not in r
        assert V("1.0+local") not in r
        assert V("1.0.post1") not in r
        assert V("1.1") in r

    def test_eq_includes_local(self) -> None:
        """==1.0 must include 1.0+local but exclude 1.0.post1.

        PEP 440: ==V (without local segment) matches V plus any
        local version of V.  The interval is [1.0, AFTER_LOCALS].
        If the conversion dropped the boundary, 1.0+local would
        compare > 1.0 and fall outside [1.0, 1.0].
        """
        r = SpecifierSet("==1.0").to_range()
        assert V("1.0") in r
        assert V("1.0+local") in r
        assert V("1.0.post1") not in r

    def test_neq_excludes_local(self) -> None:
        """!=1.0 must exclude 1.0+local but include 1.0.post1.

        The complement of ==1.0: two intervals split at the
        AFTER_LOCALS boundary.  If that boundary were lost,
        1.0+local would land in one of the intervals instead
        of the gap.
        """
        r = SpecifierSet("!=1.0").to_range()
        assert V("0.9") in r
        assert V("1.0") not in r
        assert V("1.0+local") not in r
        assert V("1.0.post1") in r
        assert V("1.1") in r

    def test_lt_post_release(self) -> None:
        """<1.0.post2 includes the final release and earlier post-releases.

        Since the specifier itself is a post-release, pre-releases of
        1.0 are also less than 1.0.post2 in PEP 440 ordering and are
        included.
        """
        r = SpecifierSet("<1.0.post2").to_range()
        assert V("1.0.dev1") in r
        assert V("1.0a1") in r
        assert V("1.0") in r
        assert V("1.0.post1") in r
        assert V("1.0.post2") not in r
        assert V("0.9") in r

    def test_lt_pre_release(self) -> None:
        """<1.0b1 allows earlier pre-releases of 1.0.

        When the specifier itself is a pre-release, earlier
        pre-releases of the same version are included.
        """
        r = SpecifierSet("<1.0b1").to_range()
        assert V("1.0a1") in r
        assert V("1.0.dev1") in r
        assert V("1.0b1") not in r
        assert V("1.0") not in r

    def test_lt_final(self) -> None:
        """<2.0 excludes pre-releases of 2.0 per PEP 440."""
        r = SpecifierSet("<2.0").to_range()
        assert V("1.9") in r
        assert V("2.0.dev0") not in r
        assert V("2.0a1") not in r
        assert V("2.0") not in r

    def test_compatible_release(self) -> None:
        """~=1.4 means >=1.4,<2.dev0."""
        r = SpecifierSet("~=1.4").to_range()
        assert V("1.3") not in r
        assert V("1.4") in r
        assert V("1.9") in r
        assert V("2.0.dev0") not in r
        assert V("2.0") not in r

    def test_wildcard_equal(self) -> None:
        """==1.* matches any 1.x release."""
        r = SpecifierSet("==1.*").to_range()
        assert V("0.9") not in r
        assert V("1.0") in r
        assert V("1.9.9") in r
        assert V("2.0.dev0") not in r

    def test_wildcard_includes_prerelease(self) -> None:
        """==1.2.* admits pre-releases in the 1.2 family."""
        r = SpecifierSet("==1.2.*").to_range()
        assert V("1.2.0a1") in r
        assert V("1.2.0") in r
        assert V("1.2.9") in r
        assert V("1.3.dev0") not in r
        assert V("1.1.9") not in r

    def test_compatible_release_post(self) -> None:
        """~=2.2.post3 drops the last release component for the prefix.

        The compatible bound is ==2.* so the upper edge is 3.dev0; the
        lower edge keeps the post-release.
        """
        r = SpecifierSet("~=2.2.post3").to_range()
        assert V("2.2.post2") not in r
        assert V("2.2.post3") in r
        assert V("2.2.5") in r
        assert V("2.3") in r
        assert V("3.0") not in r

    def test_gt_pre_release(self) -> None:
        """>1.0a1 carves out only 1.0a1 and its own pre/post/local family.

        The specifier is a pre-release, so PEP 440 excludes 1.0a1
        itself, 1.0a1+local, and 1.0a1.postN.  The final release 1.0 and
        its post-releases are higher versions and stay in.
        """
        r = SpecifierSet(">1.0a1").to_range()
        assert V("1.0a1") not in r
        assert V("1.0a1.post1") not in r
        assert V("1.0a2") in r
        assert V("1.0") in r
        assert V("1.0.post1") in r
        assert V("1.1") in r

    def test_epoch_ordering(self) -> None:
        """An epoch sorts above every lower-epoch release."""
        r = SpecifierSet(">=1!1.0").to_range()
        assert V("1!1.0") in r
        assert V("1!2.0") in r
        assert V("1.0") not in r
        assert V("999") not in r

    def test_epoch_wildcard(self) -> None:
        """==1!2.* keeps the epoch and stays within the 1!2 family."""
        r = SpecifierSet("==1!2.*").to_range()
        assert V("1!2.0") in r
        assert V("1!2.0.dev0") in r
        assert V("1!2.9") in r
        assert V("1!3.0") not in r
        assert V("2.0") not in r

    def test_epoch_compatible_release(self) -> None:
        """~=1!2.2 carries the epoch into both bounds."""
        r = SpecifierSet("~=1!2.2").to_range()
        assert V("1!2.2") in r
        assert V("1!2.9") in r
        assert V("1!3.0") not in r
        assert V("2.2") not in r

    def test_epoch_not_equal_excludes_local(self) -> None:
        """!=1!1.5 excludes the local family but keeps post-releases."""
        r = SpecifierSet("!=1!1.5").to_range()
        assert V("1!1.4") in r
        assert V("1!1.5") not in r
        assert V("1!1.5+local") not in r
        assert V("1!1.5.post1") in r


class TestTrivialResolution:
    """Resolve simple graphs with PEP 440 versions."""

    def test_no_dependencies(self) -> None:
        """Resolve a single package with no dependencies."""
        provider = PackagingProvider(
            {"root": {V("1.0"): {}}},
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )
        assert result == {"root": V("1.0")}

    def test_single_dependency(self) -> None:
        """Pick the newest version satisfying a single dependency."""
        provider = PackagingProvider(
            {
                "root": {V("1.0"): {"foo": SpecifierSet(">=1.0")}},
                "foo": {V("3.0"): {}, V("2.0"): {}, V("1.0"): {}},
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )
        assert result["root"] == V("1.0")
        assert result["foo"] == V("3.0")

    def test_transitive_dependency(self) -> None:
        """Resolve transitive dependencies through two levels."""
        provider = PackagingProvider(
            {
                "root": {V("1.0"): {"foo": SpecifierSet(">=1.0")}},
                "foo": {
                    V("2.0"): {"bar": SpecifierSet(">=1.0")},
                    V("1.0"): {},
                },
                "bar": {V("1.0"): {}},
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )
        assert result["foo"] == V("2.0")
        assert result["bar"] == V("1.0")


class TestDiamondDependency:
    """Resolve diamond-shaped dependency graphs."""

    def test_diamond(self) -> None:
        """Both foo and bar depend on baz with overlapping constraints."""
        provider = PackagingProvider(
            {
                "root": {
                    V("1.0"): {
                        "foo": SpecifierSet(""),
                        "bar": SpecifierSet(""),
                    }
                },
                "foo": {V("1.0"): {"baz": SpecifierSet(">=2.0")}},
                "bar": {V("1.0"): {"baz": SpecifierSet("<4.0")}},
                "baz": {V("4.0"): {}, V("3.0"): {}, V("2.0"): {}, V("1.0"): {}},
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )
        assert result["baz"] in (V("2.0"), V("3.0"))


class TestBacktracking:
    """Verify the resolver backtracks when a branch fails."""

    def test_simple_backtrack(self) -> None:
        """foo@2.0 requires bar>=2.0, but only bar@1.0 exists."""
        provider = PackagingProvider(
            {
                "root": {V("1.0"): {"foo": SpecifierSet("")}},
                "foo": {
                    V("2.0"): {"bar": SpecifierSet(">=2.0")},
                    V("1.0"): {},
                },
                "bar": {V("1.0"): {}},
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )
        assert result["foo"] == V("1.0")


class TestProviderEdgeCases:
    """Test PackagingProvider edge cases."""

    def test_unknown_package_returns_no_version(self) -> None:
        """choose_version returns None for unknown packages."""
        provider = PackagingProvider({})
        assert provider.choose_version("missing", VersionRange.full()) is None

    def test_unknown_package_has_no_dependencies(self) -> None:
        """get_dependencies returns empty for unknown package/version."""
        provider = PackagingProvider({})
        assert provider.get_dependencies("missing", V("1.0")) == {}

    def test_prioritize_unknown_package(self) -> None:
        """prioritize returns lowest priority for unknown packages."""
        provider = PackagingProvider({})
        result = provider.prioritize("missing", VersionRange.full(), {})
        assert result == (True, 0)

    def test_conflict_promoted_gets_higher_priority(self) -> None:
        """Packages with 5+ conflicts sort before others."""
        provider = PackagingProvider(
            {"foo": {V("1.0"): {}, V("2.0"): {}, V("3.0"): {}}},
        )
        version_range = SpecifierSet(">=1.0").to_range()
        normal = provider.prioritize("foo", version_range, {})
        promoted = provider.prioritize("foo", version_range, {"foo": 5})
        assert promoted < normal


class TestConflictDetection:
    """Verify the resolver detects and reports conflicts."""

    def test_shared_conflict(self) -> None:
        """All versions of A require D>=2.0, E requires D<1.0."""
        a_deps = {"D": SpecifierSet(">=2.0")}
        provider = PackagingProvider(
            {
                "root": {
                    V("1.0"): {
                        "A": SpecifierSet(""),
                        "E": SpecifierSet(""),
                    }
                },
                "A": {V(f"{v}.0"): a_deps for v in range(50, 0, -1)},
                "D": {V("3.0"): {}, V("2.0"): {}, V("1.0"): {}},
                "E": {V("1.0"): {"D": SpecifierSet("<1.0")}},
            },
        )
        with pytest.raises(ResolutionError):
            Resolver(provider, range_type=VersionRange, root_version="0").resolve(
                {"root": VersionRange.singleton(V("1.0"))}
            )

    def test_direct_conflict(self) -> None:
        """Root requires foo>=2.0, user also requires foo<1.0."""
        provider = PackagingProvider(
            {
                "root": {V("1.0"): {"foo": SpecifierSet(">=2.0")}},
                "foo": {V("3.0"): {}, V("2.0"): {}, V("1.0"): {}},
            },
        )
        with pytest.raises(ResolutionError):
            Resolver(provider, range_type=VersionRange, root_version="0").resolve(
                {
                    "root": VersionRange.singleton(V("1.0")),
                    "foo": SpecifierSet("<1.0").to_range(),
                }
            )


class TestArbitraryAdmittingRootRange:
    """A bare root requirement supplies the top that admits ``===`` strings.

    A subtraction that removes versions drops the admission, so recording it
    in a term would stall conflict resolution.
    """

    def test_bare_root_requirement_reports_the_conflict(self) -> None:
        """``a`` needs a ``b`` that has no versions; say so."""
        provider = PackagingProvider(
            {
                "a": {
                    V("3.0"): {"b": SpecifierSet(">=1")},
                    V("2.0"): {"b": SpecifierSet(">=1")},
                    V("1.0"): {"b": SpecifierSet(">=1")},
                },
            },
        )
        with pytest.raises(ResolutionError) as exc_info:
            Resolver(provider, range_type=VersionRange, root_version="0").resolve(
                {"a": SpecifierSet("").to_range()}
            )
        message = str(exc_info.value)
        assert "no versions of b" in message
        assert "your project depends on a" in message
        assert "resolver bug" not in message


class _ArbitraryAdmittingProvider(PackagingProvider):
    """A provider whose bare dependency edges keep the ``===`` admission.

    :class:`PackagingProvider` opts out of it; a provider written against
    :class:`~nab_resolver.types.RangeProtocol` alone need not.
    """

    def get_dependencies(
        self, package: str, version: Version
    ) -> dict[str, VersionRange]:
        """Convert bare specifiers to the arbitrary-admitting full range."""
        raw = self._packages.get(package, {}).get(version, {})
        return {
            dep: (spec.to_range() if spec else VersionRange.full())
            for dep, spec in raw.items()
        }


class TestArbitraryAdmittingDependencyRange:
    """A provider may hand back the admitting top on a bare dependency edge.

    Here a stall would cost a solution rather than an explanation:
    backtracking off ``a==3.0`` is what reaches the version that resolves.
    """

    def test_bare_dependency_edge_resolves(self) -> None:
        """``a==3.0`` is unusable, so ``a==2.0`` is the answer."""
        provider = _ArbitraryAdmittingProvider(
            {
                "a": {
                    V("3.0"): {"c": SpecifierSet("")},
                    V("2.0"): {},
                },
                "c": {V("1.0"): {"a": SpecifierSet(">=9.0")}},
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"a": VersionRange.full(admit_arbitrary=False)}
        )
        assert result == {"a": V("2.0")}


class TestMultiLevelConflict:
    """Verify the resolver handles conflicts requiring deep backtracking."""

    def test_deep_backtracking(self) -> None:
        """a@2.0 -> b>=2.0 -> c>=3.0, but c only has 1.0 and 2.0."""
        provider = PackagingProvider(
            {
                "root": {
                    V("1.0"): {
                        "a": SpecifierSet(""),
                        "c": SpecifierSet(""),
                    }
                },
                "a": {
                    V("2.0"): {"b": SpecifierSet(">=2.0")},
                    V("1.0"): {},
                },
                "b": {
                    V("2.0"): {"c": SpecifierSet(">=3.0")},
                    V("1.0"): {},
                },
                "c": {V("2.0"): {}, V("1.0"): {}},
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )
        assert result["a"] == V("1.0")

    def test_conflict_with_multiple_parents(self) -> None:
        """Two packages constrain z to disjoint ranges."""
        provider = PackagingProvider(
            {
                "root": {
                    V("1.0"): {
                        "x": SpecifierSet(""),
                        "y": SpecifierSet(""),
                    }
                },
                "x": {V("1.0"): {"z": SpecifierSet(">=3.0")}},
                "y": {V("1.0"): {"z": SpecifierSet("<2.0")}},
                "z": {V("3.0"): {}, V("2.0"): {}, V("1.0"): {}},
            },
        )
        with pytest.raises(ResolutionError):
            Resolver(provider, range_type=VersionRange, root_version="0").resolve(
                {"root": VersionRange.singleton(V("1.0"))}
            )


class TestPreference:
    """Verify the resolver prefers newer versions."""

    def test_prefers_newest(self) -> None:
        """Pick the newest version when many are available."""
        provider = PackagingProvider(
            {
                "root": {V("1.0"): {"foo": SpecifierSet(">=1.0")}},
                "foo": {
                    V("5.0"): {},
                    V("4.0"): {},
                    V("3.0"): {},
                    V("2.0"): {},
                    V("1.0"): {},
                },
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )
        assert result["foo"] == V("5.0")

    def test_prefers_newest_within_constraint(self) -> None:
        """Pick the newest version within a constrained range."""
        provider = PackagingProvider(
            {
                "root": {V("1.0"): {"foo": SpecifierSet(">=2.0,<5.0")}},
                "foo": {
                    V("5.0"): {},
                    V("4.0"): {},
                    V("3.0"): {},
                    V("2.0"): {},
                    V("1.0"): {},
                },
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )
        assert result["foo"] == V("4.0")


class TestNoExtraPackages:
    """Verify packages from failed branches are excluded."""

    def test_backtracked_dependencies_excluded(self) -> None:
        """Packages only reachable through a failed version are excluded."""
        provider = PackagingProvider(
            {
                "root": {V("1.0"): {"pkg1": SpecifierSet("")}},
                "pkg0": {V("1.0"): {}},
                "pkg1": {
                    V("1.0"): {},
                    V("2.0"): {
                        "pkg0": SpecifierSet(""),
                        "pkg2": SpecifierSet("==2.0"),
                    },
                },
                "pkg2": {V("1.0"): {}},
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )
        assert "pkg2" not in result
        assert result["pkg1"] == V("1.0")


class TestCircularDependencies:
    """Verify the resolver handles circular dependency graphs."""

    def test_circular_with_impossible_version(self) -> None:
        """pkg0 -> pkg1 -> pkg0==2.0, but pkg0 only has 1.0."""
        provider = PackagingProvider(
            {
                "root": {V("1.0"): {"pkg0": SpecifierSet("")}},
                "pkg0": {V("1.0"): {"pkg1": SpecifierSet("")}},
                "pkg1": {V("1.0"): {"pkg0": SpecifierSet("==2.0")}},
            },
        )
        with pytest.raises(ResolutionError):
            Resolver(provider, range_type=VersionRange, root_version="0").resolve(
                {"root": VersionRange.singleton(V("1.0"))}
            )


class TestBruteForceRegression:
    """Regression tests found by brute-force comparison."""

    def test_backtrack_past_failed_branch_with_mutual_deps(self) -> None:
        """Conditional dependency with circular back-edge and dead end.

        root -> pkg0
        pkg0@3.0 -> pkg1          pkg0@1.0, pkg0@2.0 have no deps
        pkg1@1.0 -> pkg0, pkg2==2.0
        pkg1@2.0 -> pkg0, pkg2==2.0
        pkg2 has only v1.0        (no version satisfies ==2.0)
        """
        provider = PackagingProvider(
            {
                "root": {V("1.0"): {"pkg0": SpecifierSet("")}},
                "pkg0": {
                    V("1.0"): {},
                    V("2.0"): {},
                    V("3.0"): {"pkg1": SpecifierSet("")},
                },
                "pkg1": {
                    V("1.0"): {
                        "pkg0": SpecifierSet(""),
                        "pkg2": SpecifierSet("==2.0"),
                    },
                    V("2.0"): {
                        "pkg0": SpecifierSet(""),
                        "pkg2": SpecifierSet("==2.0"),
                    },
                },
                "pkg2": {V("1.0"): {}},
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )
        assert result["pkg0"] in (V("1.0"), V("2.0"))
        assert "pkg1" not in result
        assert "pkg2" not in result


class _FilterProvider(PackagingProvider):
    """PackagingProvider that selects via ``version_range.filter``.

    The real PyPI provider picks with ``Provider.choose_version``, which
    calls ``version_range.filter(all_versions)`` rather than a plain
    membership test. ``filter`` is prerelease-aware, so it exposes the
    constraint prerelease behavior that membership selection hides.
    """

    def choose_version(
        self, package: str, version_range: VersionRange
    ) -> Version | None:
        return next(iter(version_range.filter(self._get_versions(package))), None)

    def has_satisfying_version(self, package: str, version_range: VersionRange) -> bool:
        return self.choose_version(package, version_range) is not None


class TestPrereleaseAuthorizationBacktracking:
    """Pre-release admission follows the dependency edge that introduced it."""

    def test_rejected_parent_does_not_leak_prerelease_admission(self) -> None:
        """A rejected parent must not make an unrelated pre-release beat a final."""
        provider = _FilterProvider(
            {
                "a": {
                    V("1.0"): {"c": SpecifierSet(">=1.0")},
                    V("2.0"): {"c": SpecifierSet(">=2.0b1")},
                },
                "c": {
                    V("1.0"): {},
                    V("1.5a1"): {},
                    V("2.0b1"): {"d": SpecifierSet("==2.0")},
                },
                "d": {
                    V("1.0"): {},
                    V("2.0"): {},
                },
            }
        )

        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {
                "a": VersionRange.full(admit_arbitrary=False),
                "d": SpecifierSet("==1.0").to_range(),
            }
        )

        assert result["a"] == V("1.0")
        assert result["d"] == V("1.0")
        assert result["c"] == V("1.0")


class TestConflictPathPrereleaseAdmission:
    """Conflict-resolution term algebra must not leak pre-release admission.

    ``Term.intersect`` and ``union_terms`` subtract a negative (exclusion)
    term from a positive one. The result must keep only the positive (or
    negative-minuend) side's pre-release opt-in, so a learned exclusion
    grants no pre-release admission when its negation is propagated back
    into a positive range.
    """

    def test_intersect_positive_minus_negative_drops_prerelease(self) -> None:
        """``c>=1.0`` AND not ``c>=2.0b1`` admits no pre-release."""
        positive = Term("c", SpecifierSet(">=1.0").to_range(), positive=True)
        negative = Term("c", SpecifierSet(">=2.0b1").to_range(), positive=False)
        result = positive.intersect(negative)
        assert result is not None
        assert result.is_positive()
        picked = list(result.constraint.filter([V("2.0b1"), V("1.5a1"), V("1.0")]))
        assert picked == [V("1.0")]

    def test_union_terms_negative_minus_positive_drops_prerelease(self) -> None:
        """The mixed-term remainder keeps the negative term's opt-in only."""
        negative = Term("c", SpecifierSet(">=1.0").to_range(), positive=False)
        positive = Term("c", SpecifierSet(">=2.0b1").to_range(), positive=True)
        result = union_terms(negative, positive)
        assert result is not None
        assert not result.is_positive()
        picked = list(result.constraint.filter([V("1.5a1"), V("1.0")]))
        assert picked == [V("1.0")]


class TestConflictUnionPrereleaseLeak:
    """A conflict-resolution union must not leak an unopted pre-release into the
    final resolution.

    When a capped ``>=2.0b1,<3`` opt-in meets a disjoint higher range in a
    learned clause, the union of the two ranges must not widen the opt-in over
    ``[3.5, 4)``. The opt-in region stays clipped to ``[2.0b1, 3)``, so ``c``
    resolves to the final ``3.5`` rather than the unopted pre-release ``3.6b1``,
    even though no active requirement opted pre-releases in for ``c``.
    """

    def _resolve(self, graph: dict) -> dict:
        provider = _FilterProvider(graph)
        return Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))}
        )

    def test_negative_union_does_not_leak_capped_prerelease(self) -> None:
        """The union path in isolation: not ``c>=2.0b1,<3`` AND not ``c>=3.5,<4``.

        ``Term.intersect`` on two negatives folds to ``not(A | B)``; when the
        clause unit-propagates, the union ``A | B`` is derived positive and
        ``choose_version`` filters candidates by it. The capped ``>=2.0b1,<3``
        opt-in must not ride the union up into ``[3.5, 4)`` and admit 3.6b1.
        """
        capped = Term("c", SpecifierSet(">=2.0b1,<3").to_range(), positive=False)
        higher = Term("c", SpecifierSet(">=3.5,<4").to_range(), positive=False)
        merged = capped.intersect(higher)
        assert merged is not None
        picked = list(merged.constraint.filter([V("3.6b1"), V("3.5"), V("2.5")]))
        assert picked == [V("3.5"), V("2.5")]

    def test_failed_parent_capped_prerelease_does_not_win(self) -> None:
        # ``e@4.0`` needs ``c==2.0b1`` (no such version) and fails; the capped
        # opt-in it introduced must not survive into c's range and beat 3.5.
        result = self._resolve(
            {
                "c": {V("2.5"): {}, V("3.5"): {}, V("3.6b1"): {}},
                "a": {V("2.5"): {"c": SpecifierSet("==2.5")}, V("3.6b1"): {}},
                "e": {
                    V("1.0"): {"c": SpecifierSet("!=2.5")},
                    V("4.0"): {"c": SpecifierSet("==2.0b1")},
                },
                "root": {
                    V("1.0"): {"e": SpecifierSet("!=2.5"), "a": SpecifierSet(">=2.5")}
                },
            }
        )
        assert result["c"] == V("3.5")

    def test_alternate_parent_capped_prerelease_does_not_win(self) -> None:
        # ``g`` has a capped-prerelease branch (2.0b1 -> ``c==2.0b1``) and a
        # higher branch (4.0 -> ``c>=3.5,<4``); their union during conflict
        # resolution must not leak the pre-release into c.
        result = self._resolve(
            {
                "c": {V("2.5"): {}, V("3.5"): {}, V("3.6b1"): {}},
                "a": {V("2.5"): {"c": SpecifierSet("==2.5")}, V("3.6b1"): {}},
                "d": {V("3.0"): {"a": SpecifierSet(">=1.0")}},
                "g": {
                    V("2.0b1"): {"c": SpecifierSet("==2.0b1")},
                    V("4.0"): {"c": SpecifierSet(">=3.5,<4")},
                },
                "root": {V("1.0"): {"d": SpecifierSet(""), "g": SpecifierSet("!=2.5")}},
            }
        )
        assert result["c"] == V("3.5")


class TestRedundantTransitivePrereleaseOptIn:
    """A transitive pre-release specifier authorizes its pre-release even when
    its bound is already implied by another requirement.

    ``a`` requires ``c>=0.5a1`` while the root requires ``c>=1.0``. ``>=1.0``
    implies ``>=0.5a1``, so the requirement is redundant on ``c``'s version
    set, yet the pre-release opt-in it carries admits ``c`` version
    ``2.0.0a1`` over stable ``1.0.0``.
    """

    def test_redundant_specifier_authorizes_prerelease(self) -> None:
        """A redundant ``c>=0.5a1`` opts ``c`` into pre-release ``2.0.0a1``."""
        provider = _FilterProvider(
            {
                "a": {V("1.0.0"): {"c": SpecifierSet(">=0.5a1")}},
                "c": {V("1.0.0"): {}, V("2.0.0a1"): {}},
            }
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {
                "a": SpecifierSet("==1.0.0").to_range(),
                "c": SpecifierSet(">=1.0").to_range(),
            }
        )
        assert result["c"] == V("2.0.0a1")

    def test_redundant_opt_in_still_prefers_newer_final(self) -> None:
        """The opt-in admits its pre-release but does not beat a newer final."""
        provider = _FilterProvider(
            {
                "a": {V("1.0.0"): {"c": SpecifierSet(">=0.5a1")}},
                "c": {V("1.0.0"): {}, V("2.0.0a1"): {}, V("3.0.0"): {}},
            }
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {
                "a": SpecifierSet("==1.0.0").to_range(),
                "c": SpecifierSet(">=1.0").to_range(),
            }
        )
        assert result["c"] == V("3.0.0")

    def test_redundant_opt_in_from_rejected_parent_does_not_leak(self) -> None:
        """A rejected parent's redundant opt-in is undone on backtracking."""
        provider = _FilterProvider(
            {
                "a": {
                    V("2.0.0"): {
                        "c": SpecifierSet(">=0.5a1"),
                        "e": SpecifierSet("==2.0"),
                    },
                    V("1.0.0"): {},
                },
                "e": {V("1.0"): {}},
                "c": {V("1.0.0"): {}, V("2.0.0a1"): {}},
            }
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {
                "a": VersionRange.full(admit_arbitrary=False),
                "c": SpecifierSet(">=1.0").to_range(),
            }
        )
        assert result["a"] == V("1.0.0")
        assert result["c"] == V("1.0.0")


class TestExclusionFallbackPrerelease:
    """A pre-release IS admitted when the only final cannot be used.

    When backtracking excludes the only final that satisfies a requirement,
    every surviving candidate is a pre-release, and nab admits one rather
    than failing. This is the PEP 440 default (pre-releases are eligible
    when no final satisfies) applied to the surviving range; pip and uv
    reject the case instead. It is not the leaked-admission bug: the
    effective range's policy stays neutral and no usable final drop-in
    exists.
    """

    def test_admits_prerelease_when_only_final_is_unusable(self) -> None:
        """``c``'s only final (1.0) forces ``a>=2.0b1`` against ``a==1.0``."""
        provider = _FilterProvider(
            {
                "a": {V("1.0"): {}, V("2.0"): {}},
                "c": {
                    V("1.0"): {"d": SpecifierSet("==2.0")},
                    V("1.5a1"): {},
                    V("2.0b1"): {"a": SpecifierSet(">=2.0")},
                },
                "d": {V("1.0"): {}, V("2.0"): {"a": SpecifierSet(">=2.0b1")}},
            }
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {
                "a": SpecifierSet("==1.0").to_range(),
                "c": SpecifierSet(">=1.0").to_range(),
            }
        )
        assert result["a"] == V("1.0")
        assert result["c"] == V("1.5a1")


class TestConstraintPrereleases:
    """A constraint that names a prerelease must enable that prerelease."""

    def test_constraint_capping_at_prerelease_keeps_it(self) -> None:
        """``foo<=2.0b1`` must pick 2.0b1, not the older 1.5.

        PEP 440 enables pre-releases for a specifier that names one, so the
        constraint range carries an opt-in for 2.0b1. ``choose_version``
        intersects it with the requirement range; the opt-in survives, so
        ``filter`` admits 2.0b1 rather than falling back to 1.5.
        """
        provider = _FilterProvider(
            {
                "root": {V("1.0"): {"foo": SpecifierSet(">=1.0")}},
                "foo": {V("2.0b1"): {}, V("1.5"): {}, V("1.0"): {}},
            },
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {"root": VersionRange.singleton(V("1.0"))},
            constraints={"foo": SpecifierSet("<=2.0b1").to_range()},
        )
        assert result["foo"] == V("2.0b1")


class TestNoVersionsConstraintAttribution:
    """A superset constraint must not steal the NO_VERSIONS cause.

    A constraint naming a prerelease carries a prerelease policy the
    dependency range lacks. The old structural ``current & constraint ==
    current`` reported them unequal even though the constraint excluded
    nothing, so a genuine no-versions failure was labeled CONSTRAINT.
    """

    def test_superset_prerelease_constraint_keeps_no_versions(self) -> None:
        """``foo`` has no version in ``>=2.0``; ``>=1.0a1`` excluded nothing."""
        provider = PackagingProvider(
            {
                "root": {V("1.0"): {"foo": SpecifierSet(">=2.0")}},
                "foo": {V("1.0"): {}},
            },
        )
        with pytest.raises(ResolutionError) as exc_info:
            Resolver(provider, range_type=VersionRange, root_version="0").resolve(
                {"root": VersionRange.singleton(V("1.0"))},
                constraints={"foo": SpecifierSet(">=1.0a1").to_range()},
            )
        message = str(exc_info.value)
        assert "no versions of" in message
        assert "the user constrained" not in message


class TestVersionRangeDifference:
    """``A - B`` keeps the minuend's pre-release opt-in; the subtrahend grants none."""

    def test_difference_selects_versions_in_self_not_other(self) -> None:
        """``a - b`` keeps a's versions and drops b's, matching ``a & ~b``."""
        a = SpecifierSet(">=1.0").to_range()
        b = SpecifierSet(">=2.0").to_range()
        diff = a - b
        assert V("1.5") in diff
        assert diff.is_subset(a)
        assert diff.is_disjoint(b)
        assert diff == a & ~b

    def test_difference_with_empty_is_self(self) -> None:
        a = SpecifierSet(">=1.0,<2.0").to_range()
        assert (a - VersionRange.empty()) == a

    def test_difference_with_full_is_empty(self) -> None:
        assert (SpecifierSet(">=1.0").to_range() - VersionRange.full()).is_empty

    def test_difference_drops_subtrahend_prerelease_policy(self) -> None:
        """A final-only requirement minus a prerelease exclusion admits no pre.

        ``>=1.0`` admits no prereleases; subtracting ``>=2.0b1`` (which on its
        own admits 2.0b1) must NOT grant prerelease admission to the result.
        This is the resolver leak in miniature.
        """
        result = SpecifierSet(">=1.0").to_range() - SpecifierSet(">=2.0b1").to_range()
        assert list(result.filter([V("2.0b1"), V("1.5a1"), V("1.0")])) == [V("1.0")]

    def test_difference_keeps_minuend_prerelease_policy(self) -> None:
        """A prerelease-naming requirement keeps admitting its prerelease."""
        result = SpecifierSet(">=2.0b1").to_range() - SpecifierSet(">=3.0").to_range()
        assert list(result.filter([V("2.5"), V("2.0b1")])) == [V("2.5"), V("2.0b1")]

    def test_difference_over_arbitrary_literals(self) -> None:
        """Difference handles ``===`` literal ranges on both sides."""
        a = SpecifierSet("===1.0").to_range()
        assert V("1.0") in (a - SpecifierSet("===2.0").to_range())
        assert V("2.0") not in (a - SpecifierSet("===2.0").to_range())
        assert V("1.0") in (a - SpecifierSet(">=2.0").to_range())

    def test_sub_not_implemented_for_non_range(self) -> None:
        assert VersionRange.full().__sub__(object()) is NotImplemented  # type: ignore[arg-type]
