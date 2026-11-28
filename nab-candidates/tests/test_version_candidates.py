"""Prepared catalogue reads retain exact identities and distinguish unknown metadata."""

from __future__ import annotations

import pytest
from packaging.ranges import VersionRange
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from nab_candidates._compat import override
from nab_candidates.resolution import PreparedResolution
from nab_candidates.versions import (
    PreparedVersionCatalogue,
    VersionCandidate,
    VersionCandidateData,
    is_pin,
)


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


def test_prepared_version_catalogue_resolves_a_permitted_yanked_file() -> None:
    class Wheels(Catalogue):
        @override
        def read_choices(self, package: str) -> tuple[VersionCandidate, ...]:
            return (
                VersionCandidate(package, Version("1"), withdrawn=package == "dep"),
            )

        @override
        def read_candidate(self, candidate: VersionCandidate) -> VersionCandidateData:
            if candidate.package == "app":
                requirement = Requirement("dep==1")
                return VersionCandidateData(
                    {"dep": requirement.specifier.to_range()}, (requirement,)
                )

            return VersionCandidateData({}, ())

    catalogue = Wheels()
    root = Requirement("app")
    approved = Requirement("dep==1")
    session = PreparedResolution.from_catalogue(
        catalogue,
        {root.name: catalogue.full_range()},
        tuple(req for req in (root, approved) if is_pin(req)),
        {},
        try_live=True,
    )

    solution = session.solve()

    dep = solution.candidates["dep"]
    assert catalogue.candidate_package(dep) == catalogue.candidate_base(dep) == "dep"
    assert catalogue.candidate_label(dep) == "1"
    assert catalogue.candidate_withdrawn(dep)
    assert catalogue.range_contains(
        dep,
        catalogue.intersect_ranges(
            catalogue.full_range(), approved.specifier.to_range()
        ),
    )
    assert catalogue.base_candidate(dep) == dep
    assert solution.edges == (("app", "dep"),)
    assert solution.admission_sources["dep"] == ("input pin dep==1",)
