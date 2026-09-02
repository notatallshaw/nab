"""What the entry path is allowed to import, proved in a fresh interpreter.

Every case here runs its probe as its own process, because the suite's
own ``conftest`` imports :mod:`nab.cli` before any test runs and a probe
sharing that interpreter would find the whole graph already built.

The bans are the design's import rule: ``nab.cli`` reads a table and walks
a line, so it needs no package of nab's own, no HTTP library and none of
the heavier stdlib modules the rest of nab uses.  ``nab._cli.dispatch``,
``nab._cli.render``, ``nab._cli.diagnose`` and ``nab.env`` are the
sanctioned exemptions, and each is loaded only by a line that asked for it.

The last cases ban what a command holds after it has dispatched: a line
that only reads settings loads nothing a resolve needs and no platform
table; a line that locks holds no part of ``email``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# The distributions a resolve needs and the two HTTP libraries a fetch
# needs.  None of them belongs on a line that has not dispatched yet.
_BANNED_ROOTS = (
    "nab_project",
    "nab_provider",
    "nab_markersets",
    "nab_resolver",
    "nab_index",
    "truststore",
    "urllib3",
)

# The stdlib modules the import rule keeps off the entry path, each one
# priced in the design.  ``typing`` is here because ``from typing import
# TYPE_CHECKING`` is itself an import of it.
_BANNED_STDLIB = (
    "typing",
    "pathlib",
    "dataclasses",
    "datetime",
    "re",
    "enum",
    "difflib",
    "textwrap",
    "shutil",
    "tomli",
)

# Matched on the full name rather than the first component, because
# ``urllib.parse`` is always loaded and ``html`` is not the offender.
_BANNED_STDLIB_NAMES = ("html.parser", "urllib.request")

_ROOTS_PROBE = """
import sys

import nab.cli

roots = {name.partition(".")[0] for name in sys.modules}
leaked = sorted(roots & set(sys.argv[1:]))
assert not leaked, f"importing nab.cli loaded {leaked}"
"""

_NAMES_PROBE = """
import sys

import nab.cli

leaked = sorted(set(sys.argv[1:]) & sys.modules.keys())
assert not leaked, f"importing nab.cli loaded {leaked}"
"""

# Runs one command line through the CLI and prints what it added to the
# module set, so a case can name the modules rather than count them.  The
# run's own two streams are captured, so the list is modules alone.
_ADDED_PROBE = """
import io
import sys
from contextlib import redirect_stderr, redirect_stdout

import nab.cli

wanted = int(sys.argv[1])
before = set(sys.modules)
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    status = nab.cli.run(tuple(sys.argv[2:]))

assert status == wanted, (status, out.getvalue(), err.getvalue())
sys.stderr.write(" ".join(sorted(set(sys.modules) - before)))
"""


# Runs one command line and prints which of the modules named in its first
# argument the process still holds.  A name is matched against a module and
# against its root package, so one list can ban a whole distribution and a
# single module of another.
_HELD_PROBE = """
import io
import sys
from contextlib import redirect_stderr, redirect_stdout

import nab.cli

banned = set(sys.argv[1].split(","))
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    status = nab.cli.run(tuple(sys.argv[2:]))

assert status == 0, (status, out.getvalue(), err.getvalue())
held = set(sys.modules) | {name.partition(".")[0] for name in sys.modules}
sys.stderr.write(" ".join(sorted(held & banned)))
"""

# What a command that only reads settings must not end up holding: the
# solver, the provider and project halves a resolve goes through, and
# the writer that emits a lockfile.
_RESOLVE_STACK = (
    "nab_resolver",
    "nab_project.lockfile",
    "nab_project.resolve",
    "nab_provider.provider",
    "nab_provider.requirements_file",
    "tomli_w",
)

# Reading an index URL only needs to know whether it says ``file:``, which
# is why :mod:`nab_index.file_urls` exists apart from the client.
_INDEX_READER = (
    "nab_index.local_index",
    "nab_index.client",
    "zipfile",
)

_PROJECT = '[project]\nname = "probe"\nversion = "0.1"\ndependencies = []\n'


def _run(probe: str, *arguments: str, cwd: Path | None = None) -> str:
    """Run one probe in a fresh interpreter and hand back what it wrote.

    A ``cwd`` is also given its own XDG roots, so a probe that reads the
    config ladder sees that directory and no file of the real user's.
    Every ``NAB_*`` variable is dropped, because a bad one is a config
    error and would exit the probe before it printed anything.
    """
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("NAB_")
    }
    if cwd is not None:
        environment["XDG_CONFIG_HOME"] = str(cwd / "config")
        environment["XDG_CACHE_HOME"] = str(cwd / "cache")

    finished = subprocess.run(  # noqa: S603 - the probe is this file's own source
        [sys.executable, "-c", probe, *arguments],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=environment,
    )

    assert finished.returncode == 0, finished.stderr
    return finished.stderr


def test_importing_the_cli_loads_no_distribution_of_its_own() -> None:
    """Probe A: not one of the four packages, and neither HTTP library."""
    _run(_ROOTS_PROBE, *_BANNED_ROOTS)


def test_importing_the_cli_loads_none_of_the_heavy_stdlib() -> None:
    """Probe B: the ten modules the import rule prices and refuses."""
    _run(_NAMES_PROBE, *_BANNED_STDLIB)


def test_importing_the_cli_loads_no_page_reader_or_url_opener() -> None:
    """One reads an HTML listing and one opens a ``file://`` URL."""
    _run(_NAMES_PROBE, *_BANNED_STDLIB_NAMES)


