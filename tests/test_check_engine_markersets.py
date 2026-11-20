"""The engine checker rejects use of the public conversion module."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tasks" / "check_engine_markersets.py"
_spec = importlib.util.spec_from_file_location("nab_check_engine_markersets", _PATH)
assert _spec is not None
assert _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


def run_checker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    *,
    exempt_import: bool = False,
) -> int:
    """Run the checker against one engine module with the given import policy."""
    path = tmp_path / "engine.py"
    path.write_text(source)
    module = checker.Module(checker.ENGINE_MODULE, path, is_package=False)
    monkeypatch.setattr(
        checker, "load_modules", lambda: {checker.ENGINE_MODULE: module}
    )
    exemptions = (
        {checker.ENGINE_MODULE: "Test import without use."} if exempt_import else {}
    )
    monkeypatch.setattr(checker, "EXEMPT", exemptions)
    monkeypatch.setattr(sys, "argv", [str(_PATH)])
    return checker.main()


@pytest.mark.parametrize("use", [False, True])
@pytest.mark.parametrize(
    "statement",
    [
        "from nab_markersets.conversions import MarkerSetCache as feature",
        "from nab_markersets import conversions as feature",
    ],
)
def test_conversion_import_and_use_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str, use: bool
) -> None:
    source = f"{statement}\ndef _resolve_with_micro_narrowing():\n"
    source += "    return feature\n" if use else "    return None\n"
    assert run_checker(tmp_path, monkeypatch, source, exempt_import=True) == int(use)


@pytest.mark.parametrize("reached", [False, True])
@pytest.mark.parametrize(
    "statement",
    [
        "from nab_markersets.conversions import MarkerSetCache as feature",
        "import nab_markersets.conversions as feature",
        "from nab_markersets import conversions as feature",
    ],
)
def test_deferred_import_is_rejected_only_in_a_reached_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str, reached: bool
) -> None:
    source = f"def helper():\n    {statement}\n    return feature\n"
    source += "def _resolve_with_micro_narrowing():\n"
    source += "    return helper()\n" if reached else "    return None\n"
    assert run_checker(tmp_path, monkeypatch, source) == int(reached)
