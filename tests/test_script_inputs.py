"""Scripts use the real lock pipeline while retaining their own declarations."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

import pytest
import tomli

from nab._lock import lock
from nab._run import effective_config
from nab.cli import run
from nab.config.ladder import SourceKind
from nab.config.model import plan_targets, read_pyproject_config
from nab_index.local_index import LocalIndexClient
from nab_project.input_source import InputFormat
from nab_provider._vendor.packaging.pylock import Pylock
from nab_provider._vendor.packaging.tags import Tag
from nab_provider.errors import ConfigError
from nab_provider.testing import make_metadata

if TYPE_CHECKING:
    from nab_provider.records import SdistFile, WheelFile


def _wheel(directory: Path, name: str, version: str, *dependencies: str) -> Path:
    """Write a real wheel usable by both the local index and pip."""
    path = directory / f"{name}-{version}-py3-none-any.whl"
    dist_info = f"{name}-{version}.dist-info"
    with ZipFile(path, "w") as archive:
        archive.writestr(f"{name}/__init__.py", f'__version__ = "{version}"\n')
        archive.writestr(
            f"{dist_info}/METADATA", make_metadata(name, version, *dependencies)
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    package_dir = directory / name
    package_dir.mkdir(exist_ok=True)
    listing = package_dir / "index.html"
    previous = listing.read_text() if listing.exists() else ""
    listing.write_text(
        previous
        + f'<a href="../{path.name}#sha256={sha256(path.read_bytes()).hexdigest()}">{path.name}</a>\n'
    )
    return path


def _write_script(
    path: Path,
    *,
    dependencies: tuple[str, ...] = ("demo",),
    requires_python: str = ">=3.10",
    configuration: str = "",
) -> Path:
    """Write inline requirements whose Python body must never run while locking."""
    metadata = (
        f"dependencies={json.dumps(dependencies)}\n"
        f"requires-python={json.dumps(requires_python)}\n" + configuration
    )
    path.write_text(
        "# /// script\n"
        + "".join("# " + line + "\n" for line in metadata.splitlines())
        + '# ///\n\nraise RuntimeError("script body must not execute")\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def script_source(hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated script with a real two-package local wheelhouse."""
    directory = hermetic_roots / "scripts"
    directory.mkdir()
    monkeypatch.setenv("NAB_CACHE_DIR", str(directory / "cache"))
    wheels = directory / "wheels"
    wheels.mkdir()
    _wheel(wheels, "child", "1.0")
    _wheel(wheels, "demo", "1.0", "child==1.0")
    return _write_script(
        directory / "example.py",
        configuration=(
            '[tool.nab]\ndist-policy="wheel-only"\n'
            f'[[tool.nab.indexes]]\nname="local"\nurl={json.dumps(wheels.as_uri())}\n'
        ),
    )


def _lock_path(script: Path) -> Path:
    return script.with_name(f"pylock.{script.stem}.toml")


