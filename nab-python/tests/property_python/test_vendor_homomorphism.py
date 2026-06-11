"""Homomorphism and soundness extras on the vendored range layer.

* ``SpecifierSet`` concatenation must map to range intersection.
* ``Specifier.filter`` (single-spec path) differential vs upstream.
* ``is_prerelease_only`` soundness: a range that contains any final
  version must not claim to be prerelease-only.
* ``is_unsatisfiable`` soundness: an unsatisfiable set matches nothing.
"""

from __future__ import annotations

import packaging.specifiers as up_spec
import pytest
from hypothesis import given

from nab_python._vendor.packaging import specifiers as nab_spec
from nab_python._vendor.packaging.version import InvalidVersion, Version

from .strategies import DEEP_SETTINGS
from .vendor_strategies import probe_lists, specifier_set_strings, specifier_strings

pytestmark = pytest.mark.property

POLICIES = (None, True, False)


@DEEP_SETTINGS
@given(a=specifier_set_strings(), b=specifier_set_strings(), probes=probe_lists())
def test_intersection_homomorphism(a: str, b: str, probes: list[str]) -> None:
    combined = nab_spec.SpecifierSet(f"{a},{b}").to_range()
    folded = nab_spec.SpecifierSet(a).to_range() & nab_spec.SpecifierSet(b).to_range()
    assert combined == folded, (
        f"to_range({a!r},{b!r}) != to_range({a!r}) & to_range({b!r}): "
        f"{combined!r} vs {folded!r}"
    )
    for probe in probes:
        assert combined.contains(probe, prereleases=True) == folded.contains(
            probe, prereleases=True
        ), f"membership: a={a!r} b={b!r} probe={probe!r}"


@DEEP_SETTINGS
@given(spec=specifier_strings(), probes=probe_lists(min_size=6, max_size=12))
def test_single_specifier_filter_agrees_with_upstream(
    spec: str, probes: list[str]
) -> None:
    try:
        upstream = up_spec.Specifier(spec)
    except up_spec.InvalidSpecifier:
        return
    vendored = nab_spec.Specifier(spec)
    for policy in POLICIES:
        up_result = list(upstream.filter(probes, prereleases=policy))
        nab_result = list(vendored.filter(probes, prereleases=policy))
        assert up_result == nab_result, (
            f"spec={spec!r} probes={probes!r} prereleases={policy!r}: "
            f"upstream={up_result} vendored={nab_result}"
        )


@DEEP_SETTINGS
@given(spec_set=specifier_set_strings(), probes=probe_lists())
def test_is_prerelease_only_soundness(spec_set: str, probes: list[str]) -> None:
    spec = nab_spec.SpecifierSet(spec_set)
    rng = spec.to_range()
    if not rng.is_prerelease_only:
        return
    for probe in probes:
        try:
            parsed = Version(probe)
        except InvalidVersion:
            continue
        if parsed.is_prerelease:
            continue
        assert not rng.contains(probe, prereleases=True), (
            f"is_prerelease_only range contains final {probe!r}: "
            f"set={spec_set!r} range={rng!r}"
        )


@DEEP_SETTINGS
@given(spec_set=specifier_set_strings(), probes=probe_lists())
def test_is_unsatisfiable_soundness(spec_set: str, probes: list[str]) -> None:
    spec = nab_spec.SpecifierSet(spec_set)
    if not spec.is_unsatisfiable():
        return
    for probe in probes:
        assert not spec.contains(probe), (
            f"unsatisfiable set matched {probe!r}: set={spec_set!r}"
        )
