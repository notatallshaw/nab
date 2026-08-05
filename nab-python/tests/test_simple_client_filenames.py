"""Differential tests for nab_index's filename parsers.

``_parse_wheel_filename`` reproduces packaging's name/version validation
while skipping the discarded tag-set parse and interning the canonical
version string and the canonical name. These tests assert it
accepts/rejects exactly what packaging does and that the interners return
byte-identical results to the uncached functions, so the optimization stays
output-invariant even if packaging is re-vendored. Neither parser raises: a
filename it cannot read comes back as ``None``.
"""

from __future__ import annotations

import sys

import pytest
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from nab_index.client import (
    _canonical_version,
    _intern_name,
    _parse_sdist_filename,
    _parse_wheel_filename,
)

# Version digit runs at and just past CPython's int-from-string limit.
AT_LIMIT = "1" * sys.get_int_max_str_digits()
OVERSIZED = AT_LIMIT + "1"

CORPUS = [
    # valid, no build tag
    "foo-1.0-py3-none-any.whl",
    "Foo.Bar-2.0.0-py3-none-any.whl",
    "numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl",
    # compressed tag set (multiple tags in one component)
    "wheel-0.1-py2.py3-none-any.whl",
    "pkg-1.0-py3-none-any.whl",
    # valid, with build tag (5 dashes)
    "torch-2.0.1-1-cp311-cp311-linux_x86_64.whl",
    "pkg-1.0-0build-py3-none-any.whl",
    # rejected: wrong extension
    "foo-1.0-py3-none-any.tar.gz",
    "foo-1.0-py3-none-any",
    # rejected: wrong number of parts
    "foo-1.0-py3-none.whl",
    "foo-1.0-1-2-py3-none-any.whl",
    # rejected: invalid project name
    "foo__bar-1.0-py3-none-any.whl",
    "foo bar-1.0-py3-none-any.whl",
    # rejected: invalid version
    "foo-notaversion-py3-none-any.whl",
    # rejected: 5-dash build part not starting with a digit
    "foo-1.0-build-py3-none-any.whl",
]


def _packaging_result(filename: str) -> tuple[str, str] | None:
    try:
        name, version, _build, _tags = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        return None
    return (str(name), str(version))


@pytest.mark.parametrize("filename", CORPUS)
def test_matches_packaging(filename: str) -> None:
    assert _parse_wheel_filename(filename) == _packaging_result(filename)


EMPTY_TAG_COMPONENT = [
    "foo-1.0-py3--any.whl",
    "foo-1.0--none-any.whl",
    "foo-1.0-py3-none-.whl",
    "foo-1.0-py2.-none-any.whl",
    "foo-1.0-py3-none-.x86.whl",
    "foo-1.0-1-py3--any.whl",
    "foo-1.0-2-cp39-cp39-.whl",
]


@pytest.mark.parametrize("filename", EMPTY_TAG_COMPONENT)
def test_rejects_empty_tag_component(filename: str) -> None:
    assert _parse_wheel_filename(filename) is None


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param(f"foo-{OVERSIZED}-py3-none-any.whl", id="release"),
        pytest.param(f"foo-{OVERSIZED}!1.0-py3-none-any.whl", id="epoch"),
        pytest.param(f"foo-1.0.dev{OVERSIZED}-py3-none-any.whl", id="dev"),
        pytest.param(f"foo-1.0.post{OVERSIZED}-py3-none-any.whl", id="post"),
        pytest.param(f"foo-{OVERSIZED}-1-py3-none-any.whl", id="build-tag"),
    ],
)
def test_rejects_oversized_wheel_version(filename: str) -> None:
    assert _parse_wheel_filename(filename) is None


def test_rejects_oversized_sdist_version() -> None:
    assert _parse_sdist_filename(f"foo-{OVERSIZED}.tar.gz") is None


def test_accepts_version_at_int_limit() -> None:
    assert _parse_wheel_filename(f"foo-{AT_LIMIT}-py3-none-any.whl") == (
        canonicalize_name("foo"),
        AT_LIMIT,
    )


def test_canonical_form_and_trailing_zeros() -> None:
    assert _parse_wheel_filename("Foo.Bar-2.0.0-py3-none-any.whl") == (
        canonicalize_name("Foo.Bar"),
        "2.0.0",
    )


@pytest.mark.parametrize("raw", ["1.26.4", "2.0.0", "1.0", "10!2.3.4rc1.post2"])
def test_canonical_version_matches_str_version(raw: str) -> None:
    assert _canonical_version(raw) == str(Version(raw))


def test_canonical_version_reuses_string() -> None:
    first = _canonical_version("3.21.0")
    second = _canonical_version("3.21.0")
    assert first is second


def test_canonical_version_rejects_invalid() -> None:
    with pytest.raises(InvalidVersion):
        _canonical_version("notaversion")
    with pytest.raises(InvalidVersion):
        _canonical_version("notaversion")


@pytest.mark.parametrize("raw", ["Foo.Bar", "foo_bar", "foo-bar", "Tensorflow_CPU"])
def test_intern_name_matches_canonicalize(raw: str) -> None:
    assert _intern_name(raw) == canonicalize_name(raw)


def test_intern_name_reuses_result() -> None:
    first = _intern_name("scikit.learn")
    second = _intern_name("scikit.learn")
    assert first is second
