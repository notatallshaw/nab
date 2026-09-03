"""The declaration's own refusals, proved on tables planted to break them.

The type parameter carries what a checker can police, and the cases below
are what it cannot: a ``Literal`` widened past the enum it was copied from,
a ladder starting outside its own choice set, one name bound twice in a
table body, a value row written without its subscript, and a type parameter
no token converts to.  Each raises as the declaration is lowered, naming the
row that broke.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Literal, NewType

import pytest

from nab.optiondefs import GLOBAL, Kind, Scope, Tokens, VType
from nab.optionlower import lower, table_rows
from nab.optionrows import (
    Count,
    Eager,
    Item,
    Items,
    Key,
    Layer,
    Many,
    Operand,
    Pairs,
    Row,
    Star,
    Switch,
    Table,
    Tri,
    Value,
    Verb,
    rows,
)
from nab.optiontypes import enum_label, shape, type_argument


def _parse(value: object, _where: str) -> object:
    """Read a source's value; no case here looks at what a hook returns."""
    return value


def _render(value: object) -> str:
    """Print a winning value; the printing half of the pair above."""
    return str(value)


def _layer(*, rdefault: object, **fields: str) -> Layer:
    """A ladder half carrying the hooks every keyed row has to declare."""
    return Layer(rdefault=rdefault, parse=_parse, render=_render, **fields)


class Strategy(enum.Enum):
    """Three tokens, as a shipped policy enum spells them."""

    HIGHEST = "highest"
    LOWEST = "lowest"
    DIRECT = "lowest-direct"


StrategyFlag = Literal["highest", "lowest", "lowest-direct"]

Requirement = NewType("Requirement", str)


def _matrix_fixture() -> type[Table]:
    """A table whose five rows spell one key each of ``matrix``."""

    class Fixture(
        Table,
        on=("lock",),
        scope=Scope.PROJECT,
        docs="explanation/universal.md",
        under="matrix",
        needs=("python", "platforms"),
    ):
        """One row of each token grammar, under one table key."""

        python = Value[str | None](help="the python range")
        platforms = Items[str](opened_by="id", help="the platforms to model")
        platform = Item[str](opened_by="id", help="the one machine to model")
        implementations = Star[str](help="the interpreters to model")
        python_patches = Pairs[str](help="pin a minor to one patch release")

    return Fixture


def _only(table: type[Table]) -> Row:
    """The one row a single-row fixture table declares."""
    (row,) = rows(table)
    return row


def _refused(table: type[Table]) -> str:
    """The message lowering ``table`` raises."""
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the message is the check
        table_rows(table)
    return str(caught.value)


class TestWhatTheTableFillsIn:
    """A table's keywords reach its rows, and a key takes no command set."""

    def test_a_row_takes_the_table_command_set_scope_and_page(self) -> None:
        class Fixture(Table, on=GLOBAL, scope=Scope.USER, docs="reference/cli.md"):
            """One row that writes none of the three."""

            offline = Tri(key=_layer(rdefault=False), env=True, help="stay offline")

        row = _only(Fixture)
        assert (row.on, row.scope, row.docs) == (GLOBAL, Scope.USER, "reference/cli.md")

    def test_a_row_keeps_the_command_set_and_page_it_writes(self) -> None:
        class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
            """One row that overrides both."""

            output = Value[Path](
                on=("lock",), docs="reference/formats.md", help="where"
            )

        row = _only(Fixture)
        assert (row.on, row.docs) == (("lock",), "reference/formats.md")

    def test_a_key_with_no_command_line_takes_no_command_set(self) -> None:
        class Fixture(Table, on=GLOBAL, scope=Scope.PROJECT, docs="reference/cli.md"):
            """A file-only key inside a table that names commands."""

            matrix = Key(_layer(rdefault=None, label="table(python)"), help="the axes")

        assert _only(Fixture).on == ()


class TestWhatTheTypeParameterSays:
    """The vtype, choice set, nullability and printed label, all derived."""

    def test_a_scalar_gives_its_own_vtype_and_label(self) -> None:
        read = shape(Path)
        assert (read.vtype, read.choices, read.label, read.nullable) == (
            "path",
            (),
            "path",
            False,
        )

    def test_a_new_type_prints_the_name_it_was_given(self) -> None:
        assert shape(Requirement).label == "requirement"

    def test_an_optional_is_nullable_and_reads_its_inner_type(self) -> None:
        read = shape(int | None)
        assert (read.vtype, read.nullable) == ("int", True)

    def test_a_literal_and_its_enum_give_the_same_tokens(self) -> None:
        assert shape(StrategyFlag).choices == shape(Strategy).choices

    def test_a_row_with_no_value_has_no_type_parameter(self) -> None:
        assert type_argument(Count(help="how loud")) is None

    def test_the_printed_label_joins_the_tokens(self) -> None:
        assert enum_label(("dir", "verify")) == "enum(dir|verify)"


