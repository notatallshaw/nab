"""The declaration refuses settings without equivalent configuration surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from nab._cli.definition.build import build_options
from nab._cli.definition.model import Scope
from nab._cli.definition.rows import Eager, Key, Layer, Many, Switch, Tri, Value
from nab._cli.definition.tables import Table
from nab.config import hooks, values


def test_scalar_setting_cannot_be_cli_only() -> None:
    class Settings(Table, on=("lock",), docs="reference/cli.md"):
        policy = Value[Literal["check", "reuse"]](
            default="check", help="select a policy"
        )

    with pytest.raises(ValueError, match="policy is CLI-only"):
        build_options(Settings)


def test_choice_cannot_bypass_layering_as_an_action() -> None:
    class Settings(Table, on=("lock",), docs="reference/cli.md"):
        policy = Value[Literal["check", "reuse"]](
            default="check", help="select a policy", action=True
        )

    with pytest.raises(ValueError, match="configurable value"):
        build_options(Settings)


def test_boolean_setting_needs_a_layer_or_explicit_action() -> None:
    class Settings(Table, on=("lock",), docs="reference/cli.md"):
        fast = Switch(default=False, help="choose faster checks")

    with pytest.raises(ValueError, match="fast is CLI-only"):
        build_options(Settings)


def test_config_key_without_dedicated_flag_requires_assignment_parser() -> None:
    class Settings(Table, scope=Scope.PROJECT, docs="reference/cli.md"):
        enabled = Key(
            Layer[bool](
                rdefault=False,
                parse=values.parse_bool,
                render=hooks.render_bool,
                label="bool",
            ),
            help="enable a feature",
        )

    with pytest.raises(ValueError, match="no dedicated CLI flag"):
        build_options(Settings)

    class Assignment(Table, on=("lock",), docs="reference/cli.md"):
        settings = Many[str](help="assign settings", alias="*")

    assert len(build_options(Settings, Assignment)) == 2


def test_actions_remain_explicit_and_separate() -> None:
    class Actions(Table, on=("lock",), docs="reference/cli.md"):
        output = Value[Path | None](help="choose this command's output", action=True)
        check = Switch(default=False, help="check this command's result", action=True)
        help = Eager(help="show help")

    assert len(build_options(Actions)) == 3


def test_environment_alternative_must_be_registered() -> None:
    class Output(Table, on=("lock",), docs="reference/cli.md"):
        color = Value[str](
            default="auto", help="choose output color", environ=("NAB_COLOR",)
        )

    with pytest.raises(ValueError, match="unregistered environment"):
        build_options(Output)

    assert len(build_options(Output, environment_names={"NAB_COLOR"})) == 1


def test_alias_requires_a_real_configuration_key() -> None:
    class Shortcut(Table, on=("lock",), docs="reference/cli.md"):
        target = Value[str | None](help="select a target", alias="environment.python")

    with pytest.raises(ValueError, match="undeclared setting"):
        build_options(Shortcut)


def test_alias_cannot_name_an_unknown_table_field() -> None:
    class Settings(Table, on=("lock",), scope=Scope.PROJECT, docs="reference/cli.md"):
        enabled = Tri(
            help="enable a feature",
            key=Layer[bool](
                rdefault=False, parse=values.parse_bool, render=hooks.render_bool
            ),
        )

    class Shortcut(Table, on=("lock",), docs="reference/cli.md"):
        target = Value[str | None](help="select a target", alias="enabled.unknown")

    with pytest.raises(ValueError, match="undeclared table key"):
        build_options(Settings, Shortcut)


def test_alias_cannot_claim_the_generic_assignment_binding() -> None:
    class Shortcut(Table, on=("lock",), docs="reference/cli.md"):
        arbitraries = Many[str](help="a new option", alias="*")

    with pytest.raises(ValueError, match="only the settings assignment"):
        build_options(Shortcut)


def test_declared_value_cannot_be_an_action_and_environment_option() -> None:
    class Settings(Table, on=("lock",), docs="reference/cli.md"):
        output = Value[Path | None](
            help="select output", action=True, environ=("NAB_OUTPUT",)
        )

    with pytest.raises(ValueError, match="competing setting/action forms"):
        build_options(Settings, environment_names={"NAB_OUTPUT"})
