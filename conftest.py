"""Top-level conftest: hypothesis profiles and the ``cap_writes`` fixture.

Three hypothesis profiles, selectable via the ``HYPOTHESIS_PROFILE`` env var:

- ``dev`` (default): low ``max_examples`` (20) for fast feedback.
- ``ci``: more thorough (200 examples) plus ``derandomize=True`` so
  failures are reproducible from the seed printed in the assertion;
  ``deadline=None`` so a slow CI machine doesn't fail tests on
  the basis of wall time alone.
- ``deep``: 2000 examples; for nightly counter-example hunts.

Loading a profile only changes the default settings; tests that
explicitly construct a ``settings(...)`` decorator are unaffected.
The property suite under ``nab-*/tests/property*/`` uses explicit
``PROPERTY_SETTINGS``/``DEEP_SETTINGS``/``BRUTE_FORCE_SETTINGS``
decorators so its example budget is independent of the profile.

``cap_writes``, ``deny_access`` and ``oversized_integer`` are here rather than
in one suite's conftest because both the ``nab-project`` suite and the CLI
suite use them.
"""

from __future__ import annotations

import errno
import io
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager
    from types import TracebackType

    from typing_extensions import Self

settings.register_profile("dev", max_examples=20)
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "deep",
    max_examples=2000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


class _CappedHandle:
    """A writable file handle that stops accepting content past a budget."""

    def __init__(self, handle: Any, budget: int) -> None:
        self._handle = handle
        self._budget = budget

    def write(self, payload: Any) -> int:
        if len(payload) > self._budget:
            self._handle.write(payload[: self._budget])
            self._handle.flush()
            self._budget = 0
            msg = os.strerror(errno.ENOSPC)
            raise OSError(errno.ENOSPC, msg)

        self._budget -= len(payload)
        written: int = self._handle.write(payload)
        return written

    def flush(self) -> None:
        self._handle.flush()

    def fileno(self) -> int:
        fd: int = self._handle.fileno()
        return fd

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self._handle.close()


@contextmanager
def _cap_writes(budget: int) -> Iterator[None]:
    real_open = io.open

    def capped_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, mode, *args, **kwargs)
        if "w" not in mode:
            return handle
        return _CappedHandle(handle, budget)

    io.open = capped_open  # type: ignore[assignment]
    try:
        yield
    finally:
        io.open = real_open  # type: ignore[assignment]


@contextmanager
def _deny_access(target: Path) -> Iterator[None]:
    real_os_stat, real_path_stat, real_open = os.stat, Path.stat, Path.open
    denied = os.fspath(target)

    def refuse(candidate: Any) -> None:
        if isinstance(candidate, (str, os.PathLike)) and os.fspath(candidate) == denied:
            raise PermissionError(errno.EACCES, "Permission denied", denied)

    def denying_os_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        refuse(path)
        return real_os_stat(path, *args, **kwargs)

    def denying_path_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        refuse(self)
        return real_path_stat(self, *args, **kwargs)

    def denying_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        refuse(self)
        return real_open(self, *args, **kwargs)

    with (
        patch.object(os, "stat", denying_os_stat),
        patch.object(Path, "stat", denying_path_stat),
        patch.object(Path, "open", denying_open),
    ):
        yield


@pytest.fixture
def deny_access() -> Callable[[Path], AbstractContextManager[None]]:
    """Simulate a directory the running user cannot search.

    Inside ``deny_access(p)`` every stat and open of ``p`` raises ``EACCES``,
    which is what a parent directory without the search bit does to both
    halves of a read. chmod cannot express that on Windows, so the state is
    simulated rather than created.

    Both stat routes are patched: on Python 3.10 ``Path.stat`` calls an
    ``os.stat`` pathlib bound at import, which a patch of ``os.stat`` does
    not reach.
    """
    return _deny_access


@pytest.fixture
def cap_writes() -> Callable[[int], AbstractContextManager[None]]:
    """Simulate a filesystem that fills up partway through a write.

    Inside ``cap_writes(n)`` every file opened for writing takes the first ``n``
    bytes and then raises ``ENOSPC``, leaving those bytes on disk. Patching
    ``io.open`` rather than a writer keeps the fixture agnostic about how the
    file gets written.
    """
    return _cap_writes


@pytest.fixture
def oversized_integer() -> str:
    """Return a decimal integer literal too long for ``int()`` to convert.

    tomli builds a TOML integer with ``int()``, which CPython caps at
    ``sys.get_int_max_str_digits()`` digits, so a literal this long fails the
    parse without being a syntax error.
    """
    return "1" * (sys.get_int_max_str_digits() + 1)
