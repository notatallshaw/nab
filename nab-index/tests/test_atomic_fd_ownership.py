"""Staging owns descriptors even when a stream refuses to borrow them."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from nab_index import atomic

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


_WRITERS: dict[str, Callable[[Path], None]] = {
    "binary": lambda path: atomic.atomic_write(path, b"new"),
    "text": lambda path: atomic.atomic_write_text(path, "new"),
    "chunks": lambda path: atomic.atomic_write_chunks(path, (b"n", b"ew"), sync=False),
}

_AUDIT_PROBE = """
import errno
import json
import os
from pathlib import Path
import sys

from nab_index import atomic

assert Path(atomic.__file__).resolve() == Path(sys.argv[1]).resolve()
target = Path("output")
target.write_bytes(b"old")
refused = []

def audit(event, arguments):
    if event == "open" and isinstance(arguments[0], int):
        refused.append(arguments[0])
        raise PermissionError(errno.EACCES, "stream construction refused")

operations = {
    "binary": lambda: atomic.atomic_write(target, b"new"),
    "text": lambda: atomic.atomic_write_text(target, "new"),
    "chunks": lambda: atomic.atomic_write_chunks(target, (b"n", b"ew"), sync=False),
}
sys.addaudithook(audit)
try:
    operations[sys.argv[2]]()
except PermissionError as error:
    error_errno = error.errno
else:
    raise AssertionError("The stream audit boundary was not reached")

leaked = []
for descriptor in refused:
    try:
        os.fstat(descriptor)
    except OSError as error:
        assert error.errno == errno.EBADF
    else:
        leaked.append(descriptor)
        os.close(descriptor)
print(json.dumps({"refused_streams": len(refused), "leaked_descriptors": len(leaked),
    "error_errno": error_errno, "output": target.read_text(),
    "staging_files": len(list(Path.cwd().glob("output.*.tmp")))}))
"""


@pytest.mark.parametrize("kind", _WRITERS)
def test_stream_audit_rejection_closes_the_descriptor(
    tmp_path: Path, kind: str
) -> None:
    # Audit hooks cannot be removed, so refuse the real open event in a child.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(atomic.__file__).parent.parent), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(  # noqa: S603 - fixed script and enumerated writers
        [sys.executable, "-c", _AUDIT_PROBE, atomic.__file__, kind],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "refused_streams": 1,
        "leaked_descriptors": 0,
        "error_errno": errno.EACCES,
        "output": "old",
        "staging_files": 0,
    }


@pytest.mark.parametrize("kind", _WRITERS)
def test_descriptor_is_closed_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    target = tmp_path / "output"
    opened = []
    real_open = os.open
    real_replace = os.replace

    def open_file(path: Path, flags: int, mode: int) -> int:
        descriptor = real_open(path, flags, mode)
        opened.append(descriptor)
        return descriptor

    def replace(source: Path, destination: Path) -> None:
        assert opened
        for descriptor in opened:
            try:
                os.fstat(descriptor)
            except OSError as error:
                assert error.errno == errno.EBADF
            else:
                pytest.fail("staging descriptor is still open before replacement")
        real_replace(source, destination)

    monkeypatch.setattr(atomic.os, "open", open_file)
    monkeypatch.setattr(atomic.os, "replace", replace)
    _WRITERS[kind](target)

    assert target.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("body_fails", [False, True])
def test_close_failure_does_not_retry_a_reused_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, body_fails: bool
) -> None:
    target = tmp_path / "output"
    target.write_bytes(b"old")
    other = tmp_path / "other"
    other.write_bytes(b"other")
    spare = os.open(other, os.O_RDWR)
    real_close = os.close
    closed = []
    replacements = []
    failure = RuntimeError("body failed")

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)
        if len(closed) == 1:
            # Make a second close hit a different file, using the real FD table.
            os.dup2(spare, descriptor)
            replacements.append(descriptor)
            raise OSError(errno.EIO, "close failed after releasing descriptor")

    def chunks() -> Iterator[bytes]:
        yield b"new"
        raise failure

    monkeypatch.setattr(atomic.os, "close", close)
    try:
        if body_fails:
            with pytest.raises(RuntimeError, match="body failed") as error:
                atomic.atomic_write_chunks(target, chunks())
            assert error.value is failure
        else:
            with pytest.raises(OSError, match="close failed") as error:
                atomic.atomic_write(target, b"new")
            assert error.value.errno == errno.EIO

        assert len(closed) == 1
        assert len(replacements) == 1
        assert os.write(replacements[0], b"live") == 4
    finally:
        for descriptor in replacements:
            with suppress(OSError):
                real_close(descriptor)
        real_close(spare)

    assert target.read_bytes() == b"old"
    assert other.read_bytes() == b"liver"
    assert set(tmp_path.iterdir()) == {target, other}
