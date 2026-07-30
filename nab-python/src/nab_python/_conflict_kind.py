"""Conflict-kind constants and PEP 508 marker-variable mapping.

A leaf module that :mod:`nab_python.config`, :mod:`nab_python.target`, and
:mod:`nab_python._lockfile.disjointness` can import without forming a
cycle.  :class:`nab_python.config.ConflictKind` takes its enum values from
``KIND_EXTRA`` / ``KIND_GROUP`` so a rename here flows to every consumer.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ._vendor.packaging.markersets import MarkerSet

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet

    from ._vendor.packaging.markers import Marker

KIND_EXTRA = "extra"
KIND_GROUP = "group"

# Membership of a conflict-fork member emits ``'name' in <variable>`` on
# the per-package marker; this mapping is the (kind -> variable) contract
# the universal matrix and the disjointness validator share.
MARKER_VARIABLE_FOR_KIND = {
    KIND_EXTRA: "extras",
    KIND_GROUP: "dependency_groups",
}

# ``extras`` and ``dependency_groups`` are PEP 685 / PEP 735 set variables that
# packaging only defines when consuming a lockfile.  At resolve time no
# conflict-fork member is active as a marker-set member (forks fold their
# members into requirements, not the environment), so both are empty.  Seeding
# them keeps a dependency marker that tests one from raising an
# UndefinedEnvironmentName at evaluation; the membership tests False and
# the dep is dropped.
EMPTY_MEMBERSHIP_SETS: dict[str, frozenset[str]] = {
    variable: frozenset() for variable in MARKER_VARIABLE_FOR_KIND.values()
}

_MEMBERSHIP_SET_PATTERN = re.compile(
    r"\b(" + "|".join(MARKER_VARIABLE_FOR_KIND.values()) + r")\b"
)


def membership_set_in_marker(marker_text: str) -> str | None:
    """Return the lockfile-only set variable a marker tests, or ``None``.

    A dependency marker that tests ``extras`` or ``dependency_groups`` is a
    mistake (usually meant as ``extra ==``): those variables are defined only
    when consuming a lockfile, so they are empty at resolve time.
    """
    match = _MEMBERSHIP_SET_PATTERN.search(marker_text)
    return match.group(1) if match else None


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
    """
    env: dict[str, str | AbstractSet[str]] = {"extra": frozenset()}
    env.update(environment)
    env.update(EMPTY_MEMBERSHIP_SETS)

    return MarkerSet.from_marker(marker).evaluate(env)
