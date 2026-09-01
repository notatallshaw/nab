"""Tests for the failure-time attribution of the listing filter's drops.

The differential oracle is the anti-drift argument for a mechanism that
re-expresses the filter's order: over a matrix of policy configurations, the
walk keeps or refuses every file the filter saw, exactly once, and keeps the
versions the filter kept.  The rest pins what the user reads, one case per
clause per grammatical number, plus the remedy notes and the screen that
keeps the walk off a lead that would discard it.
"""

from __future__ import annotations

import re
from dataclasses import fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

from nab_provider._provider import listing as listing_mod
from nab_provider._provider import listing_diagnosis as diagnosis_mod
from nab_provider._provider.listing_diagnosis import (
    DropCause,
    NoVersionsReason,
    OverrideLayer,
    ReasonKind,
)
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.diagnostics import Diagnostic
from nab_provider.errors import InvalidUploadTimeError
from nab_provider.overrides import IndexOverride
from nab_provider.provider import (
    DistPolicy,
    ListingFilterCache,
    Provider,
    ProviderStats,
)
from nab_provider.records import SdistFile, WheelFile
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator, pkg_override

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_provider._vendor.packaging.ranges import VersionRange
    from nab_provider.provider import DistFile

CUTOFF = datetime(2026, 5, 1, tzinfo=timezone.utc)
EARLY_CUTOFF = datetime(2015, 1, 1, tzinfo=timezone.utc)
BEFORE = "2020-01-01T00:00:00Z"
BETWEEN = "2020-06-01T00:00:00Z"
AFTER = "2030-01-01T00:00:00Z"
CUTOFF_TEXT = "2026-05-01T00:00:00+00:00"
EARLY_CUTOFF_TEXT = "2015-01-01T00:00:00+00:00"
WITH_CUTOFF: dict[str, object] = {"uploaded_prior_to": CUTOFF}

_LINUX312 = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("linux_x86_64")
)


def short_reason(provider: Provider, package: str) -> str | None:
    """Return the one line ``package``'s diagnostic prints at default verbosity."""
    diagnostic = provider.get_no_versions_reason(package)
    return None if diagnostic is None else diagnostic.short


def wheel(
    version: str,
    *,
    tag: str = "py3-none-any",
    requires_python: str | None = None,
    upload_time: str | None = BEFORE,
) -> WheelFile:
    """A listing wheel; ``version`` doubles as the filename's version field."""
    filename = f"pkg-{version}-{tag}.whl"
    return WheelFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=version,
        requires_python=requires_python,
        has_metadata=True,
        upload_time=upload_time,
    )


def sdist(
    version: str,
    *,
    requires_python: str | None = None,
    upload_time: str | None = BEFORE,
) -> SdistFile:
    """A listing sdist, shaped like :func:`wheel`."""
    filename = f"pkg-{version}.tar.gz"
    return SdistFile(
        filename=filename,
        url=f"https://example.com/{filename}",
        version=version,
        requires_python=requires_python,
        upload_time=upload_time,
    )


def build(files: Sequence[WheelFile | SdistFile], **kwargs: object) -> Provider:
    """A provider over ``files``, with the listing already stored for ``pkg``."""
    coordinator = make_coordinator(list(files), package="pkg", auto_metadata=True)
    return Provider(coordinator, **kwargs)  # type: ignore[arg-type]


def render(diagnostic: Diagnostic) -> str:
    """Return an entry as one string: the line, then everything ``-v`` adds."""
    return "\n".join((diagnostic.short, *diagnostic.detail))


def diagnostic_for(
    files: Sequence[WheelFile | SdistFile], *, spec: str = "", **kwargs: object
) -> Diagnostic:
    """Ask ``pkg`` for ``spec`` and return the entry that comes back."""
    provider = build(files, **kwargs)
    assert provider.choose_version("pkg", SpecifierSet(spec).to_range()) is None
    diagnostic = provider.get_no_versions_reason("pkg")
    assert diagnostic is not None
    return diagnostic


def reason_for(
    files: Sequence[WheelFile | SdistFile], *, spec: str = "", **kwargs: object
) -> str:
    """Ask ``pkg`` for ``spec`` and return both depths of what comes back."""
    return render(diagnostic_for(files, spec=spec, **kwargs))


def short_for(
    files: Sequence[WheelFile | SdistFile], *, spec: str = "", **kwargs: object
) -> str:
    """Ask ``pkg`` for ``spec`` and return its default-verbosity line."""
    return diagnostic_for(files, spec=spec, **kwargs).short


def remedy_for(
    files: Sequence[WheelFile | SdistFile], *, spec: str = "", **kwargs: object
) -> str | None:
    """Ask ``pkg`` for ``spec`` and return what its ``try:`` line instructs."""
    return diagnostic_for(files, spec=spec, **kwargs).remedy


def empty_entry(files: Sequence[WheelFile | SdistFile], **kwargs: object) -> Diagnostic:
    """Build ``pkg``'s empty-listing entry without asking for a version.

    The filter refuses the whole run on a timezone-naive upload time, so a
    listing carrying one cannot be reached through :func:`reason_for`.
    """
    provider = build(files, **kwargs)
    diagnosis = provider.diagnose_listing("pkg")
    assert diagnosis is not None
    return diagnosis_mod.empty_listing_diagnostic(provider, "pkg", diagnosis)


def rendered_for(files: Sequence[WheelFile | SdistFile], **kwargs: object) -> str:
    """Render both depths of the entry :func:`empty_entry` builds."""
    return render(empty_entry(files, **kwargs))


# One file per cause, plus a survivor, so a walk over it exercises every rung
# that can fire without ending the run.
ORACLE_LISTING: list[WheelFile | SdistFile] = [
    wheel("not-a-version"),
    wheel("1.0", requires_python=">=3.99"),
    sdist("1.0"),
    wheel("2.0", upload_time=None),
    sdist("2.0", upload_time="not-a-time"),
    wheel("3.0", upload_time=AFTER),
    sdist("3.0"),
    wheel("4.0", tag="cp312-cp312-win_amd64"),
    wheel("5.0"),
    sdist("5.0"),
]


ORACLE_CONFIGS: dict[str, dict[str, object]] = {
    "defaults": {},
    "target-and-cutoff": {"target": _LINUX312, "uploaded_prior_to": CUTOFF},
    "wheel-only": {
        "target": _LINUX312,
        "uploaded_prior_to": CUTOFF,
        "dist_policy": DistPolicy.WHEEL_ONLY,
    },
    "sdist-only": {
        "target": _LINUX312,
        "uploaded_prior_to": CUTOFF,
        "dist_policy": DistPolicy.SDIST_ONLY,
    },
    "sdist-install": {
        "target": _LINUX312,
        "uploaded_prior_to": CUTOFF,
        "dist_policy": DistPolicy.SDIST_INSTALL,
    },
    "prefer-wheel": {"target": _LINUX312, "dist_policy": DistPolicy.PREFER_WHEEL},
    "no-tags": {"uploaded_prior_to": CUTOFF},
    "package-override": {
        "target": _LINUX312,
        "uploaded_prior_to": CUTOFF,
        "package_overrides": [
            pkg_override("pkg>=3", dist_policy=DistPolicy.WHEEL_ONLY),
        ],
    },
    "index-override": {
        "target": _LINUX312,
        "index_overrides": {"pypi": IndexOverride(uploaded_prior_to=CUTOFF)},
    },
    "both-surfaces-different-fields": {
        "target": _LINUX312,
        "package_overrides": [pkg_override("pkg<2", requires_python="")],
        "index_overrides": {"pypi": IndexOverride(uploaded_prior_to=CUTOFF)},
    },
}


