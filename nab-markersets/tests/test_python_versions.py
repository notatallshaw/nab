"""Python environment domains, including comparison bounds outside them."""

from __future__ import annotations

import pytest
from packaging.markers import Marker

from nab_markersets.markersets import MarkerSet
from nab_markersets.python_versions import validate_python_version


@pytest.mark.parametrize(
    "value",
    ["1!3.12.0", "3.12.0.post0", "3.12.0.1", "1!3.12.0+", "3.12.0.post0+", "3.12.0.1+"],
)
@pytest.mark.parametrize("method", ["evaluate", "restrict"])
def test_unsupported_python_environment_is_rejected(value: str, method: str) -> None:
    marker = MarkerSet.from_marker('python_full_version >= "3.10"')
    env = {"python_full_version": value}
    with pytest.raises(ValueError, match="python_full_version"):
        getattr(marker, method)(env)
    assert env == {"python_full_version": value}


@pytest.mark.parametrize(
    "value", ["1!3.12", "3.12.post0", "3.12+local", "3.12.0", "3.12rc1", "3.12.dev0"]
)
def test_minor_marker_value_has_no_qualifiers(value: str) -> None:
    with pytest.raises(ValueError, match="python_version"):
        validate_python_version(value, variable="python_version")


@pytest.mark.parametrize(
    "value",
    [
        "3",
        "3.12",
        "3.12.0",
        "3.12rc1",
        "3.12.0a1",
        "3.12.0b2",
        "3.12.0.dev0",
        "3.12.0a1.dev1",
        "0!3.12.0",
        "3.12.0+local",
        "3.15.0a8+dev",
    ],
)
def test_supported_python_targets(value: str) -> None:
    validate_python_version(value)
    marker = MarkerSet.from_marker(f'python_full_version == "{value}"')
    assert marker.evaluate({"python_full_version": value})
    witness = marker.witness()
    assert witness is not None
    full = witness["python_full_version"]
    assert isinstance(full, str)
    validate_python_version(full)


@pytest.mark.parametrize(
    ("text", "empty"),
    [
        ('python_full_version >= "1!0"', True),
        ('python_version == "1!3.12"', True),
        ('python_full_version == "3.12.0.post0"', True),
        ('python_full_version == "3.12.0+local"', False),
        ('python_full_version == "3.12.0.1"', True),
        ('python_full_version > "3.12.0" and python_full_version < "3.12.1"', True),
        ('python_full_version == "3.12.0.0"', False),
        (
            'python_full_version > "3.12.0.post1" and python_full_version < "3.12.2"',
            False,
        ),
        ('"3.12+local" == python_full_version', False),
        ('python_full_version < "1!0"', False),
        ('python_full_version in "1!3.12.0 3.13.1+local"', False),
    ],
)
def test_comparison_literals_keep_pep440_meaning(text: str, *, empty: bool) -> None:
    marker = MarkerSet.from_marker(text)
    assert marker.is_empty() is empty
    witness = marker.witness()
    if empty:
        assert witness is None
    else:
        assert witness is not None
        assert Marker(text).evaluate(witness)
        for variable in ("python_version", "python_full_version"):
            value = witness[variable]
            assert isinstance(value, str)
            validate_python_version(value, variable=variable)


def test_python_domain_does_not_restrict_implementation_versions() -> None:
    for value in ("1!3.12", "3.12.post1", "3.12+local", "3.12.0.1"):
        marker = MarkerSet.from_marker(f'implementation_version == "{value}"')
        assert marker.evaluate({"implementation_version": value})
        assert not marker.is_empty()


@pytest.mark.parametrize("literal", ["1!", "+local", ".post", "3.12.0.1"])
def test_opaque_substring_cannot_return_an_unsupported_python_witness(
    literal: str,
) -> None:
    marker = MarkerSet.from_marker(f'"{literal}" in python_full_version')
    assert marker.witness() is None


def test_source_build_local_label_is_a_valid_python_witness() -> None:
    marker = MarkerSet.from_marker('"3.15.0a8+dev" in python_full_version')
    env = marker.witness()
    assert env == {"python_full_version": "3.15.0a8+dev", "python_version": "3.15"}
    assert marker.evaluate(env)
