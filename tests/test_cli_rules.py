"""The parser's rules, asserted against what the CLI returns.

Each case is named for the rule it pins.  The rules are set out in
``work/analysis/nab-cli-rethink/kb/research-parser-algorithms.md``, one
case per row of its matrix.

Most cases assert what :func:`parse`, :func:`diagnose` and :func:`page`
return.  The last section drives the same lines through
:func:`nab.cli.run`, which is the only way to state a rule whose outcome
is a status or a stream.  Pass-through operands have no case at all,
because no command declares any.

Most cases run against the fixture table below, which is the option table
the matrix is written against.  It carries what nab's own table cannot
express: a value-taking short option, a repeatable operand, and
``--max-concurrency`` on ``lock``.  The cases that need the shipped table
say so.
"""

from __future__ import annotations

import builtins
import errno
import io
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest

from nab._cli import spec
from nab._cli.diagnose import diagnose, suggest
from nab._cli.dispatch import dispatch
from nab._cli.parse import Parsed, UsageError, build, parse
from nab._cli.render import page, terminal_width, wrap
from nab.cli import run

_HELP = ("what it does", "what the other one does")


def _option(
    long: str,
    dest: str,
    kind: str = "flag",
    *,
    short: str = "",
    vtype: str = "",
    choices: tuple[str, ...] = (),
    const: object = True,
    default: object = False,
    required: bool = False,
) -> tuple[Any, ...]:
    return (long, short, kind, dest, vtype, choices, const, default, required, 0)


def _operand(
    dest: str,
    *,
    kind: str = "positional",
    vtype: str = "str",
    choices: tuple[str, ...] = (),
    default: object = "",
    required: bool = False,
) -> tuple[Any, ...]:
    return ("", "", kind, dest, vtype, choices, None, default, required, 0)


_ROOT = (
    _option("--verbose", "verbose", "count", short="v", default=0),
    _option("--quiet", "quiet", "count", short="q", default=0),
    _option(
        "--color",
        "color",
        "value",
        vtype="choice",
        choices=("auto", "always", "never"),
        const=None,
        default=None,
    ),
    _option("--no-progress", "no_progress"),
    _option("--help", "help", "eager", short="h"),
    _option("--version", "version", "eager", short="V"),
)

_LOCK = (
    _option(
        "--output", "output", "value", short="o", vtype="path", const=None, default=None
    ),
    _option("--upgrade", "upgrade"),
    _option("--no-upgrade", "upgrade", "neg", const=False),
    _option("--cache", "cache", default=True),
    _option("--no-cache", "cache", "neg", const=False, default=True),
    _option("--offline", "offline", "tri", vtype="bool?", default=None),
    _option("--no-offline", "offline", "neg", const=False, default=None),
    _option(
        "--project-constraint",
        "project_constraint",
        "append",
        vtype="str",
        const=None,
        default=(),
    ),
    _option("--groups", "groups", "star", vtype="str", const=None, default=()),
    _option(
        "--max-concurrency",
        "max_concurrency",
        "value",
        vtype="int",
        const=None,
        default=None,
    ),
    _option(
        "--project-resolution",
        "project_resolution",
        "value",
        vtype="choice",
        choices=("highest", "lowest", "lowest-direct"),
        const=None,
        default=None,
    ),
    _option("--index-url", "index_url", "value", vtype="str", const=None, default=None),
    _option(
        "--index-strategy",
        "index_strategy",
        "value",
        vtype="str",
        const=None,
        default=None,
    ),
    _operand("paths", default=()),
)

_CACHE = (
    _option(
        "--cache-dir", "cache_dir", "value", vtype="path?", const=None, default=None
    ),
    _operand("action", kind="verb", choices=("dir", "verify", "clear"), required=True),
)

_CONFIG = (
    _operand("action", kind="verb", choices=("list", "get", "explain"), required=True),
    _operand("key", default=""),
)

_COMMANDS = {
    "cache": _CACHE,
    "config": _CONFIG,
    "lock": _LOCK,
    "download": (_operand("paths", default=()),),
}


def _parse(argv: list[str]) -> Parsed:
    """Run the fixture table over ``argv``."""
    return parse(tuple(argv), _ROOT, _COMMANDS, "nab")


def _shipped(argv: list[str]) -> Parsed:
    """Run the table nab ships over ``argv``."""
    return parse(tuple(argv), spec.ROOT, spec.COMMANDS, "nab")


def _shipped_page(command: str, width: int) -> str:
    """One of nab's own pages, at the width a test pins."""
    return page(
        command, spec.ROOT, spec.COMMANDS, spec.HELP, spec.DISPATCH, "nab", width
    )


def _painted_page(command: str, width: int) -> str:
    """The shipped page for ``command``, rendered with colour on."""
    return page(
        command,
        spec.ROOT,
        spec.COMMANDS,
        spec.HELP,
        spec.DISPATCH,
        "nab",
        width,
        color=True,
    )


def _stripped(text: str) -> str:
    """``text`` with every SGR escape removed, written without ``re``."""
    out: list[str] = []
    rest = text
    while "\033[" in rest:
        head, _, rest = rest.partition("\033[")
        out.append(head)
        _code, _, rest = rest.partition("m")
    out.append(rest)
    return "".join(out)


def _refused(argv: list[str]) -> UsageError:
    """The error the fixture table raises on ``argv``."""
    with pytest.raises(UsageError) as caught:
        _parse(argv)
    return caught.value


def _value(parsed: Parsed, dest: str) -> object:
    """One value, whichever table declared it."""
    if dest in parsed.values:
        return parsed.values[dest]
    return parsed.options[dest]


def _ids(cases: tuple[tuple[Any, ...], ...]) -> list[str]:
    """One id per case: its rule, then which case of that rule it is."""
    seen: dict[str, int] = {}
    ids = []
    for case in cases:
        rule = case[0]
        seen[rule] = seen.get(rule, 0) + 1
        ids.append(f"{rule}-{seen[rule]}")
    return ids


