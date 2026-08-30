"""Declared conflicts, and the forks a selection engages.

``[tool.nab].conflicts`` names sets of extras and dependency groups that
cannot be installed together.
"""

from __future__ import annotations

import enum
import itertools
from typing import TYPE_CHECKING

from typing_extensions import override

from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider.conflict_kind import KIND_EXTRA, KIND_GROUP
from nab_provider.errors import ConfigError

from .value import ValueType

if TYPE_CHECKING:
    from collections.abc import Sequence
    from collections.abc import Set as AbstractSet

__all__ = [
    "MIN_ENGAGED_MEMBERS",
    "ConflictFork",
    "ConflictKind",
    "ConflictMember",
    "ConflictPolicy",
    "ConflictSelectionError",
    "ConflictSet",
    "conflict_exclusion_groups",
    "conflict_forks",
    "conflict_member_groups",
    "validate_conflict_exclusions",
    "validate_conflict_minimums",
]


class ConflictPolicy(enum.Enum):
    """How exclusive the members of a :class:`ConflictSet` are.

    Mirrors Gentoo's ``REQUIRED_USE`` group operators.  ``AT_MOST_ONE``
    (``??``) is the default for a bare uv-style set: the members are
    mutually exclusive but selecting none is fine, which suits opt-in
    extras.  ``EXACTLY_ONE`` (``^^``) additionally requires one to be
    chosen.  ``AT_LEAST_ONE`` (``||``) only forbids the empty
    selection; it is rarely useful for extras and is included for
    completeness.
    """

    AT_MOST_ONE = "at-most-one"
    EXACTLY_ONE = "exactly-one"
    AT_LEAST_ONE = "at-least-one"


class ConflictKind(enum.Enum):
    """Whether a :class:`ConflictMember` names an extra or a group."""

    EXTRA = KIND_EXTRA
    GROUP = KIND_GROUP


class ConflictMember(ValueType):
    """One side of a conflict: a named extra or dependency group.

    ``name`` is stored canonicalised (PEP 685 for extras, PEP 735 for
    groups) so a selection compares equal regardless of how the user
    spelled it.  An extra and a group sharing a name are distinct
    members, matching uv's package-qualified model.
    """

    __slots__ = __match_args__ = ("kind", "name")

    kind: ConflictKind
    name: str

    def __init__(self, kind: ConflictKind, name: str) -> None:
        """Record an extra or group ``name`` the caller has canonicalised."""
        self.kind = kind
        self.name = name

    @override
    def __str__(self) -> str:
        """Render as ``extra 'cpu'`` / ``group 'black22'`` for messages."""
        return f"{self.kind.value} {self.name!r}"


class ConflictSet(ValueType):
    """A set of mutually-exclusive members with an exclusivity policy."""

    __slots__ = __match_args__ = ("members", "policy")

    members: tuple[ConflictMember, ...]
    policy: ConflictPolicy

    def __init__(
        self,
        members: tuple[ConflictMember, ...],
        policy: ConflictPolicy = ConflictPolicy.AT_MOST_ONE,
    ) -> None:
        """Record the members ``policy`` makes exclusive."""
        self.members = members
        self.policy = policy

    @override
    def __str__(self) -> str:
        """Render as ``at-most-one (extra 'cpu', extra 'gpu')`` for messages."""
        joined = ", ".join(str(m) for m in self.members)
        return f"{self.policy.value} ({joined})"


def conflict_exclusion_groups(
    conflicts: Sequence[ConflictSet],
) -> tuple[frozenset[tuple[str, str]], ...]:
    """Project conflict sets to the neutral exclusion form the lockfile uses.

    The disjointness validator consumes a sequence of member sets, of
    which at most one member may be active in any install context.
    Only :attr:`ConflictPolicy.AT_MOST_ONE` and
    :attr:`ConflictPolicy.EXACTLY_ONE` forbid co-selection, so only
    those contribute; :attr:`ConflictPolicy.AT_LEAST_ONE` constrains the
    empty selection, not co-selection, and is omitted.  Each member
    becomes a ``(kind, canonical_name)`` pair.
    """
    return tuple(
        frozenset((m.kind.value, m.name) for m in cs.members)
        for cs in conflicts
        if cs.policy is not ConflictPolicy.AT_LEAST_ONE
    )


