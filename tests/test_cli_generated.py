"""The generated files, against the declaration they were generated from.

``src/nab/_cli/spec.py``, ``src/nab/config/registry.py`` and the
global-flag block of ``docs/reference/cli.md`` are written by
``tasks/gen_cli.py`` and committed, because the sdist ships ``src/nab``
while ``tasks/`` and ``docs/`` stay out of it: an installed nab has no
generator to run and no page to check.

``--check`` alone only proves that a file agrees with the generator, so
it passes on a generator that maps the declaration wrongly.  The cases
below restate the mapping and compare the shipped tables against
:mod:`nab.optiondefs` directly, which is what makes a wrong literal in
``gen_cli.py`` visible.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from nab import optiondefs
from nab._cli import parse as parse_module
from nab._cli import spec
from nab._cli.parse import Row, build
from nab.config import hooks, registry, values
from nab.optiontable import ALL

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

    from nab.optiondefs import Opt

_ROOT = Path(__file__).resolve().parents[1]
_GENERATOR = _ROOT / "tasks" / "gen_cli.py"
_SPEC = _ROOT / "src" / "nab" / "_cli" / "spec.py"
_REFERENCE = _ROOT / "docs" / "reference" / "cli.md"

# The kinds that store a constant rather than reading a value, restated
# here so the comparison does not borrow the generator's own answer.
_CONST_KINDS = frozenset(
    {
        optiondefs.Kind.FLAG,
        optiondefs.Kind.TRI,
        optiondefs.Kind.COUNT,
        optiondefs.Kind.EAGER,
    }
)

# What a module of literals is made of.  Anything else is a statement that
# could go one way or the other, and would need a test to reach both.
_LITERAL_NODES = frozenset(
    {
        ast.AnnAssign,
        ast.Constant,
        ast.Dict,
        ast.Expr,
        ast.Load,
        ast.Module,
        ast.Name,
        ast.Store,
        ast.Subscript,
        ast.Tuple,
    }
)

# The ten fields the walk reads off one row.
_ROW_FIELDS = 10


def test_the_generated_files_are_current() -> None:
    """A declaration edit that never reached the generator fails here."""
    finished = subprocess.run(  # noqa: S603 - the interpreter running this suite
        [sys.executable, str(_GENERATOR), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert finished.returncode == 0, (
        f"{finished.stdout}{finished.stderr}\n"
        f"run: {sys.executable} tasks/gen_cli.py --write"
    )


def test_the_generated_module_is_literals_alone() -> None:
    """No def, no if, no comprehension, no import, so importing it covers it."""
    tree = ast.parse(_SPEC.read_text(encoding="utf-8"))

    found = {type(node) for node in ast.walk(tree)}

    assert found <= _LITERAL_NODES, sorted(
        node.__name__ for node in found - _LITERAL_NODES
    )


def test_the_reference_page_lists_every_global_flag() -> None:
    """The block restates the seven root rows, and nothing else."""
    block = _REFERENCE.read_text(encoding="utf-8").partition("<!-- generated")[2]
    rows = block.partition("<!-- /generated")[0].splitlines()[3:]

    listed = [row.split("|")[1].strip() for row in rows if row.startswith("|")]
    helps = [row.split("|")[2].strip() for row in rows if row.startswith("|")]
    declared = [row for row in ALL if row.is_global]

    assert listed == [
        f"`-{row.short}`, `{row.cli_flag}`" if row.short else f"`{row.cli_flag}`"
        for row in declared
    ]
    assert helps == [row.help for row in declared]


def test_the_generator_refuses_a_page_that_lost_its_markers(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page edited past its markers is a refusal, not a silent no-op."""
    page = tmp_path / "cli.md"
    page.write_text("# CLI\n", encoding="utf-8")
    monkeypatch.setattr(generator, "_REFERENCE", page)

    with pytest.raises(SystemExit) as caught:
        generator._reference_text()

    assert "markers" in str(caught.value)


