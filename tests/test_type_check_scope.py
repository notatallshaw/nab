"""Check that all five type-checkers are aimed at the same source trees.

``noxfile.py`` passes ``TYPED_TREES`` on the command line for mypy, ty,
pyrefly and zuban, while pyright reads only ``[tool.pyright] include``. A
tree added in one place and forgotten in the other narrows a checker back
down without failing anything, which is the drift this catches.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import tomli

REPO_ROOT = Path(__file__).resolve().parents[1]
NOXFILE = REPO_ROOT / "noxfile.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _typed_trees() -> set[str]:
    """The tree list noxfile.py hands to the command-line checkers."""
    module = ast.parse(NOXFILE.read_text(encoding="utf-8"))
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == "TYPED_TREES" for t in node.targets):
            return set(ast.literal_eval(node.value))
    raise AssertionError("noxfile.py defines no TYPED_TREES")


def _tool_config(tool: str) -> dict[str, Any]:
    """One ``[tool.<name>]`` table from the root pyproject."""
    return tomli.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"][tool]


def test_pyright_include_covers_every_typed_tree() -> None:
    """pyright is run with no path argument, so include is its whole scope."""
    assert _typed_trees() <= set(_tool_config("pyright")["include"])


def test_pyrefly_project_includes_covers_every_typed_tree() -> None:
    """A bare ``pyrefly check`` takes its scope from here, not from noxfile.py."""
    assert _typed_trees() <= set(_tool_config("pyrefly")["project-includes"])


def test_mypy_path_covers_every_typed_tree() -> None:
    """Each tree is a package base; mypy and zuban both read this setting."""
    assert _typed_trees() <= set(_tool_config("mypy")["mypy_path"])
