"""Nox sessions: ``tests``, ``standalone``, ``types``, ``benchmarks``, ``dists``.

Coverage is split across two files: ``fail_under`` and the
``[tool.coverage.paths]`` remaps live in ``pyproject.toml``, while the
per-package ``--include`` globs are built here. Every session takes its
pinned dependencies from ``.github/requirements``.

The Python version comes from whoever launches nox, so CI drives the matrix
through ``actions/setup-python`` and stays off the per-OS versioned-binary
lookup. Run a single cell locally::

    nox -s tests -- project
    nox -s "types(checker='mypy')"
    nox -s benchmarks
    nox -s dists
"""

from __future__ import annotations

from typing import NamedTuple

import nox
from nox.command import CommandFailed

# Stdlib venv keeps the backend explicit; nab never resolves through uv.
nox.options.default_venv_backend = "venv"

TESTS_LOCK = ".github/requirements/pylock.tests.toml"
TYPES_LOCK = ".github/requirements/pylock.types.toml"
DISTS_LOCK = ".github/requirements/pylock.dists.toml"
BUILD_LOCK = ".github/requirements/pylock.build.toml"

# workspace -> (editable packages, pytest paths, coverage-gated packages).
# The umbrella builds from the repo root, so it installs as ".".
#
# Order matters: each entry extends the packages of the one above it, so the
# tests session can run them all in one environment.
#
# nab-index and nab-provider are both gated with the project workspace:
# nab-project is nab-index's only consumer, and full coverage of nab_provider
# needs nab-project's tests. The provider entry gates nab_markersets and
# installs only what nab-provider needs, proving a host can take the provider
# without nab-index or nab-project.
WORKSPACES = {
    "resolver": (
        ["nab-resolver"],
        ["nab-resolver/tests"],
        ["nab_resolver"],
    ),
    "provider": (
        ["nab-resolver", "nab-markersets", "nab-provider"],
        ["nab-markersets/tests", "nab-provider/tests"],
        ["nab_markersets"],
    ),
    "project": (
        ["nab-resolver", "nab-markersets", "nab-provider", "nab-index", "nab-project"],
        ["nab-provider/tests", "nab-project/tests", "nab-index/tests"],
        ["nab_provider", "nab_project", "nab_index"],
    ),
    "umbrella": (
        [
            "nab-resolver",
            "nab-markersets",
            "nab-provider",
            "nab-index",
            "nab-project",
            ".",
        ],
        ["tests"],
        ["nab"],
    ),
}

# Every shipped tree. The vendored packaging tree under nab-provider is rebuilt
# from upstream, so each checker's config in pyproject.toml leaves it out.
TYPED_TREES = [
    "nab-resolver/src",
    "nab-markersets/src",
    "nab-provider/src",
    "nab-project/src",
    "nab-index/src",
    "src",
]

# The generated bijection goes to every checker, not to pyright alone: it
# exists to be read by one, and a row typed wrong for its parameter is an
# error in whichever reads it first.
CHECKED = [*TYPED_TREES, "tests/cli_bijection.py"]

# checker -> command; pyright reads its targets from [tool.pyright] in
# pyproject.toml, the rest take them on the command line. Paths on pyrefly's
# command line switch off the excludes in its config, so the vendored tree is
# excluded again here.
TYPE_CHECKERS = {
    "mypy": ["mypy", *CHECKED],
    "pyright": ["pyright"],
    "ty": ["ty", "check", *CHECKED],
    "pyrefly": ["pyrefly", "check", "--project-excludes", "**/_vendor/**", *CHECKED],
    "zuban": ["zuban", "check", *CHECKED],
}


def _install_lock(session: nox.Session, lock: str) -> None:
    """Install a pinned dependency lock."""
    # Installing a PEP 751 lock needs a recent pip.
    session.install("--upgrade", "pip>=26.1")
    session.install("-r", lock)


def _install_editable(session: nox.Session, packages: list[str]) -> None:
    """Install the given workspace packages editable."""
    if not packages:
        return

    # --no-deps keeps the run pinned to the locked closure.
    editable_args = [arg for package in packages for arg in ("-e", package)]
    session.install("--no-deps", *editable_args)


def _install(session: nox.Session, lock: str, editables: list[str]) -> None:
    """Install a pinned lock, then the given workspace packages editable."""
    _install_lock(session, lock)
    _install_editable(session, editables)


class _Step(NamedTuple):
    """One workspace's turn in the shared environment.

    ``adds`` is what this workspace installs on top of what the steps before it
    already put there. An unselected step still installs, so a later workspace
    gets the whole chain.
    """

    workspace: str
    adds: list[str]
    paths: list[str]
    gated: list[str]
    selected: bool


