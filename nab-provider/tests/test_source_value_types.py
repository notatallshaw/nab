"""What this package's hand-written value types promise.

Each names its fields in ``__slots__``, in ``__match_args__`` and in its own
constructor, and :class:`SlottedValue` reads ``__match_args__`` for comparison,
hashing and repr.  A field missing from one of those is otherwise silent.  The
cases below drive every field of every type through all of them, and through
the defaults, copying and pickling.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from nab_provider._value import SlottedValue
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.archive import ArchiveRequest
from nab_provider.metadata import WheelMetadata
from nab_provider.overrides import IndexOverride, PackageOverride
from nab_provider.policy import (
    ArchiveSource,
    BuildPolicy,
    DistPolicy,
    LocalSource,
    SourceMaterialization,
    SourceRequest,
    VcsSource,
)
from nab_provider.vcs_request import VcsClone, VcsRequest

SHA = "0" * 40
OTHER_SHA = "1" * 40

# SourceMaterialization's metadata field needs two values that differ.
METADATA = WheelMetadata(name="pkg", version=Version("1.0"))
OTHER_METADATA = WheelMetadata(name="other", version=Version("2.0"))

SELECTOR = Requirement("pkg>=1")
OTHER_SELECTOR = Requirement("other<2")
CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)
LATER_CUTOFF = datetime(2026, 2, 1, tzinfo=timezone.utc)


class Case(NamedTuple):
    """One value type and the field values the tests drive it through.

    ``alt`` differs from ``base`` in every field, so varying one field at a
    time reaches a field that a comparison or a hash left out.  ``defaults``
    is every constructor parameter that carries one, and ``keyword_only``
    every one a caller cannot pass positionally.  ``uncompared`` names the
    fields the comparison and the hash skip, and ``rebuildable`` is False
    where a field value refuses to be deep-copied or pickled.
    """

    cls: type[SlottedValue]
    base: tuple[Any, ...]
    alt: tuple[Any, ...]
    defaults: dict[str, Any]
    keyword_only: frozenset[str] = frozenset()
    hashable: bool = True
    uncompared: frozenset[str] = frozenset()
    rebuildable: bool = True


CASES = [
    Case(
        LocalSource,
        ("pkg", "/a/b", False, None),
        ("other", "/c/d", True, "sub"),
        {"editable": False, "subdirectory": None},
        keyword_only=frozenset({"editable", "subdirectory"}),
    ),
    Case(
        VcsSource,
        ("pkg", "git+https://e/x.git"),
        ("other", "git+https://e/y.git"),
        {},
    ),
    Case(
        ArchiveSource,
        ("pkg", "https://e/x.tar.gz"),
        ("other", "https://e/y.tar.gz"),
        {},
    ),
    Case(
        SourceRequest,
        (
            "pkg",
            LocalSource("pkg", "/a/b"),
            BuildPolicy.NEVER,
            Path("/vcs"),
            Path("/archive"),
            True,
        ),
        (
            "other",
            VcsSource("other", "git+https://e/y.git"),
            BuildPolicy.BUILD_REMOTE,
            Path("/vcs2"),
            Path("/archive2"),
            False,
        ),
        {},
        keyword_only=frozenset({"require_pin"}),
    ),
    Case(
        SourceMaterialization,
        (Path("/tree"), METADATA, None),
        (Path("/other"), OTHER_METADATA, SHA),
        {},
        hashable=False,
    ),
    Case(
        VcsClone,
        (Path("/tree"), SHA, ""),
        (Path("/other"), OTHER_SHA, "sub"),
        {"subdirectory": ""},
    ),
    Case(
        VcsRequest,
        ("git", "https://e/x.git", "main", ""),
        ("hg", "https://e/y.git", "v1", "sub"),
        {},
    ),
    Case(
        ArchiveRequest,
        ("https://e/x.tar.gz", (("sha256", "ab"),), ""),
        ("https://e/y.tar.gz", (("sha512", "cd"),), "sub"),
        {"subdirectory": ""},
    ),
    Case(
        PackageOverride,
        (
            SELECTOR,
            "pkg",
            SELECTOR.specifier.to_range(),
            DistPolicy.WHEEL_ONLY,
            False,
            BuildPolicy.NEVER,
            CUTOFF,
            False,
            "private",
            (Requirement("dep"),),
            ">=3.10",
            ("cpu",),
            False,
            "packages.'pkg'",
        ),
        (
            OTHER_SELECTOR,
            "other",
            OTHER_SELECTOR.specifier.to_range(),
            DistPolicy.SDIST_ONLY,
            True,
            BuildPolicy.BUILD_REMOTE,
            LATER_CUTOFF,
            True,
            "public",
            (Requirement("other-dep"),),
            ">=3.11",
            ("gpu",),
            True,
            "package-rules[0]",
        ),
        {
            "dist_policy": None,
            "dist_trust_unverified_deps": None,
            "build_policy": None,
            "uploaded_prior_to": None,
            "uploaded_prior_to_disabled": False,
            "index": None,
            "dependencies": None,
            "requires_python": None,
            "provides_extra": None,
            "name_keyed": False,
            "source_label": "",
        },
        keyword_only=frozenset(PackageOverride.__match_args__),
        uncompared=frozenset({"source_label"}),
        rebuildable=False,
    ),
    Case(
        IndexOverride,
        (
            DistPolicy.WHEEL_ONLY,
            False,
            BuildPolicy.NEVER,
            CUTOFF,
            False,
            3600,
        ),
        (
            DistPolicy.SDIST_ONLY,
            True,
            BuildPolicy.BUILD_REMOTE,
            LATER_CUTOFF,
            True,
            60,
        ),
        {
            "dist_policy": None,
            "dist_trust_unverified_deps": None,
            "build_policy": None,
            "uploaded_prior_to": None,
            "uploaded_prior_to_disabled": False,
            "assume_fresh_seconds": None,
        },
        keyword_only=frozenset(IndexOverride.__match_args__),
    ),
]

VALUE_TYPES = [pytest.param(case, id=case.cls.__name__) for case in CASES]


def _build(case: Case, values: tuple[Any, ...]) -> Any:
    """Construct ``case.cls`` from ``values``, every field passed by name."""
    return case.cls(**dict(zip(case.cls.__match_args__, values, strict=True)))


def _build_positionally(case: Case, values: tuple[Any, ...]) -> Any:
    """Construct ``case.cls`` positionally, naming only its keyword-only fields."""
    fields = dict(zip(case.cls.__match_args__, values, strict=True))
    leading = [value for name, value in fields.items() if name not in case.keyword_only]

    return case.cls(*leading, **{name: fields[name] for name in case.keyword_only})


def _vary(base: tuple[Any, ...], alt: tuple[Any, ...], index: int) -> tuple[Any, ...]:
    """Return ``base`` with field ``index`` taken from ``alt``."""
    return (*base[:index], alt[index], *base[index + 1 :])


def _compared(case: Case, values: tuple[Any, ...]) -> tuple[Any, ...]:
    """Return ``values`` less the fields the comparison and the hash skip."""
    return tuple(
        value
        for name, value in zip(case.cls.__match_args__, values, strict=True)
        if name not in case.uncompared
    )


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_init_takes_the_declared_fields_in_order(case: Case) -> None:
    parameters = inspect.signature(case.cls.__init__).parameters
    names = list(parameters)

    assert names[0] == "self"
    assert tuple(names[1:]) == case.cls.__match_args__

    instance = _build(case, case.base)
    for name, value in zip(case.cls.__match_args__, case.base, strict=True):
        assert getattr(instance, name) == value

    assert _build_positionally(case, case.base) == instance


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_the_fields_a_caller_must_name_are_the_expected_ones(case: Case) -> None:
    parameters = inspect.signature(case.cls.__init__).parameters
    keyword_only = {
        name
        for name, parameter in parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }

    assert keyword_only == case.keyword_only


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_init_carries_exactly_the_expected_defaults(case: Case) -> None:
    parameters = inspect.signature(case.cls.__init__).parameters
    declared = {
        name: parameter.default
        for name, parameter in parameters.items()
        if parameter.default is not inspect.Parameter.empty
    }

    assert declared == case.defaults


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_equality_reads_every_compared_field(case: Case) -> None:
    """Two values differing only in a skipped field are still equal."""
    assert _build(case, case.base) == _build(case, case.base)

    for index, name in enumerate(case.cls.__match_args__):
        varied = _build(case, _vary(case.base, case.alt, index))
        if name in case.uncompared:
            assert _build(case, case.base) == varied, name
        else:
            assert _build(case, case.base) != varied, name


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_equality_defers_to_an_unrelated_type(case: Case) -> None:
    assert case.cls.__eq__(_build(case, case.base), object()) is NotImplemented
    assert _build(case, case.base) != object()


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_equality_rejects_a_subclass_holding_the_same_values(case: Case) -> None:
    subclass = type(f"{case.cls.__name__}Subclass", (case.cls,), {"__slots__": ()})
    same = subclass(**dict(zip(case.cls.__match_args__, case.base, strict=True)))

    assert _build(case, case.base) != same
    assert same != _build(case, case.base)


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_hash_is_the_compared_field_tuple(case: Case) -> None:
    instance = _build(case, case.base)
    if not case.hashable:
        # One field is itself unhashable, so hashing the field tuple raises.
        with pytest.raises(TypeError):
            hash(instance)
        return

    assert hash(instance) == hash(_compared(case, case.base))
    assert len({instance, _build(case, case.base)}) == 1

    for index, name in enumerate(case.cls.__match_args__):
        varied = hash(_build(case, _vary(case.base, case.alt, index)))
        if name in case.uncompared:
            assert hash(instance) == varied, name
        else:
            assert hash(instance) != varied, name


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_repr_names_every_field_in_order(case: Case) -> None:
    body = ", ".join(
        f"{name}={value!r}"
        for name, value in zip(case.cls.__match_args__, case.base, strict=True)
    )

    assert repr(_build(case, case.base)) == f"{case.cls.__qualname__}({body})"


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_a_write_reaches_only_a_declared_field(case: Case) -> None:
    instance = _build(case, case.base)
    first = case.cls.__match_args__[0]

    setattr(instance, first, case.alt[0])
    assert getattr(instance, first) == case.alt[0]

    with pytest.raises(AttributeError):
        instance.not_a_field = 1  # type: ignore[attr-defined]

    assert not hasattr(instance, "__dict__")
    assert tuple(sorted(case.cls.__match_args__)) == tuple(sorted(case.cls.__slots__))


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_a_shallow_copy_restores_every_field(case: Case) -> None:
    instance = _build(case, case.base)

    assert copy.copy(instance) == instance


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_deep_copying_and_pickling_restore_every_field(case: Case) -> None:
    """Both rebuild every field value, which the vendored ``VersionRange`` refuses."""
    instance = _build(case, case.base)
    if not case.rebuildable:
        with pytest.raises(TypeError):
            copy.deepcopy(instance)
        with pytest.raises(TypeError):
            pickle.loads(pickle.dumps(instance))  # noqa: S301
        return

    assert copy.deepcopy(instance) == instance
    assert pickle.loads(pickle.dumps(instance)) == instance  # noqa: S301


@pytest.mark.parametrize("case", VALUE_TYPES)
def test_the_type_is_not_a_dataclass(case: Case) -> None:
    # Re-decorating one puts back the import cost they are written out to avoid.
    assert not dataclasses.is_dataclass(case.cls)


def test_a_positional_pattern_binds_the_declared_order() -> None:
    bound: tuple[object, ...] = ()
    match LocalSource("pkg", "/a/b", editable=True, subdirectory="sub"):
        case LocalSource(name, path, editable, subdirectory):
            bound = (name, path, editable, subdirectory)

    assert bound == ("pkg", "/a/b", True, "sub")
