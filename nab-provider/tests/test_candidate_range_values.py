"""Restore a key serialized with its field-dictionary layout."""

import base64
import copy
import pickle

import pytest

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.version import Version
from nab_provider.candidate_ranges import CandidateKey, CandidateRange

# Protocol 4 field-dictionary pickles written with PYTHONHASHSEED=0.
_LEGACY_KEY = (
    "gASVnAAAAAAAAACMHW5hYl9wcm92aWRlci5jYW5kaWRhdGVfcmFuZ2VzlIwMQ2FuZGlkYXRlS2V5"
    "lJOUKYGUfZQojAd2ZXJzaW9ulIwmbmFiX3Byb3ZpZGVyLl92ZW5kb3IucGFja2FnaW5nLnZlcnNp"
    "b26UjAdWZXJzaW9ulJOUKYGUKEsASwGFlE5OTk50lGKMBnNvdXJjZZSMBmRpcmVjdJR1Yi4="
)


def test_read_legacy_key_field_dictionary_pickle() -> None:
    expected = CandidateKey(Version("1"), "direct")
    restored = pickle.loads(base64.b64decode(_LEGACY_KEY))  # noqa: S301 - fixed trusted fixture
    assert restored == expected
    assert hash(restored) == hash(expected)


class DictKey(CandidateKey):
    """Require mutable metadata that is not part of key identity."""

    initializations = 0

    def __init__(self, version: Version, source: str, extra: list[str]) -> None:
        super().__init__(version, source)
        self.extra = extra
        type(self).initializations += 1


class SlotKey(CandidateKey):
    """Keep subclass metadata in slots, including an unset optional slot."""

    __slots__ = ("extra", "unused")
    initializations = 0

    def __init__(self, version: Version, source: str, extra: list[str]) -> None:
        super().__init__(version, source)
        self.extra = extra
        type(self).initializations += 1


@pytest.mark.parametrize("cls", [DictKey, SlotKey])
def test_key_subclass_state_survives_copies_without_constructor_calls(
    cls: type,
) -> None:
    cls.initializations = 0
    key = cls(Version("1"), "direct", ["metadata"])
    for restored in (copy.copy(key), copy.deepcopy(key)):
        assert restored == key
        assert restored.extra == key.extra
        assert cls.initializations == 1
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        restored = pickle.loads(pickle.dumps(key, protocol))  # noqa: S301 - local value roundtrip
        assert restored == key
        assert restored.extra == key.extra
        assert cls.initializations == 1
    del key.extra
    key.extra = ["replacement"]
    for name in ("version", "source"):
        with pytest.raises(AttributeError):
            setattr(key, name, None)
        with pytest.raises(AttributeError):
            delattr(key, name)


class MetadataRange(CandidateRange):
    """Require subclass state that a shallow copy must retain unchanged."""

    __slots__ = ("extra",)
    initializations = 0

    def __init__(self, default: VersionRange, extra: list[str]) -> None:
        super().__init__(default)
        self.extra = extra
        type(self).initializations += 1


def test_range_subclass_shallow_copy_retains_state_and_field_identity() -> None:
    MetadataRange.initializations = 0
    original = MetadataRange(VersionRange.full(admit_arbitrary=False), ["metadata"])
    restored = copy.copy(original)
    assert restored == original
    assert restored.default is original.default
    assert restored.overrides is original.overrides
    assert restored.extra is original.extra
    assert MetadataRange.initializations == 1
    del restored.extra
    restored.extra = ["replacement"]
    for name in ("default", "overrides", "_hash"):
        with pytest.raises(AttributeError):
            setattr(restored, name, None)
        with pytest.raises(AttributeError):
            delattr(restored, name)


class ShadowKey(CandidateKey):
    """Replace an inherited field with a visible subclass slot."""

    __slots__ = ("version",)


class ShadowRange(CandidateRange):
    """Replace inherited bounds with visible subclass slots."""

    __slots__ = ("default", "overrides")


def test_constructor_respects_subclass_slots_shadowing_base_fields() -> None:
    key = ShadowKey(Version("1"), "direct")
    assert key.version == Version("1")
    assert key.source == "direct"
    copied = copy.copy(key)
    assert copied.version == key.version
    default = VersionRange.full(admit_arbitrary=False)
    constraint = ShadowRange(default, {"direct": VersionRange.singleton(key.version)})
    assert constraint.default == default
    assert key in constraint


class PropertyKey(CandidateKey):
    """Keep a visible field in dictionary state behind a property."""

    writes = 0
    storage_name = "version"

    @property
    def version(self) -> Version:
        return self.__dict__[self.storage_name]

    @version.setter
    def version(self, value: Version) -> None:
        self.__dict__[self.storage_name] = value
        type(self).writes += 1


class PrivatePropertyKey(PropertyKey):
    """Store a property under a distinct backing attribute name."""

    storage_name = "_version"


@pytest.mark.parametrize("cls", [PropertyKey, PrivatePropertyKey])
def test_copy_preserves_property_state_without_replaying_its_setter(cls: type) -> None:
    cls.writes = 0
    key = cls(Version("1"), "direct")
    assert cls.writes == 1
    for restored in (copy.copy(key), copy.deepcopy(key)):
        assert restored == key
        assert restored.version == Version("1")
        assert cls.writes == 1


def test_base_key_supports_all_pickle_protocols() -> None:
    key = CandidateKey(Version("1"), "direct")
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        restored = pickle.loads(pickle.dumps(key, protocol))  # noqa: S301 - local value roundtrip
        assert restored == key
        assert hash(restored) == hash(key)
