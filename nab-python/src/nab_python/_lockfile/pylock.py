"""PEP 751 ``pylock.toml`` emission.

Owns ``write_lock`` and ``build_pylock`` plus the
:class:`LockInput` -> :class:`Pylock` shape conversion.  The targets a
resolve ran against collapse into one or more ``Package`` entries per
name, with a marker attached when they disagree; the emit-time
disjointness validation lives in
:mod:`nab_python._lockfile.disjointness`.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from functools import reduce
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

import tomli_w

from nab_index.atomic import atomic_write_text

from .._conflict_kind import KIND_GROUP, MARKER_VARIABLE_FOR_KIND
from .._vendor.packaging.markers import Marker
from .._vendor.packaging.markersets import (
    DecisionStore,
    IntractableMarkerSet,
    MarkerSet,
    UnserializableMarkerSet,
)
from .._vendor.packaging.pylock import (
    Package,
    PackageArchive,
    PackageDirectory,
    PackageSdist,
    PackageVcs,
    PackageWheel,
    Pylock,
)
from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.utils import canonicalize_name
from .._vendor.packaging.version import Version
from ..config import conflict_exclusion_groups, conflict_member_groups
from .builder import require_artifact_hashes
from .coverage import validate_marker_coverage
from .disjointness import validate_marker_disjointness
from .groups import BASE_MEMBER

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from collections.abc import Set as AbstractSet

    from ..lockfile import (
        LockInput,
        PinShape,
        SdistArtifact,
        TargetLock,
        WheelArtifact,
    )
    from ..target import ResolveTarget


__all__ = [
    "DivergentBaseDependencyError",
    "UnsoundSimplificationError",
    "build_pylock",
    "write_lock",
]

# A hashable environment (see _env_signatures), a set of (kind, name)
# selection members, and the key a fork is indexed under.
_EnvSignature: TypeAlias = "tuple[tuple[str, str], ...]"
_Members: TypeAlias = "frozenset[tuple[str, str]]"
_ForkKey: TypeAlias = "tuple[_EnvSignature, _Members]"

_NO_MEMBERS: _Members = frozenset()


@dataclass(frozen=True, slots=True)
class _ForkAxes:
    """The declared conflict sets a lock's forks vary along.

    ``exclusion_groups`` are the ``(kind, name)`` member sets an install
    context may activate at most one member of, restricted to the members
    the resolve forked over (:func:`_forked_exclusion_groups`).  ``forks``
    indexes every target by its environment and its selection, which is how
    :func:`_project_fork` walks one set's members with the other sets
    held fixed.  ``markers`` is each fork's unprojected selection marker,
    which does not vary by package, and ``gates`` each fork's
    :attr:`~nab_python.lockfile.TargetLock.package_gates` as sets.
    """

    exclusion_groups: tuple[AbstractSet[tuple[str, str]], ...]
    forks: Mapping[_ForkKey, str]
    targets: Mapping[str, TargetLock]
    env_signatures: Mapping[str, _EnvSignature]
    markers: Mapping[str, str]
    gates: Mapping[tuple[str, str], _Members]


@dataclass(frozen=True, slots=True)
class _GatedMarker:
    """The environments one gate selects a package in.

    ``marker`` is ``None`` when the gate holds on all of them.
    """

    marker: Marker | None
    gate: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _Projection:
    """One package's marker shape at one fork.

    ``dropped`` are the selection members whose clauses fall away, and
    ``gate`` is the membership gate that stands in for the fork's own
    once they do (:func:`_merged_fork_gate`).
    """

    dropped: _Members
    gate: _Members


def _fork_index(
    targets: Mapping[str, TargetLock],
    env_signatures: Mapping[str, _EnvSignature],
) -> dict[_ForkKey, str]:
    """Index every target by its environment and its selection.

    A conflict-forked environment holds one target per point of the
    cartesian product across the engaged sets, so this is the lookup that
    answers "which fork agrees with this one everywhere but here".
    """
    return {
        (env_signatures[label], frozenset(lock.target.selection)): label
        for label, lock in targets.items()
    }


def _forked_exclusion_groups(
    exclusion_groups: Sequence[AbstractSet[tuple[str, str]]],
    targets: Mapping[str, TargetLock],
) -> tuple[AbstractSet[tuple[str, str]], ...]:
    """Restrict each declared conflict set to the members a fork selects.

    A set forks only over the members the run selected, so a declared
    member the selection omits has no fork for :func:`_project_fork` to
    swap in and no place in the lock's ``extras`` or ``dependency-groups``
    arrays for an install context to activate.
    """
    forked = frozenset(
        member for lock in targets.values() for member in lock.target.selection
    )
    return tuple(group & forked for group in exclusion_groups)


class UnsoundSimplificationError(ValueError):
    """A simplified per-package marker disagrees with the original over the universe.

    Checked on the serialised and reparsed bytes at emission, so a mismatch is a
    bug in the algebra and emission refuses rather than ship a marker that would
    install differently.
    """


class DivergentBaseDependencyError(ValueError):
    """An environment's conflict forks disagree on a base dependency's pin.

    A base dependency present in every fork of an environment drops its
    membership clause so it installs even when no conflicting member is
    selected, which requires the forks to agree on one (version, source).
    When they diverge, every candidate entry keeps a membership clause,
    so nothing would fire in the no-member install context and the
    dependency would silently not install.  Surface the divergence with
    the offending package and per-fork pins so the producer can
    reconcile the forks rather than commit an incomplete lock.
    """


def write_lock(
    lock_input: LockInput,
    *,
    output_path: str | os.PathLike[str] | None = None,
) -> str:
    """Serialise ``lock_input`` to PEP 751 TOML text.

    Returns the TOML text.  When ``output_path`` is provided, also
    writes it there; the caller chooses the path (PEP 751 does not
    mandate one).  The write is staged and renamed into place, so a
    failed write leaves any existing file intact.

    Directory, wheel and sdist paths are written relative to
    ``output_path``'s parent so the lockfile stays portable between
    machines (PEP 751 records those paths relative to the lock file).
    With no ``output_path`` the current directory is the base.
    """
    lock_dir = Path(output_path).parent if output_path is not None else None
    text = render_lock(lock_input, lock_dir=lock_dir)
    if output_path is not None:
        atomic_write_text(Path(output_path), text)
    return text


def render_lock(lock_input: LockInput, *, lock_dir: Path | None = None) -> str:
    """Serialise ``lock_input`` to PEP 751 TOML text without writing a file.

    ``lock_dir`` is the directory the lock will live in; directory, wheel
    and sdist paths are emitted relative to it so the lock stays portable.
    Defaults to the current directory.  Used by ``write_lock`` and by
    ``nab lock --locked`` to render the would-be lock for comparison.
    """
    require_artifact_hashes(lock_input)
    pylock = build_pylock(lock_input, lock_dir=lock_dir)
    pylock.validate()
    return tomli_w.dumps(dict(pylock.to_dict()))


def build_pylock(lock_input: LockInput, *, lock_dir: Path | None = None) -> Pylock:
    """Build a :class:`Pylock` from the input shape.

    The resolver-side data structures have already been simplified
    when this function runs.  The remaining work is shape conversion:
    ``Pin`` -> ``Package``, plus marker attachment from the per-target
    map.

    ``lock_dir`` is the directory the lockfile will be written to;
    local-directory, wheel and sdist paths are emitted relative to it
    so the lockfile is portable.  It defaults to the current working
    directory when the caller has no path in mind (e.g. stdout).
    """
    from ..lockfile import LOCK_VERSION

    lock_input = _name_base_group(lock_input)

    base = (lock_dir if lock_dir is not None else Path.cwd()).resolve()
    exclusion_groups = conflict_exclusion_groups(lock_input.conflicts)
    universe = _emission_universe(lock_input)
    store = DecisionStore()
    package_records = _build_packages(
        lock_input, base, exclusion_groups, universe, store
    )
    package_records.sort(key=_package_sort_key)
    validate_marker_disjointness(
        package_records,
        environments=lock_input.marker_envs,
        extras=lock_input.extras,
        groups=lock_input.active_groups,
        exclusive_groups=exclusion_groups,
        declared_groups=conflict_member_groups(lock_input.conflicts),
    )
    validate_marker_coverage(
        [lock.target for lock in lock_input.targets.values()],
        environments=lock_input.environments,
        store=store,
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
        dependency_groups=_group_array(
            lock_input.dependency_groups,
            lock_input.base_group,
            lock_input.build_group,
        ),
        default_groups=_group_array(
            lock_input.default_groups,
            lock_input.base_group if not lock_input.default_groups else None,
        ),
        created_by=lock_input.created_by,
        packages=package_records,
        tool=tool,
    )


def _name_base_group(lock_input: LockInput) -> LockInput:
    """Return ``lock_input`` with the base member named, or cut from every gate.

    The builder records :data:`~nab_python.lockfile.BASE_MEMBER` on every
    package the project's own dependencies reach.  With ``base-group``
    unset there is no name to give it, so those packages lose their gate
    and stay unconditional.
    """
    name = lock_input.base_group
    member = (KIND_GROUP, name)
    targets = {
        label: replace(
            lock,
            package_gates={
                gated: tuple(
                    member if gate_member == BASE_MEMBER else gate_member
                    for gate_member in gate
                )
                for gated, gate in lock.package_gates.items()
                if name is not None or BASE_MEMBER not in gate
            },
        )
        for label, lock in lock_input.targets.items()
    }
    return replace(lock_input, targets=targets)


def _group_array(
    groups: Sequence[str], *configured: str | None
) -> tuple[str, ...] | None:
    """Render one of the lock's group arrays, or ``None`` when it is empty.

    Every name is canonicalized here and deduplicated in order.  Which
    arrays a ``configured`` name belongs in is the caller's decision:
    ``build-group`` joins ``dependency-groups`` alone, and ``base-group``
    joins it too, since an installer that offers only those names would
    otherwise never reach it.  ``base-group`` joins ``default-groups``
    only when the project declares none of its own, because a declared
    ``default-groups`` replaces the default selection rather than
    extending it.
    """
    names = dict.fromkeys(str(canonicalize_name(group)) for group in groups)
    for name in configured:
        if name is not None:
            names[str(canonicalize_name(name))] = None
    return tuple(names) or None


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


def _dependency_entries(
    name: str, labels: Iterable[str], targets: Mapping[str, TargetLock]
) -> list[dict[str, str]] | None:
    """Render a package's forward edges as PEP 751 dependency tables.

    One entry covers the targets in ``labels``, which may disagree on
    what ``name`` depends on (a dep gated by a marker one target answers
    differently), so the edges are the union over those targets.  Each
    target's edges already point only at packages it locked, so every
    edge names a package the lock carries.
    """
    deps: set[str] = set()
    for label in labels:
        deps.update(targets[label].dependencies.get(name, ()))
    if not deps:
        return None
    return [{"name": dep} for dep in sorted(deps)]


def _pin_to_package(
    pin: PinShape,
    marker: Marker | None = None,
    *,
    lock_dir: Path,
    dependencies: list[dict[str, str]] | None = None,
) -> Package:
    from ..lockfile import ArchivePin, IndexPin, LocalPin, VcsPin

    if isinstance(pin, IndexPin):
        return Package(
            name=canonicalize_name(pin.name),
            version=Version(pin.version),
            marker=marker,
            dependencies=dependencies,
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
            dependencies=dependencies,
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
            dependencies=dependencies,
            vcs=PackageVcs(
                type=pin.vcs_type,
                url=pin.bare_repo_url,
                commit_id=pin.commit_id,
                subdirectory=pin.subdirectory,
                requested_revision=pin.requested_revision,
            ),
        )
    if isinstance(pin, ArchivePin):
        # An archive is content-pinned by its hash, so the version is
        # stable and recorded (unlike the directory/VCS cases above).
        return Package(
            name=canonicalize_name(pin.name),
            version=Version(pin.version),
            marker=marker,
            dependencies=dependencies,
            archive=PackageArchive(
                url=pin.url,
                hashes=dict(pin.hashes),
                subdirectory=pin.subdirectory,
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


def _emission_universe(lock_input: LockInput) -> MarkerSet:
    """Return the environment universe simplification must agree over.

    The union of the declared ``environments`` rows, or the full set when none
    are declared.  PEP 751 step 4 has a conforming installer read a per-package
    marker only in an environment some row admits, so within-universe
    equivalence is the sound contract.

    A union admitting no environment makes every set vacuously equivalent to
    every other, so the full set stands in for it: a simplification sound in
    every environment is sound inside an empty one too.  A union is empty
    exactly when every row is, so rows are tested one at a time rather than as a
    whole-matrix product, and a row too wide to decide counts as inhabited.
    """
    if not lock_input.environments:
        return MarkerSet.full()
    rows = [MarkerSet.from_marker(m) for m in lock_input.environments]
    try:
        uninhabited = all(row.is_empty() for row in rows)
    except IntractableMarkerSet:
        uninhabited = False
    if uninhabited:
        return MarkerSet.full()
    return reduce(MarkerSet.union, rows, MarkerSet.empty())


def _finalize_marker(
    raw: Marker | None,
    within: MarkerSet,
    name: str = "",
    store: DecisionStore | None = None,
) -> Marker | None:
    """Return ``raw`` in its shortest form equivalent over ``within``.

    ``None`` passes through, and a set full over the universe serialises back to
    ``None``.  The simplified set is serialised, reparsed, and checked equivalent
    to ``raw`` over ``within``; a mismatch raises
    :class:`UnsoundSimplificationError`.

    Simplification is only a compaction, so every way the algebra can decline to
    answer ships ``raw`` instead: :class:`IntractableMarkerSet` when a decision
    overruns the cell budget or the marker nests past the stack, and
    :class:`UnserializableMarkerSet` when the simplified set has no marker
    spelling, which is what a marker selecting nothing inside ``within``
    collapses to.
    """
    if raw is None:
        return None
    try:
        simplified = MarkerSet.from_marker(raw).simplify(within=within, store=store)
        text = simplified.to_marker_string()
        rebuilt = None if text is None else Marker(text)
        emitted = (
            MarkerSet.full() if rebuilt is None else MarkerSet.from_marker(rebuilt)
        )
        shown = "no marker" if rebuilt is None else str(rebuilt)
        sound = _sound_within_universe(raw, emitted, within, store)
    except (IntractableMarkerSet, UnserializableMarkerSet):
        return raw
    if not sound:
        msg = (
            f"{name}: emitted marker {shown!r} is not equivalent to"
            f" {str(raw)!r} over the declared environments"
        )
        raise UnsoundSimplificationError(msg)
    return rebuilt


def _finalize_cached(
    raw: Marker | None,
    within: MarkerSet,
    name: str,
    memo: dict[str, Marker | None],
    store: DecisionStore,
) -> Marker | None:
    """:func:`_finalize_marker` memoised for the span of one lock.

    ``within`` is fixed for a whole build, so two packages carrying the same raw
    marker have the same shortest form, and a universal lock repeats one
    platform or python gate across many packages.
    """
    if raw is None:
        return None
    key = str(raw)
    if key not in memo:
        memo[key] = _finalize_marker(raw, within, name, store)
    return memo[key]


def _sound_within_universe(
    raw: Marker,
    emitted: MarkerSet,
    within: MarkerSet,
    store: DecisionStore | None = None,
) -> bool:
    """Whether ``emitted`` and ``raw`` agree on every environment in ``within``.

    ``emitted`` is what the lock ships: the reparsed marker bytes, or
    :meth:`MarkerSet.full` when no marker field is emitted.  Decided per universe
    row, under the same budget as the operator it checks.
    """
    return MarkerSet.from_marker(raw).equivalent_within(emitted, within, store=store)


def _build_packages(
    lock_input: LockInput,
    lock_dir: Path,
    exclusion_groups: Sequence[AbstractSet[tuple[str, str]]],
    universe: MarkerSet,
    store: DecisionStore,
) -> list[Package]:
    """Collapse the per-target pins into Package entries with markers.

    For each canonical package name:
    * Group targets by (version, source-shape).
    * Emit a Package per group; the marker is the OR of the matching
      targets' markers.  When the group's targets cover every target
      the resolve ran against, the package is unconditional and the
      marker is omitted, which is every package of a lock with one
      target.
    * Within a group, the artefact sets (wheels and sdist) are
      unioned across the contributing targets so target-specific
      wheels (e.g. cp310-manylinux vs cp311-macos) survive.

    Each emitted marker is finalised to its shortest form equivalent over
    the declared environments (:func:`_finalize_marker`).

    A package carries the gate of every install context that reaches it,
    which with ``[tool.nab].base-group`` set includes the project's own
    dependencies.  See :func:`_build_marker`.

    A conflict fork injects a membership clause into every target's
    marker, including the forks' base dependencies.  A base dependency
    present in every fork of an environment must install regardless of
    which member is selected, so for that environment it contributes the
    membership-free env-only marker rather than the OR of the per-fork
    membership markers.  A package is recognised as a base dependency
    through ``lock_input.env_base_names``: a dep required by every member
    but not by the base is absent from that set, so it keeps the
    membership clause and does not install when no member is selected.
    See :class:`LockInput.env_base_names` for the missing-signature
    contract.  The base-name set is first closed under the emitted
    dependency edges (:func:`_close_base_names`) so a transitive dep the
    forks pull in through a base dep counts as base too.  Forks of one
    environment that disagree on a base dependency's pin raise
    :class:`DivergentBaseDependencyError` instead of emitting a lock
    whose no-member context misses it.
    """
    out: list[Package] = []
    targets = lock_input.targets
    by_name = _group_by_name(targets)
    env_signatures = _env_signatures(targets)
    env_fork_counts = _count(env_signatures.values())
    base_names = _close_base_names(
        targets, env_signatures, env_fork_counts, lock_input.env_base_names
    )
    pin_groups = {
        canonical_name: _group_pins_by_pin(per_target)
        for canonical_name, per_target in by_name.items()
    }
    shortened: dict[str, Marker | None] = {}
    forked_groups = _forked_exclusion_groups(exclusion_groups, targets)
    axes = _ForkAxes(
        exclusion_groups=forked_groups,
        forks=_fork_index(targets, env_signatures),
        targets=targets,
        env_signatures=env_signatures,
        # One selection marker per label; it does not vary by package.
        markers={
            label: _selection_marker(lock.target, forked_groups)
            for label, lock in targets.items()
        },
        gates={
            (name, label): frozenset(gate)
            for label, lock in targets.items()
            for name, gate in lock.package_gates.items()
        },
    )
    projections = _fork_projections(axes, pin_groups, env_fork_counts, base_names)

    for canonical_name, groups in pin_groups.items():
        _check_base_fork_agreement(
            canonical_name,
            by_name[canonical_name],
            groups,
            env_signatures,
            env_fork_counts,
            base_names,
        )

        for pins, labels in groups:
            parts = _build_marker(
                canonical_name,
                labels,
                env_fork_counts,
                base_names,
                axes,
                projections,
            )
            marker = _finalize_parts(parts, universe, canonical_name, shortened, store)
            out.append(
                _pin_to_package(
                    _merge_pins_in_group(pins),
                    marker,
                    lock_dir=lock_dir,
                    dependencies=_dependency_entries(canonical_name, labels, targets),
                )
            )

    return out


def _group_by_name(
    targets: Mapping[str, TargetLock],
) -> dict[str, dict[str, PinShape]]:
    """Pivot ``{label: TargetLock}`` to ``{canonical name: {label: pin}}``."""
    out: defaultdict[str, dict[str, PinShape]] = defaultdict(dict)
    for label, lock in targets.items():
        for raw_name, pin in lock.pins.items():
            out[canonicalize_name(raw_name)][label] = pin
    return out


def _group_pins_by_pin(
    per_target: dict[str, PinShape],
) -> list[tuple[list[PinShape], list[str]]]:
    """Bucket targets by pin discriminator, keeping every pin.

    Walked in sorted label order so the output is independent of dict
    insertion order.
    """
    by_key: dict[tuple, tuple[list[PinShape], list[str]]] = {}
    for label in sorted(per_target):
        pin = per_target[label]
        key = _pin_discriminator(pin)
        if key not in by_key:
            by_key[key] = ([], [])
        by_key[key][0].append(pin)
        by_key[key][1].append(label)
    return list(by_key.values())


def _pin_discriminator(pin: PinShape) -> tuple:
    """Return a hashable key that identifies the source + version of ``pin``."""
    from ..lockfile import ArchivePin, IndexPin, LocalPin, VcsPin

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
    if isinstance(pin, ArchivePin):
        return ("archive", pin.version, pin.url, pin.hashes, pin.subdirectory or "")
    msg = f"unknown pin shape: {pin!r}"
    raise TypeError(msg)


def _merge_pins_in_group(pins: list[PinShape]) -> PinShape:
    """Combine pins sharing a discriminator into one with unioned artefacts.

    For :class:`IndexPin`, accumulates every distinct wheel filename
    across the contributing targets and keeps the first non-``None``
    sdist.  ``requires_python`` survives only when every target carried
    the same value and none was unconstrained, matching
    :func:`_common_requires_python`'s rule.
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
    any_unconstrained = False
    for pin in pins:
        assert isinstance(pin, IndexPin)
        for wheel in pin.wheels:
            seen_wheels.setdefault(wheel.filename, wheel)
        if sdist is None and pin.sdist is not None:
            sdist = pin.sdist
        if pin.requires_python is None:
            any_unconstrained = True
        else:
            requires_python_set.add(pin.requires_python)
    requires_python = (
        next(iter(requires_python_set))
        if not any_unconstrained and len(requires_python_set) == 1
        else None
    )
    return IndexPin(
        name=head.name,
        version=head.version,
        index=head.index,
        sdist=sdist,
        wheels=tuple(sorted(seen_wheels.values(), key=_wheel_sort_key)),
        requires_python=requires_python,
    )