_PARSES: tuple[tuple[str, list[str], dict[str, object]], ...] = (
    ("dash-then-more-is-an-option", ["-v", "lock"], {"verbose": 1}),
    ("dash-then-more-is-an-option", ["lock", "-"], {"paths": ("-",)}),
    ("dash-then-more-is-an-option", ["lock", ""], {"paths": ("",)}),
    (
        "dash-dash-plus-name-is-a-long-option",
        ["lock", "--"],
        {"paths": (), "upgrade": False},
    ),
    (
        "nothing-after-dash-dash-is-inspected",
        ["lock", "--", "--color", "never"],
        {"paths": ("--color", "never"), "color": None},
    ),
    ("dash-is-the-only-option-prefix", ["lock", "+v"], {"paths": ("+v",)}),
    ("dash-is-the-only-option-prefix", ["lock", "/v"], {"paths": ("/v",)}),
    (
        "unclaimed-token-becomes-an-operand",
        ["lock", "a", "--upgrade", "b"],
        {"paths": ("a", "b"), "upgrade": True},
    ),
    (
        "first-dash-dash-ends-options-and-is-dropped",
        ["lock", "--", "--upgrade"],
        {"paths": ("--upgrade",), "upgrade": False},
    ),
    (
        "second-dash-dash-is-an-operand",
        ["lock", "--", "--", "x"],
        {"paths": ("--", "x")},
    ),
    (
        "dash-dash-can-be-an-option-value",
        ["lock", "--output=--", "x"],
        {"output": "--", "paths": ("x",)},
    ),
    (
        "dash-dash-before-a-command-still-dispatches",
        ["--", "lock", "--upgrade"],
        {"upgrade": True, "paths": ()},
    ),
    ("a-trailing-dash-dash-adds-no-operands", ["lock", "--"], {"paths": ()}),
    (
        "no-nested-command-after-dash-dash",
        ["lock", "--", "x", "cache"],
        {"paths": ("x", "cache")},
    ),
    ("cluster-equals-separate-options", ["-vq", "lock"], {"verbose": 1, "quiet": 1}),
    ("repeated-letter-counts-each-time", ["-vvv", "lock"], {"verbose": 3}),
    ("repeated-letter-counts-each-time", ["-vqv", "lock"], {"verbose": 2, "quiet": 1}),
    (
        "value-letter-takes-rest-of-cluster",
        ["lock", "-vqox.txt"],
        {"verbose": 1, "quiet": 1, "output": "x.txt"},
    ),
    (
        "separated-short-value-is-next-token",
        ["lock", "-o", "x.txt"],
        {"output": "x.txt"},
    ),
    ("attached-short-value-keeps-equals", ["lock", "-o=x"], {"output": "=x"}),
    ("lone-dash-is-always-an-operand", ["lock", "-"], {"paths": ("-",)}),
    ("long-name-takes-the-next-token", ["lock", "--output", "x"], {"output": "x"}),
    (
        "value-is-all-after-the-first-equals",
        ["lock", "--project-constraint=a=b"],
        {"project_constraint": ("a=b",)},
    ),
    ("bare-equals-gives-an-empty-value", ["lock", "--output="], {"output": ""}),
    ("bare-equals-gives-an-empty-value", ["lock", "--output", ""], {"output": ""}),
    ("any-string-can-be-a-value", ["lock", "--output", "a b"], {"output": "a b"}),
    ("any-string-can-be-a-value", ["lock", "--output", "="], {"output": "="}),
    ("option-shaped-token-is-not-a-value", ["lock", "--output=-v"], {"output": "-v"}),
    ("option-shaped-token-is-not-a-value", ["lock", "--output", "-"], {"output": "-"}),
    (
        "option-shaped-token-is-not-a-value",
        ["lock", "--max-concurrency", "-1"],
        {"max_concurrency": -1},
    ),
    ("unset-boolean-is-not-false", ["lock"], {"offline": None}),
    (
        "no-prefix-spelling-sets-the-boolean-false",
        ["lock", "--upgrade"],
        {"upgrade": True},
    ),
    (
        "no-prefix-spelling-sets-the-boolean-false",
        ["lock", "--no-upgrade"],
        {"upgrade": False},
    ),
    ("absent-boolean-defers-to-config", ["lock"], {"offline": None}),
    (
        "only-a-tri-state-boolean-takes-a-value",
        ["lock", "--upgrade", "true"],
        {"upgrade": True, "paths": ("true",)},
    ),
    (
        "only-a-tri-state-boolean-takes-a-value",
        ["lock", "--offline"],
        {"offline": True},
    ),
    (
        "only-a-tri-state-boolean-takes-a-value",
        ["lock", "--offline", "x"],
        {"offline": True, "paths": ("x",)},
    ),
    (
        "only-a-tri-state-boolean-takes-a-value",
        ["lock", "--offline", "None"],
        {"offline": None},
    ),
    (
        "only-a-tri-state-boolean-takes-a-value",
        ["lock", "--offline", "True"],
        {"offline": True},
    ),
    (
        "only-a-tri-state-boolean-takes-a-value",
        ["lock", "--offline", "False"],
        {"offline": False},
    ),
    (
        "only-a-tri-state-boolean-takes-a-value",
        ["lock", "--no-offline", "True"],
        {"offline": False, "paths": ("True",)},
    ),
    (
        "last-boolean-spelling-wins",
        ["lock", "--upgrade", "--no-upgrade"],
        {"upgrade": False},
    ),
    (
        "last-boolean-spelling-wins",
        ["lock", "--no-upgrade", "--upgrade"],
        {"upgrade": True},
    ),
    ("a-defaulted-boolean-reads-as-given", ["lock", "--cache"], {"cache": True}),
    ("a-defaulted-boolean-reads-as-given", ["lock"], {"cache": True}),
    (
        "repeatable-option-accumulates-in-order",
        ["lock", "--project-constraint", "a", "--project-constraint", "b"],
        {"project_constraint": ("a", "b")},
    ),
    (
        "repeated-value-option-keeps-the-last",
        ["lock", "--output", "a", "--output", "b"],
        {"output": "b"},
    ),
    ("operand-order-is-preserved", ["lock", "b", "--", "a"], {"paths": ("b", "a")}),
    (
        "options-and-operands-interleave-freely",
        ["lock", "a", "--upgrade", "b"],
        {"paths": ("a", "b"), "upgrade": True},
    ),
    (
        "options-and-operands-interleave-freely",
        ["lock", "a", "--output", "x", "b"],
        {"paths": ("a", "b"), "output": "x"},
    ),
    ("first-operand-names-the-command", ["lock", "x"], {"paths": ("x",)}),
    ("first-operand-names-the-command", ["lock", "lock"], {"paths": ("lock",)}),
    ("global-option-works-before-command", ["--verbose", "lock"], {"verbose": 1}),
    ("global-option-works-after-command", ["lock", "--verbose"], {"verbose": 1}),
    ("global-option-on-both-sides-combines", ["-v", "lock", "-v"], {"verbose": 2}),
    ("global-option-on-both-sides-combines", ["-vv", "lock"], {"verbose": 2}),
    (
        "innermost-options-searched-first",
        ["cache", "dir", "--verbose"],
        {"action": "dir", "verbose": 1},
    ),
    ("nested-commands-follow-the-same-rules", ["cache", "dir"], {"action": "dir"}),
)


@pytest.mark.parametrize(("rule", "argv", "expected"), _PARSES, ids=_ids(_PARSES))
def test_a_line_parses_to_the_values_the_rule_names(
    rule: str, argv: list[str], expected: dict[str, object]
) -> None:
    parsed = _parse(argv)

    assert {dest: _value(parsed, dest) for dest in expected} == expected, rule


_DISPATCHES: tuple[tuple[str, list[str], str], ...] = (
    ("dash-dash-before-a-command-still-dispatches", ["--", "lock"], "lock"),
    (
        "dash-dash-before-a-command-still-dispatches",
        ["--", "lock", "--upgrade"],
        "lock",
    ),
    ("a-trailing-dash-dash-adds-no-operands", ["lock", "--"], "lock"),
    ("first-operand-names-the-command", ["lock", "x"], "lock"),
    ("innermost-options-searched-first", ["cache", "dir", "--verbose"], "cache"),
    ("nested-commands-follow-the-same-rules", ["cache", "dir"], "cache"),
)


@pytest.mark.parametrize(
    ("rule", "argv", "command"), _DISPATCHES, ids=_ids(_DISPATCHES)
)
def test_a_line_names_the_command_the_rule_names(
    rule: str, argv: list[str], command: str
) -> None:
    assert _parse(argv).command == command, rule


