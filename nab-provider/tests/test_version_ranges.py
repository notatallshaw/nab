"""PEP 440 membership and range operations used by the provider."""

from __future__ import annotations

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version

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
