"""Evaluate a PEP 508 dependency marker for a resolve-time environment.

``packaging.markers.Marker.evaluate`` binds ``extra`` to a single string and
cannot say that several extras are active, so this goes through
:class:`~nab_markersets.markersets.MarkerSet` instead.  Its own module so the resolve
engine reaches no marker-set definition; ``tasks/check_engine_markersets.py``
holds the import closure to a named list this module is on.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from nab_markersets.errors import IntractableMarkerSet
from nab_markersets.markersets import MarkerSet
from nab_provider._vendor.packaging.markers import (
    UndefinedComparison,
)

from .conflict_kind import EMPTY_MEMBERSHIP_SETS
from .environment import (
    UnevaluableMarkerError,
    evaluate_prepared,
    marker_evaluation_error,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from collections.abc import Set as AbstractSet

    from nab_provider._vendor.packaging.markers import Marker


__all__ = [
    "IntractableMarkerError",
    "UnevaluableMarkerError",
    "dependency_marker_holds",
    "evaluate_prepared",
    "intractable_as_error",
    "marker_set",
]


class IntractableMarkerError(ValueError):
    """A marker question that overran a budget instead of being decided.

    The budgets bound a version literal's digits, a decomposition's cells, and
    the lock emitter's selection walk, so a pathological marker stops the run
    rather than iterating unbounded.
    """


@contextmanager
def intractable_as_error() -> Iterator[None]:
    """Report an intractable marker set as :class:`IntractableMarkerError`."""
    try:
        yield
    except IntractableMarkerSet as exc:
        raise IntractableMarkerError(str(exc)) from exc


def marker_set(marker: Marker) -> MarkerSet:
    """Return ``marker`` as a :class:`MarkerSet`.

    Building the set checks each clause against its operator, so a marker with
    no meaning is caught here.
    """
    try:
        return MarkerSet.from_marker(marker)
    except UndefinedComparison as exc:
        raise marker_evaluation_error(marker, exc) from exc


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

    A clause no comparison decides raises :class:`UnevaluableMarkerError`, and
    a marker that overruns a budget raises :class:`IntractableMarkerError`.
    """
    env: dict[str, str | AbstractSet[str]] = {"extra": frozenset()}
    env.update(environment)
    env.update(EMPTY_MEMBERSHIP_SETS)

    with intractable_as_error():
        return marker_set(marker).evaluate(env)
