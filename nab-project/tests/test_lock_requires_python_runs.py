"""Lock constraints preserve validation across repeated values and limit changes."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

import pytest

from nab_project._lockfile import builder
from nab_provider.records import SdistFile, WheelFile
from nab_provider.testing import make_sdist, make_wheel


def _files(values: tuple[str | None, ...]) -> list[WheelFile | SdistFile]:
    """Mix wheels and sdists in the supplied Requires-Python order."""
    return [
        make_sdist(requires_python=value)
        if index % 2
        else make_wheel(requires_python=value)
        for index, value in enumerate(values)
    ]


@contextmanager
def _validation_calls() -> Iterator[list[str]]:
    """Observe the real validator without replacing its implementation."""
    calls: list[str] = []
    code = builder.validate_specifier_versions.__code__
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, _value: object) -> None:
        if event == "call" and frame.f_code is code:
            calls.append(event)

    sys.setprofile(observe)
    try:
        yield calls
    finally:
        sys.setprofile(previous)


@pytest.mark.parametrize(
    ("values", "expected", "validations", "old_validations"),
    [
        ((), None, 0, 0),
        ((None,), None, 0, 0),
        (("", ""), "", 1, 2),
        ((">=3.12", ">=3.12", ">=3.12"), ">=3.12", 1, 3),
        ((">=3.8", ">=3.8.0"), None, 2, 2),
        ((">=3.10", ">=3.11", ">=3.12"), None, 3, 3),
        ((">=3.8", ">=3.9", ">=3.8"), None, 3, 3),
        ((">=3.12", None, "invalid"), None, 1, 1),
        ((">=3.12", ">=3.12", "invalid"), None, 1, 2),
        (("invalid", "invalid"), None, 0, 0),
    ],
)
def test_repeated_values_preserve_lock_constraint(
    values: tuple[str | None, ...],
    expected: str | None,
    validations: int,
    old_validations: int,
) -> None:
    files = _files(values)

    with _validation_calls() as calls:
        actual = builder._common_requires_python(iter(files))

    assert actual == expected
    assert len(calls) == (
        validations if sys.version_info >= (3, 11) else old_validations
    )


def test_validation_reuse_stays_with_one_call() -> None:
    files = _files((">=3.12", ">=3.12"))

    with _validation_calls() as calls:
        assert builder._common_requires_python(files) == ">=3.12"
        assert builder._common_requires_python(files) == ">=3.12"

    assert len(calls) == (2 if sys.version_info >= (3, 11) else 4)


def test_record_property_reads_keep_their_original_order() -> None:
    reads: list[str] = []
    responses = (">=3.8", ">=3.9", ">=3.10") * 2
    remaining = iter(responses)

    class ReadingWheel(WheelFile):
        __slots__ = ()

        @property
        def requires_python(self) -> str:
            value = next(remaining)
            reads.append(value)
            return value

        @requires_python.setter
        def requires_python(self, _value: str | None) -> None:
            """Supply scripted reads independently of the constructor input."""

    file = ReadingWheel(
        "demo-1.0-py3-none-any.whl",
        url="https://example.test/demo.whl",
        version="1.0",
        requires_python=">=3.12",
        has_metadata=False,
        upload_time=None,
    )

    assert builder._common_requires_python((file, file)) == ">=3.10"
    assert tuple(reads) == responses


@pytest.mark.parametrize("ordinary_second", [False, True])
def test_str_subclass_equality_cannot_replace_validation(ordinary_second: bool) -> None:
    class EqualText(str):
        __slots__ = ()

        def __eq__(self, _other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return hash("invalid")

    first = EqualText(">=3.12")
    second = "invalid" if ordinary_second else EqualText("invalid")
    files = _files((first, second))

    assert builder._common_requires_python(files) is None


def test_str_subclass_split_is_evaluated_for_each_record() -> None:
    segments = iter(([">=3.12"], ["invalid"]))

    class ChangingSplit(str):
        __slots__ = ()

        def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
            return next(segments)

    raw = ChangingSplit(">=3.12")
    files = _files((raw, raw))

    assert builder._common_requires_python(files) is None


def test_old_runtime_keeps_original_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _files((">=3.12", ">=3.12", ">=3.12"))

    with monkeypatch.context() as context:
        context.setattr(sys, "version_info", (3, 10))
        with _validation_calls() as calls:
            actual = builder._common_requires_python(files)

    assert actual == ">=3.12"
    assert len(calls) == 3


def test_supported_runtime_reuses_repeated_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _files((">=3.12", ">=3.12", ">=3.12"))

    with monkeypatch.context() as context:
        context.setattr(sys, "version_info", (3, 11))
        # Python 3.10.4 has no integer-limit getter to preserve.
        context.setattr(sys, "get_int_max_str_digits", lambda: 0, raising=False)
        with _validation_calls() as calls:
            actual = builder._common_requires_python(files)

    assert actual == ">=3.12"
    assert len(calls) == 1


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="Reuse is enabled from Python 3.11"
)
def test_changed_integer_limit_revalidates_repeated_value() -> None:
    original_limit = sys.get_int_max_str_digits()
    minimum_limit = sys.int_info.str_digits_check_threshold
    raw = ">=" + "9" * (minimum_limit + 1)
    file = make_wheel(requires_python=raw)

    def records() -> Iterator[WheelFile]:
        sys.set_int_max_str_digits(0)
        yield file
        sys.set_int_max_str_digits(minimum_limit)
        yield file

    try:
        actual = builder._common_requires_python(records())
    finally:
        sys.set_int_max_str_digits(original_limit)

    assert actual is None


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="Reuse is enabled from Python 3.11"
)
def test_changed_recursion_limit_revalidates_repeated_value() -> None:
    original_limit = sys.getrecursionlimit()
    file = make_wheel(requires_python=">=3.12")

    def records() -> Iterator[WheelFile]:
        yield file
        sys.setrecursionlimit(original_limit + 1)
        yield file

    try:
        with _validation_calls() as calls:
            actual = builder._common_requires_python(records())
    finally:
        sys.setrecursionlimit(original_limit)

    assert actual == ">=3.12"
    assert len(calls) == 2
