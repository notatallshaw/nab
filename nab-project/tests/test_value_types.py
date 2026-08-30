"""The hand-written value types in config and config_sources.

:mod:`nab_project.config` and :mod:`nab_project.config_sources` write
their value types against :class:`nab_project._value.ValueType` rather
than applying ``@dataclass(slots=True)``.  Field order, equality,
hashing, repr and the defaults are what the rest of nab reads off them,
so each one is pinned here instead of being left to the decorator.
"""

from __future__ import annotations

import copy
import pickle
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from nab_project import config, config_sources
from nab_project._value import ValueType
from nab_project.config import (
    ConflictFork,
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
    MatrixConfig,
)
from nab_project.config_sources import (
    EffectiveValue,
    Layer,
    OptionSpec,
    Origin,
    RejectedLayer,
    Scope,
    SourceKind,
    SourceRoots,
)
from nab_provider.tags import PlatformSpec


def _parse(value: Any, where: str) -> Any:
    return value


def _render(value: Any) -> str:
    return str(value)


def _round_trip_pickle(instance: Any) -> Any:
    """A pickled and restored copy of ``instance``."""
    return pickle.loads(pickle.dumps(instance))  # noqa: S301


ORIGIN = Origin(SourceKind.PYPROJECT, "/p/pyproject.toml")
OTHER_ORIGIN = Origin(SourceKind.USER_TOML, "/u/nab.toml")
SPEC = OptionSpec(
    key="mode",
    scope=Scope.PROJECT,
    type_label="enum(specific|universal)",
    default="specific",
    env_var="NAB_MODE",
    cli_flag="--project-mode",
    cli_param="project_mode",
    parse=_parse,
    render=_render,
)


class Case(NamedTuple):
    """One value type, a full set of field values, and a different value each.

    ``fields`` is in declaration order, which is what ``__match_args__``
    and ``__slots__`` are checked against.  ``alternates`` supplies one
    differing value per field so equality can be varied a field at a
    time.  ``hashable`` is False where one of the field values is
    itself unhashable.
    """

    cls: type[ValueType]
    fields: dict[str, Any]
    alternates: dict[str, Any]
    hashable: bool = True

    @property
    def id(self) -> str:
        return self.cls.__name__

    def build(self, **overrides: Any) -> ValueType:
        return self.cls(**{**self.fields, **overrides})


CASES = [
    Case(
        MatrixConfig,
        {
            "python": "3.12",
            "platforms": (PlatformSpec("linux_x86_64"),),
            "python_order": "asc",
            "python_patches": None,
            "implementations": ("cpython",),
        },
        {
            "python": "3.13",
            "platforms": (PlatformSpec("macos_arm64"),),
            "python_order": "desc",
            "python_patches": {"3.12": "3.12.4"},
            "implementations": ("pypy",),
        },
    ),
    Case(
        ConflictMember,
        {"kind": ConflictKind.EXTRA, "name": "cpu"},
        {"kind": ConflictKind.GROUP, "name": "gpu"},
    ),
    Case(
        ConflictSet,
        {
            "members": (ConflictMember(ConflictKind.EXTRA, "cpu"),),
            "policy": ConflictPolicy.AT_MOST_ONE,
        },
        {
            "members": (ConflictMember(ConflictKind.GROUP, "cpu"),),
            "policy": ConflictPolicy.EXACTLY_ONE,
        },
    ),
    Case(
        ConflictFork,
        {
            "selection": (("extra", "cpu"),),
            "active_extras": ("cpu",),
            "active_groups": ("dev",),
            "active_configured": ("base",),
        },
        {
            "selection": (("extra", "gpu"),),
            "active_extras": ("gpu",),
            "active_groups": ("docs",),
            "active_configured": ("build",),
        },
    ),
    Case(
        OptionSpec,
        {
            "key": "mode",
            "scope": Scope.PROJECT,
            "type_label": "enum(specific|universal)",
            "default": "specific",
            "env_var": "NAB_MODE",
            "cli_flag": "--project-mode",
            "cli_param": "project_mode",
            "parse": _parse,
            "render": _render,
        },
        {
            "key": "resolution",
            "scope": Scope.USER,
            "type_label": "str",
            "default": "universal",
            "env_var": None,
            "cli_flag": None,
            "cli_param": None,
            "parse": _render,
            "render": _parse,
        },
    ),
    Case(
        Origin,
        {"kind": SourceKind.PYPROJECT, "label": "/p/pyproject.toml"},
        {"kind": SourceKind.USER_TOML, "label": "/u/nab.toml"},
    ),
    Case(
        Layer,
        {"origin": ORIGIN, "values": {"mode": "specific"}},
        {"origin": OTHER_ORIGIN, "values": {"mode": "universal"}},
        hashable=False,
    ),
    Case(
        RejectedLayer,
        {"origin": ORIGIN, "key": "mode", "reason": "not allowed here"},
        {"origin": OTHER_ORIGIN, "key": "resolution", "reason": "unknown"},
    ),
    Case(
        EffectiveValue,
        {
            "spec": SPEC,
            "value": "specific",
            "origin": ORIGIN,
            "stack": ((ORIGIN, "specific"),),
            "rejected": (RejectedLayer(ORIGIN, "mode", "unknown"),),
        },
        {
            "spec": OptionSpec(
                key="resolution",
                scope=Scope.USER,
                type_label="str",
                default="highest",
                env_var=None,
                cli_flag=None,
                cli_param=None,
                parse=_parse,
                render=_render,
            ),
            "value": "universal",
            "origin": OTHER_ORIGIN,
            "stack": (),
            "rejected": (),
        },
    ),
    Case(
        SourceRoots,
        {
            "system_toml": Path("/etc/nab.toml"),
            "user_toml": Path("/u/nab.toml"),
            "project_dir": Path("/p"),
            "pyproject": Path("/p/pyproject.toml"),
        },
        {
            "system_toml": None,
            "user_toml": None,
            "project_dir": Path("/q"),
            "pyproject": None,
        },
    ),
]

