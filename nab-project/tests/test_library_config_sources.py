"""Project configuration works independently of CLI settings and imports."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from nab_project.config import (
    ConfigConflictError,
    ConfigConflictKind,
    plan_targets,
    read_pyproject_config,
)
from nab_project.config_values import ProjectFileConflictError, SourceConfigError
from nab_provider.errors import ConfigError
from nab_provider.policy import ResolutionStrategy, ResolveMode

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


def project(directory: Path, config: str = "") -> Path:
    """Write a project whose configuration can be read without index access."""
    path = directory / "pyproject.toml"
    path.write_text(
        '[project]\nname = "demo"\nversion = "1"\ndependencies = []\n' + config,
        encoding="utf-8",
    )
    return path


def test_resolve_with_the_cli_package_unavailable(tmp_path: Path) -> None:
    path = project(tmp_path)
    script = textwrap.dedent(
        """\
        import importlib.abc
        import pathlib
        import sys

        class RefuseCLI(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "nab" or fullname.startswith("nab."):
                    raise AssertionError("library imported CLI: " + fullname)

        sys.meta_path.insert(0, RefuseCLI())
        from nab_project.config import read_pyproject_config, plan_targets
        from nab_project.resolve import resolve_for_targets
        from nab_index.urllib3_async_transport import Urllib3AsyncTransport

        path = pathlib.Path(sys.argv[1])
        config = read_pyproject_config(path, discover_workspace=False)
        result = resolve_for_targets(
            path, Urllib3AsyncTransport(), targets=plan_targets(config),
            inputs=config.resolve_inputs(), include_dependency_requirements=True,
            cache_dir=path.parent / "cache",
        )
        result.raise_for_failure()
        assert result.target_results[0].pins == {}
        assert result.target_results[0].dependency_requirements() == {}
        assert not any(name == "nab" or name.startswith("nab.") for name in sys.modules)
        """
    )
    result = subprocess.run(  # noqa: S603 - the child runs this test's fixed program.
        [sys.executable, "-I", "-c", script, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_host_settings_and_process_environment_do_not_configure_a_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project(tmp_path)
    (tmp_path / "nab.toml").write_text(
        'cache-dir = false\nhttp-backend = "host-specific"\n'
        'offline = "host-specific"\nmax-concurrency = -1\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("NAB_RESOLUTION", "lowest")
    monkeypatch.setenv("NAB_HTTP_BACKEND", "invalid")

    config = read_pyproject_config(path)

    assert config.inputs.resolution is ResolutionStrategy.HIGHEST
    assert config.inputs.indexes[0].url == "https://pypi.org/simple/"


def test_adjacent_project_settings_are_applied(tmp_path: Path) -> None:
    path = project(tmp_path)
    (tmp_path / "nab.toml").write_text('resolution = "lowest"\n', encoding="utf-8")

    assert read_pyproject_config(path).inputs.resolution is ResolutionStrategy.LOWEST


@pytest.mark.parametrize(
    ("configuration", "kind"),
    [
        pytest.param(
            '[tool.nab]\nmode = "universal"\n',
            ConfigConflictKind.MISSING_MATRIX,
            id="missing-matrix",
        ),
        pytest.param(
            '[tool.nab]\nmode = "specific"\n[tool.nab.matrix]\n'
            'python = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n',
            ConfigConflictKind.SPECIFIC_MATRIX,
            id="specific-matrix",
        ),
    ],
)
def test_project_conflicts_have_structured_kinds(
    tmp_path: Path, configuration: str, kind: ConfigConflictKind
) -> None:
    path = project(tmp_path, configuration)

    with pytest.raises(ConfigConflictError) as error:
        read_pyproject_config(path)

    assert error.value.kind is kind


@pytest.mark.parametrize(
    "setting", ["offline", "cache-dir", "http-backend", "max-concurrency"]
)
def test_host_setting_cannot_be_a_project_setting(tmp_path: Path, setting: str) -> None:
    path = project(tmp_path, f"[tool.nab]\n{setting} = false\n")

    with pytest.raises(SourceConfigError, match="user-scope option"):
        read_pyproject_config(path)


def test_matching_project_files_share_the_cutoff_anchor(tmp_path: Path) -> None:
    path = project(tmp_path, '[tool.nab]\nuploaded-prior-to = "P7D"\n')
    (tmp_path / "nab.toml").write_text('uploaded-prior-to = "P7D"\n', encoding="utf-8")
    anchor = datetime(2026, 11, 6, tzinfo=timezone.utc)

    config = read_pyproject_config(path, anchor=anchor)

    assert config.inputs.uploaded_prior_to == anchor - timedelta(days=7)


def test_conflicting_project_files_are_refused(tmp_path: Path) -> None:
    path = project(tmp_path, '[tool.nab]\nresolution = "highest"\n')
    (tmp_path / "nab.toml").write_text('resolution = "lowest"\n', encoding="utf-8")

    with pytest.raises(ProjectFileConflictError, match="conflicting values") as error:
        read_pyproject_config(path)
    assert error.value.key == "resolution"
    assert error.value.input_kind == "pyproject"
    assert error.value.input_value is ResolutionStrategy.HIGHEST
    assert error.value.project_value is ResolutionStrategy.LOWEST


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(b"unknown = true\n", id="unknown"),
        pytest.param(b"[[", id="toml"),
        pytest.param(b"\xff", id="encoding"),
    ],
)
def test_invalid_standalone_file_is_refused(tmp_path: Path, contents: bytes) -> None:
    path = project(tmp_path)
    (tmp_path / "nab.toml").write_bytes(contents)

    with pytest.raises(SourceConfigError):
        read_pyproject_config(path)


def test_standalone_file_cannot_be_a_directory(tmp_path: Path) -> None:
    path = project(tmp_path)
    (tmp_path / "nab.toml").mkdir()

    with pytest.raises(SourceConfigError, match="not a regular file"):
        read_pyproject_config(path)


def test_unreadable_standalone_file_is_reported(
    tmp_path: Path,
    deny_access: Callable[[Path], AbstractContextManager[None]],
) -> None:
    path = project(tmp_path)
    standalone = tmp_path / "nab.toml"
    standalone.write_text('resolution = "lowest"\n', encoding="utf-8")

    with (
        deny_access(standalone),
        pytest.raises(SourceConfigError, match="cannot read .*nab.toml"),
    ):
        read_pyproject_config(path)


def test_relative_project_sources_use_the_declaring_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project(
        tmp_path,
        '[[tool.nab.local-sources]]\nname = "child"\npath = "child"\n',
    )
    (tmp_path / "child").mkdir()
    monkeypatch.chdir(tmp_path.parent)

    config = read_pyproject_config(Path(tmp_path.name) / path.name)

    assert config.inputs.local_sources[0].path == str(tmp_path / "child")


def test_standalone_matrix_infers_the_resolve_mode(tmp_path: Path) -> None:
    path = project(tmp_path)
    (tmp_path / "nab.toml").write_text(
        '[matrix]\npython = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n',
        encoding="utf-8",
    )

    config = read_pyproject_config(path)

    assert config.mode is ResolveMode.UNIVERSAL
    assert len(plan_targets(config)) == 2


def test_invalid_project_input_still_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "missing.toml"

    with pytest.raises(ConfigError, match="cannot read"):
        read_pyproject_config(path)
