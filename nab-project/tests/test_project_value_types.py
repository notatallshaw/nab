"""The conflict and input value types nab-project declares.

``ConflictSet``, ``ConflictMember``, ``ConflictFork`` and ``ResolveInputs``
are written against :class:`nab_project.value.ValueType` rather than
``@dataclass``, so the base's equality and each type's own rendering are
pinned here.  A new subclass also needs a case in the umbrella suite's
``tests/test_value_types.py``.
"""

from __future__ import annotations

import pytest

from nab_project.conflicts import (
    ConflictFork,
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
)
from nab_project.inputs import ResolveInputs
from nab_project.value import ValueType

_MEMBERS = (
    ConflictMember(ConflictKind.EXTRA, "cpu"),
    ConflictMember(ConflictKind.EXTRA, "gpu"),
)

_INSTANCES = [
    ConflictMember(ConflictKind.EXTRA, "cpu"),
    ConflictSet(_MEMBERS),
    ConflictFork((("extra", "cpu"),), ("cpu",), (), ()),
    ResolveInputs(),
]


def test_a_conflict_set_renders_its_policy_and_members() -> None:
    """The form an error message and ``nab config list`` both print."""
    assert str(ConflictSet(_MEMBERS)) == "at-most-one (extra 'cpu', extra 'gpu')"


@pytest.mark.parametrize(
    "instance", _INSTANCES, ids=lambda instance: type(instance).__name__
)
def test_equality_declines_another_type(instance: ValueType) -> None:
    """A comparison against anything else defers rather than answering False."""
    assert instance.__eq__("not a value type") is NotImplemented


def test_a_conflict_set_holds_a_policy_of_its_own() -> None:
    """``policy`` is a field, so a set can carry one other than the default."""
    required = ConflictSet(_MEMBERS, policy=ConflictPolicy.EXACTLY_ONE)

    assert required.policy is ConflictPolicy.EXACTLY_ONE
    assert ConflictSet(_MEMBERS).policy is ConflictPolicy.AT_MOST_ONE


def test_equal_conflict_sets_hash_alike() -> None:
    """The base hashes every field, so two equal sets land in one bucket."""
    assert len({ConflictSet(_MEMBERS), ConflictSet(_MEMBERS)}) == 1
    assert hash(ConflictSet(_MEMBERS)) != hash(
        ConflictSet(_MEMBERS, policy=ConflictPolicy.EXACTLY_ONE)
    )
