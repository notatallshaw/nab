"""Automatic metadata admission uses the declared target without provider state."""

from __future__ import annotations

import gc
import weakref
from collections.abc import Callable

import pytest

from nab_provider.provider import Provider
from nab_provider.records import WheelFile
from nab_provider.store import InMemoryIndex
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.testing import (
    FakeFetchPort,
    make_coordinator,
    make_wheel,
    pkg_override,
)


def no_result(*_args: str) -> None:
    """Registration cases do not request remote data."""


class AdmissionPort(FakeFetchPort):
    """Keep the native port while capturing its optional admission callback."""

    def __init__(self) -> None:
        super().__init__(
            InMemoryIndex(),
            serve_metadata=no_result,
            serve_sdist=no_result,
            serve_range=no_result,
            serve_archive=no_result,
        )
        self.admit: Callable[[list[WheelFile]], bool] | None = None

    def set_metadata_prefetch_admission(
        self, admit: Callable[[list[WheelFile]], bool] | None
    ) -> None:
        self.admit = admit


class CustomProvider(Provider):
    """Retain a subclass's existing speculative behavior."""


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ([">=3.11"], False),
        ([">=3.8"], True),
        ([None], True),
        ([""], True),
        (["invalid"], True),
        ([">=3.11", ">=3.8"], True),
        ([">=3.11", ">=3.11"], False),
        ([">=3.11", None], True),
        ([">=3.11", "invalid"], True),
    ],
)
def test_declared_python_admission_keeps_any_usable_or_unknown_bound(
    bounds: list[str | None], *, expected: bool
) -> None:
    port = AdmissionPort()
    target = ResolveTarget.for_declared(
        python_version="3.10", spec=PlatformSpec("linux_x86_64")
    )
    provider = Provider(port, target=target)
    assert port.admit is not None
    wheels = [
        make_wheel("2", requires_python=bound, tag=tag)
        for bound, tag in zip(
            bounds, ("py3-none-any", "py2.py3-none-any"), strict=False
        )
    ]

    assert port.admit(wheels) is expected
    assert provider.stats.excluded_by_python == 0


@pytest.mark.parametrize("full_version", [None, "3.10.0"])
def test_admission_preserves_existing_requires_python_minor_semantics(
    full_version: str | None,
) -> None:
    port = AdmissionPort()
    Provider(
        port,
        target=ResolveTarget.for_declared(
            python_version="3.10",
            python_full_version=full_version,
            spec=PlatformSpec("linux_x86_64"),
        ),
    )
    assert port.admit is not None

    assert port.admit([make_wheel("2", requires_python=">=3.10.9")])


def test_admission_does_not_retain_provider() -> None:
    port = AdmissionPort()
    provider = Provider(
        port,
        target=ResolveTarget.for_declared(
            python_version="3.10", spec=PlatformSpec("linux_x86_64")
        ),
    )
    reference = weakref.ref(provider)
    del provider
    gc.collect()

    assert reference() is None
    assert port.admit is not None
    assert not port.admit([make_wheel("2", requires_python=">=3.11")])


def test_custom_provider_clears_automatic_admission() -> None:
    port = AdmissionPort()
    target = ResolveTarget.for_declared(
        python_version="3.10", spec=PlatformSpec("linux_x86_64")
    )
    Provider(port, target=target)
    assert port.admit is not None

    CustomProvider(port, target=target)

    assert port.admit is None


def test_overrides_and_undeclared_targets_do_not_install_admission() -> None:
    port = AdmissionPort()
    Provider(port)
    assert port.admit is None

    Provider(
        port,
        target=ResolveTarget.for_declared(
            python_version="3.10", spec=PlatformSpec("linux_x86_64")
        ),
        package_overrides=(pkg_override("pkg", requires_python=">=3.8"),),
    )
    assert port.admit is None


def test_legacy_port_needs_no_admission_capability() -> None:
    port = make_coordinator([make_wheel("1")])
    provider = Provider(port)

    assert provider.fetch_versions("pkg")[0][0].release == (1,)
