"""Shared file facts stay available without enabling ordinary yanked selection."""

from __future__ import annotations

from functools import partial

import pytest

from nab_project._testing.yanking import ReleaseRow, graph_port
from nab_provider._provider.listing import parse_prefetched_metadata
from nab_provider._provider.metadata_resolver import fetch_sdist_metadata
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import YankAdmissionRequiredError
from nab_provider.policy import DistPolicy, ExtrasMode
from nab_provider.provider import Provider, ResolutionStrategy
from nab_provider.records import SdistFile
from nab_provider.yank_candidates import Candidate, YankCandidates, add_range, is_pin
from nab_provider.yanking import mark_yanked


def catalogue(releases: list[ReleaseRow]) -> YankCandidates:
    """Create a real deferred provider while leaving selection policy to its caller."""
    port = graph_port(releases)
    factory = partial(
        Provider, port, defer_yanked=True, extras_mode=ExtrasMode.BACKTRACK
    )
    return YankCandidates(factory(), factory, preferences={})


@pytest.mark.parametrize("requirement", ["dep", "dep==2"])
def test_ordinary_resolution_keeps_skipping_yanked_files(requirement: str) -> None:
    port = graph_port([("dep", "2", True, []), ("dep", "1", False, [])])
    provider = Provider(port)
    selected = provider.choose_version(
        "dep", Requirement(requirement).specifier.to_range()
    )
    assert selected == (Version("1") if requirement == "dep" else None)
    if selected is not None:
        assert provider.get_dependencies("dep", selected) == {}
    assert any(file.yanked for file in port.index.get_listing("dep"))
    assert all("2-True" not in str(call) for call in port.calls_to("request_metadata"))


def test_ordinary_mixed_version_reads_only_live_metadata() -> None:
    port = graph_port([("dep", "2", True, ["missing"]), ("dep", "2", False, [])])
    provider = Provider(port)
    assert provider.get_dependencies("dep", Version("2")) == {}
    assert all(not file.yanked for file in provider.dist_files_for("dep", Version("2")))
    assert all(
        "True.whl" not in str(call) for call in port.calls_to("request_metadata")
    )


def test_deferred_listing_is_not_preparation_permission() -> None:
    port = graph_port([("dep", "2", True, [])])
    provider = Provider(port, defer_yanked=True)
    assert provider.fetch_versions("dep")
    with pytest.raises(YankAdmissionRequiredError):
        provider.get_dependencies("dep", Version("2"))
    assert not port.calls_to("request_metadata")


def test_fact_catalogue_separates_file_classes_and_adopts_the_chosen_one() -> None:
    facts = catalogue([("dep", "2", True, []), ("dep", "2", False, ["missing"])])
    ordered = facts.ordered("dep", VersionRange.full())
    assert [c.withdrawn for c in ordered] == [False, True]
    live, withdrawn = ordered
    assert facts.ordered("dep", VersionRange.full(), choices=(withdrawn,)) == [
        withdrawn
    ]
    live_data = facts.prepare(live)
    withdrawn_data = facts.prepare(withdrawn)
    assert live_data is not None
    assert set(live_data.dependencies) == {"missing"}
    assert withdrawn_data is not None
    assert not withdrawn_data.dependencies
    assert facts.prepare(withdrawn) is withdrawn_data
    pins, provider = facts.adopt({"dep": withdrawn})
    assert pins == {"dep": Version("2")}
    assert provider.yank_admissions == frozenset({("dep", Version("2"))})
    assert all(file.yanked for file in provider.dist_files_for("dep", Version("2")))


def test_missing_metadata_remains_unknown_in_the_fact_cache() -> None:
    facts = catalogue([("dep", "2", False, [])])
    index = facts.provider.coordinator.index
    url = "https://example.test/dep-2-False.whl.metadata"
    index.record_offline_metadata_miss("dep", "2", url)
    index.store_metadata("dep", "2", None, metadata_url=url)
    candidate = facts.choices("dep")[0]
    assert facts.prepare(candidate) is None
    assert candidate in facts.unavailable
    assert candidate not in facts.facts
    reads = facts.provider.stats.get_dependencies_calls
    assert facts.prepare(candidate) is None
    assert facts.provider.stats.get_dependencies_calls == reads


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("==1", True),
        ("===1.0", True),
        (">=1,==1", True),
        ("==1.*", False),
        (">=1,<=1", False),
        ("", False),
    ],
)
def test_original_pin_syntax_is_distinct_from_matching_one_version(
    spec: str, expected: bool
) -> None:
    assert is_pin(Requirement("dep" + spec)) is expected


def test_collected_ranges_keep_every_constraint() -> None:
    ranges = {}
    add_range(ranges, "dep", Requirement("dep>=1").specifier.to_range())
    add_range(ranges, "dep", Requirement("dep<2").specifier.to_range())
    assert Version("1") in ranges["dep"]
    assert Version("2") not in ranges["dep"]


def test_offline_listing_is_distinct_from_a_complete_empty_page() -> None:
    facts = catalogue([])
    facts.provider.coordinator.index.store_listing("dep", [], offline_miss=True)
    assert facts.choices("dep") == ()
    assert facts.choices("dep") == ()
    assert facts.missing_listings == {"dep"}
    assert "missing offline" in facts.unavailable_reason


