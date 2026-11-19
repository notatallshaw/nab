"""Emit-time lock coverage validation for the PEP 751 emitter.

A conformant PEP 751 consumer refuses a lock unless one of its declared
``environments`` markers matches the installing interpreter.  A
non-covering lock declares no row for an interpreter its own resolve
produced pins for, so the installer refuses it there.  This is the
dual of the disjointness check. That check forbids same-name
entries firing under one context; this check forbids a resolved context
with no entry.

The universe is not expressible as a single PEP 508 marker, so the check
runs through the marker algebra.  For each target the resolve ran it asks
whether the union of the emitted rows admits the whole range the target
stands for: a minor-interval target stands for its whole minor, a whole
target for one micro.  A point in that range no row admits is returned as a
witness, and validation names the uncovered interpreter.
"""

from __future__ import annotations

__lazy_modules__ = (
    "functools",
    "nab_markersets.markersets",
    "nab_provider.target",
)

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
    """The lock's environment rows could not be shown to cover a resolved target."""


def validate_marker_coverage(
    targets: Sequence[ResolveTarget],
    *,
    environments: Sequence[Marker],
    store: DecisionStore | None = None,
    environment_sets: Sequence[MarkerSet] | None = None,
    uses_implementation_version: bool | None = None,
) -> None:
    """Confirm the emitted rows cover every target the resolve ran.

    First check the targets on each platform together. If that fails, check
    their known Python versions individually and report an uncovered interpreter
    when the marker algebra can identify one.

    An empty ``environments`` returns early: an omitted field declares
    support for every environment, so it must not be read as the empty set.

    ``environment_sets`` are ``environments`` already built as sets; they are
    built here when omitted.

    ``uses_implementation_version`` supplies the prepared rows' raw-variable flag.
    """
    if not environments:
        return

    if environment_sets is None:
        environment_sets = [MarkerSet.from_marker(marker) for marker in environments]
    covered = reduce(MarkerSet.union, environment_sets, MarkerSet.empty())
    if uses_implementation_version is None:
        uses_implementation_version = any(
            "implementation_version" in variable_names(row) for row in environments
        )
    # CPython reports its language version as its implementation version.
    cpython_covered = (
        covered.with_cpython_implementation_version()
        if uses_implementation_version
        else covered
    )

    for pins, references in _references_by_pins(targets).items():
        env = dict(pins)

        # Complementing the whole matrix carries every row's atoms on every
        # axis at once and overruns the cell budget.  Restricting first leaves
        # a single-platform residual denoting the same set.
        rows = (
            cpython_covered if env.get("implementation_name") == "cpython" else covered
        )
        covered_here = rows.restrict(env)

        requested = [
            (target, MarkerSet.from_marker(marker).restrict(env))
            for target, marker in references
        ]
        asked = reduce(
            MarkerSet.union, (row for _, row in requested), MarkerSet.empty()
        )
        if asked.is_subset(covered_here, store=store):
            continue

        for target, reference in requested:
            _check_target_coverage(target, reference, covered_here, env, store)


def _check_target_coverage(
    target: ResolveTarget,
    reference: MarkerSet,
    covered: MarkerSet,
    pins: dict[str, str],
    store: DecisionStore | None,
) -> None:
    """Prove coverage of one target or explain why the declaration failed."""
    # Substring tests on versions can be decided directly on a known value.
    # An open minor still needs coverage for every possible full version.
    known = {"python_version": target.python_version}
    if not target.is_minor_interval:
        known["python_full_version"] = target.python_full_version
    missing = reference - covered.restrict(known)
    if missing.is_empty(store=store):
        return

    witness = missing.witness(store=store)
    if witness is not None:
        raise CoverageError(_coverage_message({**pins, **witness}))

    msg = (
        f"cannot verify that the lock environments cover {target.label}. "
        "Use version comparisons that describe the supported interpreters."
    )
    raise CoverageError(msg)


def _references_by_pins(
    targets: Sequence[ResolveTarget],
) -> dict[tuple[tuple[str, str], ...], list[tuple[ResolveTarget, str]]]:
    """Group the distinct target references by the platform axes they pin.

    Targets differing only in Python version pin the same axes, so one
    restricted union serves them all.  Deduped by marker string, so a split
    minor's slices and the conflict forks of one environment count once.
    """
    grouped: dict[tuple[tuple[str, str], ...], list[tuple[ResolveTarget, str]]] = {}
    seen: set[str] = set()
    for target in targets:
        marker = declared_range_marker(target)
        if marker in seen:
            continue
        seen.add(marker)
        key = tuple(sorted(_reference_pins(target).items()))
        grouped.setdefault(key, []).append((target, marker))
    return grouped


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
