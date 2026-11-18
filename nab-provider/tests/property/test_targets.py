"""Generated checks for target expansion, platform labels and marker overlays."""

from __future__ import annotations

import os
from itertools import product

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from packaging.markers import default_environment
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from nab_provider._vendor.packaging.markers import Marker
from nab_provider.tags import DEFAULT_LIBC, LIBC_MAJOR, Libc, PlatformSpec
from nab_provider.target import (
    IMPLEMENTATION_MARKERS,
    KNOWN_PYTHON_MINORS,
    PLATFORM_MARKERS,
    Matrix,
    ResolveTarget,
)

pytestmark = pytest.mark.property
PROPERTY_SETTINGS = settings(
    max_examples=1000 if os.environ.get("HYPOTHESIS_PROFILE") == "deep" else 100,
    deadline=None,
)

minors = st.sampled_from(KNOWN_PYTHON_MINORS)
platform_ids = st.sampled_from(sorted(PLATFORM_MARKERS))
implementations = st.sampled_from(sorted(IMPLEMENTATION_MARKERS))
python_specs = st.one_of(
    st.tuples(st.sampled_from(["==", ">=", "~=", "!="]), minors).map("".join),
    st.tuples(minors, minors).map(lambda bounds: f">={bounds[0]}, <={bounds[1]}"),
)
label_values = st.text(alphabet="abc0123456789_.- /", max_size=8)


@given(
    spec=python_specs,
    platforms=st.lists(platform_ids, min_size=1, unique=True),
    selected=st.lists(implementations, min_size=1, unique=True),
    order=st.sampled_from(["asc", "desc"]),
)
@example(
    spec=">=3.11,<=3.12",
    platforms=["macos_arm64", "linux_x86_64"],
    selected=["pypy", "cpython"],
    order="desc",
)
@example(
    spec=">=3.12,<=3.10", platforms=["linux_x86_64"], selected=["pypy"], order="asc"
)
@PROPERTY_SETTINGS
def test_matrix_expands_the_requested_axes(
    spec: str, platforms: list[str], selected: list[str], order: str
) -> None:
    matrix = Matrix(
        python=spec,
        platforms=tuple(map(PlatformSpec, platforms)),
        implementations=tuple(selected),
        python_order=order,
    )
    bounds = SpecifierSet(spec)
    admitted = [
        minor for minor in KNOWN_PYTHON_MINORS if Version(f"{minor}.0") in bounds
    ]
    if not admitted:
        with pytest.raises(ValueError, match="No known Python versions"):
            matrix.expand()
        return
    if order == "desc":
        admitted.reverse()

    expected = list(product(admitted, platforms, selected))
    targets = matrix.expand()
    assert [
        (t.python_version, t.platform_id, t.implementation) for t in targets
    ] == expected
    assert len({t.label for t in targets}) == len(expected)
    assert [(t.label, t.marker_env) for t in matrix.expand()] == [
        (t.label, t.marker_env) for t in targets
    ]

    systems = {"linux": "linux", "windows": "win32", "macos": "darwin"}
    required = default_environment().keys()
    for target, (minor, platform, implementation) in zip(
        targets, expected, strict=True
    ):
        env = target.marker_env
        assert env.keys() >= required
        assert Marker(f'python_version == "{minor}"').evaluate(env)
        assert env["implementation_name"] == implementation
        expected_system = systems[platform.partition("_")[0]]
        for system in systems.values():
            assert Marker(f'sys_platform == "{system}"').evaluate(env) is (
                system == expected_system
            )


@given(
    order=st.text(min_size=1, max_size=10).filter(
        lambda value: value not in {"asc", "desc"}
    )
)
@PROPERTY_SETTINGS
def test_unknown_python_order_raises(order: str) -> None:
    with pytest.raises(ValueError, match="python_order"):
        Matrix("==3.11", (PlatformSpec("linux_x86_64"),), python_order=order).expand()


