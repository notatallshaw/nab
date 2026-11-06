"""Package diagnostic adapters preserve metadata failures and legacy hosts."""

import pytest

from nab_provider._provider.listing_diagnosis import MetadataBlock, metadata_diagnostic
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.diagnostics import Diagnostic, ResolutionMessage
from nab_provider.errors import MetadataError, MissingExtraError, UnsupportedSdistError
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator, make_metadata, make_wheel
from nab_resolver.diagnostics import DiagnosticMessage, Suggestion, error_message
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term


@pytest.fixture
def build_failure() -> ResolutionMessage:
    """A build requirement rejected by its wheel's Python metadata."""
    return ResolutionMessage(
        "could not resolve build requirements",
        (
            (
                "badleaf",
                Diagnostic(
                    "metadata rejected",
                    ("Requires-Python: >=99",),
                    suggestions=(Suggestion("use a compatible Python"),),
                ),
            ),
        ),
    )


@pytest.mark.parametrize("cached", [False, True], ids=["snapshot", "provider-cache"])
@pytest.mark.parametrize("error_type", [MetadataError, UnsupportedSdistError])
def test_metadata_snapshot_preserves_child_diagnostics(
    build_failure: ResolutionMessage, error_type: type[MetadataError], *, cached: bool
) -> None:
    snapshot = MetadataBlock.from_error(error_type(build_failure))

    if cached:
        provider = Provider(make_coordinator([]))
        cache = (
            provider._unsupported_sdists
            if error_type is UnsupportedSdistError
            else provider._invalid_metadata
        )
        cache[("pkg", Version("1"))] = snapshot

        with pytest.raises(error_type) as caught:
            provider.get_dependencies("pkg", Version("1"))
        recovered = error_message(caught.value)
    else:
        recovered = error_message(snapshot.as_error(error_type))

    assert recovered.render() == build_failure.render()
    assert recovered.render(verbose=True) == build_failure.render(verbose=True)


@pytest.mark.parametrize("duplicate_action", [False, True])
def test_metadata_report_preserves_children_and_deduplicates_actions(
    build_failure: ResolutionMessage, *, duplicate_action: bool
) -> None:
    blocks = [MetadataBlock.from_error(UnsupportedSdistError(build_failure))]
    if duplicate_action:
        other_failure = MetadataError(
            DiagnosticMessage(
                "another rejection",
                suggestions=(Suggestion("use a compatible Python"),),
            )
        )
        blocks.append(MetadataBlock.from_error(other_failure))

    diagnostic = metadata_diagnostic(Provider(make_coordinator([])), "pkg", blocks)
    report = ResolutionMessage("no solution", (("pkg", diagnostic),))

    compact = report.render()
    assert ">=99" not in compact
    assert "badleaf:" not in compact
    assert compact.count("try: use a compatible Python") == 1

    seen: set[str] = set()
    verbose = report.render(verbose=True, seen=seen)
    assert "badleaf: metadata rejected" in verbose
    assert "        Requires-Python: >=99" in verbose
    if duplicate_action:
        assert "another rejection" in verbose
    assert verbose.count("try: use a compatible Python") == 1

    repeated = report.render(verbose=True, seen=seen)
    assert "badleaf: metadata rejected" in repeated
    assert "Requires-Python: >=99" in repeated
    assert "try:" not in repeated


def test_cached_extra_rejections_preserve_child_diagnostics(
    build_failure: ResolutionMessage,
) -> None:
    provider = Provider(make_coordinator([make_wheel("1")]))
    provider._invalid_metadata[("pkg", Version("1"))] = MetadataBlock.from_error(
        MetadataError(build_failure)
    )

    assert provider.choose_version("pkg[x]", VersionRange.full()) is None
    diagnostic = provider.get_no_versions_reason("pkg[x]")

    assert diagnostic is not None
    assert any(
        "Requires-Python: >=99" in line for line in diagnostic.lines(verbose=True)
    )
    assert all(">=99" not in line for line in diagnostic.lines())


def test_metadata_failure_snapshots_preserve_actions_and_filter_attribution() -> None:
    message = DiagnosticMessage(
        "dynamic metadata", suggestions=(Suggestion("permit its build"),)
    )
    error = UnsupportedSdistError(message)
    error.filtered_sdist_version = Version("1")
    snapshot = MetadataBlock.from_error(error)
    first = snapshot.as_error(UnsupportedSdistError)
    second = snapshot.as_error(UnsupportedSdistError)

    assert isinstance(first, UnsupportedSdistError)
    assert first is not second
    assert first.filtered_sdist_version == second.filtered_sdist_version == Version("1")
    assert error_message(first).suggestions == message.suggestions
    assert str(first) == str(second) == str(error)
    assert first.__traceback__ is None


