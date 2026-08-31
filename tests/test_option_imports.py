"""What a command invocation is allowed to import.

The declaration builds 56 rows and imports the policy enums to do it, and
the command line never reads it: ``nab.optiondefs`` is the option model and
``nab.optiontable`` is where the rows are written, and the only path to
either is the configuration ladder, which ``nab lock`` and ``nab config``
ask for and the parser does not.  The four command modules resolve their
``Literal`` annotations through ``get_type_hints``, so they need the aliases
at run time and nothing else, which is why those sit in ``nab.flagtypes`` on
their own.

The probes run in a subprocess: the test session has already imported most
of nab, so an in-process check would pass whatever the import graph is.
"""

from __future__ import annotations

import subprocess
import sys

_REPORT = """
banned = [
    name
    for name in ("nab.optiondefs", "nab.optiontable", "nab.optionrows")
    if name in sys.modules
]
print(",".join(banned) if banned else "clean")
print("nab.flagtypes" in sys.modules)
"""


def _probe(source: str) -> list[str]:
    """Run ``source`` in a fresh interpreter and return its output lines."""
    finished = subprocess.run(  # noqa: S603 - the probe is this file's own source
        [sys.executable, "-c", source], capture_output=True, text=True, check=True
    )
    return finished.stdout.splitlines()


def _after_importing(module: str) -> list[str]:
    """Whether ``module`` reached the declaration, and whether it reached the leaf."""
    return _probe(f"import sys\nimport {module}\n{_REPORT}")


def test_a_command_invocation_imports_neither_the_model_nor_the_table() -> None:
    """Only the generators and the tests build the 56 rows."""
    assert _after_importing("nab.cli")[0] == "clean"


def test_the_command_signatures_reach_their_aliases_through_a_leaf() -> None:
    """A command module needs the aliases, and the leaf is how it gets them."""
    assert _after_importing("nab._lock")[1] == "True"


def test_the_alias_leaf_pulls_in_nothing_of_nabs() -> None:
    """Importing it alone leaves the rest of the package unloaded."""
    loaded = _probe(
        "import sys\n"
        "import nab.flagtypes\n"
        "print(len([n for n in sys.modules if n == 'nab' or n.startswith('nab.')]))\n"
    )
    assert loaded == ["2"]
