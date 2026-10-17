"""Lock finalization passes independently converted emitted sets to validation."""

from __future__ import annotations

import pytest
from packaging.markers import Marker as ExternalMarker

from nab_markersets.conversions import PreparedMarkerSets
from nab_markersets.markersets import DecisionStore, MarkerSet
from nab_project._lockfile import disjointness, pylock
from nab_provider._vendor.packaging.markers import Marker
from nab_provider._vendor.packaging.pylock import Package
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import Version
from nab_provider.conflict_kind import KIND_EXTRA


@pytest.fixture
def conversions(monkeypatch: pytest.MonkeyPatch) -> list[str | Marker | ExternalMarker]:
    """Observe the real factory without replacing its results or failures."""
    calls: list[str | Marker | ExternalMarker] = []
    original = MarkerSet.from_marker

    def observed(
        _cls: type[MarkerSet], marker: str | Marker | ExternalMarker
    ) -> MarkerSet:
        calls.append(marker)
        return original(marker)

    monkeypatch.setattr(MarkerSet, "from_marker", classmethod(observed))
    return calls


def test_finalizer_hands_off_the_emitted_and_fallback_sets(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    owner = PreparedMarkerSets()
    raw = Marker('sys_platform == "linux" and os_name == "posix"')
    within = MarkerSet.from_marker('os_name == "posix"')
    emitted = pylock._finalize_marker(raw, within, prepared_sets=owner)
    assert emitted is not None
    assert str(emitted) != str(raw)
    before = len(conversions)
    assert owner.convert(emitted).evaluate({"sys_platform": "linux", "os_name": "nt"})
    assert not owner.convert(emitted).evaluate(
        {"sys_platform": "win32", "os_name": "nt"}
    )
    assert len(conversions) == before

    raw = Marker('extras == "unused"')
    assert pylock._finalize_marker(raw, MarkerSet.full(), prepared_sets=owner) is raw
    before = len(conversions)
    assert owner.convert(raw).is_empty()
    assert len(conversions) == before


def test_final_assembly_hands_off_the_complete_gated_marker(
    conversions: list[str | Marker | ExternalMarker],
) -> None:
    owner = PreparedMarkerSets()
    parts = [
        pylock._GatedMarker(Marker('sys_platform == "linux"'), ((KIND_EXTRA, "a"),)),
        pylock._GatedMarker(Marker('sys_platform == "win32"'), ((KIND_EXTRA, "b"),)),
    ]
    marker = pylock._finalize_parts(
        parts, MarkerSet.full(), "demo", {}, DecisionStore(), prepared_sets=owner
    )
    assert marker is not None
    before = len(conversions)
    converted = owner.convert(marker)
    assert converted.evaluate({"sys_platform": "linux", "extras": frozenset({"a"})})
    assert not converted.evaluate({"sys_platform": "linux", "extras": frozenset({"b"})})
    assert converted.evaluate({"sys_platform": "win32", "extras": frozenset({"b"})})
    assert not converted.evaluate({"sys_platform": "win32", "extras": frozenset({"a"})})
    assert len(conversions) == before


def test_absent_marker_is_full_for_disjointness_with_a_handoff() -> None:
    packages = [
        Package(name=canonicalize_name("demo"), version=Version("1.0")),
        Package(
            name=canonicalize_name("demo"),
            version=Version("2.0"),
            marker=Marker('sys_platform == "linux"'),
        ),
    ]
    environments = {"linux": {"sys_platform": "linux"}}
    with pytest.raises(disjointness.DisjointnessError) as fresh:
        disjointness.validate_marker_disjointness(
            packages, environments=environments, extras=(), groups=()
        )
    with pytest.raises(disjointness.DisjointnessError) as prepared:
        disjointness.validate_marker_disjointness(
            packages,
            environments=environments,
            extras=(),
            groups=(),
            prepared_sets=PreparedMarkerSets(),
        )
    assert prepared.value.args == fresh.value.args