_REFUSALS: tuple[tuple[str, list[str], str], ...] = (
    ("one-dash-starts-a-short-cluster", ["--vq", "lock"], "unrecognized option '--vq'"),
    (
        "option-names-are-case-sensitive",
        ["lock", "--OUTPUT", "x"],
        "unrecognized option '--OUTPUT'",
    ),
    (
        "consumed-value-is-never-an-option",
        ["lock", "--output", "--color"],
        "option '--output' requires a value",
    ),
    (
        "dash-dash-can-be-an-option-value",
        ["lock", "--output", "--", "x"],
        "option '--output' requires a value",
    ),
    (
        "dash-dash-before-a-command-still-dispatches",
        ["--", "--verbose"],
        "unknown command '--verbose'",
    ),
    (
        "unknown-letter-names-letter-and-cluster",
        ["-vx", "lock"],
        "unrecognized option '-x' in '-vx'",
    ),
    (
        "unknown-letter-names-letter-and-cluster",
        ["-hx", "lock"],
        "unrecognized option '-x' in '-hx'",
    ),
    (
        "valueless-letter-takes-no-attached-value",
        ["-v3", "lock"],
        "unrecognized option '-3' in '-v3'",
    ),
    (
        "long-names-are-never-abbreviated",
        ["lock", "--outp", "x"],
        "unrecognized option '--outp'",
    ),
    (
        "long-names-are-never-abbreviated",
        ["lock", "--index", "u"],
        "unrecognized option '--index'",
    ),
    (
        "valueless-option-refuses-attached-value",
        ["lock", "--upgrade=yes"],
        "option '--upgrade' does not take a value",
    ),
    (
        "valueless-option-refuses-attached-value",
        ["lock", "--help=yes"],
        "option '--help' does not take a value",
    ),
    (
        "long-token-never-retried-as-cluster",
        ["--vq", "lock"],
        "unrecognized option '--vq'",
    ),
    ("dash-dash-with-equals-is-unknown", ["lock", "--=x"], "unrecognized option '--'"),
    (
        "value-option-at-end-of-line-is-refused",
        ["lock", "--output"],
        "option '--output' requires a value",
    ),
    (
        "value-taken-by-position-not-syntax",
        ["lock", "--output", "--"],
        "option '--output' requires a value",
    ),
    (
        "option-shaped-token-is-not-a-value",
        ["lock", "--output", "--verbose"],
        "write --output=--verbose to pass it",
    ),
    (
        "option-shaped-token-is-not-a-value",
        ["lock", "-o", "--verbose"],
        "write -o--verbose to pass it",
    ),
    (
        "option-shaped-token-is-not-a-value",
        ["lock", "--output", "-1"],
        "option '--output' requires a value",
    ),
    (
        "choice-value-must-be-in-the-set",
        ["lock", "--project-resolution", "bogus"],
        (
            "invalid value 'bogus' for '--project-resolution'; "
            "choose from highest, lowest, lowest-direct"
        ),
    ),
    (
        "numeric-option-refuses-a-non-number",
        ["lock", "--max-concurrency", "x"],
        "expected an integer",
    ),
    (
        "numeric-option-refuses-a-non-number",
        ["lock", "--max-concurrency", "-3.5"],
        "expected an integer",
    ),
    (
        "only-a-tri-state-boolean-takes-a-value",
        ["lock", "--upgrade=true"],
        "option '--upgrade' does not take a value",
    ),
    (
        "only-a-tri-state-boolean-takes-a-value",
        ["lock", "--offline=maybe"],
        "choose from True, False",
    ),
    (
        "double-negation-is-an-unknown-option",
        ["lock", "--no-no-upgrade"],
        "unrecognized option '--no-no-upgrade'",
    ),
    (
        "command-option-too-early-is-unknown",
        ["--upgrade", "lock"],
        "unrecognized option '--upgrade'",
    ),
    ("unknown-command-refused-with-suggestions", ["lok"], "unknown command 'lok'"),
    (
        "missing-command-lists-the-commands",
        [],
        "a command is required; choose from cache, config, lock, download",
    ),
    ("command-name-must-match-exactly", ["Lock"], "unknown command 'Lock'"),
    ("command-name-must-match-exactly", ["loc"], "unknown command 'loc'"),
    (
        "option-value-takes-the-command-word",
        ["--color", "lock"],
        "invalid value 'lock' for '--color'",
    ),
    (
        "first-refusal-stops-the-parse",
        ["--nope", "--alsonope"],
        "unrecognized option '--nope'",
    ),
    (
        "an-earlier-refusal-beats-help",
        ["--nope", "--help"],
        "unrecognized option '--nope'",
    ),
    (
        "an-earlier-refusal-beats-help",
        ["lock", "--output", "--help"],
        "option '--output' requires a value",
    ),
    (
        "required-operand-left-empty-is-refused",
        ["config"],
        "missing required argument 'action'; choose from list, get, explain",
    ),
    (
        "each-refusal-has-one-exact-wording",
        ["cache", "dir", "extra"],
        "unexpected argument 'extra'",
    ),
)


@pytest.mark.parametrize(("rule", "argv", "expected"), _REFUSALS, ids=_ids(_REFUSALS))
def test_a_line_the_walk_refuses_says_what_is_wrong(
    rule: str, argv: list[str], expected: str
) -> None:
    error = _refused(argv)

    assert expected in error.message, rule
    assert expected in diagnose(error)


_SHAPES: tuple[tuple[str, list[str], str], ...] = (
    ("unknown long option", ["lock", "--outupt"], "unrecognized option '--outupt'"),
    ("unknown short option", ["-vx"], "unrecognized option '-x' in '-vx'"),
    ("missing value", ["lock", "--output"], "option '--output' requires a value"),
    (
        "value looks like an option",
        ["lock", "--output", "--verbose"],
        (
            "option '--output' requires a value, but '--verbose' looks like an "
            "option; write --output=--verbose to pass it"
        ),
    ),
    (
        "value on a valueless option",
        ["lock", "--upgrade=yes"],
        "option '--upgrade' does not take a value",
    ),
    (
        "bad choice",
        ["lock", "--project-resolution", "bogus"],
        (
            "invalid value 'bogus' for '--project-resolution'; "
            "choose from highest, lowest, lowest-direct"
        ),
    ),
    (
        "bad number",
        ["lock", "--max-concurrency", "x"],
        "invalid value 'x' for '--max-concurrency': expected an integer",
    ),
    ("unknown command", ["lok"], "unknown command 'lok'"),
    (
        "missing command",
        [],
        "a command is required; choose from cache, config, lock, download",
    ),
    (
        "missing required argument",
        ["config"],
        "missing required argument 'action'; choose from list, get, explain",
    ),
    (
        "too many operands",
        ["cache", "dir", "extra.toml"],
        "unexpected argument 'extra.toml'",
    ),
)


@pytest.mark.parametrize(
    ("shape", "argv", "message"), _SHAPES, ids=[shape for shape, _argv, _m in _SHAPES]
)
def test_every_message_is_written_in_its_declared_shape(
    shape: str, argv: list[str], message: str
) -> None:
    """One line, lower case after the prefix, no trailing period."""
    assert _refused(argv).message == message, shape


def test_a_required_option_the_line_never_gave_is_refused() -> None:
    """nab declares no such row; the walk still has to answer for one."""
    required = _option(
        "--format",
        "format",
        "value",
        vtype="str",
        const=None,
        default=None,
        required=True,
    )
    commands = {"probe": (required,)}

    with pytest.raises(UsageError) as caught:
        parse(("probe",), _ROOT, commands, "nab")

    assert caught.value.message == "option '--format' is required"