def _close_base_names(
    targets: Mapping[str, TargetLock],
    env_signatures: Mapping[str, tuple[tuple[str, str], ...]],
    env_fork_counts: Mapping[tuple[tuple[str, str], ...], int],
    env_base_names: Mapping[tuple[tuple[str, str], ...], frozenset[str]],
) -> dict[tuple[tuple[str, str], ...], frozenset[str]]:
    """Close each env's base-name set under the emitted dependency edges.

    ``env_base_names`` records the names an independent base pass
    resolved, at that pass's versions, which emission discards in favour
    of the conflict-fork versions.  When the forks pin a base dependency
    lower than the base pass did, its emitted transitive closure differs:
    a package the forks pull in through the base dep is missing from
    ``env_base_names`` and would keep its membership clause, so it drops
    out of the no-member install context even though the base dep that
    requires it installs there.  Following the base (unconditional) edges
    present in every fork of the environment restores the closure, keeping
    the no-member context closed under the lock's own dependency graph.

    Preserves the missing-signature contract: only signatures present in
    ``env_base_names`` appear in the result, so an env with no base pass
    stays absent and its base status stays unknowable.
    """
    if not env_base_names:
        return dict(env_base_names)

    shared = _shared_fork_edges(targets, env_signatures, env_fork_counts)
    closed: dict[tuple[tuple[str, str], ...], frozenset[str]] = {}
    for signature, base in env_base_names.items():
        adjacency = shared.get(signature, {})
        reachable = set(base)
        stack = list(base)
        while stack:
            for dep in adjacency.get(stack.pop(), ()):
                if dep not in reachable:
                    reachable.add(dep)
                    stack.append(dep)
        closed[signature] = frozenset(reachable)
    return closed


