"""Hold this suite to the files the umbrella sdist carries.

The sdist ships the CLI package, this suite and the root conftest, so a test
that reads ``docs/``, ``tasks/`` or a sibling workspace fails when the suite is
run from the unpacked source. Such a test goes on the sdist's exclude list
instead.

The check reads paths built from ``__file__`` by ``.parent``, ``.parents[n]``
and ``/`` joins. A module that reaches a file some other way is not covered.
"""

from __future__ import annotations

import ast
import ntpath
import os
import sys
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest
import tomli

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent
_MANIFEST = tomli.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
_SDIST = _MANIFEST["tool"]["hatch"]["build"]["targets"]["sdist"]

# Stands in for a path component that is not a string literal.
_UNKNOWN = "*"

# hatchling ships these whatever only-include says.
_ALWAYS_SHIPPED = frozenset(
    {"pyproject.toml", "hatch.toml", _MANIFEST["project"]["readme"]}
    | set(_MANIFEST["project"]["license-files"])
)


def _suite_modules() -> list[Path]:
    """The Python files that make up the suite, shipped and excluded alike."""
    return [_ROOT / "conftest.py", *sorted(_TESTS.glob("*.py"))]


def _as_posix(relative: str, flavour: type[PurePath]) -> str:
    """``relative``, read in ``flavour``'s separators, returned posix."""
    return flavour(relative).as_posix()


def _relative(path: Path) -> str:
    """``path`` as the sdist manifest spells it: repo-root relative and posix."""
    return _as_posix(os.path.relpath(path, _ROOT), PurePath)


class _ModulePaths:
    """Reads the paths one module builds from its own ``__file__``."""

    def __init__(self, module: Path) -> None:
        self._module = module
        self._anchors: dict[str, Path] = {}

    def read(self) -> set[str]:
        """The module's ``__file__``-anchored paths, repo-root relative.

        Only the outermost path of each join is returned, and a component that
        is not a string literal becomes ``_UNKNOWN``.
        """
        tree = ast.parse(self._module.read_text(encoding="utf-8"))
        self._bind_anchors(tree)

        joins = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        ]
        nested = {id(node.left) for node in joins}

        return {
            _relative(path)
            for node in joins
            if id(node) not in nested and (path := self._resolve(node)) is not None
        }

    def _bind_anchors(self, tree: ast.Module) -> None:
        """Record the module-level names bound to a path."""
        for statement in tree.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            bound = self._resolve(statement.value)
            if isinstance(target, ast.Name) and bound is not None:
                self._anchors[target.id] = bound

    def _resolve(self, node: ast.expr) -> Path | None:
        """The path ``node`` builds, or ``None`` if it is not one of the forms read."""
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in {"resolve", "absolute"}:
                return self._resolve(func.value)
            args = node.args
            if len(args) == 1 and isinstance(args[0], ast.Name):
                return self._module if args[0].id == "__file__" else None
            return None

        if isinstance(node, ast.Attribute) and node.attr == "parent":
            base = self._resolve(node.value)
            return None if base is None else base.parent

        if isinstance(node, ast.Subscript):
            return self._walk_up(node)

        if isinstance(node, ast.Name):
            return self._anchors.get(node.id)

        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base = self._resolve(node.left)
            if base is None:
                return None
            part = node.right
            literal = isinstance(part, ast.Constant) and isinstance(part.value, str)
            return base / (part.value if literal else _UNKNOWN)

        return None

    def _walk_up(self, node: ast.Subscript) -> Path | None:
        """The directory ``<base>.parents[n]`` names, for a literal ``n``."""
        value = node.value
        index = node.slice
        if not (
            isinstance(value, ast.Attribute)
            and value.attr == "parents"
            and isinstance(index, ast.Constant)
            and isinstance(index.value, int)
        ):
            return None

        base = self._resolve(value.value)
        if base is None:
            return None

        # parents[0] is the first parent, so the walk is one step longer than n.
        for _ in range(index.value + 1):
            base = base.parent
        return base


def _is_shipped(relative: str) -> bool:
    """Whether the sdist carries ``relative``, a repo-root-relative path."""
    if relative in _ALWAYS_SHIPPED:
        return True

    parts = PurePosixPath(relative).parts
    if _UNKNOWN in parts or ".." in parts:
        return False

    for entry in _SDIST["only-include"]:
        shipped = PurePosixPath(entry).parts
        if parts[: len(shipped)] == shipped or shipped[: len(parts)] == parts:
            return True

    return False


def test_modules_reaching_outside_are_exactly_the_excluded_ones() -> None:
    """A module reads outside the sdist if and only if the sdist leaves it out.

    Run from the unpacked sdist this sees the shipped modules alone: none of
    them reaches outside, and none is excluded.
    """
    modules = _suite_modules()

    reaching = {
        _relative(module): outside
        for module in modules
        if (
            outside := sorted(
                p for p in _ModulePaths(module).read() if not _is_shipped(p)
            )
        )
    }
    excluded = set(_SDIST["exclude"]) & {_relative(module) for module in modules}

    assert set(reaching) == excluded, (
        f"reads files the sdist does not carry: {reaching}; "
        f"left out of the sdist: {sorted(excluded)}"
    )


def test_no_module_is_placed_by_a_path_the_check_cannot_read() -> None:
    """A computed path component is a blind spot, so no module may rest on one.

    A module that also names a readable outside path is placed by that path. One
    with none would be placed on a guess.
    """
    guessed: dict[str, list[str]] = {}
    for module in _suite_modules():
        paths = _ModulePaths(module).read()
        computed = sorted(path for path in paths if _UNKNOWN in path)
        read = [
            path for path in paths if _UNKNOWN not in path and not _is_shipped(path)
        ]
        if computed and not read:
            guessed[_relative(module)] = computed

    assert not guessed, f"placed by path components the check cannot read: {guessed}"


def test_a_windows_relative_path_reads_as_a_manifest_path() -> None:
    """CI runs the suite on Windows, where ``os.path.relpath`` gives backslashes.

    ``ntpath`` and ``PureWindowsPath`` are what Windows itself runs and import
    anywhere, so this covers the separator from any platform.
    """
    root = r"C:\repo"

    inside = ntpath.relpath(ntpath.join(root, "tests", "test_cli.py"), root)
    outside = ntpath.relpath(ntpath.join(root, "..", "docs", "index.md"), root)

    assert inside == r"tests\test_cli.py"
    assert outside == r"..\docs\index.md"

    shipped = _as_posix(inside, PureWindowsPath)
    reaching = _as_posix(outside, PureWindowsPath)

    assert shipped == "tests/test_cli.py"
    assert reaching == "../docs/index.md"

    assert _is_shipped(shipped)
    assert not _is_shipped(reaching)


def test_relative_reads_the_platform_it_runs_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_relative`` takes its flavour from the platform, not from posix.

    On Windows, ``os.path`` is ``ntpath`` and ``PurePath`` is ``PureWindowsPath``.
    """
    here = sys.modules[__name__]
    monkeypatch.setattr(here, "os", SimpleNamespace(path=ntpath))
    monkeypatch.setattr(here, "PurePath", PureWindowsPath)
    monkeypatch.setattr(here, "_ROOT", ntpath.join("C:", "repo"))

    inside = ntpath.join("C:", "repo", "tests", "test_cli.py")

    assert _relative(inside) == "tests/test_cli.py"