def test_an_operand_that_will_not_convert_is_named_by_its_slot() -> None:
    """An operand has no flag to quote, so the message quotes the dest."""
    commands = {"probe": (_operand("count", vtype="int"),)}

    with pytest.raises(UsageError) as caught:
        parse(("probe", "x"), _ROOT, commands, "nab")

    assert caught.value.message == "invalid value 'x' for 'count': expected an integer"


def test_a_required_operand_with_no_verbs_names_the_slot_alone() -> None:
    """nab declares no such slot, so the case runs against a fixture table."""
    commands = {"probe": (_operand("name", required=True),)}

    with pytest.raises(UsageError) as caught:
        parse(("probe",), _ROOT, commands, "nab")

    assert caught.value.message == "missing required argument 'name'"


class TestTheCommandSlot:
    """What a line with no command still has to answer."""

    def test_a_root_flag_alone_is_still_a_missing_command(self) -> None:
        assert _refused(["--verbose"]).message.startswith("a command is required")

    def test_a_root_value_converts_before_the_command_is_missed(self) -> None:
        assert "invalid value 'lock'" in _refused(["--color", "lock"]).message


class TestErrorText:
    """The rules about what a message says rather than about the refusal."""

    def test_a_root_error_names_the_program(self) -> None:
        assert diagnose(_refused(["--nope"])).startswith("nab: ")

    def test_a_command_error_names_the_command_path(self) -> None:
        assert diagnose(_refused(["lock", "--nope"])).startswith("nab lock: ")

    def test_an_error_is_three_lines_and_lists_no_options(self) -> None:
        text = diagnose(_refused(["lock", "--outupt"]))

        assert len(text.splitlines()) <= 3
        assert "--index-url" not in text

    def test_the_first_error_wins_and_the_second_is_not_reported(self) -> None:
        assert "--alsonope" not in _refused(["--nope", "--alsonope"]).message

    def test_one_candidate_is_offered_on_its_own_line(self) -> None:
        assert "did you mean '--output'?" in diagnose(
            _refused(["lock", "--outupt", "x"])
        )

    def test_a_prefix_family_is_offered_together(self) -> None:
        text = diagnose(_refused(["lock", "--index", "u"]))

        assert "did you mean one of " in text
        assert "'--index-url'" in text
        assert "'--index-strategy'" in text

    def test_a_token_nothing_resembles_draws_no_suggestion(self) -> None:
        assert "did you mean" not in diagnose(_refused(["lock", "--zzzz"]))

    def test_the_escape_the_message_advises_is_the_one_that_works(self) -> None:
        """A short option attaches with no separator, because a leading ``=`` is part of the value."""
        assert _parse(["lock", "-o--verbose"]).values["output"] == "--verbose"
        assert _parse(["lock", "--output=--verbose"]).values["output"] == "--verbose"

    def test_a_token_that_is_only_dashes_draws_no_suggestion(self) -> None:
        """It prefixes every name, so the first two rows are not an answer."""
        assert "did you mean" not in diagnose(_refused(["lock", "---"]))
        assert "did you mean" not in diagnose(_refused(["---"]))

    def test_a_command_option_before_the_command_offers_the_command(self) -> None:
        assert "did you mean 'lock'?" in diagnose(_refused(["--lock"]))

    def test_a_command_name_stops_being_a_candidate_once_it_is_given(self) -> None:
        assert "did you mean" not in diagnose(_refused(["lock", "--config"]))

    def test_a_token_that_is_not_utf8_is_escaped_rather_than_echoed(self) -> None:
        text = diagnose(_refused(["lock", "--\udcffad"]))

        assert "\\udcff" in text
        assert text.encode("ascii", "strict")


class TestNoSideEffects:
    """A refused line leaves nothing behind."""

    def test_a_cluster_that_fails_binds_none_of_its_letters(self) -> None:
        """The walk raises instead of returning, so the -v never lands."""
        error = _refused(["-vx", "lock"])

        assert error.message == "unrecognized option '-x' in '-vx'"
        assert _parse(["lock"]).options["verbose"] == 0

    def test_a_refused_line_touches_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        with pytest.raises(UsageError):
            _parse(["lock", "--nope"])

        assert list(tmp_path.iterdir()) == []


class TestEagerOptions:
    """What short-circuits a line, and what it says about it."""

    @pytest.mark.parametrize(
        ("rule", "argv", "eager", "command"),
        [
            ("both-help-spellings-print-the-page", ["--help"], "help", ""),
            ("both-help-spellings-print-the-page", ["-h"], "help", ""),
            (
                "innermost-command-owns-the-help-page",
                ["lock", "--help"],
                "help",
                "lock",
            ),
            ("version-answers-at-any-level", ["--version"], "version", ""),
            ("version-answers-at-any-level", ["-V"], "version", ""),
            (
                "version-answers-at-any-level",
                ["lock", "--version"],
                "version",
                "lock",
            ),
            (
                "help-ignores-what-comes-after",
                ["lock", "--project-resolution", "bogus", "--help"],
                "help",
                "lock",
            ),
            (
                "values-convert-after-the-line-parses",
                ["lock", "--max-concurrency", "x", "--help"],
                "help",
                "lock",
            ),
            (
                "nested-help-describes-its-own-level",
                ["cache", "--help"],
                "help",
                "cache",
            ),
        ],
    )
    def test_an_eager_option_ends_the_line_where_it_was_given(
        self, rule: str, argv: list[str], eager: str, command: str
    ) -> None:
        parsed = _parse(argv)

        assert (parsed.eager, parsed.command) == (eager, command), rule

    def test_a_command_level_version_works_rather_than_erroring(self) -> None:
        """A command level answers ``--version`` too, rather than refusing it."""
        parsed = _parse(["lock", "--version"])

        assert parsed.eager == "version"
        assert parsed.prog == "nab lock"

    def test_an_eager_option_inside_a_cluster_ends_the_line(self) -> None:
        assert _parse(["-vh", "lock"]).eager == "help"

    def test_an_eager_option_refuses_an_attached_value(self) -> None:
        """A valueless option refuses an attached value, so ``nab --version=3`` stays exit 2."""
        assert _refused(["--version=3"]).message == (
            "option '--version' does not take a value"
        )

    def test_an_eager_letter_does_not_excuse_the_rest_of_its_cluster(self) -> None:
        assert _refused(["-hx"]).message == "unrecognized option '-x' in '-hx'"

    def test_the_first_eager_of_a_cluster_is_the_one_honoured(self) -> None:
        """The same rule as two eager tokens: ``--help --version`` is help."""
        assert _parse(["-hV"]).eager == "help"
        assert _parse(["-Vh"]).eager == "version"
        assert _parse(["--help", "--version"]).eager == "help"


class TestOrderAndRepetition:
    """The order options arrive in does not change the answer."""

    def test_two_orderings_of_the_same_flags_agree(self) -> None:
        one = _parse(["--color", "never", "-v", "lock"])
        other = _parse(["-v", "--color", "never", "lock"])

        assert one.options == other.options
        assert one.values == other.values

    def test_options_and_operands_interleave(self) -> None:
        parsed = _parse(["lock", "a", "--upgrade", "b", "--output", "x", "c"])

        assert parsed.values["paths"] == ("a", "b", "c")
        assert parsed.values["output"] == "x"


