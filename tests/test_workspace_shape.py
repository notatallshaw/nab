"""Check the hand-written lists that describe this workspace's shape.

``tasks/build_dists.py`` holds ``PACKAGES``, the distributions a release builds
and publishes. The lists these tests read name that same set in TOML, workflow
YAML and Python. A name missing from one escapes whatever that list controls.

The typed-tree settings in ``pyproject.toml`` and ``noxfile.TYPED_TREES`` are
left out: they name a subset of the workspace, and
``tests/test_type_check_scope.py`` holds them to each other.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from itertools import chain
from pathlib import Path
from types import ModuleType
from typing import Any

import tomli

REPO_ROOT = Path(__file__).resolve().parents[1]
NOXFILE = REPO_ROOT / "noxfile.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
HATCH_TOML = REPO_ROOT / "hatch.toml"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
CLI_OUTPUT = REPO_ROOT / "src" / "nab" / "output.py"
PACKAGING_BACKENDS = (
    REPO_ROOT / "nab-markersets" / "src" / "nab_markersets" / "_packaging.py"
)

# The released `packaging` each distribution declares, wherever it declares it.
PACKAGING_REQUIREMENTS = {
    "nab-markersets": "packaging>=26.3,<27",
    "nab-index": "packaging>=24.0",
    "nab-project": "packaging>=24.0",
}


def _task(filename: str) -> ModuleType:
    """Load one tasks/ script by path, since tasks/ is not an importable package."""
    path = REPO_ROOT / "tasks" / filename
    spec = importlib.util.spec_from_file_location(f"nab_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_dists = _task("build_dists.py")

# check_dists.py reaches build_dists through the script directory, which is on
# sys.path only when it runs as a script.
sys.modules.setdefault("build_dists", build_dists)
check_dists = _task("check_dists.py")

PACKAGES = build_dists.PACKAGES

# Import name -> distribution name, for the lists that name one or the other.
MODULES = {name.replace("-", "_"): name for name in PACKAGES}

# The directory each released distribution builds from, and the reverse map.
RELEASED_DIRS = {name: build_dists.source_dir(name) for name in PACKAGES}
DISTRIBUTION_AT = {directory: name for name, directory in RELEASED_DIRS.items()}

# Workspace members are the distributions that live in a subdirectory; the
# umbrella builds from the repo root.
MEMBERS = sorted(
    name for name, directory in RELEASED_DIRS.items() if directory != REPO_ROOT
)

# A dependency specifier down to its bare name.
_REQUIREMENT_NAME = re.compile(r"^[^\[=<>!~;\s]+")

# `run: pip install --no-deps -e a -e b`, the editable installs the property and
# crosshair jobs do outside nox.
_EDITABLE_INSTALL = re.compile(
    r"^\s*run: pip install --no-deps ((?:-e \S+ ?)+)$", re.MULTILINE
)

# `packages-dir: dist/<package>`, one per publish step.
_PUBLISHED_DIR = re.compile(r"^\s*packages-dir: dist/(\S+)$", re.MULTILINE)


def _literal(path: Path, name: str) -> Any:
    """Evaluate one module-level literal assignment out of a Python file.

    Reading the source sidesteps importing the file: noxfile.py needs nox
    installed and tasks/release.py needs tomlkit.
    """
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    msg = f"{path.name} defines no {name}"
    raise AssertionError(msg)


def _toml(path: Path) -> dict[str, Any]:
    """One TOML file, parsed."""
    return tomli.loads(path.read_text(encoding="utf-8"))


def _imports_released_packaging(name: str) -> bool:
    """Whether a distribution's shipped modules import released ``packaging``.

    The vendored fork is a different package under a different name, so the
    ``_vendor`` tree is skipped rather than counted as a use of this dependency.
    """
    for path in (RELEASED_DIRS[name] / "src").rglob("*.py"):
        if "_vendor" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [(node.module or "").split(".")[0]]
            elif isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            else:
                continue
            if "packaging" in roots:
                return True
    return False


def _packaging_requirement(name: str) -> str | None:
    """The ``packaging`` requirement a distribution declares, or None.

    An extra counts: nab-markersets runs on either copy and declares one under
    each, so the released spelling is optional there rather than absent.
    """
    manifest = _toml(RELEASED_DIRS[name] / "pyproject.toml")["project"]
    extras = manifest.get("optional-dependencies", {}).values()
    for text in [*manifest.get("dependencies", []), *chain.from_iterable(extras)]:
        found = _REQUIREMENT_NAME.match(text)
        if found is not None and found.group() == "packaging":
            return text
    return None


def _session_editables(name: str) -> list[str]:
    """The editable targets one noxfile.py session installs.

    Its own list, outside the WORKSPACES table the other tests read.
    """
    session = next(
        node
        for node in ast.walk(ast.parse(NOXFILE.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    call = next(
        node
        for node in ast.walk(session)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_install"
    )
    return ast.literal_eval(call.args[-1])


def _nox_workspaces() -> dict[str, tuple[list[str], list[str], list[str]]]:
    """noxfile.py's workspace -> (editables, pytest paths, gated packages) table."""
    return _literal(NOXFILE, "WORKSPACES")


