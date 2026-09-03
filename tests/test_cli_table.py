"""The option table's own rules, proved on tables planted to break them.

Every rule runs against a fixture table rather than against
:data:`nab.optiontable.ALL`, so a case is a few local rows and never a
mutation of a shipped module.  The declared table is checked once, at the
top, for the shape the rest of the CLI derives from it.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any

import pytest
import tomli

from nab import optiondefs
from nab.config import hooks, values
from nab.config.ladder import SourceKind, docs_path, docs_url
from nab.optiondefs import COMMANDS, GLOBAL, UNSET, Kind, Opt, Scope, VType
from nab.optionrows import rows
from nab.optiontable import ALL, TABLES
from nab_provider import policy

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# The declared rows, paired with the ``Opt`` each lowers to: ``table_rows``
# lowers them in this order, so the two lists index each other.
_DECLARED = [row for table in TABLES for row in rows(table)]

# The bool-valued kinds, which are the ones a ``--no-`` spelling negates.
_BOOL_KINDS = frozenset({Kind.FLAG, Kind.TRI})

_LOCK_ONLY = (("lock", "nab._lock", "lock"),)

# Every row's page, in declaration order.  A page is written on the row or
# on its table and nothing derives it, so it is written out here too: point
# a row at another page that exists and no other check moves.
_PAGES = (
    ("verbose", "reference/cli.md"),
    ("quiet", "reference/cli.md"),
    ("color", "reference/cli.md"),
    ("no-color", "reference/cli.md"),
    ("no-progress", "reference/cli.md"),
    ("version", "reference/cli.md"),
    ("help", "reference/cli.md"),
    ("resolution", "reference/configuration.md"),
    ("decision-order", "reference/configuration.md"),
    ("mode", "explanation/universal.md"),
    ("constraints", "reference/configuration.md"),
    ("default-groups", "reference/selection.md"),
    ("base-group", "reference/selection.md"),
    ("build-group", "reference/selection.md"),
    ("requires-python", "reference/configuration.md"),
    ("uploaded-prior-to", "reference/configuration.md"),
    ("dist-policy", "reference/configuration.md"),
    ("build-policy", "reference/build-policy.md"),
    ("build-requires-depth", "reference/build-policy.md"),
    ("environment", "reference/configuration.md"),
    ("marker-environment", "reference/configuration.md"),
    ("vcs", "how-to/vcs.md"),
    ("workspace", "how-to/workspaces.md"),
    ("indexes", "how-to/multi-index.md"),
    ("local-sources", "how-to/local-sources.md"),
    ("vcs-sources", "how-to/vcs.md"),
    ("archive-sources", "reference/configuration.md"),
    ("packages", "reference/configuration.md"),
    ("package-rules", "reference/configuration.md"),
    ("index", "how-to/multi-index.md"),
    ("conflicts", "explanation/conflicts.md"),
    ("matrix", "explanation/universal.md"),
    ("python", "explanation/universal.md"),
    ("platforms", "explanation/universal.md"),
    ("implementations", "explanation/universal.md"),
    ("python-order", "explanation/universal.md"),
    ("python-patches", "explanation/universal.md"),
    ("python", "reference/configuration.md"),
    ("platform", "reference/configuration.md"),
    ("implementation", "reference/configuration.md"),
    ("offline", "reference/cli.md"),
    ("cache-dir", "reference/cache.md"),
    ("http-backend", "reference/cli.md"),
    ("max-concurrency", "reference/cli.md"),
    ("path", "reference/cli.md"),
    ("action", "reference/cli.md"),
    ("key", "reference/cli.md"),
    ("action", "reference/cache.md"),
    ("path", "reference/cli.md"),
    ("output", "reference/formats.md"),
    ("output", "reference/cli.md"),
    ("format", "reference/formats.md"),
    ("cache", "reference/cache.md"),
    ("python", "reference/cli.md"),
    ("groups", "reference/selection.md"),
    ("all-groups", "reference/selection.md"),
    ("extras", "reference/selection.md"),
    ("all-extras", "reference/selection.md"),
    ("workspace-discovery", "how-to/workspaces.md"),
    ("build-requirements", "reference/selection.md"),
    ("no-emit-workspace", "how-to/workspaces.md"),
    ("upgrade", "reference/cli.md"),
    ("locked", "reference/cli.md"),
    ("include-rejected", "reference/cli.md"),
)

# The type labels a row writes rather than lets its type spell.  The rest
# are derived, and the derivation is pinned with the choices it reads.
_WRITTEN_LABELS = {
    "uploaded-prior-to": "datetime|PnD",
    "environment": "table(python,platform[,knobs],implementation)",
    "marker-environment": "table(marker-var=str)",
    "vcs": "table(vcs-policy)",
    "workspace": "table(members)",
    "indexes": "array-of-tables(name,url,serialization)",
    "local-sources": "array-of-tables(name,path)",
    "vcs-sources": "array-of-tables(name,url)",
    "archive-sources": "array-of-tables(name,url)",
    "packages": "table(package-override)",
    "package-rules": "array-of-tables(match,policy)",
    "index": "table(index-override)",
    "conflicts": "array-of-tables(members,policy)",
    "matrix": "table(python,platforms)",
    "http-backend": "enum(httpx|urllib3)",
}


def _row(name: str, **fields: Any) -> Opt:
    """A row that breaks no rule, with ``fields`` replacing what a case breaks."""
    declared: dict[str, Any] = {
        "kind": Kind.FLAG,
        "commands": ("lock",),
        "default": False,
        "parse": values.parse_bool,
        "render": hooks.render_bool,
        "help": "what it does",
        "docs": "reference/cli.md",
    }
    return Opt(name, **{**declared, **fields})


def _sub_row(name: str = "python", **fields: Any) -> Opt:
    """A row that spells one key of ``matrix`` and carries none of its own."""
    declared: dict[str, Any] = {
        "scope": Scope.PROJECT,
        "under": "matrix",
        "kind": Kind.VALUE,
        "vtype": VType.STR,
        "default": None,
        "parse": None,
        "render": None,
    }
    return _row(name, **{**declared, **fields})


def _refused(rows: tuple[Opt, ...], commands: Any = _LOCK_ONLY) -> str:
    """The message ``validate`` raises on ``rows``."""
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the message is the check
        optiondefs.validate(rows, commands)
    return str(caught.value)


def _construction_error(name: str, **fields: Any) -> str:
    """The message building one row raises."""
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the message is the check
        _row(name, **fields)
    return str(caught.value)


class TestTheDeclaredTable:
    """What the rest of the CLI counts on ``ALL`` for."""

    def test_the_table_passes_its_own_rules(self) -> None:
        optiondefs.validate(ALL)

    def test_the_table_is_the_three_groups_it_is_built_from(self) -> None:
        """64 rows: 7 at the root, 44 on a command, 13 in a file alone."""
        root = [row for row in ALL if row.is_global]
        command = [row for row in ALL if row.commands and not row.is_global]
        file_only = [row for row in ALL if not row.commands]

        assert (len(root), len(command), len(file_only)) == (7, 44, 13)
        assert len(root) + len(command) + len(file_only) == len(ALL) == 64

        keyed = [row for row in ALL if row.key]
        assert len(keyed) == 29
        assert sum(1 for row in keyed if row.cli_flag) == 16
        assert sum(1 for row in keyed if not row.commands) == 13

    def test_the_sub_rows_are_the_eight_that_spell_the_two_tables(self) -> None:
        """A sub-row carries no key, so only ``under`` says which one it spells."""
        assert [(row.name, row.under) for row in ALL if row.under] == [
            ("python", "matrix"),
            ("platforms", "matrix"),
            ("implementations", "matrix"),
            ("python-order", "matrix"),
            ("python-patches", "matrix"),
            ("python", "environment"),
            ("platform", "environment"),
            ("implementation", "environment"),
        ]

    def test_every_command_is_declared_once_in_help_order(self) -> None:
        assert [name for name, _module, _function in COMMANDS] == [
            "cache",
            "config",
            "lock",
            "download",
        ]

    def test_the_layered_rows_split_by_scope_as_the_registry_does(self) -> None:
        keyed = [row for row in ALL if row.key]

        assert sum(row.scope is Scope.PROJECT for row in keyed) == 25
        assert sum(row.scope is Scope.USER for row in keyed) == 4

    def test_the_environment_names_are_the_four_user_keys(self) -> None:
        assert [row.env_var for row in ALL if row.env_var] == [
            "NAB_OFFLINE",
            "NAB_CACHE_DIR",
            "NAB_HTTP_BACKEND",
            "NAB_MAX_CONCURRENCY",
        ]

    def test_a_row_declares_a_page_and_a_line_of_help(self) -> None:
        assert all(row.help for row in ALL)
        assert all(row.docs.endswith(".md") for row in ALL)
        assert not any("#" in row.docs for row in ALL)

    def test_every_row_keeps_the_page_it_was_written_with(self) -> None:
        assert tuple((row.name, row.docs) for row in ALL) == _PAGES

    def test_a_row_names_its_page_on_the_published_site(self) -> None:
        """What ``explain`` prints is the checked path, published.

        The site is ``[project.urls].Documentation`` and the path under it
        is the one ``tasks/gen_cli.py`` resolves against the checkout, so
        a reader's page and the checked page cannot differ.  Sphinx builds
        with ``-b html``, and a row names a page rather than a section, so
        no URL carries a fragment.
        """
        urls = tomli.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["urls"]
        resolution = next(row for row in ALL if row.name == "resolution")

        assert docs_url(resolution) == (
            "https://nab.readthedocs.io/en/stable/reference/configuration.html"
        )

        for row in ALL:
            url = docs_url(row)

            assert url.startswith(urls["Documentation"]), row.name
            assert url.endswith(f"/{row.docs.removesuffix('.md')}.html"), row.name
            assert "#" not in url, row.name
            assert docs_path(row) == f"docs/{row.docs}", row.name

    def test_the_written_type_labels_are_the_ones_the_rows_carry(self) -> None:
        """The 15 labels a row spells out, which nothing else reads back.

        A derived label is pinned through the choices it is built from;
        a written one is read by nothing else, so it is written out here.
        """
        written = {
            row.name: row.key.label
            for row in _DECLARED
            if row.key is not None and row.key.label
        }

        assert written == _WRITTEN_LABELS

    def test_a_row_holds_its_fields_in_slots_alone(self) -> None:
        """A field is a slot, so a misspelled one is an error, not a new field."""
        row = _row("upgrade")

        assert not hasattr(row, "__dict__")
        with pytest.raises(AttributeError):
            row.unknown = 1  # type: ignore[attr-defined]

    def test_a_boolean_flag_is_negatable_unless_it_is_already_a_negation(self) -> None:
        """``--no-no-emit-workspace`` is the spelling this drops.

        A row named ``no-`` cannot be negatable, because the negation is
        built off the flag rather than off a second name.
        """
        negatable = [row for row in ALL if row.kind in _BOOL_KINDS]

        assert len(negatable) == 12
        for row in negatable:
            assert row.negatable is not row.name.startswith("no-"), row.name

    def test_dist_policy_is_the_one_rung_zero_outside_its_own_tokens(self) -> None:
        """Its rung 0 is a policy and whether the flag was written.

        That is the row the rung-0 check in :mod:`nab.optionlower` steps
        over, and it steps over no other.
        """
        compound = [
            row.name
            for row in ALL
            if row.choices
            and row.key is not None
            and not isinstance(row.rdefault, (str, enum.Enum))
        ]

        assert compound == ["dist-policy"]


class TestTheEnumBackedChoiceRows:
    """A choice row copied from an enum says so, and ``mirrors`` holds it there.

    The alias in ``nab.flagtypes`` is a hand-written copy of the enum's
    values, and a checker reads a ``Literal`` only where the members are
    written out, so nothing but ``mirrors=`` ties the copy to its original.
    """

    def test_the_rows_that_mirror_an_enum_are_the_five_that_carry_one(self) -> None:
        declared = {
            row.name: row.mirrors.__name__
            for row in _DECLARED
            if row.mirrors is not None
        }

        assert declared == {
            "resolution": "ResolutionStrategy",
            "decision-order": "DecisionOrder",
            "mode": "ResolveMode",
            "dist-policy": "DistPolicy",
            "build-policy": "BuildPolicy",
        }

    def test_a_row_whose_tokens_are_a_policy_enums_names_that_enum(self) -> None:
        """A sixth row copied from one has to say so, like the five above."""
        by_tokens = {
            tuple(str(member.value) for member in candidate): candidate.__name__
            for candidate in vars(policy).values()
            if isinstance(candidate, type) and issubclass(candidate, enum.Enum)
        }

        for row, lowered in zip(_DECLARED, ALL, strict=True):
            twin = by_tokens.get(lowered.choices)
            if twin is None:
                continue

            assert row.mirrors is not None, lowered.name
            assert row.mirrors.__name__ == twin, lowered.name


class TestDerivedSpellings:
    """The four members a spelling is read off rather than written down."""

    def test_a_project_row_takes_its_prefix_from_its_scope(self) -> None:
        assert _row("mode", scope=Scope.PROJECT, sample="x").cli_flag == (
            "--project-mode"
        )

    def test_a_user_row_takes_the_bare_prefix(self) -> None:
        assert _row("offline", scope=Scope.USER).cli_flag == "--offline"

    def test_a_repeatable_row_is_spelled_in_the_singular(self) -> None:
        row = _row(
            "constraints",
            scope=Scope.PROJECT,
            kind=Kind.APPEND,
            vtype=VType.STR,
            sample="attrs<24",
            default=(),
        )

        assert row.cli_flag == "--project-constraint"
        assert row.dest == "project_constraint"

    def test_a_sub_row_takes_the_key_it_is_under_into_its_flag(self) -> None:
        """The flag carries the parent key; the registry keeps one key for both."""
        row = _sub_row()

        assert row.cli_flag == "--project-matrix-python"
        assert row.dest == "project_matrix_python"
        assert row.key is None
        assert row.allowed_in_toml(SourceKind.PYPROJECT) is False

    def test_the_parameter_is_the_flag_without_its_dashes(self) -> None:
        row = _row("cache-dir", scope=Scope.USER, vtype=VType.PATH, sample="c")

        assert row.cli_param == row.dest == "cache_dir"

    def test_an_operand_has_no_flag_and_still_has_a_slot(self) -> None:
        row = _row("path", kind=Kind.POSITIONAL, vtype=VType.PATH, default="p")

        assert row.cli_flag is None
        assert row.cli_param is None
        assert row.dest == "path"

    def test_a_file_only_row_has_no_command_line_at_all(self) -> None:
        row = Opt(
            "matrix",
            scope=Scope.PROJECT,
            rdefault=None,
            parse=values.parse_matrix,
            render=hooks.render_matrix,
            help="the axes",
            docs="reference/cli.md",
        )

        assert row.cli_flag is None
        assert row.env_var is None
        assert row.key == "matrix"

    def test_a_command_local_row_carries_no_config_key(self) -> None:
        assert _row("format").key is None

    def test_only_a_layered_row_has_a_scope_word(self) -> None:
        """``nab config explain`` prints it, and it explains layered rows."""
        assert _row("offline", scope=Scope.USER).scope_name == "user"

        with pytest.raises(TypeError, match="format is not a layered row"):
            _ = _row("format").scope_name

    def test_the_environment_name_is_the_key_shouted(self) -> None:
        row = _row(
            "max-concurrency", scope=Scope.USER, env=True, vtype=VType.INT, sample="4"
        )

        assert row.env_var == "NAB_MAX_CONCURRENCY"


class TestTheCategoryGate:
    """Which TOML sources a row's scope lets set it.

    Driven with the live ``SourceKind`` rather than a stand-in, because
    the gate matches on the member's value and a renamed value would
    otherwise pass here and refuse the source in the config layer.
    """

    @pytest.mark.parametrize(
        ("scope", "kind", "allowed"),
        [
            (Scope.PROJECT, SourceKind.PYPROJECT, True),
            (Scope.PROJECT, SourceKind.PROJECT_TOML, True),
            (Scope.PROJECT, SourceKind.USER_TOML, False),
            (Scope.PROJECT, SourceKind.SYSTEM_TOML, False),
            (Scope.USER, SourceKind.PYPROJECT, False),
            (Scope.USER, SourceKind.PROJECT_TOML, True),
            (Scope.USER, SourceKind.USER_TOML, True),
            (Scope.USER, SourceKind.SYSTEM_TOML, True),
        ],
    )
    def test_the_gate_reads_off_the_scope(
        self, scope: Scope, kind: SourceKind, allowed: bool
    ) -> None:
        row = _row("offline", scope=scope, sample="x")

        assert row.allowed_in_toml(kind) is allowed

    def test_a_row_no_source_can_set_is_allowed_nowhere(self) -> None:
        assert _row("format").allowed_in_toml(SourceKind.PYPROJECT) is False


class TestPerRowRules:
    """The rules one row can be judged by, run as it is built."""

    def test_refuses_a_short_name_that_is_not_one_character(self) -> None:
        assert _construction_error("verbose", short="vv") == (
            "verbose takes one alphanumeric short name, not 'vv'"
        )

    def test_refuses_a_short_name_that_is_punctuation(self) -> None:
        assert _construction_error("verbose", short="-") == (
            "verbose takes one alphanumeric short name, not '-'"
        )

    def test_refuses_a_negatable_row_already_spelled_as_a_negation(self) -> None:
        assert _construction_error("no-cache", negatable=True) == (
            "no-cache is negatable, so it must not be named no-"
        )

    def test_refuses_a_value_taking_row_with_no_vtype(self) -> None:
        assert _construction_error("output", kind=Kind.VALUE) == (
            "output reads a value, so it needs a vtype"
        )

    @pytest.mark.parametrize("name", ["", "all_extras", "extras=", "all extras"])
    def test_refuses_a_long_name_the_walk_cannot_match(self, name: str) -> None:
        assert _construction_error(name) == f"{name!r} is not a usable long name"

    def test_refuses_a_project_key_that_writes_its_own_prefix(self) -> None:
        assert (
            _construction_error("project-mode", scope=Scope.PROJECT, sample="x")
            == "project-mode takes its project- prefix from its scope"
        )

    def test_refuses_a_repeatable_row_with_no_singular(self) -> None:
        assert (
            _construction_error(
                "constraint",
                scope=Scope.PROJECT,
                kind=Kind.APPEND,
                vtype=VType.STR,
                sample="x",
                default=(),
            )
            == "constraint is repeatable, so its name has to be plural"
        )

    def test_refuses_an_environment_name_on_a_project_key(self) -> None:
        assert _construction_error(
            "mode", scope=Scope.PROJECT, env=True, sample="x"
        ) == ("mode is not a USER key, so it takes no NAB_ name")

    def test_refuses_an_environment_name_on_a_row_with_no_key(self) -> None:
        assert _construction_error("format", env=True) == (
            "format is not a USER key, so it takes no NAB_ name"
        )

    def test_refuses_a_layered_row_with_no_parse_hook(self) -> None:
        assert _construction_error("offline", scope=Scope.USER, parse=None) == (
            "offline is layered, so it needs a parse and a render hook"
        )

    def test_refuses_a_layered_row_with_no_render_hook(self) -> None:
        assert _construction_error("offline", scope=Scope.USER, render=None) == (
            "offline is layered, so it needs a parse and a render hook"
        )

    def test_a_row_with_no_key_carries_a_hook_that_refuses_to_run(self) -> None:
        """The keyless rows keep their stand-in hook, and it never parses."""
        row = _row("format", parse=None, render=None)

        with pytest.raises(TypeError, match="carries no parse or render hook"):
            row.parse("x", "nowhere")

    def test_refuses_a_row_with_no_help(self) -> None:
        assert _construction_error("upgrade", help="") == "upgrade declares no help"

    def test_refuses_a_file_only_row_with_no_rung_zero(self) -> None:
        assert _construction_error(
            "matrix", scope=Scope.PROJECT, commands=(), default=UNSET
        ) == ("matrix has no command line, so it needs a key and rdefault")

    def test_refuses_a_file_only_row_with_no_key(self) -> None:
        assert _construction_error(
            "matrix", commands=(), default=UNSET, rdefault=None
        ) == ("matrix has no command line, so it needs a key and rdefault")

    @pytest.mark.parametrize(
        "field", [{"short": "m"}, {"env": True}, {"default": None}]
    )
    def test_refuses_a_file_only_row_that_declares_a_cli_field(
        self, field: dict[str, Any]
    ) -> None:
        fields: dict[str, Any] = {
            "scope": Scope.USER,
            "commands": (),
            "default": UNSET,
            "rdefault": None,
            **field,
        }

        assert _construction_error("matrix", **fields) == (
            "matrix has no command line, so it takes no CLI fields"
        )

    def test_refuses_a_sub_row_with_no_scope(self) -> None:
        assert (
            _construction_error(
                "python",
                under="matrix",
                kind=Kind.VALUE,
                vtype=VType.STR,
                default=None,
                parse=None,
                render=None,
            )
            == "python is under 'matrix', so it needs a scope"
        )

    def test_refuses_a_sub_row_with_no_command_line(self) -> None:
        assert (
            _construction_error(
                "python",
                scope=Scope.PROJECT,
                under="matrix",
                kind=Kind.VALUE,
                vtype=VType.STR,
                commands=(),
                default=UNSET,
                parse=None,
                render=None,
            )
            == "python is under 'matrix', so it needs a command line"
        )

    def test_refuses_a_sub_row_that_binds_an_operand(self) -> None:
        assert (
            _construction_error(
                "python",
                scope=Scope.PROJECT,
                under="matrix",
                kind=Kind.POSITIONAL,
                vtype=VType.STR,
                default="",
                parse=None,
                render=None,
            )
            == "python is under 'matrix', so it cannot be an operand"
        )

    def test_refuses_a_sub_row_carrying_a_key_of_its_own(self) -> None:
        """One registry key per table: a sub-row declares no rung 0 or hook."""
        assert (
            _construction_error(
                "python",
                scope=Scope.PROJECT,
                under="matrix",
                kind=Kind.VALUE,
                vtype=VType.STR,
                default=None,
                rdefault=None,
                parse=None,
                render=None,
            )
            == "python is under 'matrix', so it takes no key of its own"
        )

    def test_refuses_a_sub_row_that_repeats_the_key_it_is_under(self) -> None:
        assert (
            _construction_error(
                "matrix-python",
                scope=Scope.PROJECT,
                under="matrix",
                kind=Kind.VALUE,
                vtype=VType.STR,
                default=None,
                parse=None,
                render=None,
            )
            == "matrix-python takes its matrix- prefix from the key it is under"
        )

    def test_refuses_a_layered_free_form_row_with_no_sample(self) -> None:
        assert (
            _construction_error(
                "requires-python", scope=Scope.PROJECT, kind=Kind.VALUE, vtype=VType.STR
            )
            == "requires-python is layered and free-form, so it needs a sample"
        )

    def test_a_layered_row_no_command_carries_still_needs_a_sample(self) -> None:
        """A layered key needs one whether or not a command carries it."""
        assert (
            _construction_error(
                "requires-python",
                scope=Scope.PROJECT,
                kind=Kind.VALUE,
                vtype=VType.STR,
                commands=(),
                default=UNSET,
                rdefault=None,
            )
            == "requires-python is layered and free-form, so it needs a sample"
        )


class TestCrossRowRules:
    """The rules a whole table has to satisfy, run by ``validate``."""

    def test_refuses_one_command_declaring_a_flag_twice(self) -> None:
        rows = (_row("upgrade"), _row("upgrade"))

        assert _refused(rows) == "nab lock declares --upgrade twice"

    def test_refuses_one_command_declaring_a_short_name_twice(self) -> None:
        rows = (_row("upgrade", short="u"), _row("locked", short="u"))

        assert _refused(rows) == "nab lock declares -u twice"

    def test_refuses_the_root_declaring_a_flag_twice(self) -> None:
        rows = (
            _row("quiet", commands=GLOBAL),
            _row("quiet", commands=GLOBAL),
            _row("upgrade"),
        )

        assert _refused(rows) == "the root table declares --quiet twice"

    def test_refuses_a_command_flag_that_shadows_a_root_one(self) -> None:
        rows = (_row("quiet", commands=GLOBAL), _row("quiet"))

        assert _refused(rows) == "nab lock redeclares the root flag --quiet"

    def test_refuses_a_command_short_name_that_shadows_a_root_one(self) -> None:
        rows = (_row("quiet", commands=GLOBAL, short="q"), _row("upgrade", short="q"))

        assert _refused(rows) == "nab lock redeclares the root short -q"

    def test_refuses_a_command_name_that_reads_as_an_option(self) -> None:
        commands = (("-lock", "nab._lock", "lock"),)

        assert _refused((_row("upgrade"),), commands) == (
            "'-lock' is not a usable command name"
        )

    def test_refuses_two_operands_that_both_take_the_rest(self) -> None:
        rows = (
            _row("first", kind=Kind.POSITIONAL, vtype=VType.STR, default=()),
            _row("second", kind=Kind.POSITIONAL, vtype=VType.STR, default=()),
        )

        assert _refused(rows) == (
            "nab lock gives the rest of the line to first and then to second"
        )

    def test_refuses_a_declared_name_a_negation_would_generate(self) -> None:
        rows = (_row("cache", negatable=True), _row("no-cache"))

        assert _refused(rows) == "nab lock declares --no-cache, which cache generates"

    def test_reads_the_negation_off_the_flag_and_not_the_name(self) -> None:
        """A PROJECT row generates ``--no-project-X``, prefix and all."""
        rows = (
            _row("cache", scope=Scope.PROJECT, negatable=True),
            _row("no-project-cache"),
        )

        assert _refused(rows) == (
            "nab lock declares --no-project-cache, which cache generates"
        )

    def test_refuses_a_required_operand_after_an_optional_one(self) -> None:
        rows = (
            _row("key", kind=Kind.POSITIONAL, vtype=VType.STR, default=""),
            _row(
                "action", kind=Kind.VERB, vtype=VType.STR, required=True, default=UNSET
            ),
        )

        assert _refused(rows) == "nab lock requires action after optional key"

    def test_refuses_an_operand_at_the_root(self) -> None:
        rows = (
            _row(
                "path",
                kind=Kind.POSITIONAL,
                vtype=VType.PATH,
                commands=GLOBAL,
                default="p",
            ),
            _row("upgrade"),
        )

        assert _refused(rows) == (
            "path is a root operand, and the command holds that slot"
        )

    def test_refuses_a_row_naming_a_command_that_is_not_declared(self) -> None:
        assert _refused((_row("upgrade", commands=("lock", "vendor")),)) == (
            "upgrade names the undeclared command 'vendor'"
        )

    def test_an_empty_command_table_declares_no_commands(self) -> None:
        """Passing no entries means none, rather than the declared four."""
        assert _refused((_row("upgrade"),), ()) == (
            "upgrade names the undeclared command 'lock'"
        )

    def test_refuses_a_sub_row_under_a_key_no_row_declares(self) -> None:
        assert _refused((_sub_row(),)) == (
            "python is under 'matrix', which no row declares as a key"
        )

    def test_refuses_a_sub_row_scoped_apart_from_its_parent(self) -> None:
        rows = (
            _row("matrix", scope=Scope.USER, commands=(), default=UNSET, rdefault=None),
            _sub_row(),
        )

        assert _refused(rows) == (
            "python is project-scope under 'matrix', which is user-scope"
        )

    def test_refuses_a_sub_row_whose_parent_carries_a_flag(self) -> None:
        """Either the key takes a flag or its sub-rows do, never both."""
        rows = (_row("matrix", scope=Scope.PROJECT, sample="x"), _sub_row())

        assert _refused(rows) == (
            "'matrix' takes a flag of its own, so python cannot spell it as well"
        )

    def test_refuses_a_command_no_row_reaches(self) -> None:
        commands = (("lock", "nab._lock", "lock"), ("cache", "nab._cache_cmd", "c"))

        assert _refused((_row("upgrade"),), commands) == (
            "no row names the command 'cache'"
        )