class TestWhatTheConstructionRulesBuy:
    """What the construction rules buy, stated as the behaviour each protects."""

    def test_the_numeric_carve_out_belongs_to_the_row(self) -> None:
        """Declaring an option that reads as a number changes no other option."""
        root = (*_ROOT, _option("--1", "one", "count", short="1", default=0))

        parsed = parse(("lock", "--max-concurrency", "-1"), root, _COMMANDS, "nab")

        assert parsed.values["max_concurrency"] == -1

    def test_a_short_name_is_one_letter_so_a_cluster_is_letters(self) -> None:
        assert _parse(["-vq", "lock"]).options["verbose"] == 1

    def test_the_name_ends_at_the_first_equals(self) -> None:
        assert _parse(["lock", "--output=a=b"]).values["output"] == "a=b"

    def test_a_command_named_like_an_option_is_never_reached(self) -> None:
        commands = {**_COMMANDS, "-x": ()}

        with pytest.raises(UsageError) as caught:
            parse(("-x",), _ROOT, commands, "nab")

        assert caught.value.message == "unrecognized option '-x' in '-x'"

    def test_a_short_name_sets_the_positive_spelling_alone(self) -> None:
        """A negation has no short form, so ``-o`` cannot reach one."""
        parsed = _parse(["lock", "--no-upgrade", "-o", "x"])

        assert parsed.values["upgrade"] is False
        assert parsed.values["output"] == "x"


class TestTheShippedTable:
    """The cases that need nab's own rows rather than the fixture's."""

    def test_the_literal_none_is_still_an_unset_value(self) -> None:
        """Deliberate exclusion: a cache directory named None stays unreachable."""
        parsed = _shipped(["cache", "dir", "--cache-dir", "None"])

        assert parsed.values["cache_dir"] is None

    def test_an_underscored_flag_reaches_its_dashed_row(self) -> None:
        parsed = _shipped(["lock", "--project_resolution", "lowest"])

        assert parsed.values["project_resolution"] == "lowest"

    def test_a_misspelled_underscored_flag_suggests_the_dashed_row(self) -> None:
        with pytest.raises(UsageError) as caught:
            _shipped(["lock", "--projct_resolution", "lowest"])

        assert "'--project-resolution'" in diagnose(caught.value)

    def test_a_half_typed_negation_is_offered_the_negation_first(self) -> None:
        """A ``no-`` typo admits the negations, or the answer is the opposite."""
        with pytest.raises(UsageError) as caught:
            _shipped(["lock", "--no-cach"])

        assert "did you mean one of '--no-cache', '--cache'?" in diagnose(caught.value)

    def test_a_positive_typo_is_never_offered_the_negation_of_what_it_meant(
        self,
    ) -> None:
        """The filter does this, not the cap: only one spelling resembles it."""
        with pytest.raises(UsageError) as caught:
            _shipped(["lock", "--upgrad"])

        assert "did you mean '--upgrade'?" in diagnose(caught.value)

    def test_a_half_typed_option_is_offered_two_spellings_at_most(self) -> None:
        with pytest.raises(UsageError) as caught:
            _shipped(["lock", "--cach"])

        text = diagnose(caught.value)
        assert "did you mean one of '--cache-dir', '--cache'?" in text

    def test_the_star_options_take_every_word_up_to_the_next_option(self) -> None:
        parsed = _shipped(["lock", "--groups", "dev", "docs", "--upgrade"])

        assert parsed.values["groups"] == ("dev", "docs")
        assert parsed.values["upgrade"] is True

    def test_an_attached_star_value_still_swallows_the_words_after_it(self) -> None:
        """Both spellings take the same words, so a line cannot change meaning."""
        attached = _shipped(["lock", "--groups=dev", "docs"])

        assert attached.values["groups"] == ("dev", "docs")
        assert attached.values["path"] == "pyproject.toml"
        assert attached.values == _shipped(["lock", "--groups", "dev", "docs"]).values

    def test_an_attached_star_value_alone_is_one_word(self) -> None:
        assert _shipped(["lock", "--groups=dev"]).values["groups"] == ("dev",)

    def test_every_command_binds_its_own_defaults(self) -> None:
        parsed = _shipped(["download"])

        assert parsed.values["output"] == "wheels"
        assert parsed.values["path"] == "pyproject.toml"


def test_a_built_table_holds_the_spellings_and_the_operands_apart() -> None:
    table = build(_LOCK)

    assert table.options["--output"] is table.options["-o"]
    assert [row.dest for row in table.operands] == ["paths"]
    assert len(table.rows) == len(_LOCK)


_DISPATCH = {
    "cache": ("nab._cache_cmd", "cache_command", "Inspect and clear nab's cache."),
    "config": ("nab._config_cmd", "config_command", "Inspect the configuration."),
    "lock": ("nab._lock", "lock", "Resolve dependencies and emit a lockfile."),
    "download": ("nab._download", "download", "Resolve and download every wheel."),
}

# A page reads the summary alone, so a fixture needs no module or function.
_DISPATCH_PROBE = {"probe": ("", "", "a table written by a test")}

# The widest spelling nab declares, at 87 characters with its five choices.
_WIDEST_SPELLING = (
    "  --project-dist-policy "
    "{wheel-only,prefer-wheel,wheel-or-sdist,sdist-only,sdist-install}"
)


