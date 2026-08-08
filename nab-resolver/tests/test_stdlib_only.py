"""nab_resolver must import without typing_extensions, the dependency it used to declare."""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import importlib
import pkgutil
import sys

sys.modules["typing_extensions"] = None

import nab_resolver

names = sorted(module.name for module in pkgutil.iter_modules(nab_resolver.__path__))
assert names, "found no nab_resolver submodules to import"
for name in names:
    importlib.import_module(f"nab_resolver.{name}")
"""


def test_imports_without_typing_extensions() -> None:
    """Every submodule imports with typing_extensions blocked."""
    subprocess.run([sys.executable, "-c", _PROBE], check=True)  # noqa: S603
