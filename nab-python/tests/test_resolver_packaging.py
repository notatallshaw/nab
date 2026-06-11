"""End-to-end resolver tests using PEP 440 versions.

Same test scenarios as nab-resolver's test_resolver.py but using
packaging.version.Version and packaging.specifiers.SpecifierSet
through the PackagingProvider.
"""

from __future__ import annotations

import pytest

from nab_python._packaging_provider import PackagingProvider
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.version import Version
from nab_resolver.resolver import ResolutionError, Resolver
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

    def test_term_subset_ignores_arbitrary_flag(self) -> None:
        """A specifier-built full range satisfies a flag-free full term.

        ``SpecifierSet("")`` produces the full range flagged as also
        admitting arbitrary ``===`` versions, while range unions built
        during conflict resolution produce the unflagged full range.
        The two compare unequal but their difference is empty, and term
        satisfaction follows the difference: conflict resolution would
        otherwise loop on a clause it can never resolve away.
        """
        flagged_full = SpecifierSet("").to_range()
        exact = VersionRange.singleton(V("1.0"))
        plain_full = exact | ~exact
        assert plain_full != flagged_full
        assert (flagged_full & ~plain_full).is_empty
        assert Term("pkg", plain_full).satisfies(flagged_full)

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
