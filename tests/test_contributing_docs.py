"""Check the contributing page's commands against this check-out.

A contributor pastes these into a shell, so a documented pytest flag has to be
one the installed pytest accepts, and the documented coverage recipe has to run
the steps CI's gate runs. A tool the page runs out of the development
environment has to be one that environment carries.
"""

from __future__ import annotations

import ast
import itertools
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import tomli

REPO_ROOT = Path(__file__).resolve().parents[1]

CONTRIBUTING = REPO_ROOT / "docs" / "contributing.md"
NOXFILE = REPO_ROOT / "noxfile.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
HATCH_TOML = REPO_ROOT / "hatch.toml"

_FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
_INLINE = re.compile(r"`([^`\n]+)`")

_VENV = ".venv"
_VENV_BIN = f"{_VENV}/bin/"

# Hatch expands its `{root}` field to the project directory before it runs a command.
_ROOT_PREFIX = "{root}/"

# A dependency specifier down to its bare name.
_REQUIREMENT_NAME = re.compile(r"^[^\[=<>!~;\s]+")


def _documented_commands() -> list[str]:
    """Every command the page prints, fenced or inline."""
    text = CONTRIBUTING.read_text(encoding="utf-8")
    commands = [
        line for block in _FENCE.findall(text) for line in block.splitlines()[1:-1]
    ]
    commands.extend(_INLINE.findall(_FENCE.sub("", text)))
    return commands


def _invocation(command: str) -> tuple[str, tuple[str, ...]] | None:
    """Split a command into the tool it runs and that tool's arguments."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return None

    if tokens[1:2] == ["-m"] and PurePosixPath(tokens[0]).name.startswith("python"):
        tokens = tokens[2:]
    if not tokens:
        return None

    return PurePosixPath(tokens[0]).name, tuple(tokens[1:])


def _documented_pytest_arguments() -> set[tuple[str, ...]]:
    """Argument lists the page passes to pytest."""
    found = set()
    for command in _documented_commands():
        invocation = _invocation(command)
        if invocation is None:
            continue
        tool, args = invocation
        if tool == "pytest":
            found.add(args)
        elif tool == "coverage" and args[:3] == ("run", "-m", "pytest"):
            found.add(args[3:])
    return found


def _documented_coverage_steps() -> set[str]:
    """Coverage subcommands the page documents."""
    steps = set()
    for command in _documented_commands():
        invocation = _invocation(command)
        if invocation is not None and invocation[0] == "coverage" and invocation[1]:
            steps.add(invocation[1][0])
    return steps


def _ci_coverage_steps() -> set[str]:
    """Coverage subcommands the nox tests session runs."""
    tree = ast.parse(NOXFILE.read_text(encoding="utf-8"))
    session = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "tests"
    )

    steps = set()
    for call in ast.walk(session):
        if not isinstance(call, ast.Call):
            continue
        literals = [
            arg.value
            for arg in call.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        if literals[:1] == ["coverage"] and len(literals) > 1:
            steps.add(literals[1])
    return steps


def _canonical(name: str) -> str:
    """A distribution, module or script name in one spelling."""
    return name.lower().replace("_", "-")


def _group_names(group: str, groups: dict[str, list[object]]) -> set[str]:
    """The distributions one dependency-group installs, through its includes."""
    names = set()
    for entry in groups[group]:
        if isinstance(entry, dict):
            names |= _group_names(str(entry["include-group"]), groups)
        else:
            match = _REQUIREMENT_NAME.match(str(entry))
            assert match is not None
            names.add(_canonical(match.group()))
    return names


def _project_names(directory: Path) -> set[str]:
    """The distribution name and console scripts one project directory ships."""
    project = tomli.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    return {_canonical(project["name"])} | {
        _canonical(script) for script in project.get("scripts", {})
    }


def _hatch_envs() -> dict[str, Any]:
    """The environments hatch.toml declares."""
    return tomli.loads(HATCH_TOML.read_text(encoding="utf-8"))["envs"]


def _default_env() -> dict[str, Any]:
    """The environment docs/contributing.md runs its tools out of."""
    envs = _hatch_envs()
    assert "default" in envs, "hatch.toml declares no default environment"
    return envs["default"]


def _default_workspace_members() -> list[str]:
    """The member directories the default environment installs editable."""
    members = _default_env().get("workspace", {}).get("members")
    assert members, "hatch.toml's default environment names no workspace members"
    return [str(member) for member in members]


def _default_dependency_groups() -> list[str]:
    """The dependency-groups the default environment syncs."""
    groups = _default_env().get("dependency-groups")
    assert groups, "hatch.toml's default environment names no dependency-groups"
    return [str(group) for group in groups]


def _installed_names() -> set[str]:
    """The distributions and scripts hatch's default environment names.

    Transitive dependencies are left out: the page runs a tool the environment
    asks for, not one pulled in behind another.
    """
    groups = tomli.loads(PYPROJECT.read_text(encoding="utf-8"))["dependency-groups"]

    # The umbrella installs from the repo root, its members from subdirectories.
    installed = _project_names(REPO_ROOT)
    for member in _default_workspace_members():
        installed |= _project_names(REPO_ROOT / member)
    for group in _default_dependency_groups():
        installed |= _group_names(group, groups)
    return installed


def _pre_install_editables(env: dict[str, Any]) -> set[str]:
    """The paths an environment installs editable ahead of hatch's own install."""
    paths = set()
    for command in env.get("pre-install-commands", []):
        args = shlex.split(str(command))
        paths.update(value for flag, value in itertools.pairwise(args) if flag == "-e")
    return paths


