"""The generated bijection module against the declaration it was written from.

``tests/cli_bijection.py`` is what makes a renamed dest and a mistyped row
editor errors, and it is only worth that while it matches the table.  This
runs the generator's own ``--check``, which is the same comparison CI's
checkers would fail on a working day later.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "tasks" / "gen_bijection.py"
_spec = importlib.util.spec_from_file_location("nab_gen_bijection", _PATH)
assert _spec is not None
assert _spec.loader is not None
gen_bijection = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_bijection)


def test_the_generated_module_matches_the_declaration() -> None:
    """A stale file fails here, with the diff on stdout."""
    assert gen_bijection.main(["--check"]) == 0