@st.composite
def platform_specs(draw: st.DrawFn, platform: str) -> PlatformSpec:
    libc: Libc = DEFAULT_LIBC
    libc_version = None
    macos = None
    if platform.startswith("linux_"):
        libc = draw(st.sampled_from(sorted(LIBC_MAJOR)))
        libc_version = draw(
            st.none() | st.tuples(st.just(LIBC_MAJOR[libc]), st.integers(0, 20))
        )
    elif platform.startswith("macos_"):
        versions = st.tuples(st.integers(11, 15), st.integers(0, 3))
        if platform == "macos_x86_64":
            versions |= st.tuples(st.just(10), st.integers(4, 15))
        macos = draw(st.none() | versions)
    return PlatformSpec(
        platform,
        libc=libc,
        runs_on_libc=libc_version,
        runs_on_macos=macos,
        platform_release=draw(label_values),
        platform_version=draw(label_values),
        free_threaded=draw(st.booleans()),
        pypy_abi=draw(st.sampled_from([None, "pp73", "pp80"])),
    )


@st.composite
def spec_pairs(draw: st.DrawFn) -> tuple[PlatformSpec, PlatformSpec]:
    platform = draw(platform_ids)
    return draw(platform_specs(platform)), draw(platform_specs(platform))


@given(pair=spec_pairs())
@example(
    pair=(
        PlatformSpec("linux_x86_64", pypy_abi="pp73"),
        PlatformSpec("linux_x86_64", pypy_abi="pp80"),
    )
)
@example(
    pair=(
        PlatformSpec("linux_x86_64", platform_release="x-very"),
        PlatformSpec("linux_x86_64", platform_release="x", platform_version="y"),
    )
)
@PROPERTY_SETTINGS
def test_different_platform_specs_have_different_labels(
    pair: tuple[PlatformSpec, PlatformSpec],
) -> None:
    first, second = pair
    assert (first.label == second.label) is (first == second)


@given(minor=minors, patch=st.integers(0, 20))
@PROPERTY_SETTINGS
def test_patch_pin_sets_the_cpython_full_version(minor: str, patch: int) -> None:
    full = f"{minor}.{patch}"
    matrix = Matrix(
        f"=={minor}", (PlatformSpec("linux_x86_64"),), python_patches={minor: full}
    )
    (target,) = matrix.expand()
    assert target.python_version == minor
    assert (
        target.python_full_version
        == target.marker_env["implementation_version"]
        == full
    )


_OVERLAY_VALUES = {
    "os_name": ("posix", "nt"),
    "sys_platform": ("linux", "darwin", "win32"),
    "platform_machine": ("x86_64", "arm64", "AMD64"),
    "platform_python_implementation": ("CPython", "PyPy"),
    "platform_release": ("", "6.8.0", "23.0"),
    "platform_system": ("Linux", "Darwin", "Windows"),
    "platform_version": ("", "1", "2"),
    "implementation_name": ("cpython", "pypy"),
}


@st.composite
def disjoint_overlays(draw: st.DrawFn) -> tuple[dict[str, str], dict[str, str]]:
    keys = draw(
        st.lists(st.sampled_from(tuple(_OVERLAY_VALUES)), min_size=2, unique=True)
    )
    middle = draw(st.integers(1, len(keys) - 1))
    return (
        {k: draw(st.sampled_from(_OVERLAY_VALUES[k])) for k in keys[:middle]},
        {k: draw(st.sampled_from(_OVERLAY_VALUES[k])) for k in keys[middle:]},
    )


@given(minor=minors, patch=st.integers(0, 12), layers=disjoint_overlays())
@example(
    minor="3.11",
    patch=1,
    layers=({"platform_release": "custom"}, {"platform_version": "test"}),
)
@PROPERTY_SETTINGS
def test_disjoint_marker_overrides_preserve_both_layers(
    minor: str, patch: int, layers: tuple[dict[str, str], dict[str, str]]
) -> None:
    base = ResolveTarget.for_host_python(f"{minor}.{patch}")
    original = dict(base.marker_env)
    assert base.with_marker_overrides({}) is base
    first, second = layers
    expected = {**original, **first, **second}
    if (
        original["implementation_name"] != "cpython"
        and expected["implementation_name"] == "cpython"
    ):
        expected["implementation_version"] = base.python_full_version

    for left, right in ((first, second), (second, first)):
        target = base.with_marker_overrides(left).with_marker_overrides(right)
        assert target.marker_env == expected
        assert target.tags is base.tags
    assert base.marker_env == original
