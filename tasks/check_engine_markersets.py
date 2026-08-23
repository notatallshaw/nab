"""Check that the resolve engine does not depend on packaging's marker sets.

``packaging.markersets`` and ``packaging._markersets`` are 2,200 lines that
released packaging does not have, so a host vendoring nab's engine carries
them only if the engine reaches them.

Two rules, both computed statically over the shipped ``src`` trees.

``use``
    No definition in either marker-set module may be reachable through the
    use graph from the engine entry point.

``import``
    Importing a module runs its module-level imports, so a module in the
    engine's import closure that imports marker sets puts them in a host's
    vendored tree even when nothing calls them. Every such module must be on
    ``EXEMPT``, and an exemption that no longer fires is an error too.

Every ``Name`` load inside a definition counts as a possible global
reference, so a ``use`` result of zero is sound.

Run directly::

    python tasks/check_engine_markersets.py [-v]
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TREES = {
    "nab_project": "nab-project/src/nab_project",
    "nab_resolver": "nab-resolver/src/nab_resolver",
    "nab_provider": "nab-provider/src/nab_provider",
    "nab_index": "nab-index/src/nab_index",
    "nab": "src/nab",
}

MARKER_SET_MODULES = frozenset(
    {
        "nab_provider._vendor.packaging.markersets",
        "nab_provider._vendor.packaging._markersets",
    }
)

# The engine entry point: what the walk reaches from here is what a host
# vendors.
ENGINE_MODULE = "nab_project._resolve.engine"
ENGINE_ENTRY = "_resolve_with_micro_narrowing"

# Empty: every definition in the engine module is walked.
ENGINE_GROUP = frozenset()

# Modules in the engine's import closure that import marker sets, each with
# why it is on the path. No other module may.
EXEMPT = {
    "nab_provider.target": (
        "variable_names serves marker_variables, which only the lock writer "
        "calls. The engine imports target for the micro-boundary helpers, so "
        "the module stays on the path either way."
    ),
    "nab_project._lockfile.disjointness": (
        "Reached from the engine only through build_target_lock."
    ),
    "nab_project._lockfile.pylock": ("Same edge as _lockfile.disjointness."),
    "nab_project._lockfile.coverage": ("Same edge as _lockfile.disjointness."),
    "nab_provider.marker_holds": (
        "Where the marker-set dependency lives so the engine does not import "
        "it. Reached here through target, requirements_file, "
        "_provider.metadata_resolver and _lockfile.validate."
    ),
    "nab_provider._vendor.packaging.markersets": "The marker-set module itself.",
}


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _name_loads(node: ast.AST) -> set[str]:
    return {
        sub.id
        for sub in ast.walk(node)
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
    }


def _assigned(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        return [
            n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)
        ]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


class Module:
    """One shipped module: its definitions, bindings and top-level imports."""

    def __init__(self, name: str, path: Path, *, is_package: bool) -> None:
        """Parse ``path`` and record what it defines, binds and imports."""
        self.name = name
        self.path = path
        self.is_package = is_package
        self.defs: dict[str, ast.AST] = {}
        self.bindings: dict[str, tuple[str, str]] = {}
        self.top_imports: list[tuple[str, int]] = []
        self.body_refs: set[str] = set()
        for node in ast.parse(path.read_text("utf-8")).body:
            if isinstance(node, ast.If) and _is_type_checking(node.test):
                continue
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self.defs[node.name] = node
            elif isinstance(node, ast.Import | ast.ImportFrom):
                self._read_import(node)
            else:
                for target in _assigned(node):
                    self.defs[target] = node
                self.body_refs |= _name_loads(node)

    def base(self, level: int) -> str:
        """Return the package a relative import of ``level`` dots resolves against."""
        parts = self.name.split(".")
        if not self.is_package:
            parts = parts[:-1]
        drop = level - 1
        return ".".join(parts[: len(parts) - drop] if drop else parts)

    def absolute(self, node: ast.ImportFrom) -> str:
        """Return the dotted module ``node`` imports from, relative or not."""
        base = self.base(node.level) if node.level else ""
        if base and node.module:
            return f"{base}.{node.module}"
        return node.module or base

    def _read_import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                self.top_imports.append((alias.name, node.lineno))
                bound = alias.asname or alias.name.split(".")[0]
                self.bindings[bound] = (alias.name, "*module*")
            return
        target = self.absolute(node)
        if not target:
            return
        self.top_imports.append((target, node.lineno))
        for alias in node.names:
            self.bindings[alias.asname or alias.name] = (target, alias.name)


def load_modules() -> dict[str, Module]:
    """Parse every shipped module in the workspace, keyed by dotted name."""
    modules: dict[str, Module] = {}
    for top, relative in TREES.items():
        tree = REPO_ROOT / relative
        for path in sorted(tree.rglob("*.py")):
            parts = list(path.relative_to(tree).with_suffix("").parts)
            package = not parts or parts[-1] == "__init__"
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            name = ".".join([top, *parts])
            modules[name] = Module(name, path, is_package=package)
    return modules


class Walk:
    """What an entry point imports, and which definitions it uses."""

    def __init__(self, modules: dict[str, Module], split: frozenset[str]) -> None:
        """Walk ``modules``; a non-empty ``split`` gates entry to ``ENGINE_MODULE``."""
        self.modules = modules
        self.split = split
        self.imported: set[str] = set()
        self.visited: set[tuple[str, str]] = set()
        self.uses: list[tuple[str, str, str, str]] = []
        self.queue: deque[tuple[str, str]] = deque()

    def run(self, module: str, name: str) -> None:
        """Walk everything ``module.name`` reaches."""
        self.import_module(module)
        self.queue.append((module, name))
        while self.queue:
            current = self.queue.popleft()
            if current in self.visited:
                continue
            self.visited.add(current)
            self.visit(*current)

    def import_module(self, name: str) -> None:
        """Mark ``name`` and its parents imported, with their module-level imports."""
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            if prefix in self.imported or prefix not in self.modules:
                continue
            self.imported.add(prefix)
            for target, _line in self.modules[prefix].top_imports:
                self.import_module(target)

    def visit(self, module_name: str, name: str) -> None:
        """Resolve one name: follow it into a sibling module, or into its body."""
        module = self.modules.get(module_name)
        if module is None:
            return
        if name in module.bindings:
            self.follow_binding(module_name, name)
            return
        node = module.defs.get(name)
        if node is None:
            return
        if self.split and module_name == ENGINE_MODULE and name not in self.split:
            return
        for ref in _name_loads(node):
            self.queue.append((module_name, ref))
        self.follow_deferred_imports(module, node)

    def follow_binding(self, module_name: str, name: str) -> None:
        """Record a cross-module use and queue the definition behind it."""
        target, original = self.modules[module_name].bindings[name]
        self.uses.append((module_name, name, target, original))
        self.import_module(target)
        if original != "*module*":
            self.import_module(f"{target}.{original}")
            self.queue.append((target, original))

    def follow_deferred_imports(self, module: Module, node: ast.AST) -> None:
        """Follow the imports written inside a reached definition."""
        for sub in ast.walk(node):
            if sub is node:
                continue
            if isinstance(sub, ast.ImportFrom):
                target = module.absolute(sub)
                if not target:
                    continue
                self.import_module(target)
                for alias in sub.names:
                    self.import_module(f"{target}.{alias.name}")
                    self.queue.append((target, alias.name))
            elif isinstance(sub, ast.Import):
                for alias in sub.names:
                    self.import_module(alias.name)


def main() -> int:
    """Run both rules and report; non-zero when either fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    modules = load_modules()
    walk = Walk(modules, ENGINE_GROUP)
    walk.run(ENGINE_MODULE, ENGINE_ENTRY)

    used = sorted(
        f"{source}.{bound} -> {target}.{original}"
        for source, bound, target, original in walk.uses
        if target in MARKER_SET_MODULES
    )
    importers = {
        name: line
        for name in sorted(walk.imported)
        for target, line in modules[name].top_imports
        if target in MARKER_SET_MODULES
    }

    if args.verbose:
        print(f"entry point:      {ENGINE_MODULE}.{ENGINE_ENTRY}")
        print(f"modules imported: {len(walk.imported)}")
        print(f"definitions used: {len(walk.visited)}")

    failures: list[str] = []
    if used:
        failures.append("the engine uses marker-set definitions:")
        failures += [f"  {entry}" for entry in used]

    unexpected = sorted(set(importers) - set(EXEMPT))
    if unexpected:
        failures.append("marker sets imported by a module with no exemption:")
        failures += [f"  {name}:{importers[name]}" for name in unexpected]

    stale = sorted(set(EXEMPT) - set(importers))
    if stale:
        failures.append("EXEMPT names a module that no longer imports marker sets:")
        failures += [f"  {name}" for name in stale]

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1

    print(
        f"engine at {ENGINE_MODULE}.{ENGINE_ENTRY} uses no marker-set definition; "
        f"{len(importers)} exempt modules import them, all accounted for."
    )
    if args.verbose:
        for name in sorted(importers):
            print(f"  {name}:{importers[name]}  {EXEMPT[name]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
