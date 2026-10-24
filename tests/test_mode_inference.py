"""Infer mode through project files, configuration inspection and real CLI commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import tomli
import tomli_w

from nab.cli import run
from nab.config.model import plan_targets, read_pyproject_config
from nab.config.subflags import BY_PARENT, build_cli_tables
from nab_provider._vendor.packaging.markers import Environment
from nab_provider._vendor.packaging.pylock import Pylock, PylockSelectError
from nab_provider.errors import ConfigError
from nab_provider.policy import BuildPolicy, ResolveMode

if TYPE_CHECKING:
    from pathlib import Path


def _project(
    directory: Path,
    matrix: str,
    *,
    declaration: str = "pyproject",
    mode: str | None = None,
) -> Path:
    """Write a dependency-free project and a matrix in either project config file."""
    path = directory / "pyproject.toml"
    project: dict[str, object] = {
        "project": {"name": "example", "version": "1.0", "dependencies": []}
    }
    settings: dict[str, object] = {"matrix": tomli.loads(matrix)}
    if mode is not None:
        settings["mode"] = mode
    if declaration == "pyproject":
        project["tool"] = {"nab": settings}
    else:
        (directory / "nab.toml").write_text(tomli_w.dumps(settings), encoding="utf-8")
    path.write_text(tomli_w.dumps(project), encoding="utf-8")
    return path


def _consumer_environment(python: str, *, windows: bool = False) -> Environment:
    """Describe a CPython consumer independently of Nab's target planner."""
    return Environment(
        implementation_name="cpython",
        implementation_version=f"{python}.0",
        os_name="nt" if windows else "posix",
        platform_machine="AMD64" if windows else "x86_64",
        platform_python_implementation="CPython",
        platform_release="",
        platform_system="Windows" if windows else "Linux",
        platform_version="",
        python_full_version=f"{python}.0",
        python_version=python,
        sys_platform="win32" if windows else "linux",
    )


def test_project_python_support_does_not_enable_universal_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname = "example"\nversion = "1.0"\n'
        'requires-python = ">=3.10,<3.16"\ndependencies = []\n',
        encoding="utf-8",
    )

    assert run(("config", "get", "mode", "--path", str(path))) == 0
    assert capsys.readouterr().out.strip() == "specific"

    (target,) = plan_targets(read_pyproject_config(path, discover_workspace=False))

    assert target.host_faithful


@pytest.mark.parametrize("declaration", ["pyproject", "nab.toml"])
@pytest.mark.parametrize(
    ("matrix", "mode", "target_count"),
    [
        pytest.param(
            'python = "==3.11"\nplatforms = ["linux_x86_64"]\n',
            ResolveMode.SPECIFIC,
            1,
            id="singleton",
        ),
        pytest.param(
            'python = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n',
            ResolveMode.UNIVERSAL,
            2,
            id="python-minors",
        ),
        pytest.param(
            'python = "==3.11"\nplatforms = ["linux_x86_64", "windows_amd64"]\n',
            ResolveMode.UNIVERSAL,
            2,
            id="platforms",
        ),
        pytest.param(
            'python = "==3.11"\nplatforms = ["linux_x86_64"]\n'
            'implementations = ["cpython", "pypy"]\n',
            ResolveMode.UNIVERSAL,
            2,
            id="implementations",
        ),
        pytest.param(
            'python = "==3.11"\nplatforms = ["linux_x86_64"]\n'
            '[overrides.implementations.pypy]\npython = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64", "windows_amd64"]\n',
            ResolveMode.SPECIFIC,
            1,
            id="disabled-override",
        ),
        pytest.param(
            'python = ">=3.10,<3.15"\nplatforms = ["linux_x86_64", "windows_amd64"]\n'
            'implementations = ["pypy"]\n[overrides.implementations.pypy]\n'
            'python = "==3.11"\nplatforms = ["windows_amd64"]\n',
            ResolveMode.SPECIFIC,
            1,
            id="enabled-override-narrows-all-axes",
        ),
        pytest.param(
            'python = "==3.11"\nplatforms = ["linux_x86_64"]\n'
            'python-patches = { "3.11" = "3.11.4" }\n',
            ResolveMode.SPECIFIC,
            1,
            id="patch-pin",
        ),
    ],
)
def test_mode_uses_effective_declared_targets(
    tmp_path: Path,
    declaration: str,
    matrix: str,
    mode: ResolveMode,
    target_count: int,
) -> None:
    path = _project(tmp_path, matrix, declaration=declaration)
    config = read_pyproject_config(path, discover_workspace=False)

    assert config.mode is mode
    assert len(plan_targets(config)) == target_count
    assert config.build_policy is BuildPolicy.NEVER


@pytest.mark.parametrize("mode", ["specific", "universal"])
def test_explicit_mode_keeps_a_single_declared_target(
    tmp_path: Path, mode: str
) -> None:
    path = _project(
        tmp_path,
        'python = "==3.11"\nplatforms = ["windows_amd64"]\n',
        mode=mode,
    )
    config = read_pyproject_config(path, discover_workspace=False)

    assert config.mode.value == mode

    (target,) = plan_targets(config)

    assert target.python_version == "3.11"
    assert target.platform_id == "windows_amd64"


