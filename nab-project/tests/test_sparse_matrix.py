"""Resolve sparse interpreter targets and select their merged lock."""

from __future__ import annotations

import pytest

from nab_index.client import WheelFile
from nab_project._testing.coordinator_fake import FakeFetchPort, make_coordinator
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import build_pylock
from nab_project.resolve import build_lock_input, resolve_with_coordinator
from nab_provider._vendor.packaging.pylock import PackageWheel, PylockSelectError
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.tags import Tag
from nab_provider.provider import BuildPolicy
from nab_provider.tags import PlatformSpec
from nab_provider.target import ImplementationOverride, Matrix, ResolveTarget
from nab_provider.testing import make_metadata, make_wheel

HASHES = (("sha256", "a" * 64),)


def _interpreter_graph() -> FakeFetchPort:
    """Serve divergent interpreter pins and independent platform dependencies."""
    listings = {
        "base": [make_wheel("1.0", package="base", hashes=HASHES)],
        "variant": [
            make_wheel("1.0", package="variant", hashes=HASHES),
            make_wheel("2.0", package="variant", hashes=HASHES),
        ],
        "pypydep": [make_wheel("1.0", package="pypydep", hashes=HASHES)],
        "windowsdep": [make_wheel("1.0", package="windowsdep", hashes=HASHES)],
    }
    requires = {
        "base": (
            'variant==1.0 ; implementation_name == "cpython"',
            'variant==2.0 ; implementation_name == "pypy"',
            'pypydep ; implementation_name == "pypy"',
            'windowsdep ; sys_platform == "win32"',
        )
    }
    return _graph(listings, requires)


def _graph(
    listings: dict[str, list[WheelFile]], requires: dict[str, tuple[str, ...]]
) -> FakeFetchPort:
    """Serve each wheel's metadata with its package's declared dependencies."""
    metadata = {}
    for name, wheels in listings.items():
        for wheel in wheels:
            assert wheel.metadata_url is not None
            metadata[wheel.metadata_url] = (
                make_metadata(name, wheel.version, *requires.get(name, ())) + "\n"
            )
    return make_coordinator(listings=listings, metadata_by_url=metadata)


@pytest.mark.parametrize("cpython_range", [">=3.10,<3.16", ">=3.12,<3.16"])
def test_sparse_lock_selects_only_declared_interpreters(cpython_range: str) -> None:
    """Merged pins and support markers preserve the missing PyPy corners."""
    targets = Matrix(
        python=cpython_range,
        platforms=(PlatformSpec("linux_x86_64"), PlatformSpec("windows_amd64")),
        implementations=("cpython", "pypy"),
        implementation_overrides={"pypy": ImplementationOverride(python="==3.11.*")},
    ).expand()
    inputs = ResolveInputs(build_policy=BuildPolicy.NEVER)
    result = resolve_with_coordinator(
        _interpreter_graph(), targets, [Requirement("base")], inputs=inputs
    )

    assert result.success
    assert len(result.target_results) == len(targets)
    pylock = build_pylock(build_lock_input(result, inputs=inputs))
    pylock.validate()

    for target in targets:
        selected = {
            str(package.name): str(package.version)
            for package, _ in pylock.select(
                environment=target.marker_env, tags=[Tag("py3", "none", "any")]
            )
        }
        expected = {
            "base": "1.0",
            "variant": "2.0" if target.implementation == "pypy" else "1.0",
        }
        if target.implementation == "pypy":
            expected["pypydep"] = "1.0"
        assert target.platform_spec is not None
        if target.platform_spec.platform_id == "windows_amd64":
            expected["windowsdep"] = "1.0"
        assert selected == expected

    for version in ("3.10", "3.12", "3.15"):
        undeclared = ResolveTarget.for_declared(
            python_version=version,
            spec=PlatformSpec("linux_x86_64"),
            implementation="pypy",
        )
        with pytest.raises(PylockSelectError, match="environments specified"):
            list(
                pylock.select(
                    environment=undeclared.marker_env,
                    tags=[Tag("py3", "none", "any")],
                )
            )


def test_sparse_lock_restricts_implementations_with_identical_pins() -> None:
    """Interpreter overrides limit support even without dependency markers."""
    targets = Matrix(
        python="==3.12.*",
        platforms=(PlatformSpec("linux_x86_64"),),
        implementations=("cpython", "pypy"),
        implementation_overrides={
            "pypy": ImplementationOverride(
                python="==3.11.*", platforms=(PlatformSpec("windows_amd64"),)
            )
        },
    ).expand()
    coordinator = make_coordinator(
        listings={"base": [make_wheel("1.0", package="base", hashes=HASHES)]},
        auto_metadata=True,
    )
    inputs = ResolveInputs(build_policy=BuildPolicy.NEVER)
    result = resolve_with_coordinator(
        coordinator, targets, [Requirement("base")], inputs=inputs
    )
    assert result.success
    pylock = build_pylock(build_lock_input(result, inputs=inputs))
    pylock.validate()

    for target in targets:
        selected = list(
            pylock.select(
                environment=target.marker_env, tags=[Tag("py3", "none", "any")]
            )
        )
        assert [str(package.name) for package, _ in selected] == ["base"]

    for version, implementation, platform in (
        ("3.11", "cpython", "linux_x86_64"),
        ("3.12", "cpython", "windows_amd64"),
        ("3.11", "pypy", "linux_x86_64"),
        ("3.12", "pypy", "windows_amd64"),
    ):
        undeclared = ResolveTarget.for_declared(
            python_version=version,
            spec=PlatformSpec(platform),
            implementation=implementation,
        )
        with pytest.raises(PylockSelectError, match="environments specified"):
            list(
                pylock.select(
                    environment=undeclared.marker_env,
                    tags=[Tag("py3", "none", "any")],
                )
            )


