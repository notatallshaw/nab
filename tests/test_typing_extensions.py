"""No import path into nab loads typing_extensions.

Only ``override`` is needed at class-body time, and each package reads it
off ``typing`` through its own ``_compat`` shim.  The dependency stays
declared for ``Self`` and ``Protocol``, which a checker reads and the
interpreter never loads.

The probe runs in a fresh subprocess so earlier test imports cannot mask a
dependency.
"""

from __future__ import annotations

import subprocess
import sys

_PACKAGES = ("nab", "nab_index", "nab_project", "nab_provider")

_PROBE = """
import importlib
import pkgutil
import sys

sys.modules["typing_extensions"] = None

for root in sys.argv[1:]:
    package = importlib.import_module(root)
    names = sorted(
        found.name for found in pkgutil.walk_packages(package.__path__, root + ".")
    )
    assert names, f"found no submodules under {root}"
    for name in names:
        importlib.import_module(name)
"""


def test_no_module_loads_typing_extensions() -> None:
    """Every submodule of the four packages imports with typing_extensions blocked."""
    finished = subprocess.run(  # noqa: S603 - the probe is this file's own source
        [sys.executable, "-c", _PROBE, *_PACKAGES],
        capture_output=True,
        text=True,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr
