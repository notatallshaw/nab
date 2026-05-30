"""Per-name marker disjointness validation for the PEP 751 emitter.

PEP 751 forbids two ``[[packages]]`` entries with the same name from
firing under one install context.  This module owns the validator
that enumerates the install-context universe (environments x valid
extras/groups combinations) and reports any pair that collide, plus
the bookkeeping helpers that prune the axes to the marker variables a
same-name candidate actually references.  Declared conflicts shrink
the combination space directly: a set with N mutually-exclusive
members contributes only N+1 outcomes (none, or each member alone),
not the 2^N raw powerset.
"""

from __future__ import annotations

import functools
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from .._conflict_kind import KIND_EXTRA, KIND_GROUP, MARKER_VARIABLE_FOR_KIND
from .._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from collections.abc import Set as AbstractSet

    from .._vendor.packaging.markers import Marker
    from .._vendor.packaging.pylock import Package


__all__ = [
    "DisjointnessError",
]

# Two or more active members of a conflict set is the illegitimate case
# the conflict declaration prunes from the install-context universe.
_MUTUALLY_EXCLUSIVE_LIMIT = 2


class DisjointnessError(ValueError):
    """Two same-name ``[[packages]]`` entries can fire under one context.

    PEP 751 forbids ambiguous installer matches.  When more than one
    same-name entry has a marker that holds for the same install
    context, the consumer cannot pick deterministically.  The
    validator surfaces the colliding name, the witness context
    (environment + extras + groups), and the colliding versions so
    the producer can either change the resolution or declare a
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

    Builds the universe of install contexts as the cartesian product
    of declared environments and the conflict-respecting combinations
    of declared ``extras`` and ``dependency_groups`` (see
    :func:`_enumerate_valid_points`).  For every point in that
    universe and every package name, evaluates each candidate entry's
    marker.  When two or more entries hold for the same point, raises
    :class:`DisjointnessError` with the witness.

    The empty-environments path skips validation: a producer that
    does not declare a universe cannot specify what "all envs" means
    and the validator would over-report when entries have ``marker
    is None``.  Callers that emit per-tuple markers (universal
    mode) populate ``LockInput.tuple_environments`` from the
    matrix.

    Powerset pruning: ``extras`` and ``dependency_groups`` are PEP
    685 / PEP 735 marker variables that markers may or may not
    reference.  Materialising the full ``2**N`` powerset is
    intractable for projects that declare many extras.  Inspect each
    marker's string form via :func:`_referenced_membership_names`
    and only iterate the powerset over names that any marker
    actually mentions.  When no marker references a variable, that
    powerset collapses to ``{()}`` and the cartesian explosion
    disappears.

    ``exclusive_groups`` declares mutually-exclusive selections from
    ``[tool.nab].conflicts``: each entry is a set of ``(kind, name)``
    members (``kind`` is ``"extra"`` or ``"group"``) of which at most
    one may be active.  Contexts activating two or more members of a
    set are pruned before the collision check, so a universal lock can
    carry one fork per conflicting extra/group under bare
    ``'name' in extras`` markers.  A collision outside every pruned
    point still raises, hinting at the ``conflicts`` key when extras or
    groups drive the colliding markers.

    ``declared_groups`` carries every conflict set regardless of policy
    so the hint can distinguish an undeclared collision (suggest adding
    a declaration) from one already declared under ``at_least_one``
    (suggest tightening to ``at_most_one`` or ``exactly_one``).
    """
    if not environments:
        return
    by_name: defaultdict[str, list[Package]] = defaultdict(list)
    for pkg in packages:
        by_name[str(pkg.name)].append(pkg)
    same_name_entries = [entries for entries in by_name.values() if len(entries) > 1]
    if not same_name_entries:
        return
    candidate_markers = [pkg.marker for entries in same_name_entries for pkg in entries]

    extras_var = MARKER_VARIABLE_FOR_KIND[KIND_EXTRA]
    groups_var = MARKER_VARIABLE_FOR_KIND[KIND_GROUP]

    relevant_extras = _restrict_to_referenced(extras, candidate_markers, extras_var)
    relevant_groups = _restrict_to_referenced(groups, candidate_markers, groups_var)
    points = list(
        _enumerate_valid_points(relevant_extras, relevant_groups, exclusive_groups)
    )
    distinct_environments = _distinct_environments(environments)

    # Iterate env x point outermost so the install context dict is built
    # once per (env, point) and reused across every same-name entry group.
    # The earlier per-entry-group rebuild scaled M*D*P dicts where M is the
    # same-name package count; conflict-fork locks make M scale with the
    # universe size.
    for env_label, env_dict in distinct_environments:
        for extra_subset, group_subset in points:
            context: dict[str, str | AbstractSet[str]] = dict(env_dict)
            context[extras_var] = frozenset(extra_subset)
            context[groups_var] = frozenset(group_subset)
            for entries in same_name_entries:
                matching = [
                    pkg for pkg in entries if _marker_holds(pkg.marker, context)
                ]
                if len(matching) <= 1:
                    continue
                name = str(entries[0].name)
                versions = sorted(str(p.version) if p.version else "" for p in matching)
                hint = _conflict_hint(
                    [p.marker for p in matching],
                    extra_subset,
                    group_subset,
                    declared_groups,
                )
                msg = (
                    f"{name}: {len(matching)} entries fire under"
                    f" env={env_label!r} extras={sorted(extra_subset)!r}"
                    f" groups={sorted(group_subset)!r}: versions={versions}"
                    f"{hint}"
                )
                raise DisjointnessError(msg)


def _distinct_environments(
    environments: Mapping[str, Mapping[str, str]],
) -> list[tuple[str, Mapping[str, str]]]:
    """Collapse the environment axis to one label per distinct env dict.

    A conflict-forked universal lock repeats a python/platform under
    several selection labels, so identical env dicts would otherwise be
    re-evaluated once per fork with no added coverage.  The first label
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


def _enumerate_valid_points(
    relevant_extras: Sequence[str],
    relevant_groups: Sequence[str],
    exclusive_groups: Sequence[AbstractSet[tuple[str, str]]],
) -> Iterable[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Yield every (extras_subset, groups_subset) the conflicts allow.

    The full powerset is ``2^(E+G)`` and overwhelmingly dominated by
    points a declared conflict already forbids (any subset that picks
    two members of one mutually-exclusive set).  Generating then
    filtering wastes the exponent: a set with N members keeps only
    N+1 of its 2^N subsets.  This enumerates by backtracking: at each
    item, the include branch is skipped as soon as one of its
    exclusion sets already has an active member, so the search visits
    exactly the surviving subsets.

    Names are compared under :func:`canonicalize_name` against the
    declared conflict members; subsets are emitted as sorted tuples
    drawn from the input order so the downstream collision check sees
    them in a stable shape.
    """
    extras = sorted(set(relevant_extras))
    groups = sorted(set(relevant_groups))
    items: list[tuple[str, str]] = [(KIND_EXTRA, e) for e in extras] + [
        (KIND_GROUP, g) for g in groups
    ]

    # Per item, the indices of every exclusion set whose membership it
    # belongs to.  Names compare canonicalised on both sides.
    item_sets: list[list[int]] = []
    for kind, name in items:
        canonical = canonicalize_name(name)
        sets_for_item: list[int] = []
        for i, members in enumerate(exclusive_groups):
            for member_kind, member_name in members:
                if member_kind == kind and canonicalize_name(member_name) == canonical:
                    sets_for_item.append(i)
                    break
        item_sets.append(sets_for_item)

    active_per_set = [0] * len(exclusive_groups)
    extras_buf: list[str] = []
    groups_buf: list[str] = []

    def recurse(idx: int) -> Iterable[tuple[tuple[str, ...], tuple[str, ...]]]:
        if idx == len(items):
            yield (tuple(extras_buf), tuple(groups_buf))
            return

        # Exclude branch is always valid.
        yield from recurse(idx + 1)

        # Include branch is pruned as soon as a set has its single
        # allowed member already active.
        if any(
            active_per_set[i] >= _MUTUALLY_EXCLUSIVE_LIMIT - 1 for i in item_sets[idx]
        ):
            return
        kind, name = items[idx]
        target = extras_buf if kind == KIND_EXTRA else groups_buf
        target.append(name)
        for i in item_sets[idx]:
            active_per_set[i] += 1
        yield from recurse(idx + 1)
        for i in item_sets[idx]:
            active_per_set[i] -= 1
        target.pop()

    yield from recurse(0)


def _conflict_hint(
    markers: Sequence[Marker | None],
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
    hint instead suggests tightening the policy: an ``at_least_one``
    declaration permits co-selection, so the validator still raises and
    the user has to switch to an exclusive policy to prune the point.
    """
    extras_driven = _membership_drives_point(
        markers, MARKER_VARIABLE_FOR_KIND[KIND_EXTRA], extra_subset
    )
    groups_driven = _membership_drives_point(
        markers, MARKER_VARIABLE_FOR_KIND[KIND_GROUP], group_subset
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
            " policy that permits co-selection; switch to at_most_one or"
            " exactly_one to prune the colliding context"
        )
    return (
        ". If these are intentionally mutually exclusive, declare them in"
        " [tool.nab].conflicts so the colliding context is pruned"
    )


def _membership_drives_point(
    markers: Sequence[Marker | None], variable: str, subset: Sequence[str]
) -> bool:
    """Return True when a referenced ``variable`` literal is active here.

    A membership variable drives the witness only when the witness
    ``subset`` is non-empty and intersects the literals the colliding
    markers test for membership in ``variable`` (compared under
    canonicalisation, matching the powerset axis restriction).
    """
    referenced, _ = _referenced_membership_names(markers, variable)
    if not referenced:
        return False
    active = {canonicalize_name(name) for name in subset}
    return any(canonicalize_name(name) in active for name in referenced)


@functools.cache
def _membership_name_pattern(variable: str) -> re.Pattern[str]:
    """Compile (and cache) the regex matching ``"NAME" [not] in <variable>``.

    Used to detect which extras / dependency-group names a marker
    references.  PEP 508 reserves ``extras`` (PEP 685) and
    ``dependency_groups`` (PEP 735) as bare-token marker variables,
    so a literal-vs-variable membership test always serialises as
    ``"<lit>" [not] in <var>`` after :func:`Marker.__str__`
    normalisation.  Rejected alternatives: walking
    ``Marker._markers`` (private packaging API) or vendoring
    ``Marker.as_ast()`` from packaging PR #1145 (still open; loses
    operand-vs-variable distinction in the proposed shape).
    """
    return re.compile(
        r"""(['"])([^'"]*)\1\s+(?:not\s+)?in\s+""" + re.escape(variable) + r"\b",
        re.IGNORECASE,
    )


def _referenced_membership_names(
    markers: Iterable[Marker | None], variable: str
) -> tuple[frozenset[str], bool]:
    """Return the literals any marker tests for membership in ``variable``.

    ``variable`` is one of ``"extras"`` or ``"dependency_groups"``;
    a literal ``"foo"`` referenced as ``"foo" in extras`` (or its
    ``not in`` form) lands in the result.  The regex matches
    ``str(marker)`` because :class:`Marker` re-emits a canonical
    form where the literal is always quoted and the variable is
    always a bare token.

    Also returns a flag set when *any* marker contains the bare
    ``variable`` token; callers can use it to detect unusual marker
    shapes that mention the variable but did not match the regex
    (a future PEP form, a comparison flipped to put the variable
    on the LHS, etc.) and fall back to a safe over-approximation.
    """
    pattern = _membership_name_pattern(variable)
    bare_token = re.compile(r"\b" + re.escape(variable) + r"\b")
    found: set[str] = set()
    has_bare_reference = False
    for marker in markers:
        if marker is None:
            continue
        text = str(marker)
        if bare_token.search(text):
            has_bare_reference = True
        for match in pattern.finditer(text):
            found.add(match.group(2))
    return frozenset(found), has_bare_reference


def _restrict_to_referenced(
    declared: Sequence[str],
    markers: Sequence[Marker | None],
    variable: str,
) -> tuple[str, ...]:
    """Restrict ``declared`` to the subset that any marker references.

    ``variable`` is the marker token (``"extras"`` or
    ``"dependency_groups"``).  The intersection of declared names
    and regex-matched literals shrinks the powerset axis to what
    the markers actually depend on.  Both sides are normalised with
    :func:`canonicalize_name` before intersecting, because PEP 685 and
    PEP 735 compare names under normalisation but :meth:`Marker.__str__`
    re-emits the membership literal verbatim; a case- or separator-only
    difference would otherwise drop the name and miss its collision.
    When the bare token appears in some marker but no literals were
    extracted (an unusual form the regex did not anticipate), fall back
    to the full declared list so the validator over-approximates rather
    than silently misses a collision.
    """
    referenced, has_bare = _referenced_membership_names(markers, variable)
    if not has_bare:
        return ()
    if not referenced:
        return tuple(declared)
    normalized = {canonicalize_name(name) for name in referenced}
    return tuple(name for name in declared if canonicalize_name(name) in normalized)


def _marker_holds(
    marker: Marker | None, context: Mapping[str, str | AbstractSet[str]]
) -> bool:
    """Return True when ``marker`` is absent or evaluates True under ``context``."""
    if marker is None:
        return True
    # ``Marker.evaluate`` treats the environment as read-only, so no
    # defensive copy is needed here.
    return bool(marker.evaluate(context))
