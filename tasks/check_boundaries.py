"""Check the workspace import boundaries the nab packages are built on.

The packages checked are the ones ``tasks/build_dists.py`` builds for a release,
so a distribution cannot join the release without also joining this check.

Four rules:

``declared``
    A package may import a sibling only when it depends on it. The allowed
    edges come from each package's ``[project].dependencies``, so there is
    no second list to keep in step.

``public``
    A package may not import an underscore-prefixed name from a sibling.
    Reaching past the underscore couples a caller to something the owning
    package can rename without notice.

``supported``
    Where a sibling publishes a supported-path table in its package
    docstring, a name in that table must be imported from the path the
    table gives it. Another module may re-export the same name, but only
    the table's path is held still across releases. Each row is also read
    against its own package's source, so a stale row fails without waiting
    for a sibling to import the name.

``vendored``
    ``_vendor`` is stricter still: it is off limits to every other package
    and must not be re-exported to make it reachable. ``VENDOR_ALLOWANCES``
    holds the one exception, nab-project naming
    ``nab_provider._vendor.packaging``: nab-project builds ``Version``,
    ``Requirement`` and ``VersionRange`` objects the provider consumes, and a
    second copy of the fork is a second set of classes that ``isinstance`` and
    dict keying disagree about. It goes away once the fork's changes land
    upstream.

Only the ``src`` trees are walked, since tests are not shipped and may reach
into what they test. Vendored code is skipped as third-party.

Run directly::

    python tasks/check_boundaries.py
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

# The boundaries job runs this with nothing installed, so on 3.11+ it must
# import from the stdlib alone; the tests venv carries tomli for 3.10.
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

# tasks/ is not a package, so build_dists is loaded from its own file: a plain
# import rests on CPython prepending the script directory, which -I and
# PYTHONSAFEPATH switch off.
_BUILD_DISTS = Path(__file__).resolve().parent / "build_dists.py"
_spec = importlib.util.spec_from_file_location("nab_build_dists", _BUILD_DISTS)
assert _spec is not None
assert _spec.loader is not None
build_dists = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_dists)

REPO_ROOT = build_dists.REPO_ROOT

REMEDY = (
    "Use the sibling's public API, or promote the helper in the same change: "
    "drop the underscore, add it to that module's __all__, and update its "
    "existing callers."
)

# (importing package, exact vendored prefix it may name); see ``vendored`` above.
VENDOR_ALLOWANCES: frozenset[tuple[str, str]] = frozenset(
    {("nab_project", "nab_provider._vendor.packaging")}
)

# Packages whose docstring publishes a supported-path table. Listed so a table
# that stops parsing fails the run instead of dropping the ``supported`` rule.
PUBLISHES_SUPPORTED_PATHS = ("nab_resolver",)


class Package:
    """One workspace package: its module, source tree, deps and supported paths."""

    def __init__(self, directory: Path) -> None:
        """Read ``directory``'s pyproject, plus its supported-path table if any."""
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
    line.
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


def _module_file(package: Package, module_path: str) -> Path:
    """Return the file a row's dotted path names, whether module or package."""
    base = package.source.joinpath(*module_path.split(".")[1:])
    init = base / "__init__.py"
    return init if init.is_file() else base.with_suffix(".py")


def _bound_names(body: list[ast.stmt]) -> set[str]:
    """Return the names a body binds by def, class, assignment or import.

    Recurses into ``if`` and ``try`` blocks, so a name bound only under
    ``TYPE_CHECKING`` or in an import fallback still counts.
    """
    names: set[str] = set()
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names.update(
                alias.asname or alias.name.partition(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.If):
            names |= _bound_names(node.body + node.orelse)
        elif isinstance(node, ast.Try):
            names |= _bound_names(node.body + node.orelse + node.finalbody)
            for handler in node.handlers:
                names |= _bound_names(handler.body)
    return names


def unresolved_rows(package: Package) -> list[str]:
    """Return every supported-path row of ``package`` that names nothing."""
    found: list[str] = []
    for name, module_path in sorted(package.supported.items()):
        path = _module_file(package, module_path)
        location = path.relative_to(REPO_ROOT)
        prefix = f"{package.dist_name} supports {name} at {module_path}, but "

        if not path.is_file():
            found.append(f"{prefix}{location} does not exist")
            continue

        tree = ast.parse(path.read_text("utf-8"), str(path))
        if name not in _bound_names(tree.body):
            found.append(f"{prefix}{location} defines no {name}")
    return found


def packages() -> list[Package]:
    """Return one Package per distribution the release builds and publishes."""
    found: list[Package] = []
    for name in build_dists.PACKAGES:
        manifest = build_dists.source_dir(name) / "pyproject.toml"
        if not manifest.is_file():
            msg = f"{name} is in the release package list but {manifest} is missing"
            raise SystemExit(msg)
        found.append(Package(manifest.parent))
    return found


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


def _vendor_allowed(importer: str, dotted: str) -> bool:
    """Whether ``importer`` naming ``dotted`` is one of the listed allowances.

    Matched on the whole prefix, so allowing one subtree does not open the
    rest of that package's ``_vendor``.
    """
    return any(
        importer == allowed_importer
        and (dotted == prefix or dotted.startswith(f"{prefix}."))
        for allowed_importer, prefix in VENDOR_ALLOWANCES
    )


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
                if _vendor_allowed(package.module, dotted):
                    continue
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


def _report(header: str, messages: list[str], remedy: str) -> None:
    """Print one rule's failures to stderr, under ``header`` and above ``remedy``."""
    print(f"{header}\n", file=sys.stderr)
    for message in messages:
        print(f"  {message}", file=sys.stderr)
    print(f"\n{remedy}", file=sys.stderr)


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

    stale = [
        message for package in known.values() for message in unresolved_rows(package)
    ]
    if stale:
        _report(
            "Supported-path rows that name nothing:",
            stale,
            "Drop the row, or restore what it names in the same change.",
        )
        return 1

    found = [
        message for package in known.values() for message in violations(package, known)
    ]
    if found:
        _report("Workspace import boundary violations:", found, REMEDY)
        return 1

    names = ", ".join(sorted(known))
    print(f"import boundaries clean across {names}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
