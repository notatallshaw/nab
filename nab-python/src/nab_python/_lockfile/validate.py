"""Resolver-free disqualification checks for ``nab lock --locked``.

This module holds the pure, network-free tier that ``nab lock --locked``
runs before it consumes a fresh resolve. Every function here is a
disqualifier only: it returns a :class:`LockDisqualification` carrying a
rendered reason when it can prove that a fresh resolve could not
reproduce the committed lock, or ``None`` to fall through to the full
re-resolve.

The tier has no success verdict of its own. There is no "satisfied"
return value, so no path from this module can report a lock as up to
date. Only the full re-resolve comparison may do that. nab is
non-sticky, so a lock can still satisfy every input while a newer
admissible version makes it stale, and only a fresh resolve can tell the
two apart.

Family E, the envelope checks, cover the lockfile-level fields the writer
computes straight from the current inputs rather than from the search:
``requires-python``, ``extras``, ``dependency-groups`` and
``default-groups``. When the committed lock carries a different value in
one of them, the full comparison would also differ, so firing here is
sound.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .._vendor.packaging.specifiers import SpecifierSet
from .._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .._vendor.packaging.pylock import Pylock


@dataclass(frozen=True, slots=True)
class LockDisqualification:
    """A proven reason the committed lock is out of date.

    ``reason`` is a plain-language clause naming the disqualifier.
    """

    reason: str


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


def _render_specifier(spec: SpecifierSet | None) -> str:
    return "(none)" if spec is None else str(spec)


def _render_name_set(names: Iterable[str]) -> str:
    return "{" + ", ".join(sorted(names)) + "}"
