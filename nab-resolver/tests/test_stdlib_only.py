"""Hold nab-resolver to the standard library at run time."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import nab_resolver

if TYPE_CHECKING:
    from collections.abc import Iterator

SOURCE_ROOT = Path(nab_resolver.__file__).parent


def _guards_type_checking(test: ast.expr) -> bool:
    """Return whether ``test`` is the TYPE_CHECKING flag, however spelled."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _runtime_imports(node: ast.AST) -> Iterator[str]:
    """Yield the top-level module of every import under ``node`` that executes.

    A ``TYPE_CHECKING`` block is what a checker reads and never runs, so only
    the fallback arm of one is walked. Relative imports stay inside the package
    and are not reported.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name.partition(".")[0]
        return

    if isinstance(node, ast.ImportFrom):
        if node.level == 0 and node.module is not None:
            yield node.module.partition(".")[0]
        return

    if isinstance(node, ast.If) and _guards_type_checking(node.test):
        for fallback in node.orelse:
            yield from _runtime_imports(fallback)
        return

    for child in ast.iter_child_nodes(node):
        yield from _runtime_imports(child)


def test_every_shipped_module_imports_only_the_standard_library() -> None:
    modules = sorted(SOURCE_ROOT.rglob("*.py"))
    assert modules

    outside = {
        (path.name, imported)
        for path in modules
        for imported in _runtime_imports(ast.parse(path.read_text(encoding="utf-8")))
        if imported != "nab_resolver" and imported not in sys.stdlib_module_names
    }
    assert outside == set()
