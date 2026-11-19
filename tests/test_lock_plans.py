"""Validate named lock declarations before resolver I/O."""

import pytest

from nab.config.lock_plans import LockPlan
from nab.config.values import SourceConfigError, parse_input_tables


@pytest.mark.parametrize(
    "entry",
    [
        {"unknown": []},
        {"groups": "lint"},
        {"extras": "extra"},
        {"inputs": "docs"},
        {"inputs": [3]},
        {"build-requirements": 1},
        {"settings": []},
    ],
)
def test_invalid_plans_are_refused(entry: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError), match="lock plan 'dev'"):
        LockPlan.read("dev", {"dev": entry}, "pylock")


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"dev": 1},
        {"dev": {1: "not a TOML key"}},
        {"../bad": {}},
        {"Dev_Env": {}, "dev-env": {}},
    ],
)
def test_invalid_declaration_table_names_are_refused(value: object) -> None:
    with pytest.raises(SourceConfigError):
        parse_input_tables(value, "test input")
