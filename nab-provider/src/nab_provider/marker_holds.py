"""Evaluate a PEP 508 dependency marker for a resolve-time environment.

``packaging.markers.Marker.evaluate`` binds ``extra`` to a single string and
cannot say that several extras are active, so this goes through
:class:`~packaging.markersets.MarkerSet` instead.  Its own module so the
resolve engine never imports ``packaging.markersets``;
``tasks/check_engine_markersets.py`` enforces that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nab_provider._vendor.packaging.markers import UndefinedComparison
from nab_provider._vendor.packaging.markersets import MarkerSet

from .conflict_kind import EMPTY_MEMBERSHIP_SETS

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet

    from nab_provider._vendor.packaging.markers import Marker


class UnevaluableMarkerError(ValueError):
    """A dependency marker parses but no comparison decides it.

    PEP 508 accepts any operator between a variable and a literal, and PEP 440
    gives ``~=`` a meaning only over a release with at least two components.
    So ``python_full_version ~= "3"`` is a valid marker with nothing to
    evaluate, and ``sys_platform ~= "linux"`` is one on a variable that holds
    no version at all.  Either guess about it changes what gets locked, so the
    run stops.
    """


def _unevaluable(marker: Marker, exc: UndefinedComparison) -> UnevaluableMarkerError:
    """Return the error for ``marker``, named in full.

    The failing clause alone does not say which dependency to edit.
    """
    return UnevaluableMarkerError(f"marker {marker} cannot be evaluated: {exc}")


def marker_set(marker: Marker) -> MarkerSet:
    """Return ``marker`` as a :class:`MarkerSet`.

    Building the set checks each clause against its operator, so a marker with
    no meaning is caught here.
    """
    try:
        return MarkerSet.from_marker(marker)
    except UndefinedComparison as exc:
        raise _unevaluable(marker, exc) from exc


def evaluate_prepared(
    marker: Marker, environment: dict[str, str | AbstractSet[str]]
) -> bool:
    """Evaluate ``marker`` against a ``prepare_environment`` result.

    A clause no comparison decides raises :class:`UnevaluableMarkerError`, as
    it does through :func:`marker_set`.
    """
    try:
        return marker.evaluate_prepared(environment)
    except UndefinedComparison as exc:
        raise _unevaluable(marker, exc) from exc


def dependency_marker_holds(
    marker: Marker, environment: Mapping[str, str | AbstractSet[str]]
) -> bool:
    """Evaluate a dependency marker for a resolve-time ``environment``.

    ``extra`` is set-valued: bound to the active extra names, ``extra == "x"``
    tests membership and ``extra != "x"`` non-membership, both PEP 685
    normalised.  It defaults to the empty set when ``environment`` omits it.

    A standard variable a marker names but ``environment`` omits raises
    ``UndefinedEnvironmentName``.  The lockfile-only set variables are seeded
    empty, so a marker that tests one evaluates to False rather than raising.

    A clause no comparison decides raises :class:`UnevaluableMarkerError`.
    """
    env: dict[str, str | AbstractSet[str]] = {"extra": frozenset()}
    env.update(environment)
    env.update(EMPTY_MEMBERSHIP_SETS)

    return marker_set(marker).evaluate(env)
