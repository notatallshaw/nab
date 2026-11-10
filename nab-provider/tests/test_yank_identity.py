"""Solver identities retain version text, withdrawal state and subclass fields."""

from __future__ import annotations

import pickle
from typing import TYPE_CHECKING, Final

import pytest

from nab_provider._vendor.packaging.version import Version
from nab_provider.value import HashableRecord, replace
from nab_provider.yank_candidates import Candidate
from nab_provider.yanked_resolution import MetadataProxy, PermissionProxy

if TYPE_CHECKING:
    from collections.abc import Callable


class _CandidateWithOrigin(Candidate):
    """A candidate whose equality depends only on its origin."""

    __slots__ = ("origin",)
    __record_compare_fields__ = ("origin",)
    origin: Final[int]

    def __init__(self, origin: int) -> None:
        super().__init__("dep", Version("1"), withdrawn=False)
        self.origin = origin


class _MetadataWithOrigin(MetadataProxy):
    """A metadata proxy whose equality depends only on its origin."""

    __slots__ = ("origin",)
    __record_compare_fields__ = ("origin",)
    origin: Final[int]

    def __init__(self, origin: int) -> None:
        super().__init__(1)
        self.origin = origin


class _PermissionWithOrigin(PermissionProxy):
    """A permission proxy whose equality depends only on its origin."""

    __slots__ = ("origin",)
    __record_compare_fields__ = ("origin",)
    origin: Final[int]

    def __init__(self, origin: int) -> None:
        super().__init__(1)
        self.origin = origin


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [
        (
            Candidate("dep", Version("1"), withdrawn=False),
            Candidate("dep", Version("1"), withdrawn=False),
            True,
        ),
        (
            Candidate("dep", Version("1"), withdrawn=False),
            Candidate("other", Version("1"), withdrawn=False),
            False,
        ),
        (
            Candidate("dep", Version("1"), withdrawn=False),
            Candidate("dep", Version("1"), withdrawn=True),
            False,
        ),
        (
            Candidate("dep", Version("1"), withdrawn=False),
            Candidate("dep", Version("1.0"), withdrawn=False),
            False,
        ),
        (MetadataProxy(1), MetadataProxy(1), True),
        (MetadataProxy(1), MetadataProxy(2), False),
        (PermissionProxy(1), PermissionProxy(1), True),
        (PermissionProxy(1), PermissionProxy(2), False),
        (MetadataProxy(1), PermissionProxy(1), False),
    ],
)
def test_identity_dictionary_lookup(
    left: HashableRecord, right: HashableRecord, *, equal: bool
) -> None:
    values = {left: "first", right: "second"}
    assert (left == right) is equal
    assert len(values) == (1 if equal else 2)
    assert values[right] == "second"
    assert values[left] == ("second" if equal else "first")


@pytest.mark.parametrize(
    "value",
    [
        Candidate("dep", Version("1.0"), withdrawn=True),
        MetadataProxy(1),
        PermissionProxy(1),
    ],
)
def test_identity_reconstruction_retains_lookup(value: HashableRecord) -> None:
    restored = pickle.loads(pickle.dumps(value))  # noqa: S301 - the record is constructed locally
    copied = replace(value)

    values = {value: "original"}
    assert values[restored] == "original"
    assert values[copied] == "original"


@pytest.mark.parametrize(
    "factory", [_CandidateWithOrigin, _MetadataWithOrigin, _PermissionWithOrigin]
)
def test_subclasses_can_select_comparison_fields(
    factory: Callable[[int], HashableRecord],
) -> None:
    first, same, other = factory(1), factory(1), factory(2)

    assert first == same
    assert first != other
    assert {first: "first", same: "same", other: "other"} == {
        first: "same",
        other: "other",
    }
