"""Package diagnostic adapters preserve metadata failures and legacy hosts."""

from nab_provider._provider.listing_diagnosis import MetadataBlock
from nab_provider._vendor.packaging.version import Version
from nab_provider.diagnostics import Diagnostic
from nab_provider.errors import UnsupportedSdistError
from nab_resolver.diagnostics import DiagnosticMessage, Suggestion, error_message


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