BY_ID = pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])

DEFAULTS = [
    (
        MatrixConfig,
        {"python": "3.12", "platforms": ()},
        {
            "python_order": "asc",
            "python_patches": None,
            "implementations": ("cpython",),
        },
    ),
    (ConflictSet, {"members": ()}, {"policy": ConflictPolicy.AT_MOST_ONE}),
    (
        ConflictFork,
        {"selection": (), "active_extras": (), "active_groups": ()},
        {"active_configured": ()},
    ),
    (
        EffectiveValue,
        {"spec": SPEC, "value": 1, "origin": ORIGIN, "stack": ()},
        {"rejected": ()},
    ),
    (
        SourceRoots,
        {},
        {
            "system_toml": None,
            "user_toml": None,
            "project_dir": None,
            "pyproject": None,
        },
    ),
]


def test_the_cases_cover_every_value_type() -> None:
    """A subclass added later and left out of ``CASES`` would go unchecked."""
    declared = {
        obj
        for module in (config, config_sources)
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, ValueType) and obj is not ValueType
    }

    assert declared == {case.cls for case in CASES}


@BY_ID
def test_the_field_names_are_declared_once_in_order(case: Case) -> None:
    """``__slots__`` and ``__match_args__`` are the one field tuple."""
    names = tuple(case.fields)

    assert case.cls.__match_args__ == names
    assert case.cls.__slots__ == names


@BY_ID
def test_instances_carry_no_dict(case: Case) -> None:
    with pytest.raises(AttributeError):
        _ = case.build().__dict__


@BY_ID
def test_positional_construction_binds_the_declared_order(case: Case) -> None:
    """A signature binding in a different order than the field tuple shows up here."""
    assert case.cls(*case.fields.values()) == case.build()


@BY_ID
def test_repr_names_every_field_in_order(case: Case) -> None:
    rendered = ", ".join(f"{name}={value!r}" for name, value in case.fields.items())

    assert repr(case.build()) == f"{case.cls.__qualname__}({rendered})"


def test_repr_leads_with_the_qualified_name() -> None:
    """All ten are module-level, so only a nested class separates the two names."""

    class Nested(ValueType):
        __slots__ = __match_args__ = ("value",)

        value: int

        def __init__(self, value: int) -> None:
            self.value = value

    assert "." in Nested.__qualname__
    assert repr(Nested(1)) == f"{Nested.__qualname__}(value=1)"


@BY_ID
def test_equality_varies_with_every_field(case: Case) -> None:
    """One field at a time, so no field can drop out of ``__eq__``."""
    assert case.alternates.keys() == case.fields.keys()

    instance = case.build()
    assert instance == case.build()

    for name, value in case.alternates.items():
        assert instance != case.build(**{name: value}), name


@BY_ID
def test_equality_declines_another_type(case: Case) -> None:
    assert case.build().__eq__("not a value type") is NotImplemented


@BY_ID
def test_a_subclass_holding_the_same_fields_is_not_equal(case: Case) -> None:
    """Equality is by exact class, so a subclass never compares equal."""

    class Subclass(case.cls):  # type: ignore[misc, name-defined]
        __slots__ = ()

    assert case.build() != Subclass(**case.fields)


@BY_ID
def test_hashing_follows_equality(case: Case) -> None:
    instance = case.build()
    if not case.hashable:
        with pytest.raises(TypeError):
            hash(instance)
        return

    assert hash(instance) == hash(case.build())
    assert {instance, case.build()} == {instance}


@BY_ID
def test_an_undeclared_name_cannot_be_set(case: Case) -> None:
    """Without ``__slots__ = ()`` on the base every instance would carry a dict."""
    with pytest.raises(AttributeError):
        case.build().unknown = 1  # type: ignore[attr-defined]


@BY_ID
def test_copying_and_unpickling_restore_every_field(case: Case) -> None:
    """All three rebuild an instance from its slots, with no state hook of its own."""
    instance = case.build()
    restored = [
        copy.copy(instance),
        copy.deepcopy(instance),
        _round_trip_pickle(instance),
    ]

    for other in restored:
        assert other is not instance
        assert type(other) is case.cls
        assert other == instance


@pytest.mark.parametrize(
    ("cls", "required", "expected"),
    DEFAULTS,
    ids=[cls.__name__ for cls, _, _ in DEFAULTS],
)
def test_the_optional_fields_keep_their_defaults(
    cls: type[ValueType], required: dict[str, Any], expected: dict[str, Any]
) -> None:
    instance = cls(**required)

    assert {name: getattr(instance, name) for name in expected} == expected


def test_a_conflict_member_deduplicates_by_canonical_identity() -> None:
    """``_check_conflict_member_uniqueness`` holds members in a set."""
    member = ConflictMember(ConflictKind.EXTRA, "cpu")
    same = ConflictMember(ConflictKind.EXTRA, "cpu")
    other_kind = ConflictMember(ConflictKind.GROUP, "cpu")

    assert same in {member}
    assert other_kind not in {member}


def test_a_conflict_set_renders_its_policy_and_members() -> None:
    """The form an error message and ``nab config list`` both print."""
    members = (
        ConflictMember(ConflictKind.EXTRA, "cpu"),
        ConflictMember(ConflictKind.EXTRA, "gpu"),
    )

    assert str(ConflictSet(members)) == "at-most-one (extra 'cpu', extra 'gpu')"
