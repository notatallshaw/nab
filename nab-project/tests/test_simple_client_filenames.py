"""Corpus tests for nab_index's filename parsers.

``_parse_wheel_filename`` skips the tag-set parse that
:func:`packaging.utils.parse_wheel_filename` runs, and interns the canonical
name and version. Its oracle is nab-provider's vendored packaging, not the
ambient one, because the vendored ``parse_tag`` ranks the wheel afterwards.

Neither parser raises: a filename it cannot read comes back as ``None``.
"""

from __future__ import annotations

import sys

import pytest
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from nab_index.client import (
    _canonical_version,
    _intern_name,
    _parse_sdist_filename,
    _parse_wheel_filename,
)
from nab_provider._vendor.packaging.utils import (
    InvalidWheelFilename,
    parse_wheel_filename,
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
    "-1.0-py3-none-any.whl",
    "foo\n-1.0-py3-none-any.whl",
    # rejected: interpreter that is not an identifier
    "foo-1.0-py3 -none-any.whl",
    "foo-1.0-0-none-any.whl",
    "foo-1.0-py2.0-none-any.whl",
    "foo-1.0-py3.7-none-any.whl",
    "foo-1.0-3.7-none-any.whl",
    "foo-1.0-0-0-0.whl",
    # rejected: invalid version
    "foo-notaversion-py3-none-any.whl",
    # rejected: 5-dash build part not starting with a digit
    "foo-1.0-build-py3-none-any.whl",
]


def _vendored_result(filename: str) -> tuple[str, str] | None:
    """Parse via the vendored packaging; ``None`` when it rejects."""
    try:
        name, version, _build, _tags = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        return None
    return (str(name), str(version))


@pytest.mark.parametrize("filename", CORPUS)
def test_matches_vendored_packaging(filename: str) -> None:
    assert _parse_wheel_filename(filename) == _vendored_result(filename)


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
    """The empty-component rule: the property differential reaches it only sometimes."""
    assert _parse_wheel_filename(filename) is None


NON_IDENTIFIER_INTERPRETER = [
    "foo-1.0-3.7-none-any.whl",
    "foo-1.0-py3.7-none-any.whl",
    "foo-1.0-0-0-0.whl",
    "foo-1.0-py 3-none-any.whl",
    "foo-1.0-1-3.7-none-any.whl",
]


@pytest.mark.parametrize("filename", NON_IDENTIFIER_INTERPRETER)
def test_rejects_non_identifier_interpreter(filename: str) -> None:
    """An interpreter names an implementation and a version, so it is an identifier.

    A compressed set is rejected whole when one member is not, so ``py3.7``
    sits here beside the bare ``3.7``.
    """
    assert _parse_wheel_filename(filename) is None


NON_IDENTIFIER_ABI_OR_PLATFORM = [
    "foo-1.0-py3-0-any.whl",
    "foo-1.0-py3-none-0.whl",
    "foo-1.0-py3-3.7-any.whl",
]


@pytest.mark.parametrize("filename", NON_IDENTIFIER_ABI_OR_PLATFORM)
def test_accepts_non_identifier_abi_and_platform(filename: str) -> None:
    """Only the interpreter field is held to the identifier rule."""
    assert _parse_wheel_filename(filename) == _vendored_result(filename) is not None


def test_rejects_empty_project_name() -> None:
    assert _parse_wheel_filename("-1.0-py3-none-any.whl") is None


def test_rejects_newline_terminated_project_name() -> None:
    """The name pattern ends with ``\\Z``: ``$`` matches before a trailing newline."""
    filename = "foo\n-1.0-py3-none-any.whl"
    assert _parse_wheel_filename(filename) is None
    with pytest.raises(InvalidWheelFilename):
        parse_wheel_filename(filename)


def test_admits_build_tag_packaging_cannot_convert() -> None:
    """``parse_wheel_filename`` calls ``int()`` on the build number.

    A digit run past CPython's limit raises there instead of rejecting. nab
    keeps the wheel: it reads a build tag only to sort by it.
    """
    filename = f"foo-1.0-{OVERSIZED}-py3-none-any.whl"
    assert _parse_wheel_filename(filename) == (canonicalize_name("foo"), "1.0")
    with pytest.raises(ValueError, match="Exceeds the limit"):
        parse_wheel_filename(filename)


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
