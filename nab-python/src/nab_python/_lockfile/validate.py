"""Resolver-free disqualification checks for ``nab lock --locked``.

These run before the fresh resolve. Each function is a disqualifier: it
returns a :class:`LockDisqualification` when it can prove a fresh resolve
could not reproduce the committed lock, or ``None`` to fall through to the
full re-resolve. There is no success verdict here; only the full re-resolve
reports a lock as up to date. nab is non-sticky, so a lock can satisfy every
input yet be stale once a newer admissible version exists, and only a fresh
resolve tells the two apart.

The envelope checks (:func:`check_envelope`) cover the lockfile fields the
writer computes straight from the inputs: ``requires-python``, ``extras``,
``dependency-groups`` and ``default-groups``. The validity checks
(:func:`check_direct_requirements`, :func:`check_constraints`) cover what
every successful resolve renders: each active direct requirement is present
and its specifier met, and each pin satisfies every active constraint.

:func:`check_locked` is the entry point the CLI calls: it reads the committed
lock and runs both stages, so a caller never handles a parsed lock itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import tomli

from .._conflict_kind import dependency_marker_holds
from .._vendor.packaging.markers import (
    UndefinedComparison,
    UndefinedEnvironmentName,
)
from .._vendor.packaging.pylock import Pylock, PylockValidationError
from .._vendor.packaging.requirements import Requirement
from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from .._vendor.packaging.markers import Marker
    from .._vendor.packaging.pylock import Package
    from .._vendor.packaging.version import Version


class LockfileSyntaxError(Exception):
    """A committed lockfile that is not readable as TOML."""


class InvalidLockfileError(Exception):
    """A committed lockfile that parses as TOML but not as PEP 751."""


@dataclass(frozen=True, slots=True)
class LockDisqualification:
    """A proven reason the committed lock is out of date.

    ``reason`` is a plain-language clause naming the disqualifier.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class RootRequirement:
    """One active direct requirement plus the pyproject clause it came from.

    ``source`` is the plain-language origin named in a disqualification
    reason, for example ``[project].dependencies`` or a selected extra or group.
    """

    requirement: Requirement
    source: str


def check_envelope(
    committed: Pylock,
    *,
    requires_python: str | None,
    extras: tuple[str, ...],
    dependency_groups: tuple[str, ...],
    default_groups: tuple[str, ...],
) -> LockDisqualification | None:
    """Compare the current envelope against the committed lock.

    ``requires-python`` compares as ``SpecifierSet`` equality and each
    selection as a set of normalized names, so reformatting or reordering
    does not fire and an empty selection matches the ``None`` the writer
    commits. Returns the first difference, or ``None`` when every field agrees.
    """
    requires_python_result = _check_requires_python(
        committed.requires_python, requires_python
    )
    if requires_python_result is not None:
        return requires_python_result

    for kind, committed_names, current_names in (
        ("extras", committed.extras, extras),
        ("dependency-groups", committed.dependency_groups, dependency_groups),
        ("default-groups", committed.default_groups, default_groups),
    ):
        result = _check_name_set(kind, committed_names, current_names)
        if result is not None:
            return result

    return None


def _check_requires_python(
    committed: SpecifierSet | None,
    current: str | None,
) -> LockDisqualification | None:
    current_set = SpecifierSet(current) if current else None
    if committed == current_set:
        return None
    return LockDisqualification(
        reason=(
            f"the lockfile requires-python {_render_specifier(committed)} "
            f"does not match this run's {_render_specifier(current_set)}"
        )
    )


def _check_name_set(
    kind: str,
    committed: Sequence[str] | None,
    current: Sequence[str],
) -> LockDisqualification | None:
    committed_set = {canonicalize_name(name) for name in committed or ()}
    current_set = {canonicalize_name(name) for name in current}
    if committed_set == current_set:
        return None
    return LockDisqualification(
        reason=(
            f"the lockfile was built with {kind} {_render_name_set(committed_set)} "
            f"but this run selects {_render_name_set(current_set)}"
        )
    )


def check_direct_requirements(
    committed: Pylock,
    requirements: Iterable[RootRequirement],
    *,
    marker_env: Mapping[str, str],
) -> LockDisqualification | None:
    """Check every active direct requirement against the committed lock.

    A requirement is active when its marker holds for ``marker_env``. Each
    active requirement must be pinned somewhere in ``committed.packages``, and
    when the matching pin records a concrete version the specifier must
    contain it. Anything that cannot be reduced to a name and specifier
    against a concrete version is skipped: an inactive or indeterminate
    marker, a URL requirement, a version-less or direct (URL, VCS, directory)
    pin, and a name carrying more than one versioned pin. Returns the first
    violation, or ``None``.
    """
    package_names = {package.name for package in committed.packages}
    versioned = _versioned_pins(committed)
    for root in requirements:
        req = root.requirement
        if _marker_skips(req.marker, marker_env):
            continue
        name = canonicalize_name(req.name)
        if name not in package_names:
            return LockDisqualification(
                reason=(
                    f"{root.source} requires {req.name} and its marker applies "
                    f"here, but the lock has no {req.name} pin"
                )
            )
        if req.url:
            continue
        version = versioned.get(name)
        if version is None:
            continue
        if not req.specifier.contains(version, prereleases=True):
            return LockDisqualification(
                reason=(
                    f"{root.source} requires {req.name}{req.specifier} but the "
                    f"lock pins {req.name} {version}"
                )
            )
    return None


