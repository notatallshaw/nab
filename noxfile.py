"""Nox sessions for nab's per-workspace tests and its type-check matrix.

``tests`` runs one workspace's suite and gates each package it owns at 100
percent. ``fail_under`` and the ``[tool.coverage.paths]`` remaps live in
``pyproject.toml``; the per-package ``--include`` globs are built here.
``types`` runs one type-checker over the source trees in ``TYPED_TREES``.
``benchmarks`` runs the benchmark-harness tests, which carry the ``benchmark``
marker and so sit outside every workspace run. ``dists`` builds every
distribution and installs each sdist and wheel to catch packaging regressions
that building alone misses. All take their pinned dependencies from
``.github/requirements``.

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
# nab-index rides with the python workspace: nab-python is its only consumer
# and its tests supply most of nab-index's coverage, so the two are gated here
# without running nab-python's suite twice.
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
    # see, and combines their data files when the session ends. Its own gate is
    # off because it scores every source package at once, and a workspace only
    # imports the ones it owns; the per-package reports below are the gate.
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
    # nab-python/benchmarks, which no coverage gate owns, so this session only
    # has to prove they still pass.
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
    """Type-check the trees in ``TYPED_TREES`` with one checker."""
    # Installing the whole workspace resolves every import; TYPED_TREES alone
    # decides what is reported, which is how nab-python's own errors stay out.
    _install(session, TYPES_LOCK, ["nab-resolver", "nab-index", "nab-python", "."])
    session.run(*TYPE_CHECKERS[checker])


@nox.session
def dists(session: nox.Session) -> None:
    """Build every distribution and prove each sdist and wheel installs."""
    # Only the build toolchain lands here; the check builds each package in a
    # subprocess and installs it into its own throwaway venv.
    session.install("--upgrade", "pip>=26.1")
    session.install("-r", DISTS_LOCK)
    # The build runs with --no-isolation, so the backend comes from the lock
    # of [build-system].requires rather than from a hand-listed group.
    session.install("-r", BUILD_LOCK)
    session.run("python", "tasks/check_dists.py")
