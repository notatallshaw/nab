"""nab_resolver must not pull ``dataclasses`` into an importing interpreter.

Importing it also loads ``inspect``, ``ast``, ``dis``, ``tokenize``,
``linecache``, ``opcode``, ``token`` and ``copy``, which a consumer that wants
only the resolver pays for at startup.  The package's value types are written
out by hand so that none of it arrives.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import importlib
import pkgutil
import sys

# A None entry makes ``import dataclasses`` raise ImportError.
sys.modules["dataclasses"] = None

import nab_resolver

names = sorted(module.name for module in pkgutil.iter_modules(nab_resolver.__path__))
assert names, "found no nab_resolver submodules to import"
for name in names:
    importlib.import_module(f"nab_resolver.{name}")
"""


def test_imports_without_dataclasses() -> None:
    """Every submodule imports with dataclasses blocked."""
    subprocess.run([sys.executable, "-c", _PROBE], check=True)  # noqa: S603
