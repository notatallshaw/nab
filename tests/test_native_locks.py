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
def native_project(tmp_path: Path) -> Path:
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
        '[tool.nab.plans.install]\ngroups=["tools"]\nformat="pdm"\noutput="pdm.lock"\n'
    )
    return project


def test_pdm_installs_plan_and_transitive_dependency(
    native_project: Path, tmp_path: Path
) -> None:
    pdm = shutil.which("pdm")
    if pdm is None:
        pytest.skip("requires the PDM consumer executable")

    assert run(("lock", str(native_project), "--plan", "install", "--no-cache")) == 0
    output = tmp_path / "pdm.lock"
    saved = output.read_bytes()
    env = {
        **os.environ,
        "PDM_CHECK_UPDATE": "false",
        "PDM_IGNORE_ACTIVE_VENV": "1",
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }

    def consume(*args: str) -> None:
        """Run the installed consumer in the isolated project."""
        subprocess.run(  # noqa: S603 - fixed PDM commands in the temporary project.
            [pdm, *args], cwd=tmp_path, env=env, check=True, timeout=60
        )

    environment = tmp_path / ".venv"
    venv.EnvBuilder(symlinks=os.name != "nt").create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    consume("use", "-f", str(python))
    consume("lock", "--check")
    consume("sync", "--no-self")
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