def _shared_fork_edges(
    targets: Mapping[str, TargetLock],
    env_signatures: Mapping[str, tuple[tuple[str, str], ...]],
    env_fork_counts: Mapping[tuple[tuple[str, str], ...], int],
) -> dict[tuple[tuple[str, str], ...], dict[str, list[str]]]:
    """Per-env adjacency of the base edges present in every fork of the env.

    Walks ``base_dependencies`` (a package's unconditional edges), never
    the extra-folded ``dependencies``, so a dep a conflict member pulls in
    through an extra is not an edge here even when every fork activates
    that extra.  A base edge only some forks carry (a base dep pinned to a
    version whose deps differ) is dropped too.  Following these edges thus
    never promotes a member-only dep to base.
    """
    edge_counts: defaultdict[tuple[tuple[str, str], ...], Counter[tuple[str, str]]] = (
        defaultdict(Counter)
    )
    for label, lock in targets.items():
        counter = edge_counts[env_signatures[label]]
        for source, deps in lock.base_dependencies.items():
            for dep in dict.fromkeys(deps):
                counter[source, dep] += 1

    shared: dict[tuple[tuple[str, str], ...], dict[str, list[str]]] = {}
    for signature, counter in edge_counts.items():
        count = env_fork_counts[signature]
        adjacency: defaultdict[str, list[str]] = defaultdict(list)
        for (source, dep), seen in counter.items():
            if seen == count:
                adjacency[source].append(dep)
        shared[signature] = adjacency
    return shared


