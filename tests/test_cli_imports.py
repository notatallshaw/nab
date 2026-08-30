"""What the entry path is allowed to import, proved in a fresh interpreter.

Every case here runs its probe as its own process, because the suite's
own ``conftest`` imports :mod:`nab.cli` before any test runs and a probe
sharing that interpreter would find the whole graph already built.

The bans are the design's import rule: ``nab.cli`` reads a table and walks
a line, so it needs no package of nab's own, no HTTP library and none of
the heavier stdlib modules the rest of nab uses.  ``nab._cli.dispatch``,
``nab._cli.render``, ``nab._cli.diagnose`` and ``nab.env`` are the
sanctioned exemptions, and each is loaded only by a line that asked for it.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# The distributions a resolve needs and the two HTTP libraries a fetch
# needs.  None of them belongs on a line that has not dispatched yet.
_BANNED_ROOTS = (
    "nab_project",
    "nab_provider",
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


def _run(probe: str, *arguments: str) -> str:
    """Run one probe in a fresh interpreter and hand back what it wrote."""
    finished = subprocess.run(  # noqa: S603 - the probe is this file's own source
        [sys.executable, "-c", probe, *arguments],
        capture_output=True,
        text=True,
        check=False,
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
