"""Lock sparse target matrices through the public command-line interface."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nab.cli import run
from nab_project.toml_io import loads
from nab_provider._vendor.packaging.pylock import Pylock, PylockSelectError
from nab_provider._vendor.packaging.tags import Tag
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

from ._local_artifacts import make_wheel_archive, publish_artifacts

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("implementation", ["cpython", "pypy"])
def test_config_inspection_keeps_empty_override_bodies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], implementation: str
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname="example"\nversion="1"\n'
        '[tool.nab.matrix]\npython="==3.15"\nplatforms=["linux_x86_64"]\n'
        f"[tool.nab.matrix.overrides.implementations.{implementation}]\n",
        encoding="utf-8",
    )
    assert run(["config", "get", "matrix", "--path", str(project)]) == 0
    assert f"'{implementation}': {{}}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("selection", "count"), [(None, 3), ("cpython", 2), ("pypy", 1)]
)
def test_override_follows_cli_implementation_selection(
    tmp_path: Path, selection: str | None, count: int
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname="example"\nversion="1"\ndependencies=[]\n'
        '[tool.nab.matrix]\npython=">=3.10,<3.12"\nplatforms=["linux_x86_64"]\n'
        'implementations=["cpython","pypy"]\n'
        '[tool.nab.matrix.overrides.implementations.pypy]\npython="==3.11"\n',
        encoding="utf-8",
    )
    output = tmp_path / "pylock.toml"
    arguments = [
        "lock",
        str(project),
        "--offline",
        "--no-cache",
        "--output",
        str(output),
    ]
    if selection is not None:
        arguments.extend(["--project-matrix-implementations", selection])
    assert run(arguments) == 0
    lock = Pylock.from_dict(loads(output.read_text(encoding="utf-8")))
    lock.validate()
    assert lock.environments is not None
    assert len(lock.environments) == count
    if selection == "pypy":
        assert 'python_version == "3.11"' in str(lock.environments[0])
        assert 'implementation_name == "pypy"' in str(lock.environments[0])


@pytest.mark.parametrize("threaded", [False, True])
def test_config_inspection_retains_implementation_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], threaded: bool
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname="example"\nversion="1"\n'
        '[tool.nab.matrix]\npython="==3.15"\nplatforms=["linux_x86_64"]\n'
        "[tool.nab.matrix.overrides.implementations.cpython]\n"
        'python="==3.15"\nplatforms=["windows_amd64"]\n'
        f"free-threaded={str(threaded).lower()}\n",
        encoding="utf-8",
    )
    assert run(["config", "get", "matrix", "--path", str(project)]) == 0
    output = capsys.readouterr().out
    assert "'python': '==3.15'" in output
    assert "'platforms': ['windows_amd64']" in output
    assert f"'free-threaded': {threaded}" in output


@pytest.mark.parametrize(
    "musl_version", ["1.0", "2.0"], ids=["same-pin", "different-pins"]
)
def test_include_selects_libc_variants_through_one_lock(
    tmp_path: Path, musl_version: str
) -> None:
    index = tmp_path / "index"
    artifacts = {}
    for platform, version in [
        ("manylinux_2_17_x86_64", "1.0"),
        ("musllinux_1_2_x86_64", musl_version),
    ]:
        tag = f"cp315-cp315-{platform}"
        filename = f"demo-{version}-{tag}.whl"
        metadata = f"Metadata-Version: 2.1\nName: demo\nVersion: {version}\n"
        artifacts[filename] = make_wheel_archive("demo", version, metadata, tag)
    publish_artifacts(index, "demo", artifacts)
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "example"\nversion = "1.0"\ndependencies = ["demo"]\n'
        '[tool.nab]\ndist-policy = "wheel-only"\n'
        f'[[tool.nab.indexes]]\nname = "local"\nurl = "{index.as_uri()}"\n'
        '[tool.nab.matrix]\npython = "==3.15"\nplatforms = ["linux_x86_64"]\n'
        "[[tool.nab.matrix.include]]\n"
        'platforms = [{ id = "linux_x86_64", libc = "musl", runs-on-libc = "1.2" }]\n',
        encoding="utf-8",
    )
    output = tmp_path / "pylock.toml"
    output.write_text("existing lock\n", encoding="utf-8")
    status = run(
        ["lock", str(project), "--offline", "--no-cache", "--output", str(output)]
    )
    if musl_version != "1.0":
        assert status == 1
        assert output.read_text(encoding="utf-8") == "existing lock\n"
        return

    assert status == 0
    lock = Pylock.from_dict(loads(output.read_text(encoding="utf-8")))
    lock.validate()
    assert {
        wheel.name for package in lock.packages for wheel in package.wheels or ()
    } == set(artifacts)
    environment = ResolveTarget.for_declared(
        python_version="3.15", spec=PlatformSpec("linux_x86_64")
    ).marker_env
    for platform in ("manylinux_2_17_x86_64", "musllinux_1_2_x86_64"):
        selected = list(
            lock.select(environment=environment, tags=[Tag("cp315", "cp315", platform)])
        )
        assert len(selected) == 1
        assert selected[0][1].name == f"demo-1.0-cp315-cp315-{platform}.whl"


@pytest.mark.parametrize("declaration", ["pyproject", "nab.toml"])
@pytest.mark.parametrize("shared_python", [">=3.10,<3.16", "==3.12"])
def test_lock_declares_only_selected_interpreter_targets(
    tmp_path: Path,
    declaration: str,
    shared_python: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """File overrides survive CLI changes to the shared matrix defaults."""
    project = tmp_path / "pyproject.toml"
    body = (
        '[project]\nname = "example"\nversion = "1.0"\n'
        'requires-python = ">=3.10"\ndependencies = []\n'
    )
    root = "tool.nab" if declaration == "pyproject" else ""
    matrix = f"{root}.matrix" if root else "matrix"
    config = (
        (f"[{root}]\n" if root else "")
        + 'mode = "universal"\n'
        + f'[{matrix}]\npython = ">=3.10,<3.16"\n'
        + 'platforms = ["linux_x86_64", "macos_arm64"]\n'
        + f'[[{matrix}.include]]\nimplementations = ["pypy"]\npython = "==3.11"\n'
        + 'platforms = ["linux_x86_64"]\n'
    )
    if declaration == "pyproject":
        body += config
    else:
        (tmp_path / "nab.toml").write_text(config, encoding="utf-8")
    project.write_text(body, encoding="utf-8")
    flags = ["--project-matrix-python", shared_python]

    assert run(["config", "get", "matrix", "--path", str(project), *flags]) == 0
    rendered = capsys.readouterr().out
    assert "implementations=['cpython']" in rendered
    assert (
        "include=[{'python': '==3.11', 'platforms': ['linux_x86_64'], 'implementations': ['pypy']}]"
        in rendered
    )

    output = tmp_path / "pylock.toml"
    assert (
        run(
            [
                "lock",
                str(project),
                "--offline",
                "--no-cache",
                "--format",
                "pylock",
                "--output",
                str(output),
                *flags,
            ]
        )
        == 0
    )

    pylock = Pylock.from_dict(loads(output.read_text(encoding="utf-8")))
    pylock.validate()
    assert pylock.requires_python is not None
    assert str(pylock.requires_python) == ">=3.10"
    assert pylock.environments is not None
    assert len(pylock.environments) == (13 if shared_python == ">=3.10,<3.16" else 3)

    for minor in ("3.10", "3.11", "3.12", "3.13", "3.14", "3.15"):
        for platform in ("linux_x86_64", "macos_arm64", "windows_amd64"):
            for implementation in ("cpython", "pypy"):
                target = ResolveTarget.for_declared(
                    python_version=minor,
                    spec=PlatformSpec(platform),
                    implementation=implementation,
                )
                cpython_supported = (
                    implementation == "cpython"
                    and platform != "windows_amd64"
                    and (shared_python == ">=3.10,<3.16" or minor == "3.12")
                )
                pypy_supported = (
                    implementation == "pypy"
                    and minor == "3.11"
                    and platform == "linux_x86_64"
                )
                if cpython_supported or pypy_supported:
                    assert (
                        list(pylock.select(environment=target.marker_env, tags=[]))
                        == []
                    )
                else:
                    with pytest.raises(PylockSelectError):
                        list(pylock.select(environment=target.marker_env, tags=[]))


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("", "include=[{'implementations': ['pypy']}]"),
        (
            'python = "==3.11"\n',
            "include=[{'python': '==3.11', 'implementations': ['pypy']}]",
        ),
        (
            'platforms = ["windows_amd64"]\n',
            "include=[{'platforms': ['windows_amd64'], 'implementations': ['pypy']}]",
        ),
    ],
)
def test_config_inspection_shows_only_explicit_include_fields(
    tmp_path: Path, body: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inspection distinguishes inherited axes from explicitly replaced values."""
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "example"\nversion = "1.0"\ndependencies = []\n'
        '[tool.nab]\nmode = "universal"\n'
        '[tool.nab.matrix]\npython = "==3.12"\n'
        'platforms = ["linux_x86_64"]\n'
        '[[tool.nab.matrix.include]]\nimplementations = ["pypy"]\n' + body,
        encoding="utf-8",
    )
    assert run(["config", "get", "matrix", "--path", str(project)]) == 0
    assert expected in capsys.readouterr().out
