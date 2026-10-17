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


@pytest.mark.parametrize("use", [False, True])
def test_conversion_import_and_use_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use: bool
) -> None:
    source = tmp_path / "engine.py"
    source.write_text(
        "from nab_markersets.conversions import MarkerSetCache\n"
        "def _resolve_with_micro_narrowing():\n"
        + ("    return MarkerSetCache()\n" if use else "    return None\n")
    )
    module = checker.Module(checker.ENGINE_MODULE, source, is_package=False)
    monkeypatch.setattr(
        checker, "load_modules", lambda: {checker.ENGINE_MODULE: module}
    )
    monkeypatch.setattr(
        checker, "EXEMPT", {checker.ENGINE_MODULE: "Test import without use."}
    )
    monkeypatch.setattr(sys, "argv", [str(_PATH)])
    assert checker.main() == int(use)