class TestHelpPages:
    """What a page holds, and that each level gets its own."""

    def test_the_root_page_lists_every_command_with_its_summary(self) -> None:
        text = page("", _ROOT, _COMMANDS, _HELP, _DISPATCH, "nab", 80)

        assert text.startswith("Usage: nab [options] <command> [arguments]")
        assert "  cache" in text
        assert "Inspect and clear nab's cache." in text
        assert "Try 'nab <command> --help' for more information." in text

    def test_a_command_page_differs_from_the_root_page(self) -> None:
        root = page("", _ROOT, _COMMANDS, _HELP, _DISPATCH, "nab", 80)
        command = page("cache", _ROOT, _COMMANDS, _HELP, _DISPATCH, "nab", 80)

        assert root != command
        assert command.startswith("Usage: nab cache [options] <action>")

    def test_a_command_page_lists_the_global_flags_under_their_own_heading(
        self,
    ) -> None:
        text = page("cache", _ROOT, _COMMANDS, _HELP, _DISPATCH, "nab", 80)

        assert "Global options:" in text
        assert "-v, --verbose" in text

    def test_a_command_with_no_options_of_its_own_shows_no_options_heading(
        self,
    ) -> None:
        text = page("download", _ROOT, _COMMANDS, _HELP, _DISPATCH, "nab", 80)

        assert "Arguments:" in text
        assert "Options:" not in text.replace("Global options:", "")

    def test_a_negation_is_folded_into_the_row_it_negates(self) -> None:
        text = page("lock", _ROOT, _COMMANDS, _HELP, _DISPATCH, "nab", 80)

        assert "--cache / --no-cache" in text
        assert "\n  --no-cache" not in text

    def test_a_value_option_names_what_it_takes(self) -> None:
        text = page("lock", _ROOT, _COMMANDS, _HELP, _DISPATCH, "nab", 80)

        assert "--output PATH" in text
        assert "--max-concurrency INT" in text
        assert "--groups STR ..." in text
        assert "--color {auto,always,never}" in text
        assert "--offline [{True,False}] / --no-offline" in text

    def test_an_operand_names_the_verbs_it_takes(self) -> None:
        text = page("cache", _ROOT, _COMMANDS, _HELP, _DISPATCH, "nab", 80)

        assert "action {dir,verify,clear}" in text

    @pytest.mark.parametrize("width", [12, 40, 60, 80, 100])
    def test_no_page_cuts_a_word_in_half(self, width: int) -> None:
        """The defect two candidate designs shipped: a fixed column mid-word.

        Wrapping moves words between lines and never changes which words
        a page holds, so a page laid out at any width holds the words of
        one laid out where nothing has to wrap at all.
        """
        for command in ("", *spec.COMMANDS):
            unwrapped = sorted(_shipped_page(command, 200).split())

            assert sorted(_shipped_page(command, width).split()) == unwrapped, command

    @pytest.mark.parametrize("width", [100, 120])
    def test_a_page_stays_inside_a_width_every_spelling_fits(self, width: int) -> None:
        """Nothing on these pages is too wide to break, so nothing may overrun."""
        for command in ("", *spec.COMMANDS):
            lines = _shipped_page(command, width).splitlines()

            assert max(len(line) for line in lines) <= width, command

    def test_a_spelling_wider_than_half_the_page_takes_its_own_line(self) -> None:
        """The row the rule exists for: five choices, 87 characters.

        At 100 columns the spelling fits the page and still may not set
        the column, which is what half rather than all of the width buys.
        """
        assert _WIDEST_SPELLING in _shipped_page("lock", 100).splitlines()

    def test_the_spelling_that_sets_the_column_keeps_its_help_beside_it(self) -> None:
        """The widest spelling inside the limit sits on its help's line."""
        commands = {
            "probe": (_option("--wide-option-name", "wide"), _option("--x", "x"))
        }
        text = page("probe", (), commands, _HELP, _DISPATCH_PROBE, "nab", 80)

        entries = [line for line in text.splitlines() if line.startswith("  --")]

        assert len(entries) == 2
        assert all(line.endswith("what it does") for line in entries)

    def test_the_shipped_pages_render_for_every_command(self) -> None:
        for command in ("", *spec.COMMANDS):
            text = page(
                command, spec.ROOT, spec.COMMANDS, spec.HELP, spec.DISPATCH, "nab", 80
            )
            assert text.endswith("\n")

    def test_a_usage_line_marks_a_required_slot_and_an_optional_one(self) -> None:
        required = page(
            "cache", spec.ROOT, spec.COMMANDS, spec.HELP, spec.DISPATCH, "nab", 80
        )
        optional = page(
            "lock", spec.ROOT, spec.COMMANDS, spec.HELP, spec.DISPATCH, "nab", 80
        )
        repeatable = page("lock", _ROOT, _COMMANDS, _HELP, _DISPATCH, "nab", 80)

        assert required.startswith("Usage: nab cache [options] <action>")
        assert optional.startswith("Usage: nab lock [options] [path]")
        assert repeatable.startswith("Usage: nab lock [options] [paths ...]")


class TestPaintedPages:
    """What a page carries when the caller asks for colour, and when it does not."""

    def test_a_plain_page_carries_no_escape_at_all(self) -> None:
        for command in ("", *spec.COMMANDS):
            assert "\033" not in _shipped_page(command, 100), command

    def test_painting_changes_nothing_but_the_escapes(self) -> None:
        """The layout is measured on plain text, so stripping gives it back.

        An escape occupies no cell, so a column measured over a painted
        spelling would push every help line right by the nine characters
        the colour and the reset take.
        """
        for command in ("", *spec.COMMANDS):
            for width in (40, 80, 120):
                painted = _painted_page(command, width)

                assert _stripped(painted) == _shipped_page(command, width), command

    def test_the_word_usage_and_the_headings_are_bold_with_no_hue(self) -> None:
        text = _painted_page("lock", 100)

        assert text.startswith("\033[1mUsage:\033[0m nab lock")
        assert "\033[1mOptions:\033[0m" in text
        assert "\033[1mGlobal options:\033[0m" in text

    def test_a_command_name_and_an_option_spelling_are_cyan(self) -> None:
        root = _painted_page("", 100)
        command = _painted_page("lock", 100)

        assert "  \033[36mcache\033[0m" in root
        assert "\033[36m--output\033[0m PATH" in command
        assert "\033[36m--cache\033[0m / \033[36m--no-cache\033[0m" in command

    def test_a_metavar_and_an_operand_stay_the_terminals_own_colour(self) -> None:
        text = _painted_page("cache", 100)

        assert "  action {dir,verify,clear}" in text
        assert "\033[36m--color\033[0m {auto,always,never}" in text

    def test_help_text_and_the_trailer_are_never_painted(self) -> None:
        root = _painted_page("", 100)

        assert "Inspect and clear nab's on-disk cache." in root
        assert "\nTry 'nab <command> --help' for more information.\n" in root


class TestPaintedRefusals:
    """What a refusal carries when the caller asks for colour."""

    def test_a_plain_refusal_carries_no_escape_at_all(self) -> None:
        assert "\033" not in diagnose(_refused(["lock", "--outupt"]))

    def test_the_leading_token_is_red_up_to_its_colon(self) -> None:
        text = diagnose(_refused(["lock", "--nope"]), color=True)

        assert text.startswith("\033[31mnab lock:\033[0m unrecognized option")

    def test_a_suggested_spelling_is_cyan_inside_plain_quotes(self) -> None:
        text = diagnose(_refused(["lock", "--outupt"]), color=True)

        assert "did you mean '\033[36m--output\033[0m'?" in text

    def test_the_trailer_is_never_painted(self) -> None:
        text = diagnose(_refused(["lock", "--nope"]), color=True)

        assert text.endswith("\nTry 'nab lock --help' for more information.\n")

    def test_painting_changes_nothing_but_the_escapes(self) -> None:
        error = _refused(["lock", "--outupt"])

        assert _stripped(diagnose(error, color=True)) == diagnose(error)


class TestTheWrapper:
    """The greedy wrapper and the width it wraps to."""

    def test_a_line_breaks_between_words_and_never_inside_one(self) -> None:
        lines = wrap("one two three four five", 10)

        assert lines == ["one two", "three four", "five"]

    def test_a_word_longer_than_the_width_takes_its_own_line(self) -> None:
        """Wherever it falls: a line already started, and a line it starts."""
        assert wrap("a supercalifragilistic b", 8) == ["a", "supercalifragilistic", "b"]
        assert wrap("supercalifragilistic b", 8) == ["supercalifragilistic", "b"]

    def test_empty_text_is_one_empty_line(self) -> None:
        assert wrap("", 20) == [""]

    def test_a_narrow_page_still_renders_one_word_per_line(self) -> None:
        text = page("cache", _ROOT, _COMMANDS, _HELP, _DISPATCH, "nab", 12)

        assert "what it does" in text.replace("\n  ", " ")

    def test_columns_sets_the_width(self) -> None:
        assert terminal_width({"COLUMNS": "132"}) == 132

    def test_a_columns_of_zero_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "nab._cli.render.os.get_terminal_size",
            lambda: types.SimpleNamespace(columns=99),
        )

        assert terminal_width({"COLUMNS": "0"}) == 99

    def test_a_columns_that_is_not_a_number_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "nab._cli.render.os.get_terminal_size",
            lambda: types.SimpleNamespace(columns=99),
        )

        assert terminal_width({"COLUMNS": "wide"}) == 99

    def test_no_terminal_falls_back_to_eighty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse() -> None:
            raise OSError

        monkeypatch.setattr("nab._cli.render.os.get_terminal_size", refuse)

        assert terminal_width({}) == 80

    def test_the_process_environment_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLUMNS", "77")

        assert terminal_width() == 77


