"""Pytest fixtures shared by the Nab workspace suites."""

# ruff: noqa: INP001 - This file also serves as tests/conftest.py in the project sdist.
from __future__ import annotations

import errno
import io
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from contextlib import AbstractContextManager
    from types import TracebackType

    from typing_extensions import Self


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
def _cap_writes(budget: int) -> Generator[None, None, None]:
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
def _refuse_over_nested(payload: bytes) -> Generator[None, None, None]:
    real_loads = json.loads

    def refusing_loads(raw: Any, *args: Any, **kwargs: Any) -> Any:
        if raw == payload:
            msg = "maximum recursion depth exceeded while decoding a JSON array"
            raise RecursionError(msg)
        return real_loads(raw, *args, **kwargs)

    with patch.object(json, "loads", refusing_loads):
        yield


@contextmanager
def _record_parses() -> Generator[list[str], None, None]:
    # nab-resolver and nab-provider run against this conftest without nab-project
    # installed, so the import cannot sit at module scope.
    from nab_project import toml_io  # noqa: PLC0415

    parsed: list[str] = []
    real_loads = toml_io.loads

    def recording_loads(text: str) -> dict[str, Any]:
        parsed.append(text)
        return real_loads(text)

    with patch.object(toml_io, "loads", recording_loads):
        yield parsed


@contextmanager
def _deny_access(target: Path) -> Generator[None, None, None]:
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
def refuse_over_nested() -> Callable[[bytes], AbstractContextManager[None]]:
    """Make the JSON decoder raise ``RecursionError`` for the given bytes."""
    return _refuse_over_nested


@pytest.fixture
def record_parses() -> Callable[[], AbstractContextManager[list[str]]]:
    """Record each document passed to ``toml_io.loads`` inside the block."""
    return _record_parses


@pytest.fixture
def deny_access() -> Callable[[Path], AbstractContextManager[None]]:
    """Make stat and open raise ``EACCES`` for one path on every platform.

    Python 3.10 captures the stat function inside pathlib, so patch both
    ``Path.stat`` and ``os.stat``.
    """
    return _deny_access


@pytest.fixture
def cap_writes() -> Callable[[int], AbstractContextManager[None]]:
    """Raise ``ENOSPC`` after the byte or character limit for each file."""
    return _cap_writes


@pytest.fixture
def oversized_integer() -> str:
    """Return a decimal integer literal too long for ``int()`` to convert.

    tomli builds a TOML integer with ``int()``, which CPython caps at
    ``sys.get_int_max_str_digits()`` digits, so a literal this long fails the
    parse without being a syntax error.
    """
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    if get_limit is None:
        pytest.skip("requires an interpreter integer conversion limit")
    limit = get_limit()
    assert isinstance(limit, int)
    if not limit:
        pytest.skip("requires an enabled interpreter integer conversion limit")
    return "1" * (limit + 1)


@pytest.fixture
def over_nested_marker() -> str:
    """Return a PEP 508 marker nested past what the interpreter can parse.

    The parser spends two frames per parenthesised level, so nesting to the
    recursion limit overflows the stack from any starting depth.
    """
    depth = sys.getrecursionlimit()
    return "(" * depth + "os_name == 'posix'" + ")" * depth