class TestWhatEachKindLowersTo:
    """The kind, and the default a row that writes none is given."""

    def test_every_kind_lowers_to_the_kind_the_parser_reads(self) -> None:
        class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
            """One row of each kind that takes no configuration key."""

            verbose = Count(help="louder")
            cache = Switch(default=True, negatable=True, help="use the cache")
            version = Eager(short="V", help="print the version and exit")
            python = Value[str](help="the interpreter")
            groups = Star[str](help="the groups")
            path = Operand[Path](default=Path("pyproject.toml"), help="the project")
            action = Verb[Literal["dir", "verify"]](help="what to do")

        lowered = {row.name: row for row in table_rows(Fixture)}
        assert lowered["verbose"].kind is Kind.COUNT
        assert lowered["version"].kind is Kind.EAGER
        assert lowered["action"].kind is Kind.VERB
        assert lowered["action"].vtype is VType.STR
        assert lowered["path"].default == "pyproject.toml"

    def test_a_flag_the_parser_may_leave_absent_hands_its_command_none(self) -> None:
        class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
            """A value row that writes no default."""

            python = Value[str | None](help="the interpreter")

        assert table_rows(Fixture)[0].default is None

    def test_a_repeatable_row_prints_its_label_as_a_list(self) -> None:
        class Fixture(Table, on=GLOBAL, scope=Scope.PROJECT, docs="reference/cli.md"):
            """A repeatable layered row, whose label wraps its inner type."""

            constraints = Many[Requirement](
                key=_layer(rdefault=(), sample="attrs<24"), help="bound a package"
            )

        assert table_rows(Fixture)[0].type_label == "list(requirement)"


class TestWhatIsRefusedAsTheTableIsBuilt:
    """The four faults the type parameter cannot police on its own."""

    def test_a_literal_widened_past_its_enum_names_both_token_sets(self) -> None:
        class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
            """A choice row whose alias has gained a token the enum lacks."""

            resolution = Value[Literal["highest", "lowest", "fastest"]](
                mirrors=Strategy, help="which version to prefer"
            )

        message = _refused(Fixture)
        assert "resolution lists ('highest', 'lowest', 'fastest')" in message
        assert "Strategy holds ('highest', 'lowest', 'lowest-direct')" in message

    def test_a_rung_zero_outside_the_choice_set_names_the_tokens_offered(self) -> None:
        class Fixture(Table, on=GLOBAL, scope=Scope.PROJECT, docs="reference/cli.md"):
            """A layered choice row starting on a token its flag will not take."""

            resolution = Value[StrategyFlag](
                key=_layer(rdefault="arrival"), help="which version to prefer"
            )

        assert "resolution starts at 'arrival'" in _refused(Fixture)

    def test_a_layered_choice_row_may_start_unset(self) -> None:
        class Fixture(Table, on=GLOBAL, scope=Scope.PROJECT, docs="reference/cli.md"):
            """A layered choice row whose rung 0 is no token at all."""

            resolution = Value[StrategyFlag | None](
                key=_layer(rdefault=None), help="which version to prefer"
            )

        assert table_rows(Fixture)[0].rdefault is None

    def test_one_name_declared_twice_is_refused_as_the_body_binds_it(self) -> None:
        with pytest.raises(ValueError, match="locked is declared twice in one table"):

            class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
                """Two rows under one name, where the second would win."""

                locked = Switch(default=False, help="check the lock is current")
                locked = Switch(default=True, help="the row that would have won")  # noqa: PIE794 - the point of the case

    def test_a_row_rebound_to_a_plain_value_is_refused_as_well(self) -> None:
        """The direction that would otherwise drop the row and raise nothing."""
        with pytest.raises(ValueError, match="locked is declared twice in one table"):

            class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
                """A row a later binding replaces with something that is not one."""

                locked = Switch(default=False, help="check the lock is current")
                locked = True  # noqa: PIE794 - the point of the case

    def test_a_row_landing_on_a_name_already_bound_is_refused(self) -> None:
        with pytest.raises(ValueError, match="width is declared twice in one table"):

            class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
                """A plain attribute a row then lands on."""

                width = 88
                width = Count(help="how wide the report runs")  # noqa: PIE794 - the point of the case

    def test_a_value_row_written_without_its_subscript_is_refused(self) -> None:
        class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
            """A value row whose type parameter was left off."""

            output = Value(help="where to write the lock")

        assert "output reads a value, so it needs a vtype" in _refused(Fixture)

    def test_a_type_no_token_converts_to_names_the_row(self) -> None:
        class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
            """A value row holding a scalar the parser has no reader for."""

            ratio = Value[float](help="the share of the whole to keep")

        assert _refused(Fixture) == (
            "ratio holds float, and a token converts to str, int, bool or Path"
        )

    def test_a_union_of_two_types_names_the_row_rather_than_taking_the_first(
        self,
    ) -> None:
        class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
            """A value row offering a choice of types, which no flag reads."""

            depth = Value[int | str | None](help="how deep the walk may go")

        assert _refused(Fixture) == (
            "depth holds int | str | None, and a row reads one type, or one and None"
        )


