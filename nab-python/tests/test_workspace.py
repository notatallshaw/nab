"""Tests for the workspace discovery helpers."""

from __future__ import annotations

import errno
import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nab_python.provider import LocalSource
from nab_python.workspace import (
    WorkspaceDiscoveryError,
    discover_workspace_root,
    merge_workspace_local_sources,
    read_workspace_members,
)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _deny_open(target: Path) -> AbstractContextManager[Any]:
    """Patch ``Path.open`` to refuse ``target`` and pass everything else through."""
    real = Path.open

    def opener(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return real(self, *args, **kwargs)

    return patch.object(Path, "open", opener)


class TestDiscoverWorkspaceRoot:
    def test_walks_up_to_root_with_workspace_table(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        _write(
            root / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\nmembers = []\n",
        )
        member = _write(
            root / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        assert discover_workspace_root(member) == (root / "pyproject.toml")

    def test_returns_input_when_input_is_workspace_root(self, tmp_path: Path) -> None:
        root_pyproject = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\nmembers = []\n",
        )
        assert discover_workspace_root(root_pyproject) == root_pyproject

    def test_returns_none_when_no_ancestor_has_workspace_table(
        self, tmp_path: Path
    ) -> None:
        # A standalone pyproject with no workspace ancestor.
        member = _write(
            tmp_path / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        assert discover_workspace_root(member) is None

    def test_skips_unparsable_intermediate_pyprojects(self, tmp_path: Path) -> None:
        # A malformed sibling pyproject between member and root must not
        # block discovery.
        _write(
            tmp_path / "ws" / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\nmembers = []\n",
        )
        _write(
            tmp_path / "ws" / "broken" / "pyproject.toml",
            "this is not valid TOML [[[",
        )
        member = _write(
            tmp_path / "ws" / "broken" / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        assert discover_workspace_root(member) == (tmp_path / "ws" / "pyproject.toml")

    def test_skips_non_utf8_intermediate_pyprojects(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "ws" / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\nmembers = []\n",
        )
        broken = tmp_path / "ws" / "broken" / "pyproject.toml"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b'[project]\nname = "\xe9"\n')
        member = _write(
            tmp_path / "ws" / "broken" / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        assert discover_workspace_root(member) == (tmp_path / "ws" / "pyproject.toml")

    def test_walks_past_pyproject_without_workspace_table(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "outer" / "pyproject.toml",
            '[project]\nname = "outer"\nversion = "0"\n'
            "[tool.nab.workspace]\nmembers = []\n",
        )
        # Intermediate pyproject without [tool.nab.workspace]: walk
        # continues past it to the outer root.
        _write(
            tmp_path / "outer" / "inner" / "pyproject.toml",
            '[project]\nname = "inner"\nversion = "0"\n',
        )
        member = _write(
            tmp_path / "outer" / "inner" / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        assert discover_workspace_root(member) == (
            tmp_path / "outer" / "pyproject.toml"
        )

    def test_walks_past_non_table_tool(self, tmp_path: Path) -> None:
        # A non-table top-level ``tool`` must be skipped, not crash the walk.
        _write(
            tmp_path / "outer" / "pyproject.toml",
            '[project]\nname = "outer"\nversion = "0"\n'
            "[tool.nab.workspace]\nmembers = []\n",
        )
        _write(tmp_path / "outer" / "inner" / "pyproject.toml", 'tool = "oops"\n')
        member = _write(
            tmp_path / "outer" / "inner" / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        assert discover_workspace_root(member) == (
            tmp_path / "outer" / "pyproject.toml"
        )

    def test_walks_up_to_root_declaring_workspace_in_nab_toml(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ws"
        _write(
            root / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n',
        )
        _write(root / "nab.toml", '[workspace]\nmembers = ["pkg"]\n')
        member = _write(
            root / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        assert discover_workspace_root(member) == (root / "nab.toml")

    def test_root_pyproject_workspace_wins_over_sibling_nab_toml(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ws"
        _write(
            root / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\nmembers = []\n",
        )
        _write(root / "nab.toml", '[workspace]\nmembers = ["pkg"]\n')
        member = _write(
            root / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        assert discover_workspace_root(member) == (root / "pyproject.toml")

    def test_walks_past_non_table_tool_nab(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "outer" / "pyproject.toml",
            '[project]\nname = "outer"\nversion = "0"\n'
            "[tool.nab.workspace]\nmembers = []\n",
        )
        _write(
            tmp_path / "outer" / "inner" / "pyproject.toml",
            '[tool]\nnab = "oops"\n',
        )
        member = _write(
            tmp_path / "outer" / "inner" / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        assert discover_workspace_root(member) == (
            tmp_path / "outer" / "pyproject.toml"
        )


class TestReadWorkspaceMembers:
    def test_literal_members_become_local_sources(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["a", "sub/b"]\n',
        )
        _write(
            tmp_path / "a" / "pyproject.toml",
            '[project]\nname = "alpha"\nversion = "0"\n',
        )
        _write(
            tmp_path / "sub" / "b" / "pyproject.toml",
            '[project]\nname = "beta"\nversion = "0"\n',
        )
        sources = read_workspace_members(root)
        assert sources == (
            LocalSource(name="alpha", path=str(tmp_path / "a"), editable=True),
            LocalSource(name="beta", path=str(tmp_path / "sub" / "b"), editable=True),
        )

    def test_nab_toml_root_members_become_local_sources(self, tmp_path: Path) -> None:
        root = _write(tmp_path / "nab.toml", '[workspace]\nmembers = ["a"]\n')
        _write(
            tmp_path / "a" / "pyproject.toml",
            '[project]\nname = "alpha"\nversion = "0"\n',
        )
        assert read_workspace_members(root) == (
            LocalSource(name="alpha", path=str(tmp_path / "a"), editable=True),
        )

    def test_nab_toml_root_non_list_members_raises(self, tmp_path: Path) -> None:
        root = _write(tmp_path / "nab.toml", '[workspace]\nmembers = "not-a-list"\n')
        with pytest.raises(
            WorkspaceDiscoveryError, match=re.escape("[workspace].members must be")
        ):
            read_workspace_members(root)

    def test_members_default_to_editable(self, tmp_path: Path) -> None:
        # Workspace members install editably by default, matching uv.
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["a"]\n',
        )
        _write(
            tmp_path / "a" / "pyproject.toml",
            '[project]\nname = "alpha"\nversion = "0"\n',
        )
        (source,) = read_workspace_members(root)
        assert source.editable is True

    def test_dot_member_resolves_to_root_directory(self, tmp_path: Path) -> None:
        # ``.`` as a member entry points at the workspace root itself.
        # This is airflow's pattern.
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "apache-airflow"\nversion = "3.3.0"\n'
            "[tool.nab.workspace]\n"
            'members = [".", "core"]\n',
        )
        _write(
            tmp_path / "core" / "pyproject.toml",
            '[project]\nname = "apache-airflow-core"\nversion = "0"\n',
        )
        sources = read_workspace_members(root)
        assert sources == (
            LocalSource(name="apache-airflow", path=str(tmp_path), editable=True),
            LocalSource(
                name="apache-airflow-core",
                path=str(tmp_path / "core"),
                editable=True,
            ),
        )

    def test_glob_in_members_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["providers/*"]\n',
        )
        with pytest.raises(WorkspaceDiscoveryError, match="globs in"):
            read_workspace_members(root)

    def test_question_mark_glob_in_members_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg-?"]\n',
        )
        with pytest.raises(WorkspaceDiscoveryError, match="globs in"):
            read_workspace_members(root)

    def test_bracket_glob_in_members_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg-[ab]"]\n',
        )
        with pytest.raises(WorkspaceDiscoveryError, match="globs in"):
            read_workspace_members(root)

    def test_member_with_nul_raises(self, tmp_path: Path) -> None:
        # An embedded NUL is valid TOML, so the parser hands it through.
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pk\\u0000g"]\n',
        )
        with pytest.raises(
            WorkspaceDiscoveryError, match="is not a usable filesystem path"
        ):
            read_workspace_members(root)

    def test_member_without_pyproject_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["missing"]\n',
        )
        (tmp_path / "missing").mkdir()
        with pytest.raises(WorkspaceDiscoveryError, match="has no pyproject.toml"):
            read_workspace_members(root)

    def test_member_without_project_name_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg"]\n',
        )
        _write(
            tmp_path / "pkg" / "pyproject.toml",
            "[build-system]\nrequires = []\n",
        )
        with pytest.raises(WorkspaceDiscoveryError, match=r"\[project\]\.name"):
            read_workspace_members(root)

    def test_member_non_table_project_raises(self, tmp_path: Path) -> None:
        # ``project = "x"`` (a scalar) is a common slip for ``[project]``;
        # it must raise a clean WorkspaceDiscoveryError, not AttributeError.
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg"]\n',
        )
        _write(
            tmp_path / "pkg" / "pyproject.toml",
            'project = "broken"\n',
        )
        with pytest.raises(
            WorkspaceDiscoveryError, match=r"\[project\] must be a table"
        ):
            read_workspace_members(root)

    def test_member_malformed_toml_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg"]\n',
        )
        member = _write(
            tmp_path / "pkg" / "pyproject.toml",
            "name = 'pkg-b\n",
        )
        with pytest.raises(
            WorkspaceDiscoveryError,
            match=rf"{re.escape(str(member))} is not valid TOML",
        ):
            read_workspace_members(root)

    def test_root_malformed_toml_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            "name = 'ws\n",
        )
        with pytest.raises(
            WorkspaceDiscoveryError, match=rf"{re.escape(str(root))} is not valid TOML"
        ):
            read_workspace_members(root)

    def test_member_non_utf8_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg"]\n',
        )
        member = tmp_path / "pkg" / "pyproject.toml"
        member.parent.mkdir(parents=True)
        member.write_bytes(b'[project]\nname = "pkg"\ndescription = "\xe9"\n')
        with pytest.raises(
            WorkspaceDiscoveryError,
            match=rf"{re.escape(str(member))} is not valid TOML",
        ):
            read_workspace_members(root)

    def test_member_unreadable_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg"]\n',
        )
        member = _write(
            tmp_path / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        with (
            _deny_open(member),
            pytest.raises(
                WorkspaceDiscoveryError,
                match=rf"cannot read {re.escape(str(member))}.*Permission denied",
            ),
        ):
            read_workspace_members(root)

    def test_member_in_unsearchable_directory_raises(
        self,
        tmp_path: Path,
        deny_access: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        # EACCES on the presence check's stat must not escape raw, and the
        # member is there, so it is not the has-no-pyproject.toml error.
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg"]\n',
        )
        member = _write(
            tmp_path / "pkg" / "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0"\n',
        )
        with (
            deny_access(member),
            pytest.raises(
                WorkspaceDiscoveryError,
                match=rf"cannot read {re.escape(str(member))}.*Permission denied",
            ),
        ):
            read_workspace_members(root)

    def test_duplicate_canonical_name_raises(self, tmp_path: Path) -> None:
        # ``A`` and ``a`` canonicalise to the same name.
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["one", "two"]\n',
        )
        _write(
            tmp_path / "one" / "pyproject.toml",
            '[project]\nname = "Pkg-Foo"\nversion = "0"\n',
        )
        _write(
            tmp_path / "two" / "pyproject.toml",
            '[project]\nname = "pkg_foo"\nversion = "0"\n',
        )
        with pytest.raises(WorkspaceDiscoveryError, match="duplicate canonical name"):
            read_workspace_members(root)

    def test_non_list_members_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = "not-a-list"\n',
        )
        with pytest.raises(WorkspaceDiscoveryError, match="must be a list"):
            read_workspace_members(root)

    def test_workspace_value_not_table_raises(self, tmp_path: Path) -> None:
        # ``tool.nab.workspace = "string"`` parses as a non-table value.
        # ``discover_workspace_root`` only checks key presence; the
        # type check lives in ``read_workspace_members``.
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab]\n"
            'workspace = "not-a-table"\n',
        )
        with pytest.raises(WorkspaceDiscoveryError, match="must be a table"):
            read_workspace_members(root)

    def test_non_string_entry_raises(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            "members = [123]\n",
        )
        with pytest.raises(WorkspaceDiscoveryError, match="entries must be"):
            read_workspace_members(root)


