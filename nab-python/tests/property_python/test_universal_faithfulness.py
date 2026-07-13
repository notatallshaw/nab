"""End-to-end property tests for per-tuple lockfile emission.

`PEP 751`_ says all packages to be installed MUST narrow down to a
single entry at install time, and recommends consistent output to
minimize diff noise.  For a universal lock built from per-tuple pins
that means three end-to-end guarantees:

1. Faithfulness: for every declared tuple and every package name,
   the emitted ``[[packages]]`` entries select exactly the version
   that tuple pinned (one matching entry), and zero entries for
   names the tuple did not pin.  Nothing silently dropped, nothing
   leaking into other tuples.
2. Byte-stability: shuffling every insertion-ordered input (tuple
   order, per-name order, wheel order) yields byte-identical output.
3. Fixed point: emit -> parse -> emit through the vendored
   ``packaging.pylock`` round-trip reproduces the same bytes.

.. _PEP 751: https://peps.python.org/pep-0751/#packages
"""

from __future__ import annotations

import sys
from typing import NamedTuple

import pytest
import tomli_w
from hypothesis import given
from hypothesis import strategies as st

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

from nab_python._vendor.packaging.pylock import Pylock
from nab_python.lockfile import (
    IndexPin,
    LockInput,
    PinShape,
    TargetLock,
    WheelArtifact,
    write_lock,
)
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property

PLATFORMS = ("linux_x86_64", "windows_amd64", "macos_arm64")
PYTHONS = ("3.10", "3.11")
NAMES = ("aa", "bb", "cc")
VERSIONS = ("1.0", "2.0", "3.0")

WHEEL_TAGS = (
    "py3-none-any",
    "cp310-cp310-manylinux_2_17_x86_64",
    "cp311-cp311-win_amd64",
)


class UniversalCase(NamedTuple):
    """One generated universal-lock scenario."""

    targets: list[ResolveTarget]
    pinned: dict[str, dict[str, str]]
    n_wheels: int


def _wheels(
    name: str, version: str, tags: tuple[str, ...]
) -> tuple[WheelArtifact, ...]:
    """Build one wheel per tag for a pin."""
    return tuple(
        WheelArtifact(
            filename=f"{name}-{version}-{tag}.whl",
            url=f"https://example.com/{name}-{version}-{tag}.whl",
            hashes=(("sha256", "a" * 64), ("sha512", "b" * 128)),
            size=1,
        )
        for tag in tags
    )