def test_the_generator_refuses_to_run_with_neither_flag() -> None:
    finished = subprocess.run(  # noqa: S603 - the interpreter running this suite
        [sys.executable, str(_GENERATOR)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert finished.returncode != 0
    assert "--write" in finished.stderr


@pytest.mark.parametrize("command", list(spec.COMMANDS))
def test_every_row_carries_the_fields_the_walk_reads(command: str) -> None:
    for row in spec.COMMANDS[command]:
        assert len(row) == _ROW_FIELDS, row
        assert 0 <= row[9] < len(spec.HELP), row


def test_every_root_row_carries_the_fields_the_walk_reads() -> None:
    for row in spec.ROOT:
        assert len(row) == _ROW_FIELDS, row
        assert 0 <= row[9] < len(spec.HELP), row


def test_every_dispatch_entry_names_a_function_that_exists() -> None:
    for name, (module_name, function, summary) in spec.DISPATCH.items():
        module = __import__(module_name, fromlist=(function,))

        assert callable(getattr(module, function)), name
        assert summary == (getattr(module, function).__doc__ or "").splitlines()[0]


def test_the_commands_are_declared_in_one_order() -> None:
    assert list(spec.DISPATCH) == list(spec.COMMANDS) == list(spec.PATH_DESTS)


def test_every_coerced_dest_is_a_path_the_command_takes() -> None:
    for command, dests in spec.PATH_DESTS.items():
        table = build(spec.COMMANDS[command])
        rows = {row.dest: row for row in table.rows}

        for dest in dests:
            assert rows[dest].vtype == "path", (command, dest)


def _probe(
    *,
    kind: optiondefs.Kind | None = optiondefs.Kind.FLAG,
    docs: str = "reference/cli.md",
) -> optiondefs.Opt:
    """A declared row that exists only to drive one of the generator's refusals."""
    return optiondefs.Opt(
        "probe",
        scope=optiondefs.Scope.PROJECT,
        kind=kind,
        commands=("lock",),
        default=False,
        parse=values.parse_bool,
        render=hooks.render_bool,
        help="a row written by a test",
        docs=docs,
    )


@pytest.fixture
def generator() -> ModuleType:
    """``tasks/gen_cli.py`` as a module: it is outside the package and the gate."""
    loaded = importlib.util.spec_from_file_location("gen_cli", _GENERATOR)
    assert loaded is not None
    assert loaded.loader is not None

    module = importlib.util.module_from_spec(loaded)
    loaded.loader.exec_module(module)
    return module


def _declared(command: str) -> list[optiondefs.Opt]:
    """The rows ``command`` carries, in declaration order; the root ones when empty."""
    if not command:
        return [row for row in ALL if row.is_global]
    return [row for row in ALL if not row.is_global and command in row.commands]


def _generated(command: str) -> list[Row]:
    """The rows the shipped table carries for ``command``, in the same order."""
    if not command:
        return list(build(spec.ROOT, root=True).rows)
    return list(build(spec.COMMANDS[command]).rows)


def _assert_row_says_what_the_option_says(row: Row, option: optiondefs.Opt) -> None:
    """One generated row against the declared option it was written from."""
    assert row.long == (option.cli_flag or ""), option.name
    assert row.short == option.short, option.name
    assert option.kind is not None, option.name
    assert row.kind == option.kind.value, option.name
    assert row.dest == option.dest, option.name
    assert row.choices == option.choices, option.name
    assert row.required == option.required, option.name
    assert spec.HELP[row.help_index] == option.help, option.name

    wanted = None if option.default is optiondefs.UNSET else option.default
    assert row.default == wanted, option.name

    stored = True if option.kind in _CONST_KINDS else None
    assert row.const is stored, option.name

    if option.vtype is None:
        assert row.vtype == "", option.name
        assert not row.nullable, option.name
    else:
        assert row.vtype == option.vtype.value, option.name
        assert row.nullable == option.nullable, option.name


def _assert_negation_says_what_the_option_says(
    row: Row, option: optiondefs.Opt
) -> None:
    """The generated ``--no-X`` row: same dest and help, inverted constant."""
    assert option.cli_flag is not None, option.name
    assert row.long == "--no-" + option.cli_flag[2:], option.name
    assert row.kind == "neg", option.name
    assert row.dest == option.dest, option.name
    assert row.const is False, option.name
    assert spec.HELP[row.help_index] == option.help, option.name


@pytest.mark.parametrize("command", ["", *spec.COMMANDS])
def test_the_shipped_table_says_what_the_declaration_says(command: str) -> None:
    """A generator that maps a field wrongly fails here, where --check cannot."""
    rows = _generated(command)

    for option in _declared(command):
        _assert_row_says_what_the_option_says(rows.pop(0), option)
        if option.negatable:
            _assert_negation_says_what_the_option_says(rows.pop(0), option)

    assert rows == []


def _same_rows(shipped: Sequence[Opt], declared: Sequence[Opt]) -> None:
    """Compare a generated tuple field by field against the rows behind it."""
    for row, option in zip(shipped, declared, strict=True):
        for field in optiondefs.Opt.__slots__:
            assert getattr(row, field) == getattr(option, field), (option.name, field)


def test_the_shipped_registry_says_what_the_declaration_says() -> None:
    """A generator that spells a field wrongly fails here, where --check cannot."""
    _same_rows(registry.OPTIONS, [option for option in ALL if option.key is not None])


def test_the_shipped_sub_rows_say_what_the_declaration_says() -> None:
    """The rows under a table key are generated too, and pinned the same way."""
    _same_rows(registry.SUB_ROWS, [option for option in ALL if option.under])


def test_every_path_a_command_takes_is_listed_for_coercion() -> None:
    """The other direction: a Path parameter left off the list is handed a str."""
    for command, (module_name, function, _summary) in spec.DISPATCH.items():
        signature = inspect.signature(
            getattr(__import__(module_name, fromlist=(function,)), function)
        )
        annotated = {
            name
            for name, parameter in signature.parameters.items()
            if "Path" in str(parameter.annotation)
        }

        assert annotated == set(spec.PATH_DESTS[command]), command


def test_the_generator_refuses_a_row_that_names_no_page(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page check, which no run of --check on a current tree can reach."""
    monkeypatch.setattr(generator, "ALL", (_probe(docs="reference/nope.md"),))

    with pytest.raises(SystemExit) as caught:
        generator._check_pages()

    assert str(caught.value) == (
        "probe names docs/reference/nope.md, which is not a documentation page"
    )


def test_the_generator_refuses_a_rung_zero_it_cannot_spell(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rung 0 with no source spelling is a refusal naming the row."""
    unspellable = optiondefs.Opt(
        "probe",
        scope=optiondefs.Scope.PROJECT,
        rdefault=object(),
        parse=values.parse_bool,
        render=hooks.render_bool,
        help="a row written by a test",
        docs="reference/cli.md",
    )
    monkeypatch.setattr(generator, "ALL", (unspellable,))

    with pytest.raises(SystemExit) as caught:
        generator._registry_text()

    assert str(caught.value).startswith("probe holds <object object at ")
    assert str(caught.value).endswith("which the generator cannot spell")


def test_the_generator_refuses_a_row_with_no_kind(generator: ModuleType) -> None:
    """A row that names a command but declares no kind has no surface to write."""
    with pytest.raises(SystemExit) as caught:
        generator._table_lines([_probe(kind=None)], [], "    ")

    assert "has no kind" in str(caught.value)


def test_the_generator_writes_the_row_type_the_walk_reads(
    generator: ModuleType,
) -> None:
    """The two hand-written spellings of the ten-field row, held in step."""
    assert str(parse_module.Spec) == generator._ROW_TYPE
