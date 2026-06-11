"""Metamorphic and canonical-form properties on the vendored range layer.

* Trailing-zero padding of versions in non-wildcard specifiers must not
  change the range (PEP 440: release segments compare zero-padded).
* PEP 440 defines ``~= V`` as ``>= V, == prefix.*``; both spellings
  must produce the same range.
* Unsatisfiable sets must equal the canonical empty range.
* A single ``Specifier`` and the singleton ``SpecifierSet`` must agree.
* ``==V`` and ``!=V`` must partition the PEP 440 version universe.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.specifiers import Specifier, SpecifierSet
from nab_python._vendor.packaging.version import InvalidVersion, Version

from .strategies import DEEP_SETTINGS
from .vendor_strategies import (
    DEVS,
    EPOCHS,
    POSTS,
    PRES,
    probe_lists,
    releases,
    specifier_strings,
    version_strings,
    version_strings_no_local,
)

pytestmark = pytest.mark.property

PADDABLE_OPS = st.sampled_from(["==", "!=", ">=", "<=", ">", "<"])


@DEEP_SETTINGS
@given(
    op=PADDABLE_OPS,
    version=version_strings_no_local(),
    pad=st.integers(1, 3),
    probes=probe_lists(),
)
def test_trailing_zero_padding_is_identity(
    op: str, version: str, pad: int, probes: list[str]
) -> None:
    # "1.2rc1.post0" -> pad release only; splitting at the first non-digit
    # non-dot character keeps suffixes intact.
    head_end = 0
    while head_end < len(version) and (
        version[head_end].isdigit() or version[head_end] in ".!"
    ):
        head_end += 1
    head, tail = version[:head_end], version[head_end:]
    if head.endswith("."):
        return
    padded = head + ".0" * pad + tail
    base_range = SpecifierSet(op + version).to_range()
    padded_range = SpecifierSet(op + padded).to_range()
    assert base_range == padded_range, (
        f"padding changed the range: {op + version!r} vs {op + padded!r}: "
        f"{base_range!r} vs {padded_range!r}"
    )
    for probe in probes:
        assert base_range.contains(probe, prereleases=True) == padded_range.contains(
            probe, prereleases=True
        ), f"padding membership: {op + version!r} vs {op + padded!r} probe={probe!r}"


@DEEP_SETTINGS
@given(
    epoch=EPOCHS,
    release=releases(max_segments=3),
    pre=PRES,
    post=POSTS,
    dev=DEVS,
    probes=probe_lists(),
)
def test_compatible_release_equals_pep440_definition(
    epoch: str, release: str, pre: str, post: str, dev: str, probes: list[str]
) -> None:
    if "." not in release:
        release += ".0"
    version = epoch + release + pre + post + dev
    prefix = epoch + release.rsplit(".", 1)[0]
    tilde = SpecifierSet(f"~={version}").to_range()
    spelled = SpecifierSet(f">={version},=={prefix}.*").to_range()
    assert tilde == spelled, (
        f"~={version} != '>={version},=={prefix}.*': {tilde!r} vs {spelled!r}"
    )
    for probe in probes:
        assert tilde.contains(probe, prereleases=True) == spelled.contains(
            probe, prereleases=True
        ), f"~= membership: version={version!r} probe={probe!r}"


@DEEP_SETTINGS
@given(version=version_strings_no_local())
def test_unsatisfiable_set_is_canonical_empty(version: str) -> None:
    rng = SpecifierSet(f">{version},<{version}").to_range()
    assert rng.is_empty
    assert rng == VersionRange.empty(), (
        f">{version},<{version} -> {rng!r}, expected canonical empty"
    )


@DEEP_SETTINGS
@given(spec=specifier_strings())
def test_specifier_and_singleton_set_agree(spec: str) -> None:
    single = VersionRange.from_specifier(Specifier(spec))
    as_set = SpecifierSet(spec).to_range()
    assert single == as_set, f"{spec!r}: {single!r} vs {as_set!r}"


@DEEP_SETTINGS
@given(version=version_strings(), probes=probe_lists())
def test_eq_neq_partition(version: str, probes: list[str]) -> None:
    eq_range = SpecifierSet(f"=={version}").to_range()
    neq_range = SpecifierSet(f"!={version}").to_range()
    assert (eq_range & neq_range).is_empty, f"=={version} & !={version} not empty"
    assert ~eq_range == neq_range, (
        f"~(=={version}) != (!={version}): {~eq_range!r} vs {neq_range!r}"
    )
    union = eq_range | neq_range
    pep440_full = VersionRange.full(admit_arbitrary=False)
    for probe in probes:
        try:
            Version(probe)
        except InvalidVersion:
            continue
        if pep440_full.contains(probe, prereleases=True):
            assert union.contains(probe, prereleases=True), (
                f"== | != must cover every version: version={version!r} probe={probe!r}"
            )