def _distributions(editables: list[str]) -> set[str]:
    """The distributions a list of editable install targets names."""
    return {DISTRIBUTION_AT[(REPO_ROOT / editable).resolve()] for editable in editables}


def _requires(name: str) -> set[str]:
    """The released distributions ``name`` declares a dependency on."""
    manifest = _toml(RELEASED_DIRS[name] / "pyproject.toml")["project"]
    declared = {
        found.group()
        for text in manifest.get("dependencies", [])
        if (found := _REQUIREMENT_NAME.match(text))
    }
    return declared & set(PACKAGES)


def _closure(names: set[str]) -> set[str]:
    """``names`` plus every released distribution they depend on, transitively."""
    reached: set[str] = set()
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending.extend(_requires(name) - reached)
    return reached


def _dependabot_pip_directories() -> set[str]:
    """The directories dependabot raises Python dependency updates for."""
    blocks = re.split(
        r"^\s*- package-ecosystem: ",
        DEPENDABOT.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    pip = [block for block in blocks if block.startswith("pip\n")]
    assert len(pip) == 1, "expected one pip ecosystem block in dependabot.yml"
    listed = re.search(r"directories:\n((?:\s*- \S+\n)+)", pip[0])
    assert listed is not None, "the pip ecosystem block lists no directories"
    return set(re.findall(r"- (\S+)", listed.group(1)))


def _repo_relative(name: str, subdirectory: str) -> str:
    """A distribution's subdirectory as a repo-relative posix path."""
    return (RELEASED_DIRS[name] / subdirectory).relative_to(REPO_ROOT).as_posix()


def test_packages_holds_every_distribution_in_the_tree() -> None:
    """PACKAGES names every pyproject in the tree, the umbrella's at the root.

    Dotted directories are tooling (.venv, .github) and never distributions. An
    undotted one is taken at face value, so keep scratch checkouts out of the
    repo root.
    """
    in_tree = {REPO_ROOT} | {
        path.parent
        for path in REPO_ROOT.glob("*/pyproject.toml")
        if not path.parent.name.startswith(".")
    }

    assert in_tree == set(RELEASED_DIRS.values())


def test_hatch_envs_install_every_released_package() -> None:
    """Each hatch env that installs the workspace declares every member of it.

    Detached and skip-install envs install nothing, so they are left out.
    """
    declared = {
        name: sorted(env.get("workspace", {}).get("members", []))
        for name, env in _toml(HATCH_TOML)["envs"].items()
        if not env.get("detached") and not env.get("skip-install")
    }

    assert declared
    assert declared == dict.fromkeys(declared, MEMBERS)


def _suite_owners(paths: list[str]) -> set[str]:
    """The distributions whose test suites a workspace's pytest paths run."""
    return {DISTRIBUTION_AT[(REPO_ROOT / path).resolve().parent] for path in paths}


def test_nox_workspaces_install_what_they_run_and_gate() -> None:
    """Each nox workspace installs what its suites and gates need, exactly.

    Held one entry at a time, since a package dropped from one entry's
    editables is still installed by another.
    """
    for workspace, (editables, paths, gated) in _nox_workspaces().items():
        owned = {MODULES[module] for module in gated} | _suite_owners(paths)

        assert _distributions(editables) == _closure(owned), workspace


def test_nox_workspaces_form_an_install_chain() -> None:
    """Each workspace's editables extend the entry above it in the table.

    The tests session installs the whole table into one environment, adding
    each workspace's packages right before its suites run. A workspace imports
    only what it declares for as long as the table stays a chain.
    """
    installed: list[str] = []
    for workspace, (editables, _, _) in _nox_workspaces().items():
        assert editables[: len(installed)] == installed, workspace
        installed = editables


def test_benchmarks_session_installs_every_member() -> None:
    """The benchmarks session installs the workspace it collects from.

    pytest imports every module under the paths it is given before the
    ``benchmark`` marker deselects anything, so a member missing here fails
    collection in a job no coverage gate or workspace table watches.
    """
    assert _distributions(_session_editables("benchmarks")) == set(MEMBERS)


def test_umbrella_workspace_installs_every_released_package() -> None:
    """The umbrella entry installs every released package.

    No coverage gate pins this list.
    """
    editables, _, _ = _nox_workspaces()["umbrella"]

    assert _distributions(editables) == set(PACKAGES)


def test_nox_gates_every_released_package_at_full_coverage() -> None:
    """Between them the nox workspaces report coverage on every distribution."""
    gated = {
        module for _, _, modules in _nox_workspaces().values() for module in modules
    }

    assert gated == set(MODULES)


def test_ci_runs_every_nox_workspace() -> None:
    """CI runs ``nox -s tests`` unfiltered, which runs every workspace.

    A filtered run still passes, leaving the packages the others gate
    unmeasured.
    """
    text = TEST_WORKFLOW.read_text(encoding="utf-8")

    assert re.search(r"^\s*run: nox -s tests$", text, re.MULTILINE)


def test_coverage_measures_every_released_package() -> None:
    """source_pkgs decides what coverage measures at all."""
    measured = _toml(PYPROJECT)["tool"]["coverage"]["run"]["source_pkgs"]

    assert set(measured) == set(MODULES)


def test_coverage_paths_remap_every_released_package() -> None:
    """Each distribution's installed copy remaps onto its source tree.

    The workspaces install editable and run under coverage's parallel mode, so a
    package with no remap reports as two half-covered trees and misses the gate.
    """
    expected = {
        module: [_repo_relative(name, f"src/{module}"), f"*/{module}"]
        for module, name in MODULES.items()
    }

    assert _toml(PYPROJECT)["tool"]["coverage"]["paths"] == expected


def test_test_paths_cover_every_released_package() -> None:
    """pytest and nox collect every distribution's own suite."""
    expected = {_repo_relative(name, "tests") for name in PACKAGES}
    testpaths = _toml(PYPROJECT)["tool"]["pytest"]["ini_options"]["testpaths"]
    collected = {path for _, paths, _ in _nox_workspaces().values() for path in paths}

    assert set(testpaths) == expected
    assert collected == expected


def test_ci_installs_every_released_package_outside_nox() -> None:
    """The property and crosshair jobs install the workspace by hand, outside nox.

    A distribution missing from one of their install lines resolves from PyPI,
    so the job tests the published copy.
    """
    lines = _EDITABLE_INSTALL.findall(TEST_WORKFLOW.read_text(encoding="utf-8"))

    assert lines
    for line in lines:
        assert _distributions(re.findall(r"-e (\S+)", line)) == set(PACKAGES)


def test_dependabot_watches_every_released_package() -> None:
    """Dependabot raises Python updates per directory, not per repository."""
    watched = _dependabot_pip_directories()

    assert watched == {"/", *(f"/{name}" for name in MEMBERS)}


def test_release_workflow_publishes_every_released_package() -> None:
    """One publish step per distribution, reading the directory the build wrote.

    The umbrella cross-pins an exact version of each sibling, so a distribution
    built and not published leaves PyPI serving a ``nab`` nobody can install.
    """
    published = _PUBLISHED_DIR.findall(RELEASE_WORKFLOW.read_text(encoding="utf-8"))

    assert set(published) == set(PACKAGES)


def test_the_dists_check_imports_every_released_package() -> None:
    """The dists check installs the whole wheel set, then imports it.

    The import list is derived from PACKAGES and pinned here.
    """
    assert set(check_dists.MODULES) == set(MODULES)


def test_lock_config_covers_every_released_package() -> None:
    """``nab lock`` resolves every distribution from the tree and skips its cooldown.

    A member missing from the workspace table locks against the published copy
    instead of the source tree.
    """
    config = _toml(PYPROJECT)["tool"]["nab"]
    exempt = {
        name
        for rule in config["package-rules"]
        if rule.get("uploaded-prior-to") is False
        for name in rule["match"]
    }

    assert sorted(config["workspace"]["members"]) == MEMBERS
    assert exempt == set(PACKAGES)


def test_released_packaging_is_declared_wherever_it_is_imported() -> None:
    """Every distribution that imports released ``packaging`` declares it."""
    importing = {name for name in PACKAGES if _imports_released_packaging(name)}

    assert importing == set(PACKAGING_REQUIREMENTS)


def test_declared_packaging_ranges_are_the_tested_ones() -> None:
    """The declared ranges are the ones the suites have been run against.

    nab_markersets reads ``packaging._parser``, ``packaging._tokenizer`` and
    ``packaging.markers._eval_op``, none of which packaging promises, so its
    range carries a ceiling the others do not need.
    """
    declared = {
        name: requirement
        for name in PACKAGES
        if (requirement := _packaging_requirement(name)) is not None
    }

    assert declared == PACKAGING_REQUIREMENTS


def test_the_marker_algebra_takes_the_vendored_fork_first() -> None:
    """nab-markersets binds nab's fork when it is installed, released packaging otherwise.

    The reach into nab-provider's ``_vendor`` is by name at import time, so
    ``tasks/check_boundaries.py`` cannot see it and this is where it is held.
    Each candidate is an extra, so an install can ask for either by name.
    """
    extras = _toml(RELEASED_DIRS["nab-markersets"] / "pyproject.toml")["project"][
        "optional-dependencies"
    ]
    version = _toml(PYPROJECT)["project"]["version"]

    assert _literal(PACKAGING_BACKENDS, "BACKENDS") == (
        "nab_provider._vendor.packaging",
        "packaging",
    )
    assert extras == {
        "packaging": [PACKAGING_REQUIREMENTS["nab-markersets"]],
        "nab-vendored-packaging": [f"nab-provider=={version}"],
    }


def test_the_marker_algebra_holds_the_floor_it_declares() -> None:
    """The floor `_packaging` checks at import is the one the extra installs."""
    minimum = _literal(PACKAGING_BACKENDS, "MINIMUM")

    assert PACKAGING_REQUIREMENTS["nab-markersets"].startswith(f"packaging>={minimum},")


def test_the_standalone_session_installs_the_algebra_alone() -> None:
    """Adding a package here would put nab-provider's fork back in reach.

    That session is the only run where `nab_markersets` binds released
    packaging, and nothing about it would go red if it stopped doing so.
    """
    assert _distributions(_session_editables("standalone")) == {"nab-markersets"}


def test_cli_log_handlers_reach_every_released_package() -> None:
    """nab's log handler is attached per package, by top-level import name.

    A package missing from the tuple gets no handler, so its records fall
    through to the root logger: unformatted, not gated by ``-v``/``-q``, and
    outside the writer that clears the progress line.
    """
    loggers = _literal(CLI_OUTPUT, "_NAB_LOGGERS")

    assert set(loggers) == set(MODULES)


def test_version_and_classifier_checks_read_every_manifest() -> None:
    """The lockstep version check and the classifier check both walk every member.

    Each builds its manifest list from its own copy of the member names.
    """
    release = _literal(REPO_ROOT / "tasks" / "release.py", "WORKSPACE_PACKAGES")
    classifiers = _literal(
        Path(__file__).with_name("test_classifiers.py"), "WORKSPACE_PACKAGES"
    )

    assert sorted(release) == MEMBERS
    assert sorted(classifiers) == MEMBERS
