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
    UndefinedEnvironmentName,
)

from .conflict_kind import EMPTY_MEMBERSHIP_SETS

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from collections.abc import Set as AbstractSet

    from nab_provider._vendor.packaging.markers import Marker


class UnevaluableMarkerError(ValueError):
    """A dependency marker parses but nothing decides it.

    PEP 508 accepts any operator between a variable and a literal, and PEP 440
    gives ``~=`` a meaning only over a release with at least two components.
    So ``python_full_version ~= "3"`` is a valid marker with nothing to
    evaluate, and ``sys_platform ~= "linux"`` is one on a variable that holds
    no version at all.  A marker that quotes its variable, ``"extra" == "gpu"``,
    is a third: neither side names a variable to look up.  Any guess about one
    of these changes what gets locked.
    """


class IntractableMarkerError(ValueError):
    """A marker question that overran a budget instead of being decided.

    The budgets bound a version literal's digits, a decomposition's cells, and
    the lock emitter's selection walk, so a pathological marker stops the run
    rather than iterating unbounded.
    """


def _unevaluable(
    marker: Marker, exc: UndefinedComparison | UndefinedEnvironmentName
) -> UnevaluableMarkerError:
    """Return the error for ``marker``, named in full.

    The failing clause alone does not say which dependency to edit.
    """
    return UnevaluableMarkerError(f"marker {marker} cannot be evaluated: {exc}")


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
        raise _unevaluable(marker, exc) from exc


def evaluate_prepared(
    marker: Marker, environment: dict[str, str | AbstractSet[str]]
) -> bool:
    """Evaluate ``marker`` against a ``prepare_environment`` result.

    A marker packaging cannot decide raises :class:`UnevaluableMarkerError`, as
    it does through :func:`marker_set`.  ``"extra" == "gpu"`` is one: packaging
    reads the right-hand literal as a variable name and finds none.  So
    ``environment`` has to carry every variable a marker may name, or a gap in
    it is reported as an unevaluable marker.
    """
    try:
        return marker.evaluate_prepared(environment)
    except (UndefinedComparison, UndefinedEnvironmentName) as exc:
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

    A clause no comparison decides raises :class:`UnevaluableMarkerError`, and
    a marker that overruns a budget raises :class:`IntractableMarkerError`.
    """
    env: dict[str, str | AbstractSet[str]] = {"extra": frozenset()}
    env.update(environment)
    env.update(EMPTY_MEMBERSHIP_SETS)

    with intractable_as_error():
        return marker_set(marker).evaluate(env)
