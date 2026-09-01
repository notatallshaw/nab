"""The declaration and the code that reads it say the same thing.

``nab/optiontable.py`` names every flag and ``nab/_lock.py`` and its three
siblings name every parameter, and nothing in the language ties the two
together.  These cases do, along with four that hold a whole table: the
verbs a command body offers, the ``--project-*`` spellings a committed
lockfile carries, the parse hook every configuration key's flag hands its
walked value to, and the seven root rows, whose reader is the generated
parser rather than any command signature.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest

from nab._cli import spec as cli_spec
from nab._cli.parse import UsageError, parse
from nab.cli import run
from nab.config.ladder import build_cli_layer
from nab.optiondefs import COMMANDS, UNSET, Kind, Opt, Scope, VType
from nab.optiontable import ALL

_TESTS = Path(__file__).resolve().parent
_FLAG_SPELLINGS = _TESTS / "data" / "lockfile_flag_spellings.txt"

_NAMES = [name for name, _module, _function in COMMANDS]

_MINIMAL_PROJECT = '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'

_ROOT_ROWS = [row for row in ALL if row.is_global]


def _command_function(command: str) -> Any:
    """The function ``command`` dispatches to, found the way the CLI finds it."""
    for name, module, function in COMMANDS:
        if name == command:
            return getattr(importlib.import_module(module), function)
    raise AssertionError(command)


def _rows(command: str) -> list[Opt]:
    """The rows ``command`` carries, in declaration order."""
    return [row for row in ALL if command in row.commands]


def _parameters(command: str) -> dict[str, inspect.Parameter]:
    """The parameters ``command``'s function takes."""
    return dict(inspect.signature(_command_function(command)).parameters)


@pytest.mark.parametrize("command", _NAMES)
class TestSignatureConformance:
    """Case by case, one command's declaration against its signature."""

    def test_the_keyword_parameters_are_exactly_the_flag_rows(
        self, command: str
    ) -> None:
        declared = {row.dest for row in _rows(command) if not row.is_positional}
        taken = {
            name
            for name, parameter in _parameters(command).items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        }

        assert declared == taken

    def test_the_operands_are_the_positional_rows_in_declaration_order(
        self, command: str
    ) -> None:
        declared = [row.dest for row in _rows(command) if row.is_positional]
        taken = [
            name
            for name, parameter in _parameters(command).items()
            if parameter.kind is not inspect.Parameter.KEYWORD_ONLY
        ]

        assert declared == taken

    def test_every_default_is_the_one_the_signature_carries(self, command: str) -> None:
        by_dest = {row.dest: row for row in _rows(command)}

        for name, parameter in _parameters(command).items():
            row = by_dest.get(name)
            assert row is not None, f"no row declares {name}"

            if parameter.default is inspect.Parameter.empty:
                assert row.required, name
                assert row.default is UNSET, name
                continue

            assert not row.required, name
            expected = parameter.default
            if row.vtype is VType.PATH and expected is not None:
                expected = str(expected)
            assert row.default == expected, name

    def test_every_nullable_row_is_the_one_the_signature_leaves_optional(
        self, command: str
    ) -> None:
        """``nullable`` decides whether the literal string None is accepted."""
        hints = get_type_hints(_command_function(command))

        for row in _rows(command):
            optional = type(None) in get_args(hints[row.dest])
            assert row.nullable is optional, row.dest

    def test_every_choice_row_is_annotated_with_its_own_choices(
        self, command: str
    ) -> None:
        hints = get_type_hints(_command_function(command))

        for row in _rows(command):
            if row.vtype is not VType.CHOICE:
                continue
            annotated = hints[row.dest]
            optional = [arg for arg in get_args(annotated) if arg is not type(None)]
            literal = optional[0] if len(optional) == 1 else annotated

            assert get_args(literal) == row.choices, row.dest


