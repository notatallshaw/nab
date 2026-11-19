"""File-system contracts for atomic staging."""

from __future__ import annotations

import errno
import os
import sys
from base64 import urlsafe_b64encode
from pathlib import Path

import pytest

from nab_index import atomic


@pytest.mark.parametrize("collisions", [0, 1, 19, 20])
def test_staging_retries_occupied_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collisions: int
) -> None:
    occupied = tmp_path / ("." + urlsafe_b64encode(b"a" * 6).decode() + ".tmp")
    occupied.write_bytes(b"keep")
    names = iter([b"a" * 6] * collisions + [b"b" * 6])
    monkeypatch.setattr(atomic.os, "urandom", lambda _size: next(names))
    target = tmp_path / "lock"
    if collisions == 20:
        with pytest.raises(FileExistsError) as info:
            atomic.atomic_write(target, b"new")
        assert info.value.errno == errno.EEXIST
        assert not target.exists()
    else:
        atomic.atomic_write(target, b"new")
        assert target.read_bytes() == b"new"

    assert occupied.read_bytes() == b"keep"
    assert set(tmp_path.iterdir()) <= {target, occupied}


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_staging_never_follows_an_occupied_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    occupied = tmp_path / ("." + urlsafe_b64encode(b"a" * 6).decode() + ".tmp")
    original = tmp_path / "original"
    original.write_bytes(b"keep")
    if kind == "directory":
        occupied.mkdir()
    else:
        try:
            occupied.symlink_to(original)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    names = iter([b"a" * 6, b"b" * 6])
    monkeypatch.setattr(atomic.os, "urandom", lambda _size: next(names))
    atomic.atomic_write(tmp_path / "lock", b"new")

    assert original.read_bytes() == b"keep"
    assert occupied.is_symlink() == (kind == "symlink")
    assert occupied.is_dir() == (kind == "directory")


def test_staging_is_private_and_not_inherited(tmp_path: Path) -> None:
    fd, path = atomic.create_staging_file(tmp_path / "lock")
    try:
        assert path.parent == tmp_path.absolute()
        if os.name != "nt":
            assert os.fstat(fd).st_mode & 0o077 == 0
        assert not os.get_inheritable(fd)
    finally:
        os.close(fd)
        path.unlink()


def test_audit_policy_can_refuse_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "lock"
    target.write_bytes(b"keep")
    events: list[tuple[str, object]] = []

    def audit(event: str, name: object) -> None:
        events.append((event, name))
        raise PermissionError

    monkeypatch.setattr(atomic.sys, "audit", audit)
    with pytest.raises(PermissionError):
        atomic.atomic_write(target, b"new")

    assert len(events) == 1

    event, name = events[0]

    assert event == "tempfile.mkstemp"
    assert isinstance(name, str)
    assert Path(name).parent == tmp_path
    assert target.read_bytes() == b"keep"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_access_denials_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    calls = 0

    def denied(_path: Path, _flags: int, _mode: int) -> int:
        nonlocal calls
        calls += 1
        raise PermissionError

    with monkeypatch.context() as context:
        context.setattr(atomic.sys, "platform", platform)
        context.setattr(atomic.os, "open", denied)
        with pytest.raises(PermissionError):
            atomic.atomic_write(tmp_path / "lock", b"new")

    assert calls == (20 if platform == "win32" else 1)
    assert list(tmp_path.iterdir()) == []


