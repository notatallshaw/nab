"""The lock updater preserves signatures and refuses raced or failed updates."""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

TASK = Path(__file__).resolve().parents[1] / "tasks/update_lock_pr.py"
LOCK = b"""lock-version="1.0"
created-by="nab"
[[packages]]
name="setuptools"
version="81.0.0"
[tool.nab]
created-at=2026-11-01T00:00:00Z
command-line=["nab", "lock"]
[tool.nab.validation]
version=1
all-inputs-sha256="inputs"
contents-sha256="contents"
"""


@pytest.fixture
def task() -> ModuleType:
    spec = importlib.util.spec_from_file_location("update_lock_pr_task", TASK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b"81.0.0", b"82.0.1"),
        (b"inputs", b"other-inputs"),
        (b"contents", b"other-contents"),
        (b'created-by="nab"', b'created-by="other"'),
    ],
)
def test_content_and_validation_changes_are_not_ignored(
    task: ModuleType,
    before: bytes,
    after: bytes,
) -> None:
    assert not task.same_lock(LOCK, LOCK.replace(before, after))


def test_only_refresh_provenance_is_ignored(task: ModuleType) -> None:
    after = LOCK.replace(b"2026-11-01", b"2026-11-08").replace(
        b'["nab", "lock"]',
        b'["nab", "lock", "--upgrade"]',
    )
    assert task.same_lock(LOCK, after)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", [None, "lookup", "commit", "verification", "push"])
def test_publication_handles_existing_prs_and_external_failures(
    tmp_path: Path,
    task: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    failure: str | None,
) -> None:
    calls = []
    changed = LOCK.replace(b"81.0.0", b"82.0.1")
    for name in task.LOCKS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(changed if name == "pylock.dev.toml" else LOCK)

    def invoke(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """Answer only the Git and GitHub subprocess boundary."""
        assert kwargs["cwd"] == tmp_path
        assert kwargs["check"] is True
        payload = kwargs["input"]
        calls.append((command, payload))
        output = ""
        if command[:2] == ("git", "show"):
            output = LOCK.decode()
        elif command[:2] == ("git", "ls-remote"):
            output = "old-head\trefs/heads/ci/update-locks" if existing else ""
        elif command[:3] == ("gh", "pr", "list"):
            if failure == "lookup":
                raise subprocess.CalledProcessError(1, command)
            output = "123" if existing else ""
        elif command[:2] == ("git", "rev-parse"):
            output = "base-head"
        elif "graphql" in command:
            if failure == "commit":
                raise subprocess.CalledProcessError(1, command)
            assert isinstance(payload, str)
            request = json.loads(payload)["variables"]["input"]
            assert request["expectedHeadOid"] == "base-head"
            assert request["fileChanges"]["additions"] == [
                {
                    "path": "pylock.dev.toml",
                    "contents": base64.b64encode(changed).decode(),
                }
            ]
            output = "signed-head"
        elif ".commit.verification" in " ".join(command):
            output = "false unsigned" if failure == "verification" else "true valid"
        elif "push" in command:
            expected = "old-head" if existing else ""
            assert (
                f"--force-with-lease=refs/heads/ci/update-locks:{expected}" in command
            )
            assert command[-1] == "signed-head:refs/heads/ci/update-locks"
            if failure == "push":
                raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(task.subprocess, "run", invoke)
    if failure is not None:
        with pytest.raises((subprocess.CalledProcessError, ValueError)):
            task.publish(tmp_path, "owner/repo")
    else:
        assert task.publish(tmp_path, "owner/repo")

    commands = [command for command, _ in calls]
    assert any("DELETE" in command for command in commands) == (failure != "lookup")
    assert any(command[:3] == ("gh", "pr", "create") for command in commands) == (
        failure is None and not existing
    )
    if failure in ("lookup", "commit", "verification"):
        assert not any("push" in command for command in commands)


def test_unchanged_locks_do_not_touch_github(
    tmp_path: Path,
    task: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in task.LOCKS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(LOCK.replace(b"2026-11-01", b"2026-11-08"))

    def invoke(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert command[:2] == ("git", "show")
        return subprocess.CompletedProcess(command, 0, stdout=LOCK.decode())

    monkeypatch.setattr(task.subprocess, "run", invoke)
    assert not task.publish(tmp_path, "owner/repo")
