"""Tests for the presence classifier (:mod:`nab_project.paths`)."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from nab_project import paths
from nab_project.paths import PathState, path_state, realpath, resolve_path


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


class TestDenyAccessFixture:
    """The shared ``deny_access`` fixture, exercised through ``path_state``."""

    def test_denies_stat_bound_into_pathlib_at_import(
        self,
        tmp_path: Path,
        deny_access: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("")

        # Python 3.10's pathlib stats through an os.stat bound at import.
        # Rebuilding that binding puts every interpreter on the 3.10 route.
        bound_at_import = os.stat

        def stat_through_binding(self: Path, **kwargs: Any) -> os.stat_result:
            return bound_at_import(self, **kwargs)

        with patch.object(Path, "stat", stat_through_binding), deny_access(path):
            state = path_state(path)

        assert state is PathState.UNREADABLE


class TestRealpath:
    def test_follows_links(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        (base / "real").mkdir()
        (base / "link").symlink_to("real", target_is_directory=True)
        assert realpath(base / "link") == base / "real"

    def test_symlink_loop_is_left_unresolved(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        (base / "loop").symlink_to("loop")
        assert realpath(base / "loop") == base / "loop"


class TestResolvePath:
    def test_normalises_against_base(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        (base / "pkg").mkdir()
        assert resolve_path(base / "pkg", "../lib") == base / "lib"

    def test_nul_in_name(self, tmp_path: Path) -> None:
        assert resolve_path(tmp_path, "pk\x00g") is None

    def test_unencodable_name(self, tmp_path: Path) -> None:
        # A lone surrogate only fails the encode on POSIX, so ``realpath``
        # is mocked to reach the branch on every platform.
        broken = UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
        with patch.object(paths, "realpath", side_effect=broken):
            assert resolve_path(tmp_path, "pkg") is None
