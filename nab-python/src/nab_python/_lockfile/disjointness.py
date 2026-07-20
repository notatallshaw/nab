"""Per-name marker disjointness validation for the PEP 751 emitter.

PEP 751 forbids two ``[[packages]]`` entries with the same name from
firing under one install context.  This module owns the validator
that decides, for every same-name pair, whether their markers can
hold together under the declared install-context universe (declared
environments, with conflict-forbidden co-selections excluded).

The universe is not expressible as a single PEP 508 marker string, so
the check runs through the marker algebra.  For each same-name pair and
each declared environment the validator restricts both markers to the
environment, then walks the conflict-respecting selections of the
membership names the pair references: each conflict set contributes at
most one active member, names in no conflict set are free, and
undeclared names stay absent.  The pair collides when both residuals
still fire together under some selection.  Only the referenced names
enter the walk, so the cost tracks the pair's own markers rather than
the ``powerset(extras) x powerset(groups)`` grid.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from typing import TYPE_CHECKING

from .._conflict_kind import KIND_EXTRA, KIND_GROUP, MARKER_VARIABLE_FOR_KIND
from .._vendor.packaging.markersets import IntractableMarkerSet, MarkerSet
from .._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from collections.abc import Set as AbstractSet

    from .._vendor.packaging.markers import Marker
    from .._vendor.packaging.pylock import Package


__all__ = [
    "DisjointnessError",
]

# Two or more active members of a conflict set is the illegitimate case
# the conflict declaration prunes from the install-context universe.
_MUTUALLY_EXCLUSIVE_LIMIT = 2

# The selection walk fails loud past this many conflict-respecting
# selections, keeping the bounded-failure contract on pathological input.
_MAX_SELECTIONS = 100_000


class DisjointnessError(ValueError):
    """Two same-name ``[[packages]]`` entries can fire under one context.

    PEP 751 forbids ambiguous installer matches.  When more than one
    same-name entry has a marker that holds for the same install
    context, the consumer cannot pick deterministically.  The
    validator surfaces the colliding name and the witness environment.
    When the reported collision reduces to a concrete point it also
    reports the active extras and groups and the colliding versions; when
    that collision is an over-approximated ``contains`` region the algebra
    cannot pin to a point, it reports only the name and environment.
    Either way the producer can change the resolution or declare a
    conflict.
    """


def validate_marker_disjointness(
    packages: Sequence[Package],
    *,
    environments: Mapping[str, Mapping[str, str]],
    extras: Sequence[str],
    groups: Sequence[str],
    exclusive_groups: Sequence[AbstractSet[tuple[str, str]]] = (),
    declared_groups: Sequence[AbstractSet[tuple[str, str]]] = (),
) -> None:
    """Confirm same-name ``[[packages]]`` entries are pairwise disjoint.

    For every same-name pair the validator asks the marker algebra
    whether the two markers can hold together within the declared
    install-context universe.  The universe is the declared
    environments on the environment axes, with the extras and
    dependency-group axes restricted so that no conflict set has two
    active members (``exclusive_groups``) and no name outside the
    declared ``extras`` / ``groups`` is ever selected.  A pair collides
    when, in some declared environment, both markers still fire under a
    conflict-respecting selection of the membership names they
    reference.

    The empty-environments path skips validation: a producer that
    does not declare a universe cannot specify what "all envs" means
    and the validator would over-report when entries have ``marker
    is None``.  Every resolve declares one environment per target, so
    that path is reached only by a caller that builds a
    :class:`~nab_python.lockfile.LockInput` with no targets of its own.

    ``exclusive_groups`` declares mutually-exclusive selections from
    ``[tool.nab].conflicts``: each entry is a set of ``(kind, name)``
    members (``kind`` is ``"extra"`` or ``"group"``) of which at most
    one may be active.  Their co-selection points are removed from the
    universe, so a same-name pair that can fire together only at a
    forbidden co-selection is not counted as a collision.  A collision
    outside every pruned point still raises, hinting at the
    ``conflicts`` key when extras or groups drive the colliding markers.

    ``declared_groups`` carries every conflict set regardless of policy
    so the hint can distinguish an undeclared collision (suggest adding
    a declaration) from one already declared under ``at-least-one``
    (suggest tightening to ``at-most-one`` or ``exactly-one``).
    """
    if not environments:
        return

    by_name: defaultdict[str, list[Package]] = defaultdict(list)
    for pkg in packages:
        by_name[str(pkg.name)].append(pkg)
    same_name_entries = [entries for entries in by_name.values() if len(entries) > 1]
    if not same_name_entries:
        return

    distinct_environments = _distinct_environments(environments)
    exclusion_sets = _canonical_exclusion_sets(exclusive_groups)
    declared_members = _declared_membership_literals(extras, groups)

    for entries in same_name_entries:
        marker_sets = [_marker_set(pkg.marker) for pkg in entries]
        literals = [ms.membership_literals() for ms in marker_sets]
        for i, j in itertools.combinations(range(len(entries)), 2):
            names = literals[i] | literals[j]
            selections = _conflict_respecting_selections(
                names, exclusion_sets, declared_members
            )
            collision = _pair_collision(
                marker_sets[i], marker_sets[j], selections, distinct_environments
            )
            if collision is not None:
                label, env, witness_env = collision
                _raise_collision(
                    entries, marker_sets, label, env, witness_env, declared_groups
                )


def _distinct_environments(
    environments: Mapping[str, Mapping[str, str]],
) -> list[tuple[str, Mapping[str, str]]]:
    """Collapse the environment axis to one label per distinct env dict.

    A conflict-forked universal lock repeats a python/platform under
    several selection labels, so identical env dicts would otherwise be
    re-checked once per fork with no added coverage.  The first label
    seen for each signature is kept so the error message matches the
    pre-dedup iteration order.
    """
    seen: set[tuple[tuple[str, str], ...]] = set()
    distinct: list[tuple[str, Mapping[str, str]]] = []
    for label, env_dict in environments.items():
        signature = tuple(sorted(env_dict.items()))
        if signature in seen:
            continue
        seen.add(signature)
        distinct.append((label, env_dict))
    return distinct


def _marker_set(marker: Marker | None) -> MarkerSet:
    """Return the algebra set for ``marker``; the full set when absent."""
    if marker is None:
        return MarkerSet.full()
    return MarkerSet.from_marker(marker)


def _canonical_exclusion_sets(
    exclusive_groups: Sequence[AbstractSet[tuple[str, str]]],
) -> list[frozenset[tuple[str, str]]]:
    """Project each conflict set to ``(marker variable, canonical name)`` pairs.

    ``exclusive_groups`` members are ``(kind, name)`` with ``kind`` one
    of ``"extra"`` / ``"group"``; the axis a marker tests them on is the
    membership variable, so they are keyed by it here to compare against
    the markers' membership literals under canonicalisation.
    """
    return [
        frozenset(
            (MARKER_VARIABLE_FOR_KIND[kind], canonicalize_name(name))
            for kind, name in members
        )
        for members in exclusive_groups
    ]


def _declared_membership_literals(
    extras: Sequence[str], groups: Sequence[str]
) -> frozenset[tuple[str, str]]:
    """Return every ``(variable, canonical name)`` the universe may select.

    A membership literal outside this set names an extra or group the
    producer never declared, so no install context selects it; the
    universe pins it absent.
    """
    extras_var = MARKER_VARIABLE_FOR_KIND[KIND_EXTRA]
    groups_var = MARKER_VARIABLE_FOR_KIND[KIND_GROUP]
    return frozenset(
        {(extras_var, canonicalize_name(name)) for name in extras}
        | {(groups_var, canonicalize_name(name)) for name in groups}
    )


def _conflict_respecting_selections(
    names: frozenset[tuple[str, str]],
    exclusion_sets: Sequence[AbstractSet[tuple[str, str]]],
    declared_members: frozenset[tuple[str, str]],
) -> list[dict[str, frozenset[str]]]:
    """Enumerate the membership selections the pair's markers can see.

    A selection binds every membership variable the pair references to a
    concrete set of active names.  Only declared names can be active, so
    an undeclared reference stays absent in every selection.  Each
    conflict set that touches the referenced names contributes at most
    one active member; names in no conflict set are free and appear in
    both states.  The count is the product of the per-conflict-set
    choices and the free powerset, guarded by :data:`_MAX_SELECTIONS` so
    a pathological input fails loud rather than iterating unbounded.
    """
    referenced_vars = frozenset(variable for variable, _ in names)
    declared = names & declared_members
    conflict_choices = [
        sorted(members & declared) for members in exclusion_sets if members & declared
    ]
    constrained = {member for choice in conflict_choices for member in choice}
    free = sorted(declared - constrained)

    counts = [len(choice) + 1 for choice in conflict_choices] + [2] * len(free)
    size = 1
    for count in counts:
        size *= count
        if size > _MAX_SELECTIONS:
            msg = f"conflict-respecting selections exceed {_MAX_SELECTIONS}"
            raise IntractableMarkerSet(msg)

    group_options = [
        [frozenset()] + [frozenset({member}) for member in choice]
        for choice in conflict_choices
    ]
    selections: list[dict[str, frozenset[str]]] = []
    for combo in itertools.product(*group_options):
        base = frozenset().union(*combo)
        if not _conflict_respecting(base, exclusion_sets):
            continue
        for mask in range(1 << len(free)):
            active = base | {free[k] for k in range(len(free)) if mask & (1 << k)}
            selections.append(_selection_env(referenced_vars, active))
    return selections


def _conflict_respecting(
    active: frozenset[tuple[str, str]],
    exclusion_sets: Sequence[AbstractSet[tuple[str, str]]],
) -> bool:
    """Whether no conflict set has two active members in ``active``."""
    return all(
        len(members & active) < _MUTUALLY_EXCLUSIVE_LIMIT for members in exclusion_sets
    )


def _selection_env(
    referenced_vars: frozenset[str], active: frozenset[tuple[str, str]]
) -> dict[str, frozenset[str]]:
    """Group an active selection into a per-variable ``restrict`` mapping.

    Every referenced membership variable is bound, even to the empty set,
    so a marker's membership atoms all fold to constants under the
    resulting restriction rather than leaving a residual axis.
    """
    grouped: dict[str, set[str]] = {variable: set() for variable in referenced_vars}
    for variable, name in active:
        grouped[variable].add(name)
    return {variable: frozenset(names) for variable, names in grouped.items()}


def _pair_collision(
    left: MarkerSet,
    right: MarkerSet,
    selections: Sequence[dict[str, frozenset[str]]],
    distinct_environments: Sequence[tuple[str, Mapping[str, str]]],
) -> tuple[str, Mapping[str, str], dict[str, str | frozenset[str]] | None] | None:
    """Return a declared env where both markers can fire together, or None.

    Each marker is restricted to the environment (its environment atoms
    fold to constants, leaving the membership residual).  Each
    conflict-respecting selection binds the membership variables, so the
    pair collides in that environment when both bound residuals stay
    non-empty together.  On a collision the label, the environment, and a
    concrete satisfying assignment (the selection merged with
    :meth:`MarkerSet.witness`) are returned; the witness is ``None`` when
    the first colliding selection is an opaque-``contains``
    over-approximation the algebra cannot reduce to a point.
    """
    for label, env in distinct_environments:
        left_here = left.restrict(env)
        right_here = right.restrict(env)
        for selection in selections:
            collision = left_here.restrict(selection) & right_here.restrict(selection)
            if collision.is_empty():
                continue
            witness = collision.witness()
            if witness is None:
                return label, env, None
            return label, env, {**witness, **selection}
    return None


def _membership_selection(
    witness_env: Mapping[str, str | frozenset[str]], variable: str
) -> frozenset[str]:
    """Return the set the witness selected for a membership variable."""
    value = witness_env.get(variable)
    if isinstance(value, frozenset):
        return value
    return frozenset()


def _raise_collision(
    entries: Sequence[Package],
    marker_sets: Sequence[MarkerSet],
    label: str,
    env: Mapping[str, str],
    witness_env: dict[str, str | frozenset[str]] | None,
    declared_groups: Sequence[AbstractSet[tuple[str, str]]],
) -> None:
    """Raise :class:`DisjointnessError` with a concrete witness context.

    The witness names one install context where the pair holds together,
    so the message reports the environment, the active ``(extras,
    groups)`` selection, the entries that fire there, and their versions.
    When the reported collision has no pinned point (an opaque-``contains``
    over-approximation), the pair is still reported as non-disjoint,
    without a point.
    """
    name = str(entries[0].name)
    if witness_env is None:
        msg = f"{name}: same-name entries are not disjoint under env={label!r}"
        raise DisjointnessError(msg)

    extras_var = MARKER_VARIABLE_FOR_KIND[KIND_EXTRA]
    groups_var = MARKER_VARIABLE_FOR_KIND[KIND_GROUP]
    extra_selection = _membership_selection(witness_env, extras_var)
    group_selection = _membership_selection(witness_env, groups_var)

    # A same-name entry outside the colliding pair may reference a
    # variable the point leaves unbound, so each entry is folded against
    # the point rather than evaluated: a non-empty residual fires here too.
    point: dict[str, str | AbstractSet[str]] = {
        **env,
        **witness_env,
        extras_var: extra_selection,
        groups_var: group_selection,
    }
    matching = [
        (pkg, ms)
        for pkg, ms in zip(entries, marker_sets, strict=True)
        if not ms.restrict(point).is_empty()
    ]

    extra_subset = sorted(extra_selection)
    group_subset = sorted(group_selection)
    versions = sorted(str(p.version) if p.version else "" for p, _ in matching)
    hint = _conflict_hint(
        [ms for _, ms in matching], extra_subset, group_subset, declared_groups
    )
    msg = (
        f"{name}: {len(matching)} entries fire under"
        f" env={label!r} extras={extra_subset!r}"
        f" groups={group_subset!r}: versions={versions}"
        f"{hint}"
    )
    raise DisjointnessError(msg)


def _conflict_hint(
    marker_sets: Sequence[MarkerSet],
    extra_subset: Sequence[str],
    group_subset: Sequence[str],
    declared_groups: Sequence[AbstractSet[tuple[str, str]]] = (),
) -> str:
    """Return a one-line hint about declaring a conflict, when relevant.

    Relevant means a membership variable actually drives the witness
    point: a referenced extra or dependency group is active in the
    colliding context, so declaring the pair mutually exclusive in
    ``[tool.nab].conflicts`` could prune that point.  A purely
    environment-driven collision (no membership variable referenced, or
    the witness selects none of the referenced ones) gets no hint
    because a conflict declaration would not help.

    When ``declared_groups`` already covers the active members, the
    hint instead suggests tightening the policy: an ``at-least-one``
    declaration permits co-selection, so the validator still raises and
    the user has to switch to an exclusive policy to prune the point.
    """
    extras_driven = _membership_drives_point(
        marker_sets, MARKER_VARIABLE_FOR_KIND[KIND_EXTRA], extra_subset
    )
    groups_driven = _membership_drives_point(
        marker_sets, MARKER_VARIABLE_FOR_KIND[KIND_GROUP], group_subset
    )
    if not (extras_driven or groups_driven):
        return ""

    active = {(KIND_EXTRA, canonicalize_name(n)) for n in extra_subset} | {
        (KIND_GROUP, canonicalize_name(n)) for n in group_subset
    }
    already_declared = any(
        sum(1 for kind, name in declared if (kind, name) in active) >= 2  # noqa: PLR2004
        for declared in declared_groups
    )
    if already_declared:
        return (
            ". These members are declared in [tool.nab].conflicts under a"
            " policy that permits co-selection; switch to at-most-one or"
            " exactly-one to prune the colliding context"
        )
    return (
        ". If these are intentionally mutually exclusive, declare them in"
        " [tool.nab].conflicts so the colliding context is pruned"
    )


def _membership_drives_point(
    marker_sets: Sequence[MarkerSet], variable: str, subset: Sequence[str]
) -> bool:
    """Return True when a referenced ``variable`` literal is active here.

    A membership variable drives the witness only when the witness
    ``subset`` is non-empty and intersects the literals the colliding
    markers test for membership in ``variable`` (compared under
    canonicalisation, matching the universe's axis restriction).
    """
    referenced = {
        name
        for marker_set in marker_sets
        for var, name in marker_set.membership_literals()
        if var == variable
    }
    if not referenced:
        return False
    active = {canonicalize_name(name) for name in subset}
    return any(name in active for name in referenced)
