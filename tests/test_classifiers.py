"""Check the published Python classifiers against the tested CI matrix.

PyPI metadata is immutable per release, so a stale classifier list cannot be
corrected without cutting another one.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import tomli

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every distribution except the umbrella, which builds from the repo root.
WORKSPACE_PACKAGES = (
    "nab-resolver",
    "nab-markersets",
    "nab-provider",
    "nab-project",
    "nab-index",
)

PYPROJECT_PATHS = (
    REPO_ROOT / "pyproject.toml",
    *(REPO_ROOT / name / "pyproject.toml" for name in WORKSPACE_PACKAGES),
)

SELECTOR = REPO_ROOT / "tasks" / "select_tests.py"
PREFIX = "Programming Language :: Python :: "

# A matrix entry may carry an implementation or build tag (pypy3.11, 3.14t);
# only the language version reaches a classifier.
_LANGUAGE_VERSION = re.compile(r"\d+\.\d+")


def _tested_versions(output: Path) -> set[str]:
    subprocess.run(  # noqa: S603 - execute the repository selector with a temporary output path
        [sys.executable, str(SELECTOR), "--output", str(output)],
        cwd=REPO_ROOT,
        check=True,
    )
    outputs = dict(
        line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines()
    )
    versions = set()
    for job in json.loads(outputs["test-matrix"])["include"]:
        for key in ("python", "second"):
            if key in job:
                found = _LANGUAGE_VERSION.search(job[key])
                assert found is not None
                versions.add(found.group())
    return versions


def _claims_version(classifier: str) -> bool:
    if not classifier.startswith(PREFIX):
        return False
    # Siblings such as "3 :: Only" and "Implementation :: CPython" share the
    # prefix but name no version.
    return all(part.isdigit() for part in classifier[len(PREFIX) :].split("."))


def _version_classifiers(path: Path) -> set[str]:
    project = tomli.loads(path.read_text(encoding="utf-8"))["project"]
    return {entry for entry in project["classifiers"] if _claims_version(entry)}


def test_classifiers_cover_every_tested_python_version(tmp_path: Path) -> None:
    expected = {
        PREFIX + version for version in {"3", *_tested_versions(tmp_path / "selection")}
    }
    for path in PYPROJECT_PATHS:
        assert _version_classifiers(path) == expected, path
