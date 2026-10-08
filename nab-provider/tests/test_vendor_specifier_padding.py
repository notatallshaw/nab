"""Specifier padding and invalid-input diagnostics."""

from __future__ import annotations

import re

import pytest

from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import InvalidSpecifier, Specifier

# Whitespace padding accepted around the operator and version.
PADDED = [
    (" ==1.0", ("==", "1.0")),
    ("==1.0 ", ("==", "1.0")),
    ("\t==1.0\n", ("==", "1.0")),
    ("\x0b==1.0\x0c", ("==", "1.0")),
    ("\xa0==1.0\xa0", ("==", "1.0")),
    ("  ===  ", ("===", "")),
]

# Strings the pattern refuses however they are padded.
REFUSED = ["", " ", "=1.0", "== ", "==1.0 2.0", "~=1"]


@pytest.mark.parametrize(("spec", "expected"), PADDED)
def test_padding_preserves_operator_and_version(
    spec: str, expected: tuple[str, str]
) -> None:
    parsed = Specifier(spec)

    assert (parsed.operator, parsed.version) == expected


@pytest.mark.parametrize("spec", REFUSED)
def test_a_refused_specifier_is_reported_with_its_padding(spec: str) -> None:
    padded = f" {spec}\t"
    message = re.escape(f"Invalid specifier: {padded!r}")

    with pytest.raises(InvalidSpecifier, match=message):
        Specifier(padded)


def test_the_tokenizer_reads_a_specifier_out_of_a_requirement() -> None:
    requirement = Requirement('foo >=1.0,<2.0; python_version >= "3.9"')

    assert str(requirement.specifier) == "<2.0,>=1.0"
    assert str(requirement.marker) == 'python_version >= "3.9"'