def test_windows_transient_denial_can_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = os.open
    calls = 0

    def open_file(path: Path, flags: int, mode: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError
        return actual(path, flags, mode)

    with monkeypatch.context() as context:
        context.setattr(atomic.sys, "platform", "win32")
        context.setattr(atomic.os, "open", open_file)
        atomic.atomic_write(tmp_path / "lock", b"new")

    assert calls == 2
    assert (tmp_path / "lock").read_bytes() == b"new"


def test_binary_content_is_preserved(tmp_path: Path) -> None:
    data = b"line\r\n\x00\xff"
    target = tmp_path / "lock"
    atomic.atomic_write(target, data)

    assert target.read_bytes() == data


@pytest.mark.parametrize("exists", [False, True])
def test_overwrite_retains_the_destination_mode(
    tmp_path: Path, *, exists: bool
) -> None:
    target = tmp_path / "lock"
    reference = tmp_path / "reference"
    reference.write_bytes(b"reference")
    if exists:
        target.write_bytes(b"old")
        target.chmod(0o640)
        expected = target.stat().st_mode
    else:
        expected = reference.stat().st_mode
    atomic.atomic_write_text(target, "new")

    assert target.read_text() == "new"
    assert target.stat().st_mode == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX filename limits")
def test_staging_accepts_a_destination_at_the_filename_limit(tmp_path: Path) -> None:
    maximum = os.pathconf(tmp_path, "PC_NAME_MAX")
    if maximum == -1:
        pytest.skip("filesystem has no fixed filename limit")
    target = tmp_path / ("x" * maximum)
    atomic.atomic_write(target, b"new")

    assert target.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("prefix", ["", "."])
def test_staging_keeps_a_temporary_looking_destination_absent_until_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    random_bytes = b"a" * 6
    token = urlsafe_b64encode(random_bytes).decode("ascii")
    target = tmp_path / f"{prefix}{token}.tmp"
    monkeypatch.setattr(atomic.os, "urandom", lambda _size: random_bytes)

    fd, stage = atomic.create_staging_file(target)
    try:
        assert stage.exists()
        assert not target.exists()
    finally:
        os.close(fd)
        stage.unlink()


def test_default_binary_sync_failure_keeps_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact"
    target.write_bytes(b"old")

    def fail_sync(_fd: int) -> None:
        raise OSError("storage sync failed")

    monkeypatch.setattr(atomic.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="storage sync failed"):
        atomic.atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("text", [False, True])
def test_removed_destination_keeps_the_observed_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, text: bool
) -> None:
    target = tmp_path / "lock"
    target.write_bytes(b"old")
    target.chmod(0o640)
    expected = target.stat().st_mode
    actual_stat = os.stat
    observed = False

    def remove_after_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal observed
        result = actual_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == target and not observed:
            observed = True
            target.unlink()
        return result

    with monkeypatch.context() as context:
        context.setattr(os, "stat", remove_after_stat)
        # Older pathlib releases retain the original OS accessor.
        context.setattr(
            Path,
            "stat",
            lambda path, *, follow_symlinks=True: remove_after_stat(
                path, follow_symlinks=follow_symlinks
            ),
        )
        if text:
            atomic.atomic_write_text(target, "new")
        else:
            atomic.atomic_write(target, b"new")

    assert observed
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode == expected
    assert list(tmp_path.iterdir()) == [target]


def test_self_referencing_destination_uses_the_missing_target_policy(
    tmp_path: Path,
) -> None:
    target = tmp_path / "lock"
    reference = tmp_path / "reference"
    reference.write_bytes(b"mode")
    expected = reference.stat().st_mode
    try:
        target.symlink_to(target)
    except OSError as exc:
        if sys.platform == "win32" and exc.winerror == 1314:
            pytest.skip("creating symlinks requires Windows privilege")
        raise

    atomic.atomic_write(target, b"new")

    assert not target.is_symlink()
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode == expected


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO, errno.ELOOP])
@pytest.mark.parametrize("patch_os_stat", [False, True])
def test_destination_errors_follow_pathlib_exists_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    *,
    patch_os_stat: bool,
) -> None:
    target = tmp_path / "lock"
    target.write_bytes(b"old")
    actual_stat = os.stat

    def refuse_stat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if path == target:
            raise OSError(error_number, "destination metadata unavailable")
        return actual_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as context:
        if patch_os_stat:
            context.setattr(os, "stat", refuse_stat)
        # Older pathlib releases retain the original OS accessor.
        context.setattr(
            Path,
            "stat",
            lambda path, *, follow_symlinks=True: refuse_stat(
                path, follow_symlinks=follow_symlinks
            ),
        )
        # A native existence probe can bypass the failing metadata lookup.
        try:
            refused = target.exists()
        except OSError:
            refused = True

        if refused:
            with pytest.raises(
                OSError, match="destination metadata unavailable"
            ) as error:
                atomic.atomic_write(target, b"new")
            assert error.value.errno == error_number
        else:
            atomic.atomic_write(target, b"new")

    assert target.read_bytes() == (b"old" if refused else b"new")
    assert list(tmp_path.iterdir()) == [target]


def test_existing_destination_refuses_an_unreadable_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "lock"
    target.write_bytes(b"old")
    actual_stat = os.stat
    failure = OSError(errno.EIO, "destination metadata unavailable")
    failed = False

    def fail_once(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal failed
        if path == target and not failed:
            failed = True
            raise failure
        return actual_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as context:
        context.setattr(os, "stat", fail_once)
        # Older pathlib releases retain the original OS accessor.
        context.setattr(
            Path,
            "stat",
            lambda path, *, follow_symlinks=True: fail_once(
                path, follow_symlinks=follow_symlinks
            ),
        )
        with pytest.raises(OSError, match="destination metadata unavailable") as error:
            atomic.atomic_write(target, b"new")

    assert error.value is failure
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]
