"""Operation-owned project declarations with change detection."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from math import isnan
from typing import TYPE_CHECKING, Any

from . import toml_io
from .input_source import InputChangedError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["PreparedProjectInput"]


def _same_value(original: object, current: object) -> bool:
    """Compare parsed TOML values, including nested tables and arrays."""
    if isinstance(current, dict):
        return (
            isinstance(original, Mapping)
            and original.keys() == current.keys()
            and all(_same_value(value, current[key]) for key, value in original.items())
        )
    if isinstance(current, list):
        return (
            isinstance(original, list)
            and len(original) == len(current)
            and all(
                _same_value(left, right)
                for left, right in zip(original, current, strict=True)
            )
        )

    # A NaN read twice compares unequal in Python. Conversely, True, 1 and
    # 1.0 compare equal even though they are different TOML types.
    if isinstance(original, float) and isnan(original):
        return isinstance(current, float) and isnan(current)
    return type(original) is type(current) and original == current


class PreparedProjectInput:
    """Keep a derived declaration view attached to its original source files."""

    def __init__(
        self,
        path: Path,
        original: Mapping[str, Any],
        document: Mapping[str, Any],
        *,
        identity: Mapping[str, Any],
        local_inputs: Mapping[Path, str | None],
    ) -> None:
        """Own the declaration snapshot and the local files read to prepare it."""
        self.path = path.parent.resolve() / path.name
        self._original: Mapping[str, object] | bytes = copy.deepcopy(dict(original))
        self._document = copy.deepcopy(dict(document))
        self.identity = copy.deepcopy(dict(identity))
        self._local_inputs = dict(local_inputs)

    def read(self, path: Path) -> dict[str, Any]:
        """Refuse changed source data before returning an independent document."""
        if path.parent.resolve() / path.name != self.path:
            msg = f"prepared input belongs to {self.path}, not {path}"
            raise InputChangedError(msg)
        contents = self.path.read_bytes()
        original = self._original
        if contents != original:
            if isinstance(original, bytes):
                original = toml_io.loads(original.decode())
            if not _same_value(original, toml_io.loads(contents.decode())):
                msg = f"project inputs changed while locking: {self.path}"
                raise InputChangedError(msg)
            # Keep the verified file bytes so unchanged reads need no TOML parsing.
            self._original = contents

        for source, expected in self._local_inputs.items():
            try:
                actual = source.read_text(encoding="utf-8")
            except FileNotFoundError:
                actual = None
            if actual != expected:
                msg = f"project inputs changed while locking: {source}"
                raise InputChangedError(msg)
        return copy.deepcopy(self._document)
