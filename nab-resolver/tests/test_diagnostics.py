"""Suggestions retain actions until an owned report renders them."""

from __future__ import annotations

import pytest

from nab_resolver.diagnostics import (
    DiagnosticMessage,
    Suggestion,
    error_message,
    suggestion_lines,
)


def test_repeated_actions_share_one_rendering_without_dropping_distinct_fixes() -> None:
    actions = (
        Suggestion('set packages."a".dist-policy = "wheel-or-sdist"'),
        Suggestion('set packages."a".dist-policy = "wheel-or-sdist"'),
        Suggestion('set packages."b".dist-policy = "wheel-or-sdist"'),
    )

    assert suggestion_lines(actions) == (
        '    try: set packages."a".dist-policy = "wheel-or-sdist"',
        '    try: set packages."b".dist-policy = "wheel-or-sdist"',
    )


def test_caller_state_deduplicates_messages_and_a_fresh_report_has_fresh_state() -> (
    None
):
    first = DiagnosticMessage(
        "first cause", suggestions=(Suggestion("run --no-offline"),)
    )
    second = DiagnosticMessage(
        "second cause", suggestions=(Suggestion("run --no-offline"),)
    )
    seen: set[str] = set()

    assert first.render(seen=seen) == "first cause\n    try: run --no-offline"
    assert second.render(seen=seen) == "second cause"
    assert second.render() == "second cause\n    try: run --no-offline"
    assert first.render() == "first cause\n    try: run --no-offline"


def test_conditions_remain_part_of_the_action_identity() -> None:
    actions = (
        Suggestion("if this release has universal dependencies, enable borrowing"),
        Suggestion("if the host can build this release, permit its metadata build"),
    )

    assert len(suggestion_lines(actions)) == 2


@pytest.mark.parametrize("verbose", [False, True])
def test_verbose_context_does_not_change_the_action_or_repeat_it(
    *, verbose: bool
) -> None:
    action = Suggestion(
        "set index.internal.uploaded-prior-to = false",
        detail="The index sets this cutoff.\nOther indexes retain theirs.",
    )
    lines = suggestion_lines((action, action), verbose=verbose, indent="  ")

    assert lines[0] == "  try: set index.internal.uploaded-prior-to = false"
    assert lines[1:] == (
        ("       The index sets this cutoff.", "       Other indexes retain theirs.")
        if verbose
        else ()
    )


def test_structured_messages_survive_exception_translation() -> None:
    message = DiagnosticMessage(
        "no usable release", suggestions=(Suggestion("choose another release"),)
    )
    original = ValueError(message)
    translated = RuntimeError(*original.args)

    assert error_message(translated) is message
    assert str(translated) == "no usable release\n    try: choose another release"


def test_outer_context_preserves_the_suggestion_without_mutating_the_original() -> None:
    original = DiagnosticMessage("refused", suggestions=(Suggestion("change policy"),))
    contextual = error_message(ValueError(original)).prefixed("archive: ")

    assert str(contextual) == "archive: refused\n    try: change policy"
    assert str(original) == "refused\n    try: change policy"


def test_exception_representations_keep_the_diagnostic_text() -> None:
    message = DiagnosticMessage("refused", suggestions=(Suggestion("change policy"),))

    assert repr(ValueError(message)) == f"ValueError({str(message)!r})"


@pytest.mark.parametrize(
    "error", [ValueError("ordinary error"), ValueError("left", "right"), ValueError()]
)
def test_plain_exception_text_is_preserved(error: ValueError) -> None:
    assert str(error_message(error)) == str(error)
    assert error_message(error).suggestions == ()


def test_message_conversion_never_logs_a_suggestion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = DiagnosticMessage(
        "rejected candidate", suggestions=(Suggestion("edit policy"),)
    )

    assert "edit policy" in str(message)
    assert caplog.records == []


def test_multiline_actions_align_their_continuation_with_the_action() -> None:
    assert suggestion_lines((Suggestion("use this path:\n--output 'file.txt'"),)) == (
        "    try: use this path:",
        "         --output 'file.txt'",
    )
