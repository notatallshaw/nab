"""Lock membership preserves explicit contexts and skips omitted contexts."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from types import FrameType

import pytest

from nab_project._lockfile import builder
from nab_project.lockfile import BASE_MEMBER, build_target_lock
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator, make_metadata, make_wheel

TARGET = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("linux_x86_64")
)
VERSION = Version("1.0")


def _provider() -> Provider:
    """Read base and optional dependencies through the real provider."""
    requirements = {
        "core": ("shared",),
        "tool": ("shared", 'fancy-addon; extra == "fancy"'),
        "shared": (),
        "fancy-addon": (),
    }
    wheels = {
        name: make_wheel(package=name.replace("-", "_"), hashes=(("sha256", "a" * 64),))
        for name in requirements
    }
    metadata = {}
    for name, wheel in wheels.items():
        assert wheel.metadata_url is not None
        metadata[wheel.metadata_url] = make_metadata(
            name,
            str(VERSION),
            *requirements[name],
            provides_extra=("fancy",) if name == "tool" else (),
        )
    provider = Provider(
        make_coordinator(
            listings={name: [wheel] for name, wheel in wheels.items()},
            metadata_by_url=metadata,
        ),
        target=TARGET,
    )
    for name in requirements:
        provider.fetch_versions(name)
        provider.get_dependencies(name, VERSION)
    provider.get_dependencies("tool[fancy]", VERSION)
    return provider


@contextmanager
def _membership_calls() -> Iterator[list[str]]:
    """Count real membership construction without replacing the builder."""
    calls: list[str] = []
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, _value: object) -> None:
        if event == "call" and frame.f_code is builder._membership_gates.__code__:
            calls.append(event)

    sys.setprofile(observe)
    try:
        yield calls
    finally:
        sys.setprofile(previous)


@pytest.mark.parametrize("selectors", [None, {}])
def test_absent_contexts_do_not_construct_membership(
    selectors: Mapping[tuple[str, str], Iterable[str]] | None,
) -> None:
    provider = _provider()
    pins = dict.fromkeys(("tool", "shared", "fancy-addon"), VERSION)

    with _membership_calls() as calls:
        lock = build_target_lock(
            provider,
            TARGET,
            pins,
            resolved_keys=(*pins, "tool[fancy]"),
            selector_roots=selectors,
        )

    assert calls == []
    assert lock.package_gates == {}
    assert lock.dependencies == {"tool": ("fancy-addon", "shared")}
    assert lock.base_dependencies == {"tool": ("shared",)}


@pytest.mark.parametrize("kind", ["extra", "group"])
@pytest.mark.parametrize(
    "empty_roots",
    [
        pytest.param(tuple, id="tuple"),
        pytest.param(frozenset, id="frozenset"),
        pytest.param(lambda: iter(()), id="iterator"),
    ],
)
def test_empty_base_roots_keep_selector_membership(
    kind: str, empty_roots: Callable[[], Iterable[str]]
) -> None:
    provider = _provider()
    pins = dict.fromkeys(("tool", "shared", "fancy-addon"), VERSION)
    member = (kind, "dev")
    lock = build_target_lock(
        provider,
        TARGET,
        pins,
        resolved_keys=(*pins, "tool[fancy]"),
        base_roots=empty_roots(),
        selector_roots={member: iter(("tool[fancy]",))},
    )

    assert lock.package_gates == dict.fromkeys(pins, (member,))


@pytest.mark.parametrize("kind", ["extra", "group"])
def test_single_pass_roots_keep_base_and_extra_reachability(kind: str) -> None:
    provider = _provider()
    pins = dict.fromkeys(("core", "tool", "shared", "fancy-addon"), VERSION)
    member = (kind, "dev")
    lock = build_target_lock(
        provider,
        TARGET,
        pins,
        resolved_keys=(*pins, "tool[fancy]"),
        base_roots=iter(("core",)),
        selector_roots={member: iter(("tool[fancy]",))},
    )

    assert lock.package_gates == {
        "core": (BASE_MEMBER,),
        "tool": (member,),
        "shared": tuple(sorted((BASE_MEMBER, member))),
        "fancy-addon": (member,),
    }


def test_selector_roots_still_require_explicit_base_roots() -> None:
    provider = _provider()
    with pytest.raises(ValueError, match="need base_roots"):
        build_target_lock(
            provider,
            TARGET,
            {"tool": VERSION},
            selector_roots={("extra", "dev"): iter(("tool",))},
        )