def _check_base_fork_agreement(
    name: str,
    per_target: Mapping[str, PinShape],
    groups: list[tuple[list[PinShape], list[str]]],
    env_signatures: Mapping[str, tuple[tuple[str, str], ...]],
    env_fork_counts: Mapping[tuple[tuple[str, str], ...], int],
    env_base_names: Mapping[tuple[tuple[str, str], ...], frozenset[str]],
) -> None:
    """Reject a base dep whose forks within one env pin it differently.

    The env-only collapse in :func:`_build_marker` needs a single
    (version, source) group spanning every fork of the environment.
    Divergent pins split the forks across groups, so every entry would
    keep its membership clause and none would fire when no member is
    selected: the base dependency would silently not install.
    """
    by_env: defaultdict[tuple[tuple[str, str], ...], list[str]] = defaultdict(list)
    for label in sorted(per_target):
        by_env[env_signatures[label]].append(label)
    for signature, labels in by_env.items():
        if len(labels) < env_fork_counts[signature]:
            continue
        if name not in env_base_names.get(signature, frozenset()):
            continue
        in_env = set(labels)
        widest = max(
            sum(1 for label in group_labels if label in in_env)
            for _, group_labels in groups
        )
        if widest >= env_fork_counts[signature]:
            continue
        forks = ", ".join(f"{label} -> {per_target[label].version}" for label in labels)
        msg = (
            f"{name}: the conflict forks of one environment pin this base"
            f" dependency differently ({forks}); no lockfile entry would"
            " install it when no conflicting member is selected"
        )
        raise DivergentBaseDependencyError(msg)


