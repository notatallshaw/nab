"""Differential PEP 508 requirement parsing: vendored vs upstream.

Oracles:

* outcome agreement on valid-by-construction and corrupted strings:
  either both parsers raise the same exception class or both accept
  with equal attributes (name, extras, specifier, marker, url)
* ``str()`` round-trip fixed point on both parsers
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from packaging.requirements import Requirement as UpReq

from nab_python._vendor.packaging.requirements import Requirement as NabReq

from .strategies import DEEP_SETTINGS

pytestmark = pytest.mark.property

ws = st.text(alphabet=" \t", max_size=2)

names = st.from_regex(r"[A-Za-z0-9]([A-Za-z0-9._-]{0,10}[A-Za-z0-9])?", fullmatch=True)

extra_names = st.from_regex(
    r"[A-Za-z0-9]([A-Za-z0-9._-]{0,6}[A-Za-z0-9])?", fullmatch=True
)


@st.composite
def versions(draw: st.DrawFn) -> str:
    epoch = draw(st.one_of(st.none(), st.integers(0, 3)))
    release = ".".join(
        str(draw(st.integers(0, 100))) for _ in range(draw(st.integers(1, 4)))
    )
    pre = draw(st.one_of(st.none(), st.sampled_from(["a1", "b2", "rc3", ".alpha4"])))
    post = draw(st.one_of(st.none(), st.sampled_from([".post1", "-1", "post2"])))
    dev = draw(st.one_of(st.none(), st.sampled_from([".dev0", "dev3"])))
    local = draw(st.one_of(st.none(), st.sampled_from(["+local", "+abc.123", "+1-2"])))
    v = release
    if epoch is not None:
        v = f"{epoch}!{v}"
    if pre is not None:
        v += pre
    if post is not None:
        v += post
    if dev is not None:
        v += dev
    if local is not None:
        v += local
    return v


@st.composite
def specifiers(draw: st.DrawFn) -> str:
    n = draw(st.integers(1, 3))
    clauses = []
    for _ in range(n):
        op = draw(st.sampled_from(["==", "!=", ">=", "<=", ">", "<", "~=", "==="]))
        v = draw(versions())
        if op == "~=" and "!" not in v and v.count(".") == 0:
            v += ".0"  # ~= needs at least two release segments
        if op in ("==", "!=") and draw(st.booleans()) and "+" not in v:
            v += ".*"
        clauses.append(op + draw(ws) + v)
    return (draw(ws) + ",").join(clauses)


MARKER_VARS = [
    "python_version",
    "python_full_version",
    "os_name",
    "sys_platform",
    "platform_machine",
    "platform_python_implementation",
    "implementation_name",
    "extra",
]


@st.composite
def marker_atoms(draw: st.DrawFn) -> str:
    var = draw(st.sampled_from(MARKER_VARS))
    op = draw(st.sampled_from(["==", "!=", ">=", "<=", ">", "<", "~=", "in", "not in"]))
    if var == "extra":
        # Both parsers skip PEP 685 normalization for parenthesized extra atoms, breaking the str fixed point; only canonical values here.
        val = draw(st.sampled_from(["linux", "win32", "x86-64", "cpython"]))
    else:
        val = draw(
            st.sampled_from(
                [
                    "3.8",
                    "3.10.1",
                    "linux",
                    "win32",
                    "x86_64",
                    "cpython",
                    "spam eggs",
                    "",
                ]
            )
        )
    quote = draw(st.sampled_from(['"', "'"]))
    flip = draw(st.booleans())
    lhs, rhs = (f"{quote}{val}{quote}", var) if flip else (var, f"{quote}{val}{quote}")
    return f"{lhs} {op} {rhs}"


@st.composite
def markers(draw: st.DrawFn) -> str:
    n = draw(st.integers(1, 3))
    parts = [draw(marker_atoms())]
    for _ in range(n - 1):
        joiner = draw(st.sampled_from([" and ", " or "]))
        atom = draw(marker_atoms())
        if draw(st.booleans()):
            atom = f"({atom})"
        parts.append(joiner + atom)
    return "".join(parts)


URLS = [
    "https://example.com/pkg-1.0-py3-none-any.whl",
    "file:///tmp/pkg-1.0.tar.gz",
    "git+https://github.com/o/r@deadbeef#egg=pkg",
    "https://example.com/x.whl#sha256=0123abcd",
]


@st.composite
def requirement_strings(draw: st.DrawFn) -> str:
    s = draw(names)
    if draw(st.booleans()):
        extras = draw(st.lists(extra_names, min_size=1, max_size=3))
        s += draw(ws) + "[" + (draw(ws) + ",").join(extras) + draw(ws) + "]"
    use_url = draw(st.booleans())
    if use_url:
        s += draw(ws) + "@ " + draw(st.sampled_from(URLS))
        if draw(st.booleans()):
            s += " ; " + draw(markers())  # URL reqs need whitespace before ;
        return s
    if draw(st.booleans()):
        spec = draw(specifiers())
        if draw(st.booleans()):
            spec = "(" + spec + ")"
        s += draw(ws) + spec
    if draw(st.booleans()):
        s += draw(ws) + ";" + draw(ws) + draw(markers())
    return s


def _outcome(req_cls: type, s: str) -> tuple[str, object]:
    try:
        req = req_cls(s)
    except Exception as exc:  # noqa: BLE001
        return ("error", type(exc).__name__)
    marker = str(req.marker) if req.marker is not None else None
    return (
        "value",
        (req.name, sorted(req.extras), str(req.specifier), marker, req.url, str(req)),
    )


@DEEP_SETTINGS
@given(s=requirement_strings())
def test_acceptance_and_attributes_match_upstream(s: str) -> None:
    up = _outcome(UpReq, s)
    nb = _outcome(NabReq, s)
    assert up == nb, f"requirement {s!r}: upstream {up} vendored {nb}"


@DEEP_SETTINGS
@given(s=requirement_strings())
def test_str_is_fixed_point_both_parsers(s: str) -> None:
    try:
        nab = NabReq(s)
        up = UpReq(s)
    except Exception:  # noqa: BLE001
        return
    once = str(nab)
    assert str(NabReq(once)) == once, s
    up_once = str(up)
    assert str(UpReq(up_once)) == up_once, s


@st.composite
def corrupted(draw: st.DrawFn) -> str:
    s = draw(requirement_strings())
    kind = draw(st.integers(0, 6))
    if not s:
        return s
    i = draw(st.integers(0, len(s) - 1))
    if kind == 0:
        return s[:i] + "\n" + s[i:]
    if kind == 1:
        return s[:i] + s[i + 1 :]
    if kind == 2:
        return s[:i] + draw(st.sampled_from(",;[]()@<>=!~ ")) + s[i:]
    if kind == 3:
        return s[:i] + s[i] + s[i:]
    if kind == 4:
        return s.upper()
    if kind == 5:
        return s + draw(st.sampled_from([",", ";", "[", "]", "==", "@"]))
    return draw(st.sampled_from([",", ";", " ", ""])) + s


@DEEP_SETTINGS
@given(s=corrupted())
def test_corrupted_acceptance_agreement(s: str) -> None:
    up = _outcome(UpReq, s)
    nb = _outcome(NabReq, s)
    assert up == nb, f"corrupted {s!r}: upstream {up} vendored {nb}"
