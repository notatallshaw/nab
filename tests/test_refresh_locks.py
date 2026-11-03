"""Lock refresh refuses incompatible inputs and runs in its active environment."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

TASK = Path(__file__).resolve().parents[1] / "tasks/refresh_locks.py"


@pytest.fixture
def task() -> ModuleType:
    spec = importlib.util.spec_from_file_location("refresh_locks_task", TASK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workspace(root: Path) -> None:
    declaration = (
        '[build-system]\nrequires=["backend"]\nbuild-backend="backend.build"\n'
    )
    (root / "pyproject.toml").write_text(declaration)
    child = root / "nab-child"
    child.mkdir()
    (child / "pyproject.toml").write_text(declaration)


def _lock(root: Path, name: str, version: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'lock-version="1.0"\ncreated-by="test"\n[[packages]]\nname="backend"\n'
        f'version="{version}"\n[[packages.wheels]]\n'
        f'url="https://example.com/backend-{version}-py3-none-any.whl"\n'
        '[packages.wheels.hashes]\nsha256="' + "a" * 64 + '"\n'
    )


def test_build_mismatch_stops_before_generation(
    tmp_path: Path, task: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace(tmp_path)
    (tmp_path / "nab-child/pyproject.toml").write_text(
        '[build-system]\nrequires=["other"]\nbuild-backend="other.build"\n'
    )

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("different build systems must be refused before generation")

    monkeypatch.setattr(task.subprocess, "run", unexpected)
    assert task.main(["--locked"], tmp_path) == 1
    assert not (tmp_path / "pylock.dev.toml").exists()


@pytest.mark.parametrize("exit_code", [0, 1])
def test_generation_uses_the_active_python_and_preserves_failure(
    tmp_path: Path, task: ModuleType, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    _workspace(tmp_path)
    _lock(tmp_path, "pylock.dev.toml", "1")
    _lock(tmp_path, ".github/requirements/pylock.build.toml", "1")
    calls = []

    def invoke(
        command: Sequence[str], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(command))
        assert cwd == tmp_path
        assert check is False
        return subprocess.CompletedProcess(command, exit_code)

    monkeypatch.setattr(task.subprocess, "run", invoke)
    assert task.main(["--locked"], tmp_path) == exit_code
    assert len(calls) == (2 if exit_code == 0 else 1)
    assert calls[0][:4] == (sys.executable, "-m", "nab", "lock")
    assert "--all-groups" in calls[0]
    assert calls[0][-1] == "--locked"
    if exit_code == 0:
        assert "--build-requirements" in calls[1]


def test_shared_version_mismatch_is_refused(
    tmp_path: Path, task: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace(tmp_path)
    _lock(tmp_path, "pylock.dev.toml", "2")
    _lock(tmp_path, ".github/requirements/pylock.build.toml", "1")
    monkeypatch.setattr(
        task.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )
    assert task.main(["--locked"], tmp_path) == 1