def _venv_tools() -> set[str]:
    """The tools the page runs out of the development environment."""
    tools = set()
    for command in _documented_commands():
        if not command.startswith(_VENV_BIN):
            continue
        invocation = _invocation(command)
        if invocation is not None:
            tools.add(_canonical(invocation[0]))
    return tools


def _documented_hatch_scripts() -> set[tuple[str, str]]:
    """The `hatch run <env>:<script>` targets the page prints."""
    targets = set()
    for command in _documented_commands():
        invocation = _invocation(command)
        if invocation is None:
            continue
        tool, args = invocation
        if tool != "hatch" or args[:1] != ("run",) or len(args) < 2:
            continue
        env, _, script = args[1].partition(":")
        if script:
            targets.add((env, script))
    return targets


def test_documented_pytest_arguments_are_accepted() -> None:
    """Each documented pytest command must parse and name paths that exist."""
    documented = _documented_pytest_arguments()
    assert documented, "contributing.md documents no pytest command"

    # The probe sees only the flags the page prints, and writes no coverage data.
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("COVERAGE_PROCESS_START", None)

    for args in sorted(documented):
        result = subprocess.run(  # noqa: S603 - arguments come from the page
            [
                sys.executable,
                "-m",
                "pytest",
                *args,
                "--collect-only",
                "-q",
                # Parse the arguments and resolve their paths, collect no test.
                "--ignore-glob=*",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            check=False,
        )
        assert result.returncode != pytest.ExitCode.USAGE_ERROR, (
            f"contributing.md documents `pytest {shlex.join(args)}`, which the "
            f"installed pytest will not run:\n{result.stdout}{result.stderr}"
        )


def test_documented_coverage_recipe_matches_ci() -> None:
    """The documented coverage recipe must run every step CI runs."""
    missing = _ci_coverage_steps() - _documented_coverage_steps()
    assert not missing, (
        f"contributing.md omits the coverage step(s) CI runs: {sorted(missing)}"
    )


def test_development_environment_lives_where_the_page_runs_it() -> None:
    """The page runs tools by path, so hatch's default environment has to be `.venv`."""
    assert _hatch_envs()["default"].get("path") == _VENV, (
        f"contributing.md runs tools out of {_VENV_BIN}, which is not where "
        f"hatch.toml puts the default environment"
    )


def test_only_the_default_environment_is_the_check_out_venv() -> None:
    """Hatch environments inherit `path`, so every one but the default opts out."""
    sharing = sorted(
        name
        for name, env in _hatch_envs().items()
        if name != "default" and not env.get("detached") and env.get("template") != name
    )
    assert not sharing, (
        f"hatch environments {sharing} inherit the default environment's path, "
        f"so they would share {_VENV} with it"
    )


def test_the_docs_environment_carries_no_project() -> None:
    """CI and Read the Docs build the docs from pylock.docs.toml and nothing else.

    Hatch installs `workspace.members` editable even under `skip-install`, so an
    environment that names members carries more than that lock does.
    """
    envs = _hatch_envs()
    assert "docs" in envs, "hatch.toml declares no docs environment"

    docs = envs["docs"]
    assert docs.get("skip-install") is True, (
        "the docs environment installs the project, which the docs lock does not"
    )
    assert "workspace" not in docs, (
        "the docs environment names workspace members, which hatch installs "
        "editable even under skip-install"
    )


def test_workspace_members_go_in_before_the_umbrella() -> None:
    """The default environment installs its members ahead of hatch's own install.

    The umbrella pins them to the unreleased version under development, so
    hatch's install resolves only once they are already there.
    """
    editables = _pre_install_editables(_default_env())
    installed = {path.removeprefix(_ROOT_PREFIX) for path in editables}
    assert installed == set(_default_workspace_members())


def test_pre_install_paths_are_anchored_at_the_project_root() -> None:
    """Hatch runs a pre-install command wherever it was invoked, not at the root."""
    relative = sorted(
        path
        for path in _pre_install_editables(_default_env())
        if not path.startswith(_ROOT_PREFIX)
    )
    assert not relative, (
        f"hatch.toml installs {relative} relative to the invocation directory, "
        f"so `hatch shell` fails from any subdirectory of the check-out"
    )


def test_documented_venv_tools_are_installed() -> None:
    """The development environment must carry every tool the page runs from it."""
    installed = _installed_names()
    assert installed, "hatch's default environment carries nothing"

    tools = _venv_tools()
    assert tools, f"contributing.md runs nothing out of {_VENV_BIN}"

    missing = sorted(tools - installed)
    assert not missing, (
        f"contributing.md runs {missing} out of {_VENV_BIN}, which hatch's "
        f"default environment does not carry"
    )


def test_documented_hatch_scripts_exist() -> None:
    """Each `hatch run <env>:<script>` the page prints names a script hatch.toml has."""
    envs = _hatch_envs()
    documented = _documented_hatch_scripts()
    assert documented, "contributing.md documents no hatch script"

    missing = sorted(
        f"{env}:{script}"
        for env, script in documented
        if script not in envs.get(env, {}).get("scripts", {})
    )
    assert not missing, f"contributing.md runs undeclared hatch scripts: {missing}"
