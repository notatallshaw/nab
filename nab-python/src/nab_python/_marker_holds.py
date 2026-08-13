"""Evaluate a PEP 508 dependency marker for a resolve-time environment.

Its own module because it is the resolve path's only marker-set
dependency.  ``packaging.markers.Marker.evaluate`` binds ``extra`` to a
single string, which cannot say "these three extras are active", so this
goes through :class:`~packaging.markersets.MarkerSet` instead.  Everything
that needs the predicate takes it as an argument, so the engine never
imports the marker-set engine to get it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._conflict_kind import EMPTY_MEMBERSHIP_SETS
from ._vendor.packaging.markers import UndefinedComparison
from ._vendor.packaging.markersets import MarkerSet

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet

    from ._vendor.packaging.markers import Marker


class UnevaluableMarkerError(ValueError):
    """A dependency marker parses but no comparison decides it.

    PEP 508 accepts any operator between a variable and a literal, and PEP 440
    gives the compatible-release operator a meaning only over a release with
    at least two components.  So ``python_full_version ~= "3"`` is a valid
    marker with nothing to evaluate, and ``sys_platform ~= "linux"`` is one on
    a variable that holds no version at all.

    A requirement the project declares is neither activated nor dropped by
    such a marker, and either guess changes what gets locked, so the run stops
    and names it.  A candidate's own metadata takes the other route:
    ``Provider.get_dependencies`` already turns metadata it cannot read into a
    candidate skip.
    """


def marker_set(marker: Marker) -> MarkerSet:
    """Return the algebra form of ``marker``.

    Each clause is checked against its operator while the set is built, so
    this is the one place a marker with no meaning can be caught.  Raises
    :class:`UnevaluableMarkerError` naming the whole marker, since the failing
    clause alone does not say which dependency to edit.
    """
    try:
        return MarkerSet.from_marker(marker)
    except UndefinedComparison as exc:
        msg = f"marker {marker} cannot be evaluated: {exc}"
        raise UnevaluableMarkerError(msg) from exc


def dependency_marker_holds(
    marker: Marker, environment: Mapping[str, str | AbstractSet[str]]
) -> bool:
    """Evaluate a dependency marker for a resolve-time ``environment``.

    ``extra`` is set-valued: bound to the active extra names, ``extra == "x"``
    tests membership and ``extra != "x"`` non-membership, both PEP 685
    normalised.  It defaults to the empty set when ``environment`` omits it.

    A standard variable a marker names but ``environment`` omits raises
    ``UndefinedEnvironmentName``; callers pass a complete
    ``ResolveTarget.marker_env``.  The lockfile-only set variables are seeded
    empty, so a marker that tests one evaluates to False rather than raising.

    A clause no comparison decides raises :class:`UnevaluableMarkerError`.
    """
    env: dict[str, str | AbstractSet[str]] = {"extra": frozenset()}
    env.update(environment)
    env.update(EMPTY_MEMBERSHIP_SETS)

    return marker_set(marker).evaluate(env)
