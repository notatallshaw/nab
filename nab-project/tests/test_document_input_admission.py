"""Preserve native rendering of supported stateful mapping and record inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

import tomli_w

from nab_project._lockfile import pylock
from nab_project._testing.artifacts import make_wheel_artifact
from nab_project.lockfile import IndexPin, LockInput, PinShape, TargetLock
from nab_provider._vendor.packaging.markers import Marker
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget, environment_declaration

if TYPE_CHECKING:
    from collections.abc import Mapping, ValuesView
    from pathlib import Path
    from typing import ClassVar

_Value = TypeVar("_Value")


class _SwapOnSecondValues(dict[str, _Value]):
    """Swap the advertised records when a second values view is requested."""

    __slots__ = ("_reads", "_second")

    def __init__(
        self, first: Mapping[str, _Value], second: Mapping[str, _Value]
    ) -> None:
        super().__init__(first)
        self._second = second
        self._reads = 0

    def values(self) -> ValuesView[str, _Value]:
        self._reads += 1
        if self._reads == 2:
            self.update(self._second)
        return super().values()


class _PinTimeline:
    """Supply alternating pin maps through the public record properties."""

    def __init__(
        self, first: Mapping[str, PinShape], second: Mapping[str, PinShape]
    ) -> None:
        self._first = first
        self._second = second
        self._reads = 0

    def read(self) -> Mapping[str, PinShape]:
        self._reads += 1
        return self._first if self._reads % 2 else self._second


def _target() -> ResolveTarget:
    return ResolveTarget.for_declared(
        python_version="3.12", spec=PlatformSpec("linux_x86_64")
    )


def _pins(version: str) -> dict[str, PinShape]:
    return {
        "demo": IndexPin(
            "demo",
            version,
            "https://example.invalid/",
            wheels=(make_wheel_artifact("demo", version),),
        )
    }


def _input(target: ResolveTarget, locks: Mapping[str, TargetLock]) -> LockInput:
    return LockInput(
        targets=locks,
        environments=[Marker(environment_declaration(target, []))],
    )


def _native_first_text(target: ResolveTarget, lock_dir: Path) -> str:
    value = _input(target, {target.label: TargetLock(target, _pins("1.0"))})
    return tomli_w.dumps(pylock.build_pylock(value, lock_dir=lock_dir).to_dict())


def test_custom_pin_mapping_keeps_native_pins(tmp_path: Path) -> None:
    target = _target()
    pins = _SwapOnSecondValues(_pins("1.0"), _pins("2.0"))
    value = _input(target, {target.label: TargetLock(target, pins)})

    assert pylock.render_lock(value, lock_dir=tmp_path) == _native_first_text(
        target, tmp_path
    )


def test_custom_target_mapping_keeps_native_pins(tmp_path: Path) -> None:
    target = _target()
    locks = _SwapOnSecondValues(
        {target.label: TargetLock(target, _pins("1.0"))},
        {target.label: TargetLock(target, _pins("2.0"))},
    )

    assert pylock.render_lock(_input(target, locks), lock_dir=tmp_path) == (
        _native_first_text(target, tmp_path)
    )


def test_target_subclass_keeps_native_property_reads(tmp_path: Path) -> None:
    target = _target()

    class ChangingTarget(TargetLock):
        __slots__ = ()
        timeline: ClassVar[_PinTimeline] = _PinTimeline(_pins("1.0"), _pins("2.0"))

        @property
        def pins(self) -> Mapping[str, PinShape]:
            return self.timeline.read()

        @pins.setter
        def pins(self, _value: Mapping[str, PinShape]) -> None:
            """The inherited constructor must retain the supplied timeline."""

    value = _input(target, {target.label: ChangingTarget(target, _pins("1.0"))})

    assert pylock.render_lock(value, lock_dir=tmp_path) == _native_first_text(
        target, tmp_path
    )


def test_input_subclass_keeps_native_property_reads(tmp_path: Path) -> None:
    target = _target()

    class ChangingInput(LockInput):
        __slots__ = ()
        timeline: ClassVar[_PinTimeline] = _PinTimeline(_pins("1.0"), _pins("2.0"))

        @property
        def targets(self) -> Mapping[str, TargetLock]:
            return {target.label: TargetLock(target, self.timeline.read())}

        @targets.setter
        def targets(self, _value: Mapping[str, TargetLock]) -> None:
            """The inherited constructor must retain the supplied timeline."""

    value = ChangingInput(
        targets={target.label: TargetLock(target, _pins("1.0"))},
        environments=[Marker(environment_declaration(target, []))],
    )

    assert pylock.render_lock(value, lock_dir=tmp_path) == _native_first_text(
        target, tmp_path
    )