def test_platform_override_keeps_native_wheel_eligibility() -> None:
    """PyPy Windows and free-threaded CPython Linux select their native wheels."""
    gil_wheel = make_wheel(
        "4.0", package="native", tag="cp313-cp313-manylinux_2_17_x86_64", hashes=HASHES
    )
    pypy_wheel = make_wheel(
        "3.0", package="native", tag="pp311-pypy311_pp73-win_amd64", hashes=HASHES
    )
    free_threaded_wheel = make_wheel(
        "2.0", package="native", tag="cp313-cp313t-manylinux_2_17_x86_64", hashes=HASHES
    )
    coordinator = _graph(
        {
            "native": [gil_wheel, pypy_wheel, free_threaded_wheel],
            "runtime": [make_wheel("1.0", package="runtime", hashes=HASHES)],
        },
        {"native": ("runtime",)},
    )
    targets = Matrix(
        python="==3.13.*",
        platforms=(PlatformSpec("linux_x86_64", free_threaded=True),),
        implementations=("cpython", "pypy"),
        implementation_overrides={
            "pypy": ImplementationOverride(
                python="==3.11.*", platforms=(PlatformSpec("windows_amd64"),)
            )
        },
    ).expand()
    inputs = ResolveInputs(build_policy=BuildPolicy.NEVER)
    result = resolve_with_coordinator(
        coordinator, targets, [Requirement("native")], inputs=inputs
    )

    assert result.success
    assert {
        target_result.target.implementation: str(target_result.pins["native"])
        for target_result in result.target_results
    } == {"cpython": "2.0", "pypy": "3.0"}
    pylock = build_pylock(build_lock_input(result, inputs=inputs))
    pylock.validate()

    for target in targets:
        wheel = (
            free_threaded_wheel if target.implementation == "cpython" else pypy_wheel
        )
        native_tag = (
            Tag("cp313", "cp313t", "manylinux_2_17_x86_64")
            if target.implementation == "cpython"
            else Tag("pp311", "pypy311_pp73", "win_amd64")
        )
        selected = {
            str(package.name): artifact
            for package, artifact in pylock.select(
                environment=target.marker_env,
                tags=[native_tag, Tag("py3", "none", "any")],
            )
        }
        assert set(selected) == {"native", "runtime"}
        native = selected["native"]
        assert isinstance(native, PackageWheel)
        assert native.url == wheel.url


def test_platform_override_replaces_libc_for_same_platform_id() -> None:
    """Implementation-specific tag settings distinguish glibc and musl wheels."""
    glibc_wheel = make_wheel(
        "4.0", package="native", tag="py3-none-manylinux_2_17_x86_64", hashes=HASHES
    )
    musl_wheel = make_wheel(
        "3.0", package="native", tag="py3-none-musllinux_1_2_x86_64", hashes=HASHES
    )
    targets = Matrix(
        python="==3.12.*",
        platforms=(PlatformSpec("linux_x86_64", runs_on_libc=(2, 17)),),
        implementations=("cpython", "pypy"),
        implementation_overrides={
            "pypy": ImplementationOverride(
                python="==3.11.*",
                platforms=(
                    PlatformSpec("linux_x86_64", libc="musl", runs_on_libc=(1, 2)),
                ),
            )
        },
    ).expand()
    inputs = ResolveInputs(build_policy=BuildPolicy.NEVER)
    result = resolve_with_coordinator(
        _graph({"native": [glibc_wheel, musl_wheel]}, {}),
        targets,
        [Requirement("native")],
        inputs=inputs,
    )

    assert result.success
    assert {
        target_result.target.implementation: str(target_result.pins["native"])
        for target_result in result.target_results
    } == {"cpython": "4.0", "pypy": "3.0"}
    pylock = build_pylock(build_lock_input(result, inputs=inputs))
    pylock.validate()

    for target in targets:
        wheel = glibc_wheel if target.implementation == "cpython" else musl_wheel
        platform_tag = (
            "manylinux_2_17_x86_64"
            if target.implementation == "cpython"
            else "musllinux_1_2_x86_64"
        )
        ((package, artifact),) = pylock.select(
            environment=target.marker_env, tags=[Tag("py3", "none", platform_tag)]
        )
        assert str(package.name) == "native"
        assert isinstance(artifact, PackageWheel)
        assert artifact.url == wheel.url
