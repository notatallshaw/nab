"""Install the native lock written by a plan using the consumer's CLI."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from nab.cli import run
from nab_provider.testing import make_metadata

from ._local_artifacts import make_wheel_archive, publish_artifacts


def _consumer(name: str) -> Path:
    """Use the executable installed from the consumer's own lock."""
    location = os.environ.get("LOCKFILE_CONSUMER_ENVS")
    if not location:
        pytest.skip("set LOCKFILE_CONSUMER_ENVS to run native lockfile tests")

    # Resolve before the consumer changes into its temporary project.
    environment = Path(location).resolve() / name
    if os.name == "nt":
        return environment / "Scripts" / f"{name}.exe"

    return environment / "bin" / name


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


@pytest.mark.parametrize("format_name", ["pdm", "poetry", "uv"])
def test_native_tool_installs_plan_and_transitive_dependency(
    native_project: Path, tmp_path: Path, format_name: str
) -> None:
    executable = _consumer(format_name)

    assert run(("lock", str(native_project), "--plan", "install", "--no-cache")) == 0
    output = tmp_path / f"{format_name}.lock"
    saved = output.read_bytes()
    env = {
        **os.environ,
        "UV_CACHE_DIR": str(tmp_path / "cache" / "uv"),
        "UV_PYTHON_DOWNLOADS": "never",
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
    elif format_name == "uv":
        consume("sync", "--frozen", "--offline", "--python", str(python))
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


@pytest.mark.parametrize("format_name", ["uv"])
def test_uv_runs_a_script_from_nabs_lock(
    native_project: Path, format_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _consumer(format_name)

    project_dir = native_project.parent
    script = project_dir / "demo.py"
    script.write_text(
        '# /// script\n# requires-python = ">=3.10"\n# dependencies = ["nativeroot"]\n'
        f'# [[tool.nab.indexes]]\n# name = "local"\n# url = "{(project_dir / "wheels").as_uri()}"\n# ///\n'
        "import nativeroot, nativeleaf\n"
        "assert nativeroot.__version__ == nativeleaf.__version__ == '1'\n"
    )
    assert run(("lock", "--script", str(script), "--format", "uv", "--no-cache")) == 0
    lockfile = script.with_name("demo.py.lock")
    saved = lockfile.read_bytes()
    monkeypatch.setenv("UV_CACHE_DIR", str(project_dir / "uv-cache"))
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    subprocess.run(  # noqa: S603 - the script imports the two test wheels.
        [
            executable,
            "run",
            "--frozen",
            "--offline",
            "--python",
            sys.executable,
            "--script",
            str(script),
        ],
        cwd=project_dir,
        check=True,
        timeout=60,
    )
    assert lockfile.read_bytes() == saved


def test_uv_selects_each_conflicting_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv = _consumer("uv")
    wheels = tmp_path / "wheels"
    files = {}
    for version in ("1", "2"):
        filename = f"nativechoice-{version}-py3-none-any.whl"
        files[filename] = make_wheel_archive(
            "nativechoice",
            version,
            make_metadata("nativechoice", version),
            "py3-none-any",
        )
    publish_artifacts(wheels, "nativechoice", files)

    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname="app"\nversion="1"\nrequires-python=">=3.10"\n'
        '[project.optional-dependencies]\ncpu=["nativechoice==1"]\ngpu=["nativechoice==2"]\n'
        '[tool.nab]\nconflicts=[[{extra="cpu"}, {extra="gpu"}]]\n'
        f'[[tool.nab.indexes]]\nname="local"\nurl="{wheels.as_uri()}"\n'
    )
    output = tmp_path / "uv.lock"
    assert (
        run(
            (
                "lock",
                str(project),
                "--format",
                "uv",
                "--all-extras",
                "--no-cache",
                "--output",
                str(output),
            )
        )
        == 0
    )
    saved = output.read_bytes()

    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")

    # Both runs must select from the same unmodified lock.
    for extra, version in (("cpu", "1"), ("gpu", "2")):
        subprocess.run(  # noqa: S603 - install and inspect the fixture selected by the named extra.
            [
                uv,
                "run",
                "--frozen",
                "--offline",
                "--python",
                sys.executable,
                "--extra",
                extra,
                "python",
                "-c",
                f"import nativechoice; assert nativechoice.__version__ == '{version}'",
            ],
            cwd=tmp_path,
            check=True,
            timeout=60,
        )
    assert output.read_bytes() == saved


@pytest.mark.parametrize("format_name", ["uv"])
def test_uv_installs_dependencies_of_omitted_workspace_members(
    native_project: Path, format_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv = _consumer(format_name)

    project = native_project.parent
    member = project / "member"
    member.mkdir()
    (member / "pyproject.toml").write_text(
        '[project]\nname="member"\nversion="1"\ndependencies=["nativeleaf"]\n',
        encoding="utf-8",
    )
    native_project.write_text(
        '[project]\nname="app"\nversion="1"\nrequires-python=">=3.10"\n'
        'dependencies=["member"]\n[tool.nab.workspace]\nmembers=["member"]\n'
        f'[[tool.nab.indexes]]\nname="local"\nurl="{(project / "wheels").as_uri()}"\n',
        encoding="utf-8",
    )

    output = project / "uv.lock"
    args = (
        "lock",
        str(native_project),
        "--format",
        "uv",
        "--no-emit-workspace",
        "--output",
        str(output),
    )
    assert run(args) == 0
    assert run((*args, "--locked")) == 0
    assert run((*args, "--reproduce")) == 0

    monkeypatch.setenv("UV_CACHE_DIR", str(project / "cache"))
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    subprocess.run(  # noqa: S603 - frozen install of the fixture and its dependency.
        [
            uv,
            "run",
            "--frozen",
            "--offline",
            "--python",
            sys.executable,
            "python",
            "-c",
            (
                "from importlib.metadata import distributions, version; "
                "assert version('nativeleaf') == '1'; "
                "assert 'member' not in {d.metadata['Name'] for d in distributions()}"
            ),
        ],
        cwd=project,
        check=True,
        timeout=60,
    )
