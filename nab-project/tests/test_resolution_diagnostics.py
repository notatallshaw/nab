"""Final resolution reports keep evidence while sharing suggestion emission state."""

from __future__ import annotations

from nab_project.diagnostics import ResolutionMessage
from nab_provider.diagnostics import Diagnostic
from nab_resolver.diagnostics import Suggestion, error_message
from nab_resolver.errors import ResolutionError


def test_repeated_actions_keep_every_package_cause() -> None:
    message = ResolutionMessage(
        "no solution",
        (
            (
                "first",
                Diagnostic(
                    "offline", suggestions=(Suggestion("re-run with --no-offline"),)
                ),
            ),
            (
                "second",
                Diagnostic(
                    "offline", suggestions=(Suggestion("re-run with --no-offline"),)
                ),
            ),
        ),
    )
    rendered = str(message)

    assert "first: offline" in rendered
    assert "second: offline" in rendered
    assert rendered.count("try: re-run with --no-offline") == 1


def test_distinct_policy_scopes_survive_deduplication() -> None:
    message = ResolutionMessage(
        "no solution",
        tuple(
            (
                name,
                Diagnostic(
                    "cutoff",
                    suggestions=(
                        Suggestion(f"set index.{name}.uploaded-prior-to = false"),
                    ),
                ),
            )
            for name in ("public", "internal")
        ),
    )

    assert str(message).count("try:") == 2


def test_compact_report_offers_the_primary_action_and_verbose_keeps_other_fixes() -> (
    None
):
    message = ResolutionMessage(
        "no solution",
        (
            (
                "pkg",
                Diagnostic(
                    "refused",
                    ("the cutoff rejected one file",),
                    suggestions=(
                        Suggestion(
                            "change cutoff", detail="The project sets this cutoff."
                        ),
                        Suggestion("change distribution policy"),
                    ),
                ),
            ),
        ),
    )
    compact = str(message)

    assert "Diagnostics: (-v for detail)" in compact
    assert "try: change cutoff" in compact
    assert "change distribution policy" not in compact

    verbose = message.render(verbose=True)

    assert "the cutoff rejected one file" in verbose
    assert "The project sets this cutoff." in verbose
    assert "try: change cutoff" in verbose
    assert "try: change distribution policy" in verbose


def test_cause_only_reports_need_no_verbose_pointer() -> None:
    message = ResolutionMessage("no solution", (("pkg", Diagnostic("missing")),))

    assert str(message) == "no solution\n\nDiagnostics:\n  - pkg: missing"


def test_secondary_corrections_are_advertised_as_verbose_detail() -> None:
    diagnostic = Diagnostic(
        "refused",
        suggestions=(Suggestion("change cutoff"), Suggestion("change policy")),
    )
    message = ResolutionMessage("no solution", (("pkg", diagnostic),))

    assert "Diagnostics: (-v for detail)" in str(message)
    assert "try: change policy" in message.render(verbose=True)


def test_exception_context_preserves_attribution_and_rendering() -> None:
    original = ResolutionMessage(
        "no solution", (("pkg", Diagnostic("missing", remedy="choose another index")),)
    )
    contextual = error_message(ResolutionError(original)).prefixed(
        "build requirement: "
    )

    assert isinstance(contextual, ResolutionMessage)
    assert contextual.entries == original.entries
    assert str(contextual).startswith("build requirement: no solution")
    assert "try: choose another index" in str(contextual)
    assert str(original).startswith("no solution")


def test_shared_state_suppresses_advice_across_targets_without_hiding_causes() -> None:
    message = ResolutionMessage(
        "no solution", (("pkg", Diagnostic("missing", remedy="choose another index")),)
    )
    seen: set[str] = set()

    assert "try:" in message.render(seen=seen)

    repeated = message.render(seen=seen)

    assert "pkg: missing" in repeated
    assert "try:" not in repeated
    assert "try:" in message.render()


def test_report_without_package_causes_has_no_empty_section() -> None:
    message = ResolutionMessage("no solution", ())

    assert str(message) == "no solution"
    assert message.render(verbose=True) == "no solution"