class TestMergeWorkspaceLocalSources:
    def test_explicit_only(self) -> None:
        explicit = (LocalSource("a", "/a"), LocalSource("b", "/b"))
        out = merge_workspace_local_sources(explicit, ())
        assert out == explicit

    def test_discovered_only(self) -> None:
        discovered = (LocalSource("a", "/a"), LocalSource("b", "/b"))
        out = merge_workspace_local_sources((), discovered)
        assert out == discovered

    def test_explicit_wins_over_workspace_member(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        explicit = (LocalSource("foo", "/explicit/foo"),)
        discovered = (
            LocalSource("foo", "/workspace/foo"),
            LocalSource("bar", "/workspace/bar"),
        )
        with caplog.at_level(logging.INFO, logger="nab_python.workspace"):
            out = merge_workspace_local_sources(explicit, discovered)
        assert out == (
            LocalSource("foo", "/explicit/foo"),
            LocalSource("bar", "/workspace/bar"),
        )
        assert any("shadowed by explicit" in r.message for r in caplog.records)

    def test_collision_uses_canonical_name(self) -> None:
        explicit = (LocalSource("Pkg-Foo", "/explicit"),)
        discovered = (LocalSource("pkg_foo", "/workspace"),)
        out = merge_workspace_local_sources(explicit, discovered)
        # The explicit entry survives; the canonically-equal discovered
        # entry is shadowed.
        assert out == explicit
