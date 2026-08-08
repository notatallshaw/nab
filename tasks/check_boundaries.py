"""Check the workspace import boundaries the four nab packages are built on.

Four rules, all over shipped source only:

``declared``
    A package may import a sibling only when it depends on it. The allowed
    edges come from each package's ``[project].dependencies``, so the check
    has no second list to keep in step with the real one.

``public``
    A package may not import an underscore-prefixed name from a sibling.
    Reaching past the underscore couples a caller to something the owning
    package can rename without notice.

``supported``
    Where a sibling publishes a supported-path table in its package
    docstring, a name in that table must be imported from the path the
    table gives it. Another module may happen to re-export the same name,
    but only the table's path is held still across releases, so an import
    that goes the other way works today and breaks on a reshuffle.

``vendored``
    ``_vendor`` is stricter still: it is off limits to every other package
    and must not be re-exported to make it reachable. nab-python vendors
    packaging so a resolve does not depend on the ambient copy, and
    publishing any of that tree would commit nab-python to a third-party
    surface it does not own and cannot re-vendor freely.

Tests are not shipped and may reach into what they test, so only the
``src`` trees are walked. Vendored code is skipped: it is third-party.

Run directly::

    python tasks/check_boundaries.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]

REMEDY = (
    "Use the sibling's public API, or promote the helper in the same change: "
    "drop the underscore, add it to that module's __all__, and update its "
    "existing callers."
)

# Packages whose docstring publishes a supported-path table. Listed here so a
# table that stops parsing fails the run rather than quietly dropping the
# ``supported`` rule for the package it covers.
PUBLISHES_SUPPORTED_PATHS = ("nab_resolver",)


class Package:
    """One workspace package: its module, its source tree, its declared deps."""

    def __init__(self, directory: Path) -> None:
        """Read ``directory``'s pyproject for the name and dependency list."""
        config = tomllib.loads((directory / "pyproject.toml").read_text("utf-8"))
        project = config["project"]
        self.dist_name: str = project["name"]
        self.module: str = self.dist_name.replace("-", "_")
        self.source: Path = directory / "src" / self.module
        self.requires: frozenset[str] = frozenset(
            _requirement_module(text) for text in project.get("dependencies", [])
        )
        self.supported: dict[str, str] = (
            _supported_paths(self.source, self.module)
            if self.module in PUBLISHES_SUPPORTED_PATHS
            else {}
        )


def _requirement_module(text: str) -> str:
    """Return the module a dependency specifier names, without its version."""
    for separator in ("[", "=", ">", "<", "!", "~", ";", " "):
        text = text.split(separator, 1)[0]
    return text.strip().replace("-", "_")


def _supported_paths(source: Path, module: str) -> dict[str, str]:
    """Map each name in ``module``'s supported-path table to its module path.

    The table sits in the package docstring as indented ``<module path>  <names>``
    rows. A row whose name list ends in a comma continues on the next indented
    line, which is how a long row wraps.
    """
    init = source / "__init__.py"
    docstring = ast.get_docstring(ast.parse(init.read_text("utf-8"), str(init))) or ""
    row = re.compile(rf"^\s+({re.escape(module)}\.\w+)\s+(\S.*)$")

    supported: dict[str, str] = {}
    module_path = ""
    wrapped = False
    for line in docstring.splitlines():
        match = row.match(line)
        if match:
            module_path, names = match.group(1), match.group(2).rstrip()
        elif wrapped and line.startswith(" ") and line.strip():
            names = line.strip()
        else:
            wrapped = False
            continue
        supported.update(
            (name.strip(), module_path) for name in names.split(",") if name.strip()
        )
        wrapped = names.endswith(",")
    return supported


def packages() -> list[Package]:
    """Return the umbrella package and every hatch workspace member."""
    hatch = tomllib.loads((REPO_ROOT / "hatch.toml").read_text("utf-8"))
    members = hatch["envs"]["default"]["workspace"]["members"]
    return [Package(REPO_ROOT)] + [Package(REPO_ROOT / name) for name in members]


def imported_paths(tree: ast.AST) -> list[tuple[str, int]]:
    """Every absolute dotted path the module imports, with its line number."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, node.lineno))
            found.extend(
                (f"{node.module}.{alias.name}", node.lineno) for alias in node.names
            )
    return found


def violations(package: Package, modules: dict[str, Package]) -> list[str]:
    """Return every boundary rule ``package`` breaks, as printable lines."""
    found: list[str] = []
    for path in sorted(package.source.rglob("*.py")):
        if "_vendor" in path.parts:
            continue
        tree = ast.parse(path.read_text("utf-8"), str(path))
        location = path.relative_to(REPO_ROOT)
        for dotted, lineno in sorted(set(imported_paths(tree))):
            head, _, rest = dotted.partition(".")
            if head not in modules or head == package.module:
                continue
            if head not in package.requires:
                found.append(
                    f"{location}:{lineno}: {package.dist_name} imports {dotted} "
                    f"but does not depend on {modules[head].dist_name}"
                )
            parts = rest.split(".")
            if "_vendor" in parts:
                found.append(
                    f"{location}:{lineno}: {dotted} reaches into a vendored tree; "
                    f"it stays private to {modules[head].dist_name} and is not to "
                    "be re-exported"
                )
                continue
            private = next(
                (
                    part
                    for part in parts
                    if part.startswith("_") and not part.startswith("__")
                ),
                None,
            )
            if private is not None:
                found.append(f"{location}:{lineno}: {dotted} is private ({private})")

            module_path, _, name = dotted.rpartition(".")
            promised = modules[head].supported.get(name)
            if promised is not None and promised != module_path:
                found.append(
                    f"{location}:{lineno}: {modules[head].dist_name} supports "
                    f"{name} at {promised}, not through {module_path}"
                )
    return found


def main() -> int:
    """Report every violation and return the process exit status."""
    known = {package.module: package for package in packages()}
    unread = [
        module
        for module in PUBLISHES_SUPPORTED_PATHS
        if module not in known or not known[module].supported
    ]
    if unread:
        print(
            f"Could not read a supported-path table from: {', '.join(unread)}",
            file=sys.stderr,
        )
        return 1

    found = [
        message for package in known.values() for message in violations(package, known)
    ]
    if found:
        print("Workspace import boundary violations:\n", file=sys.stderr)
        for message in found:
            print(f"  {message}", file=sys.stderr)
        print(f"\n{REMEDY}", file=sys.stderr)
        return 1

    names = ", ".join(sorted(known))
    print(f"import boundaries clean across {names}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
