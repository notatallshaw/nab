"""Tests for ``Range``'s memoized hash and restored set algebra."""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from nab_resolver._compat import override
from nab_resolver.ranges import Interval, Range

PROTOCOLS = tuple(range(pickle.HIGHEST_PROTOCOL + 1))
"""Protocols 0 and 1 route through ``copyreg`` and drop a falsy pickle state,
which is a different code path from the one the default protocol takes."""

WRITER = """
import pickle
import sys

from nab_resolver.ranges import Range

value = Range.at_least(3)
hash(value)
with open(sys.argv[1], "wb") as handle:
    pickle.dump(value, handle)

sys.stdout.write(str(hash("_PositiveInfinity")))
"""

READER = """
import pickle
import sys

from nab_resolver.ranges import Range

with open(sys.argv[1], "rb") as handle:
    restored = pickle.load(handle)

fresh = Range.at_least(3)
index = {restored: "restored"}

sys.stdout.write(
    f'{hash("_PositiveInfinity")}'
    f' {hash(restored) == hash(fresh)}'
    f' {index.get(fresh)}'
    f' {len({restored, fresh})}'
)
"""


def round_trip(
    value: Range[int], protocol: int = pickle.DEFAULT_PROTOCOL
) -> Range[int]:
    """Pickle and unpickle ``value`` inside this process."""
    return pickle.loads(pickle.dumps(value, protocol))  # noqa: S301


def restore_range(value: Range[int], protocol: int | None) -> Range[int]:
    """Copy a range through pickle, or deepcopy when ``protocol`` is ``None``."""
    return deepcopy(value) if protocol is None else round_trip(value, protocol)


def run_at_seed(source: str, seed: int, *args: str) -> str:
    """Run ``source`` in a child interpreter at a fixed ``PYTHONHASHSEED``.

    The child inherits this process's ``sys.path`` so it imports the same
    ``nab_resolver`` the test does, editable install or not.
    """
    environment = dict(
        os.environ,
        PYTHONHASHSEED=str(seed),
        PYTHONPATH=os.pathsep.join(sys.path),
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", source, *args],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    return completed.stdout


class TestHashMemo:
    def test_the_first_hash_is_the_one_that_is_kept(self) -> None:
        value = Range.between(2, 9)
        assert value._hash == 0

        first = hash(value)

        assert value._hash == first
        assert hash(value) == first

    def test_equal_ranges_hash_alike(self) -> None:
        assert hash(Range.at_least(4)) == hash(Range.at_least(4))

    def test_an_interval_tuple_hashing_to_zero_memoizes_as_one(self) -> None:
        """Zero is the "not computed" marker, so a real zero is stored as one instead."""

        class ZeroHashing(tuple[Interval, ...]):
            """An interval tuple whose hash collides with the marker."""

            __slots__ = ()

            @override
            def __hash__(self) -> int:
                return 0

        value: Range[int] = Range(ZeroHashing([(1, True, 1, True)]))

        assert hash(value) == 1
        assert value._hash == 1
        assert hash(value) == 1


class TestPickle:
    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_a_round_trip_restores_an_equal_range(self, protocol: int) -> None:
        original = Range.at_least(3) | Range.singleton(1)
        restored = round_trip(original, protocol)

        assert restored == original
        assert 3 in restored
        assert 2 not in restored

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_the_empty_range_round_trips(self, protocol: int) -> None:
        """Its intervals are ``()``, which the older protocols drop as a state."""
        restored = round_trip(Range.empty(), protocol)

        assert restored == Range.empty()
        assert restored.is_empty
        assert hash(restored) == hash(Range.empty())
        assert 1 not in restored

    def test_the_memo_does_not_travel_with_the_pickle(self) -> None:
        """The restored range has to compute its own hash, not inherit one."""
        original = Range.at_least(3)
        hash(original)

        restored = round_trip(original)

        assert restored._hash == 0
        assert hash(restored) == hash(original)

    def test_an_unpickled_range_hashes_under_the_reading_process_seed(
        self, tmp_path: Path
    ) -> None:
        """A range written at one hash seed indexes at another.

        Without the state methods the writer's memo rides along in the pickle,
        the restored range disagrees with an equal one built locally, and a
        dict keyed by the restored range misses.
        """
        pickle_path = str(tmp_path / "range.pickle")

        writer_sentinel = run_at_seed(WRITER, 1, pickle_path).strip()
        reader_sentinel, hashes_agree, index_hit, distinct = run_at_seed(
            READER, 2, pickle_path
        ).split()

        # A test where both seeds hashed the sentinel alike would prove nothing.
        assert writer_sentinel != reader_sentinel

        assert hashes_agree == "True"
        assert index_hit == "restored"
        assert distinct == "1"


@pytest.mark.parametrize("protocol", [*PROTOCOLS, pytest.param(None, id="deepcopy")])
@pytest.mark.parametrize(
    "original",
    [
        pytest.param(Range.full(), id="full"),
        pytest.param(Range.less_than(3), id="upper-bounded"),
        pytest.param(Range.at_least(3), id="lower-bounded"),
    ],
)
class TestRestoredAlgebra:
    def test_complement_matches_fresh_range(
        self, original: Range[int], protocol: int | None
    ) -> None:
        restored = restore_range(original, protocol)

        assert ~restored == ~original

    def test_difference_from_equal_fresh_range_is_empty(
        self, original: Range[int], protocol: int | None
    ) -> None:
        restored = restore_range(original, protocol)

        assert (original - restored).is_empty
        assert (restored - original).is_empty

    def test_complement_is_disjoint_from_fresh_range(
        self, original: Range[int], protocol: int | None
    ) -> None:
        restored = restore_range(original, protocol)

        assert original.is_disjoint(~restored)
