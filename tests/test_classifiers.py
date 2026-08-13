"""Check the published Python classifiers against the tested CI matrix.

PyPI metadata is immutable per release, so a stale classifier list cannot be
corrected without cutting another one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import tomli

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every distribution except the umbrella, which builds from the repo root.
WORKSPACE_PACKAGES = ("nab-resolver", "nab-provider", "nab-project", "nab-index")

PYPROJECT_PATHS = (
    REPO_ROOT / "pyproject.toml",
    *(REPO_ROOT / name / "pyproject.toml" for name in WORKSPACE_PACKAGES),
)

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"

PREFIX = "Programming Language :: Python :: "

# Only the test matrix gives python-version a list; the standalone jobs pin a
# scalar. A YAML flow sequence of quoted strings is also valid JSON.
_MATRIX = re.compile(r"^\s*python-version:\s*(\[[^\]]*\])\s*$", re.MULTILINE)

# A matrix entry may carry an implementation or build tag (pypy3.11, 3.14t);
# only the language version reaches a classifier.
_LANGUAGE_VERSION = re.compile(r"\d+\.\d+")


def _tested_versions() -> set[str]:
    lists = _MATRIX.findall(WORKFLOW.read_text(encoding="utf-8"))
    assert len(lists) == 1, f"expected one python-version list, found {lists}"
    versions = set()
    for entry in json.loads(lists[0]):
        found = _LANGUAGE_VERSION.search(entry)
        assert found is not None, f"no Python version in matrix entry {entry!r}"
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


def test_classifiers_cover_every_tested_python_version() -> None:
    expected = {PREFIX + version for version in {"3", *_tested_versions()}}
    for path in PYPROJECT_PATHS:
        assert _version_classifiers(path) == expected, path