def test_native_script_lock_and_saved_solution_check(
    script_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = script_source.read_bytes()
    arguments = ("lock", "--script", str(script_source), "--no-cache")
    monkeypatch.setattr(sys, "argv", ["nab", *arguments])
    assert run(arguments) == 0
    path = _lock_path(script_source)
    data = tomli.loads(path.read_text())
    assert [(package["name"], package["version"]) for package in data["packages"]] == [
        ("child", "1.0"),
        ("demo", "1.0"),
    ]
    assert data["requires-python"] == ">=3.10"
    assert data["tool"]["nab"]["input-path"] == str(script_source)
    assert "--script" in data["tool"]["nab"]["command-line"]
    saved = path.read_bytes()
    assert run(("lock", "--script", str(script_source), "--locked", "--offline")) == 0
    assert path.read_bytes() == saved
    assert script_source.read_bytes() == original


def test_script_does_not_read_parent_project_or_workspace(
    script_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = script_source.parent.parent
    (parent / "pyproject.toml").write_text(
        '[project]\ndependencies=["must-not-resolve"]\nrequires-python=">=99"\n'
        '[tool.nab.workspace]\nmembers=["missing"]\n'
    )
    (parent / "nab.toml").write_text('default-groups=["parent-only"]\n')
    monkeypatch.chdir(parent)
    assert run(("lock", "--script", str(script_source), "--no-cache")) == 0
    assert not (parent / "pylock.toml").exists()
    assert _lock_path(script_source).exists()


@pytest.mark.parametrize("format_name", ["requirements", "requirements-without-hashes"])
def test_script_requirements_output(script_source: Path, format_name: str) -> None:
    assert run(("lock", "--script", str(script_source), "--format", format_name)) == 0
    written = script_source.with_name(
        script_source.name + ".requirements.txt"
    ).read_text()
    assert "demo==1.0" in written
    assert "child==1.0" in written
    assert ("--hash=sha256:" in written) == (format_name == "requirements")


def test_script_standard_output(
    script_source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(("lock", "--script", str(script_source), "--output", "-")) == 0
    data = tomli.loads(capsys.readouterr().out)
    assert len(data["packages"]) == 2
    assert not _lock_path(script_source).exists()


def test_script_download_uses_the_same_input(
    script_source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "downloads"
    assert (
        run(("download", "--script", str(script_source), "--output", str(output))) == 0
    )
    assert sorted(path.name for path in output.iterdir()) == [
        "child-1.0-py3-none-any.whl",
        "demo-1.0-py3-none-any.whl",
    ]
    assert not _lock_path(script_source).exists()


def test_script_configuration_has_its_own_origin(
    script_source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run(("config", "explain", "dist-policy", "--script", str(script_source))) == 0
    )
    output = capsys.readouterr().out
    assert "wheel-only" in output
    assert str(script_source) in output
    config = read_pyproject_config(script_source, input_format=InputFormat.SCRIPT)
    assert config.dist_policy.value == "wheel-only"
    assert (
        effective_config(script_source, input_format=InputFormat.SCRIPT)[
            "dist-policy"
        ].origin.kind
        is SourceKind.SCRIPT
    )


def test_script_python_bound_cannot_be_broadened_by_tool_settings(
    script_source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = script_source.read_text(encoding="utf-8").replace(
        'requires-python=">=3.10"', 'requires-python=">=3.11"'
    )
    script_source.write_text(text, encoding="utf-8")
    assert (
        run(
            (
                "lock",
                "--script",
                str(script_source),
                "--python",
                "3.10",
                "--project-requires-python",
                ">=3.10",
            )
        )
        == 1
    )
    assert "requires-python" in capsys.readouterr().err
    assert not _lock_path(script_source).exists()


@pytest.mark.parametrize(
    ("blocker", "tool_source"),
    [
        ("script", SourceKind.PROJECT_TOML),
        ("tool", SourceKind.PROJECT_TOML),
        ("tool", SourceKind.SCRIPT),
        ("tool", SourceKind.CLI),
    ],
)
def test_script_python_bound_failure_names_the_blocking_declaration(
    hermetic_roots: Path,
    capsys: pytest.CaptureFixture[str],
    blocker: str,
    tool_source: SourceKind,
) -> None:
    script_bound = ">=3.12" if blocker == "script" else ">=3.10"
    tool_bound = ">=3.12" if blocker == "tool" else ">=3.10"
    configuration = (
        f'[tool.nab]\nrequires-python="{tool_bound}"\n'
        if tool_source is SourceKind.SCRIPT
        else ""
    )
    script_source = _write_script(
        hermetic_roots / "example.py",
        dependencies=(),
        requires_python=script_bound,
        configuration=configuration,
    )
    tool_config = script_source.with_name("nab.toml")
    if tool_source is SourceKind.PROJECT_TOML:
        tool_config.write_text(f'requires-python="{tool_bound}"\n')
    arguments = (
        "lock",
        "--script",
        str(script_source),
        "--python",
        "3.11",
        "--offline",
        "--no-cache",
    )
    if tool_source is SourceKind.CLI:
        arguments += ("--project-requires-python", tool_bound)

    assert run(arguments) == 1

    if blocker == "script":
        source = f"{script_source}: PEP 723 requires-python"
    elif tool_source is SourceKind.PROJECT_TOML:
        source = f"{tool_config}: requires-python"
    elif tool_source is SourceKind.SCRIPT:
        source = f"{script_source}: tool.nab.requires-python"
    else:
        source = "--project-requires-python"
    assert (
        f"{source} = '>=3.12' excludes the resolve target Python 3.11"
        in capsys.readouterr().err
    )
    assert not _lock_path(script_source).exists()

    if blocker == "script":
        _write_script(script_source, dependencies=())
    elif tool_source is SourceKind.PROJECT_TOML:
        tool_config.write_text('requires-python=">=3.10"\n')
    elif tool_source is SourceKind.SCRIPT:
        _write_script(
            script_source,
            dependencies=(),
            configuration='[tool.nab]\nrequires-python=">=3.10"\n',
        )
    else:
        arguments = (*arguments[:-1], ">=3.10")

    assert run(arguments) == 0
    assert _lock_path(script_source).exists()


@pytest.mark.parametrize(
    "change", ["body", "relaxed-bound", "tightened-bound", "dependency", "policy"]
)
def test_script_lock_freshness(script_source: Path, change: str) -> None:
    assert run(("lock", "--script", str(script_source))) == 0
    path = _lock_path(script_source)
    original = path.read_bytes()
    text = script_source.read_text(encoding="utf-8")
    if change == "body":
        text += '\nprint("a body-only change")\n'
    elif change == "relaxed-bound":
        text = text.replace('["demo"]', '["demo>=1"]')
    elif change == "tightened-bound":
        text = text.replace('["demo"]', '["demo>=2"]')
    elif change == "dependency":
        text = text.replace('["demo"]', '["child"]')
    else:
        text = text.replace('dist-policy="wheel-only"', 'dist-policy="sdist-if-needed"')
    script_source.write_text(text, encoding="utf-8")
    expected = 0 if change in ("body", "relaxed-bound") else 1
    assert (
        run(("lock", "--script", str(script_source), "--locked", "--offline"))
        == expected
    )
    assert path.read_bytes() == original


def test_missing_script_lock_fails_without_creating_one(script_source: Path) -> None:
    assert run(("lock", "--script", str(script_source), "--locked", "--offline")) == 1
    assert not _lock_path(script_source).exists()


def test_script_reproduction(script_source: Path) -> None:
    assert run(("lock", "--script", str(script_source))) == 0
    path = _lock_path(script_source)
    original = path.read_bytes()
    assert run(("lock", "--script", str(script_source), "--reproduce")) == 0
    assert path.read_bytes() == original


@pytest.mark.parametrize("filename", ["example", "example.pyw", "example.test.py"])
def test_script_filenames_and_explicit_outputs(
    script_source: Path, filename: str
) -> None:
    renamed = script_source.with_name(filename)
    script_source.rename(renamed)
    explicit = renamed.with_name("pylock.chosen.toml")
    assert run(("lock", "--script", str(renamed), "--output", str(explicit))) == 0
    assert explicit.exists()
    if "." in renamed.stem:
        assert run(("lock", "--script", str(renamed))) == 1
    else:
        assert run(("lock", "--script", str(renamed))) == 0
        assert _lock_path(renamed).exists()


@pytest.mark.parametrize(
    "selector",
    ["--all-groups", "--all-extras", "--build-requirements", "--no-emit-workspace"],
)
def test_script_rejects_project_selectors(script_source: Path, selector: str) -> None:
    assert run(("lock", "--script", str(script_source), selector)) == 1
    assert not _lock_path(script_source).exists()


@pytest.mark.parametrize("command", ["lock", "download", "config"])
def test_script_and_explicit_default_project_are_mutually_exclusive(
    script_source: Path, command: str
) -> None:
    arguments = (
        ("config", "get", "resolution", "--path", "pyproject.toml")
        if command == "config"
        else (command, "pyproject.toml")
    )
    assert run((*arguments, "--script", str(script_source))) == 2


def test_script_matrix_locks_every_declared_target(script_source: Path) -> None:
    script_source.write_text(
        script_source.read_text(encoding="utf-8").replace(
            "# ///\n",
            '# [tool.nab.matrix]\n# python=">=3.11,<3.13"\n'
            '# platforms=["linux_x86_64", "windows_amd64"]\n# ///\n',
        ),
        encoding="utf-8",
    )
    assert run(("lock", "--script", str(script_source))) == 0
    data = tomli.loads(_lock_path(script_source).read_text())
    lock = Pylock.from_dict(data)
    targets = plan_targets(
        read_pyproject_config(script_source, input_format=InputFormat.SCRIPT)
    )
    assert len(targets) == 4
    for target in targets:
        selected = list(
            lock.select(
                environment=target.marker_env,
                tags=[
                    Tag(tag.interpreter, tag.abi, tag.platform)
                    for tag in target.tags.ordered
                ],
            )
        )
        assert {str(package.name) for package, _artifact in selected} == {
            "demo",
            "child",
        }
    assert run(("lock", "--script", str(script_source), "--locked", "--offline")) == 0


def test_relative_script_index_uses_the_script_directory(
    script_source: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script_source.write_text(
        script_source.read_text(encoding="utf-8").replace(
            (script_source.parent / "wheels").as_uri(), "file:wheels"
        ),
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert run(("lock", "--script", str(script_source))) == 0
    assert _lock_path(script_source).exists()
    assert not (elsewhere / "pylock.toml").exists()


@pytest.mark.parametrize("input_format", [InputFormat.PROJECT, InputFormat.SCRIPT])
@pytest.mark.parametrize("relative_path", ["simple", "route/../simple"])
def test_relative_file_index_preserves_symlink_base(
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    input_format: InputFormat,
    relative_path: str,
) -> None:
    packages = hermetic_roots / "packages"
    packages.mkdir()
    _wheel(packages, "child", "1.0")
    _wheel(packages, "demo", "1.0", "child==1.0")

    stored_index = tmp_path / "storage" / "simple"
    for name in ("demo", "child"):
        page_dir = stored_index / name
        page_dir.mkdir(parents=True)
        filename = f"{name}-1.0-py3-none-any.whl"
        digest = sha256((packages / filename).read_bytes()).hexdigest()
        (page_dir / "index.html").write_text(
            f'<a href="../../packages/{filename}#sha256={digest}">{name}</a>\n'
        )

    index = hermetic_roots / "simple"
    index.symlink_to(stored_index, target_is_directory=True)
    physical_route = tmp_path / "storage" / "nested"
    physical_route.mkdir()
    (hermetic_roots / "route").symlink_to(physical_route, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    expected_url = (hermetic_roots / relative_path).as_uri()
    for url_kind, url in (
        ("absolute", expected_url),
        ("relative", "file:" + relative_path),
    ):
        configuration = f'[[tool.nab.indexes]]\nname="local"\nurl={json.dumps(url)}\n'
        if input_format is InputFormat.SCRIPT:
            source = _write_script(
                hermetic_roots / "example.py", configuration=configuration
            )
            arguments = ("--script", str(source))
        else:
            source = hermetic_roots / "pyproject.toml"
            source.write_text(
                '[project]\nname="app"\nversion="1.0"\ndependencies=["demo"]\n'
                + configuration
            )
            arguments = (str(source),)

        output = hermetic_roots / f"pylock.{url_kind}.toml"
        assert (
            run(
                ("lock", *arguments, "--no-cache", "--offline", "--output", str(output))
            )
            == 0
        )

        packages_locked = tomli.loads(output.read_text())["packages"]
        pins = [(package["name"], package["version"]) for package in packages_locked]
        assert pins == [("child", "1.0"), ("demo", "1.0")]


def test_local_script_source_metadata_is_checked_fresh(script_source: Path) -> None:
    local = script_source.parent / "local"
    local.mkdir()
    project = local / "pyproject.toml"
    project.write_text(
        '[project]\nname="local"\nversion="1.0"\ndependencies=["child>=1"]\n'
    )
    text = script_source.read_text(encoding="utf-8").replace('["demo"]', '["local"]')
    script_source.write_text(
        text.replace(
            "# ///\n",
            '# [[tool.nab.local-sources]]\n# name="local"\n# path="local"\n# ///\n',
        ),
        encoding="utf-8",
    )
    assert run(("lock", "--script", str(script_source))) == 0
    saved = _lock_path(script_source).read_bytes()
    assert run(("lock", "--script", str(script_source), "--locked", "--offline")) == 0
    project.write_text(project.read_text().replace("child>=1", "child>=2"))
    assert run(("lock", "--script", str(script_source), "--locked", "--offline")) == 1
    assert _lock_path(script_source).read_bytes() == saved


@pytest.mark.parametrize("change", ["body", "dependencies", "malformed"])
def test_script_input_changes_during_resolution_are_handled(
    script_source: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = script_source.read_text(encoding="utf-8")
    if change == "body":
        altered = original + '\nprint("body change")\n'
    elif change == "dependencies":
        altered = original.replace('["demo"]', '["child"]')
    else:
        altered = original.replace("# /// script", "# incomplete metadata")
    get_files = LocalIndexClient.get_files

    async def changed_files(
        self: LocalIndexClient, package: str
    ) -> list[WheelFile | SdistFile]:
        files = await get_files(self, package)
        if package == "demo":
            await asyncio.to_thread(script_source.write_text, altered)
        return files

    monkeypatch.setattr(LocalIndexClient, "get_files", changed_files)
    expected = 0 if change == "body" else 1
    assert run(("lock", "--script", str(script_source))) == expected
    assert _lock_path(script_source).exists() == (expected == 0)
    if expected:
        assert "script inputs changed" in capsys.readouterr().err


def test_script_output_is_preserved_when_an_index_is_unavailable(
    script_source: Path,
) -> None:
    assert run(("lock", "--script", str(script_source))) == 0
    output = _lock_path(script_source)
    before = output.read_bytes()
    script_source.write_text(
        script_source.read_text(encoding="utf-8").replace('["demo"]', '["missing"]'),
        encoding="utf-8",
    )
    assert run(("lock", "--script", str(script_source), "--offline")) == 1
    assert output.read_bytes() == before


def test_script_inline_and_directory_policies_conflict_explicitly(
    script_source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (script_source.parent / "nab.toml").write_text('dist-policy="sdist-if-needed"\n')
    assert run(("lock", "--script", str(script_source))) == 1
    error = capsys.readouterr().err
    assert "conflicting values" in error
    assert "script" in error
    assert not _lock_path(script_source).exists()


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        pytest.param("offline=true", "project-scope", id="user-setting"),
        pytest.param('default-groups=["dev"]', "script inputs", id="default-group"),
        pytest.param('base-group="runtime"', "script inputs", id="base-group"),
        pytest.param('build-group="build"', "script inputs", id="build-group"),
        pytest.param(
            'conflicts=[[{group="a"},{group="b"}]]', "script inputs", id="conflict"
        ),
        pytest.param('workspace={members=["member"]}', "script inputs", id="workspace"),
    ],
)
def test_script_refuses_inapplicable_inline_settings(
    script_source: Path,
    setting: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_source.write_text(
        script_source.read_text(encoding="utf-8").replace(
            "# [tool.nab]\n", f"# [tool.nab]\n# {setting}\n"
        ),
        encoding="utf-8",
    )
    assert run(("lock", "--script", str(script_source))) == 1
    assert expected in capsys.readouterr().err
    assert not _lock_path(script_source).exists()


def test_script_without_python_requirement_locks_for_the_host(
    script_source: Path,
) -> None:
    script_source.write_text(
        script_source.read_text(encoding="utf-8").replace(
            '# requires-python=">=3.10"\n', ""
        ),
        encoding="utf-8",
    )
    assert run(("lock", "--script", str(script_source))) == 0
    assert "requires-python" not in tomli.loads(_lock_path(script_source).read_text())


def test_malformed_script_is_a_clean_cli_error(
    script_source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script_source.write_text(
        "# /// script\n# dependencies=[\n# ///\n", encoding="utf-8"
    )
    assert run(("lock", "--script", str(script_source))) == 1
    captured = capsys.readouterr()
    assert "PEP 723" in captured.err
    assert "Traceback" not in captured.err
    assert not _lock_path(script_source).exists()


def test_script_urls_are_not_local_input_files() -> None:
    assert run(("lock", "--script", "https://example.invalid/script.py")) == 2


def test_direct_lock_call_rejects_a_second_input(script_source: Path) -> None:
    with pytest.raises(SystemExit, match="1"):
        lock(Path("other.toml"), script=script_source)


def test_direct_script_directory_is_not_a_lock_input() -> None:
    with pytest.raises(SystemExit, match="1"):
        lock(script=Path())


def test_invalid_relative_file_index_is_a_clean_error(
    script_source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script_source.write_text(
        script_source.read_text(encoding="utf-8").replace(
            (script_source.parent / "wheels").as_uri(), "file:bad%00path"
        ),
        encoding="utf-8",
    )
    assert run(("lock", "--script", str(script_source))) == 1
    assert "invalid file-index URL" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mode", ["inline-universal", "cli-universal", "inline-specific"]
)
def test_matrix_diagnostic_can_be_repaired_in_the_script(
    script_source: Path, mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    matrix = (
        '# [tool.nab.matrix]\n# python=">=3.11,<3.13"\n'
        '# platforms=["linux_x86_64", "windows_amd64"]\n'
    )
    text = script_source.read_text(encoding="utf-8")
    if mode == "inline-universal":
        text = text.replace("# [tool.nab]\n", '# [tool.nab]\n# mode="universal"\n')
    elif mode == "inline-specific":
        text = text.replace("# [tool.nab]\n", '# [tool.nab]\n# mode="specific"\n')
        text = text.replace("# ///\n", matrix + "# ///\n")
    script_source.write_text(text, encoding="utf-8")
    arguments = ("lock", "--script", str(script_source))
    if mode == "cli-universal":
        arguments += ("--project-mode", "universal")
    assert run(arguments) == 1
    message = capsys.readouterr().err
    assert "script's metadata" in message
    assert "pyproject.toml" not in message

    if mode == "inline-specific":
        text = text.replace('mode="specific"', 'mode="universal"')
    else:
        text = text.replace("# ///\n", matrix + "# ///\n")
    script_source.write_text(text, encoding="utf-8")
    assert run(arguments) == 0


def test_vcs_policy_repair_updates_both_declaring_files(script_source: Path) -> None:
    """Apply the suggested edits and prove the two policy layers still agree."""
    policy = (
        'policy="block"\nallowed-schemes=["git+https"]\n'
        'allowed-repos=["https://example.org/demo.git"]\n'
    )
    script_source.write_text(
        script_source.read_text(encoding="utf-8").replace(
            "# ///\n",
            "# [tool.nab.vcs]\n"
            + "".join("# " + line + "\n" for line in policy.splitlines())
            + '# [[tool.nab.vcs-sources]]\n# name="demo"\n'
            f'# url="git+https://example.org/demo.git@{"a" * 40}"\n# ///\n',
        ),
        encoding="utf-8",
    )
    directory_config = script_source.parent / "nab.toml"
    directory_config.write_text("[vcs]\n" + policy)
    with pytest.raises(ConfigError) as error:
        read_pyproject_config(script_source, input_format=InputFormat.SCRIPT)
    repairs = re.findall(
        r'\[(tool\.nab\.vcs|vcs)\]\.policy = "allow"', str(error.value)
    )
    files = {"tool.nab.vcs": script_source, "vcs": directory_config}
    for table in repairs:
        target = files[table]
        target.write_text(
            target.read_text().replace('policy="block"', 'policy="allow"')
        )

    config = read_pyproject_config(script_source, input_format=InputFormat.SCRIPT)
    assert config.vcs.policy.value == "allow"


def test_environment_repair_updates_both_declaring_files(script_source: Path) -> None:
    """Use the named table and file locations to add the missing platform."""
    script_source.write_text(
        script_source.read_text(encoding="utf-8").replace(
            "# ///\n", '# [tool.nab.environment]\n# implementation="pypy"\n# ///\n'
        ),
        encoding="utf-8",
    )
    (script_source.parent / "nab.toml").write_text(
        '[environment]\nimplementation="pypy"\n'
    )
    with pytest.raises(ConfigError) as error:
        read_pyproject_config(script_source, input_format=InputFormat.SCRIPT)
    repairs = re.findall(
        r"\[(tool\.nab\.environment|environment)\] in (.*?\.(?:py|toml))(?= and |\.$)",
        str(error.value),
    )
    for table, filename in repairs:
        target = Path(filename)
        prefix = "# " if target == script_source else ""
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                f"{prefix}[{table}]\n",
                f'{prefix}[{table}]\n{prefix}platform="linux_x86_64"\n',
            ),
            encoding="utf-8",
        )

    config = read_pyproject_config(script_source, input_format=InputFormat.SCRIPT)
    assert config.environment is not None
    assert config.environment.implementation == "pypy"
    assert config.environment.platform is not None
