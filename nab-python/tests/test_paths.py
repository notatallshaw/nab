"""Tests for the presence classifier (:mod:`nab_python.paths`)."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from unittest.mock import patch

from nab_python.paths import PathState, path_state, resolve_path


def _fake_stat(mode: int) -> os.stat_result:
    return os.stat_result((mode, *([0] * 9)))


class TestPathState:
    def test_regular_file(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("")
        assert path_state(path) is PathState.FILE
        assert path_state(path).should_read

    def test_directory(self, tmp_path: Path) -> None:
        assert path_state(tmp_path) is PathState.DIRECTORY
        assert not path_state(tmp_path).should_read

    def test_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.toml"
        assert path_state(path) is PathState.ABSENT
        assert not path_state(path).should_read

    def test_component_is_not_a_directory(self, tmp_path: Path) -> None:
        # ENOTDIR: a file part-way along the path leaves nothing at the end.
        blocker = tmp_path / "file"
        blocker.write_text("")
        assert path_state(blocker / "pyproject.toml") is PathState.ABSENT

    def test_name_the_os_cannot_carry(self, tmp_path: Path) -> None:
        # An embedded NUL fails the stat with ValueError, not an errno.
        assert path_state(tmp_path / "pyproject\x00.toml") is PathState.ABSENT

    def test_neither_file_nor_directory(self, tmp_path: Path) -> None:
        with patch.object(Path, "stat", return_value=_fake_stat(stat.S_IFIFO | 0o644)):
            state = path_state(tmp_path / "fifo")
        assert state is PathState.OTHER
        assert not state.should_read

    def test_stat_denied(self, tmp_path: Path) -> None:
        # An unsearchable parent directory lands EACCES on the stat itself,
        # and the read decides what that means, so the path is still read.
        path = tmp_path / "pyproject.toml"
        denied = PermissionError(errno.EACCES, "Permission denied", str(path))
        with patch.object(Path, "stat", side_effect=denied):
            state = path_state(path)
        assert state is PathState.UNREADABLE
        assert state.should_read


class TestResolvePath:
    def test_normalises_against_base(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        (base / "pkg").mkdir()
        assert resolve_path(base / "pkg", "../lib") == base / "lib"

    def test_nul_in_name(self, tmp_path: Path) -> None:
        assert resolve_path(tmp_path, "pk\x00g") is None

    def test_unencodable_name(self, tmp_path: Path) -> None:
        # A lone surrogate only fails the encode on POSIX, so the resolve is
        # mocked to reach the arm on every platform.
        broken = UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
        with patch.object(Path, "resolve", side_effect=broken):
            assert resolve_path(tmp_path, "pkg") is None
