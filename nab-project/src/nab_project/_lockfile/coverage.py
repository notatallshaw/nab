"""Emit-time lock coverage validation for the PEP 751 emitter.

A conformant PEP 751 consumer refuses a lock unless one of its declared
``environments`` markers matches the installing interpreter.  A
non-covering lock declares no row for an interpreter its own resolve
produced pins for, so the installer refuses it there.  This is the
completeness dual of the disjointness gate: that one forbids two same-name
entries firing under one context, this one forbids a resolved context with
no entry.

The universe is not expressible as a single PEP 508 marker, so the check
runs through the marker algebra.  For each target the resolve ran it asks
whether the union of the emitted rows admits the whole range the target
stands for: a minor-interval target stands for its whole minor, a whole
target for one micro.  A point in that range no row admits is returned as a
witness, and the gate fires naming the uncovered interpreter.
"""

from __future__ import annotations

from functools import reduce
from typing import TYPE_CHECKING

from nab_markersets.markersets import DecisionStore, MarkerSet, variable_names
from nab_provider.target import UNBOUNDABLE_MARKER_VARIABLES, declared_range_marker

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_provider._vendor.packaging.markers import Marker
    from nab_provider.target import ResolveTarget


__all__ = [
    "CoverageError",
]


# Axes a reference leaves open rather than pinning to one value: the shared
# python-version axis, which a minor interval carries as a range, and
# implementation_version, which the resolve mirrors onto that range on CPython.
_OPEN_PYTHON_AXIS = frozenset(
    {"python_version", "python_full_version", "implementation_version"}
)


def _reference_pins(target: ResolveTarget) -> dict[str, str]:
    """Return the env axes a reference pins to single equality values.

    Every boundable axis :func:`declared_range_marker` fixes to one value,
    minus the open python axis.
    """
    return {
        name: value
        for name, value in target.marker_env.items()
        if name not in UNBOUNDABLE_MARKER_VARIABLES and name not in _OPEN_PYTHON_AXIS
    }


class CoverageError(ValueError):
    """A resolved target has no covering ``environments`` row.

    The error names one uncovered interpreter as a concrete point so the
    producer can widen the declaration or drop the target.
    """


def validate_marker_coverage(
    targets: Sequence[ResolveTarget],
    *,
    environments: Sequence[Marker],
    store: DecisionStore | None = None,
    environment_sets: Sequence[MarkerSet] | None = None,
) -> None:
    """Confirm the emitted rows cover every target the resolve ran.

    Asks the algebra for a point the declared rows miss; a witness is a
    real interpreter the declaration would refuse.  Targets on one platform
    share a restriction, so they are asked as a single question.

    An empty ``environments`` returns early: an omitted field declares
    support for every environment, so it must not be read as the empty set.

    ``environment_sets`` are ``environments`` already built as sets; they are
    built here when omitted.
    """
    if not environments:
        return

    if environment_sets is None:
        environment_sets = [MarkerSet.from_marker(marker) for marker in environments]
    covered = reduce(MarkerSet.union, environment_sets, MarkerSet.empty())
    covered = _project_implementation_version(covered, environments, targets)

    for pins, references in _references_by_pins(targets).items():
        env = dict(pins)

        # Complementing the whole matrix carries every row's atoms on every
        # axis at once and overruns the cell budget.  Restricting first leaves
        # a single-platform residual denoting the same set.
        covered_here = covered.restrict(env)

        # Restricting the question too is sound: a group's pins are a subset
        # of every reference's own == clauses at the same values, since both
        # read the target's marker_env and the reference only ever adds the
        # python axes _reference_pins leaves open.  So this drops conjuncts
        # the reference already fixes rather than narrowing what is asked.
        asked = reduce(
            MarkerSet.union,
            (MarkerSet.from_marker(marker) for marker in references),
            MarkerSet.empty(),
        ).restrict(env)

        witness = (asked & ~covered_here).witness(store=store)
        if witness is not None:
            # The residual carries no pins, so env restores them for the message.
            raise CoverageError(_coverage_message({**env, **witness}))


def _references_by_pins(
    targets: Sequence[ResolveTarget],
) -> dict[tuple[tuple[str, str], ...], list[str]]:
    """Group the distinct target references by the platform axes they pin.

    Targets differing only in Python version pin the same axes, so one
    restricted union serves them all.  Deduped by marker string, so a split
    minor's slices and the conflict forks of one environment count once.
    """
    grouped: dict[tuple[tuple[str, str], ...], list[str]] = {}
    seen: set[str] = set()
    for target in targets:
        marker = declared_range_marker(target)
        if marker in seen:
            continue
        seen.add(marker)
        key = tuple(sorted(_reference_pins(target).items()))
        grouped.setdefault(key, []).append(marker)
    return grouped


def _project_implementation_version(
    covered: MarkerSet,
    environments: Sequence[Marker],
    targets: Sequence[ResolveTarget],
) -> MarkerSet:
    """Drop ``implementation_version`` from ``covered`` when a row mirrors it.

    On CPython the resolve mirrors each slice's ``python_full_version`` bounds
    onto ``implementation_version``, and the algebra treats the two as
    independent.  A cross term (``python_full_version`` from one slice,
    ``implementation_version`` from another) then survives the plain union and
    manufactures a false hole on a covering lock, since the reference leaves
    ``implementation_version`` open.

    With no existential primitive the projection is reproduced by ``restrict``:
    binding ``implementation_version`` to one representative per slice drops the
    axis, and the union over the targets' ``python_full_version`` reassembles
    the minor.  Runs only when a row names ``implementation_version``.
    """
    if not any("implementation_version" in variable_names(row) for row in environments):
        return covered
    reps = {target.python_full_version for target in targets}
    return reduce(
        MarkerSet.union,
        (covered.restrict({"implementation_version": rep}) for rep in reps),
        MarkerSet.empty(),
    )


def _coverage_message(witness: dict[str, str | frozenset[str]]) -> str:
    """Render the uncovered interpreter the witness names.

    ``python_full_version`` first, then the other boundable axes the witness
    pins as a sorted ``name == "value"`` list.  Kernel axes and the
    ``python_version`` half of the shared python axis are dropped; membership
    selections never appear on an environment row, so every value is a string.
    """
    skip = UNBOUNDABLE_MARKER_VARIABLES | {"python_version"}
    boundable = {
        name: value
        for name, value in witness.items()
        if isinstance(value, str) and name not in skip
    }
    full_version = boundable.pop("python_full_version")
    axes = ", ".join(
        f'{name} == "{value}"' for name, value in sorted(boundable.items())
    )
    return (
        "the lock declares no environment covering"
        f' python_full_version == "{full_version}" ({axes}).'
        " The resolve produced pins for this interpreter but no environments"
        " row admits it."
    )
