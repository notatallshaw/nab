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
from ..config import conflict_exclusion_groups
from .disjointness import validate_marker_disjointness

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

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
        exclusive_groups=conflict_exclusion_groups(lock_input.conflicts),
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
            tuple(canonicalize_name(g) for g in lock_input.dependency_groups)
            if lock_input.dependency_groups
            else None
        ),
        default_groups=(
            tuple(canonicalize_name(g) for g in lock_input.default_groups)
            if lock_input.default_groups
            else None
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

    A Windows cross-drive ValueError falls back to the absolute path.
    """
    try:
        rel = os.path.relpath(target, lock_dir)
    except ValueError:
        rel = os.fspath(target)
    return Path(rel).as_posix()


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
            wheels=tuple(
                _wheel_to_package(w, lock_dir=lock_dir)
                for w in sorted(pin.wheels, key=_wheel_sort_key)
            )
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
        # vcs.url is the bare repository URL; the ref and subdirectory
        # travel in their own fields below.
        return Package(
            name=canonicalize_name(pin.name),
            version=None,
            marker=marker,
            vcs=PackageVcs(
                type=pin.vcs_type,
                url=pin.bare_repo_url,
                commit_id=pin.commit_id,
                subdirectory=pin.subdirectory,
                requested_revision=pin.requested_revision,
            ),
        )
    msg = f"unknown pin shape: {pin!r}"
    raise TypeError(msg)


def _wheel_to_package(wheel: WheelArtifact, *, lock_dir: Path) -> PackageWheel:
    """Convert a wheel artefact to its PEP 751 ``packages.wheels`` entry.

    A wheel from a local find-links directory carries its on-disk
    ``local_path``; it is written as a ``path`` relative to the lock
    file so the lockfile stays portable.  A remote wheel records its
    ``url`` verbatim.
    """
    if wheel.local_path is not None:
        return PackageWheel(
            name=wheel.filename,
            path=_relativize_path(wheel.local_path.resolve(), lock_dir),
            size=wheel.size,
            hashes=dict(sorted(wheel.hashes)),
            upload_time=wheel.upload_time,
        )
    return PackageWheel(
        name=wheel.filename,
        url=wheel.url,
        size=wheel.size,
        hashes=dict(sorted(wheel.hashes)),
        upload_time=wheel.upload_time,
    )


def _sdist_to_package(sdist: SdistArtifact, *, lock_dir: Path) -> PackageSdist:
    """Convert an sdist artefact to its PEP 751 ``packages.sdist`` entry.

    See :func:`_wheel_to_package` for the ``local_path`` handling.
    """
    if sdist.local_path is not None:
        return PackageSdist(
            name=sdist.filename,
            path=_relativize_path(sdist.local_path.resolve(), lock_dir),
            size=sdist.size,
            hashes=dict(sorted(sdist.hashes)),
            upload_time=sdist.upload_time,
        )
    return PackageSdist(
        name=sdist.filename,
        url=sdist.url,
        size=sdist.size,
        hashes=dict(sorted(sdist.hashes)),
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

    A conflict fork injects a membership clause into every tuple's
    ``tuple_markers`` entry, including the forks' base dependencies.
    A base dependency present in every fork of an environment must
    install regardless of which member is selected, so for that
    environment it contributes the membership-free env-only marker
    rather than the OR of the per-fork membership markers.  A package
    is recognised as a base dependency through
    ``lock_input.env_base_names``: a dep required by every member but
    not by the base is absent from that set, so it keeps the
    membership clause and does not install when no member is selected.
    """
    out: list[Package] = []
    by_name = _group_by_name(lock_input.per_tuple_pins)
    total_tuples = len(lock_input.tuple_markers)
    env_signatures = _env_signatures(
        lock_input.tuple_markers, lock_input.tuple_environments
    )
    env_fork_counts = _count(
        env_signatures[label] for label in lock_input.tuple_markers
    )
    for canonical_name, per_tuple in by_name.items():
        groups = _group_pins_by_pin(per_tuple)
        for pins, tuple_labels in groups:
            marker = _build_marker(
                canonical_name,
                tuple_labels,
                lock_input.tuple_markers,
                lock_input.tuple_env_markers,
                env_signatures,
                env_fork_counts,
                lock_input.env_base_names,
                total_tuples,
            )
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
    """Bucket tuples by pin discriminator, keeping every pin.

    Walked in sorted label order so the output is independent of dict
    insertion order.
    """
    by_key: dict[tuple, tuple[list[PinShape], list[str]]] = {}
    for tuple_label in sorted(per_tuple):
        pin = per_tuple[tuple_label]
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
        return (
            "vcs",
            pin.vcs_type,
            pin.commit_id,
            pin.repo_url,
            pin.subdirectory or "",
        )
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
        wheels=tuple(sorted(seen_wheels.values(), key=_wheel_sort_key)),
        requires_python=requires_python,
    )


