"""PEP 643 staticness properties for ``metadata_deps_are_static``.

* ``Dynamic`` field is case-insensitive end to end (header value casing
  never matters)
* version threshold: parsed ``(major, minor) < (2, 2)`` is never static
* monotonicity: adding ``Dynamic`` entries never makes metadata more
  trusted
* arbitrary ``Metadata-Version`` strings never crash; unparseable means
  untrusted
* pre-2.2 PKG-INFO deps untrusted by default
  (``_sdist_deps_need_dynamic``)
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_python._provider.metadata_resolver import _sdist_deps_need_dynamic
from nab_python._vendor.packaging.version import Version
from nab_python.metadata import (
    DEPENDENCY_FIELDS,
    WheelMetadata,
    metadata_deps_are_static,
    parse_metadata,
)

from .strategies import DEEP_SETTINGS

pytestmark = pytest.mark.property

DEP_FIELDS = sorted(DEPENDENCY_FIELDS)
NON_DEP_FIELDS = ["author", "license-file", "classifier", "requires-python", "summary"]


def _randcase(s: str, mask: int) -> str:
    return "".join(c.upper() if (mask >> i) & 1 else c.lower() for i, c in enumerate(s))


@DEEP_SETTINGS
@given(
    field=st.sampled_from(DEP_FIELDS),
    mask=st.integers(0, 2**16),
    mv=st.sampled_from(["2.2", "2.3", "2.4", "2.5", "3.0", "12.0"]),
)
def test_dynamic_dep_field_any_casing_blocks_static(
    field: str, mask: int, mv: str
) -> None:
    text = (
        f"Metadata-Version: {mv}\nName: p\nVersion: 1.0\n"
        f"Dynamic: {_randcase(field, mask)}\n"
    )
    md = parse_metadata(text)
    assert metadata_deps_are_static(md) is False, text


@DEEP_SETTINGS
@given(
    fields=st.lists(st.sampled_from(NON_DEP_FIELDS), max_size=4),
    mv=st.sampled_from(["2.2", "2.3", "2.6", "10.0"]),
)
def test_non_dep_dynamic_fields_do_not_block_static(fields: list[str], mv: str) -> None:
    lines = [f"Metadata-Version: {mv}", "Name: p", "Version: 1.0"]
    lines += [f"Dynamic: {f}" for f in fields]
    md = parse_metadata("\n".join(lines) + "\n")
    assert metadata_deps_are_static(md) is True


@DEEP_SETTINGS
@given(major=st.integers(0, 1), minor=st.integers(0, 9))
def test_below_2_2_never_static(major: int, minor: int) -> None:
    md = WheelMetadata(
        name="p", version=Version("1.0"), metadata_version=f"{major}.{minor}"
    )
    assert metadata_deps_are_static(md) is False
    assert _sdist_deps_need_dynamic(md, trust_unverified=False) is True


@DEEP_SETTINGS
@given(mv=st.text(max_size=12))
def test_arbitrary_metadata_version_never_crashes(mv: str) -> None:
    md = WheelMetadata(name="p", version=Version("1.0"), metadata_version=mv)
    result = metadata_deps_are_static(md)
    assert isinstance(result, bool)
    parts = mv.split(".")[:2]
    try:
        nums = [int(p) for p in parts]
        parseable = len(nums) == 2
    except ValueError:
        parseable = False
    if not parseable:
        assert result is False, f"unparseable {mv!r} must be untrusted"


@DEEP_SETTINGS
@given(
    mv=st.sampled_from(["2.2", "2.5", "9.9"]),
    base=st.sets(st.sampled_from(DEP_FIELDS + NON_DEP_FIELDS), max_size=5),
    extra=st.sets(st.sampled_from(DEP_FIELDS + NON_DEP_FIELDS), max_size=3),
)
def test_adding_dynamic_fields_is_monotone(
    mv: str, base: set[str], extra: set[str]
) -> None:
    md_small = WheelMetadata(
        name="p", version=Version("1.0"), metadata_version=mv, dynamic=frozenset(base)
    )
    md_big = WheelMetadata(
        name="p",
        version=Version("1.0"),
        metadata_version=mv,
        dynamic=frozenset(base | extra),
    )
    if not metadata_deps_are_static(md_small):
        assert not metadata_deps_are_static(md_big)


@DEEP_SETTINGS
@given(mv=st.sampled_from(["1.0", "1.1", "1.2", "2.1"]), trust=st.booleans())
def test_pre_2_2_trust_flag_is_the_only_escape(mv: str, trust: bool) -> None:
    md = WheelMetadata(name="p", version=Version("1.0"), metadata_version=mv)
    assert _sdist_deps_need_dynamic(md, trust_unverified=trust) is (not trust)