class TestSuggestions:
    """The suggestion pass, on the names the message would offer."""

    def test_a_prefix_match_comes_before_a_close_one(self) -> None:
        assert suggest("--index", ("--index-url", "--offline", "--index-strategy")) == (
            "--index-url",
            "--index-strategy",
        )

    def test_a_stripped_name_keeps_unrelated_spellings_out(self) -> None:
        assert suggest("--nope", ("--no-progress", "--offline")) == ()

    def test_the_list_is_capped_at_two(self) -> None:
        named = suggest("--cach", ("--cache", "--cache-dir", "--cache-key"))

        assert len(named) == 2

    def test_a_command_name_is_suggested_without_dashes(self) -> None:
        assert suggest("lok", ("cache", "config", "lock", "download")) == ("lock",)


class TestDispatch:
    """What dispatch returns, including the two errors it catches."""

    @pytest.fixture
    def command(self, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
        """A module standing in for a command, registered where import finds it."""
        module = types.ModuleType("nab_probe_command")
        module.calls = []  # type: ignore[attr-defined]

        def run(**values: object) -> None:
            module.calls.append(values)  # type: ignore[attr-defined]

        module.run = run  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "nab_probe_command", module)
        return module

    def _parsed(self, values: dict[str, object]) -> Parsed:
        """A line whose root options are the dests the shipped table declares.

        Reading them off a real parse rather than off literals is what
        makes a renamed root row fail here instead of at run time.
        """
        options = _shipped(["cache", "dir"]).options
        return Parsed("probe", values, options, "nab probe")

    _TABLE: ClassVar = {"probe": ("nab_probe_command", "run", "a probe")}
    _PATHS: ClassVar = {"probe": ("path", "output")}

    def test_a_command_runs_with_its_paths_coerced(
        self, command: types.ModuleType
    ) -> None:
        parsed = self._parsed({"path": "pyproject.toml", "output": None, "cache": True})

        assert dispatch(parsed, self._TABLE, self._PATHS) == (0, "")
        assert command.calls == [
            {"path": Path("pyproject.toml"), "output": None, "cache": True}
        ]

    def test_a_command_with_no_paths_to_coerce_still_runs(
        self, command: types.ModuleType
    ) -> None:
        parsed = self._parsed({"path": "x", "output": "y"})

        assert dispatch(parsed, self._TABLE, {}) == (0, "")
        assert command.calls == [{"path": "x", "output": "y"}]

    def test_a_command_that_exits_reports_its_status(
        self, command: types.ModuleType
    ) -> None:
        def run(**_values: object) -> None:
            raise SystemExit(3)

        command.run = run  # type: ignore[attr-defined]

        assert dispatch(self._parsed({}), self._TABLE, {}) == (3, "")

    def test_a_command_that_exits_with_a_message_reports_it(
        self, command: types.ModuleType
    ) -> None:
        def run(**_values: object) -> None:
            raise SystemExit("nothing to lock")

        command.run = run  # type: ignore[attr-defined]

        assert dispatch(self._parsed({}), self._TABLE, {}) == (1, "nothing to lock")

    def test_a_command_that_exits_with_no_status_succeeded(
        self, command: types.ModuleType
    ) -> None:
        def run(**_values: object) -> None:
            raise SystemExit

        command.run = run  # type: ignore[attr-defined]

        assert dispatch(self._parsed({}), self._TABLE, {}) == (0, "")

    def test_the_resume_runs_between_the_import_and_the_command(
        self, command: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``resume`` marks the end of startup: the module is in, nothing has run."""
        events: list[str] = []
        real_import = builtins.__import__

        def record_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "nab_probe_command":
                events.append("import")
            return real_import(name, *args, **kwargs)

        def run(**_values: object) -> None:
            events.append("command")

        command.run = run  # type: ignore[attr-defined]
        monkeypatch.setattr(builtins, "__import__", record_import)

        dispatch(
            self._parsed({}), self._TABLE, {}, resume=lambda: events.append("resume")
        )

        assert events == ["import", "resume", "command"]

    def test_a_line_the_printer_refuses_never_resumes(
        self, command: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing was imported, so the caller is left as it was."""
        monkeypatch.setenv("NAB_VERBOSITY", "loud")
        resumed: list[int] = []

        status, _message = dispatch(
            self._parsed({}), self._TABLE, {}, resume=lambda: resumed.append(1)
        )

        assert status == 2
        assert resumed == []

    def test_a_malformed_verbosity_is_a_usage_error_and_no_command_runs(
        self, command: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NAB_VERBOSITY", "loud")

        status, message = dispatch(self._parsed({}), self._TABLE, {})

        assert status == 2
        assert message.startswith("error: NAB_VERBOSITY=")
        assert command.calls == []


_TABLE_RULES = frozenset(case[0] for case in _PARSES + _DISPATCHES + _REFUSALS)

# --- the process level: the same lines, driven through run() ---

# The three refusal cases nab's own table cannot drive.  It declares no
# short option that takes a value, and ``--max-concurrency`` belongs to
# ``download`` rather than to ``lock``, so the last two are re-driven
# where the flag really is.
_FIXTURE_ONLY = (
    ["lock", "-o", "--verbose"],
    ["lock", "--max-concurrency", "x"],
    ["lock", "--max-concurrency", "-3.5"],
)

_REFUSED_LINES: tuple[tuple[str, list[str], str], ...] = (
    *(case for case in _REFUSALS if case[1] not in _FIXTURE_ONLY),
    (
        "numeric-option-refuses-a-non-number",
        ["download", "--max-concurrency", "x"],
        "expected an integer",
    ),
    (
        "numeric-option-refuses-a-non-number",
        ["download", "--max-concurrency", "-3.5"],
        "expected an integer",
    ),
)


@pytest.mark.parametrize(
    ("rule", "argv", "expected"), _REFUSED_LINES, ids=_ids(_REFUSED_LINES)
)
def test_a_refused_line_writes_to_stderr_alone_and_exits_two(
    rule: str, argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """One status and one stream, for every refusal there is."""
    status = run(tuple(argv))

    captured = capsys.readouterr()
    assert status == 2, rule
    assert captured.out == "", rule
    assert expected in captured.err, rule
    assert captured.err.endswith("for more information.\n"), rule


def test_a_page_goes_to_stdout_and_leaves_stderr_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """the-page-goes-only-to-stdout: help is the requested output, so it is not a diagnostic."""
    status = run(("--help",))

    captured = capsys.readouterr()
    assert status == 0
    assert captured.out.startswith("Usage: nab ")
    assert captured.err == ""


def test_a_version_line_goes_to_stdout_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = run(("--version",))

    captured = capsys.readouterr()
    assert status == 0
    assert captured.out.startswith("nab ")
    assert captured.err == ""


def test_a_message_dispatch_returns_leaves_through_the_one_stderr_write(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of refusals-go-only-to-stderr: a command refused before it ran still writes once.

    ``dispatch`` hands the text back rather than writing it, so this is
    what proves the text reaches a stream at all, newline included.
    """
    monkeypatch.setenv("NAB_VERBOSITY", "loud")

    status = run(("cache", "dir"))

    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert captured.err == (
        "error: NAB_VERBOSITY='loud' is not one of "
        "silent, quiet, normal, verbose, debug\n"
    )


class _RefusingStream(io.StringIO):
    """A stream that refuses every write, the way a full disk does."""

    def write(self, text: str, /) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.parametrize(
    ("line", "stream"), [(("--version",), "stdout"), (("--nope",), "stderr")]
)
def test_a_refused_write_replaces_the_status_it_would_have_returned(
    line: tuple[str, ...], stream: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule at the level ``run`` decides it, rather than at exit."""
    monkeypatch.setattr(sys, stream, _RefusingStream())

    assert run(line) == 120


# The console entry, which is the shape that owns its process: it flushes
# both streams itself and leaves through ``os._exit``, so a stream that
# refused the write never reaches the interpreter's own flush at shutdown.
_CONSOLE = "from nab._entry import console_entry; console_entry()"

_FULL = Path("/dev/full")


@pytest.mark.skipif(
    not _FULL.exists(), reason="needs a stream that refuses every write"
)
@pytest.mark.parametrize(
    ("line", "stream"),
    [
        (["--help"], "stdout"),
        (["--version"], "stdout"),
        (["--nope"], "stderr"),
    ],
    ids=["help", "version", "usage-error"],
)
def test_output_that_cannot_be_written_exits_120_without_a_traceback(
    line: list[str], stream: str, tmp_path: Path
) -> None:
    """help-survives-a-closed-stdout: a full disk replaces the status the run would have returned.

    Both stdout pages fit the 8,192-byte buffer, so the write lands and
    the flush at the end of ``console_entry`` is what fails; stderr is
    unbuffered, so the usage error is the case whose write raises.
    """
    kept = tmp_path / "kept.txt"
    with _FULL.open("w") as full, kept.open("w") as elsewhere:
        finished = subprocess.run(  # noqa: S603 - the probe is this file's own source
            [sys.executable, "-c", _CONSOLE, *line],
            stdout=full if stream == "stdout" else elsewhere,
            stderr=full if stream == "stderr" else subprocess.PIPE,
            check=False,
        )

    assert finished.returncode == 120
    if stream == "stdout":
        assert finished.stderr == b""


# The rules whose case is a test of its own rather than a row of a table.
_CASE_RULES = frozenset(
    {
        "failed-cluster-binds-no-letters",
        "short-name-is-one-alphanumeric-character",
        "name-ends-at-the-first-equals",
        "numeric-carve-out-is-per-option",
        "values-convert-after-the-line-parses",
        "the-word-none-is-an-ordinary-value",
        "negation-has-no-short-form",
        "option-order-does-not-matter",
        "command-names-never-start-with-dash",
        "both-help-spellings-print-the-page",
        "innermost-command-owns-the-help-page",
        "version-answers-at-any-level",
        "help-ignores-what-comes-after",
        "nested-help-describes-its-own-level",
        "refusals-name-the-command-path",
        "a-refusal-is-three-lines-at-most",
        "suggestions-get-their-own-line",
        "a-refusal-has-no-side-effects",
        "refusals-escape-undecodable-tokens",
        "refusals-go-only-to-stderr",
        "one-exit-code-per-outcome",
        "the-page-goes-only-to-stdout",
        "help-survives-a-closed-stdout",
    }
)

# No command declares pass-through operands, so there is nothing to drive
# and no code to reach.
_DROPPED = frozenset({"pass-through-operands-after-dash-dash"})

# The ten parser sections of the matrix, in its order, plus the one
# operand rule.  Its construction section is absent: those rules have no
# line to drive, and are pinned where the declaration is built.
_ALL_RULES = frozenset(
    {
        "dash-then-more-is-an-option",
        "dash-dash-plus-name-is-a-long-option",
        "one-dash-starts-a-short-cluster",
        "option-names-are-case-sensitive",
        "consumed-value-is-never-an-option",
        "nothing-after-dash-dash-is-inspected",
        "dash-is-the-only-option-prefix",
        "unclaimed-token-becomes-an-operand",
        "first-dash-dash-ends-options-and-is-dropped",
        "second-dash-dash-is-an-operand",
        "dash-dash-can-be-an-option-value",
        "dash-dash-before-a-command-still-dispatches",
        "a-trailing-dash-dash-adds-no-operands",
        "no-nested-command-after-dash-dash",
        "cluster-equals-separate-options",
        "repeated-letter-counts-each-time",
        "value-letter-takes-rest-of-cluster",
        "separated-short-value-is-next-token",
        "attached-short-value-keeps-equals",
        "unknown-letter-names-letter-and-cluster",
        "failed-cluster-binds-no-letters",
        "valueless-letter-takes-no-attached-value",
        "lone-dash-is-always-an-operand",
        "short-name-is-one-alphanumeric-character",
        "long-name-takes-the-next-token",
        "value-is-all-after-the-first-equals",
        "bare-equals-gives-an-empty-value",
        "long-names-are-never-abbreviated",
        "valueless-option-refuses-attached-value",
        "long-token-never-retried-as-cluster",
        "dash-dash-with-equals-is-unknown",
        "name-ends-at-the-first-equals",
        "value-option-at-end-of-line-is-refused",
        "any-string-can-be-a-value",
        "value-taken-by-position-not-syntax",
        "option-shaped-token-is-not-a-value",
        "numeric-carve-out-is-per-option",
        "choice-value-must-be-in-the-set",
        "numeric-option-refuses-a-non-number",
        "values-convert-after-the-line-parses",
        "the-word-none-is-an-ordinary-value",
        "unset-boolean-is-not-false",
        "no-prefix-spelling-sets-the-boolean-false",
        "absent-boolean-defers-to-config",
        "only-a-tri-state-boolean-takes-a-value",
        "last-boolean-spelling-wins",
        "double-negation-is-an-unknown-option",
        "a-defaulted-boolean-reads-as-given",
        "negation-has-no-short-form",
        "repeatable-option-accumulates-in-order",
        "repeated-value-option-keeps-the-last",
        "option-order-does-not-matter",
        "operand-order-is-preserved",
        "options-and-operands-interleave-freely",
        "pass-through-operands-after-dash-dash",
        "first-operand-names-the-command",
        "global-option-works-before-command",
        "global-option-works-after-command",
        "global-option-on-both-sides-combines",
        "innermost-options-searched-first",
        "command-option-too-early-is-unknown",
        "unknown-command-refused-with-suggestions",
        "missing-command-lists-the-commands",
        "command-name-must-match-exactly",
        "option-value-takes-the-command-word",
        "nested-commands-follow-the-same-rules",
        "command-names-never-start-with-dash",
        "both-help-spellings-print-the-page",
        "innermost-command-owns-the-help-page",
        "version-answers-at-any-level",
        "help-ignores-what-comes-after",
        "an-earlier-refusal-beats-help",
        "the-page-goes-only-to-stdout",
        "help-survives-a-closed-stdout",
        "nested-help-describes-its-own-level",
        "refusals-go-only-to-stderr",
        "refusals-name-the-command-path",
        "a-refusal-is-three-lines-at-most",
        "first-refusal-stops-the-parse",
        "one-exit-code-per-outcome",
        "each-refusal-has-one-exact-wording",
        "suggestions-get-their-own-line",
        "a-refusal-has-no-side-effects",
        "refusals-escape-undecodable-tokens",
        "required-operand-left-empty-is-refused",
    }
)


def test_every_rule_is_pinned_or_dropped_on_purpose() -> None:
    """No rule of the matrix goes missing without saying which it is."""
    accounted = _TABLE_RULES | _CASE_RULES | _DROPPED

    assert not _ALL_RULES - accounted
    assert not accounted - _ALL_RULES
    assert not _TABLE_RULES & _DROPPED