def _build_marker(
    name: str,
    labels: Sequence[str],
    env_fork_counts: Mapping[tuple[tuple[str, str], ...], int],
    env_base_names: Mapping[tuple[tuple[str, str], ...], frozenset[str]],
    axes: _ForkAxes,
    projections: Mapping[tuple[str, str], _Projection],
) -> tuple[_GatedMarker, ...]:
    """Return the marker selecting ``labels``, split by gate.

    For each environment the package appears in, the contribution is
    the membership-free env-only marker when the package is present in
    every fork of that env AND ``env_base_names`` lists it as a base
    dep there; otherwise the contribution is the OR of the per-fork
    membership-carrying markers.  The result is the OR of those
    contributions, or ``None`` when the package covers every target
    AND every env collapsed to its env-only marker, which is every
    package of a single-target lock.

    A dep required by every member of an ``at-most-one`` set but not
    by the base is absent from ``env_base_names``, so it keeps the
    membership OR and does not install when no member is selected.
    An environment with no base-name set (no conflict fork ran) leaves
    the gate open.

    A membership contribution carries the fork's positive selection AND
    the negation of every co-member of the conflict sets it selects from
    (``"cpu" in extras and "gpu" not in extras``), so the forks are
    mutually exclusive in the marker itself: a PEP 751 consumer that never
    reads ``[tool.nab].conflicts`` still installs at most one fork.  See
    :func:`_selection_marker`.

    The selection is projected per package first: a conflict set the
    package does not vary over contributes no clause, so with two sets
    engaged a dep reached through one of them names only that one and
    still installs for a selection that leaves the other set empty.  See
    :func:`_fork_projections`.  The projection makes several forks render
    the same contribution, which is emitted once.

    A package carries the gate of every install context that reaches it
    (see :attr:`~nab_python.lockfile.TargetLock.package_gates`), joined
    by ``and`` onto each contribution.  A fork whose gate names
    its own selection needs no gate: its marker already asserts that
    member.  An env collapses only when its forks agree on the rest of
    the gate, and the collapsed entry carries their union, so a package
    one fork reaches through its own member and another through a
    non-conflicting extra installs for either.  When the package covers
    every target, every env collapsed, and every env gates it the same
    way, the env clauses are dropped and the gate stands alone: a
    selection is a property of the install context, not of the platform,
    so ``"cli" in extras`` is the whole marker.
    """
    targets = axes.targets
    by_env: defaultdict[tuple[tuple[str, str], ...], list[str]] = defaultdict(list)
    for label in labels:
        by_env[axes.env_signatures[label]].append(label)

    gates = {label: targets[label].package_gates.get(name, ()) for label in labels}

    # The package is unconditional only when it covers every target AND
    # every environment collapsed to its env-only marker. A member-only
    # dep present in all forks of an env keeps the membership OR, so it
    # is not unconditional even at full coverage.
    by_gate: defaultdict[tuple[tuple[str, str], ...], list[Marker]] = defaultdict(list)
    loose: list[Marker] = []
    unconditional = len(labels) >= len(targets)
    for signature, env_labels in by_env.items():
        is_base = _is_base(name, signature, env_fork_counts, env_base_names)
        agreed_gate = (
            len({_shared_gate(targets[label], gates[label]) for label in env_labels})
            == 1
        )

        collapses = len(env_labels) >= env_fork_counts[signature] and is_base
        head = targets[env_labels[0]].target

        if collapses and agreed_gate:
            merged = _merge_gates(gates[label] for label in env_labels)
            by_gate[merged].append(Marker(head.environment_marker_string))
            continue

        # Forks that disagree on the gate still agree on what they share,
        # and a base dep is present in all of them, so the shared part
        # holds whichever fork the install context picks, including none.
        if collapses:
            shared = _common_gate(gates[label] for label in env_labels)
            if shared:
                by_gate[shared].append(Marker(head.environment_marker_string))

        loose.extend(
            Marker(text)
            for text in _fork_contributions(axes, projections, name, env_labels)
        )
        unconditional = False

    parts = tuple(
        _GatedMarker(_or_markers(environments), gate)
        for gate, environments in by_gate.items()
    )
    if loose:
        parts += (_GatedMarker(_or_markers(loose), ()),)
    if not unconditional:
        return parts

    # Every env collapsed at full coverage: the environment is not what
    # selects this package, so only the gate can, and only when every
    # env agrees on it.
    if set(by_gate) == {()}:
        return (_GatedMarker(None, ()),)
    if len(by_gate) == 1:
        return (_GatedMarker(None, next(iter(by_gate))),)
    return parts


