"""Conflict-kind constants and PEP 508 marker-variable mapping."""

from __future__ import annotations

KIND_EXTRA = "extra"
KIND_GROUP = "group"

# Membership of a conflict-fork member emits ``'name' in <variable>`` on the
# per-package marker.
MARKER_VARIABLE_FOR_KIND = {
    KIND_EXTRA: "extras",
    KIND_GROUP: "dependency_groups",
}

# Packaging defines these sets for lockfile evaluation. At resolve time the
# selected extras and groups contribute their requirements directly.
EMPTY_MEMBERSHIP_SETS: dict[str, frozenset[str]] = {
    variable: frozenset() for variable in MARKER_VARIABLE_FOR_KIND.values()
}
