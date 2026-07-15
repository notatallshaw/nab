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
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli_w

from .._vendor.packaging.markers import Marker
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
from .disjointness import validate_marker_disjointness

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from ..lockfile import (
        LockInput,
        PinShape,
        SdistArtifact,
        TargetLock,
        WheelArtifact,
    )


__all__ = [
    "DivergentBaseDependencyError",
    "build_pylock",
    "write_lock",
]


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
    mandate one).

    Directory, wheel and sdist paths are written relative to
    ``output_path``'s parent so the lockfile stays portable between
    machines (PEP 751 records those paths relative to the lock file).
    With no ``output_path`` the current directory is the base.
    """
    lock_dir = Path(output_path).parent if output_path is not None else None
    text = render_lock(lock_input, lock_dir=lock_dir)
    if output_path is not None:
        Path(output_path).write_text(text, encoding="utf-8")
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

    base = (lock_dir if lock_dir is not None else Path.cwd()).resolve()
    package_records = _build_packages(lock_input, base)
    package_records.sort(key=_package_sort_key)
    validate_marker_disjointness(
        package_records,
        environments=lock_input.marker_envs,
        extras=lock_input.extras,
        groups=lock_input.active_groups,
        exclusive_groups=conflict_exclusion_groups(lock_input.conflicts),
        declared_groups=conflict_member_groups(lock_input.conflicts),
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


def _build_packages(lock_input: LockInput, lock_dir: Path) -> list[Package]:
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

    The emitted marker is the raw OR of the per-target marker
    expressions; no Boolean minimisation runs.

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

    for canonical_name, per_target in by_name.items():
        groups = _group_pins_by_pin(per_target)
        _check_base_fork_agreement(
            canonical_name,
            per_target,
            groups,
            env_signatures,
            env_fork_counts,
            base_names,
        )

        for pins, labels in groups:
            marker = _build_marker(
                canonical_name,
                labels,
                targets,
                env_signatures,
                env_fork_counts,
                base_names,
            )
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
    targets: Mapping[str, TargetLock],
    env_signatures: Mapping[str, tuple[tuple[str, str], ...]],
    env_fork_counts: Mapping[tuple[tuple[str, str], ...], int],
    env_base_names: Mapping[tuple[tuple[str, str], ...], frozenset[str]],
) -> Marker | None:
    """Return the marker selecting ``labels``, or ``None`` if unconditional.

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
    """
    by_env: defaultdict[tuple[tuple[str, str], ...], list[str]] = defaultdict(list)
    for label in labels:
        by_env[env_signatures[label]].append(label)

    # The package is unconditional only when it covers every target AND
    # every environment collapsed to its env-only marker. A member-only
    # dep present in all forks of an env keeps the membership OR, so it
    # is not unconditional even at full coverage.
    contributions: list[Marker] = []
    unconditional = len(labels) >= len(targets)
    for signature, env_labels in by_env.items():
        base_names = env_base_names.get(signature)

        # When no base pass ran for an env (``base_names is None``),
        # treat the dep as base only if no fork ran either; with forks
        # but no base attribution, base status is unknowable and the
        # safe answer is to keep the membership OR.
        is_base = (
            (name in base_names)
            if base_names is not None
            else (env_fork_counts[signature] == 1)
        )
        if len(env_labels) >= env_fork_counts[signature] and is_base:
            head = targets[env_labels[0]].target
            contributions.append(Marker(head.environment_marker_string))
        else:
            contributions.extend(
                Marker(targets[label].target.marker_string) for label in env_labels
            )
            unconditional = False

    if unconditional:
        return None
    return _or_markers(contributions)


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
