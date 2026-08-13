"""Differential property tests for ``filter(assume_sorted=...)``.

What the sorted path yields is the candidate order ``choose_version`` walks, so
it has to equal the entry-by-entry filter element for element: every property
compares the two over random ordered listings and random ranges, in both
declared orders, under each pre-release policy, and through ``key``.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version

from .strategies import PROPERTY_SETTINGS, RANGE_VERSION_POOL, range_specs

pytestmark = pytest.mark.property

_VERSIONS = [Version(text) for text in RANGE_VERSION_POOL]


@st.composite
def _listings(draw: st.DrawFn) -> list[Version]:
    """An ascending listing over the pool, duplicates allowed."""
    picked = draw(st.lists(st.sampled_from(_VERSIONS), min_size=0, max_size=12))
    return sorted(picked)


def _first(entry: tuple[Version, str]) -> Version:
    return entry[0]


def _bisects(version_range: VersionRange) -> bool:
    """True when the range decides membership from its bounds alone."""
    return not version_range._admit and not version_range._reject


def _check_every_shape(
    version_range: VersionRange, versions: list[Version], prereleases: bool | None
) -> None:
    """Every sorted calling shape equals the entry-by-entry filter."""
    descending = list(reversed(versions))
    ascending_paired = [(v, f"pkg-{v}.whl") for v in versions]
    descending_paired = list(reversed(ascending_paired))

    assert list(
        version_range.filter(versions, prereleases, assume_sorted="ascending")
    ) == list(version_range.filter(versions, prereleases))
    assert list(
        version_range.filter(descending, prereleases, assume_sorted="descending")
    ) == list(version_range.filter(descending, prereleases))
    assert list(
        version_range.filter(
            ascending_paired, prereleases, _first, assume_sorted="ascending"
        )
    ) == list(version_range.filter(ascending_paired, prereleases, _first))
    assert list(
        version_range.filter(
            descending_paired, prereleases, _first, assume_sorted="descending"
        )
    ) == list(version_range.filter(descending_paired, prereleases, _first))


@given(versions=_listings(), spec=range_specs())
@PROPERTY_SETTINGS
def test_sorted_filter_matches_the_entry_walk(
    versions: list[Version], spec: str
) -> None:
    """The sorted path equals the entry-by-entry filter."""
    _check_every_shape(SpecifierSet(spec).to_range(), versions, None)


@given(
    versions=_listings(),
    spec=range_specs(),
    prereleases=st.sampled_from([True, False]),
)
@PROPERTY_SETTINGS
def test_sorted_filter_matches_under_a_configured_policy(
    versions: list[Version], spec: str, prereleases: bool
) -> None:
    """The result stays exact once a pre-release policy is configured."""
    configured = SpecifierSet(spec, prereleases=prereleases).to_range()
    _check_every_shape(configured, versions, None)


@given(
    versions=_listings(),
    spec=range_specs(),
    prereleases=st.sampled_from([True, False, None]),
)
@PROPERTY_SETTINGS
def test_sorted_filter_matches_under_an_argument_policy(
    versions: list[Version], spec: str, prereleases: bool | None
) -> None:
    """The ``prereleases`` argument governs both paths alike."""
    _check_every_shape(SpecifierSet(spec).to_range(), versions, prereleases)


@given(versions=_listings(), left=range_specs(), right=range_specs())
@PROPERTY_SETTINGS
def test_sorted_filter_matches_on_algebra_derived_ranges(
    versions: list[Version], left: str, right: str
) -> None:
    """Set algebra reaches shapes no single specifier writes.

    It is the only way to produce an opt-in region that covers part of the
    bounds rather than all of them.
    """
    a: VersionRange = SpecifierSet(left).to_range()
    b: VersionRange = SpecifierSet(right).to_range()
    for derived in (a & b, a | b, a - b, ~a):
        _check_every_shape(derived, versions, None)


@given(versions=_listings(), spec=range_specs())
@PROPERTY_SETTINGS
def test_a_contradicted_direction_raises(versions: list[Version], spec: str) -> None:
    """A bisected sequence whose ends contradict the declared order is refused."""
    version_range = SpecifierSet(spec).to_range()
    if not _bisects(version_range) or len(versions) < 2 or versions[0] == versions[-1]:
        assert list(version_range.filter(versions, assume_sorted="descending")) == list(
            version_range.filter(versions)
        )
        return
    with pytest.raises(ValueError, match="assume_sorted='descending'"):
        version_range.filter(versions, assume_sorted="descending")
    with pytest.raises(ValueError, match="assume_sorted='ascending'"):
        version_range.filter(list(reversed(versions)), assume_sorted="ascending")


@given(versions=_listings(), spec=range_specs())
@PROPERTY_SETTINGS
def test_an_unrecognised_direction_raises(versions: list[Version], spec: str) -> None:
    """Only the two named orders are accepted, whatever the range holds."""
    version_range = SpecifierSet(spec).to_range()
    with pytest.raises(ValueError, match="must be 'ascending' or 'descending'"):
        version_range.filter(versions, assume_sorted="Ascending")  # type: ignore[arg-type]


@given(versions=_listings(), spec=range_specs())
@PROPERTY_SETTINGS
def test_string_entries_filter_by_version(versions: list[Version], spec: str) -> None:
    """A sorted sequence of version strings filters as its parsed versions."""
    version_range = SpecifierSet(spec).to_range()
    texts = [str(v) for v in versions]
    assert list(version_range.filter(texts, assume_sorted="ascending")) == list(
        version_range.filter(texts)
    )


@given(versions=_listings(), spec=range_specs())
@PROPERTY_SETTINGS
def test_an_arbitrarily_admitted_member_breaks_the_precondition(
    versions: list[Version], spec: str
) -> None:
    """A string the bounds do not describe has no place in version order.

    The entry walk yields it, but nothing says where it sits, so the sorted
    path refuses the sequence rather than guessing.
    """
    version_range = SpecifierSet(spec).to_range()
    if not version_range._arbitrary_active():
        return
    texts: list[str] = [*[str(v) for v in versions], "frobnicate"]
    assert "frobnicate" in list(version_range.filter(texts))
    with pytest.raises(ValueError, match="does not parse as a version"):
        list(version_range.filter(texts, assume_sorted="ascending"))
