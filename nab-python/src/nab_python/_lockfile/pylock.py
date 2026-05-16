"""PEP 751 ``pylock.toml`` emission.

Owns ``write_lock`` and ``build_pylock`` plus the
:class:`LockInput` -> :class:`Pylock` shape conversion.  The
per-tuple expansion path (when a universal resolve produced
different pins for different environments) collapses into one or
more ``Package`` entries with markers attached; the emit-time
disjointness validation lives in
:mod:`nab_python._lockfile.disjointness`.
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import tomli_w

from .._vendor.packaging.markers import Marker
from .._vendor.packaging.pylock import (
    Package,
    PackageDirectory,
    PackageSdist,
    PackageVcs,
    PackageWheel,
    Pylock,
)
from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.utils import canonicalize_name
from .._vendor.packaging.version import Version
from .disjointness import validate_marker_disjointness

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ..lockfile import (
        LockInput,
        PinShape,
        SdistArtifact,
        WheelArtifact,
    )


__all__ = [
    "build_pylock",
    "write_lock",
]


def write_lock(
    lock_input: LockInput,
    *,
    output_path: str | os.PathLike[str] | None = None,
) -> str:
    """Serialise ``lock_input`` to PEP 751 TOML text.

    Returns the TOML text.  When ``output_path`` is provided, also
    writes it there; the caller chooses the path (PEP 751 does not
    mandate one).

    Directory, wheel and sdist paths are written relative to
    ``output_path``'s parent so the lockfile stays portable between
    machines (PEP 751 records those paths relative to the lock file).
    With no ``output_path`` the current directory is the base.
    """
    lock_dir = Path(output_path).parent if output_path is not None else None
    pylock = build_pylock(lock_input, lock_dir=lock_dir)
    pylock.validate()
    text = tomli_w.dumps(dict(pylock.to_dict()))
    if output_path is not None:
        Path(output_path).write_text(text, encoding="utf-8")
    return text


def build_pylock(lock_input: LockInput, *, lock_dir: Path | None = None) -> Pylock:
    """Build a :class:`Pylock` from the input shape.

    The resolver-side data structures have already been simplified
    when this function runs.  The remaining work is shape conversion:
    ``Pin`` -> ``Package``, plus marker attachment from the per-tuple
    map.

    ``lock_dir`` is the directory the lockfile will be written to;
    local-directory, wheel and sdist paths are emitted relative to it
    so the lockfile is portable.  It defaults to the current working
    directory when the caller has no path in mind (e.g. stdout).
    """
    from ..lockfile import LOCK_VERSION

    base = (lock_dir if lock_dir is not None else Path.cwd()).resolve()
    if lock_input.per_tuple_pins:
        package_records = _build_per_tuple_packages(lock_input, base)
    else:
        package_records = [
            _pin_to_package(pin, lock_dir=base) for pin in lock_input.pins.values()
        ]
    package_records.sort(key=_package_sort_key)
    validate_marker_disjointness(
        package_records,
        environments=lock_input.tuple_environments,
        extras=lock_input.extras,
        groups=lock_input.dependency_groups,
    )
    tool: dict[str, Any] | None = (
        {"nab": lock_input.provenance.to_block()}
        if lock_input.provenance is not None
        else None
    )
    return Pylock(
        lock_version=Version(LOCK_VERSION),
        environments=tuple(lock_input.environments) or None,
        requires_python=(
            SpecifierSet(lock_input.requires_python)
            if lock_input.requires_python
            else None
        ),
        extras=(
            tuple(canonicalize_name(e) for e in lock_input.extras)
            if lock_input.extras
            else None
        ),
        dependency_groups=(
            tuple(lock_input.dependency_groups)
            if lock_input.dependency_groups
            else None
        ),
        default_groups=(
            tuple(lock_input.default_groups) if lock_input.default_groups else None
        ),
        created_by=lock_input.created_by,
        packages=package_records,
        tool=tool,
    )


def _relativize_path(target: str | os.PathLike[str], lock_dir: Path) -> str:
    """Return ``target`` as a POSIX path relative to ``lock_dir``.

    PEP 751 records ``packages.directory.path`` and the wheel/sdist
    ``path`` fields relative to the lock file so the lockfile stays
    portable between machines.  :func:`os.path.relpath` is used
    rather than :meth:`pathlib.Path.relative_to` so a ``target``
    outside ``lock_dir`` still resolves, to a ``../``-prefixed path.
    The result uses POSIX separators, which the spec recommends for
    portable relative paths.
    """
    return Path(os.path.relpath(target, lock_dir)).as_posix()


def _file_url_to_path(url: str) -> Path:
    """Return the filesystem path a ``file:`` URL points at.

    The inverse of :meth:`pathlib.Path.as_uri`, which nab-index uses
    to label wheels and sdists discovered in a local find-links
    directory.  A non-empty URL authority is kept as a leading
    ``//host`` component.
    """
    parts = urlsplit(url)
    raw = unquote(parts.path)
    if parts.netloc:
        raw = f"//{parts.netloc}{raw}"
    return Path(raw)


def _pin_to_package(
    pin: PinShape, marker: Marker | None = None, *, lock_dir: Path
) -> Package:
    from ..lockfile import IndexPin, LocalPin, VcsPin

    if isinstance(pin, IndexPin):
        return Package(
            name=canonicalize_name(pin.name),
            version=Version(pin.version),
            marker=marker,
            requires_python=(
                SpecifierSet(pin.requires_python) if pin.requires_python else None
            ),
            index=pin.index,
            sdist=(
                _sdist_to_package(pin.sdist, lock_dir=lock_dir) if pin.sdist else None
            ),
            wheels=tuple(_wheel_to_package(w, lock_dir=lock_dir) for w in pin.wheels)
            or None,
        )
    if isinstance(pin, LocalPin):
        # PEP 751: omit version for directory sources; it is not
        # deterministic (the source tree may change at install time).
        # The path is recorded relative to the lock file for portability.
        return Package(
            name=canonicalize_name(pin.name),
            version=None,
            marker=marker,
            directory=PackageDirectory(
                path=_relativize_path(pin.path, lock_dir),
                editable=pin.editable,
                subdirectory=pin.subdirectory,
            ),
        )
    if isinstance(pin, VcsPin):
        # PEP 751: omit version for VCS sources for the same reason.
        return Package(
            name=canonicalize_name(pin.name),
            version=None,
            marker=marker,
            vcs=PackageVcs(
                type="git",
                url=pin.repo_url,
                commit_id=pin.commit_id,
                subdirectory=pin.subdirectory,
                requested_revision=pin.requested_revision,
            ),
        )
    msg = f"unknown pin shape: {pin!r}"
    raise TypeError(msg)


def _wheel_to_package(wheel: WheelArtifact, *, lock_dir: Path) -> PackageWheel:
    """Convert a wheel artefact to its PEP 751 ``packages.wheels`` entry.

    A ``file:`` URL (a wheel from a local find-links directory) is
    rewritten to a ``path`` relative to the lock file so the lockfile
    stays portable; a remote ``url`` is recorded verbatim.
    """
    if wheel.url.startswith("file:"):
        return PackageWheel(
            name=wheel.filename,
            path=_relativize_path(_file_url_to_path(wheel.url).resolve(), lock_dir),
            size=wheel.size,
            hashes=dict(wheel.hashes),
            upload_time=wheel.upload_time,
        )
    return PackageWheel(
        name=wheel.filename,
        url=wheel.url,
        size=wheel.size,
        hashes=dict(wheel.hashes),
        upload_time=wheel.upload_time,
    )


def _sdist_to_package(sdist: SdistArtifact, *, lock_dir: Path) -> PackageSdist:
    """Convert an sdist artefact to its PEP 751 ``packages.sdist`` entry.

    See :func:`_wheel_to_package` for the ``file:`` URL handling.
    """
    if sdist.url.startswith("file:"):
        return PackageSdist(
            name=sdist.filename,
            path=_relativize_path(_file_url_to_path(sdist.url).resolve(), lock_dir),
            size=sdist.size,
            hashes=dict(sdist.hashes),
            upload_time=sdist.upload_time,
        )
    return PackageSdist(
        name=sdist.filename,
        url=sdist.url,
        size=sdist.size,
        hashes=dict(sdist.hashes),
        upload_time=sdist.upload_time,
    )


def _build_per_tuple_packages(lock_input: LockInput, lock_dir: Path) -> list[Package]:
    """Collapse per-tuple pins into Package entries with markers.

    For each canonical package name:
    * Group tuples by (version, source-shape).
    * Emit a Package per group; the marker is the OR of the matching
      tuples' markers.  When the group's tuples cover the entire
      declared universe (``lock_input.tuple_markers``) the package is
      unconditional and the marker is omitted.
    * Within a group, the artefact sets (wheels and sdist) are
      unioned across the contributing tuples so tuple-specific
      wheels (e.g. cp310-manylinux vs cp311-macos) survive.

    The emitted marker is the raw OR of the per-tuple marker
    expressions; no Boolean minimisation runs.
    """
    out: list[Package] = []
    by_name = _group_by_name(lock_input.per_tuple_pins)
    total_tuples = len(lock_input.tuple_markers)
    for per_tuple in by_name.values():
        groups = _group_pins_by_pin(per_tuple)
        for pins, tuple_labels in groups:
            marker = _build_marker(tuple_labels, lock_input.tuple_markers, total_tuples)
            out.append(
                _pin_to_package(_merge_pins_in_group(pins), marker, lock_dir=lock_dir)
            )
    # Pins only present in lock_input.pins (e.g. tuples agreed via the
    # single-source path) emit unconditionally.
    for canonical_name, pin in lock_input.pins.items():
        if canonical_name not in by_name:
            out.append(_pin_to_package(pin, lock_dir=lock_dir))
    return out


def _group_by_name(
    per_tuple_pins: Mapping[str, Mapping[str, PinShape]],
) -> dict[str, dict[str, PinShape]]:
    """Pivot ``{tuple_label: {name: pin}}`` to ``{canonical: {label: pin}}``."""
    out: defaultdict[str, dict[str, PinShape]] = defaultdict(dict)
    for tuple_label, per_name in per_tuple_pins.items():
        for raw_name, pin in per_name.items():
            canonical = canonicalize_name(raw_name)
            out[canonical][tuple_label] = pin
    return out


def _group_pins_by_pin(
    per_tuple: dict[str, PinShape],
) -> list[tuple[list[PinShape], list[str]]]:
    """Bucket tuples by structural pin discriminator, keeping every pin."""
    by_key: dict[tuple, tuple[list[PinShape], list[str]]] = {}
    for tuple_label, pin in per_tuple.items():
        key = _pin_discriminator(pin)
        if key not in by_key:
            by_key[key] = ([], [])
        by_key[key][0].append(pin)
        by_key[key][1].append(tuple_label)
    return list(by_key.values())


def _pin_discriminator(pin: PinShape) -> tuple:
    """Return a hashable key that identifies the source + version of ``pin``."""
    from ..lockfile import IndexPin, LocalPin, VcsPin

    if isinstance(pin, IndexPin):
        return ("index", pin.version, pin.index)
    if isinstance(pin, LocalPin):
        # editable and subdirectory change install behaviour, so two
        # otherwise-identical local pins differing only here are distinct.
        return (
            "local",
            pin.version,
            pin.path,
            pin.editable,
            pin.subdirectory or "",
        )
    if isinstance(pin, VcsPin):
        # requested_revision is informational; it does not affect the
        # checkout, so it is intentionally left out of the discriminator.
        return ("vcs", pin.commit_id, pin.repo_url, pin.subdirectory or "")
    msg = f"unknown pin shape: {pin!r}"
    raise TypeError(msg)


def _merge_pins_in_group(pins: list[PinShape]) -> PinShape:
    """Combine pins sharing a discriminator into one with unioned artefacts.

    For :class:`IndexPin`, accumulates every distinct wheel filename
    across the contributing tuples and keeps the first non-``None``
    sdist.  ``requires_python`` survives only when every tuple agreed,
    matching :func:`_common_requires_python`'s rule.
    Non-IndexPin shapes are already fully discriminated, so the first
    pin is returned unchanged.
    """
    from ..lockfile import IndexPin

    head = pins[0]
    if not isinstance(head, IndexPin):
        return head
    seen_wheels: dict[str, WheelArtifact] = {}
    sdist = head.sdist
    requires_python_set: set[str] = set()
    for pin in pins:
        assert isinstance(pin, IndexPin)
        for wheel in pin.wheels:
            seen_wheels.setdefault(wheel.filename, wheel)
        if sdist is None and pin.sdist is not None:
            sdist = pin.sdist
        if pin.requires_python is not None:
            requires_python_set.add(pin.requires_python)
    requires_python = (
        next(iter(requires_python_set)) if len(requires_python_set) == 1 else None
    )
    return IndexPin(
        name=head.name,
        version=head.version,
        index=head.index,
        sdist=sdist,
        wheels=tuple(seen_wheels.values()),
        requires_python=requires_python,
    )


def _build_marker(
    tuple_labels: Sequence[str],
    tuple_markers: Mapping[str, Marker],
    total_tuples: int,
) -> Marker | None:
    """Return the marker selecting ``tuple_labels``, or ``None`` if unconditional.

    The package is unconditional when ``tuple_labels`` covers every
    declared tuple in ``tuple_markers``.  Otherwise the marker is the
    OR of the per-tuple markers.  When ``tuple_markers`` is empty the
    caller has not declared a tuple universe and we omit the marker.
    """
    if total_tuples == 0 or len(tuple_labels) >= total_tuples:
        return None
    markers = [tuple_markers[label] for label in tuple_labels if label in tuple_markers]
    if not markers:
        return None
    return _or_markers(markers)


def _or_markers(markers: Sequence[Marker]) -> Marker:
    """Return a Marker that evaluates True if any of ``markers`` does."""
    if not markers:
        msg = "_or_markers requires at least one marker"
        raise ValueError(msg)
    if len(markers) == 1:
        return markers[0]
    parts = [f"({m})" for m in markers]
    return Marker(" or ".join(parts))


def _package_sort_key(package: Package) -> tuple:
    return (str(package.name), str(package.version) if package.version else "")