def _fork_contributions(
    axes: _ForkAxes,
    projections: Mapping[tuple[str, str], _Projection],
    name: str,
    env_labels: Sequence[str],
) -> list[str]:
    """Render one environment's per-fork markers, projected and deduped.

    Each fork drops the clauses of the conflict sets the package does not
    vary over (:func:`_fork_projections`), which leaves the forks of a
    dropped set rendering the same text; the duplicates are folded into
    one contribution, in first-seen order.
    """
    seen: dict[str, None] = {}
    for label in env_labels:
        target = axes.targets[label].target
        projection = projections[name, label]
        text = (
            axes.markers[label]
            if not projection.dropped
            else _selection_marker(target, axes.exclusion_groups, projection.dropped)
        )
        kept = frozenset(target.selection) - projection.dropped
        seen[_with_gate(text, _fork_gate(kept, projection.gate))] = None
    return list(seen)


def _fork_projections(
    axes: _ForkAxes,
    pin_groups: Mapping[str, Sequence[tuple[list[PinShape], list[str]]]],
    env_fork_counts: Mapping[_EnvSignature, int],
    env_base_names: Mapping[_EnvSignature, frozenset[str]],
) -> dict[tuple[str, str], _Projection]:
    """Return, per (package, fork), the selection clauses its marker can drop.

    Every fork starts free to drop its whole selection and
    :func:`_project_fork` keeps only what the fork space justifies.  The
    dependency edges then narrow it: an entry that fires where one of its
    own dependencies does not is a lock that cannot be installed, so a
    package keeps every member its dependencies at that fork keep.  The
    allowances only shrink, so the loop settles.
    """
    same_pin: dict[tuple[str, str], frozenset[str]] = {}
    edges: dict[tuple[str, str], frozenset[str]] = {}
    for name, groups in pin_groups.items():
        for _, labels in groups:
            group = frozenset(labels)
            declared = frozenset(
                dep
                for peer in labels
                for dep in axes.targets[peer].dependencies.get(name, ())
            )
            for label in labels:
                same_pin[name, label] = group
                edges[name, label] = declared

    limits = {key: frozenset(axes.targets[key[1]].target.selection) for key in same_pin}
    projections: dict[tuple[str, str], _Projection] = {}
    stale = set(same_pin)
    while stale:
        for name, label in stale:
            projections[name, label] = _project_fork(
                axes,
                name,
                label,
                limits[name, label],
                same_pin[name, label],
                is_base=_is_base(
                    name, axes.env_signatures[label], env_fork_counts, env_base_names
                ),
            )
        narrowed = {
            (name, label): limit
            & _dependency_drops(axes, projections, label, edges[name, label])
            for (name, label), limit in limits.items()
        }
        stale = {key for key, limit in narrowed.items() if limit != limits[key]}
        limits = narrowed
    return projections