def conflict_member_groups(
    conflicts: Sequence[ConflictSet],
) -> tuple[frozenset[tuple[str, str]], ...]:
    """Project every conflict set (any policy) to ``(kind, name)`` member sets.

    Distinct from :func:`conflict_exclusion_groups`, which drops
    :attr:`ConflictPolicy.AT_LEAST_ONE` because that policy permits
    co-selection.  The disjointness validator uses this projection to
    tell already-declared collisions from undeclared ones when shaping
    the hint.
    """
    return tuple(
        frozenset((m.kind.value, m.name) for m in cs.members) for cs in conflicts
    )


class ConflictFork(ValueType):
    """One fork of a conflict-driven universal resolve.

    ``selection`` is the active conflicting members as ``(kind, name)``
    pairs.  ``active_extras`` and ``active_groups`` are the selections
    this fork resolves with, the non-conflicting ones plus its own chosen
    members, and hold only names the project declares.  A name
    ``[tool.nab]`` configures is on ``active_configured`` instead.  An
    unforked resolve is a single fork with an empty ``selection``.
    """

    __slots__ = __match_args__ = (
        "selection",
        "active_extras",
        "active_groups",
        "active_configured",
    )

    selection: tuple[tuple[str, str], ...]
    active_extras: tuple[str, ...]
    active_groups: tuple[str, ...]
    active_configured: tuple[str, ...]
    """The configured group names (``base-group``, ``build-group``) this
    fork carries.  Their requirements come from a pyproject table rather
    than from ``[dependency-groups]``."""

    def __init__(
        self,
        selection: tuple[tuple[str, str], ...],
        active_extras: tuple[str, ...],
        active_groups: tuple[str, ...],
        active_configured: tuple[str, ...] = (),
    ) -> None:
        """Record one fork of a conflict-driven resolve."""
        self.selection = selection
        self.active_extras = active_extras
        self.active_groups = active_groups
        self.active_configured = active_configured


# Two active selections engage the set's exclusivity.  Not the structural
# minimum a declaration must list, which is the same number by coincidence.
MIN_ENGAGED_MEMBERS = 2


def conflict_forks(
    selected_extras: Sequence[str],
    selected_groups: Sequence[str],
    conflicts: Sequence[ConflictSet],
    configured_groups: Sequence[str] = (),
) -> list[ConflictFork]:
    """Split a selection into one fork per mutually-exclusive combination.

    A conflict set is *engaged* when the selection activates two or more
    of its members under an exclusivity policy (at-most-one or
    exactly-one); only an engaged set forces a fork.  Each engaged set
    contributes one chosen member per fork, and the forks are the
    cartesian product across engaged sets.  Members of engaged sets are
    dropped from the shared base; non-conflicting selections stay active
    in every fork.  With no engaged set the result is a single unforked
    fork carrying the whole selection.

    A declared group is active when the selection names it.  A
    ``configured_groups`` name is active whenever it is set, since the
    context it names is part of every resolve rather than something a run
    asks for, so a set naming one engages on every run.  Those names come
    back on :attr:`ConflictFork.active_configured` rather than
    ``active_groups``, which stays what the ``[dependency-groups]`` loader
    can resolve.

    Names compare and emit canonicalised; the extra and group loaders
    normalise on lookup, so a canonical active set resolves the same
    requirements the user's spelling would.
    """
    base_extras = [canonicalize_name(e) for e in selected_extras]
    base_groups = [canonicalize_name(g) for g in selected_groups]
    configured = list(dict.fromkeys(canonicalize_name(g) for g in configured_groups))
    configured_set = set(configured)
    extra_set = set(base_extras)
    group_set = set(base_groups) | configured_set

    # Collect the engaged sets (2+ selected members) and the members to
    # drop from the shared base; each engaged set becomes a fork axis.
    engaged: list[list[ConflictMember]] = []
    drop_extras: set[str] = set()
    drop_groups: set[str] = set()
    for conflict_set in conflicts:
        if conflict_set.policy is ConflictPolicy.AT_LEAST_ONE:
            continue
        members = [
            m for m in conflict_set.members if _member_active(m, extra_set, group_set)
        ]
        if len(members) < MIN_ENGAGED_MEMBERS:
            continue
        engaged.append(members)
        for member in members:
            target = drop_extras if member.kind is ConflictKind.EXTRA else drop_groups
            target.add(member.name)

    if not engaged:
        return [
            ConflictFork(
                selection=(),
                active_extras=tuple(base_extras),
                active_groups=tuple(base_groups),
                active_configured=tuple(configured),
            )
        ]

    # One fork per choice of a single member from each engaged set.
    rest_extras = [e for e in base_extras if e not in drop_extras]
    rest_groups = [g for g in base_groups if g not in drop_groups]
    rest_configured = [g for g in configured if g not in drop_groups]
    forks: list[ConflictFork] = []
    for combo in itertools.product(*engaged):
        chosen_extras = [m.name for m in combo if m.kind is ConflictKind.EXTRA]
        chosen = [m.name for m in combo if m.kind is ConflictKind.GROUP]
        chosen_groups = [g for g in chosen if g not in configured_set]
        chosen_configured = [g for g in chosen if g in configured_set]
        forks.append(
            ConflictFork(
                selection=tuple(sorted((m.kind.value, m.name) for m in combo)),
                active_extras=tuple(rest_extras + chosen_extras),
                active_groups=tuple(rest_groups + chosen_groups),
                active_configured=tuple(rest_configured + chosen_configured),
            )
        )
    return forks


