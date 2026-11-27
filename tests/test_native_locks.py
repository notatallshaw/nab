"""Install the native lock written by a plan using the consumer's CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import venv
from typing import TYPE_CHECKING

import pytest

from nab.cli import run
from nab_provider.testing import make_metadata

from ._local_artifacts import make_wheel_archive, publish_artifacts

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def native_project(tmp_path: Path, format_name: str) -> Path:
    """Publish two installable local wheels with a transitive dependency."""
    wheels = tmp_path / "wheels"
    for name, requirements in (("nativeleaf", ()), ("nativeroot", ("nativeleaf==1",))):
        filename = f"{name}-1-py3-none-any.whl"
        metadata = make_metadata(name, "1", *requirements)
        archive = make_wheel_archive(name, "1", metadata, "py3-none-any")
        publish_artifacts(wheels, name, {filename: archive})

    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname="app"\nversion="1"\nrequires-python=">=3.10"\n'
        'dependencies=[]\n[dependency-groups]\ntools=["nativeroot"]\n'
        f'[[tool.nab.indexes]]\nname="local"\nurl="{wheels.as_uri()}"\n'
        '[tool.nab.plans.install]\ngroups=["tools"]\n'
        f'format="{format_name}"\noutput="{format_name}.lock"\n'
    )
    return project


@pytest.mark.parametrize("format_name", ["pdm", "poetry"])
def test_native_tool_installs_plan_and_transitive_dependency(
    native_project: Path, tmp_path: Path, format_name: str
) -> None:
    executable = shutil.which(format_name)
    if executable is None:
        pytest.skip(f"requires the {format_name} consumer executable")

    assert run(("lock", str(native_project), "--plan", "install", "--no-cache")) == 0
    output = tmp_path / f"{format_name}.lock"
    saved = output.read_bytes()
    env = {
        **os.environ,
        "POETRY_VIRTUALENVS_IN_PROJECT": "true",
        "PDM_CHECK_UPDATE": "false",
        "PDM_IGNORE_ACTIVE_VENV": "1",
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }

    def consume(*args: str) -> None:
        """Run the installed consumer in the isolated project."""
        subprocess.run(  # noqa: S603 - fixed consumer commands in the temporary project.
            [executable, *args], cwd=tmp_path, env=env, check=True, timeout=60
        )

    environment = tmp_path / ".venv"
    venv.EnvBuilder(symlinks=os.name != "nt").create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if format_name == "pdm":
        consume("use", "-f", str(python))
        consume("lock", "--check")
        consume("sync", "--no-self")
    else:
        env["VIRTUAL_ENV"] = str(environment)
        consume("check", "--lock")
        consume("sync", "--no-root")

    subprocess.run(  # noqa: S603 - verify fixture wheels in their own interpreter.
        [
            python,
            "-c",
            (
                "import nativeroot, nativeleaf; "
                "assert nativeroot.__version__ == nativeleaf.__version__ == '1'"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        timeout=10,
    )
    assert output.read_bytes() == saved
