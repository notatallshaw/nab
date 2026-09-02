"""Top-level conftest: hypothesis profiles and the simulated-failure fixtures.

Three hypothesis profiles, selectable via the ``HYPOTHESIS_PROFILE`` env var:

- ``dev`` (default): low ``max_examples`` (20) for fast feedback.
- ``ci``: more thorough (200 examples), drawn fresh each run so the
  runs together search more of the space than any one could; a failure
  prints the ``@reproduce_failure`` blob that replays it. ``deadline=None``
  so a slow CI machine doesn't fail tests on the basis of wall time alone.
- ``deep``: 2000 examples; for nightly counter-example hunts.

Loading a profile only changes the default settings; tests that
explicitly construct a ``settings(...)`` decorator are unaffected.
The property suite under ``nab-*/tests/property*/`` uses explicit
``PROPERTY_SETTINGS``/``DEEP_SETTINGS``/``BRUTE_FORCE_SETTINGS``
decorators so its example budget is independent of the profile.

``cap_writes``, ``deny_access``, ``oversized_integer``,
``over_nested_marker``, ``refuse_over_nested`` and ``record_parses`` are here
rather than in one suite's conftest because the workspace suites are all
packages named ``tests``, so only the umbrella suite can carry one.
"""

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
    print_blob=True,
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
def _refuse_over_nested(payload: bytes) -> Iterator[None]:
    real_loads = json.loads

    def refusing_loads(raw: Any, *args: Any, **kwargs: Any) -> Any:
        if raw == payload:
            msg = "maximum recursion depth exceeded while decoding a JSON array"
            raise RecursionError(msg)
        return real_loads(raw, *args, **kwargs)

    with patch.object(json, "loads", refusing_loads):
        yield


@contextmanager
def _record_parses() -> Iterator[list[str]]:
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
def refuse_over_nested() -> Callable[[bytes], AbstractContextManager[None]]:
    """Simulate a JSON body the decoder refuses as nested too deeply.

    Inside ``refuse_over_nested(b)`` decoding ``b`` raises ``RecursionError``
    and every other body decodes as usual. The depth that provokes one belongs
    to the interpreter version and to the C stack left to the running thread
    rather than to the document, so a literal deep enough to trigger it here
    decodes cleanly on the next machine.
    """
    return _refuse_over_nested


@pytest.fixture
def record_parses() -> Callable[[], AbstractContextManager[list[str]]]:
    """Record the text of every TOML document parsed inside the block.

    The hook sits on ``toml_io.loads``, the funnel every nab parse reaches, so a
    caller that opens a file and parses it some other way is recorded too.
    Counting one file's own text in the result keeps the other files a command
    reads out of the tally.
    """
    return _record_parses


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


@pytest.fixture
def over_nested_marker() -> str:
    """Return a PEP 508 marker nested past what the interpreter can parse.

    The parser spends two frames per parenthesised level, so nesting to the
    recursion limit overflows the stack from any starting depth.
    """
    depth = sys.getrecursionlimit()
    return "(" * depth + "os_name == 'posix'" + ")" * depth