class TestDifferentialOracle:
    """The walk partitions the listing the filter partitioned, the same way."""

    @pytest.mark.parametrize("config", sorted(ORACLE_CONFIGS), ids=str)
    @pytest.mark.parametrize("pythons", [1, 3], ids=["one-python", "matrix-memo"])
    def test_every_file_is_kept_or_refused_exactly_once(
        self, config: str, pythons: int
    ) -> None:
        """No file is claimed twice, dropped silently, or refused after surviving.

        The kept sets agree too, so no clause can describe a release that
        survived the filter.
        """
        kwargs = dict(ORACLE_CONFIGS[config])
        kwargs["listing_filter_cache"] = ListingFilterCache(pythons)
        provider = build(ORACLE_LISTING, **kwargs)

        kept = provider.fetch_versions("pkg")
        diagnosis = provider.diagnose_listing("pkg")
        assert diagnosis is not None

        kept_names = [dist.filename for _version, dist in kept]
        dropped_names = [record.filename for record in diagnosis.dropped]
        assert sorted(kept_names + dropped_names) == sorted(
            dist.filename for dist in ORACLE_LISTING
        )
        assert not set(kept_names) & set(dropped_names)

        assert diagnosis.kept == {version for version, _dist in kept}
        assert diagnosis.unexplained == 0

    def test_a_drop_the_walk_cannot_model_is_counted_not_hidden(self) -> None:
        """A filter that removes more than the rungs explain says so in the line.

        Driven through the ``filter_distributions`` override seam, which
        ``fetch_versions`` routes through so a subclass's answer is what
        reaches ``versions_cache``.  It is the only way a host can drop a
        file no rung of the walk knows about.
        """

        class DropsEverything(Provider):
            def filter_distributions(
                self, normalized: str, files: Sequence[WheelFile | SdistFile]
            ) -> list[tuple[Version, object]]:
                super().filter_distributions(normalized, files)
                return []

        coordinator = make_coordinator([wheel("1.0"), wheel("2.0")], package="pkg")
        provider = DropsEverything(coordinator)
        assert provider.choose_version("pkg", SpecifierSet("").to_range()) is None

        assert short_reason(provider, "pkg") == (
            "every file was refused; the filter cannot be named"
        )

    def test_an_unmodelled_drop_answers_the_in_range_lead_too(self) -> None:
        """The in-range lead names no filter but still says the release was filtered.

        No rung explains the drop, so there is nothing to put in the
        parenthetical; the release the requirement asked for is still gone
        from a listing that published it, which the no-match line denies.
        """

        class DropsTheAskedRelease(Provider):
            def filter_distributions(
                self, normalized: str, files: Sequence[WheelFile | SdistFile]
            ) -> list[tuple[Version, DistFile]]:
                kept = super().filter_distributions(normalized, files)
                return [pair for pair in kept if pair[0] != Version("1.0")]

        coordinator = make_coordinator([wheel("1.0"), wheel("2.0")], package="pkg")
        provider = DropsTheAskedRelease(coordinator)
        assert provider.choose_version("pkg", SpecifierSet("==1.0").to_range()) is None

        assert short_reason(provider, "pkg") == (
            "every version in range was refused; the filter cannot be named"
        )

    def test_one_unmodelled_drop_reads_as_one_version(self) -> None:
        """The singular of the same clause."""
        coordinator = make_coordinator([wheel("1.0")], package="pkg")
        provider = Provider(coordinator)
        provider.fetch_versions("pkg")
        provider.versions_cache["pkg"] = []

        diagnosis = provider.diagnose_listing("pkg")
        assert diagnosis is not None
        assert render(
            diagnosis_mod.empty_listing_diagnostic(provider, "pkg", diagnosis)
        ) == (
            "every file was refused; the filter cannot be named"
            "\n1 version was dropped for a reason this report cannot name"
            "\nthe files nab read hold no sdist to build from"
        )

    def test_a_named_rung_beside_an_unmodelled_drop_still_names_its_key(self) -> None:
        """One rung explains one file and nothing explains the other.

        The line cannot say a single rung emptied the listing, so it joins
        what it has, which here is the one key.
        """

        class DropsTheSurvivor(Provider):
            def filter_distributions(
                self, normalized: str, files: Sequence[WheelFile | SdistFile]
            ) -> list[tuple[Version, DistFile]]:
                kept = super().filter_distributions(normalized, files)
                return [pair for pair in kept if pair[0] != Version("2.0")]

        coordinator = make_coordinator(
            [wheel("1.0", upload_time=AFTER), wheel("2.0")], package="pkg"
        )
        provider = DropsTheSurvivor(coordinator, uploaded_prior_to=CUTOFF)
        assert provider.choose_version("pkg", SpecifierSet("").to_range()) is None

        diagnosis = provider.diagnose_listing("pkg")
        assert diagnosis is not None
        assert diagnosis.unexplained == 1
        assert diagnosis_mod.empty_listing_diagnostic(
            provider, "pkg", diagnosis
        ).short == ("uploaded-prior-to excluded every file")


class TestTheWalkRunsOnlyWhereItIsRead:
    """The walk costs one record per dropped file, so it is screened first."""

    def test_a_version_the_index_never_published_never_walks(self) -> None:
        """A range holding nothing the filter dropped answers without the walk."""
        provider = build([wheel("1.0"), wheel("2.0")])
        assert provider.choose_version("pkg", SpecifierSet(">=5").to_range()) is None

        assert short_reason(provider, "pkg") == ("no version matches the requirement")
        assert provider.listing_diagnoses == {}

    def test_a_dropped_release_inside_the_range_does_walk(self) -> None:
        """The same screen lets the walk run where its detail is quoted."""
        provider = build(
            [wheel("1.0"), wheel("2.0", requires_python=">=3.99")], target=_LINUX312
        )
        assert provider.choose_version("pkg", SpecifierSet(">=2").to_range()) is None

        assert short_reason(provider, "pkg") == (
            "no version in range supports Python 3.12"
        )
        assert list(provider.listing_diagnoses) == ["pkg"]


class TestTheWalkLeavesNoTrace:
    """The walk calls counting predicates, so it has to put the counts back."""

    def test_the_counters_and_the_tag_tally_are_unchanged(self) -> None:
        """A diagnosis must not move a number the benchmarks or the lock read."""
        provider = build(ORACLE_LISTING, target=_LINUX312, uploaded_prior_to=CUTOFF)
        provider.fetch_versions("pkg")

        counters = {
            field.name: getattr(provider.stats, field.name)
            for field in fields(ProviderStats)
        }
        tallies = dict(provider.tag_excluded_wheels_by_version)

        provider.diagnose_listing("pkg")
        provider.diagnose_listing("pkg")

        assert counters == {
            field.name: getattr(provider.stats, field.name)
            for field in fields(ProviderStats)
        }
        assert tallies == provider.tag_excluded_wheels_by_version
        assert counters["excluded_by_wheel_tags"] > 0
        assert tallies

    def test_the_diagnosis_is_walked_once_per_package(self) -> None:
        """The memo hands back the same object rather than walking again."""
        provider = build(ORACLE_LISTING, target=_LINUX312)
        provider.fetch_versions("pkg")

        first = provider.diagnose_listing("pkg")
        assert first is not None
        assert provider.diagnose_listing("pkg") is first

    def test_a_second_render_keeps_the_causes_the_walk_found(self) -> None:
        """The memo replays the whole diagnosis, so the sentence cannot thin."""
        first = reason_for([wheel("1.0", upload_time=AFTER)], **WITH_CUTOFF)
        provider = build([wheel("1.0", upload_time=AFTER)], **WITH_CUTOFF)
        assert provider.choose_version("pkg", SpecifierSet("").to_range()) is None

        second = provider.get_no_versions_reason("pkg")
        assert second is not None
        assert render(second) == first
        assert render(second) == first
        assert "excluded 1 file uploaded at" in first

    def test_an_absent_listing_memoises_its_own_absence(self) -> None:
        """A package the index never served is walked once and answers None."""
        provider = build([])
        assert provider.diagnose_listing("pkg") is None
        assert provider.diagnose_listing("pkg") is None
        assert provider.listing_diagnoses == {"pkg": None}


