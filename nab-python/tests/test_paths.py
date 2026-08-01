"""Tests for the presence classifier (:mod:`nab_python.paths`).

The classifier exists to keep an absent path apart from one whose stat
fails for another reason, so the unreadable case is pinned here rather
than left to whichever ``Path`` method a caller reached for.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from unittest.mock import patch

from nab_python.paths import PathState, path_state


def _fake_stat(mode: int) -> os.stat_result:
    return os.stat_result((mode, *([0] * 9)))


class TestPathState:
    def test_regular_file(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("")
        assert path_state(path) is PathState.FILE

    def test_directory(self, tmp_path: Path) -> None:
        assert path_state(tmp_path) is PathState.DIRECTORY

    def test_missing(self, tmp_path: Path) -> None:
        assert path_state(tmp_path / "missing.toml") is PathState.ABSENT

    def test_component_is_not_a_directory(self, tmp_path: Path) -> None:
        # ENOTDIR: a file part-way along the path leaves nothing at the end.
        blocker = tmp_path / "file"
        blocker.write_text("")
        assert path_state(blocker / "pyproject.toml") is PathState.ABSENT

    def test_neither_file_nor_directory(self, tmp_path: Path) -> None:
        with patch.object(Path, "stat", return_value=_fake_stat(stat.S_IFIFO | 0o644)):
            assert path_state(tmp_path / "fifo") is PathState.OTHER

    def test_stat_denied(self, tmp_path: Path) -> None:
        # An unsearchable parent directory lands EACCES on the stat itself.
        path = tmp_path / "pyproject.toml"
        denied = PermissionError(errno.EACCES, "Permission denied", str(path))
        with patch.object(Path, "stat", side_effect=denied):
            assert path_state(path) is PathState.UNREADABLE


class TestShouldRead:
    def test_only_a_file_or_an_unreadable_path_is_read(self) -> None:
        # An unreadable path is read so the open reports the errno; the
        # other three have nothing worth opening.
        assert [state for state in PathState if state.should_read] == [
            PathState.FILE,
            PathState.UNREADABLE,
        ]