def _dependency_drops(
    axes: _ForkAxes,
    projections: Mapping[tuple[str, str], _Projection],
    label: str,
    declared: AbstractSet[str],
) -> _Members:
    """Return the members every dependency an entry declares drops at one fork.

    :func:`_dependency_entries` unions the edges over the entry's forks,
    so an edge only one fork has still rides on the whole entry; a fork
    that does not carry that dependency at all can drop nothing.
    """
    shared = frozenset(axes.targets[label].target.selection)
    for dep in declared:
        projection = projections.get((dep, label))
        shared &= projection.dropped if projection is not None else _NO_MEMBERS
    return shared


def _project_fork(
    axes: _ForkAxes,
    name: str,
    label: str,
    limit: _Members,
    same_pin: AbstractSet[str],
    *,
    is_base: bool,
) -> _Projection:
    """Return the clauses one fork's entry for ``name`` can drop.

    A conflict set is irrelevant to a package when swapping the fork's
    member of that set for any other member, every other set held fixed,
    leaves the package at the same pin reached the same way.
    Conjoining such a set's clauses narrows the entry to the forks that
    vary something the package does not depend on, so a selection naming
    a member of one set alone matches no entry and the package silently
    does not install.

    Sets are folded in one at a time and each candidate is checked over
    the whole sub-cube of the sets folded in so far rather than one axis
    at a time.  Two axes that are each flat through this fork can still
    meet at a fork with a different pin; dropping both would leave two
    entries of one package overlapping, and the forks have to stay
    mutually exclusive in the marker itself.
    """
    own_gate = axes.gates.get((name, label), _NO_MEMBERS)
    selection = frozenset(axes.targets[label].target.selection)
    signature = axes.env_signatures[label]

    dropped: _Members = frozenset()
    gate = own_gate
    varying: list[AbstractSet[tuple[str, str]]] = []
    for group in axes.exclusion_groups:
        member = group & selection & limit
        if len(member) != 1:
            continue

        candidate = [*varying, group]
        erased = _NO_MEMBERS.union(*candidate)
        held = selection - erased
        keys = [
            (signature, held | frozenset(swap))
            for swap in product(*(sorted(members) for members in candidate))
        ]
        peers = [axes.forks[key] for key in keys if key in axes.forks]
        if len(peers) != len(keys) or not same_pin.issuperset(peers):
            continue

        merged = _merged_fork_gate(axes, name, peers, erased)
        if merged is None:
            continue
        varying = candidate
        dropped |= member
        gate = merged

    # A member-only package with nothing left to name would fire in the
    # no-member install context, where a dep the base does not require
    # must not install.
    if not is_base and not gate and not selection - dropped:
        return _Projection(frozenset(), own_gate)
    return _Projection(dropped, gate)


def _merged_fork_gate(
    axes: _ForkAxes,
    name: str,
    peers: Sequence[str],
    erased: _Members,
) -> _Members | None:
    """Return the gate the sub-cube's forks share, or ``None`` when they do not.

    Dropping a set replaces the fork's own gate with one that holds at
    every point of the sub-cube: the part naming nothing in the dropped
    sets, plus every dropped member that reaches the package in the forks
    that select it.  The forks share such a gate only when each one's is
    exactly that, so a member that reaches the package under one fork of
    the other dropped sets but not another refuses the drop rather than
    guessing which way the projected entry should fire.
    """
    gates = {peer: axes.gates.get((name, peer), _NO_MEMBERS) for peer in peers}

    # An empty gate means that fork installs the package whichever of its
    # members are selected (:func:`_merge_gates`), which no clause stands
    # in for, so a sub-cube mixing the two does not project.
    if len({bool(gate) for gate in gates.values()}) != 1:
        return None

    residual = gates[peers[0]] - erased
    carried = frozenset(member for gate in gates.values() for member in gate & erased)
    for peer, gate in gates.items():
        if gate != residual | (carried & set(axes.targets[peer].target.selection)):
            return None
    return residual | carried


def _is_base(
    name: str,
    signature: _EnvSignature,
    env_fork_counts: Mapping[_EnvSignature, int],
    env_base_names: Mapping[_EnvSignature, frozenset[str]],
) -> bool:
    """Say whether an environment's base pass reached ``name``.

    When no base pass ran for the env (the signature is missing), treat
    the dep as base only if no fork ran either; with forks but no base
    attribution, base status is unknowable and the safe answer is to keep
    the membership OR.
    """
    base_names = env_base_names.get(signature)
    if base_names is None:
        return env_fork_counts[signature] == 1
    return name in base_names


