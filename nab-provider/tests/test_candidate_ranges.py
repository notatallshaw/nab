"""Check source-specific constraints against their product-set semantics."""

import copy
import operator
import pickle
import weakref
from itertools import product

import pytest

from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.candidate_ranges import CandidateKey, CandidateRange


@pytest.mark.parametrize(
    "specifier", ["", "<2", ">=1rc1", "!=1.5", "==1.*", "===invalid"]
)
def test_source_constraints_obey_set_laws(specifier: str) -> None:
    versions = SpecifierSet(specifier).to_range()
    ranges = [
        CandidateRange.empty(),
        CandidateRange.full(),
        CandidateRange(versions),
        CandidateRange.for_source("direct", versions),
        CandidateRange(versions, {"installed": SpecifierSet("==1").to_range()}),
    ]
    keys = [
        CandidateKey(Version(version), source)
        for version, source in product(
            ["0", "1rc1", "1", "1.5", "2", "2+local"],
            ["index", "installed", "direct", "undiscovered"],
        )
    ]
    for left, right in product(ranges, repeat=2):
        assert left.is_subset((left - right) | right)
        assert (left - right).is_disjoint(right)
        assert left | right == right | left
        assert left & right == right & left
        assert left.is_superset(~~left)
        assert (~~left).is_superset(left)
        assert left & CandidateRange.full() == left
        assert left | CandidateRange.empty() == left
        for key in keys:
            assert (key in (left & right)) == (key in left and key in right)
            assert (key in (left | right)) == (key in left or key in right)
            assert (key in (left - right)) == (key in left and key not in right)
            assert (key in ~left) == (key not in left)


def test_singleton_keeps_the_source_at_the_same_version() -> None:
    installed = CandidateKey(Version("1"), "installed")
    direct = CandidateKey(Version("1"), "direct")
    selected = CandidateRange.singleton(installed)
    assert installed in selected
    assert direct not in selected
    assert selected.is_disjoint(CandidateRange.singleton(direct))
    assert str(installed) == "1"
    assert "installed" in str(selected)


def test_overrides_are_snapshotted_and_redundant_coordinates_are_removed() -> None:
    full = CandidateRange.full().default
    overrides = {"direct": SpecifierSet("==1").to_range()}
    constraint = CandidateRange(full, overrides)
    overrides["direct"] = SpecifierSet("<0").to_range()
    assert CandidateKey(Version("1"), "direct") in constraint

    restored = CandidateRange(full, [("direct", overrides["direct"]), ("direct", full)])
    assert restored == CandidateRange.full()
    assert hash(restored) == hash(CandidateRange.full())
    with pytest.raises(AttributeError, match="cannot assign to field"):
        constraint.default = full


def test_relation_reports_each_subset_and_disjoint_combination() -> None:
    one = CandidateRange.singleton(CandidateKey(Version("1"), "index"))
    two = CandidateRange.singleton(CandidateKey(Version("2"), "index"))
    for left, right, subset, disjoint in (
        (CandidateRange.empty(), one, True, True),
        (one, one | two, True, False),
        (one, two, False, True),
        (one | two, one, False, False),
    ):
        relation = left.relation(right)
        assert relation.is_subset is subset
        assert relation.is_disjoint is disjoint


def test_unknown_operand_protocol() -> None:
    constraint = CandidateRange.full()
    assert constraint.__eq__(object()) is NotImplemented
    assert constraint.__and__(object()) is NotImplemented
    assert constraint.__or__(object()) is NotImplemented
    assert constraint.__sub__(object()) is NotImplemented


def test_source_complement_includes_sources_not_known_to_the_host() -> None:
    direct = CandidateRange.for_source("direct")
    unknown = CandidateKey(Version("1"), "not-discovered-yet")
    assert unknown not in direct
    assert unknown in ~direct
    assert ~CandidateRange.empty() == CandidateRange.full()


@pytest.mark.parametrize("prereleases", [True, False])
def test_configured_prerelease_policies_are_rejected(prereleases: bool) -> None:
    versions = SpecifierSet(">=1", prereleases=prereleases).to_range()
    with pytest.raises(ValueError, match="different pre-release policies"):
        CandidateRange(versions)

    with pytest.raises(ValueError, match="different pre-release policies"):
        CandidateRange(CandidateRange.full().default, {"direct": versions})


