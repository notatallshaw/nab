"""Tests for the vendored patch that shares one compiled specifier pattern.

The tokenizer rule is ``Specifier._regex`` itself, and that pattern matches no
surrounding whitespace, so ``Specifier.__init__`` strips before matching. Both
are pinned: ``re`` caches on the pattern text, so the identity assertion alone
would still hold if the tokenizer compiled the string a second time.
"""

from __future__ import annotations

import re

import pytest

from nab_provider._vendor.packaging._tokenizer import DEFAULT_RULES
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import InvalidSpecifier, Specifier

# Padding ``Specifier`` accepts, with the operator and version it parses to.
# ``str.strip()`` and ``\s`` have to accept the same characters, hence ``\xa0``.
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


def test_the_tokenizer_rule_is_the_specifier_pattern_object() -> None:
    assert DEFAULT_RULES["SPECIFIER"] is Specifier._regex


def test_the_pattern_carries_no_surrounding_whitespace() -> None:
    assert Specifier._regex.pattern == Specifier._specifier_regex_str


@pytest.mark.parametrize(("spec", "expected"), PADDED)
def test_padding_is_stripped_before_the_operator_split(
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
