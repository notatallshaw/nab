"""Pip-compatible ``requirements.txt`` rendering for a finished resolve.

Produces text that pip's hash-checking mode can install (with
``--hash=sha256:...`` lines on index pins) or the same text without
those lines.  Per-tuple resolves render as commented sections; pip
cannot install a single requirements.txt across multiple
``(python, platform)`` tuples in hash-checking mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from nab_index.atomic import atomic_write_text

from .builder import require_artifact_hashes

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

    An index pin is ``name==version`` followed by one ``--hash=<algo>:<digest>``
    per recorded digest, in the format pip's hash-checking mode accepts.  The
    hash lines are sorted, so the output does not depend on artefact order.
    Local and VCS pins are emitted as ``name @ <url>`` lines without hashes
    (pip does not hash-check those forms); an editable local pin renders as
    ``-e <url>`` and a ``subdirectory`` as a ``#subdirectory=`` fragment.  An
    archive pin is a third form, ``name @ <url>#sha256=...``, carrying its hash
    in the fragment; :func:`require_artifact_hashes` skips it because that hash
    is guaranteed at config parse.  Returns the text and, when ``output_path``
    is provided, atomically writes it.
    """
    require_artifact_hashes(lock_input)
    return _render_requirements(lock_input, with_hashes=True, output_path=output_path)


def write_requirements_without_hashes(
    lock_input: LockInput, *, output_path: str | os.PathLike[str] | None = None
) -> str:
    """Render ``lock_input`` without the ``--hash=sha256:...`` lines.

    Same shape as :func:`write_requirements_with_hashes`, so an index pin
    is a bare ``name==version``; local, VCS, and archive pins render the
    same in both variants.  Returns the text and, when ``output_path`` is
    provided, atomically writes it.
    """
    return _render_requirements(lock_input, with_hashes=False, output_path=output_path)


def _render_requirements(
    lock_input: LockInput,
    *,
    with_hashes: bool,
    output_path: str | os.PathLike[str] | None,
) -> str:
    """Render one target's pins flat, or several as labelled blocks.

    Pip cannot install a single requirements.txt across several
    ``(python, platform)`` targets in hash-checking mode, so a resolve
    that ran against more than one serialises as commented sections that
    callers are expected to extract per environment.  One target is one
    installable file, so it carries no section header.
    """
    targets = lock_input.targets
    if len(targets) > 1:
        blocks = [
            "\n".join(
                [
                    f"# {label}",
                    *_render_pins(targets[label].pins, with_hashes=with_hashes),
                ]
            )
            for label in sorted(targets)
        ]
        text = "\n\n".join(blocks) + "\n"
    else:
        pins = {
            name: pin for lock in targets.values() for name, pin in lock.pins.items()
        }
        text = "\n".join(_render_pins(pins, with_hashes=with_hashes)) + "\n"
    if output_path is not None:
        atomic_write_text(Path(output_path), text)
    return text


def _render_pins(pins: Mapping[str, PinShape], *, with_hashes: bool) -> list[str]:
    """Render a flat ``{name: pin}`` mapping in alphabetical order."""
    from ..lockfile import ArchivePin, IndexPin, LocalPin, VcsPin

    lines: list[str] = []
    for canonical in sorted(pins):
        pin = pins[canonical]

        if isinstance(pin, IndexPin):
            lines.extend(_render_index_pin(pin, with_hashes=with_hashes))
        elif isinstance(pin, LocalPin):
            url = Path(pin.path).resolve().as_uri()
            if pin.subdirectory is not None:
                url += f"#subdirectory={quote(pin.subdirectory, safe='/')}"
            if pin.editable:
                lines.append(f"-e {url}")
            else:
                lines.append(f"{pin.name} @ {url}")
        elif isinstance(pin, VcsPin):
            lines.append(f"{pin.name} @ {pin.repo_url}")
        elif isinstance(pin, ArchivePin):
            # The hash is the archive's identity, so carry it (and any
            # subdirectory) in the fragment for a reproducible, hash-checkable
            # install line, mirroring how VcsPin pins its commit in the URL.
            fragment = "&".join(f"{algo}={digest}" for algo, digest in pin.hashes)
            if pin.subdirectory is not None:
                fragment += f"&subdirectory={quote(pin.subdirectory, safe='/')}"
            lines.append(f"{pin.name} @ {pin.url}#{fragment}")
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
