"""Pip-compatible ``requirements.txt`` rendering for a finished resolve.

Produces text that pip's hash-checking mode can install (with
``--hash=sha256:...`` lines) or a plain ``name==version`` list when
hashes are not required.  Per-tuple resolves render as commented
sections; pip cannot install a single requirements.txt across
multiple ``(python, platform)`` tuples in hash-checking mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping

    from ..lockfile import IndexPin, LockInput, PinShape


__all__ = [
    "write_requirements_with_hashes",
    "write_requirements_without_hashes",
]


def write_requirements_with_hashes(
    lock_input: LockInput, *, output_path: str | os.PathLike[str] | None = None
) -> str:
    """Render ``lock_input`` as a pip-compatible requirements.txt.

    Each line is ``name==version`` followed by one ``--hash=sha256:...``
    per recorded artefact, in the format pip's hash-checking mode
    accepts.  Local and VCS pins are emitted as ``name @ <url>`` lines
    without hashes (pip does not hash-check those forms); an editable
    local pin renders as ``-e <url>`` and a ``subdirectory`` as a
    ``#subdirectory=`` fragment.  Returns the text and, when
    ``output_path`` is provided, atomically writes it.
    """
    return _render_requirements(lock_input, with_hashes=True, output_path=output_path)


def write_requirements_without_hashes(
    lock_input: LockInput, *, output_path: str | os.PathLike[str] | None = None
) -> str:
    """Render ``lock_input`` as a plain ``name==version`` list.

    Same shape as :func:`write_requirements_with_hashes` but without
    the ``--hash=sha256:...`` lines.  Local and VCS pins render the
    same in both variants.  Returns the text and, when ``output_path``
    is provided, atomically writes it.
    """
    return _render_requirements(lock_input, with_hashes=False, output_path=output_path)


def _render_requirements(
    lock_input: LockInput,
    *,
    with_hashes: bool,
    output_path: str | os.PathLike[str] | None,
) -> str:
    if lock_input.per_tuple_pins:
        text = _render_per_tuple_requirements(lock_input, with_hashes=with_hashes)
    else:
        lines = _render_pins(lock_input.pins, with_hashes=with_hashes)
        text = "\n".join(lines) + "\n"
    if output_path is not None:
        Path(output_path).write_text(text, encoding="utf-8")
    return text


def _render_per_tuple_requirements(lock_input: LockInput, *, with_hashes: bool) -> str:
    """Emit one ``# label`` block per tuple in sorted label order.

    Each block is followed by that tuple's pins.  Pip cannot install a
    single requirements.txt across multiple ``(python, platform)``
    tuples in hash-checking mode, so a multi-tuple resolve serialises as
    commented sections that callers are expected to extract per
    environment.
    """
    blocks: list[str] = []
    for label in sorted(lock_input.per_tuple_pins):
        pins = lock_input.per_tuple_pins[label]
        block = [f"# {label}"]
        block.extend(_render_pins(pins, with_hashes=with_hashes))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks) + "\n"


def _render_pins(pins: Mapping[str, PinShape], *, with_hashes: bool) -> list[str]:
    """Render a flat ``{name: pin}`` mapping in alphabetical order."""
    from ..lockfile import IndexPin, LocalPin, VcsPin

    lines: list[str] = []
    for canonical in sorted(pins):
        pin = pins[canonical]

        if isinstance(pin, IndexPin):
            lines.extend(_render_index_pin(pin, with_hashes=with_hashes))
        elif isinstance(pin, LocalPin):
            url = Path(pin.path).resolve().as_uri()
            if pin.subdirectory is not None:
                url += f"#subdirectory={pin.subdirectory}"
            if pin.editable:
                lines.append(f"-e {url}")
            else:
                lines.append(f"{pin.name} @ {url}")
        elif isinstance(pin, VcsPin):
            lines.append(f"{pin.name} @ {pin.repo_url}")
        else:  # pragma: no cover - exhaustive
            msg = f"unknown pin shape: {pin!r}"
            raise TypeError(msg)

    return lines


def _render_index_pin(pin: IndexPin, *, with_hashes: bool = True) -> list[str]:
    if not with_hashes:
        return [f"{pin.name}=={pin.version}"]
    digests: list[tuple[str, str]] = []
    if pin.sdist is not None:
        digests.extend(pin.sdist.hashes)
    for wheel in pin.wheels:
        digests.extend(wheel.hashes)
    if not digests:
        return [f"{pin.name}=={pin.version}"]
    parts = [f"{pin.name}=={pin.version}"]
    parts.extend(f"--hash={algo}:{d}" for algo, d in sorted(digests))
    return [" \\\n    ".join(parts)]
