"""Differential METADATA parsing: nab parse_metadata vs upstream packaging.

Oracles:

* field agreement with upstream ``packaging.metadata.parse_email`` raw
  output
* metamorphic: header-name recasing, unknown-header interleaving, body
  isolation
* bytes/str input equivalence
"""

from __future__ import annotations

import random

import packaging.metadata as up_meta
import pytest
from hypothesis import given
from hypothesis import strategies as st
from packaging.requirements import Requirement as UpReq

from nab_python.metadata import parse_metadata

from .strategies import DEEP_SETTINGS

pytestmark = pytest.mark.property

names = st.from_regex(r"[A-Za-z0-9]([A-Za-z0-9._-]{0,10}[A-Za-z0-9])?", fullmatch=True)
version_strs = st.sampled_from(
    ["1.0", "0.1.dev0", "2!1.0rc1", "1.0.post3+local.7", "1.0a1", "10.20.30"]
)
req_strs = st.sampled_from(
    [
        "numpy>=1.26",
        'pytest; extra == "test"',
        "pkg[a,b]~=2.0",
        'dep===1.0+local; python_version < "3.10" or os_name == "posix"',
        "url-dep @ https://example.com/x.whl",
    ]
)
extra_strs = st.sampled_from(["test", "Dev_Tools", "docs.x", "ALL"])
dynamic_strs = st.sampled_from(
    ["Requires-Dist", "requires-dist", "PROVIDES-EXTRA", "Author", "License-File"]
)
rp_strs = st.sampled_from([">=3.9", "<4, >=3.8", "~=3.10", "==3.11.*"])


def _recase(upper: bool, s: str) -> str:
    return s.upper() if upper else s.lower()


@st.composite
def metadata_inputs(draw: st.DrawFn) -> str:
    lines: list[str] = []
    cases = st.booleans()
    lines.append(f"{_recase(draw(cases), 'Metadata-Version')}: 2.2")
    lines.append(f"{_recase(draw(cases), 'Name')}: {draw(names)}")
    lines.append(f"{_recase(draw(cases), 'Version')}: {draw(version_strs)}")
    if draw(st.booleans()):
        lines.append(f"{_recase(draw(cases), 'Requires-Python')}: {draw(rp_strs)}")
    for r in draw(st.lists(req_strs, max_size=4)):
        lines.append(f"{_recase(draw(cases), 'Requires-Dist')}: {r}")
    for e in draw(st.lists(extra_strs, max_size=3)):
        lines.append(f"{_recase(draw(cases), 'Provides-Extra')}: {e}")
    for d in draw(st.lists(dynamic_strs, max_size=3)):
        lines.append(f"{_recase(draw(cases), 'Dynamic')}: {d}")
    # unknown headers sprinkled anywhere
    unknown = st.sampled_from(["Summary: stuff", "Author: A B", "X-Junk: 1"])
    for u in draw(st.lists(unknown, max_size=3)):
        lines.insert(draw(st.integers(0, len(lines))), u)
    text = "\n".join(lines) + "\n"
    if draw(st.booleans()):
        text += "\nLong description body...\nRequires-Dist: not-a-header-anymore\n"
    return text


@DEEP_SETTINGS
@given(text=metadata_inputs())
def test_fields_agree_with_upstream_raw(text: str) -> None:
    nab = parse_metadata(text)
    raw, _unparsed = up_meta.parse_email(text)
    assert nab.name == raw.get("name"), text
    assert str(nab.version) == raw.get("version"), text
    up_rp = raw.get("requires_python")
    if up_rp is None:
        assert nab.requires_python is None, text
    else:
        assert nab.requires_python is not None, text
        assert str(nab.requires_python) == str(type(nab.requires_python)(up_rp)), text
    up_reqs = raw.get("requires_dist") or []
    assert [str(r) for r in nab.requires_dist] == [str(UpReq(r)) for r in up_reqs], text
    assert nab.provides_extra == (raw.get("provides_extra") or []), text
    assert nab.dynamic == frozenset(d.lower() for d in (raw.get("dynamic") or [])), text
    assert nab.metadata_version == raw.get("metadata_version"), text


@DEEP_SETTINGS
@given(text=metadata_inputs())
def test_bytes_and_str_inputs_equivalent(text: str) -> None:
    a = parse_metadata(text)
    b = parse_metadata(text.encode("utf-8"))
    assert (a.name, str(a.version), a.provides_extra, a.dynamic) == (
        b.name,
        str(b.version),
        b.provides_extra,
        b.dynamic,
    )
    assert [str(r) for r in a.requires_dist] == [str(r) for r in b.requires_dist]


@DEEP_SETTINGS
@given(text=metadata_inputs(), rng=st.randoms(use_true_random=False))
def test_header_recasing_is_identity(text: str, rng: random.Random) -> None:
    headers, sep, body = text.partition("\n\n")
    recased_lines = []
    for line in headers.splitlines():
        key, colon, val = line.partition(":")
        recased_lines.append(
            "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in key)
            + colon
            + val
        )
    recased = "\n".join(recased_lines) + "\n" + sep + body
    a = parse_metadata(text)
    b = parse_metadata(recased)
    assert a.name == b.name
    assert a.version == b.version
    assert [str(r) for r in a.requires_dist] == [str(r) for r in b.requires_dist]
    assert a.provides_extra == b.provides_extra
    assert a.dynamic == b.dynamic
    assert (a.requires_python is None) == (b.requires_python is None)


@DEEP_SETTINGS
@given(text=metadata_inputs())
def test_body_never_contributes_fields(text: str) -> None:
    headers, _, _ = text.partition("\n\n")
    if not headers.endswith("\n"):
        headers += "\n"
    base = parse_metadata(headers)
    poisoned = (
        headers
        + "\nRequires-Dist: evil-pkg\nDynamic: Requires-Dist\nProvides-Extra: evil\n"
    )
    after = parse_metadata(poisoned)
    assert [str(r) for r in base.requires_dist] == [str(r) for r in after.requires_dist]
    assert base.dynamic == after.dynamic
    assert base.provides_extra == after.provides_extra
