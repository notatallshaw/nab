"""Conflict-kind constants and PEP 508 marker-variable mapping.

A leaf module that :mod:`nab_python.config`, :mod:`nab_python.target`, and
:mod:`nab_python._lockfile.disjointness` can import without forming a
cycle.  :class:`nab_python.config.ConflictKind` takes its enum values from
``KIND_EXTRA`` / ``KIND_GROUP`` so a rename here flows to every consumer.

Nothing here imports :mod:`packaging.markersets`.  Evaluating a dependency
marker does, and lives in :mod:`nab_python._marker_holds` for that reason.
"""

from __future__ import annotations

import re

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