class ConflictSelectionError(ConfigError):
    """A requested extra/group selection violates a declared conflict.

    Raised when one resolve cannot serve the selection: a project
    resolving for a single environment cannot install two
    mutually-exclusive members at once.  A declared matrix forks the
    resolve instead of raising, and only raises when one fork still
    reaches two members (through an umbrella extra, say).
    """


def _member_active(
    member: ConflictMember,
    active_extras: AbstractSet[str],
    active_groups: AbstractSet[str],
) -> bool:
    """Return True when ``member`` is in the selected extras/groups."""
    if member.kind is ConflictKind.EXTRA:
        return member.name in active_extras
    return member.name in active_groups


def validate_conflict_minimums(
    conflicts: Sequence[ConflictSet],
    selected_extras: Sequence[str],
    selected_groups: Sequence[str],
) -> None:
    """Raise when a require-one set has no active member.

    Enforces only the "must select one" policies: an exactly-one set
    and an at-least-one set each require at least one active member.
    Names compare under canonicalisation.  Universal mode calls this to
    apply the minimums without the co-selection rejection, which it
    handles by forking instead.
    """
    active_extras = {canonicalize_name(e) for e in selected_extras}
    active_groups = {canonicalize_name(g) for g in selected_groups}
    for conflict_set in conflicts:
        any_active = any(
            _member_active(m, active_extras, active_groups)
            for m in conflict_set.members
        )
        if any_active:
            continue
        if conflict_set.policy is ConflictPolicy.AT_MOST_ONE:
            continue
        members = ", ".join(str(m) for m in conflict_set.members)
        quantifier = (
            "exactly one"
            if conflict_set.policy is ConflictPolicy.EXACTLY_ONE
            else "at least one"
        )
        msg = (
            f"{quantifier} of {members} must be selected: declared"
            f" {conflict_set.policy.value} in [tool.nab].conflicts"
        )
        raise ConflictSelectionError(msg)


def validate_conflict_exclusions(
    conflicts: Sequence[ConflictSet],
    selected_extras: Sequence[str],
    selected_groups: Sequence[str],
) -> None:
    """Raise when a selection co-activates two members of an exclusive set.

    An at-most-one or exactly-one set cannot have two active members at
    once.  Names compare under canonicalisation.  Universal mode applies
    this per fork, against the self-reference- and include-expanded
    active set, to catch members an umbrella selection reaches only
    transitively (one fork cannot serve two of them disjointly).
    """
    active_extras = {canonicalize_name(e) for e in selected_extras}
    active_groups = {canonicalize_name(g) for g in selected_groups}
    exclusive = {ConflictPolicy.AT_MOST_ONE, ConflictPolicy.EXACTLY_ONE}
    for conflict_set in conflicts:
        active = [
            m
            for m in conflict_set.members
            if _member_active(m, active_extras, active_groups)
        ]
        if len(active) > 1 and conflict_set.policy in exclusive:
            chosen = ", ".join(str(m) for m in active)
            msg = (
                f"{chosen} cannot be selected together: declared mutually"
                f" exclusive ({conflict_set.policy.value}) in [tool.nab].conflicts"
            )
            raise ConflictSelectionError(msg)
