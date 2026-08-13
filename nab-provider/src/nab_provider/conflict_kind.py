"""Conflict-kind constants and PEP 508 marker-variable mapping."""

from __future__ import annotations

import re

KIND_EXTRA = "extra"
KIND_GROUP = "group"

# Membership of a conflict-fork member emits ``'name' in <variable>`` on the
# per-package marker.
MARKER_VARIABLE_FOR_KIND = {
    KIND_EXTRA: "extras",
    KIND_GROUP: "dependency_groups",
}

# ``extras`` and ``dependency_groups`` are PEP 685 / PEP 735 set variables that
# packaging only defines when consuming a lockfile.  Forks fold their members
# into requirements rather than the environment, so both are empty at resolve
# time; seeding them keeps a marker that tests one from raising
# UndefinedEnvironmentName.
EMPTY_MEMBERSHIP_SETS: dict[str, frozenset[str]] = {
    variable: frozenset() for variable in MARKER_VARIABLE_FOR_KIND.values()
}

_MEMBERSHIP_SET_PATTERN = re.compile(
    r"\b(" + "|".join(MARKER_VARIABLE_FOR_KIND.values()) + r")\b"
)


def membership_set_in_marker(marker_text: str) -> str | None:
    """Return the lockfile-only set variable a marker tests, or ``None``.

    A dependency marker that tests ``extras`` or ``dependency_groups`` is a
    mistake, usually meant as ``extra ==``, since both are empty at resolve
    time.
    """
    match = _MEMBERSHIP_SET_PATTERN.search(marker_text)
    return match.group(1) if match else None
