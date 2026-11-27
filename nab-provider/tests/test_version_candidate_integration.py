"""Native candidate identity and reconstruction across the catalogue boundary."""

import copy
import os
import pickle
import subprocess
import sys

from packaging.version import Version

from nab_candidates.versions import VersionCandidate, VersionCatalogue
from nab_provider.value import field_names, replace
from nab_provider.yank_candidates import Candidate


def test_native_candidate_identity_preserves_raw_labels_and_withdrawal() -> None:
    original = Candidate("sample[feature]", Version("1.0"), withdrawn=False)
    same = Candidate("sample[feature]", Version("1.0"), withdrawn=False)
    different = [
        Candidate("sample", Version("1.0"), withdrawn=False),
        Candidate("sample[feature]", Version("1"), withdrawn=False),
        Candidate("sample[feature]", Version("1.0"), withdrawn=True),
        VersionCandidate("sample[feature]", Version("1.0"), withdrawn=False),
    ]

    assert {original: "metadata"}[same] == "metadata"
    assert all(original != candidate for candidate in different)
    assert original != object()
    assert len({original, same, *different}) == 1 + len(different)

    assert copy.copy(original) == original
    assert pickle.loads(pickle.dumps(original)) == original  # noqa: S301 - locally created test data.
    assert replace(original, withdrawn=True) == different[2]
    assert field_names(original) == ("package", "version", "withdrawn", "label")


def test_native_candidate_pickle_rehashes_under_another_seed() -> None:
    setup = """import pickle, sys
from packaging.version import Version
from nab_provider.yank_candidates import Candidate
candidate = Candidate('sample', Version('1.0'), withdrawn=False)
"""
    payload = subprocess.check_output(  # noqa: S603 - fixed interpreter and literal test program.
        [
            sys.executable,
            "-c",
            setup + "sys.stdout.buffer.write(pickle.dumps({candidate: 'metadata'}))",
        ],
        env={**os.environ, "PYTHONHASHSEED": "0"},
    )
    subprocess.run(  # noqa: S603 - fixed interpreter and literal test program.
        [
            sys.executable,
            "-c",
            setup
            + "assert pickle.loads(sys.stdin.buffer.read())[candidate] == 'metadata'",
        ],
        input=payload,
        env={**os.environ, "PYTHONHASHSEED": "1"},
        check=True,
    )


def test_extra_base_keeps_its_native_withdrawal_identity() -> None:
    candidate = Candidate("sample[feature]", Version("1.0"), withdrawn=True)

    base = VersionCatalogue.base_candidate(candidate)

    assert type(base) is Candidate
    assert base == Candidate("sample", Version("1.0"), withdrawn=True)
    assert base.label == "1.0"
