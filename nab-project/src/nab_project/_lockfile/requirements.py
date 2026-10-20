"""Pip-compatible ``requirements.txt`` rendering for a finished resolve.

Produces text that pip's hash-checking mode can install (with
``--hash=sha256:...`` lines on index pins) or the same text without
those lines.  Per-tuple resolves render as commented sections; pip
cannot install a single requirements.txt across multiple
``(python, platform)`` tuples in hash-checking mode.
"""

from __future__ import annotations

__lazy_modules__ = (
    "nab_index.atomic",
    "nab_project._lockfile.builder",
    "pathlib",
    "urllib.parse",
)

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from nab_index.atomic import atomic_write_text

from .builder import MissingHashError, validate_artifact_hashes

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping

    from ..lockfile import IndexPin, LockInput, PinShape


__all__ = [
    "write_requirements_with_hashes",
    "write_requirements_without_hashes",
]

# Pip recognizes more algorithms in URL fragments than in --hash options.
_PIP_FRAGMENT_HASH_ALGORITHMS = ("sha256", "sha384", "sha512", "sha224", "sha1", "md5")


def write_requirements_with_hashes(
    lock_input: LockInput, *, output_path: str | os.PathLike[str] | None = None
) -> str:
    """Render ``lock_input`` as a pip-compatible requirements.txt.

    Index pins use ``name==version`` with sorted ``--hash=<algo>:<digest>`` lines.
    These options accept only recorded sha256, sha384, and sha512 digests.
    An index artefact without one raises :class:`MissingHashError`.

    Local and VCS pins use ``name @ <url>``; editable local pins use ``-e <url>``.
    Local subdirectories use a ``#subdirectory=`` fragment.
    Archive URL fragments carry pip-recognized hashes and optional subdirectories.
    An archive without a pip-recognized hash raises :class:`MissingHashError`.

    Return the text and atomically write it when ``output_path`` is provided.
    """
    _require_pip_hashes(lock_input)
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


def _pip_hashes(
    hashes: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Return hashes accepted by pip's hash-checking mode."""
    from ..lockfile import ACCEPTED_HASH_ALGORITHMS

    return tuple(pair for pair in hashes if pair[0] in ACCEPTED_HASH_ALGORITHMS)


def _require_pip_hashes(lock_input: LockInput) -> None:
    """Reject index artefacts without a pip-compatible hash."""
    from ..lockfile import ACCEPTED_HASH_ALGORITHMS, IndexPin

    for lock in lock_input.targets.values():
        for pin in lock.pins.values():
            if not isinstance(pin, IndexPin):
                continue
            artefacts = (*pin.wheels, *((pin.sdist,) if pin.sdist is not None else ()))
            for artefact in artefacts:
                if not _pip_hashes(artefact.hashes):
                    msg = (
                        f"{pin.name}: artefact {artefact.filename!r} has no "
                        f"acceptable hash (need one of "
                        f"{list(ACCEPTED_HASH_ALGORITHMS)!r})"
                    )
                    raise MissingHashError(msg)


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
            validate_artifact_hashes(pin.name, pin.hashes)
            hashes = tuple(
                pair for pair in pin.hashes if pair[0] in _PIP_FRAGMENT_HASH_ALGORITHMS
            )
            if not hashes:
                msg = f"{pin.name}: archive has no pip-recognized hash"
                raise MissingHashError(msg)
            fragment = "&".join(f"{algo}={digest}" for algo, digest in hashes)
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
        validate_artifact_hashes(pin.sdist.filename, pin.sdist.hashes)
        digests.extend(_pip_hashes(pin.sdist.hashes))
    for wheel in pin.wheels:
        validate_artifact_hashes(wheel.filename, wheel.hashes)
        digests.extend(_pip_hashes(wheel.hashes))
    if not digests:
        return [f"{pin.name}=={pin.version}"]
    parts = [f"{pin.name}=={pin.version}"]
    parts.extend(f"--hash={algo}:{d}" for algo, d in sorted(digests))
    return [" \\\n    ".join(parts)]
