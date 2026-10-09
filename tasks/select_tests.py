"""Select CI workspaces from the packages each nox workspace installs."""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_FIELDS = 3


def workspace_inputs(root: Path) -> dict[str, tuple[list[str], list[str], list[str]]]:
    """Read nox's install closures without importing nox or installing packages."""
    module = ast.parse((root / "noxfile.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "WORKSPACES"
            for target in node.targets
        ):
            return _workspace_mapping(ast.literal_eval(node.value))
    msg = "noxfile.py defines no WORKSPACES"
    raise ValueError(msg)


def _string_list(value: object) -> list[str]:
    """Validate one list read from the nox workspace table."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = "workspace inputs must be lists of strings"
        raise ValueError(msg)
    return [item for item in value if isinstance(item, str)]


def _workspace_mapping(
    value: object,
) -> dict[str, tuple[list[str], list[str], list[str]]]:
    """Validate the literal table before passing its arguments to CI."""
    if not isinstance(value, dict) or not value:
        msg = "WORKSPACES must be a nonempty mapping"
        raise ValueError(msg)
    workspaces: dict[str, tuple[list[str], list[str], list[str]]] = {}
    for name, inputs in value.items():
        if (
            not isinstance(name, str)
            or not isinstance(inputs, tuple)
            or len(inputs) != WORKSPACE_FIELDS
        ):
            msg = "each workspace must name install, test and coverage lists"
            raise ValueError(msg)
        packages, paths, coverage = inputs
        workspaces[name] = (
            _string_list(packages),
            _string_list(paths),
            _string_list(coverage),
        )
    return workspaces


def select_workspaces(
    paths: Iterable[str],
    workspaces: dict[str, tuple[list[str], list[str], list[str]]],
) -> list[str]:
    """Select consumers of changed packages; unclassified paths select all."""
    selected: set[str] = set()
    members = {
        package for packages, _, _ in workspaces.values() for package in packages
    }
    members.discard(".")
    for path in paths:
        package = path.split("/", 1)[0]
        if package in members:
            selected.update(
                workspace
                for workspace, (packages, _, _) in workspaces.items()
                if package in packages
            )
        elif package in {"src", "docs"}:
            # Project tests scan CLI source and execute the tutorial example.
            selected.update(
                workspace
                for workspace, (packages, _, _) in workspaces.items()
                if "nab-project" in packages or "." in packages
            )
        elif package == "tests" or path == "README.md":
            selected.update(
                workspace
                for workspace, (packages, _, _) in workspaces.items()
                if "." in packages
            )
        else:
            return list(workspaces)
    # An empty comparison must not turn a nox invocation into an accidental full run.
    return [workspace for workspace in workspaces if workspace in selected] or list(
        workspaces
    )


def changed_paths(root: Path, base: str, head: str) -> list[str]:
    """Compare the PR head to its merge base, preserving deleted and renamed paths."""
    merge_base = subprocess.check_output(
        ["git", "merge-base", base, head], cwd=root, text=True
    ).strip()
    diff = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", "-z", merge_base, head],
        cwd=root,
    )
    return [os.fsdecode(path) for path in diff.split(b"\0") if path]


def selection_outputs(
    selected: list[str],
    workspaces: dict[str, tuple[list[str], list[str], list[str]]],
) -> dict[str, str]:
    """Build arguments for nox and pytest, and the resolver-only job condition."""
    paths = dict.fromkeys(
        path for workspace in selected for path in workspaces[workspace][1]
    )
    return {
        "workspaces": " ".join(selected),
        "test-paths": " ".join(paths),
        "resolver": str("resolver" in selected).lower(),
    }


def main() -> None:
    """Write selection outputs; a failed Git comparison runs every workspace."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspaces = workspace_inputs(REPO_ROOT)
    selected = list(workspaces)
    if args.base:
        try:
            selected = select_workspaces(
                changed_paths(REPO_ROOT, args.base, args.head), workspaces
            )
        except subprocess.CalledProcessError:
            print("Git comparison failed; selecting every workspace.")

    outputs = selection_outputs(selected, workspaces)
    with args.output.open("a", encoding="utf-8") as output:
        for name, value in outputs.items():
            print(f"{name}={value}")
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