def _test_steps(session: nox.Session, selected: set[str]) -> list[_Step]:
    """Order ``WORKSPACES`` into install steps, ending at the last one selected.

    Fails the session, before anything is installed, if an entry does not
    extend the one above it: one environment has to serve them all.
    """
    steps: list[_Step] = []
    installed: list[str] = []
    for workspace, (editables, paths, gated) in WORKSPACES.items():
        if editables[: len(installed)] != installed:
            session.error(
                f"workspace {workspace!r} installs {editables}, "
                f"which does not extend {installed}"
            )

        adds = editables[len(installed) :]
        steps.append(_Step(workspace, adds, paths, gated, workspace in selected))
        installed = list(editables)

    last = max(index for index, step in enumerate(steps) if step.selected)
    return steps[: last + 1]


def _run_workspace(session: nox.Session, step: _Step, paths: list[str]) -> bool:
    """Run ``paths`` under coverage, then gate the workspace; True when both passed.

    ``paths`` is the workspace's suites less any an earlier workspace already
    ran; the coverage they recorded is still in the session's data file.
    Returns instead of raising so the caller can go on to the workspaces after
    this one. Within a workspace the first failure stops the rest.
    """
    try:
        # pytest-cov measures the xdist workers, which a bare `coverage run`
        # cannot see, and combines their data files at the end. Its own gate is
        # off because it scores every source package at once, and a workspace
        # only imports the ones it owns.
        if paths:
            session.run(
                "python",
                "-m",
                "pytest",
                "-n",
                "auto",
                "--cov",
                "--cov-append",
                "--cov-report=",
                "--cov-fail-under=0",
                *paths,
            )

        for package in step.gated:
            session.run("coverage", "report", f"--include=*/{package}/*")
    except CommandFailed:
        return False

    return True


@nox.session(reuse_venv=False)
def tests(session: nox.Session) -> None:
    """Run every workspace's tests and gate each package it owns at 100 percent.

    Positional arguments select workspaces (``nox -s tests -- project``);
    without them every workspace runs.

    One environment serves them all: each workspace extends the one before it,
    so installing what it adds right before its own suites leaves it importing
    exactly the packages it declares. A reused environment would already hold
    what the last run installed, so this session never reuses one.

    One coverage data file serves them all too. A suite runs once, in the first
    selected workspace that lists it, and a later workspace gates on what every
    suite before it recorded.

    A failing workspace does not stop the others, so one run reports them all.
    """
    selected = set(session.posargs) or set(WORKSPACES)
    unknown = selected - set(WORKSPACES)
    if unknown:
        session.error(f"no such workspace: {', '.join(sorted(unknown))}")

    steps = _test_steps(session, selected)
    _install_lock(session, TESTS_LOCK)
    session.run("coverage", "erase")

    ran: set[str] = set()
    failed: list[str] = []
    for step in steps:
        _install_editable(session, step.adds)
        if not step.selected:
            continue

        paths = [path for path in step.paths if path not in ran]
        ran.update(paths)
        if not _run_workspace(session, step, paths):
            failed.append(step.workspace)

    if failed:
        session.error(f"failing workspaces: {', '.join(failed)}")


@nox.session
def standalone(session: nox.Session) -> None:
    """Run nab-markersets' suite with nab-provider absent.

    Every other session installs nab-provider, whose vendored fork
    ``nab_markersets`` binds in preference, so this is the only run that reaches
    released packaging.
    """
    _install_lock(session, TESTS_LOCK)

    # The extra, and without --no-deps, so the range nab-markersets declares is
    # what pip resolves. The lock already pins a packaging inside that range,
    # so nothing moves; a range that stopped covering it would fail here.
    session.install("-e", "nab-markersets[packaging]")

    session.run("python", "-m", "pytest", "-n", "auto", "nab-markersets/tests")


@nox.session
def benchmarks(session: nox.Session) -> None:
    """Run the benchmark-harness tests the workspace sessions deselect."""
    # These cover the scripts under nab-resolver/benchmarks and
    # nab-project/benchmarks, which no coverage gate owns.
    _install(
        session,
        TESTS_LOCK,
        ["nab-resolver", "nab-markersets", "nab-provider", "nab-index", "nab-project"],
    )
    session.run(
        "python",
        "-m",
        "pytest",
        "-m",
        "benchmark",
        "nab-resolver/tests",
        "nab-project/tests",
    )


@nox.session
@nox.parametrize("checker", list(TYPE_CHECKERS))
def types(session: nox.Session, checker: str) -> None:
    """Run one checker over its own scope: :data:`CHECKED`, or pyright's include."""
    # The umbrella entry installs every distribution, so every import resolves.
    editables, _, _ = WORKSPACES["umbrella"]
    _install(session, TYPES_LOCK, editables)
    session.run(*TYPE_CHECKERS[checker])


@nox.session
def dists(session: nox.Session) -> None:
    """Build every distribution and prove each sdist and wheel installs."""
    session.install("--upgrade", "pip>=26.1")
    session.install("-r", DISTS_LOCK)
    # The build runs with --no-isolation, so the backend comes from the lock
    # of [build-system].requires rather than from a hand-listed group.
    session.install("-r", BUILD_LOCK)
    session.run("python", "tasks/check_dists.py")
