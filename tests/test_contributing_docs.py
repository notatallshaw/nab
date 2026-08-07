"""Check the contributing page's commands against this check-out.

A contributor pastes these into a shell, so a documented pytest flag has to be
one the installed pytest accepts, and the documented coverage recipe has to run
the steps CI's gate runs.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

CONTRIBUTING = REPO_ROOT / "docs" / "contributing.md"
NOXFILE = REPO_ROOT / "noxfile.py"

_FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
_INLINE = re.compile(r"`([^`\n]+)`")


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
