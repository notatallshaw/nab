"""Differential tests for nab_index's wheel-filename parser.

``_parse_wheel_filename`` reproduces packaging's name/version validation
while skipping the discarded tag-set parse and interning the version, its
canonical string, and the canonical name. These tests assert it
accepts/rejects exactly what packaging does and that the interners return
byte-identical results to the uncached functions, so the optimization stays
output-invariant even if packaging is re-vendored.
"""

from __future__ import annotations

import pytest
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion

from nab_index.client import (
    _canonical_version,
    _intern_name,
    _intern_version,
    _parse_wheel_filename,
)

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


def test_canonical_form_and_trailing_zeros() -> None:
    assert _parse_wheel_filename("Foo.Bar-2.0.0-py3-none-any.whl") == (
        canonicalize_name("Foo.Bar"),
        "2.0.0",
    )


def test_intern_reuses_version_object() -> None:
    first = _intern_version("9.99.123")
    second = _intern_version("9.99.123")
    assert first is second


@pytest.mark.parametrize("raw", ["1.26.4", "2.0.0", "1.0", "10!2.3.4rc1.post2"])
def test_canonical_version_matches_str_version(raw: str) -> None:
    assert _canonical_version(raw) == str(_intern_version(raw))


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
