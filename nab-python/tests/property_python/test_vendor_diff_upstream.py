"""Differential: vendored range-backed specifiers vs upstream packaging.

The vendored copy rewires ``Specifier``/``SpecifierSet`` contains and
filter through the ``VersionRange`` machinery; upstream implements the
same PEP 440 semantics directly, so it is an oracle.  Any divergence on
contains/filter for valid inputs is a behavioral bug in the range
machinery (or an intentional upstream fix, to be checked against
PEP 440).
"""

from __future__ import annotations

import packaging.specifiers as up_spec
import pytest
from hypothesis import given

from nab_python._vendor.packaging import specifiers as nab_spec

from .strategies import DEEP_SETTINGS
from .vendor_strategies import probe_lists, specifier_set_strings, specifier_strings

pytestmark = pytest.mark.property

POLICIES = (None, True, False)


def _build_pair(
    spec_str: str,
) -> tuple[up_spec.SpecifierSet | None, nab_spec.SpecifierSet | None]:
    try:
        upstream = up_spec.SpecifierSet(spec_str)
    except up_spec.InvalidSpecifier:
        upstream = None
    try:
        vendored = nab_spec.SpecifierSet(spec_str)
    except nab_spec.InvalidSpecifier:
        vendored = None
    assert (upstream is None) == (vendored is None), (
        f"validity disagreement for {spec_str!r}: "
        f"upstream={'rejects' if upstream is None else 'accepts'}"
    )
    return upstream, vendored


@DEEP_SETTINGS
@given(spec=specifier_strings(), probes=probe_lists())
def test_single_specifier_contains_agrees(spec: str, probes: list[str]) -> None:
    try:
        upstream = up_spec.Specifier(spec)
    except up_spec.InvalidSpecifier:
        upstream = None
    try:
        vendored = nab_spec.Specifier(spec)
    except nab_spec.InvalidSpecifier:
        vendored = None
    assert (upstream is None) == (vendored is None), f"validity: {spec!r}"
    if upstream is None or vendored is None:
        return
    assert upstream.prereleases == vendored.prereleases, f"prereleases: {spec!r}"
    for probe in probes:
        for policy in POLICIES:
            up_result = upstream.contains(probe, prereleases=policy)
            nab_result = vendored.contains(probe, prereleases=policy)
            assert up_result == nab_result, (
                f"spec={spec!r} probe={probe!r} prereleases={policy!r}: "
                f"upstream={up_result} vendored={nab_result}"
            )


@DEEP_SETTINGS
@given(spec_set=specifier_set_strings(), probes=probe_lists())
def test_specifier_set_contains_agrees(spec_set: str, probes: list[str]) -> None:
    upstream, vendored = _build_pair(spec_set)
    if upstream is None or vendored is None:
        return
    assert upstream.prereleases == vendored.prereleases, f"prereleases: {spec_set!r}"
    for probe in probes:
        for policy in POLICIES:
            up_result = upstream.contains(probe, prereleases=policy)
            nab_result = vendored.contains(probe, prereleases=policy)
            assert up_result == nab_result, (
                f"set={spec_set!r} probe={probe!r} prereleases={policy!r}: "
                f"upstream={up_result} vendored={nab_result}"
            )


@DEEP_SETTINGS
@given(spec_set=specifier_set_strings(), probes=probe_lists(min_size=6, max_size=12))
def test_specifier_set_filter_agrees(spec_set: str, probes: list[str]) -> None:
    upstream, vendored = _build_pair(spec_set)
    if upstream is None or vendored is None:
        return
    for policy in POLICIES:
        up_result = list(upstream.filter(probes, prereleases=policy))
        nab_result = list(vendored.filter(probes, prereleases=policy))
        assert up_result == nab_result, (
            f"set={spec_set!r} probes={probes!r} prereleases={policy!r}: "
            f"upstream={up_result} vendored={nab_result}"
        )


@DEEP_SETTINGS
@given(spec_set=specifier_set_strings(), probes=probe_lists())
def test_specifier_set_in_operator_agrees(spec_set: str, probes: list[str]) -> None:
    upstream, vendored = _build_pair(spec_set)
    if upstream is None or vendored is None:
        return
    for probe in probes:
        assert (probe in upstream) == (probe in vendored), (
            f"set={spec_set!r} probe={probe!r}: "
            f"upstream={probe in upstream} vendored={probe in vendored}"
        )
