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
from nab_resolver.partial_solution import PartialSolution
from nab_resolver.report import union_terms
from nab_resolver.resolver import ResolutionError, Resolver
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

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


class TestExclusionComplementPrerelease:
    """The vendored range algebra scopes a pre-release opt-in to its region.

    The solver derives an effective range as ``positive & ~negative``. An
    exclusion ``negative`` that names a pre-release must not carry that
    pre-release admission onto the intersection: ``~negative`` keeps the
    opt-in attached to ``negative``'s own versions, which the intersection
    has already removed, so the result admits no pre-release of its own.
    This is the leak the resolver hit, reproduced at the API it relies on.
    """

    def test_intersection_with_complement_drops_exclusion_prerelease(self) -> None:
        """``>=1.0 & ~(>=2.0b1)`` admits no pre-release."""
        positive = SpecifierSet(">=1.0").to_range()
        negative = SpecifierSet(">=2.0b1").to_range()
        effective = positive & ~negative
        picked = list(effective.filter([V("2.0b1"), V("1.5a1"), V("1.0")]))
        assert picked == [V("1.0")]

    def test_intersection_with_complement_keeps_own_prerelease(self) -> None:
        """A positive that names its own pre-release still admits it."""
        positive = SpecifierSet(">=2.0b1").to_range()
        negative = SpecifierSet(">=3.0").to_range()
        effective = positive & ~negative
        assert list(effective.filter([V("2.5"), V("2.0b1")])) == [V("2.5"), V("2.0b1")]


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
                "a": VersionRange.full(),
                "d": SpecifierSet("==1.0").to_range(),
            }
        )

        assert result["a"] == V("1.0")
        assert result["d"] == V("1.0")
        assert result["c"] == V("1.0")

    def test_capped_prerelease_exclusion_does_not_leak_above_cap(self) -> None:
        """A backtracked ``>=2.0b1,<3`` edge must not admit a pre-release at 3.5.

        The pre-release-naming edge caps ``c`` below 3, so once ``a 2.0`` is
        backtracked away no requirement opts ``c`` into a pre-release. The final
        ``c 1.0`` must win over ``c 3.5a1``.
        """
        provider = _FilterProvider(
            {
                "a": {
                    V("2.0"): {"c": SpecifierSet(">=2.0b1,<3")},
                    V("1.0"): {},
                },
                "c": {
                    V("3.5a1"): {},
                    V("2.0b1"): {"d": SpecifierSet("==2.0")},
                    V("1.0"): {},
                },
                "d": {V("1.0"): {}, V("2.0"): {}},
            }
        )
        result = Resolver(provider, range_type=VersionRange, root_version="0").resolve(
            {
                "a": VersionRange.full(),
                "c": SpecifierSet(">=1.0").to_range(),
                "d": SpecifierSet("==1.0").to_range(),
            }
        )
        assert result["a"] == V("1.0")
        assert result["c"] == V("1.0")


class TestConflictPathPrereleaseAdmission:
    """Conflict-resolution term algebra must not leak pre-release admission.

    ``Term.intersect`` and ``union_terms`` subtract a negative (exclusion)
    term from a positive one. The result must keep only the positive (or
    negative-minuend) side's pre-release policy, so a learned exclusion
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
        """The mixed-term remainder keeps the negative term's policy only."""
        negative = Term("c", SpecifierSet(">=1.0").to_range(), positive=False)
        positive = Term("c", SpecifierSet(">=2.0b1").to_range(), positive=True)
        result = union_terms(negative, positive)
        assert result is not None
        assert not result.is_positive()
        picked = list(result.constraint.filter([V("1.5a1"), V("1.0")]))
        assert picked == [V("1.0")]


class TestNegativeOnlyEffectiveRange:
    """A package holding only an exclusion grants no pre-release admission."""

    def test_negative_only_get_drops_prerelease(self) -> None:
        """``~negative`` keeps the neutral policy, not the exclusion's."""
        solution = PartialSolution(range_type=VersionRange)
        root = Incompatibility([], cause=IncompatibilityCause.ROOT)
        solution.derive(
            "foo", SpecifierSet(">=2.0b1").to_range(), positive=False, cause=root
        )
        effective = solution.get("foo")
        assert effective is not None
        picked = list(effective.filter([V("2.0b1"), V("1.5a1"), V("1.0")]))
        assert picked == [V("1.0")]


class TestExclusionFallbackPrerelease:
    """A pre-release IS admitted when the only final cannot be used.

    Intended behaviour: when backtracking excludes the only final that
    satisfies a requirement, the surviving candidates are all pre-releases,
    and nab admits one rather than failing. This is the PEP 440 default
    (pre-releases are eligible when no final satisfies) applied to the
    surviving range, and is deliberate; nab finds a solution here where pip
    and uv give up. It is not the leaked-admission bug: the effective range's
    policy stays neutral, and no usable final drop-in exists.
    """

    def test_admits_prerelease_when_only_final_is_unusable(self) -> None:
        """``c``'s only final (1.0) forces ``a==2.0b1`` against ``a==1.0``."""
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

        PEP 440 enables prereleases for a specifier that names one. A
        constraint is injected and read back through a double complement;
        the old vendored packaging dropped the prerelease policy there, so
        ``filter`` excluded 2.0b1 and the resolver fell back to 1.5.
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


class TestComplementPrereleases:
    """``VersionRange.complement`` drops the autodetected opt-in region."""

    def test_double_complement_drops_prerelease_admission(self) -> None:
        """``~~r`` keeps the versions but force-admits none of its prereleases.

        A complement is an exclusion and carries no opt-in, so the double
        complement covers the same versions as ``r`` yet buffers ``2.0b1`` away
        under the PEP 440 default. The resolver applies a user constraint by
        intersection (``current_range & constraint``), which keeps the opt-in,
        so a prerelease-naming constraint still resolves to its prerelease.
        """
        r = SpecifierSet("<=2.0b1").to_range()
        versions = [V("2.0b1"), V("1.5"), V("1.0")]
        assert list(r.filter(versions)) == versions
        assert list(r.complement().complement().filter(versions)) == [
            V("1.5"),
            V("1.0"),
        ]


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
