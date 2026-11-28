"""Operation-owned project declarations with change detection."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from . import toml_io
from .input_source import InputChangedError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = ["PreparedProjectInput"]


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
        self._original: Mapping[str, object] | bytes = toml_io.copy_table(original)
        self._document = toml_io.copy_table(document)
        self.identity = toml_io.copy_table(identity)
        self._local_inputs = dict(local_inputs)

    def with_identity(self, identity: Mapping[str, Any]) -> PreparedProjectInput:
        """Use another output identity with the same checked files and declarations."""
        prepared = copy(self)
        prepared.identity = toml_io.copy_table(identity)
        return prepared

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
            if not toml_io.values_match(original, toml_io.loads(contents.decode())):
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
        return toml_io.copy_table(self._document)
