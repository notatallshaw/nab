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

from .._vendor.packaging.markersets import MarkerSet
from ..target import UNBOUNDABLE_MARKER_VARIABLES, declared_range_marker

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .._vendor.packaging.markers import Marker
    from ..target import ResolveTarget


__all__ = [
    "CoverageError",
]


class CoverageError(ValueError):
    """A resolved target has no covering ``environments`` row.

    The error names one uncovered interpreter as a concrete point so the
    producer can widen the declaration or drop the target.
    """


def validate_marker_coverage(
    targets: Sequence[ResolveTarget],
    *,
    environments: Sequence[Marker],
) -> None:
    """Confirm the emitted rows cover every target the resolve ran.

    For each distinct target reference the validator asks the algebra for a
    point the union of rows does not admit (``reference & ~covered``); a
    witness is a real interpreter the declaration would refuse.  References
    are deduped by marker string, so a split minor's slices and conflict
    forks sharing an environment are checked once.

    An empty ``environments`` returns early: an omitted field declares
    support for every environment, so it must not be read as the empty set.
    """
    if not environments:
        return

    covered = reduce(
        MarkerSet.union,
        (MarkerSet.from_marker(marker) for marker in environments),
        MarkerSet.empty(),
    )

    seen: set[str] = set()
    for target in targets:
        marker = declared_range_marker(target)
        if marker in seen:
            continue
        seen.add(marker)
        residual = MarkerSet.from_marker(marker) & ~covered
        witness = residual.witness()
        if witness is not None:
            raise CoverageError(_coverage_message(witness))


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