class TestClauseText:
    """One clause per cause, singular and plural, over a real filter run."""

    def test_missing_upload_time(self) -> None:
        assert (
            f"the uploaded-prior-to cutoff {CUTOFF_TEXT} excluded 1 file that"
            " publishes no upload time (1.0)"
        ) in reason_for([wheel("1.0", upload_time=None)], **WITH_CUTOFF)

    def test_missing_upload_time_plural(self) -> None:
        assert (
            f"the uploaded-prior-to cutoff {CUTOFF_TEXT} excluded 2 files that"
            " publish no upload time (newest: 2.0)"
        ) in reason_for(
            [wheel("1.0", upload_time=None), wheel("2.0", upload_time=None)],
            **WITH_CUTOFF,
        )

    def test_unparseable_upload_time(self) -> None:
        assert (
            "the uploaded-prior-to cutoff excluded 1 file whose upload time is"
            " not ISO 8601 (1.0, 'not-a-time')"
        ) in reason_for([wheel("1.0", upload_time="not-a-time")], **WITH_CUTOFF)

    def test_unparseable_upload_time_plural(self) -> None:
        assert (
            "the uploaded-prior-to cutoff excluded 2 files whose upload time is"
            " not ISO 8601 (newest: 2.0, 'also-not-a-time')"
        ) in reason_for(
            [
                wheel("1.0", upload_time="not-a-time"),
                wheel("2.0", upload_time="also-not-a-time"),
            ],
            **WITH_CUTOFF,
        )

    def test_after_cutoff(self) -> None:
        assert (
            f"the uploaded-prior-to cutoff {CUTOFF_TEXT} excluded 1 file"
            f" uploaded at {AFTER} (1.0)"
        ) in reason_for([wheel("1.0", upload_time=AFTER)], **WITH_CUTOFF)

    def test_after_cutoff_plural(self) -> None:
        assert (
            f"the uploaded-prior-to cutoff {CUTOFF_TEXT} excluded 2 files"
            f" uploaded on or after it (newest: 2.0, uploaded {AFTER})"
        ) in reason_for(
            [wheel("1.0", upload_time=AFTER), wheel("2.0", upload_time=AFTER)],
            **WITH_CUTOFF,
        )

    def test_a_naive_upload_time_is_a_clause_not_an_error(self) -> None:
        """The walk answers where the filter refuses the run.

        The filter raises :class:`InvalidUploadTimeError` on a naive stamp and
        ends the resolve, so this cause is not reachable from one.  It is
        modelled because the walk runs inside an error path, where turning a
        report into an exception would lose the report.
        """
        assert (
            "the uploaded-prior-to cutoff could not judge 1 file whose upload"
            " time carries no timezone (1.0, '2020-01-01T00:00:00')"
        ) in rendered_for(
            [wheel("1.0", upload_time="2020-01-01T00:00:00")], **WITH_CUTOFF
        )

    def test_a_naive_upload_time_plural(self) -> None:
        assert (
            "the uploaded-prior-to cutoff could not judge 2 files whose upload"
            " time carries no timezone (newest: 2.0, '2021-01-01T00:00:00')"
        ) in rendered_for(
            [
                wheel("1.0", upload_time="2020-01-01T00:00:00"),
                wheel("2.0", upload_time="2021-01-01T00:00:00"),
            ],
            **WITH_CUTOFF,
        )

    def test_dist_policy(self) -> None:
        assert 'dist-policy = "wheel-only" excluded 1 sdist (1.0)' in reason_for(
            [sdist("1.0")], dist_policy=DistPolicy.WHEEL_ONLY
        )

    def test_dist_policy_plural(self) -> None:
        assert (
            'dist-policy = "sdist-only" excluded 2 wheels (newest: 2.0)'
        ) in reason_for([wheel("1.0"), wheel("2.0")], dist_policy=DistPolicy.SDIST_ONLY)

    def test_two_cutoffs_over_one_listing_read_as_two_clauses(self) -> None:
        """A clause names the cutoff that refused the files it counts.

        The version-scoped override gives 1.0 a cutoff of its own.  One
        clause over both files would count 1.0 among the files uploaded on
        or after a cutoff it precedes by nearly six years.
        """
        assert reason_for(
            [wheel("1.0", upload_time=BETWEEN), wheel("2.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[pkg_override("pkg<2", uploaded_prior_to=EARLY_CUTOFF)],
        ) == (
            "uploaded-prior-to excluded every file"
            f"\nthe uploaded-prior-to cutoff {EARLY_CUTOFF_TEXT} excluded 1 file"
            f" uploaded at {BETWEEN} (1.0)"
            f"\nthe uploaded-prior-to cutoff {CUTOFF_TEXT} excluded 1 file"
            f" uploaded at {AFTER} (2.0)"
            "\nthe files nab read hold no sdist to build from"
            "\nnote: the uploaded-prior-to on pkg<2 set that cutoff; setting it"
            " to false there lifts it"
            "\nnote: the project-level uploaded-prior-to set that cutoff; pkg<2"
            " already sets uploaded-prior-to over another version range, so widen"
            " that entry over this version or drop the project-level cutoff"
        )

    def test_two_cutoffs_over_files_with_no_upload_time_read_as_two_clauses(
        self,
    ) -> None:
        """This clause names a cutoff too, so two of them split it in two.

        Neither file publishes an upload time, and the cutoff each was
        judged against is the one its own clause has to name.
        """
        assert reason_for(
            [wheel("1.0", upload_time=None), wheel("2.0", upload_time=None)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[pkg_override("pkg<2", uploaded_prior_to=EARLY_CUTOFF)],
        ).startswith(
            "uploaded-prior-to excluded every file; none is dated"
            f"\nthe uploaded-prior-to cutoff {EARLY_CUTOFF_TEXT} excluded 1 file"
            " that publishes no upload time (1.0)"
            f"\nthe uploaded-prior-to cutoff {CUTOFF_TEXT} excluded 1 file that"
            " publishes no upload time (2.0)"
        )

    def test_two_dist_policies_over_one_listing_read_as_two_clauses(self) -> None:
        """A clause names the policy that refused the artifacts it counts.

        ``sdist-only`` refused the wheel and ``wheel-only`` the sdist, so a
        single clause would name one policy over two files and call both of
        them the kind only one of them is.
        """
        assert reason_for(
            [wheel("1.0"), sdist("2.0")],
            dist_policy=DistPolicy.WHEEL_ONLY,
            package_overrides=[
                pkg_override("pkg==1.0", dist_policy=DistPolicy.SDIST_ONLY)
            ],
        ) == (
            'dist-policy = "sdist-only" and "wheel-only" excluded every file'
            '\ndist-policy = "sdist-only" excluded 1 wheel (1.0)'
            '\ndist-policy = "wheel-only" excluded 1 sdist (2.0)'
            "\nnote: the dist-policy on pkg==1.0 set that policy; setting it to"
            ' "wheel-or-sdist" there admits both formats'
            "\nnote: the project-level dist-policy set that policy; pkg==1.0"
            " already sets dist-policy over another version range, so widen that"
            " entry over this version or drop the project-level policy"
        )

    def test_sdist_install_without_an_sdist(self) -> None:
        """The whole sentence: this clause already says no sdist is available."""
        assert reason_for([wheel("1.0")], dist_policy=DistPolicy.SDIST_INSTALL) == (
            'dist-policy = "sdist-install" excluded every version'
            '\ndist-policy = "sdist-install" excluded 1 version that publishes'
            " no sdist (1.0)"
            "\nnote: the project-level dist-policy set that policy; setting"
            ' packages."pkg".dist-policy = "wheel-or-sdist" admits both formats'
            " for this package"
        )

    def test_sdist_install_is_asked_before_the_wheel_tags(self) -> None:
        """The rung that refused the version first is the one the clause names.

        The filter drops a wheel-only ``sdist-install`` version in the base
        pass, before the target's tags are consulted, so blaming the tags
        would point at a config key that was never reached.
        """
        assert reason_for(
            [wheel("1.0", tag="cp312-cp312-win_amd64")],
            target=_LINUX312,
            dist_policy=DistPolicy.SDIST_INSTALL,
        ) == (
            'dist-policy = "sdist-install" excluded every version'
            '\ndist-policy = "sdist-install" excluded 1 version that publishes'
            " no sdist (1.0)"
            "\nnote: the project-level dist-policy set that policy; setting"
            ' packages."pkg".dist-policy = "wheel-or-sdist" admits both formats'
            " for this package"
        )

    def test_two_wheels_of_one_release_are_one_version(self) -> None:
        """This clause counts versions where every other one counts files."""
        assert reason_for(
            [wheel("1.0"), wheel("1.0", tag="py2-none-any")],
            dist_policy=DistPolicy.SDIST_INSTALL,
        ) == (
            'dist-policy = "sdist-install" excluded every version'
            '\ndist-policy = "sdist-install" excluded 1 version that publishes'
            " no sdist (1.0)"
            "\nnote: the project-level dist-policy set that policy; setting"
            ' packages."pkg".dist-policy = "wheel-or-sdist" admits both formats'
            " for this package"
        )

    def test_sdist_install_without_an_sdist_plural(self) -> None:
        assert (
            'dist-policy = "sdist-install" excluded 2 versions that publish no'
            " sdist (newest: 2.0)"
        ) in reason_for(
            [wheel("1.0"), wheel("2.0")], dist_policy=DistPolicy.SDIST_INSTALL
        )

    def test_requires_python(self) -> None:
        assert (
            "requires-python excluded 1 file (1.0 requires >=3.99, the resolve"
            " targets Python 3.12)"
        ) in reason_for([wheel("1.0", requires_python=">=3.99")], target=_LINUX312)

    def test_an_overridden_requires_python_is_the_spec_quoted(self) -> None:
        """The override replaces the file's own metadata, so it is what refused it.

        The wheel declares no Requires-Python at all, so quoting the file
        would print ``None`` where the entry that fired says ``>=3.99``.
        """
        assert (
            "requires-python excluded 1 file (1.0 requires >=3.99, the resolve"
            " targets Python 3.12)"
        ) in reason_for(
            [wheel("1.0")],
            target=_LINUX312,
            package_overrides=[pkg_override("pkg", requires_python=">=3.99")],
        )

    def test_requires_python_plural(self) -> None:
        assert (
            "requires-python excluded 2 files (newest: 2.0 requires >=4, the"
            " resolve targets Python 3.12)"
        ) in reason_for(
            [
                wheel("1.0", requires_python=">=3.99"),
                wheel("2.0", requires_python=">=4"),
            ],
            target=_LINUX312,
        )

    def test_wheel_tags(self) -> None:
        assert (
            "no wheel's tags are compatible with the resolve target (1 wheel rejected)"
        ) in reason_for([wheel("1.0", tag="cp312-cp312-win_amd64")], target=_LINUX312)

    def test_wheel_tags_plural(self) -> None:
        assert (
            "no wheel's tags are compatible with the resolve target (2 wheels rejected)"
        ) in reason_for(
            [
                wheel("1.0", tag="cp312-cp312-win_amd64"),
                wheel("2.0", tag="cp312-cp312-win_amd64"),
            ],
            target=_LINUX312,
        )

    def test_invalid_version(self) -> None:
        assert (
            "1 file carries a version PEP 440 cannot parse ('not-a-version')"
        ) in reason_for([wheel("not-a-version")])

    def test_invalid_version_plural_quotes_the_first(self) -> None:
        assert (
            "2 files carry a version PEP 440 cannot parse (first: 'not-a-version')"
        ) in reason_for([wheel("not-a-version"), wheel("nor-is-this")])

    def test_clauses_print_in_report_order(self) -> None:
        """Two causes on one listing print cutoff first, Requires-Python second.

        Report order is :data:`DropCause.REPORT_ORDER` and is deliberately
        not the order the filter asks the questions in.  This is also the
        whole shape of the message: lead, clauses, sdist tail, then the note.
        """
        assert reason_for(
            [wheel("1.0", requires_python=">=3.99"), wheel("2.0", upload_time=AFTER)],
            target=_LINUX312,
            uploaded_prior_to=CUTOFF,
        ) == (
            "uploaded-prior-to and requires-python excluded every file"
            "\nthe uploaded-prior-to cutoff 2026-05-01T00:00:00+00:00 excluded 1"
            " file uploaded at 2030-01-01T00:00:00Z (2.0)"
            "\nrequires-python excluded 1 file (1.0 requires >=3.99, the resolve"
            " targets Python 3.12)"
            "\nthe files nab read hold no sdist to build from"
            "\nnote: the project-level uploaded-prior-to set that cutoff; setting"
            ' packages."pkg".uploaded-prior-to = false lifts it for this package'
        )

    def test_a_line_naming_two_keys_leaves_the_values_to_v(self) -> None:
        """Two keys make the line a summary, and the clauses under it carry the values."""
        assert short_for(
            [wheel("1.0", upload_time=AFTER), sdist("2.0")],
            dist_policy=DistPolicy.WHEEL_ONLY,
            uploaded_prior_to=CUTOFF,
        ) == ("uploaded-prior-to and dist-policy excluded every file")

    def test_two_policy_values_over_one_listing_are_both_named(self) -> None:
        """One key, two entries, two values, and neither of them can be dropped.

        ``sdist-only`` refused the wheel and ``wheel-only`` the sdist.  The
        line is about one key, so it says the key once and both values.
        """
        assert short_for(
            [wheel("1.0"), sdist("2.0")],
            dist_policy=DistPolicy.WHEEL_ONLY,
            package_overrides=[
                pkg_override("pkg==1.0", dist_policy=DistPolicy.SDIST_ONLY)
            ],
        ) == ('dist-policy = "sdist-only" and "wheel-only" excluded every file')

    def test_four_filters_are_counted(self) -> None:
        """Past three the line stops naming and says how many.

        Four keys spelled out ran to 94 characters on a live run, which is
        past the point a bullet reads at a glance.
        """
        assert short_for(
            [
                wheel("1.0", upload_time=AFTER),
                sdist("2.0"),
                wheel("3.0", requires_python=">=3.99"),
                wheel("4.0", tag="cp312-cp312-win_amd64"),
            ],
            dist_policy=DistPolicy.WHEEL_ONLY,
            uploaded_prior_to=CUTOFF,
            target=_LINUX312,
        ) == ("4 filters excluded every file")

    def test_the_first_rung_that_refuses_a_file_is_the_one_named(self) -> None:
        """Two rungs that both refuse a wheel read as the one the filter asked first.

        ``dist-policy`` runs ahead of ``requires-python``, so the wheel is
        gone before its ``Requires-Python`` is consulted and the clause must
        not blame a rung that never ran.
        """
        assert reason_for(
            [wheel("1.0", requires_python=">=3.99")],
            target=_LINUX312,
            dist_policy=DistPolicy.SDIST_ONLY,
        ) == (
            'dist-policy = "sdist-only" excluded every file'
            '\ndist-policy = "sdist-only" excluded 1 wheel (1.0)'
            "\nthe files nab read hold no sdist to build from"
            "\nnote: the project-level dist-policy set that policy; setting"
            ' packages."pkg".dist-policy = "wheel-or-sdist" admits both formats'
            " for this package"
        )

    def test_an_sdist_on_the_index_takes_no_no_sdist_tail(self) -> None:
        """The tail says the index published no sdist, not that none survived."""
        reason = reason_for([sdist("1.0", requires_python=">=3.99")], target=_LINUX312)
        assert "the files nab read hold no sdist to build from" not in reason


class TestTheRemedyNamesTheLayer:
    """Which of the three config layers set the cutoff that fired."""

    def test_the_project_level_cutoff(self) -> None:
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)], uploaded_prior_to=CUTOFF
        ).endswith(
            "\nnote: the project-level uploaded-prior-to set that cutoff;"
            ' setting packages."pkg".uploaded-prior-to = false lifts it for this'
            " package"
        )

    def test_a_per_package_cutoff(self) -> None:
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)],
            package_overrides=[
                pkg_override(
                    "pkg", uploaded_prior_to=CUTOFF, source_label="packages.'pkg'"
                )
            ],
        ) == (
            "uploaded-prior-to excluded every file"
            f"\nthe uploaded-prior-to cutoff {CUTOFF_TEXT} excluded 1 file"
            f" uploaded at {AFTER} (1.0)"
            "\nthe files nab read hold no sdist to build from"
            "\nnote: the uploaded-prior-to on packages.'pkg' set that cutoff;"
            " setting it to false there lifts it"
        )

    def test_the_note_reads_the_layer_at_the_newest_version_refused(self) -> None:
        """Two layers refused two releases, and only one of them is worth lifting.

        The project cutoff refused 1.0 and the ``pkg>=3`` entry refused 3.0.
        Naming the project level here would point at a cutoff that has
        nothing to do with the newest release the user could have had.
        """
        assert reason_for(
            [wheel("1.0", upload_time=AFTER), wheel("3.0", upload_time=BETWEEN)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[pkg_override("pkg>=3", uploaded_prior_to=EARLY_CUTOFF)],
        ).endswith(
            "\nnote: the uploaded-prior-to on pkg>=3 set that cutoff; setting"
            " it to false there lifts it"
        )

    @pytest.mark.parametrize(
        "index_name",
        ["pypi", "corp mirror", "my.index"],
        ids=["bare", "spaced", "dotted"],
    )
    def test_a_per_index_cutoff(self, index_name: str) -> None:
        """The note names the entry the user wrote, whatever the index is called.

        Index names are free-form, and a name carrying a space or a dot is
        one no bare TOML key can spell.
        """
        provider = build(
            [wheel("1.0", upload_time=AFTER)],
            index_overrides={index_name: IndexOverride(uploaded_prior_to=CUTOFF)},
        )
        provider.coordinator.index.store_listing_index("pkg", index_name)
        assert provider.choose_version("pkg", SpecifierSet("").to_range()) is None

        diagnostic = provider.get_no_versions_reason("pkg")
        assert diagnostic is not None
        assert render(diagnostic).endswith(
            f"the uploaded-prior-to cutoff {CUTOFF_TEXT} excluded 1 file uploaded"
            f" at {AFTER} (1.0)"
            "\nthe files nab read hold no sdist to build from"
            f'\nnote: the per-index uploaded-prior-to for index "{index_name}"'
            " set that cutoff; setting it to false there lifts it"
        )

    def test_a_package_that_already_scopes_the_cutoff_is_offered_no_entry(
        self,
    ) -> None:
        """The project level answered, but a second package entry would conflict.

        The ``pkg<2`` entry sets ``uploaded-prior-to`` over another range, and
        the config layer refuses two per-package entries setting one field over
        overlapping versions.  A bare-name entry overlaps everything, so the
        remedy names the project-level cutoff and stops.
        """
        assert reason_for(
            [wheel("1.0", upload_time=BETWEEN), wheel("2.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[pkg_override("pkg<2", uploaded_prior_to=EARLY_CUTOFF)],
        ).endswith(
            "\nnote: the project-level uploaded-prior-to set that cutoff; pkg<2"
            " already sets uploaded-prior-to over another version range, so widen"
            " that entry over this version or drop the project-level cutoff"
        )

    def test_a_package_scoping_another_field_still_gets_the_entry(self) -> None:
        """Only an uploaded-prior-to entry blocks the suggestion.

        Two per-package entries may set different fields over overlapping
        ranges, and this one is keyed by a selector of its own, so the
        remedy can still write ``packages."pkg"``.
        """
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[
                pkg_override(
                    "pkg<2",
                    dist_policy=DistPolicy.WHEEL_OR_SDIST,
                    name_keyed=True,
                    source_label="packages.'pkg<2'",
                )
            ],
        ).endswith(
            "\nnote: the project-level uploaded-prior-to set that cutoff;"
            ' setting packages."pkg".uploaded-prior-to = false lifts it for this'
            " package"
        )

    def test_a_bare_name_table_setting_another_field_is_named(self) -> None:
        """``packages."pkg"`` is declared already, and TOML has one of each.

        The project cutoff is what refused the file, so the remedy is the
        one that writes ``packages."pkg"``.  The package already has that
        table, so the note sends the reader into it rather than asking for
        a second declaration of it.
        """
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[
                pkg_override(
                    "pkg",
                    dist_policy=DistPolicy.WHEEL_OR_SDIST,
                    name_keyed=True,
                    source_label="packages.'pkg'",
                )
            ],
        ).endswith(
            "\nnote: the project-level uploaded-prior-to set that cutoff;"
            " packages.'pkg' already exists, so adding uploaded-prior-to = false"
            " there lifts it for this package"
        )

    def test_a_bare_name_table_setting_another_field_is_named_for_the_policy(
        self,
    ) -> None:
        """The policy takes the same turn, since the collision is the table."""
        assert reason_for(
            [wheel("1.0")],
            target=_LINUX312,
            dist_policy=DistPolicy.SDIST_ONLY,
            package_overrides=[
                pkg_override(
                    "pkg",
                    uploaded_prior_to=None,
                    uploaded_prior_to_disabled=True,
                    name_keyed=True,
                    source_label="packages.'pkg'",
                )
            ],
        ).endswith(
            "\nnote: the project-level dist-policy set that policy;"
            " packages.'pkg' already exists, so adding"
            ' dist-policy = "wheel-or-sdist" there admits both formats for this'
            " package"
        )

    def test_a_rule_matching_two_packages_says_how_wide_it_is(self) -> None:
        """A rule is one entry across a ``match`` list, and changing it moves all of it.

        Both overrides carry the entry's label, which is how the note counts
        what the reader is about to change.
        """
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)],
            package_overrides=[
                pkg_override(
                    "pkg",
                    uploaded_prior_to=CUTOFF,
                    source_label="package-rules[0]",
                ),
                pkg_override(
                    "other",
                    uploaded_prior_to=CUTOFF,
                    source_label="package-rules[0]",
                ),
            ],
        ).endswith(
            "\nnote: the uploaded-prior-to on package-rules[0], which matches 2"
            " packages, set that cutoff; setting it to false there lifts it"
        )

    def test_a_scoped_rule_matching_two_packages_says_how_wide_it_is(self) -> None:
        """The entry a widening points at is as wide as the one a setting points at."""
        assert reason_for(
            [wheel("2.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[
                pkg_override(
                    "pkg<2",
                    uploaded_prior_to=EARLY_CUTOFF,
                    source_label="package-rules[0]",
                ),
                pkg_override(
                    "other<2",
                    uploaded_prior_to=EARLY_CUTOFF,
                    source_label="package-rules[0]",
                ),
            ],
        ).endswith(
            "\nnote: the project-level uploaded-prior-to set that cutoff;"
            " package-rules[0], which matches 2 packages, already sets"
            " uploaded-prior-to over another version range, so widen that entry"
            " over this version or drop the project-level cutoff"
        )

    def test_a_rule_matching_one_package_says_nothing_about_its_width(self) -> None:
        """One package is what the reader already knows, so the note stays quiet."""
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)],
            package_overrides=[
                pkg_override(
                    "pkg",
                    uploaded_prior_to=CUTOFF,
                    source_label="package-rules[0]",
                )
            ],
        ).endswith(
            "\nnote: the uploaded-prior-to on package-rules[0] set that cutoff;"
            " setting it to false there lifts it"
        )

    def test_two_selectors_for_one_package_are_one_package(self) -> None:
        """A ``match`` list may hold two selectors of the same package."""
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)],
            package_overrides=[
                pkg_override(
                    "pkg<5",
                    uploaded_prior_to=CUTOFF,
                    source_label="package-rules[0]",
                ),
                pkg_override(
                    "pkg>=5",
                    uploaded_prior_to=CUTOFF,
                    source_label="package-rules[0]",
                ),
            ],
        ).endswith(
            "\nnote: the uploaded-prior-to on package-rules[0] set that cutoff;"
            " setting it to false there lifts it"
        )

    def test_an_index_scoping_another_field_is_not_the_layer(self) -> None:
        """An index entry answers for the cutoff only when it sets one.

        The entry here sets ``dist-policy``, so the cutoff came from the
        project level and naming the index would send the user to a key
        that never judged the file.
        """
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            index_overrides={
                "pypi": IndexOverride(dist_policy=DistPolicy.PREFER_WHEEL)
            },
        ).endswith(
            "\nnote: the project-level uploaded-prior-to set that cutoff;"
            ' setting packages."pkg".uploaded-prior-to = false lifts it for this'
            " package"
        )

    def test_an_override_with_no_source_label_names_its_requirement(self) -> None:
        """A host that builds overrides itself leaves the label unset.

        ``nab_provider`` is published on its own, and the note has to name
        something the reader can find either way.
        """
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)],
            package_overrides=[pkg_override("pkg<2", uploaded_prior_to=CUTOFF)],
        ).endswith(
            "\nnote: the uploaded-prior-to on pkg<2 set that cutoff; setting it"
            " to false there lifts it"
        )

    def test_an_index_override_on_another_index_is_not_the_layer(self) -> None:
        """Only the serving index's override answers for the cutoff."""
        provider = build(
            [wheel("1.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            index_overrides={"other": IndexOverride(uploaded_prior_to=CUTOFF)},
        )
        source = provider.override_source(
            "pkg", Version("1.0"), "pypi", field="uploaded-prior-to"
        )
        assert source.layer == OverrideLayer.GLOBAL

    def test_a_synthetic_source_has_no_index_layer(self) -> None:
        """A package with no serving index falls through to the project level."""
        provider = build([wheel("1.0")], uploaded_prior_to=CUTOFF)
        source = provider.override_source(
            "pkg", Version("1.0"), None, field="uploaded-prior-to"
        )
        assert source.layer == OverrideLayer.GLOBAL
        assert source.label == ""
        assert source.selector == "pkg"

    def test_requires_python_is_offered_no_remedy(self) -> None:
        """Overriding requires-python would tell the resolver a falsehood."""
        reason = reason_for([wheel("1.0", requires_python=">=3.99")], target=_LINUX312)
        assert "note:" not in reason


class TestTheTryLine:
    """The instruction a default run prints under the line, per layer.

    Read at this depth because it is not the ``note:`` it was cut from: a
    note points at the entry the config file has, and an instruction has to
    name a setting the reader can change.  Each is quoted here as it was
    executed against a real project.
    """

    def test_the_project_level_cutoff_is_a_key_to_set(self) -> None:
        """No entry sets the key yet, so the line can name the whole path."""
        assert remedy_for(
            [wheel("1.0", upload_time=AFTER)], uploaded_prior_to=CUTOFF
        ) == ('set packages."pkg".uploaded-prior-to = false')

    @pytest.mark.parametrize(
        ("source_label", "named"),
        [
            ("packages.'pkg > 0.5'", "packages.'pkg > 0.5'"),
            ("package-rules[0]", "package-rules[0]"),
            ("", "pkg>0.5"),
        ],
        ids=["sugar-table", "package-rules", "host-built"],
    )
    def test_a_per_package_cutoff_names_the_entry_it_was_written_on(
        self, source_label: str, named: str
    ) -> None:
        """The line names the entry, which is not a selector and not a package.

        ``packages.'pkg > 0.5'`` and ``package-rules[0]`` are the same
        override on two surfaces, and only one of them is spelled
        ``packages."<selector>"``, so composing either into a second key
        path gives configuration nab rejects.  A ``package-rules`` entry can
        also match several packages, and naming the one being reported would
        send the reader to change the cutoff for the rest of them too.  A
        host that built the override itself named no entry, so the line
        falls back to the requirement it was given.
        """
        assert (
            remedy_for(
                [wheel("1.0", upload_time=AFTER)],
                package_overrides=[
                    pkg_override(
                        "pkg>0.5",
                        uploaded_prior_to=CUTOFF,
                        source_label=source_label,
                    )
                ],
            )
            == f"set uploaded-prior-to = false on {named}"
        )

    @pytest.mark.parametrize(
        ("index_name", "key"),
        [
            ("pypi", '"pypi"'),
            ("corp mirror", '"corp mirror"'),
            ("my.index", '"my.index"'),
            ('corp "the" mirror', "'corp \"the\" mirror'"),
            ('it\'s "ours"', '"it\'s \\"ours\\""'),
        ],
        ids=["bare", "spaced", "dotted", "quoted", "both-quotes"],
    )
    def test_a_per_index_cutoff_quotes_whatever_the_index_is_called(
        self, index_name: str, key: str
    ) -> None:
        """A cutoff can only reach an index through the table that names it.

        nab takes an index name from a TOML string, so the name can hold
        either quote, and the key the line writes it into has to be the
        form that parses back.
        """
        provider = build(
            [wheel("1.0", upload_time=AFTER)],
            index_overrides={index_name: IndexOverride(uploaded_prior_to=CUTOFF)},
        )
        provider.coordinator.index.store_listing_index("pkg", index_name)
        assert provider.choose_version("pkg", SpecifierSet("").to_range()) is None

        diagnostic = provider.get_no_versions_reason("pkg")
        assert diagnostic is not None
        assert diagnostic.remedy == f"set index.{key}.uploaded-prior-to = false"

    def test_a_package_that_already_scopes_the_cutoff_is_told_to_widen_it(
        self,
    ) -> None:
        """A second entry would overlap the first, so there is nothing to set.

        The project cutoff is what refused 2.0; the ``pkg<2`` entry does not
        reach it, and a bare-name entry beside that one is two per-package
        entries setting one field over overlapping versions.
        """
        assert remedy_for(
            [wheel("2.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[pkg_override("pkg<2", uploaded_prior_to=EARLY_CUTOFF)],
        ) == ("widen pkg<2 over this version, or drop the project cutoff")

    def test_a_package_whose_table_exists_is_told_to_add_the_key(self) -> None:
        """The line has to name the table, since a second one is a TOML error.

        Executed: with ``[tool.nab.packages.pkg]`` in the file, both
        ``[tool.nab.packages."pkg"]`` and the dotted key under
        ``[tool.nab]`` are refused as declaring that table twice, and
        adding the key inside the table that is there resolves.
        """
        assert remedy_for(
            [wheel("1.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[
                pkg_override(
                    "pkg",
                    dist_policy=DistPolicy.WHEEL_OR_SDIST,
                    name_keyed=True,
                    source_label="packages.'pkg'",
                )
            ],
        ) == ("add uploaded-prior-to = false to packages.'pkg'")

    def test_a_rule_setting_another_field_leaves_the_key_path_alone(self) -> None:
        """A ``[[package-rules]]`` entry is an array element, not that table.

        Executed: ``[tool.nab.packages."pkg"]`` beside a rule matching the
        same package parses and resolves.
        """
        assert remedy_for(
            [wheel("1.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[
                pkg_override(
                    "pkg",
                    dist_policy=DistPolicy.WHEEL_OR_SDIST,
                    source_label="package-rules[0]",
                )
            ],
        ) == ('set packages."pkg".uploaded-prior-to = false')

    def test_a_table_keyed_by_a_selector_leaves_the_key_path_alone(self) -> None:
        """``packages."pkg>=1"`` is a different key from ``packages."pkg"``.

        Executed: the two tables sit side by side, and since they set
        different fields the config layer takes both.
        """
        assert remedy_for(
            [wheel("1.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            package_overrides=[
                pkg_override(
                    "pkg>=1",
                    dist_policy=DistPolicy.WHEEL_OR_SDIST,
                    name_keyed=True,
                    source_label="packages.'pkg>=1'",
                )
            ],
        ) == ('set packages."pkg".uploaded-prior-to = false')

    def test_a_package_whose_table_exists_is_told_to_add_the_policy(self) -> None:
        """The policy takes the same turn, since the collision is the table."""
        assert remedy_for(
            [wheel("1.0")],
            target=_LINUX312,
            dist_policy=DistPolicy.SDIST_ONLY,
            package_overrides=[
                pkg_override(
                    "pkg",
                    uploaded_prior_to=None,
                    uploaded_prior_to_disabled=True,
                    name_keyed=True,
                    source_label="packages.'pkg'",
                )
            ],
        ) == ("add dist-policy = \"wheel-or-sdist\" to packages.'pkg'")

    @pytest.mark.parametrize(
        "policy",
        [DistPolicy.SDIST_ONLY, DistPolicy.SDIST_INSTALL],
        ids=["sdist-only", "sdist-install"],
    )
    def test_both_dist_policy_causes_offer_the_wider_policy(
        self, policy: DistPolicy
    ) -> None:
        """Both causes are the one key, so both are lifted by the one setting."""
        assert remedy_for([wheel("1.0")], dist_policy=policy, target=_LINUX312) == (
            'set packages."pkg".dist-policy = "wheel-or-sdist"'
        )

    @pytest.mark.parametrize(
        ("source_label", "named"),
        [
            ("packages.'pkg > 0.5'", "packages.'pkg > 0.5'"),
            ("package-rules[0]", "package-rules[0]"),
            ("", "pkg>0.5"),
        ],
        ids=["sugar-table", "package-rules", "host-built"],
    )
    def test_a_per_package_dist_policy_names_the_entry_it_was_written_on(
        self, source_label: str, named: str
    ) -> None:
        """The policy layers the way the cutoff does, and so does its remedy.

        Routing a set of packages to ``sdist-only`` through one
        ``package-rules`` entry is an ordinary configuration, and composing
        it into ``packages."<name>"`` gives two per-package overrides
        setting one field over overlapping versions, which nab refuses.
        """
        assert (
            remedy_for(
                [wheel("1.0")],
                target=_LINUX312,
                package_overrides=[
                    pkg_override(
                        "pkg>0.5",
                        dist_policy=DistPolicy.SDIST_ONLY,
                        source_label=source_label,
                    )
                ],
            )
            == f'set dist-policy = "wheel-or-sdist" on {named}'
        )

    def test_a_per_index_dist_policy_is_set_on_the_index(self) -> None:
        """A policy that reached the filter through an index is lifted there."""
        provider = build(
            [wheel("1.0")],
            target=_LINUX312,
            index_overrides={
                "corp mirror": IndexOverride(dist_policy=DistPolicy.SDIST_ONLY)
            },
        )
        provider.coordinator.index.store_listing_index("pkg", "corp mirror")
        assert provider.choose_version("pkg", SpecifierSet("").to_range()) is None

        diagnostic = provider.get_no_versions_reason("pkg")
        assert diagnostic is not None
        assert diagnostic.remedy == (
            'set index."corp mirror".dist-policy = "wheel-or-sdist"'
        )

    def test_a_package_that_already_scopes_the_policy_is_told_to_widen_it(
        self,
    ) -> None:
        """A second entry would overlap the first, so there is nothing to set."""
        assert remedy_for(
            [wheel("2.0")],
            target=_LINUX312,
            dist_policy=DistPolicy.SDIST_ONLY,
            package_overrides=[
                pkg_override("pkg<2", dist_policy=DistPolicy.WHEEL_OR_SDIST)
            ],
        ) == ("widen pkg<2 over this version, or drop the project dist-policy")

    def test_the_first_rung_in_report_order_answers(self) -> None:
        """Two rungs fired and the earlier one holds the line.

        ``dist-policy`` took the wheel and the cutoff took the sdist, so
        both have a remedy.  Lifting the one the ``-v`` clauses lead with
        is what the reader is being pointed at.
        """
        assert remedy_for(
            [wheel("1.0"), sdist("2.0", upload_time=AFTER)],
            dist_policy=DistPolicy.SDIST_ONLY,
            uploaded_prior_to=CUTOFF,
            target=_LINUX312,
        ) == ('set packages."pkg".uploaded-prior-to = false')

    def test_requires_python_is_offered_no_try_line(self) -> None:
        """Overriding requires-python would tell the resolver a falsehood."""
        assert (
            remedy_for([wheel("1.0", requires_python=">=3.99")], target=_LINUX312)
            is None
        )

    def test_requires_python_beside_a_cutoff_offers_the_cutoff(self) -> None:
        """The ban is on the key, not on the line: another rung may still answer."""
        assert remedy_for(
            [
                wheel("1.0", requires_python=">=3.99"),
                wheel("2.0", upload_time=AFTER),
            ],
            uploaded_prior_to=CUTOFF,
            target=_LINUX312,
        ) == ('set packages."pkg".uploaded-prior-to = false')

    def test_wheel_tags_is_offered_no_try_line(self) -> None:
        """No configuration turns the tag pass on, so nothing can be set."""
        assert (
            remedy_for([wheel("1.0", tag="cp312-cp312-win_amd64")], target=_LINUX312)
            is None
        )


class TestTheLadderSdistLine:
    """The entry for the sdist the metadata ladder went looking for."""

    def test_a_wheel_the_same_release_lost_is_not_the_refused_sdist(self) -> None:
        """Only an sdist can be the file the ladder wanted.

        ``requires-python`` refused 1.0's wheel and the cutoff refused its
        sdist, so the walk's first refusal for that release is a file the
        ladder never asked about.
        """
        provider = build(
            [wheel("1.0", requires_python=">=3.99"), sdist("1.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            target=_LINUX312,
        )

        diagnostic = provider.filtered_sdist_diagnostic("pkg", Version("1.0"))

        assert diagnostic is not None
        assert diagnostic.short == (
            "uploaded-prior-to excluded the sdist nab needed for metadata"
        )
        detail = "\n".join(diagnostic.detail)
        assert "pkg-1.0.tar.gz" in detail
        assert "pkg-1.0-py3-none-any.whl" not in detail


class TestTheInRangeLead:
    """Which filters dropped the release the requirement asked for."""

    def test_two_filters_read_as_a_pair(self) -> None:
        reason = reason_for(
            [
                wheel("1.0"),
                wheel("2.0", requires_python=">=3.99"),
                wheel("3.0", tag="cp312-cp312-win_amd64"),
            ],
            spec=">=2",
            target=_LINUX312,
        )
        assert reason == (
            "requires-python and wheel tags excluded every version in range"
            "\nrequires-python excluded 1 file (2.0 requires >=3.99, the resolve"
            " targets Python 3.12)"
            "\nno wheel's tags are compatible with the resolve target"
            " (1 wheel rejected)"
        )

    def test_three_filters_are_named_on_the_line(self) -> None:
        """Three still fit: naming them beats a count that points nowhere."""
        reason = reason_for(
            [
                wheel("1.0"),
                wheel("2.0", upload_time=AFTER),
                wheel("3.0", requires_python=">=3.99"),
                wheel("4.0", tag="cp312-cp312-win_amd64"),
            ],
            spec=">=2",
            target=_LINUX312,
            uploaded_prior_to=CUTOFF,
        )
        assert reason == (
            "uploaded-prior-to, requires-python and wheel tags excluded every"
            " version in range"
            "\nthe uploaded-prior-to cutoff 2026-05-01T00:00:00+00:00 excluded 1"
            " file uploaded at 2030-01-01T00:00:00Z (2.0)"
            "\nrequires-python excluded 1 file (3.0 requires >=3.99, the resolve"
            " targets Python 3.12)"
            "\nno wheel's tags are compatible with the resolve target"
            " (1 wheel rejected)"
            "\nnote: the project-level uploaded-prior-to set that cutoff; setting"
            ' packages."pkg".uploaded-prior-to = false lifts it for this package'
        )

    def test_an_out_of_range_drop_does_not_name_its_filter(self) -> None:
        """A filter that touched only a release outside the ask stays unnamed."""
        reason = reason_for(
            [
                wheel("0.5", requires_python=">=3.99"),
                wheel("1.0"),
                wheel("2.0", upload_time=AFTER),
            ],
            spec=">=2",
            target=_LINUX312,
            uploaded_prior_to=CUTOFF,
        )
        assert reason.startswith("uploaded-prior-to excluded every version in range")

    def test_one_filter_is_named_once_however_many_files_it_refused(self) -> None:
        """Two in-range releases refused by one filter name it once."""
        reason = reason_for(
            [
                wheel("1.0"),
                wheel("2.0", requires_python=">=3.99"),
                wheel("3.0", requires_python=">=3.99"),
            ],
            spec=">=2",
            target=_LINUX312,
        )
        assert reason == (
            "no version in range supports Python 3.12"
            "\nrequires-python excluded 2 files (newest: 3.0 requires >=3.99,"
            " the resolve targets Python 3.12)"
        )

    def test_a_cutoff_outside_the_ask_offers_no_remedy(self) -> None:
        """The note follows the drops the lead describes, not every drop.

        The cutoff here refused 0.5, which the requirement did not ask for, so
        lifting it would not give the user the release they wanted.
        """
        reason = reason_for(
            [
                wheel("0.5", upload_time=AFTER),
                wheel("1.0"),
                wheel("2.0", requires_python=">=3.99"),
            ],
            spec=">=2",
            target=_LINUX312,
            uploaded_prior_to=CUTOFF,
        )
        assert reason == (
            "no version in range supports Python 3.12"
            "\nrequires-python excluded 1 file (2.0 requires >=3.99, the resolve"
            " targets Python 3.12)"
        )

    def test_a_refused_spelling_of_a_kept_release_names_no_filter(self) -> None:
        """The wheel's ``1.0.0`` and the sdist's ``1.0`` are one release.

        ``===`` compares the string, so the refused wheel falls in range
        while the representative the listing kept does not.  The release
        survived, and no filter took it away.
        """
        assert reason_for(
            [wheel("1.0.0", tag="cp312-cp312-win_amd64"), sdist("1.0")],
            spec="===1.0.0",
            target=_LINUX312,
        ) == ("no version matches the requirement")

    def test_the_walk_reads_its_own_kept_set_for_that_rule(self) -> None:
        """The same rule where the surviving list can no longer answer it.

        The host drops the sdist that carries the release, so the version
        the requirement spells is missing from ``versions_cache`` and the
        walk is asked for a sentence.  The wheel it refused on tags is that
        same release under another spelling, and the walk kept it, so the
        line must not blame the tags for a drop the host made.
        """

        class DropsTheSdist(Provider):
            def filter_distributions(
                self, normalized: str, files: Sequence[WheelFile | SdistFile]
            ) -> list[tuple[Version, DistFile]]:
                kept = super().filter_distributions(normalized, files)
                return [pair for pair in kept if not isinstance(pair[1], SdistFile)]

        coordinator = make_coordinator(
            [wheel("1.0.0", tag="cp312-cp312-win_amd64"), sdist("1.0"), wheel("3.0")],
            package="pkg",
        )
        provider = DropsTheSdist(coordinator, target=_LINUX312)
        assert (
            provider.choose_version("pkg", SpecifierSet("===1.0.0").to_range()) is None
        )

        assert short_reason(provider, "pkg") == (
            "every version in range was refused; the filter cannot be named"
        )

    def test_a_dropped_pre_release_is_the_release_the_range_asked_for(self) -> None:
        """A range holding only a dropped pre-release still reaches the walk.

        The screen filters the dropped versions through the range rather
        than testing each one, so the pre-release is admitted where no
        final release in range could stand in for it.
        """
        assert reason_for(
            [wheel("0.5"), wheel("1.0b1", upload_time=AFTER)],
            spec=">=0.9",
            **WITH_CUTOFF,
        ).startswith("uploaded-prior-to excluded every version in range")

    def test_a_recorded_range_of_none_stays_a_no_match(self) -> None:
        """A scan that rejected every candidate without a range says no match.

        ``_run_full_scan`` records with no range, so the marker carries none
        and the walk has nothing to compare against.
        """
        provider = build([wheel("1.0")])
        provider._no_versions_reasons["pkg"] = NoVersionsReason(ReasonKind.NO_MATCH)
        assert short_reason(provider, "pkg") == ("no version matches the requirement")


class TestSharedPredicates:
    """The rungs the walk and the filter read from one body."""

    def test_the_walk_reads_the_filter_own_rung(self) -> None:
        """One body answers both, so the two cannot disagree about a cause."""
        provider = build([wheel("1.0", upload_time=AFTER)], uploaded_prior_to=CUTOFF)
        policy = listing_mod.listing_policy(provider, "pkg")
        dist = wheel("1.0", upload_time=AFTER)

        assert (
            listing_mod.python_or_time_cause(
                provider, "pkg", Version("1.0"), dist, policy
            )
            is DropCause.UPLOAD_TIME_AFTER_CUTOFF
        )
        assert provider.stats.excluded_by_time == 1

    def test_the_walk_answers_where_the_filter_refuses_the_run(self) -> None:
        """A naive stamp is a cause to the walk and an error to the filter."""
        naive = wheel("1.0", upload_time="2020-01-01T00:00:00")
        provider = build([naive], uploaded_prior_to=CUTOFF)
        policy = listing_mod.listing_policy(provider, "pkg")

        with pytest.raises(InvalidUploadTimeError):
            listing_mod.python_or_time_cause(
                provider, "pkg", Version("1.0"), naive, policy
            )

        assert (
            diagnosis_mod.python_or_time_verdict(
                provider, "pkg", Version("1.0"), naive, policy
            )
            is DropCause.UPLOAD_TIME_NAIVE
        )


def blocker(package: str, kind: diagnosis_mod.BlockerKind) -> diagnosis_mod.Blocker:
    """A look-ahead record wanting ``package`` in ``==2.0``, blocked at 1.0.

    Only a decided blocker stands at a version; a held or root one stands in
    a range, which is what the capture records for them.
    """
    held: Version | VersionRange = (
        Version("1.0")
        if kind is diagnosis_mod.BlockerKind.DECIDED
        else SpecifierSet("==1.0").to_range()
    )
    return diagnosis_mod.Blocker(
        kind, package, (SpecifierSet("==2.0").to_range(),), held
    )


class TestBlockerLines:
    """What a look-ahead rejection says at each depth."""

    def _diagnostic(
        self,
        blockers: Sequence[diagnosis_mod.Blocker],
        metadata: Sequence[diagnosis_mod.MetadataBlock] = (),
        provider: Provider | None = None,
    ) -> Diagnostic:
        """Render a look-ahead rejection for ``pkg``.

        The provider spells every blocker's ranges, so the default hands over
        a listing that resolves; a marked metadata block passes its own.
        """
        return diagnosis_mod.blockers_diagnostic(
            build([wheel("1.0")]) if provider is None else provider,
            "pkg",
            blockers,
            metadata,
        )

    def test_one_decided_blocker(self) -> None:
        """One rejection states its ranges on the line, so -v adds nothing."""
        decided = blocker("bar", diagnosis_mod.BlockerKind.DECIDED)
        diagnostic = self._diagnostic([decided])
        assert diagnostic.short == (
            "every version in range needs bar in ==2.0, but the resolve chose bar 1.0"
        )
        assert diagnostic.detail == ()

    def test_one_held_blocker(self) -> None:
        held = blocker("bar", diagnosis_mod.BlockerKind.HELD)
        assert self._diagnostic([held]).short == (
            "every version in range needs bar in ==2.0, but the resolve holds bar in ==1.0"
        )

    def test_one_root_blocker(self) -> None:
        root = blocker("bar", diagnosis_mod.BlockerKind.ROOT)
        assert self._diagnostic([root]).short == (
            "every version in range needs bar in ==2.0, but your project requires bar ==1.0"
        )

    def test_two_blockers_name_their_packages_and_stop(self) -> None:
        """Two ranges do not fit on one line, so the line names the packages."""
        diagnostic = self._diagnostic(
            [
                blocker("bar", diagnosis_mod.BlockerKind.DECIDED),
                blocker("baz", diagnosis_mod.BlockerKind.ROOT),
            ]
        )
        assert diagnostic.short == ("every version in range is blocked by bar and baz")
        assert len(diagnostic.detail) == 2

    def test_three_blockers_are_named_on_the_line(self) -> None:
        """Three names still read at a glance, and each is one to go look at."""
        diagnostic = self._diagnostic(
            [
                blocker("bar", diagnosis_mod.BlockerKind.DECIDED),
                blocker("baz", diagnosis_mod.BlockerKind.ROOT),
                blocker("qux", diagnosis_mod.BlockerKind.HELD),
            ]
        )
        assert diagnostic.short == (
            "every version in range is blocked by bar, baz and qux"
        )
        assert len(diagnostic.detail) == 3

    def test_four_blockers_are_counted(self) -> None:
        """Past three the line grows with the resolve, so it says how many."""
        diagnostic = self._diagnostic(
            [
                blocker("bar", diagnosis_mod.BlockerKind.DECIDED),
                blocker("baz", diagnosis_mod.BlockerKind.ROOT),
                blocker("qux", diagnosis_mod.BlockerKind.HELD),
                blocker("quux", diagnosis_mod.BlockerKind.DECIDED),
            ]
        )
        assert diagnostic.short == ("every version in range is blocked by 4 packages")
        assert len(diagnostic.detail) == 4

    def test_two_blockers_on_one_package_name_it_once(self) -> None:
        """A decided blocker and a root disagreement over the same dependency."""
        diagnostic = self._diagnostic(
            [
                blocker("bar", diagnosis_mod.BlockerKind.DECIDED),
                blocker("bar", diagnosis_mod.BlockerKind.ROOT),
            ]
        )
        assert diagnostic.short == ("every version in range is blocked by bar")

    def test_a_blocker_beside_unreadable_metadata_says_both(self) -> None:
        diagnostic = self._diagnostic(
            [blocker("bar", diagnosis_mod.BlockerKind.DECIDED)],
            (diagnosis_mod.MetadataBlock("No metadata for pkg==2.0"),),
        )
        assert diagnostic.short == (
            "every version in range is blocked by bar or rejected on its metadata"
        )
        assert diagnostic.detail == (
            "needs bar in ==2.0, but the resolve chose bar 1.0",
            "No metadata for pkg==2.0",
        )

    def test_metadata_alone_reads_as_the_metadata_line(self) -> None:
        """One unreadable version reads like several: a line, then the error."""
        block = diagnosis_mod.MetadataBlock("No metadata for pkg==2.0")
        diagnostic = self._diagnostic([], (block,))
        assert diagnostic.short == "every version in range was rejected on its metadata"
        assert diagnostic.detail == ("No metadata for pkg==2.0",)

    def test_a_marked_block_asks_the_walk_for_the_filter(self) -> None:
        """The ladder leaves a marker, and the filter is named here.

        Naming it costs a walk of the whole listing, so the ladder stores
        the version it wanted and the walk runs on this side, where the
        resolve has already failed.
        """
        provider = build([wheel("2.0"), sdist("2.0", upload_time=AFTER)], **WITH_CUTOFF)
        block = diagnosis_mod.MetadataBlock("No metadata for pkg==2.0", Version("2.0"))

        diagnostic = self._diagnostic([], (block,), provider)

        assert diagnostic.short == (
            "uploaded-prior-to excluded the sdist nab needed for metadata"
        )

    def test_a_marked_block_the_walk_cannot_name_keeps_its_sentence(self) -> None:
        """A marker the walk answers nothing for leaves the ladder's own line."""
        provider = build([wheel("2.0")], **WITH_CUTOFF)
        block = diagnosis_mod.MetadataBlock("No metadata for pkg==2.0", Version("2.0"))

        diagnostic = self._diagnostic([], (block,), provider)

        assert diagnostic.short == "every version in range was rejected on its metadata"
        assert diagnostic.detail == ("No metadata for pkg==2.0",)


_WINDOWS312 = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("windows_amd64")
)


class TestTheWalkIsSharedAcrossTargets:
    """A matrix attributes one listing once per Python, not once per tuple."""

    def _pair(
        self, files: Sequence[WheelFile | SdistFile], **kwargs: object
    ) -> tuple[Provider, Provider]:
        """Two providers over one listing and one shared filter memo."""
        coordinator = make_coordinator(list(files), package="pkg", auto_metadata=True)
        cache = ListingFilterCache()
        return (
            Provider(  # type: ignore[arg-type]
                coordinator, listing_filter_cache=cache, target=_LINUX312, **kwargs
            ),
            Provider(  # type: ignore[arg-type]
                coordinator, listing_filter_cache=cache, target=_WINDOWS312, **kwargs
            ),
        )

    def test_the_base_pass_runs_once_for_two_platforms(self) -> None:
        """The second target reads the refusals the first one's walk recorded.

        The rungs before the tag pass read the listing, the policy config and
        the target Python, so a matrix that fans out over platforms would
        otherwise re-walk the whole listing per failing tuple.
        """
        linux, windows = self._pair(
            [wheel("1.0", upload_time=AFTER)], uploaded_prior_to=CUTOFF
        )

        first = linux.diagnose_listing("pkg")
        second = windows.diagnose_listing("pkg")
        assert first is not None
        assert second is not None
        assert first.dropped[0] is second.dropped[0]

    def test_the_tag_pass_still_answers_per_target(self) -> None:
        """Sharing stops at the rung whose answer differs by platform."""
        linux, windows = self._pair([wheel("1.0", tag="cp312-cp312-win_amd64")])

        assert linux.diagnose_listing("pkg") is not None
        linux_diagnosis = linux.diagnose_listing("pkg")
        windows_diagnosis = windows.diagnose_listing("pkg")
        assert linux_diagnosis is not None
        assert windows_diagnosis is not None

        assert [record.cause for record in linux_diagnosis.dropped] == [
            DropCause.WHEEL_TAGS
        ]
        assert windows_diagnosis.dropped == ()
        assert windows_diagnosis.kept == {Version("1.0")}

    def test_a_second_python_walks_the_listing_again(self) -> None:
        """The Requires-Python rung answers per Python, so the memo keys on it."""
        coordinator = make_coordinator(
            [wheel("1.0", requires_python=">=3.12")], package="pkg", auto_metadata=True
        )
        cache = ListingFilterCache()
        old = Provider(
            coordinator,
            listing_filter_cache=cache,
            target=ResolveTarget.for_declared(
                python_version="3.11", spec=PlatformSpec("linux_x86_64")
            ),
        )
        new = Provider(coordinator, listing_filter_cache=cache, target=_LINUX312)

        old_diagnosis = old.diagnose_listing("pkg")
        new_diagnosis = new.diagnose_listing("pkg")
        assert old_diagnosis is not None
        assert new_diagnosis is not None

        assert [record.cause for record in old_diagnosis.dropped] == [
            DropCause.REQUIRES_PYTHON
        ]
        assert new_diagnosis.dropped == ()

    def test_a_provider_with_no_memo_walks_on_its_own(self) -> None:
        """A single-target resolve carries no cache and still gets its answer."""
        provider = build([wheel("1.0", upload_time=AFTER)], **WITH_CUTOFF)
        assert provider.listing_filter_cache is None

        diagnosis = provider.diagnose_listing("pkg")
        assert diagnosis is not None
        assert [record.cause for record in diagnosis.dropped] == [
            DropCause.UPLOAD_TIME_AFTER_CUTOFF
        ]


def unnamed_drop_entry() -> Diagnostic:
    """The entry for a drop no rung of the walk models."""
    provider = build([wheel("1.0")])
    provider.fetch_versions("pkg")
    provider.versions_cache["pkg"] = []
    diagnosis = provider.diagnose_listing("pkg")
    assert diagnosis is not None
    return diagnosis_mod.empty_listing_diagnostic(provider, "pkg", diagnosis)


def ladder_entry() -> Diagnostic:
    """The entry for the sdist the metadata ladder went looking for."""
    provider = build([wheel("2.0"), sdist("2.0", upload_time=AFTER)], **WITH_CUTOFF)
    entry = provider.filtered_sdist_diagnostic("pkg", Version("2.0"))
    assert entry is not None
    return entry


def extra_entry(kind: str, **kwargs: object) -> Diagnostic:
    """The entry an extras proxy gets for one of its three markers."""
    recorded = NoVersionsReason(kind, **kwargs)  # type: ignore[arg-type]
    return diagnosis_mod.extra_diagnostic("foo", "bar", recorded, "<1.5")


def every_shape() -> dict[str, Diagnostic]:
    """One entry per shape the ``Diagnostics:`` section can print.

    The two rules below hold over the whole report rather than over the
    cases a test happened to write down, so they are checked against this
    table.  It is also what the line-length census is counted from.
    """
    blocked = [blocker("bar", diagnosis_mod.BlockerKind.DECIDED)]
    unreadable = (diagnosis_mod.MetadataBlock("No metadata for pkg==2.0"),)
    return {
        **{kind: entry for kind, entry in diagnosis_mod.FIXED_DIAGNOSTICS.items()},
        "no-match": diagnosis_mod.NO_MATCH,
        "upload-time-missing": empty_entry(
            [wheel("1.0", upload_time=None)], **WITH_CUTOFF
        ),
        "upload-time-unparseable": empty_entry(
            [wheel("1.0", upload_time="not-a-time")], **WITH_CUTOFF
        ),
        "upload-time-naive": empty_entry(
            [wheel("1.0", upload_time="2020-01-01T00:00:00")], **WITH_CUTOFF
        ),
        "upload-time-after-cutoff": empty_entry(
            [wheel("1.0", upload_time=AFTER)], **WITH_CUTOFF
        ),
        "dist-policy": empty_entry(
            [wheel("1.0")], dist_policy=DistPolicy.SDIST_ONLY, target=_LINUX312
        ),
        "sdist-install": empty_entry(
            [wheel("1.0")], dist_policy=DistPolicy.SDIST_INSTALL
        ),
        "requires-python": empty_entry(
            [wheel("1.0", requires_python=">=3.99")], target=_LINUX312
        ),
        "wheel-tags": empty_entry(
            [wheel("1.0", tag="cp312-cp312-win_amd64")], target=_LINUX312
        ),
        "invalid-version": empty_entry([wheel("not-a-version")]),
        "two-keys": empty_entry(
            [wheel("1.0", requires_python=">=3.99"), wheel("2.0", upload_time=AFTER)],
            target=_LINUX312,
            **WITH_CUTOFF,
        ),
        "three-keys": empty_entry(
            [
                wheel("1.0", requires_python=">=3.99"),
                wheel("2.0", upload_time=AFTER),
                sdist("3.0"),
            ],
            target=_LINUX312,
            dist_policy=DistPolicy.WHEEL_ONLY,
            **WITH_CUTOFF,
        ),
        "unnamed": unnamed_drop_entry(),
        "in-range-one-key": diagnostic_for(
            [wheel("1.0"), wheel("2.0", upload_time=AFTER)],
            spec=">=2",
            **WITH_CUTOFF,
        ),
        "in-range-two-keys": diagnostic_for(
            [
                wheel("1.0"),
                wheel("2.0", requires_python=">=3.99"),
                wheel("3.0", tag="cp312-cp312-win_amd64"),
            ],
            spec=">=2",
            target=_LINUX312,
        ),
        "in-range-requires-python": diagnostic_for(
            [wheel("1.0"), wheel("2.0", requires_python=">=3.99")],
            spec=">=2",
            target=_LINUX312,
        ),
        "in-range-wheel-tags": diagnostic_for(
            [wheel("1.0"), wheel("2.0", tag="cp312-cp312-win_amd64")],
            spec=">=2",
            target=_LINUX312,
        ),
        "in-range-three-keys": diagnostic_for(
            [
                wheel("1.0"),
                wheel("2.0", upload_time=AFTER),
                wheel("3.0", requires_python=">=3.99"),
                wheel("4.0", tag="cp312-cp312-win_amd64"),
            ],
            spec=">=2",
            target=_LINUX312,
            **WITH_CUTOFF,
        ),
        "ladder-sdist": ladder_entry(),
        "blocker-decided": diagnosis_mod.blockers_diagnostic(
            build([wheel("1.0")]), "pkg", blocked, ()
        ),
        "blocker-held": diagnosis_mod.blockers_diagnostic(
            build([wheel("1.0")]),
            "pkg",
            [blocker("bar", diagnosis_mod.BlockerKind.HELD)],
            (),
        ),
        "blocker-root": diagnosis_mod.blockers_diagnostic(
            build([wheel("1.0")]),
            "pkg",
            [blocker("bar", diagnosis_mod.BlockerKind.ROOT)],
            (),
        ),
        "blockers-two": diagnosis_mod.blockers_diagnostic(
            build([wheel("1.0")]),
            "pkg",
            [*blocked, blocker("baz", diagnosis_mod.BlockerKind.ROOT)],
            (),
        ),
        "blockers-three": diagnosis_mod.blockers_diagnostic(
            build([wheel("1.0")]),
            "pkg",
            [
                *blocked,
                blocker("baz", diagnosis_mod.BlockerKind.ROOT),
                blocker("qux", diagnosis_mod.BlockerKind.HELD),
            ],
            (),
        ),
        "blockers-and-metadata": diagnosis_mod.blockers_diagnostic(
            build([wheel("1.0")]), "pkg", blocked, unreadable
        ),
        "metadata": diagnosis_mod.blockers_diagnostic(
            build([wheel("1.0")]), "pkg", [], unreadable
        ),
        "extra-undeclared": extra_entry(
            ReasonKind.EXTRA_UNDECLARED, version_range=SpecifierSet(">=1.0").to_range()
        ),
        "extra-metadata": extra_entry(ReasonKind.EXTRA_METADATA, metadata=unreadable),
        "extra-narrowed": extra_entry(ReasonKind.EXTRA_NARROWED),
    }


class TestTheTwoDepths:
    """What ``-v`` owes the line it deepens, over every entry the report has.

    The three rules are that it never drops what the default line carried,
    never says the same thing again, and never reaches for the resolver's
    own vocabulary.  Where a shape has no more to say, it says nothing.
    """

    @pytest.mark.parametrize("name", list(every_shape()))
    def test_a_try_line_always_has_its_note_behind_it(self, name: str) -> None:
        """Asking for more must never give less than the default line gave."""
        entry = every_shape()[name]
        if entry.remedy is None:
            return
        assert [line for line in entry.detail if line.startswith("note: ")]

    @pytest.mark.parametrize("name", list(every_shape()))
    def test_no_detail_line_repeats_the_line_it_deepens(self, name: str) -> None:
        """A restatement is not detail, and an empty block is the honest form."""
        entry = every_shape()[name]
        assert entry.short not in entry.detail

    @pytest.mark.parametrize("name", list(every_shape()))
    def test_no_line_uses_the_resolvers_own_words(self, name: str) -> None:
        """The report is read by someone who wrote a config file, not a solver."""
        entry = every_shape()[name]
        internal = re.compile(r"\b(root|rung|walk|walked|predicate|marker)\b")
        for line in (entry.short, *entry.detail):
            assert internal.search(line) is None, line