class TestRootRowsAgainstTheirReader:
    """The seven root rows against the parser that reads them.

    A root flag is read on either side of the command name and lands in
    ``Parsed.options``.  The two eager rows end the line where they stand,
    so a command never runs and the entry point answers them instead.
    """

    @staticmethod
    def _options(*written: str) -> dict[str, object]:
        """The root options a line carries, read through the shipped tables."""
        argv = (*written, "cache", "dir")
        return parse(argv, cli_spec.ROOT, cli_spec.COMMANDS, "nab").options

    def test_writing_a_root_flag_reaches_the_reader(self) -> None:
        bare = self._options()

        for row in _ROOT_ROWS:
            if row.kind is Kind.EAGER:
                continue

            written = [row.cli_flag]
            if row.vtype is VType.CHOICE:
                written.append(row.choices[0])

            assert self._options(*written)[row.dest] != bare[row.dest], row.name
            if row.short:
                assert self._options(f"-{row.short}")[row.dest] != bare[row.dest]

    def test_the_eager_rows_end_the_line_where_they_stand(self) -> None:
        """``--version`` and ``--help`` are answered before a command runs."""
        eager = [row for row in _ROOT_ROWS if row.kind is Kind.EAGER]

        assert [row.name for row in eager] == ["version", "help"]
        for row in eager:
            parsed = parse((row.cli_flag,), cli_spec.ROOT, cli_spec.COMMANDS, "nab")
            assert parsed.eager == row.name

    def test_the_declared_defaults_are_what_an_empty_command_line_means(self) -> None:
        """A default is what the reader receives when the flag is absent."""
        declared = {
            row.dest: (None if row.default is UNSET else row.default)
            for row in _ROOT_ROWS
        }

        assert self._options() == declared

    def test_the_colour_row_offers_its_reader_tokens_and_no_none(self) -> None:
        """``nullable`` would mean the literal token ``None`` is accepted."""
        row = next(row for row in _ROOT_ROWS if row.vtype is VType.CHOICE)

        for token in row.choices:
            assert self._options("--color", token)[row.dest] == token

        with pytest.raises(UsageError):
            self._options("--color", "None")

        assert not row.nullable


class TestVerbSets:
    """A verb row's declared choices are the verbs the parser will accept.

    The set is written once, as the ``Literal`` the row is annotated with,
    and the parser refuses anything outside it.  Driving a real command
    line with a verb no row declares makes the refusal list the set.
    """

    @pytest.mark.parametrize("command", ["config", "cache"])
    def test_the_refusal_names_exactly_the_declared_verbs(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        row = next(row for row in _rows(command) if row.kind is Kind.VERB)
        offered = "choose from " + ", ".join(row.choices)

        assert run((command, "frobnicate", "--cache-dir", str(tmp_path))) == 2

        assert offered in capsys.readouterr().err


class TestLockfileFlagStability:
    """The spellings a committed lockfile carries do not move quietly."""

    def test_the_project_flags_are_the_recorded_spellings(self) -> None:
        declared = [
            row.cli_flag
            for row in ALL
            if row.scope is Scope.PROJECT and row.cli_flag is not None
        ]
        recorded = _FLAG_SPELLINGS.read_text(encoding="utf-8").split()

        assert declared == recorded

    def test_a_repeatable_project_flag_is_singular(self) -> None:
        """The two spellings a plural key would otherwise have derived."""
        by_key = {row.key: row for row in ALL if row.key}

        assert by_key["constraints"].cli_flag == "--project-constraint"
        assert by_key["default-groups"].cli_flag == "--project-default-group"
        assert by_key["constraints"].kind is Kind.APPEND


# The sixteen rows a configuration source and a flag both reach, so every
# one of them has a spelling to drive and a hook to hand the result to.
_SETTABLE = [row for row in ALL if row.key and row.cli_flag]

_BOOL_TOKENS = ("True", "False")


def _tokens(row: Opt) -> tuple[str, ...]:
    """Every token ``row``'s flag accepts, or the one token it documents."""
    if row.vtype is VType.CHOICE:
        return row.choices
    if row.vtype is VType.BOOL:
        return _BOOL_TOKENS
    return (row.sample,)


def _walked(row: Opt, token: str) -> object:
    """What the walk stores for ``row`` when its flag is given ``token``."""
    line = (row.commands[0], row.cli_flag, token)
    return parse(line, cli_spec.ROOT, cli_spec.COMMANDS, "nab").values[row.dest]


class TestHooksReadWhatTheWalkStores:
    """The sixteen flagged keys reach their hook in a shape it takes.

    The token goes through the real walk and the real CLI layer, so a row
    whose flag converts to one type while its hook expects another fails
    here.  ``build-requires-depth`` is the one that would: its hook takes
    the integer the walk built and refuses the raw token.
    """

    @pytest.mark.parametrize("row", _SETTABLE, ids=lambda row: row.name)
    def test_every_token_the_flag_accepts_survives_the_hook(self, row: Opt) -> None:
        for token in _tokens(row):
            value = _walked(row, token)

            build_cli_layer({row.name: value})

    def test_the_settable_keys_are_the_sixteen_flagged_rows(self) -> None:
        """A derivation that emptied would leave the case above with no rows."""
        assert len(_SETTABLE) == 16