def _shared_gate(
    lock: TargetLock, gate: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    """Return the part of a gate the fork's own selection does not supply.

    Two forks of one environment gate a package the same way when these
    agree; each fork's own member necessarily differs, and
    :func:`_merge_gates` folds it back in.
    """
    return tuple(sorted(set(gate) - set(lock.target.selection)))


def _fork_gate(
    kept: AbstractSet[tuple[str, str]], gate: AbstractSet[tuple[str, str]]
) -> tuple[tuple[str, str], ...]:
    """Return the gate to conjoin onto one fork's own membership marker.

    A gate that names a member the fork's marker still asserts is already
    satisfied there, so it drops whole rather than narrowing the fork's
    marker to the gate's other selections.  A member whose clause the
    projection dropped asserts nothing, so it does not count.
    """
    if gate & kept:
        return ()
    return tuple(sorted(gate))


def _common_gate(
    gates: Iterable[tuple[tuple[str, str], ...]],
) -> tuple[tuple[str, str], ...]:
    """Return the members every fork of one environment gates a package on.

    A gate is a disjunction, so a member every fork names implies every
    fork's gate: the package is reached under that member whichever fork
    the install context selects.
    """
    common: set[tuple[str, str]] | None = None
    for gate in gates:
        common = set(gate) if common is None else common & set(gate)
    return tuple(sorted(common or ()))


def _merge_gates(
    gates: Iterable[tuple[tuple[str, str], ...]],
) -> tuple[tuple[str, str], ...]:
    """OR the per-fork gates of one collapsing environment into one gate.

    A fork with an empty gate installs the package whenever its own
    member is selected, so the merged gate is empty too.
    """
    merged: set[tuple[str, str]] = set()
    for gate in gates:
        if not gate:
            return ()
        merged |= set(gate)
    return tuple(sorted(merged))


def _selection_marker(
    target: ResolveTarget,
    exclusion_groups: Sequence[AbstractSet[tuple[str, str]]],
    dropped: AbstractSet[tuple[str, str]] = frozenset(),
) -> str:
    """Return the fork's per-package marker with its co-members negated.

    Conjoins ``'name' in <variable>`` for every member of the selection
    onto the target's environment marker, then ``'name' not in
    <variable>`` for every other member of every conflict set the
    selection draws from, so at most one fork installs.  With no
    ``dropped`` members this is the target's
    :attr:`~nab_python.target.ResolveTarget.marker_string` plus the
    negations, and a selection drawing from no exclusion group is
    ``marker_string`` unchanged.

    ``dropped`` are the members of conflict sets the package does not
    vary over (:func:`_project_fork`); both their positive clause and
    their set's negations fall away, since a set nothing is kept from is
    no longer drawn from.
    """
    selected = set(target.selection) - set(dropped)

    co_members: set[tuple[str, str]] = set()
    for group in exclusion_groups:
        if group & selected:
            co_members |= group - selected

    marker = target.environment_marker_string
    for kind, name in sorted(selected):
        variable = MARKER_VARIABLE_FOR_KIND[kind]
        marker += f' and "{name}" in {variable}'
    for kind, name in sorted(co_members):
        variable = MARKER_VARIABLE_FOR_KIND[kind]
        marker += f' and "{name}" not in {variable}'
    return marker


def _gate_clause(gate: Sequence[tuple[str, str]]) -> str:
    """Render a package's membership gate as a PEP 508 clause.

    Each ``(kind, name)`` member becomes ``'name' in extras`` or
    ``'name' in dependency_groups``; a package two selections reach
    disjoins them, since either one installs it.
    """
    return " or ".join(
        f'"{name}" in {MARKER_VARIABLE_FOR_KIND[kind]}' for kind, name in sorted(gate)
    )


def _finalize_parts(
    parts: Sequence[_GatedMarker],
    universe: MarkerSet,
    name: str,
    memo: dict[str, Marker | None],
    store: DecisionStore,
) -> Marker | None:
    """Simplify each gate's environments, then the marker they assemble into.

    Simplifying the whole marker in one pass walks a cell space the
    membership variables multiply.  Each gate's environments carry no
    membership variable, so they shrink first, and the marker they
    assemble into is small by the time the second pass canonicalises it.
    """
    assembled: list[Marker] = []
    for part in parts:
        simplified = _finalize_cached(part.marker, universe, name, memo, store)
        if not part.gate:
            if simplified is None:
                return None
            assembled.append(simplified)
            continue
        assembled.append(_gated_marker(simplified, part.gate))

    # One ungated part is already what the second pass would return.
    if len(parts) == 1 and not parts[0].gate:
        return assembled[0]
    return _finalize_cached(_or_markers(assembled), universe, name, memo, store)


def _gated_marker(marker: Marker | None, gate: Sequence[tuple[str, str]]) -> Marker:
    """Conjoin a gate onto the simplified environments it selects in.

    With no environments the gate is the whole marker.  Otherwise they
    are parenthesised, since they may disjoin and ``and`` binds tighter.
    """
    if marker is None:
        return Marker(_gate_clause(gate))
    return Marker(_with_gate(f"({marker})", gate))


def _with_gate(marker: str, gate: Sequence[tuple[str, str]]) -> str:
    """AND a package's membership gate onto the marker of one target."""
    if not gate:
        return marker
    clause = _gate_clause(gate)
    if len(gate) > 1:
        clause = f"({clause})"
    return f"{marker} and {clause}"


def _env_signatures(
    targets: Mapping[str, TargetLock],
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Map each target label to a hashable signature of its environment.

    Two labels share an environment (they differ only by conflict-fork
    selection) when their marker environments are equal, so the sorted
    items form a stable, hashable key.
    """
    return {
        label: tuple(sorted(lock.target.marker_env.items()))
        for label, lock in targets.items()
    }


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