@st.composite
def universal_cases(draw: st.DrawFn) -> UniversalCase:
    """Generate 1-4 tuples, each pinning 1-3 names at random versions."""
    combos = draw(
        st.lists(
            st.tuples(st.sampled_from(PLATFORMS), st.sampled_from(PYTHONS)),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    targets = [
        ResolveTarget.for_declared(python_version=py, spec=PlatformSpec(plat))
        for plat, py in combos
    ]
    pinned: dict[str, dict[str, str]] = {}
    for target in targets:
        names = draw(
            st.lists(st.sampled_from(NAMES), min_size=1, max_size=3, unique=True)
        )
        pinned[target.label] = {name: draw(st.sampled_from(VERSIONS)) for name in names}
    n_wheels = draw(st.integers(min_value=1, max_value=3))
    return UniversalCase(targets, pinned, n_wheels)


def _build_input(
    case: UniversalCase,
    *,
    target_order: list[ResolveTarget] | None = None,
    name_shift: int = 0,
    wheel_shift: int = 0,
) -> LockInput:
    """Assemble a per-tuple ``LockInput``, optionally rotating each axis."""
    ordered = target_order if target_order is not None else case.targets
    targets: dict[str, TargetLock] = {}
    for target in ordered:
        names = list(case.pinned[target.label])
        shift = name_shift % len(names)
        names = names[shift:] + names[:shift]
        per_name: dict[str, PinShape] = {}
        for name in names:
            version = case.pinned[target.label][name]
            wheels = list(_wheels(name, version, WHEEL_TAGS[: case.n_wheels]))
            shift = wheel_shift % len(wheels)
            wheels = wheels[shift:] + wheels[:shift]
            per_name[name] = IndexPin(
                name=name,
                version=version,
                index="https://pypi.org/simple/",
                wheels=tuple(wheels),
            )
        targets[target.label] = TargetLock(target=target, pins=per_name)
    return LockInput(targets=targets)


class TestQuotePerTupleFaithfulness:
    """PEP 751, § ``[[packages]]``:

    > "Packages MAY be listed multiple times with varying data, but
    > all packages to be installed MUST narrow down to a single entry
    > at install time."

    Installing under a tuple's environment must select exactly the
    pins that tuple's resolve produced: one firing entry per pinned
    name at the pinned version, zero firing entries for unpinned
    names.  A miss here silently under- or over-installs.

    Reference: https://peps.python.org/pep-0751/#packages
    """

    @given(case=universal_cases())
    @PROPERTY_SETTINGS
    def test_per_tuple_faithfulness(self, case: UniversalCase) -> None:
        """Each tuple's environment selects exactly that tuple's pins."""
        text = write_lock(_build_input(case))
        pylock = Pylock.from_dict(tomllib.loads(text))
        for target in case.targets:
            label = target.label
            env = dict(target.marker_env)
            for name in NAMES:
                matching = [
                    p
                    for p in pylock.packages
                    if str(p.name) == name
                    and (
                        p.marker is None
                        or p.marker.evaluate(dict(env), context="lock_file")
                    )
                ]
                if name in case.pinned[label]:
                    assert len(matching) == 1, (
                        f"{name} under {label}: {len(matching)} entries fire "
                        f"(want 1)\n{text}"
                    )
                    assert str(matching[0].version) == case.pinned[label][name]
                else:
                    assert not matching, (
                        f"{name} not pinned for {label} but "
                        f"{len(matching)} entries fire\n{text}"
                    )


class TestQuoteByteStabilityUnderShuffle:
    """PEP 751, § File Format:

    > "Tools SHOULD write their lock files in a consistent way to
    > minimize noise in diff output. ... As well, tools SHOULD sort
    > arrays in consistent order."

    Rotating every insertion-ordered input axis (tuple order, the
    per-name map order, each pin's wheel order) must produce
    byte-identical output; otherwise a re-resolve that populates its
    dicts in a different order churns the lockfile.

    Reference: https://peps.python.org/pep-0751/#file-format
    """

    @given(
        case=universal_cases(),
        name_shift=st.integers(min_value=0, max_value=2),
        wheel_shift=st.integers(min_value=0, max_value=2),
        reverse_labels=st.booleans(),
    )
    @PROPERTY_SETTINGS
    def test_byte_stability_under_input_shuffle(
        self,
        case: UniversalCase,
        name_shift: int,
        wheel_shift: int,
        reverse_labels: bool,
    ) -> None:
        """Shuffled-but-equivalent input emits byte-identical output."""
        canonical = write_lock(_build_input(case))
        target_order = list(reversed(case.targets)) if reverse_labels else case.targets
        shuffled = write_lock(
            _build_input(
                case,
                target_order=target_order,
                name_shift=name_shift,
                wheel_shift=wheel_shift,
            )
        )
        assert shuffled == canonical


class TestEmitParseEmitFixedPoint:
    """Emitted text parsed back through the vendored
    ``packaging.pylock`` and re-serialised must reproduce the same
    bytes.  A drift means the writer and the spec parser disagree
    about some field's canonical form, so a consumer that round-trips
    the file (audit tooling, ``nab lock`` re-runs) would churn it.
    """

    @given(case=universal_cases())
    @PROPERTY_SETTINGS
    def test_emit_parse_emit_fixed_point(self, case: UniversalCase) -> None:
        """``write_lock`` output is a fixed point of parse + re-serialise."""
        text = write_lock(_build_input(case))
        reparsed = Pylock.from_dict(tomllib.loads(text))
        assert tomli_w.dumps(dict(reparsed.to_dict())) == text
