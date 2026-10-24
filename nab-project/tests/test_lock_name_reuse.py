"""Lock name reuse preserves live pins and custom string behavior."""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

import pytest

from nab_project._lockfile import builder
from nab_project.lockfile import build_target_lock
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import Provider
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import make_coordinator, make_metadata, make_wheel

TARGET = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("linux_x86_64")
)
VERSION = Version("1.0")
LATER = Version("2.0")


def _provider() -> Provider:
    """Load both core releases and the optional edge through real metadata."""
    requirements = {
        ("core-tool", VERSION): ("shared-lib", 'other-tool; extra == "fast"'),
        ("core-tool", LATER): ("other-tool",),
        ("other-tool", VERSION): ("shared-lib",),
        ("shared-lib", VERSION): (),
    }
    listings = {}
    metadata = {}
    for (name, version), dependencies in requirements.items():
        wheel = make_wheel(
            package=name.replace("-", "_"),
            version=str(version),
            hashes=(("sha256", "a" * 64),),
        )
        listings.setdefault(name, []).append(wheel)
        assert wheel.metadata_url is not None
        metadata[wheel.metadata_url] = make_metadata(
            name,
            str(version),
            *dependencies,
            provides_extra=("fast",) if name == "core-tool" else (),
        )
    provider = Provider(
        make_coordinator(listings=listings, metadata_by_url=metadata), target=TARGET
    )
    for name, version in requirements:
        provider.fetch_versions(name)
        provider.get_dependencies(name, version)
    provider.get_dependencies("core-tool[fast]", VERSION)
    return provider


@contextmanager
def _normalizations() -> Iterator[list[str]]:
    """Observe the builder's actual calls to the production normalizer."""
    calls: list[str] = []
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, _value: object) -> None:
        if (
            event == "call"
            and frame.f_code is builder.canonicalize_name.__code__
            and frame.f_back is not None
            and frame.f_back.f_code.co_filename == builder.__file__
        ):
            calls.append(frame.f_locals["name"])

    sys.setprofile(observe)
    try:
        yield calls
    finally:
        sys.setprofile(previous)


def test_normalize_each_input_name_once_per_target() -> None:
    provider = _provider()
    pins = dict.fromkeys(("Core_Tool", "Other.Tool", "SHARED_lib"), VERSION)
    expected = {
        "Core_Tool",
        "Other.Tool",
        "SHARED_lib",
        "core-tool",
        "other-tool",
        "shared-lib",
    }

    with _normalizations() as calls:
        for _ in range(2):
            lock = build_target_lock(
                provider, TARGET, pins, resolved_keys=iter(("core-tool[fast]",))
            )
            assert set(lock.pins) == {"core-tool", "other-tool", "shared-lib"}
            assert lock.dependencies == {
                "core-tool": ("other-tool", "shared-lib"),
                "other-tool": ("shared-lib",),
            }
            assert lock.base_dependencies == {
                "core-tool": ("shared-lib",),
                "other-tool": ("shared-lib",),
            }

    assert Counter(calls) == Counter(dict.fromkeys(expected, 2))


def test_dependency_graph_reads_versions_after_provider_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    pins = dict.fromkeys(("Core_Tool", "Other.Tool", "SHARED_lib"), VERSION)
    original = Provider.tag_excluded_wheel_count

    def change_pin(self: Provider, name: str, version: Version) -> int:
        pins["Core_Tool"] = LATER
        return original(self, name, version)

    monkeypatch.setattr(Provider, "tag_excluded_wheel_count", change_pin)
    lock = build_target_lock(provider, TARGET, pins)

    assert lock.pins["core-tool"].version == "1.0"
    assert lock.base_dependencies == {
        "core-tool": ("other-tool",),
        "other-tool": ("shared-lib",),
    }


def test_custom_string_keys_do_not_gain_hash_calls() -> None:
    class UnhashableName(str):
        __slots__ = ()

        def __hash__(self) -> int:
            raise AssertionError("Unexpected key hashing")

    names = {}

    assert builder._lock_name(UnhashableName("Core_Tool"), names) == "core-tool"
    assert names == {}