class TestTheRowsHelper:
    """What ``rows`` reads out of a table body."""

    def test_only_the_rows_are_returned_and_in_declaration_order(self) -> None:
        class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
            """Two rows either side of a plain class attribute."""

            first = Count(help="one")
            width = 88
            second = Count(help="two")

        assert [row.name for row in rows(Fixture)] == ["first", "second"]
        assert Fixture.width == 88

    def test_a_row_names_itself_in_a_refusal(self) -> None:
        class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
            """One row, read back through its repr."""

            no_progress = Count(help="quieter")

        assert repr(_only(Fixture)) == "Count('no-progress')"


def test_lowering_one_row_needs_no_table_of_its_own() -> None:
    """``lower`` is the unit ``table_rows`` runs over."""

    class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
        """One row lowered on its own."""

        verbose = Count(short="v", help="louder")

    assert lower(_only(Fixture)).cli_flag == "--verbose"


@pytest.mark.parametrize(
    ("name", "tokens"),
    [
        ("python", Tokens.SCALAR),
        ("platforms", Tokens.ITEMS),
        ("platform", Tokens.ITEM),
        ("implementations", Tokens.LIST),
        ("python-patches", Tokens.PAIRS),
    ],
)
def test_the_row_class_says_how_its_tokens_read(name: str, tokens: Tokens) -> None:
    """Each row's class is what says how the assembler reads its tokens."""
    lowered = {row.name: row for row in table_rows(_matrix_fixture())}

    assert lowered[name].tokens is tokens
    assert lowered[name].under == "matrix"


def test_the_table_says_which_of_its_rows_a_command_line_has_to_give() -> None:
    lowered = {row.name: row for row in table_rows(_matrix_fixture())}

    assert lowered["platforms"].opened_by == "id"
    assert [name for name, row in lowered.items() if row.needed] == [
        "python",
        "platforms",
    ]


def test_a_row_under_no_key_reads_its_tokens_as_nothing() -> None:
    """``tokens`` is the assembler's field, so a row it never sees has none."""

    class Fixture(Table, on=GLOBAL, docs="reference/cli.md"):
        """A star row naming a key of its own."""

        groups = Star[str](help="the groups")

    assert lower(_only(Fixture)).tokens is None


def test_a_table_refuses_needs_without_under() -> None:
    """``needs`` names the keys of a table, so there has to be one."""
    with pytest.raises(ValueError, match="Fixture declares needs without under"):

        class Fixture(
            Table,
            on=("lock",),
            scope=Scope.PROJECT,
            docs="reference/cli.md",
            needs=("python",),
        ):
            """A table naming a needed key without an ``under``."""

            python = Value[str | None](help="the python range")


def test_a_table_refuses_needs_it_does_not_declare() -> None:
    """``needs`` takes the hyphenated name, which is the one the row carries."""
    with pytest.raises(
        ValueError, match="Fixture needs 'python_order', which it does not declare"
    ):

        class Fixture(
            Table,
            on=("lock",),
            scope=Scope.PROJECT,
            docs="reference/cli.md",
            under="matrix",
            needs=("python_order",),
        ):
            """A table naming a needed key with underscores, not the hyphenated name."""

            python_order = Value[str | None](help="the direction the axis aligns")
