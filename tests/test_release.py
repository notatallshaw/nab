"""Tests for the release tooling in tasks/release.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from packaging.version import InvalidVersion

pytest.importorskip("tomlkit")

_PATH = Path(__file__).resolve().parents[1] / "tasks" / "release.py"
_spec = importlib.util.spec_from_file_location("nab_release_tasks", _PATH)
assert _spec is not None
assert _spec.loader is not None
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def _make_tree(tmp_path: Path, version: str, pin: str) -> tuple[Path, Path]:
    root = tmp_path / "pyproject.toml"
    member = tmp_path / "nab-python" / "pyproject.toml"
    member.parent.mkdir()
    root.write_text(
        f'[project]\nname = "nab"\nversion = "{version}"\n'
        f'dependencies = ["nab-python=={pin}"]\n',
        encoding="utf-8",
    )
    member.write_text(
        f'[project]\nname = "nab-python"\nversion = "{version}"\n'
        'dependencies = ["typing_extensions>=4.6"]\n',
        encoding="utf-8",
    )
    return root, member


def test_cross_pin() -> None:
    assert release.cross_pin("0.0.3") == "==0.0.3"


def test_is_dev_version() -> None:
    assert release.is_dev_version("0.0.4.dev0")
    assert not release.is_dev_version("0.0.3")
    assert not release.is_dev_version("0.0.3a0")
    assert not release.is_dev_version("not-a-version")


def test_is_release_version() -> None:
    assert release.is_release_version("0.0.3")
    assert release.is_release_version("0.0.3rc1")
    assert not release.is_release_version("0.0.3.dev0")
    assert not release.is_release_version("0.0.3+local")
    assert not release.is_release_version("not-a-version")


def test_next_dev_version() -> None:
    assert release.next_dev_version("0.0.3") == "0.0.4.dev0"
    assert release.next_dev_version("1.2.0") == "1.2.1.dev0"


def test_next_dev_version_rejects_invalid() -> None:
    with pytest.raises(InvalidVersion):
        release.next_dev_version("not-a-version")


def test_rewrite_requirement_updates_workspace_pins() -> None:
    assert (
        release._rewrite_requirement("nab-resolver==0.0.2", "0.0.3")
        == "nab-resolver==0.0.3"
    )
    assert (
        release._rewrite_requirement("nab-index[httpx]==0.0.2", "0.0.3")
        == "nab-index[httpx]==0.0.3"
    )


def test_rewrite_requirement_leaves_third_party_alone() -> None:
    assert release._rewrite_requirement("tyro>=1.0", "0.0.3") == "tyro>=1.0"
    assert release._rewrite_requirement("packaging>=24.0", "0.0.3") == "packaging>=24.0"


def test_apply_version_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, member = _make_tree(tmp_path, "0.0.2", "0.0.2")
    monkeypatch.setattr(release, "PYPROJECT_PATHS", (root, member))
    release.apply_version("0.0.3")
    assert release.read_current_version() == "0.0.3"
    assert '"nab-python==0.0.3"' in root.read_text(encoding="utf-8")
    assert "typing_extensions>=4.6" in member.read_text(encoding="utf-8")


def test_read_current_version_rejects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text('[project]\nname = "a"\nversion = "0.0.2"\n', encoding="utf-8")
    b.write_text('[project]\nname = "b"\nversion = "0.0.3"\n', encoding="utf-8")
    monkeypatch.setattr(release, "PYPROJECT_PATHS", (a, b))
    with pytest.raises(ValueError, match="not in lockstep"):
        release.read_current_version()


def test_check_release_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, member = _make_tree(tmp_path, "0.0.3", "0.0.3")
    monkeypatch.setattr(release, "PYPROJECT_PATHS", (root, member))
    release.check_release("v0.0.3")


def test_check_release_rejects_missing_v(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, member = _make_tree(tmp_path, "0.0.3", "0.0.3")
    monkeypatch.setattr(release, "PYPROJECT_PATHS", (root, member))
    with pytest.raises(SystemExit, match="must start with"):
        release.check_release("0.0.3")


def test_check_release_rejects_tag_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, member = _make_tree(tmp_path, "0.0.3", "0.0.3")
    monkeypatch.setattr(release, "PYPROJECT_PATHS", (root, member))
    with pytest.raises(SystemExit, match="does not match"):
        release.check_release("v0.0.4")


def test_check_release_rejects_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, member = _make_tree(tmp_path, "0.0.3.dev0", "0.0.3.dev0")
    monkeypatch.setattr(release, "PYPROJECT_PATHS", (root, member))
    with pytest.raises(SystemExit, match="dev version"):
        release.check_release("v0.0.3.dev0")


def test_check_release_rejects_stale_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, member = _make_tree(tmp_path, "0.0.3", "0.0.2")
    monkeypatch.setattr(release, "PYPROJECT_PATHS", (root, member))
    with pytest.raises(SystemExit, match="pinned"):
        release.check_release("v0.0.3")


def test_plan_returns_branch_and_tag() -> None:
    assert release._plan("0.0.3", "0.0.3.dev0", None) == (
        "0.0.3",
        "0.0.4.dev0",
        "release/0.0.3",
        "v0.0.3",
    )


def test_plan_respects_explicit_next_dev() -> None:
    assert release._plan("0.0.3", "0.0.3.dev0", "0.1.0.dev0") == (
        "0.0.3",
        "0.1.0.dev0",
        "release/0.0.3",
        "v0.0.3",
    )


def test_plan_rejects_non_release_version() -> None:
    with pytest.raises(SystemExit, match="not a release version"):
        release._plan("0.0.3.dev0", "0.0.2.dev0", None)


def test_plan_rejects_non_dev_current() -> None:
    with pytest.raises(SystemExit, match="expected a .dev"):
        release._plan("0.0.3", "0.0.2", None)


def test_plan_rejects_bad_next_dev() -> None:
    with pytest.raises(SystemExit, match="not a dev version"):
        release._plan("0.0.3", "0.0.3.dev0", "0.1.0")


def test_github_slug() -> None:
    assert (
        release._github_slug("https://github.com/notatallshaw/nab.git")
        == "notatallshaw/nab"
    )
    assert (
        release._github_slug("git@github.com:notatallshaw/nab.git")
        == "notatallshaw/nab"
    )
    assert release._github_slug("https://gitlab.com/x/y.git") is None