@pytest.mark.parametrize("line", [("--version",), ("lock", "--nope")])
def test_the_output_layer_is_untouched_until_a_command_runs(
    line: tuple[str, ...],
) -> None:
    """Probe C: neither the version line nor a refusal builds a printer.

    Routing a refused line through the printer would put ``nab.output``
    and the 44 modules behind it on the path of every mistyped command.
    A refusal reaches ``nab.env`` alone, for the colour decision.
    """
    status = "0" if line == ("--version",) else "2"

    added = _run(_ADDED_PROBE, status, *line).split()

    assert "nab.output" not in added


# The most a page may cost: the renderer, the module holding the colour
# rule, and the one import that module makes.  A ``TYPE_CHECKING`` block in
# ``nab.env`` would be an ``import typing``, and this bound is what says it
# has none.
_PAGE_COST = frozenset({"collections.abc", "nab._cli.render", "nab.env"})


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (("--version",), frozenset[str]()),
        (("--help",), _PAGE_COST),
        (("lock", "--help"), _PAGE_COST),
    ],
)
def test_a_page_or_a_version_adds_only_what_writes_it(
    line: tuple[str, ...], expected: frozenset[str]
) -> None:
    """Probe D: the renderer and the colour rule are the whole cost of ``--help``.

    A bound rather than a list, because ``collections.abc`` is already
    loaded on some interpreters.  It is a claim about which modules the line
    adds to the set ``import nab.cli`` already loaded, not about the size of
    that set: probes A and B are what pin the size.
    """
    assert set(_run(_ADDED_PROBE, "0", *line).split()) <= expected


@pytest.mark.parametrize("line", [("cache", "dir"), ("config", "list")])
def test_a_settings_command_loads_no_resolve_stack(
    line: tuple[str, ...], tmp_path: Path
) -> None:
    """Probe E: neither command that only reads settings holds any of these.

    Both run through the shared helpers in :mod:`nab._run`, which is why
    the resolve half of those helpers lives in :mod:`nab._resolve`.
    """
    (tmp_path / "pyproject.toml").write_text(_PROJECT, encoding="utf-8")

    assert _run(_HELD_PROBE, ",".join(_RESOLVE_STACK), *line, cwd=tmp_path) == ""


@pytest.mark.parametrize("line", [("cache", "dir"), ("config", "list")])
def test_a_settings_command_loads_no_index_reader(
    line: tuple[str, ...], tmp_path: Path
) -> None:
    """Probe F: neither command holds the reader, which only fetching needs."""
    (tmp_path / "pyproject.toml").write_text(_PROJECT, encoding="utf-8")

    assert _run(_HELD_PROBE, ",".join(_INDEX_READER), *line, cwd=tmp_path) == ""


def test_a_lock_command_holds_no_email_module(tmp_path: Path) -> None:
    """Probe G: locking loads no header parser and no HTTP-date reader.

    :mod:`nab_index.cached_client` and :mod:`nab_index.local_index` are held
    by the line even under ``--offline``, which keeps the probe off the
    network. They reach :mod:`email` only from the branch that reads an
    ``Expires`` header and the two that read a metadata block.
    """
    (tmp_path / "pyproject.toml").write_text(_PROJECT, encoding="utf-8")

    assert _run(_HELD_PROBE, "email", "lock", "--offline", cwd=tmp_path) == ""


# The platform vocabulary the matrix and environment tables are written in.
# ``nab_provider.target`` pulls ``nab_provider.marker_holds`` and
# ``nab_markersets`` in behind it.
_MATRIX_VOCABULARY = (
    "nab_provider.tags",
    "nab_provider.target",
    "nab_provider.marker_holds",
    "nab_markersets",
)


@pytest.mark.parametrize("line", [("cache", "dir"), ("config", "list")])
def test_a_settings_command_loads_no_platform_vocabulary(
    line: tuple[str, ...], tmp_path: Path
) -> None:
    """Probe H: a project declaring no matrix builds no platform tag table.

    :mod:`nab.config.values` is on the import path of every line that
    reads the configuration ladder, and it names these modules only in
    the parsers a ``[tool.nab.matrix]`` or ``[tool.nab.environment]``
    table reaches.
    """
    (tmp_path / "pyproject.toml").write_text(_PROJECT, encoding="utf-8")

    assert _run(_HELD_PROBE, ",".join(_MATRIX_VOCABULARY), *line, cwd=tmp_path) == ""
