"""Nox sessions for nab: ``tests``, ``types``, ``benchmarks`` and ``dists``.

Coverage is split across two files: ``fail_under`` and the
``[tool.coverage.paths]`` remaps live in ``pyproject.toml``, while the
per-package ``--include`` globs are built here. Every session takes its
pinned dependencies from ``.github/requirements``.

The Python version comes from whoever launches nox, so CI drives the matrix
through ``actions/setup-python`` and stays off the per-OS versioned-binary
lookup. Run a single cell locally, for example::

    nox -s "tests(workspace='python')"
    nox -s "types(checker='mypy')"
    nox -s benchmarks
    nox -s dists
"""

from __future__ import annotations

import nox

# Stdlib venv keeps the backend explicit; nab never resolves through uv.
nox.options.default_venv_backend = "venv"

TESTS_LOCK = ".github/requirements/pylock.tests.toml"
TYPES_LOCK = ".github/requirements/pylock.types.toml"
DISTS_LOCK = ".github/requirements/pylock.dists.toml"
BUILD_LOCK = ".github/requirements/pylock.build.toml"

# workspace -> (editable packages, pytest paths, coverage-gated packages).
# Each entry installs the dependency closure of what it gates; the umbrella
# builds from the repo root, so it installs as ".".
#
# nab-index is gated with the python workspace because each entry has to reach
# 100 percent from its own suite, and nab-python's tests cover most of nab-index.
WORKSPACES = {
    "resolver": (
        ["nab-resolver"],
        ["nab-resolver/tests"],
        ["nab_resolver"],
    ),
    "python": (
        ["nab-resolver", "nab-index", "nab-python"],
        ["nab-python/tests", "nab-index/tests"],
        ["nab_python", "nab_index"],
    ),
    "umbrella": (
        ["nab-resolver", "nab-index", "nab-python", "."],
        ["tests"],
        ["nab"],
    ),
}

# nab-python is left out: it carries the vendored packaging tree, which is
# rebuilt from upstream and cannot be edited to satisfy a checker.
TYPED_TREES = ["nab-resolver/src", "nab-index/src", "src"]

# checker -> command; pyright reads its targets from [tool.pyright] in
# pyproject.toml, the rest take the trees on the command line.
TYPE_CHECKERS = {
    "mypy": ["mypy", *TYPED_TREES],
    "pyright": ["pyright"],
    "ty": ["ty", "check", *TYPED_TREES],
    "pyrefly": ["pyrefly", "check", *TYPED_TREES],
    "zuban": ["zuban", "check", *TYPED_TREES],
}


def _install(session: nox.Session, lock: str, editables: list[str]) -> None:
    """Install a pinned lock, then the given workspace packages editable."""
    # Installing a PEP 751 lock needs a recent pip.
    session.install("--upgrade", "pip>=26.1")
    session.install("-r", lock)

    # --no-deps keeps the run pinned to the locked closure above.
    editable_args = [arg for package in editables for arg in ("-e", package)]
    session.install("--no-deps", *editable_args)


@nox.session
@nox.parametrize("workspace", list(WORKSPACES))
def tests(session: nox.Session, workspace: str) -> None:
    """Run one workspace's tests and gate each package it owns at 100 percent."""
    editables, paths, packages = WORKSPACES[workspace]
    _install(session, TESTS_LOCK, editables)

    session.run("coverage", "erase")

    # pytest-cov measures the xdist workers, which a bare `coverage run` cannot
    # see, and combines their data files at the end. Its own gate is off because
    # it scores every source package at once, and a workspace only imports the
    # ones it owns.
    session.run(
        "python",
        "-m",
        "pytest",
        "-n",
        "auto",
        "--cov",
        "--cov-report=",
        "--cov-fail-under=0",
        *paths,
    )

    for package in packages:
        session.run("coverage", "report", f"--include=*/{package}/*")


@nox.session
def benchmarks(session: nox.Session) -> None:
    """Run the benchmark-harness tests the workspace sessions deselect."""
    # These cover the scripts under nab-resolver/benchmarks and
    # nab-python/benchmarks, which no coverage gate owns.
    _install(session, TESTS_LOCK, ["nab-resolver", "nab-index", "nab-python"])
    session.run(
        "python",
        "-m",
        "pytest",
        "-m",
        "benchmark",
        "nab-resolver/tests",
        "nab-python/tests",
    )


@nox.session
@nox.parametrize("checker", list(TYPE_CHECKERS))
def types(session: nox.Session, checker: str) -> None:
    """Run one checker over its own scope: ``TYPED_TREES``, or pyright's include."""
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
