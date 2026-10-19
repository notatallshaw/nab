"""Reject directory syntax in CLI file arguments."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nab.cli import run

_DIRECTORY_SUFFIXES = ["/", "//", "/.", "/./"]
if os.altsep is not None:
    _DIRECTORY_SUFFIXES.extend([os.sep, os.sep + "."])


@pytest.fixture
def project(hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty offline project with isolated configuration discovery."""
    path = hermetic_roots / "pyproject.toml"
    path.write_text('[project]\nname="demo"\nversion="1.0"\ndependencies=[]\n')
    monkeypatch.chdir(hermetic_roots)
    return path


@pytest.mark.parametrize("suffix", _DIRECTORY_SUFFIXES)
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    ("format_name", "filename"),
    [
        ("pylock", "pylock.toml"),
        ("requirements", "requirements.txt"),
        ("requirements-without-hashes", "requirements.txt"),
    ],
)
def test_directory_suffixed_output_preserves_destination(
    project: Path,
    suffix: str,
    existing: bool,
    format_name: str,
    filename: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = project.parent / filename
    original = b"# preserve existing bytes\n"
    if existing:
        destination.write_bytes(original)
    raw_path = filename + suffix

    status = run(
        (
            "lock",
            "--offline",
            "--no-cache",
            "--format",
            format_name,
            "--output",
            raw_path,
        )
    )

    if existing:
        assert destination.read_bytes() == original
    else:
        assert not destination.exists()

    assert status == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert raw_path in captured.err
    assert "file" in captured.err


@pytest.mark.parametrize("suffix", _DIRECTORY_SUFFIXES)
@pytest.mark.parametrize("command", ["config", "lock", "download"])
def test_directory_suffixed_project_is_rejected(
    project: Path,
    suffix: str,
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_path = str(project) + suffix
    original = project.read_bytes()
    arguments = (
        ("config", "get", "resolution", "--path", raw_path)
        if command == "config"
        else (command, raw_path, "--offline", "--no-cache")
    )

    assert run(arguments) == 1

    assert project.read_bytes() == original
    assert not (project.parent / "pylock.toml").exists()
    assert not (project.parent / "wheels").exists()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert raw_path in captured.err
    assert "file" in captured.err