def _build_marker(
    name: str,
    tuple_labels: Sequence[str],
    tuple_markers: Mapping[str, Marker],
    tuple_env_markers: Mapping[str, Marker],
    env_signatures: Mapping[str, tuple[tuple[str, str], ...]],
    env_fork_counts: Mapping[tuple[tuple[str, str], ...], int],
    env_base_names: Mapping[tuple[tuple[str, str], ...], frozenset[str]],
    total_tuples: int,
) -> Marker | None:
    """Return the marker selecting ``tuple_labels``, or ``None`` if unconditional.

    For each environment the package appears in, the contribution is
    the membership-free env-only marker when the package is present in
    every fork of that env AND ``env_base_names`` lists it as a base
    dep there; otherwise the contribution is the OR of the per-fork
    membership-carrying markers.  The result is the OR of those
    contributions, or ``None`` when the package covers every declared
    tuple AND every env collapsed to its env-only marker.  An empty
    ``tuple_markers`` means the caller has not declared a tuple
    universe and the marker is omitted.

    A dep required by every member of an ``at_most_one`` set but not
    by the base is absent from ``env_base_names``, so it keeps the
    membership OR and does not install when no member is selected.
    An environment with no base-name set (no conflict fork ran) leaves
    the gate open, so the no-conflict path emits markers byte for byte
    as before.
    """
    if total_tuples == 0:
        return None
    present = [label for label in tuple_labels if label in tuple_markers]
    if not present:
        return None
    by_env: defaultdict[tuple[tuple[str, str], ...], list[str]] = defaultdict(list)
    for label in present:
        by_env[env_signatures[label]].append(label)

    # The package is unconditional only when it covers every declared
    # tuple AND every environment collapsed to its env-only marker. A
    # member-only dep present in all forks of an env keeps the
    # membership OR, so it is not unconditional even at full coverage.
    contributions: list[Marker] = []
    unconditional = len(present) >= total_tuples
    for signature, labels in by_env.items():
        base_names = env_base_names.get(signature)
        is_base = base_names is None or name in base_names
        if len(labels) >= env_fork_counts[signature] and is_base:
            contributions.append(
                tuple_env_markers.get(labels[0], tuple_markers[labels[0]])
            )
        else:
            contributions.extend(tuple_markers[label] for label in labels)
            unconditional = False

    if unconditional:
        return None
    return _or_markers(contributions)


def _env_signatures(
    tuple_markers: Mapping[str, Marker],
    tuple_environments: Mapping[str, Mapping[str, str]],
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Map each tuple label to a hashable signature of its environment.

    Two labels share an environment (differ only by conflict-fork
    selection) when their environment dicts are equal, so the sorted
    items form a stable, hashable key.  A label without a declared
    environment falls back to a signature unique to itself, so it
    never groups with another label: the env-only marker collapses to
    the per-tuple marker and the no-conflict behaviour is preserved.
    """
    signatures: dict[str, tuple[tuple[str, str], ...]] = {}
    for label in tuple_markers:
        env = tuple_environments.get(label)
        signatures[label] = (
            tuple(sorted(env.items())) if env is not None else (("__label__", label),)
        )
    return signatures


def _count(
    signatures: Iterable[tuple[tuple[str, str], ...]],
) -> dict[tuple[tuple[str, str], ...], int]:
    """Count how many forks each environment signature spans."""
    counts: defaultdict[tuple[tuple[str, str], ...], int] = defaultdict(int)
    for signature in signatures:
        counts[signature] += 1
    return counts


def _or_markers(markers: Sequence[Marker]) -> Marker:
    """Return a Marker that evaluates True if any of ``markers`` does."""
    if not markers:
        msg = "_or_markers requires at least one marker"
        raise ValueError(msg)
    if len(markers) == 1:
        return markers[0]
    # str(Marker) does not canonicalise OR-clause order, so sort the
    # fragments by string to keep the joined marker order-independent.
    parts = [f"({m})" for m in sorted(markers, key=str)]
    return Marker(" or ".join(parts))


def _wheel_sort_key(wheel: WheelArtifact) -> tuple[str, str, str]:
    """Stable sort key for a wheel: filename, then url, then local path.

    Wheel tags are not used as a key; they parse to a ``frozenset``,
    which has no total order.
    """
    return (wheel.filename, wheel.url or "", str(wheel.local_path or ""))


def _package_sort_key(package: Package) -> tuple[str, str, str]:
    """Stable sort key for a package row: name, version, marker string.

    The marker tiebreak separates two rows that share a name and version
    but differ by environment marker, as a universal resolve emits.
    """
    return (
        str(package.name),
        str(package.version) if package.version else "",
        str(package.marker) if package.marker else "",
    )
