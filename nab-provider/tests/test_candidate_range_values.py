"""Restore a key serialized with its field-dictionary layout."""

import base64
import pickle

from nab_provider._vendor.packaging.version import Version
from nab_provider.candidate_ranges import CandidateKey

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