class DerivedKey(CandidateKey):
    """Exercise inherited comparisons without changing the key fields."""


def test_key_preserves_value_operations_and_exact_class_comparisons() -> None:
    low = CandidateKey(Version("1"), "z")
    high = CandidateKey(version=Version("2"), source="a")
    assert low == CandidateKey(Version("1"), "z")
    assert low != high
    assert hash(low) == hash((low.version, low.source))
    assert repr(low) == "CandidateKey(version=<Version('1')>, source='z')"
    assert sorted([high, low, CandidateKey(Version("1"), "a")]) == [
        CandidateKey(Version("1"), "a"),
        low,
        high,
    ]

    for operation in (operator.eq, operator.lt, operator.le, operator.gt, operator.ge):
        for other in (high, low, CandidateKey(low.version, "a")):
            assert operation(low, other) == operation(
                (low.version, low.source), (other.version, other.source)
            )
    for name in ("__eq__", "__lt__", "__le__", "__gt__", "__ge__"):
        assert getattr(low, name)(object()) is NotImplemented
        assert getattr(low, name)(DerivedKey(low.version, low.source)) is NotImplemented
        assert getattr(DerivedKey(low.version, low.source), name)(low) is NotImplemented
    assert DerivedKey(low.version, low.source) == DerivedKey(low.version, low.source)
    assert DerivedKey(low.version, low.source) < DerivedKey(high.version, high.source)
    assert low != DerivedKey(low.version, low.source)


def test_values_keep_their_pattern_fields_and_range_representation() -> None:
    key = CandidateKey(Version("1"), "direct")
    constraint = CandidateRange.singleton(key)
    assert CandidateKey.__match_args__ == ("version", "source")
    assert CandidateRange.__match_args__ == ("default", "overrides", "_hash")
    match key:
        case CandidateKey(version, source):
            assert (version, source) == (Version("1"), "direct")
        case _:
            pytest.fail("CandidateKey did not match its declared fields")
    match constraint:
        case CandidateRange(default, overrides, cached_hash):
            assert (default, overrides, cached_hash) == (
                constraint.default,
                constraint.overrides,
                hash(constraint),
            )
        case _:
            pytest.fail("CandidateRange did not match its declared fields")
    assert repr(constraint) == (
        f"CandidateRange(default={constraint.default!r}, overrides={constraint.overrides!r}, "
        f"_hash={hash(constraint)!r})"
    )


def test_slotted_values_refuse_replacement_and_deletion() -> None:
    key = CandidateKey(Version("1"), "direct")
    constraint = CandidateRange.singleton(key)
    for value, fields in (
        (key, ("version", "source")),
        (constraint, ("default", "overrides", "_hash")),
    ):
        saved_hash = hash(value)
        assert not hasattr(value, "__dict__")
        assert weakref.ref(value)() is value
        for name in (*fields, "extra"):
            with pytest.raises(AttributeError, match="cannot assign to field"):
                setattr(value, name, None)
            with pytest.raises(AttributeError, match="cannot delete field"):
                delattr(value, name)
        assert hash(value) == saved_hash


def test_copy_and_pickle_preserve_keys_and_shallow_range_copies() -> None:
    key = CandidateKey(Version("1"), "direct")
    restored_keys = (
        copy.copy(key),
        copy.deepcopy(key),
        pickle.loads(pickle.dumps(key)),  # noqa: S301 - roundtrip of a local value
    )
    for restored in restored_keys:
        assert restored is not key
        assert restored == key
        assert hash(restored) == hash(key)
        assert repr(restored) == repr(key)

    constraint = CandidateRange.singleton(key)
    restored_range = copy.copy(constraint)
    assert restored_range is not constraint
    assert restored_range.default is constraint.default
    assert restored_range.overrides is constraint.overrides
    assert restored_range == constraint
    assert hash(restored_range) == hash(constraint)
    assert repr(restored_range) == repr(constraint)
