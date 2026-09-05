"""Import boundaries for command invocations.

Runtime code imports ``optiondefs`` through the generated configuration
registry and never imports ``optiontable`` or ``optionrows``. Commands resolve
their ``Literal`` aliases from ``flagtypes``. Fresh subprocesses keep earlier
test imports from masking dependencies.
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


def test_a_command_invocation_imports_no_option_module() -> None:
    """Only the generators and the tests build the 64 rows."""
    assert _after_importing("nab.cli")[0] == "clean"


def test_a_run_reads_the_registry_and_never_the_declaration() -> None:
    """A run loads ``nab.optiondefs`` and no other option module."""
    loaded = _probe(
        "import sys\n"
        "import nab._lock\n"
        "print(sorted(n for n in sys.modules if n.startswith('nab.option')))\n"
    )
    assert loaded == ["['nab.optiondefs']"]


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
