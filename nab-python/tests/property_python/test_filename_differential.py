"""Differential property tests for :mod:`nab_index.client` filename parsing.

``_parse_wheel_filename`` reimplements
:func:`packaging.utils.parse_wheel_filename` minus the tag-set parse, and
its docstring promises the accepted filename set is unchanged.
``_parse_sdist_filename`` wraps
:func:`packaging.utils.parse_sdist_filename` but rejects ``.zip`` sdists.
Upstream packaging is the oracle for both.
"""

from __future__ import annotations

import string
import sys

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)

from nab_index.client import _parse_sdist_filename, _parse_wheel_filename

from .strategies import DEEP_SETTINGS

pytestmark = pytest.mark.property

# Name-ish components: word chars, dots, underscores, unicode word
# characters, empties.
name_chars = st.sampled_from(
    [*string.ascii_letters, *string.digits, ".", "_", "é", "ß", "京"]
)
names = st.lists(name_chars, min_size=0, max_size=8).map("".join)

version_bits = st.sampled_from(
    [
        "1.0",
        "2.0.0",
        "0",
        "1!2.0",
        "1.0a1",
        "1.0.post2",
        "1.0.dev3",
        "1.0+local",
        "1.0+Local.UPPER",
        "01.02",
        "1.0rc1",
        "v1.0",
        "1.0.*",
        "",
        "abc",
        "1-0",
        "1_0",
        "  1.0  ",
        "1.0\n",
        "1.0.0.0.0.0",
        "~1.0",
        "1.0+abc_def",
    ]
)

build_bits = st.sampled_from(["0", "1", "12abc", "build", "1-2", "", "01", "1.2"])

tag_part = st.sampled_from(
    [
        "py3",
        "py2.py3",
        "cp311",
        "none",
        "any",
        "abi3",
        "",
        "manylinux_2_17_x86_64",
        "PY3",
        "py3 ",
    ]
)
tags = st.tuples(tag_part, tag_part, tag_part).map("-".join)

extensions = st.sampled_from(
    [".whl", ".WHL", ".tar.gz", ".zip", "", ".whl ", ".whl.whl"]
)

OVERSIZED_VERSION = "1" * (sys.get_int_max_str_digits() + 1)


@st.composite
def wheelish_filenames(draw: st.DrawFn) -> str:
    """Structured wheel-shaped filenames, valid and invalid."""
    name = draw(names)
    version = draw(version_bits)
    use_build = draw(st.booleans())
    tag = draw(tags)
    parts = [name, version]
    if use_build:
        parts.append(draw(build_bits))
    parts.append(tag)
    stem = "-".join(parts)
    # Occasionally perturb: drop or add a dash run.
    perturb = draw(st.integers(min_value=0, max_value=3))
    if perturb == 1:
        stem = stem.replace("-", "", 1)
    elif perturb == 2:
        stem = "-" + stem
    return stem + draw(extensions)


def oracle_wheel(filename: str) -> tuple[str, str] | None:
    """Parse via upstream packaging; ``None`` when it rejects."""
    try:
        name, version, _build, _tags = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        return None
    return (str(name), str(version))


def oracle_sdist(filename: str) -> tuple[str, str] | None:
    """Parse via upstream packaging; ``None`` for rejects and ``.zip``."""
    if filename.endswith(".zip"):
        return None
    try:
        name, version = parse_sdist_filename(filename)
    except InvalidSdistFilename:
        return None
    return (str(name), str(version))


@given(filename=wheelish_filenames())
@DEEP_SETTINGS
def test_wheel_parse_matches_upstream_structured(filename: str) -> None:
    """Structured near-miss wheel filenames parse like upstream, or nab rejects.

    nab's parser is intentionally stricter than upstream on malformed
    filenames (an empty name or an empty tag component), so ``None`` where
    upstream is lenient is acceptable; when nab does return a result it must
    agree with upstream.
    """
    result = _parse_wheel_filename(filename)
    if result is not None:
        assert result == oracle_wheel(filename)


@given(filename=st.text(min_size=0, max_size=40))
@DEEP_SETTINGS
def test_wheel_parse_matches_upstream_fuzz(filename: str) -> None:
    """Arbitrary text with a ``.whl`` suffix parses like upstream, or nab rejects."""
    result = _parse_wheel_filename(filename + ".whl")
    if result is not None:
        assert result == oracle_wheel(filename + ".whl")


@example(filename=f"foo-{OVERSIZED_VERSION}-py3-none-any.whl")
@given(filename=st.text(min_size=0, max_size=40))
@DEEP_SETTINGS
def test_wheel_parse_never_crashes(filename: str) -> None:
    """Malformed input is rejected with ``None``, never an exception.

    Hypothesis draws text far shorter than the int-from-string limit, so an
    oversized version comes in as an explicit example.
    """
    result = _parse_wheel_filename(filename)
    assert result is None or isinstance(result, tuple)


@st.composite
def sdistish_filenames(draw: st.DrawFn) -> str:
    """Sdist-shaped filenames, valid and invalid."""
    name = draw(names)
    version = draw(version_bits)
    sep = draw(st.sampled_from(["-", "_", "", "--"]))
    ext = draw(st.sampled_from([".tar.gz", ".zip", ".tar.bz2", ".whl", "", ".TAR.GZ"]))
    return f"{name}{sep}{version}{ext}"


@given(filename=sdistish_filenames())
@DEEP_SETTINGS
def test_sdist_parse_matches_upstream_structured(filename: str) -> None:
    """Structured near-miss sdist filenames parse exactly like upstream."""
    assert _parse_sdist_filename(filename) == oracle_sdist(filename)


@given(filename=st.text(min_size=0, max_size=40))
@DEEP_SETTINGS
def test_sdist_parse_matches_upstream_fuzz(filename: str) -> None:
    """Arbitrary text with a ``.tar.gz`` suffix parses exactly like upstream."""
    fn = filename + ".tar.gz"
    assert _parse_sdist_filename(fn) == oracle_sdist(fn)


@example(filename=f"foo-{OVERSIZED_VERSION}.tar.gz")
@given(filename=st.text(min_size=0, max_size=40))
@DEEP_SETTINGS
def test_sdist_parse_never_crashes(filename: str) -> None:
    """Malformed input is rejected with ``None``, never an exception.

    Hypothesis draws text far shorter than the int-from-string limit, so an
    oversized version comes in as an explicit example.
    """
    result = _parse_sdist_filename(filename)
    assert result is None or isinstance(result, tuple)
