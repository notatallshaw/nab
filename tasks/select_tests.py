"""Select CI workspaces from the packages each nox workspace installs."""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_FIELDS = 3
PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14", "3.15")


def _documentation(path: str) -> bool:
    return path.startswith("docs/") or path.endswith(".md")


def _benchmark(path: str) -> bool:
    return path.startswith(
        (
            "nab-resolver/benchmarks/",
            "nab-project/benchmarks/",
            "nab-project/tests/benchmark/",
        )
    ) or path in {
        "nab-resolver/tests/test_range_benchmark.py",
        "nab-project/tests/test_deterministic_smoke_core.py",
    }


def _test_matrix(paths: list[str] | None, *, quick: bool) -> list[dict[str, str]]:
    oldest, newest = PYTHON_VERSIONS[0], PYTHON_VERSIONS[-1]
    if quick:
        return [{"os": "ubuntu", "python": newest}]

    versions = [*PYTHON_VERSIONS, newest + "t"]
    patches = ["3.10.4"]
    if not paths:
        # Main and manual runs also cover the older free-threaded and patch releases.
        versions.append("3.14t")
        patches.extend(("3.11.0", "3.12.0", "3.13.0", "3.14.0"))
    return [
        *({"os": "ubuntu", "python": version} for version in versions),
        *(
            {"os": "ubuntu", "python": version, "runner": "ubuntu-24.04"}
            for version in patches
        ),
        *(
            {"os": os_name, "python": oldest, "second": newest}
            for os_name in ("macos", "windows")
        ),
    ]


def _benchmark_matrix(paths: list[str] | None) -> list[dict[str, str | bool]]:
    newest = PYTHON_VERSIONS[-1]
    runners: list[dict[str, str | bool]] = [
        {"os": "ubuntu", "python": newest, "reports": True}
    ]
    if not paths or any(
        _benchmark(path)
        or path in {"noxfile.py", "pyproject.toml", "pylock.dev.toml"}
        or path.startswith((".github/", "tasks/"))
        for path in paths
    ):
        runners.extend(
            (
                {"os": "ubuntu", "python": PYTHON_VERSIONS[0]},
                {"os": "windows", "python": newest},
            )
        )
    return runners


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
        # Test modules run in the sessions that collect them. Shared fixtures
        # and package code still select every downstream consumer.
        if Path(path).name.startswith("test_") and path.endswith(".py"):
            owners = {
                name
                for name, (_, suites, _) in workspaces.items()
                if any(path.startswith(f"{suite}/") for suite in suites)
            }
            if owners:
                selected.update(owners)
                continue
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


def auxiliary_outputs(paths: list[str] | None) -> dict[str, str]:
    """Select auxiliary jobs only when every changed path is understood."""
    jobs = {"benchmarks", "dists", "standalone"}
    selected: set[str] = set()
    if not paths:
        selected = jobs
    for path in paths or []:
        package = path.split("/", 1)[0]
        if _benchmark(path):
            selected.add("benchmarks")
        elif _documentation(path):
            if Path(path).name == "README.md":
                selected.add("dists")
        elif package in {
            "nab-resolver",
            "nab-markersets",
            "nab-provider",
            "nab-index",
            "nab-project",
            "src",
            "tests",
        }:
            if not (Path(path).name.startswith("test_") and path.endswith(".py")):
                selected.update({"benchmarks", "dists"})
            if package == "nab-markersets":
                selected.add("standalone")
        elif path in {"README.md", "LICENSE", "LICENSE.txt"} or path in {
            "tasks/build_dists.py",
            "tasks/check_dists.py",
            "tasks/check_installed.py",
            "tasks/verify_published.py",
        }:
            selected.add("dists")
        else:
            selected = jobs
            break
    return {job: str(job in selected).lower() for job in sorted(jobs)}


def job_outputs(
    selected: list[str],
    workspaces: dict[str, tuple[list[str], list[str], list[str]]],
    paths: list[str] | None,
) -> dict[str, str]:
    """Permit only the skips justified by the same job-selection outputs."""
    auxiliary = auxiliary_outputs(paths)
    outputs = {**selection_outputs(selected, workspaces), **auxiliary}
    quick = bool(paths) and all(
        _documentation(path) or _benchmark(path) for path in paths or []
    )
    outputs["test-matrix"] = json.dumps({"include": _test_matrix(paths, quick=quick)})
    outputs["benchmark-matrix"] = json.dumps({"include": _benchmark_matrix(paths)})
    outputs["properties"] = str(not quick).lower()
    if quick:
        outputs["resolver"] = "false"
    conditional = {
        "resolver": "crosshair-tests",
        "properties": "property-tests",
        **{job: job for job in auxiliary},
    }
    outputs["allowed-skips"] = json.dumps(
        [job for flag, job in conditional.items() if outputs[flag] == "false"]
    )
    return outputs


def main() -> None:
    """Write selection outputs; a failed Git comparison runs every workspace."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspaces = workspace_inputs(REPO_ROOT)
    selected = list(workspaces)
    paths = None
    if args.base:
        try:
            paths = changed_paths(REPO_ROOT, args.base, args.head)
            selected = select_workspaces(paths, workspaces)
        except subprocess.CalledProcessError:
            print("Git comparison failed; selecting every workspace.")

    outputs = job_outputs(selected, workspaces, paths)
    with args.output.open("a", encoding="utf-8") as output:
        for name, value in outputs.items():
            print(f"{name}={value}")
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