def test_cached_declared_extras_keep_only_their_active_pin_syntax() -> None:
    port = graph_port(
        [
            (
                "dep",
                "1",
                False,
                [
                    "leaf==1",
                    'other==1; extra == "x"',
                    'absent==1; python_version < "2"',
                ],
            )
        ],
        {("dep", "1", False): ("x",)},
    )
    factory = partial(
        Provider, port, defer_yanked=True, extras_mode=ExtrasMode.BACKTRACK
    )
    facts = YankCandidates(factory(), factory, preferences={})
    base = facts.prepare(facts.choices("dep")[0])
    extra = facts.prepare(facts.choices("dep[x]")[0])
    assert base is not None
    assert extra is not None
    assert {req.name for req in base.pins} == {"leaf"}
    assert {req.name for req in extra.pins} == {"leaf", "other"}
    pins, provider = facts.adopt(
        {"dep": facts.choices("dep")[0], "dep[x]": facts.choices("dep[x]")[0]}
    )
    assert set(pins) == {"dep", "dep[x]"}
    assert not provider.yank_admissions


def test_invalid_extra_is_a_metadata_rejection() -> None:
    facts = catalogue([("dep", "2", False, []), ("dep", "1", False, [])])
    candidate = facts.choices("dep[x]")[0]
    assert facts.prepare(candidate) is None
    assert candidate in facts.facts
    assert "does not provide extra" in facts.rejections[0]
    assert facts.prepare(candidate) is None


@pytest.mark.parametrize(
    ("preference", "lowest", "expected"),
    [
        ("1", False, ["1", "1.0", "2"]),
        ("1.0", False, ["1.0", "1", "2"]),
        ("1", True, ["1", "1.0", "2"]),
    ],
)
def test_fact_ordering_preserves_preferences_and_strategy(
    preference: str, lowest: bool, expected: list[str]
) -> None:
    port = graph_port([("dep", version, False, []) for version in ("1", "1.0", "2")])
    factory = partial(
        Provider,
        port,
        defer_yanked=True,
        resolution_strategy=ResolutionStrategy.LOWEST
        if lowest
        else ResolutionStrategy.HIGHEST,
    )
    facts = YankCandidates(factory(), factory, preferences={"dep": Version(preference)})
    actual = [c.label for c in facts.ordered("dep", VersionRange.full())]
    if not lowest:
        assert actual[0] == preference
        assert set(actual) == set(expected)
    else:
        assert actual == expected
    assert facts.ordered("dep", VersionRange.empty()) == []


def test_sdist_install_requires_a_source_in_the_selected_file_class() -> None:
    facts = catalogue([("dep", "2", False, [])])
    facts.provider.dist_policy = DistPolicy.SDIST_INSTALL
    source = withdrawn_sdist()
    version = Version("2")
    files = [(version, source)]
    assert facts.installable(Candidate("dep", version, withdrawn=True), files)
    assert not facts.installable(Candidate("dep", version, withdrawn=False), files)
    assert not facts.installable(Candidate("dep", Version("1"), withdrawn=True), files)
    assert not facts.installable(
        Candidate("dep", Version("2.0"), withdrawn=True), files
    )


def test_prepared_live_metadata_reports_a_withdrawn_alternative() -> None:
    facts = catalogue([("dep", "2", False, []), ("dep", "2", True, [])])
    choices = facts.choices("dep")
    assert not facts.provider.has_withdrawn_alternative()
    live = next(c for c in choices if not c.withdrawn)
    data = facts.prepare(live)
    assert data is not None
    facts.provider.metadata_cache[("dep", Version("2"))] = data.provider.metadata_cache[
        ("dep", Version("2"))
    ]
    assert facts.provider.has_withdrawn_alternative()


def withdrawn_sdist() -> SdistFile:
    """Return the withdrawn archive used to test preparation and install policy."""
    file = mark_yanked(
        SdistFile(
            filename="dep-2.tar.gz",
            url="https://example.test/dep-2.tar.gz",
            version="2",
            requires_python=None,
            upload_time=None,
            hashes=(("sha256", "a" * 64),),
        ),
        reason="broken source release",
    )
    assert isinstance(file, SdistFile)
    return file


def test_artifact_view_excludes_withdrawn_siblings_without_permission() -> None:
    port = graph_port([("dep", "2", False, []), ("dep", "2", True, [])])
    provider = Provider(port, defer_yanked=True)
    provider.fetch_versions("dep")
    files = provider.dist_files_for("dep", Version("2"))
    assert len(files) == 1
    assert not files[0].yanked


@pytest.mark.parametrize("live_wheel", [False, True])
def test_artifact_view_cannot_supply_a_withdrawn_install_source(
    live_wheel: bool,
) -> None:
    port = graph_port([("dep", "2", False, [])] if live_wheel else [])
    port.index.store_listing(
        "dep", [*(port.index.get_listing("dep") or []), withdrawn_sdist()]
    )
    provider = Provider(port, defer_yanked=True, dist_policy=DistPolicy.SDIST_INSTALL)
    provider.fetch_versions("dep")
    with pytest.raises(YankAdmissionRequiredError):
        provider.dist_files_for("dep", Version("2"))
    with pytest.raises(YankAdmissionRequiredError):
        fetch_sdist_metadata(provider, "dep", "2", withdrawn_sdist())
    assert not port.calls_to("request_sdist")


def test_prefetch_does_not_promote_legacy_sdist_metadata_to_wheel_metadata() -> None:
    port = graph_port([("dep", "2", False, [])])
    url = "https://example.test/dep-2-False.whl.metadata"
    port.index.store_metadata("dep", "2", None, metadata_url=url)
    port.index.store_sdist_metadata(
        "dep",
        "2",
        "Metadata-Version: 2.2\nName: dep\nVersion: 2\nDynamic: Requires-Dist\n\n",
    )
    provider = Provider(port, defer_yanked=True)
    key = ("dep", Version("2"))
    provider.pending_metadata_parses[key] = ("2", url)
    parse_prefetched_metadata(provider, key)
    assert key not in provider.metadata_cache
    assert key not in provider.deps_cache