def test_legacy_diagnostic_hosts_can_still_read_the_primary_remedy() -> None:
    diagnostic = Diagnostic(
        "refused", ("evidence",), "old action", suggestions=(Suggestion("new action"),)
    )

    assert diagnostic.remedy == "old action"
    assert [suggestion.action for suggestion in diagnostic.suggestions] == [
        "old action",
        "new action",
    ]
    assert Diagnostic("nothing to change").remedy is None


@pytest.mark.parametrize("state", ["unfetched", "matching", "outside-range", "absent"])
def test_known_empty_domains_can_be_explained_without_a_selection(state: str) -> None:
    provider = Provider(
        make_coordinator([] if state == "absent" else [make_wheel("1")])
    )
    if state != "unfetched":
        provider.fetch_versions("pkg")
    allowed = VersionRange.singleton(Version("2" if state == "outside-range" else "1"))

    diagnostic = provider.get_no_versions_reason("pkg", allowed)

    if state in {"unfetched", "matching"}:
        assert diagnostic is None
    else:
        assert diagnostic is not None
        expected = (
            "package not found on any configured index"
            if state == "absent"
            else "no version matches the requirement"
        )
        assert diagnostic.short == expected


def test_reporting_one_missing_domain_does_not_persist_it_for_another_domain() -> None:
    provider = Provider(make_coordinator([make_wheel("1")]))
    provider.fetch_versions("pkg")

    missing = provider.get_no_versions_reason(
        "pkg", VersionRange.singleton(Version("2"))
    )
    available = provider.get_no_versions_reason(
        "pkg", VersionRange.singleton(Version("1"))
    )

    assert missing is not None
    assert missing.short == "no version matches the requirement"
    assert available is None
    assert provider.get_no_versions_reason("pkg") is None


def test_availability_domains_do_not_narrow_permanent_ban_evidence() -> None:
    provider = Provider(make_coordinator([make_wheel("1")]))
    provider.fetch_versions("pkg")
    provider.record_root_ban(
        "pkg",
        "cap",
        Requirement("cap>=2").specifier.to_range(),
        Requirement("cap==1").specifier.to_range(),
        [Version("3"), Version("4")],
    )
    provider.record_metadata_ban(
        "pkg",
        {
            Version("3"): MetadataBlock("pkg 3 metadata unavailable"),
            Version("4"): MetadataBlock("pkg 4 metadata unavailable"),
        },
    )

    diagnostic = provider.get_no_versions_reason(
        "pkg",
        Requirement("pkg>=2,<4").specifier.to_range(),
        availability_range=VersionRange.singleton(Version("2")),
    )

    assert diagnostic is not None
    report = "\n".join(diagnostic.lines(verbose=True))
    assert "3 needs cap in >=2, but your project requires cap ==1" in report
    assert "pkg 3 metadata unavailable" in report
    assert "4 needs cap" not in report
    assert "pkg 4 metadata unavailable" not in report


def test_missing_extra_rejections_can_share_the_metadata_failure_snapshot() -> None:
    snapshot = MetadataBlock.from_error(
        MissingExtraError("pkg==1 does not provide extra 'x'")
    )

    assert snapshot.message == "pkg==1 does not provide extra 'x'"
    assert snapshot.filtered_sdist_version is None


@pytest.mark.parametrize(
    ("parent_bound", "root_bound", "constraint", "expected"),
    [
        ("==2", "", "", ">=2"),
        ("", "", "", ">=1"),
        ("", ">=2", "", ">=2"),
        ("", "", ">=2", ">=2"),
    ],
)
def test_extra_requirement_bounds_use_only_the_declaring_parent_domain(
    parent_bound: str, root_bound: str, constraint: str, expected: str
) -> None:
    provider = Provider(
        make_coordinator(
            listings={
                "mid": [make_wheel(version, package="mid") for version in ("1", "2")],
                "other": [make_wheel("3", package="other")],
                "leaf": [],
            },
            metadata_by_version={
                "1": make_metadata("mid", "1", "leaf[x]>=1"),
                "2": make_metadata("mid", "2", "leaf[x]>=2"),
                "3": make_metadata("other", "3", "leaf[x]"),
            },
        ),
        root_requirements={
            "leaf": Requirement(f"leaf{root_bound}").specifier.to_range()
        },
        constraints={"leaf": Requirement(f"leaf{constraint}").specifier.to_range()},
    )
    for name, version in (("mid", "1"), ("mid", "2"), ("other", "3")):
        provider.get_dependencies(name, Version(version))

    clause = Incompatibility(
        [
            Term("mid", Requirement(f"mid{parent_bound}").specifier.to_range()),
            Term("leaf[x]", VersionRange.full(), positive=False),
        ],
        IncompatibilityCause.DEPENDENCY,
    )

    allowed = provider.extra_requirement_range("leaf[x]", clause)

    assert allowed == Requirement(f"leaf{expected}").specifier.to_range()
    assert provider.extra_requirement_range("leaf[y]", clause) is None