def test_cli_narrowing_changes_the_inferred_mode(tmp_path: Path) -> None:
    path = _project(tmp_path, 'python = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n')
    params: dict[str, object] = {
        str(row.cli_param): None for rows in BY_PARENT.values() for row in rows
    }
    params["project_matrix_python"] = "==3.11"
    tables = build_cli_tables(params)
    config = read_pyproject_config(path, discover_workspace=False, cli_overrides=tables)

    assert config.mode is ResolveMode.SPECIFIC
    assert len(plan_targets(config)) == 1


def test_explicit_specific_cli_override_suppresses_an_inferred_matrix(
    tmp_path: Path,
) -> None:
    path = _project(tmp_path, 'python = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n')
    config = read_pyproject_config(
        path, discover_workspace=False, cli_overrides={"mode": "specific"}
    )

    assert config.mode is ResolveMode.SPECIFIC
    assert config.matrix is None
    assert plan_targets(config)[0].host_faithful


def test_explicit_specific_file_mode_rejects_multiple_targets(tmp_path: Path) -> None:
    path = _project(
        tmp_path,
        'python = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n',
        mode="specific",
    )
    with pytest.raises(ConfigError, match="mode is 'specific'"):
        read_pyproject_config(path, discover_workspace=False)


@pytest.mark.parametrize(
    ("matrix", "mode"),
    [
        pytest.param(
            'python = "==3.11"\nplatforms = ["linux_x86_64"]\n',
            "specific",
            id="singleton",
        ),
        pytest.param(
            'python = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n',
            "universal",
            id="multiple-minors",
        ),
        pytest.param(
            'python = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n'
            'implementations = ["pypy"]\n[overrides.implementations.pypy]\n'
            'python = "==3.11"\n',
            "specific",
            id="enabled-override-narrows-python",
        ),
    ],
)
def test_config_cli_reports_the_inferred_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], matrix: str, mode: str
) -> None:
    path = _project(tmp_path, matrix)

    assert run(("config", "get", "mode", "--path", str(path))) == 0
    assert capsys.readouterr().out.strip() == mode
    assert run(("config", "explain", "mode", "--path", str(path))) == 0
    assert "inferred from matrix" in capsys.readouterr().out


def test_singleton_matrix_locks_its_target_and_supports_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _project(tmp_path, 'python = "==3.11"\nplatforms = ["windows_amd64"]\n')
    monkeypatch.chdir(tmp_path)
    command = ("lock", str(path), "--offline", "--no-cache")

    assert run(command) == 0

    output = tmp_path / "pylock.toml"
    original = output.read_bytes()
    document = tomli.loads(original.decode())

    assert document["tool"]["nab"]["mode"] == "specific"
    assert document["tool"]["nab"]["python-specifier"] == "==3.11"

    pylock = Pylock.from_dict(document)
    pylock.validate()
    for platform in ("windows_amd64", "linux_x86_64"):
        environment = _consumer_environment("3.11", windows=platform == "windows_amd64")
        if platform == "windows_amd64":
            assert list(pylock.select(environment=environment, tags=[])) == []
        else:
            with pytest.raises(PylockSelectError):
                list(pylock.select(environment=environment, tags=[]))

    assert run((*command, "--locked")) == 0
    assert output.read_bytes() == original


def test_inferred_universal_rejects_the_short_python_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _project(tmp_path, 'python = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n')

    assert run(("lock", str(path), "--python", "3.11", "--offline")) == 1
    assert "--python is not supported in universal mode" in capsys.readouterr().err


@pytest.mark.parametrize("explicit", [False, True])
def test_universal_lock_records_its_targets_and_refuses_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    python = "==3.11" if explicit else ">=3.11,<3.13"
    path = _project(
        tmp_path,
        f'python = "{python}"\nplatforms = ["linux_x86_64"]\n',
        mode="universal" if explicit else None,
    )
    monkeypatch.chdir(tmp_path)
    command = ("lock", str(path), "--offline", "--no-cache")

    assert run(command) == 0

    output = tmp_path / "pylock.toml"
    original = output.read_bytes()
    document = tomli.loads(original.decode())

    assert document["tool"]["nab"]["mode"] == "universal"

    pylock = Pylock.from_dict(document)
    pylock.validate()

    assert pylock.environments is not None
    assert len(pylock.environments) == (1 if explicit else 2)

    for minor in ("3.11", "3.12", "3.13"):
        environment = _consumer_environment(minor)
        if minor == "3.11" or (minor == "3.12" and not explicit):
            assert list(pylock.select(environment=environment, tags=[])) == []
        else:
            with pytest.raises(PylockSelectError):
                list(pylock.select(environment=environment, tags=[]))

    assert run((*command, "--locked")) == 1
    assert output.read_bytes() == original


@pytest.mark.parametrize("python", ["==3.11", ">=3.11,<3.13"])
def test_inferred_mode_selects_the_default_requirements_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    python: str,
) -> None:
    path = _project(tmp_path, f'python = "{python}"\nplatforms = ["linux_x86_64"]\n')
    monkeypatch.chdir(tmp_path)

    assert (
        run(("lock", str(path), "--offline", "--no-cache", "--format", "requirements"))
        == 0
    )

    output = capsys.readouterr().out
    if python == "==3.11":
        assert (tmp_path / "requirements.txt").read_text().strip() == ""
        assert output == ""
    else:
        assert not (tmp_path / "requirements.txt").exists()
        assert output
