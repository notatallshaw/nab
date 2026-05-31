"""Conflict-kind constants and PEP 508 marker-variable mapping.

A leaf module that :mod:`nab_python.config`, :mod:`nab_python.universal.matrix`,
and :mod:`nab_python._lockfile.disjointness` can import without forming a
cycle.  :class:`nab_python.config.ConflictKind` takes its enum values from
``KIND_EXTRA`` / ``KIND_GROUP`` so a rename here flows to every consumer.
"""

from __future__ import annotations

KIND_EXTRA = "extra"
KIND_GROUP = "group"

# Membership of a conflict-fork member emits ``'name' in <variable>`` on
# the per-package marker; this mapping is the (kind -> variable) contract
# the universal matrix and the disjointness validator share.
MARKER_VARIABLE_FOR_KIND = {
    KIND_EXTRA: "extras",
    KIND_GROUP: "dependency_groups",
}
