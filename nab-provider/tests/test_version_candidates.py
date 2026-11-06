"""Prepared catalogue reads retain exact identities and distinguish unknown metadata."""

from __future__ import annotations

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
