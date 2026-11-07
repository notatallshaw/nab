"""Prepared catalogue reads retain exact identities and distinguish unknown metadata."""

from __future__ import annotations

import pytest

from nab_provider._compat import override
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import Version
from nab_provider.version_candidates import (
    PreparedVersionCatalogue,
    VersionCandidate,
    VersionCandidateData,
    VersionCatalogue,
)
from nab_provider.yank_candidates import Candidate


class Catalogue(PreparedVersionCatalogue[VersionCandidate, VersionCandidateData]):
    """Supply fixed listing and metadata reads while recording their acquisition."""

    range_type = VersionRange

    def __init__(self) -> None:
        super().__init__(unavailable_reason="metadata is not present")
        self.listing_reads = 0
        self.metadata_reads: list[VersionCandidate] = []
        self.unknown = VersionCandidate("unknown", Version("1"), withdrawn=False)
        self.rejected = VersionCandidate("rejected", Version("1"), withdrawn=False)
        self.data = VersionCandidateData({}, ())

    @staticmethod
    @override
    def pin_project(pin: Requirement) -> str:
        return canonicalize_name(pin.name)

    @staticmethod
    @override
    def base_name(package: str) -> str:
        return package

    @override
    def read_choices(self, package: str) -> tuple[VersionCandidate, ...]:
        self.listing_reads += 1
        if package == "empty":
            return ()
        return tuple(
            VersionCandidate(package, Version(version), withdrawn=False)
            for version in ("1", "1", "1.0")
        )

    @override
    def read_candidate(
        self, candidate: VersionCandidate
    ) -> VersionCandidateData | None:
        self.metadata_reads.append(candidate)
        if candidate == self.unknown:
            self.unavailable.add(candidate)
            return None
        return None if candidate == self.rejected else self.data


def test_listing_cache_retains_distinct_version_text() -> None:
    catalogue = Catalogue()
    candidates = catalogue.choices("sample")

    assert [candidate.label for candidate in candidates] == ["1", "1.0"]
    assert candidates[0].version == candidates[1].version
    assert candidates[0].base == "sample"
    assert catalogue.choices("sample") is candidates
    assert catalogue.listing_reads == 1


def test_metadata_cache_keeps_rejection_separate_from_unavailability() -> None:
    catalogue = Catalogue()
    accepted = catalogue.choices("sample")[0]

    assert catalogue.prepare(accepted) is catalogue.data
    assert catalogue.prepare(accepted) is catalogue.data
    assert catalogue.prepare(catalogue.rejected) is None
    assert catalogue.prepare(catalogue.rejected) is None
    assert catalogue.prepare(catalogue.unknown) is None
    assert catalogue.prepare(catalogue.unknown) is None

    assert catalogue.facts == {accepted: catalogue.data, catalogue.rejected: None}
    assert catalogue.unavailable == {catalogue.unknown}
    assert catalogue.metadata_reads == [accepted, catalogue.rejected, catalogue.unknown]


def test_empty_listing_is_cached() -> None:
    catalogue = Catalogue()

    assert catalogue.choices("empty") == ()
    assert catalogue.choices("empty") == ()
    assert catalogue.listing_reads == 1


def test_extra_base_keeps_its_native_withdrawal_identity() -> None:
    candidate = Candidate("sample[feature]", Version("1.0"), withdrawn=True)

    base = VersionCatalogue.base_candidate(candidate)

    assert type(base) is Candidate
    assert base == Candidate("sample", Version("1.0"), withdrawn=True)
    assert base.label == "1.0"


@pytest.mark.parametrize(
    ("prereleases", "expected"),
    [
        (None, ["2.0", "2", "1", "3b1", "!4", "!5b1"]),
        (True, ["3b1", "2.0", "2", "1", "!5b1", "!4"]),
        (False, ["2.0", "2", "1", "!4"]),
    ],
)
def test_release_batches_preserve_host_prerelease_policy_and_label_order(
    prereleases: bool | None, expected: list[str]
) -> None:
    class OrderedCatalogue(Catalogue):
        @override
        def candidate_prereleases(self, package: str) -> bool | None:
            assert package == "sample"
            return prereleases

    catalogue = OrderedCatalogue()
    choices = [
        VersionCandidate("sample", Version(version), withdrawn=withdrawn)
        for version, withdrawn in (
            ("3b1", False),
            ("1", False),
            ("4", True),
            ("2.0", False),
            ("2", False),
            ("5b1", True),
        )
    ]
    actual = catalogue.order_candidates("sample", VersionRange.full(), choices)

    assert [
        ("!" if candidate.withdrawn else "") + candidate.label for candidate in actual
    ] == expected
    assert catalogue.metadata_reads == []
    assert catalogue.order_candidates("sample", VersionRange.empty(), choices) == []


def test_automatic_fallback_keeps_an_admitted_prerelease_without_a_final() -> None:
    catalogue = Catalogue()
    choice = VersionCandidate("sample", Version("2b1"), withdrawn=False)

    assert catalogue.order_candidates("sample", VersionRange.full(), [choice]) == [
        choice
    ]
