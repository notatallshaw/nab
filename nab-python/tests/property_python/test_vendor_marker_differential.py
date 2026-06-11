"""Differential PEP 508 marker semantics: vendored vs upstream packaging.

Generates syntactically plausible marker strings (all comparison ops,
``in``/``not in``, ``and``/``or`` chains, parens, literal vs variable on
either side) plus random environments (valid and invalid version
strings) and asserts both implementations agree on parse acceptance,
evaluation result, and raised exception class name.
"""

from __future__ import annotations

from types import ModuleType

import packaging.markers as up_markers
import pytest
from hypothesis import given
from hypothesis import strategies as st

import nab_python._vendor.packaging.markers as nb_markers

from .strategies import DEEP_SETTINGS

pytestmark = pytest.mark.property

VERSION_VARS = (
    "python_version",
    "python_full_version",
    "implementation_version",
    "platform_release",
)
STRING_VARS = (
    "os_name",
    "sys_platform",
    "platform_machine",
    "platform_system",
    "platform_version",
    "platform_python_implementation",
    "implementation_name",
    "extra",
)
ALL_VARS = VERSION_VARS + STRING_VARS

OPS = ("<", "<=", "==", "!=", ">=", ">", "~=", "===", "in", "not in")

VERSIONISH = (
    "1.0",
    "2.0.1",
    "1!1.0",
    "1.0+abc",
    "1.0+ubuntu.1",
    "1.0.dev1",
    "1.0.post1",
    "1.0rc1",
    "1.0a1",
    "3.*",
    "1.0.*",
    "0.dev0",
    "1.2.3.4.5",
    "01.0",
    "v1.0",
    "3.11",
    "3.11.0",
)
STRINGY = (
    "linux",
    "Linux",
    "",
    "a b",
    "extra",
    "5.15.0-86-generic",
    "not-a-version",
    "*",
    ".",
    "10",
)
VALUES = VERSIONISH + STRINGY

values = st.sampled_from(VALUES)
variables = st.sampled_from(ALL_VARS)
ops = st.sampled_from(OPS)


@st.composite
def atoms(draw: st.DrawFn) -> str:
    op = draw(ops)
    form = draw(st.integers(0, 2))
    var = draw(variables)
    val = draw(values)
    if form == 0:
        return f'{var} {op} "{val}"'
    if form == 1:
        return f'"{val}" {op} {var}'
    other = draw(values)
    return f'"{val}" {op} "{other}"'


def _combine(children: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    @st.composite
    def inner(draw: st.DrawFn) -> str:
        n = draw(st.integers(2, 3))
        parts = [draw(children) for _ in range(n)]
        joiners = [draw(st.sampled_from([" and ", " or "])) for _ in range(n - 1)]
        out = parts[0]
        for joiner, part in zip(joiners, parts[1:], strict=True):
            out += joiner + part
        if draw(st.booleans()):
            return f"({out})"
        return out

    return inner()


marker_strings = st.recursive(atoms(), _combine, max_leaves=6)

environments = st.dictionaries(st.sampled_from(ALL_VARS), values, max_size=6)


def _outcome(mod: ModuleType, text: str, env: dict[str, str]) -> tuple[str, object]:
    try:
        marker = mod.Marker(text)
    except Exception as exc:  # noqa: BLE001
        return ("parse-error", type(exc).__name__)
    try:
        return ("value", marker.evaluate(dict(env)))
    except Exception as exc:  # noqa: BLE001
        return ("eval-error", type(exc).__name__)


@DEEP_SETTINGS
@given(text=marker_strings, env=environments)
def test_vendored_matches_upstream(text: str, env: dict[str, str]) -> None:
    up = _outcome(up_markers, text, env)
    nb = _outcome(nb_markers, text, env)
    assert up == nb, f"marker {text!r} env {env!r}: upstream {up} vendored {nb}"


_FREE_CHARS = st.characters(
    codec="ascii", min_codepoint=32, max_codepoint=126, exclude_characters="\"'\\"
)
free_values = st.text(_FREE_CHARS, max_size=12)


@st.composite
def free_atoms(draw: st.DrawFn) -> str:
    var = draw(variables)
    op = draw(ops)
    val = draw(free_values)
    quote = draw(st.sampled_from(['"', "'"]))
    if draw(st.booleans()):
        return f"{var} {op} {quote}{val}{quote}"
    return f"{quote}{val}{quote} {op} {var}"


free_marker_strings = st.recursive(free_atoms(), _combine, max_leaves=4)


@DEEP_SETTINGS
@given(
    text=free_marker_strings,
    env=st.dictionaries(variables, free_values, max_size=4),
)
def test_free_text_values_match_upstream(text: str, env: dict[str, str]) -> None:
    up = _outcome(up_markers, text, env)
    nb = _outcome(nb_markers, text, env)
    assert up == nb, f"marker {text!r} env {env!r}: upstream {up} vendored {nb}"


@DEEP_SETTINGS
@given(text=marker_strings)
def test_str_roundtrip_matches_upstream(text: str) -> None:
    try:
        up: str | tuple[str, str] = str(up_markers.Marker(text))
    except Exception as exc:  # noqa: BLE001
        up = ("parse-error", type(exc).__name__)
    try:
        nb: str | tuple[str, str] = str(nb_markers.Marker(text))
    except Exception as exc:  # noqa: BLE001
        nb = ("parse-error", type(exc).__name__)
    assert up == nb, f"marker {text!r}: upstream {up!r} vendored {nb!r}"
