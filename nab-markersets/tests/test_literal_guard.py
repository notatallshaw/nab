"""Membership values need no reads or string conversion for version-limit checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nab_markersets import _markersets as engine

if TYPE_CHECKING:
    from collections.abc import Mapping


@pytest.mark.parametrize(
    "variable",
    [
        "python_version",
        "python_full_version",
        "implementation_version",
        "platform_release",
    ],
)
def test_literal_guard_does_not_read_membership_values(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    marker = engine.parse(
        f'{variable} >= "1" and sys_platform == "linux"'
        ' and "dev" in dependency_groups and "fast" in extras'
    )
    read = engine._atom_env_value
    calls: list[str] = []

    def recorded_read(atom: engine.Atom, env: Mapping[str, object]) -> object:
        """Count actual environment reads while preserving their values."""
        calls.append(atom.origin)
        return read(atom, env)

    monkeypatch.setattr(engine, "_atom_env_value", recorded_read)
    engine.reject_oversized_literals(
        marker,
        {
            variable: "3.12",
            "sys_platform": "linux",
            "dependency_groups": frozenset({"dev"}),
            "extras": frozenset({"fast"}),
        },
    )
    assert calls == [variable, "sys_platform"]
