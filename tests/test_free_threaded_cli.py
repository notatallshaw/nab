"""Resolve both CPython wheel ABIs through the public CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nab.cli import run
from nab_project.resolve import ResolveResult, resolve_for_targets
from nab_project.toml_io import loads
from nab_provider._vendor.packaging.pylock import Pylock
from nab_provider._vendor.packaging.tags import Tag
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

from ._local_artifacts import make_wheel_archive, publish_artifacts

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


def _project(
    tmp_path: Path,
    *,
    declaration: str = "pyproject",
    include_threaded: bool = True,
    free_threaded: str | None = None,
) -> Path:
    """Declare a local index and build selections on the requested config surface."""
    project = tmp_path / "pyproject.toml"
    matrix = '[tool.nab.matrix]\npython = "==3.14"\nplatforms = ["linux_x86_64"]\n'
    if free_threaded is not None:
        matrix += f"free-threaded = {free_threaded}\n"
    if include_threaded:
        matrix += "[[tool.nab.matrix.include]]\nfree-threaded = true\n"

    project.write_text(
        '[project]\nname = "example"\nversion = "1.0"\ndependencies = []\n'
        '[dependency-groups]\ndev = ["demo"]\n'
        '[tool.nab]\ndist-policy = "wheel-only"\n'
        f'[[tool.nab.indexes]]\nname = "local"\nurl = "{(tmp_path / "index").as_uri()}"\n'
        + (matrix if declaration == "pyproject" else ""),
        encoding="utf-8",
    )
    if declaration == "nab.toml":
        (tmp_path / "nab.toml").write_text(
            matrix.replace("tool.nab.matrix", "matrix"), encoding="utf-8"
        )
    return project


def _publish(tmp_path: Path, version: str, abi: str, *dependencies: str) -> str:
    """Add one native wheel with target-specific dependency metadata."""
    tag = f"cp314-{abi}-manylinux_2_17_x86_64"
    filename = f"demo-{version}-{tag}.whl"
    metadata = f"Metadata-Version: 2.1\nName: demo\nVersion: {version}\n" + "".join(
        f"Requires-Dist: {dependency}\n" for dependency in dependencies
    )
    index = tmp_path / "index"
    existing = {path.name: path.read_bytes() for path in (index / "demo").glob("*.whl")}
    publish_artifacts(
        index,
        "demo",
        {**existing, filename: make_wheel_archive("demo", version, metadata, tag)},
    )
    return filename


def _lock(project: Path, *flags: str) -> int:
    return run(
        (
            "lock",
            str(project),
            "--all-groups",
            "--offline",
            "--no-cache",
            "--output",
            str(project.with_name("pylock.toml")),
            *flags,
        )
    )


def test_lock_reconciles_a_build_specific_transitive_dependency(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _publish(tmp_path, "1", "cp314")
    _publish(tmp_path, "1", "cp314t")
    _publish(tmp_path, "2", "cp314", "helper")
    tag = "cp314-cp314-manylinux_2_17_x86_64"
    publish_artifacts(
        tmp_path / "index",
        "helper",
        {
            f"helper-1-{tag}.whl": make_wheel_archive(
                "helper", "1", "Metadata-Version: 2.1\nName: helper\nVersion: 1\n", tag
            )
        },
    )

    assert _lock(project) == 0
    lock = Pylock.from_dict(loads(project.with_name("pylock.toml").read_text()))
    for free_threaded in (False, True):
        target = ResolveTarget.for_declared(
            python_version="3.14",
            spec=PlatformSpec("linux_x86_64", free_threaded=free_threaded),
        )
        selected = lock.select(
            environment=target.marker_env,
            tags=target.tags.ordered,
            dependency_groups=("dev",),
        )
        assert [
            (str(package.name), str(package.version)) for package, _ in selected
        ] == [("demo", "1")]

    assert _lock(project, "--locked") == 0
    assert _lock(project, "--reproduce") == 0


def test_locked_keeps_a_valid_snapshot_when_new_versions_appear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    _publish(tmp_path, "1", "cp314")
    _publish(tmp_path, "1", "cp314t")
    assert _lock(project) == 0
    committed = (tmp_path / "pylock.toml").read_bytes()
    _publish(tmp_path, "2", "cp314")
    _publish(tmp_path, "2", "cp314t")
    assert _lock(project, "--reproduce") == 1

    def unexpected(_backend: object) -> None:
        pytest.fail("checking a saved snapshot must not create a transport")

    monkeypatch.setattr("nab._lock._make_resolve_transport", unexpected)
    assert _lock(project, "--locked") == 0
    assert (tmp_path / "pylock.toml").read_bytes() == committed


@pytest.mark.parametrize(
    "flags",
    [("--upgrade",), ("--reproduce",), ("--format", "requirements"), ("--output", "-")],
)
def test_locked_refuses_incompatible_modes(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    assert _lock(_project(tmp_path), "--locked", *flags) == 1


def test_locked_missing_evidence_reports_the_update_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    assert _lock(project, "--locked") == 1
    assert "run `nab lock" in capsys.readouterr().err


def test_locked_reads_current_local_dependency_bounds(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _publish(tmp_path, "1", "cp314")
    _publish(tmp_path, "1", "cp314t")
    source = tmp_path / "local"
    source.mkdir()
    metadata = source / "pyproject.toml"
    metadata.write_text(
        '[project]\nname="local"\nversion="1"\ndependencies=["demo>=1"]\n'
    )
    project.write_text(
        project.read_text().replace("dependencies = []", 'dependencies = ["local"]')
        + f'[[tool.nab.local-sources]]\nname="local"\npath="{source.as_posix()}"\n'
    )
    assert _lock(project) == 0
    committed = (tmp_path / "pylock.toml").read_bytes()
    metadata.write_text(metadata.read_text().replace('version="1"', 'version="2"'))
    assert _lock(project, "--locked") == 0
    metadata.write_text(metadata.read_text().replace("demo>=1", "demo>=2"))
    assert _lock(project, "--locked") == 1
    assert (tmp_path / "pylock.toml").read_bytes() == committed


@pytest.mark.parametrize("declaration", ["pyproject", "nab.toml", "cli", "include"])
def test_one_lock_selects_both_wheel_abis(tmp_path: Path, declaration: str) -> None:
    regular = _publish(tmp_path, "1.0", "cp314")
    threaded = _publish(tmp_path, "1.0", "cp314t")
    project = _project(
        tmp_path,
        declaration="pyproject" if declaration == "include" else declaration,
        include_threaded=declaration == "include",
        free_threaded="[false, true]"
        if declaration in {"pyproject", "nab.toml"}
        else None,
    )
    flags = (
        (
            "--project-matrix-python",
            "==3.14",
            "--project-matrix-platforms",
            "linux_x86_64",
            "--project-matrix-free-threaded",
            "false",
            "true",
        )
        if declaration == "cli"
        else ()
    )
    assert _lock(project, *flags) == 0

    document = loads(project.with_name("pylock.toml").read_text(encoding="utf-8"))
    lock = Pylock.from_dict(document)
    lock.validate()
    assert len(lock.packages) == 1
    assert {wheel.name for wheel in lock.packages[0].wheels or ()} == {
        regular,
        threaded,
    }
    target = ResolveTarget.for_declared(
        python_version="3.14", spec=PlatformSpec("linux_x86_64")
    )
    for abi, filename in (("cp314", regular), ("cp314t", threaded)):
        selected = list(
            lock.select(
                environment=target.marker_env,
                dependency_groups=["dev"],
                tags=[Tag("cp314", abi, "manylinux_2_17_x86_64")],
            )
        )
        assert len(selected) == 1
        assert selected[0][1].name == filename


@pytest.mark.parametrize(("threaded", "abi"), [(False, "cp314"), (True, "cp314t")])
def test_cli_can_replace_the_base_feature_flag(
    tmp_path: Path, threaded: bool, abi: str
) -> None:
    regular = _publish(tmp_path, "1.0", abi)
    _publish(tmp_path, "2.0", "cp314t" if abi == "cp314" else "cp314")
    project = _project(tmp_path, include_threaded=False)
    with project.open("a", encoding="utf-8") as stream:
        stream.write(f"free-threaded = {str(not threaded).lower()}\n")
    assert _lock(project, "--project-matrix-free-threaded", str(threaded).lower()) == 0
    document = loads(project.with_name("pylock.toml").read_text(encoding="utf-8"))
    assert [wheel["name"] for wheel in document["packages"][0]["wheels"]] == [regular]


def test_incompatible_abi_pins_leave_the_existing_lock_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(tmp_path, "2.0", "cp314")
    _publish(tmp_path, "1.0", "cp314t")
    project = _project(tmp_path)
    output = project.with_name("pylock.toml")
    output.write_text("existing lock\n", encoding="utf-8")
    assert _lock(project) == 1
    assert output.read_text(encoding="utf-8") == "existing lock\n"
    error = capsys.readouterr().err
    assert "demo" in error
    assert "2.0" in error
    assert "1.0" in error


def test_config_reports_explicit_include_features(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    assert run(("config", "get", "matrix", "--path", str(project))) == 0
    assert "include=[{'free-threaded': True}]" in capsys.readouterr().out
    with project.open("a", encoding="utf-8") as stream:
        stream.write(
            '[[tool.nab.matrix.include]]\npython = "==3.15"\nfree-threaded = false\n'
        )
    assert run(("config", "get", "matrix", "--path", str(project))) == 0
    assert "{'python': '==3.15', 'free-threaded': False}" in capsys.readouterr().out


@pytest.mark.parametrize("threaded", [False, True])
def test_config_reports_an_explicit_base_feature(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], threaded: bool
) -> None:
    project = _project(tmp_path, include_threaded=False)
    with project.open("a", encoding="utf-8") as stream:
        stream.write(f"free-threaded = {str(threaded).lower()}\n")
    assert run(("config", "get", "matrix", "--path", str(project))) == 0
    assert f"free-threaded={threaded}" in capsys.readouterr().out


@pytest.mark.parametrize("where", ["base", "override", "include"])
def test_config_reports_feature_lists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    project = _project(tmp_path, include_threaded=False)
    table = {
        "base": "",
        "override": "[tool.nab.matrix.overrides.implementations.cpython]\n",
        "include": "[[tool.nab.matrix.include]]\n",
    }[where]
    with project.open("a", encoding="utf-8") as stream:
        stream.write(table + "free-threaded = [false, true]\n")
    assert run(("config", "get", "matrix", "--path", str(project))) == 0
    output = capsys.readouterr().out
    expected = (
        "free-threaded=[False, True]"
        if where == "base"
        else "'free-threaded': [False, True]"
    )
    assert expected in output


def test_a_missing_free_threaded_wheel_refuses_the_lock(tmp_path: Path) -> None:
    _publish(tmp_path, "1.0", "cp314")
    project = _project(tmp_path)
    assert _lock(project) == 1
    assert not project.with_name("pylock.toml").exists()


def test_build_specific_dependencies_refuse_a_shared_lock(tmp_path: Path) -> None:
    _publish(tmp_path, "1.0", "cp314", "ordinary-dep")
    _publish(tmp_path, "1.0", "cp314t", "threaded-dep")
    for name in ("ordinary-dep", "threaded-dep"):
        wheel_name = name.replace("-", "_")
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
        publish_artifacts(
            tmp_path / "index",
            name,
            {
                f"{wheel_name}-1.0-py3-none-any.whl": make_wheel_archive(
                    wheel_name, "1.0", metadata, "py3-none-any"
                )
            },
        )
    project = _project(tmp_path)
    assert _lock(project) == 1
    assert not project.with_name("pylock.toml").exists()


@pytest.mark.parametrize("change", ["none", "artifact", "targets"])
def test_universal_locked_checks_content_without_rewriting(
    tmp_path: Path, change: str
) -> None:
    ordinary = _publish(tmp_path, "1.0", "cp314")
    _publish(tmp_path, "1.0", "cp314t")
    project = _project(tmp_path)
    project.write_text(
        project.read_text().replace("[tool.nab]\n", '[tool.nab]\nmode = "universal"\n')
    )
    assert _lock(project) == 0
    output = project.with_name("pylock.toml")
    before = output.read_bytes()
    if change == "artifact":
        directory = tmp_path / "index" / "demo"
        artifacts = {path.name: path.read_bytes() for path in directory.glob("*.whl")}
        artifacts[ordinary] = make_wheel_archive(
            "demo",
            "1.0",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\nSummary: changed artifact\n",
            "cp314-cp314-manylinux_2_17_x86_64",
        )
        publish_artifacts(tmp_path / "index", "demo", artifacts)
    elif change == "targets":
        project.write_text(
            project.read_text().replace(
                "[[tool.nab.matrix.include]]\nfree-threaded = true\n",
                "",
            )
        )

    assert _lock(project, "--locked") == (0 if change == "none" else 1)
    assert output.read_bytes() == before


def test_universal_locked_does_not_apply_another_environments_constraint(
    tmp_path: Path,
) -> None:
    publish_artifacts(
        tmp_path / "index",
        "demo",
        {
            "demo-1.0-py3-none-any.whl": make_wheel_archive(
                "demo",
                "1.0",
                "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
                "py3-none-any",
            )
        },
    )
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "example"\nversion = "1.0"\n'
        "dependencies = [\"demo==1.0; sys_platform == 'win32'\"]\n"
        '[tool.nab]\nmode = "universal"\n'
        "constraints = [\"demo>=2; sys_platform == 'linux'\"]\n"
        f'[[tool.nab.indexes]]\nname = "local"\nurl = "{(tmp_path / "index").as_uri()}"\n'
        '[tool.nab.matrix]\npython = "==3.14"\nplatforms = ["linux_x86_64", "windows_amd64"]\n',
        encoding="utf-8",
    )
    assert _lock(project) == 0
    before = project.with_name("pylock.toml").read_bytes()
    assert _lock(project, "--locked") == 0
    assert project.with_name("pylock.toml").read_bytes() == before


def test_inputs_changing_during_resolution_do_not_receive_validation_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish(tmp_path, "1.0", "cp314")
    _publish(tmp_path, "1.0", "cp314t")
    project = _project(tmp_path)

    def changing_inputs(*args: Any, **kwargs: Any) -> ResolveResult:
        result = resolve_for_targets(*args, **kwargs)
        project.write_text(
            project.read_text().replace('dev = ["demo"]', 'dev = ["demo>=2"]')
        )
        return result

    monkeypatch.setattr("nab._resolve.resolve_for_targets", changing_inputs)
    assert _lock(project) == 1
    assert not project.with_name("pylock.toml").exists()


@pytest.mark.parametrize("declaration", ["pyproject", "nab.toml"])
def test_specific_free_threaded_target_excludes_newer_standard_wheel(
    tmp_path: Path, declaration: str
) -> None:
    threaded = _publish(tmp_path, "1.0", "cp314t")
    _publish(tmp_path, "2.0", "cp314")
    _publish(tmp_path, "3.0", "abi3")
    project = _project(tmp_path, declaration="cli")
    environment = (
        '[tool.nab.environment]\npython = "3.14"\n'
        'platform = { id = "linux_x86_64", free-threaded = true }\n'
    )
    flags: tuple[str, ...] = ()
    if declaration == "pyproject":
        with project.open("a", encoding="utf-8") as stream:
            stream.write(environment)
    elif declaration == "nab.toml":
        (tmp_path / "nab.toml").write_text(
            environment.replace("[tool.nab.environment]", "[environment]"),
            encoding="utf-8",
        )
    else:
        flags = (
            "--project-environment-python",
            "3.14",
            "--project-environment-platform",
            "linux_x86_64",
            "free-threaded=true",
        )
    assert _lock(project, *flags) == 0
    document = loads(project.with_name("pylock.toml").read_text(encoding="utf-8"))
    package = document["packages"][0]
    assert package["version"] == "1.0"
    assert [wheel["name"] for wheel in package["wheels"]] == [threaded]


def test_locked_refuses_a_different_host_build_abi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, declaration="cli")
    _publish(tmp_path, "1.0", "cp314")
    standard = ResolveTarget.for_host(
        tags_source=lambda: (
            Tag("cp314", "cp314", "manylinux_2_17_x86_64"),
            Tag("py3", "none", "any"),
        )
    )
    threaded = ResolveTarget.for_host(
        tags_source=lambda: (
            Tag("cp314", "cp314t", "manylinux_2_17_x86_64"),
            Tag("py3", "none", "any"),
        )
    )
    assert standard.marker_env == threaded.marker_env
    monkeypatch.setattr(ResolveTarget, "for_host", classmethod(lambda _cls: standard))
    assert _lock(project) == 0
    output = project.with_name("pylock.toml")
    before = output.read_bytes()
    monkeypatch.setattr(ResolveTarget, "for_host", classmethod(lambda _cls: threaded))

    def unexpected(_backend: object) -> None:
        pytest.fail("checking another build must not create a transport")

    monkeypatch.setattr("nab._lock._make_resolve_transport", unexpected)
    assert _lock(project, "--locked") == 1
    assert output.read_bytes() == before


def test_locked_target_reordering_keeps_the_nonplanning_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    _publish(tmp_path, "1.0", "cp314")
    _publish(tmp_path, "1.0", "cp314t")
    assert _lock(project) == 0
    output = project.with_name("pylock.toml")
    before = output.read_bytes()
    project.write_text(
        project.read_text().replace(
            "[[tool.nab.matrix.include]]\nfree-threaded = true",
            "free-threaded = true\n[[tool.nab.matrix.include]]\nfree-threaded = false",
        )
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("reordering targets must not plan or resolve")

    monkeypatch.setattr("nab._lock._make_resolve_transport", unexpected)
    monkeypatch.setattr("nab_project.lock_check.plan_project_inputs", unexpected)
    assert _lock(project, "--locked") == 0
    assert output.read_bytes() == before