def check_constraints(
    committed: Pylock,
    constraints: Iterable[Requirement],
    *,
    marker_env: Mapping[str, str],
) -> LockDisqualification | None:
    """Check every active constraint against the committed lock.

    A constraint is active when its marker holds for ``marker_env``. When it
    names a single versioned pin, that version must satisfy the constraint. An
    inactive or indeterminate marker, and a name with no single versioned pin
    (absent, version-less, or multiple under a conflict fork), are skipped.
    Returns the first violation, or ``None``.
    """
    versioned = _versioned_pins(committed)
    for constraint in constraints:
        if _marker_skips(constraint.marker, marker_env):
            continue
        version = versioned.get(canonicalize_name(constraint.name))
        if version is None:
            continue
        if not constraint.specifier.contains(version, prereleases=True):
            return LockDisqualification(
                reason=(
                    f"the constraint {constraint.name}{constraint.specifier} is "
                    f"violated by the pinned {constraint.name} {version}"
                )
            )
    return None


def _versioned_pins(committed: Pylock) -> dict[str, Version]:
    """Map canonical name to version for names with a single concrete pin.

    URL, VCS and directory pins are excluded: they have no index version to
    test a specifier against. A name carrying more than one versioned pin is
    excluded too: a conflict fork records the same package once per member
    under a disjoint marker, so no single version stands for the name.
    """
    versions: dict[str, Version] = {}
    duplicated: set[str] = set()
    for package in committed.packages:
        if package.version is None or _is_direct_pin(package):
            continue
        if package.name in versions:
            duplicated.add(package.name)
        versions[package.name] = package.version
    for name in duplicated:
        del versions[name]
    return versions


def _is_direct_pin(package: Package) -> bool:
    return (
        package.vcs is not None
        or package.directory is not None
        or package.archive is not None
    )


def _marker_skips(marker: Marker | None, marker_env: Mapping[str, str]) -> bool:
    """Return whether an item is inactive or indeterminate for ``marker_env``.

    A missing marker is active. A false marker is inactive. A marker that
    cannot be evaluated is indeterminate. Both inactive and indeterminate skip.
    """
    if marker is None:
        return False
    try:
        active = dependency_marker_holds(marker, marker_env)
    except (UndefinedComparison, UndefinedEnvironmentName):
        return True
    return not active


def _render_specifier(spec: SpecifierSet | None) -> str:
    return "(none)" if spec is None else str(spec)


def _render_name_set(names: Iterable[str]) -> str:
    return "{" + ", ".join(sorted(names)) + "}"


def read_committed_pylock(path: Path) -> Pylock:
    """Parse the committed lockfile at ``path``.

    Raises :class:`OSError` when the file cannot be read,
    :class:`LockfileSyntaxError` when it is not TOML, and
    :class:`InvalidLockfileError` when it is TOML but not a PEP 751 lock.
    """
    try:
        data = tomli.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomli.TOMLDecodeError) as e:
        raise LockfileSyntaxError(str(e)) from e
    try:
        return Pylock.from_dict(data)
    except PylockValidationError as e:
        raise InvalidLockfileError(str(e)) from e


def check_locked(  # noqa: PLR0913 - the envelope fields and the validity inputs are each a separate check
    lockfile: Path,
    *,
    requires_python: str | None,
    extras: tuple[str, ...],
    dependency_groups: tuple[str, ...],
    default_groups: tuple[str, ...],
    roots: Iterable[RootRequirement] | None = None,
    constraints: Iterable[str] = (),
    marker_env: Mapping[str, str] | None = None,
    exclude: frozenset[str] = frozenset(),
) -> LockDisqualification | None:
    """Disqualify the committed lock at ``lockfile``, or return ``None``.

    Runs the envelope checks, then the validity checks over the active
    direct requirements and constraints. ``roots`` of ``None`` (the caller
    could not read them) runs the envelope checks alone. ``exclude`` holds
    canonical names to skip, for the workspace members ``--no-emit-workspace``
    drops from both sides. Constraints arrive as text; the config loader has
    already rejected any that do not parse.

    Raises the errors :func:`read_committed_pylock` raises.
    """
    committed = read_committed_pylock(lockfile)
    disqualification = check_envelope(
        committed,
        requires_python=requires_python,
        extras=extras,
        dependency_groups=dependency_groups,
        default_groups=default_groups,
    )
    if disqualification is not None or roots is None or marker_env is None:
        return disqualification
    active = [
        root
        for root in roots
        if canonicalize_name(root.requirement.name) not in exclude
    ]
    direct = check_direct_requirements(committed, active, marker_env=marker_env)
    if direct is not None:
        return direct
    parsed = [Requirement(text) for text in constraints]
    return check_constraints(committed, parsed, marker_env=marker_env)
