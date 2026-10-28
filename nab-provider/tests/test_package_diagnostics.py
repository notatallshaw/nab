"""Package diagnostic adapters preserve metadata failures and legacy hosts."""

import pytest

from nab_provider._provider.listing_diagnosis import MetadataBlock
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.diagnostics import Diagnostic
from nab_provider.errors import MissingExtraError, UnsupportedSdistError
from nab_provider.provider import Provider
from nab_provider.testing import make_coordinator, make_metadata, make_wheel
from nab_resolver.diagnostics import DiagnosticMessage, Suggestion, error_message
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term


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
