"""The declaration and the code that reads it say the same thing.

``nab/optiontable.py`` names every flag and ``nab/_lock.py`` and its three
siblings name every parameter, and nothing in the language ties the two
together.  These cases do, along with four that hold a whole table: the
verbs a command body offers, the ``--project-*`` spellings a committed
lockfile carries, the keys the live registry carries beside them, and the
seven root rows, whose reader is the generated parser rather than any
command signature.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest

from nab._cli import spec as cli_spec
from nab._cli.parse import UsageError, parse
from nab.optiondefs import COMMANDS, UNSET, Kind, Opt, Scope, VType
from nab.optiontable import ALL
from nab_project.config_sources import OPTIONS, SourceKind

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
    """A verb row's choices are the verbs its command body offers.

    Neither verb set is annotated with a ``Literal``, because an unknown
    verb exits 1 from the body rather than raising a type error.  Driving
    the body with a verb it does not know makes it name the ones it does.
    """

    @pytest.mark.parametrize("command", ["config", "cache"])
    def test_the_refusal_names_exactly_the_declared_verbs(
        self,
        command: str,
        hermetic_roots: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        row = next(row for row in _rows(command) if row.kind is Kind.VERB)
        offered = "expected one of " + ", ".join(repr(verb) for verb in row.choices)

        with pytest.raises(SystemExit):
            self._refuse(command, hermetic_roots)

        assert capsys.readouterr().err.rstrip().endswith(offered)

    @staticmethod
    def _refuse(command: str, project: Path) -> None:
        """Call ``command`` with a verb no body knows."""
        arguments: dict[str, Any] = {"cache_dir": project / "cache"}

        if command == "config":
            pyproject = project / "pyproject.toml"
            pyproject.write_text(_MINIMAL_PROJECT, encoding="utf-8")
            arguments["path"] = pyproject

        _command_function(command)("frobnicate", **arguments)


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


class TestParallelRegistry:
    """The declaration and the live registry describe the same 29 keys.

    A key the declaration gains and the registry lacks parses, binds, and
    is then read by nobody.
    """

    def test_the_keys_are_equal_and_in_the_same_order(self) -> None:
        """Order counts: it is the order ``nab config list`` prints."""
        assert [row.key for row in ALL if row.key] == [spec.key for spec in OPTIONS]

    @pytest.mark.parametrize("spec", OPTIONS, ids=lambda spec: spec.key)
    def test_every_derived_spelling_matches_the_registry(self, spec: Any) -> None:
        row = next(row for row in ALL if row.key == spec.key)

        assert row.cli_flag == spec.cli_flag
        assert row.cli_param == spec.cli_param
        assert row.env_var == spec.env_var
        assert row.scope.value == spec.scope.value

    @pytest.mark.parametrize("spec", OPTIONS, ids=lambda spec: spec.key)
    def test_every_rung_zero_matches_the_registry(self, spec: Any) -> None:
        """``rdefault`` is the value row 0 of the ladder falls back to.

        The type is compared too, because ``()`` equals ``[]`` and
        ``False`` equals ``0``.
        """
        row = next(row for row in ALL if row.key == spec.key)

        assert row.rdefault == spec.default
        assert type(row.rdefault) is type(spec.default)

    @pytest.mark.parametrize("spec", OPTIONS, ids=lambda spec: spec.key)
    def test_every_category_gate_matches_the_registry(self, spec: Any) -> None:
        """The gate matches a source by value, so a renamed one shows here."""
        row = next(row for row in ALL if row.key == spec.key)

        for kind in SourceKind:
            assert row.allowed_in_toml(kind) == spec.allowed_in_toml(kind), kind

    @pytest.mark.parametrize("spec", OPTIONS, ids=lambda spec: spec.key)
    def test_every_type_label_matches_the_registry(self, spec: Any) -> None:
        """The deprecated marker is a field here and part of the string there."""
        row = next(row for row in ALL if row.key == spec.key)
        label = row.type_label + (" [deprecated]" if row.deprecated else "")

        assert label == spec.type_label
