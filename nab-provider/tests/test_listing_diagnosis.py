"""Tests for the failure-time attribution of the listing filter's drops.

Two things are pinned here.  One is the differential oracle: over a matrix of
policy configurations, the walk keeps or refuses every file the filter saw,
exactly once, and keeps the versions the filter kept.  That is the whole
anti-drift argument for a mechanism that re-expresses the filter's order.  The
other is the clause text, one table entry per cause per grammatical number.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

from nab_provider._provider import listing as listing_mod
from nab_provider._provider import listing_diagnosis as diagnosis_mod
from nab_provider._provider.listing_diagnosis import (
    CutoffLayer,
    DropCause,
    NoVersionsReason,
    ReasonKind,
)
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
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

CUTOFF = datetime(2026, 5, 1, tzinfo=timezone.utc)
BEFORE = "2020-01-01T00:00:00Z"
AFTER = "2030-01-01T00:00:00Z"
CUTOFF_TEXT = "2026-05-01T00:00:00+00:00"
WITH_CUTOFF: dict[str, object] = {"uploaded_prior_to": CUTOFF}

_LINUX312 = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("linux_x86_64")
)


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


def reason_for(
    files: Sequence[WheelFile | SdistFile], *, spec: str = "", **kwargs: object
) -> str:
    """Ask ``pkg`` for ``spec`` and return the sentence that comes back."""
    provider = build(files, **kwargs)
    assert provider.choose_version("pkg", SpecifierSet(spec).to_range()) is None
    reason = provider.get_no_versions_reason("pkg")
    assert reason is not None
    return reason


def rendered_for(files: Sequence[WheelFile | SdistFile], **kwargs: object) -> str:
    """Render ``pkg``'s empty-listing sentence without asking for a version.

    The filter refuses the whole run on a timezone-naive upload time, so a
    listing carrying one cannot be reached through :func:`reason_for`.
    """
    provider = build(files, **kwargs)
    diagnosis = provider.diagnose_listing("pkg")
    assert diagnosis is not None
    return diagnosis_mod.empty_listing_reason(provider, "pkg", diagnosis)


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
        """No file is claimed twice, dropped silently, or refused after surviving."""
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

    @pytest.mark.parametrize("config", sorted(ORACLE_CONFIGS), ids=str)
    def test_the_walk_keeps_the_versions_the_filter_kept(self, config: str) -> None:
        """The kept sets agree, so no clause describes a release that survived."""
        provider = build(ORACLE_LISTING, **ORACLE_CONFIGS[config])

        kept = provider.fetch_versions("pkg")
        diagnosis = provider.diagnose_listing("pkg")
        assert diagnosis is not None

        assert diagnosis.kept == {version for version, _dist in kept}
        assert diagnosis.unexplained == 0

    def test_a_drop_the_walk_cannot_model_is_counted_not_hidden(self) -> None:
        """A filter that removes more than the rungs explain says so in the line.

        Driven through the documented ``filter_distributions`` override seam,
        which is the only way a host can drop a file no rung of the walk knows
        about.
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

        assert provider.get_no_versions_reason("pkg") == (
            "found on index but no distribution is compatible: 2 versions were"
            " dropped for a reason this report cannot name; no sdist is"
            " available to build from"
        )

    def test_one_unmodelled_drop_reads_as_one_version(self) -> None:
        """The singular of the same clause."""
        coordinator = make_coordinator([wheel("1.0")], package="pkg")
        provider = Provider(coordinator)
        provider.fetch_versions("pkg")
        provider.versions_cache["pkg"] = []

        diagnosis = provider.diagnose_listing("pkg")
        assert diagnosis is not None
        assert diagnosis_mod.empty_listing_reason(provider, "pkg", diagnosis) == (
            "found on index but no distribution is compatible: 1 version was"
            " dropped for a reason this report cannot name; no sdist is"
            " available to build from"
        )


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
        assert first.dropped

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

    def test_sdist_install_without_an_sdist(self) -> None:
        assert (
            'dist-policy = "sdist-install" excluded 1 version that publishes no'
            " sdist (1.0)"
        ) in reason_for([wheel("1.0")], dist_policy=DistPolicy.SDIST_INSTALL)

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
            "none of the wheel's tags are compatible with the resolve target"
            " (1 wheel rejected)"
        ) in reason_for([wheel("1.0", tag="cp312-cp312-win_amd64")], target=_LINUX312)

    def test_wheel_tags_plural(self) -> None:
        assert (
            "none of the wheel's tags are compatible with the resolve target"
            " (2 wheels rejected)"
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

        Report order is the enum's member values and is deliberately not the
        order the filter asks the questions in.  This is also the whole shape
        of the message: lead, clauses, sdist tail, then the note.
        """
        assert reason_for(
            [wheel("1.0", requires_python=">=3.99"), wheel("2.0", upload_time=AFTER)],
            target=_LINUX312,
            uploaded_prior_to=CUTOFF,
        ) == (
            "found on index but no distribution is compatible: the"
            " uploaded-prior-to cutoff 2026-05-01T00:00:00+00:00 excluded 1 file"
            " uploaded at 2030-01-01T00:00:00Z (2.0); requires-python excluded 1"
            " file (1.0 requires >=3.99, the resolve targets Python 3.12); no"
            " sdist is available to build from\n    note: the project-level"
            " uploaded-prior-to set that cutoff; uploaded-prior-to = false under"
            ' [tool.nab.packages."pkg"] lifts it for this package'
        )

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
            "found on index but no distribution is compatible: dist-policy ="
            ' "sdist-only" excluded 1 wheel (1.0); no sdist is available to'
            " build from"
        )

    def test_an_sdist_on_the_index_takes_no_no_sdist_tail(self) -> None:
        """The tail says the index published no sdist, not that none survived."""
        reason = reason_for([sdist("1.0", requires_python=">=3.99")], target=_LINUX312)
        assert "no sdist is available to build from" not in reason


class TestTheRemedyNamesTheLayer:
    """Which of the three config layers set the cutoff that fired."""

    def test_the_project_level_cutoff(self) -> None:
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)], uploaded_prior_to=CUTOFF
        ).endswith(
            "\n    note: the project-level uploaded-prior-to set that cutoff;"
            ' uploaded-prior-to = false under [tool.nab.packages."pkg"] lifts it'
            " for this package"
        )

    def test_a_per_package_cutoff(self) -> None:
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)],
            package_overrides=[
                pkg_override(
                    "pkg", uploaded_prior_to=CUTOFF, source_label="packages.'pkg'"
                )
            ],
        ).endswith(
            "\n    note: the per-package uploaded-prior-to for packages.'pkg' set"
            " that cutoff; setting it to false there lifts it"
        )

    def test_a_per_index_cutoff(self) -> None:
        assert reason_for(
            [wheel("1.0", upload_time=AFTER)],
            index_overrides={"pypi": IndexOverride(uploaded_prior_to=CUTOFF)},
        ).endswith(
            '\n    note: the per-index uploaded-prior-to for index "pypi" set'
            " that cutoff; uploaded-prior-to = false under [tool.nab.index.pypi]"
            " lifts it"
        )

    def test_an_index_override_on_another_index_is_not_the_layer(self) -> None:
        """Only the serving index's override answers for the cutoff."""
        provider = build(
            [wheel("1.0", upload_time=AFTER)],
            uploaded_prior_to=CUTOFF,
            index_overrides={"other": IndexOverride(uploaded_prior_to=CUTOFF)},
        )
        source = provider.uploaded_prior_to_source("pkg", Version("1.0"), "pypi")
        assert source.layer is CutoffLayer.GLOBAL

    def test_a_synthetic_source_has_no_index_layer(self) -> None:
        """A package with no serving index falls through to the project level."""
        provider = build([wheel("1.0")], uploaded_prior_to=CUTOFF)
        source = provider.uploaded_prior_to_source("pkg", Version("1.0"), None)
        assert source.layer is CutoffLayer.GLOBAL
        assert source.label == ""

    def test_requires_python_is_offered_no_remedy(self) -> None:
        """Overriding requires-python would tell the resolver a falsehood."""
        reason = reason_for([wheel("1.0", requires_python=">=3.99")], target=_LINUX312)
        assert "note:" not in reason


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
            "found on index but every version matching the requirement was"
            " filtered (by requires-python or wheel tags)"
        )

    def test_three_filters_read_as_a_list(self) -> None:
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
            "found on index but every version matching the requirement was"
            " filtered (by upload-time, requires-python, or wheel tags)"
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
        assert reason == (
            "found on index but every version matching the requirement was"
            " filtered (by upload-time)"
        )

    def test_a_recorded_range_of_none_stays_a_no_match(self) -> None:
        """A scan that rejected every candidate without a range says no match.

        ``_run_full_scan`` records with no range, so the marker carries none
        and the walk has nothing to compare against.
        """
        provider = build([wheel("1.0")])
        provider._no_versions_reasons["pkg"] = NoVersionsReason(ReasonKind.NO_MATCH)
        assert provider.get_no_versions_reason("pkg") == (
            "no version matches the requirement"
        )


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
