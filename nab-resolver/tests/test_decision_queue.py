"""Unit tests for :mod:`nab_resolver.decision_queue`.

``DecisionQueue`` answers the same question as ``min(undecided, key=sort_key)``
while evaluating only the keys that can have moved, so these tests pin the
invalidation: which packages a scan re-evaluates, and that the answer matches
the one a full scan would give.

Most of them hand the queue its signals directly, over a synthetic
:class:`KeyBook`. That leaves the other half of the contract untested, because
a signal the resolver never sends reaches the queue as no signal at all, and
the resolve then goes somewhere else without failing. The last test in the
module covers that by driving a real resolve and checking every cached key
against a fresh one.

The sort keys here have the shape ``choose_package_to_decide`` builds,
``(ready_penalty, priority, tiebreak)``, since a truthy first field is what
marks a package whose listing has not settled.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from typing import Any

from nab_resolver.decision_queue import DecisionQueue
from nab_resolver.ranges import Range
from nab_resolver.resolver import Resolver
from nab_resolver.types import RangeProtocol

from .property.providers import FuzzProvider


class KeyBook:
    """The sort keys of a scan, and a record of which ones were read."""

    def __init__(self, keys: dict[str, tuple[Any, ...]]) -> None:
        """Hold one key per package, with nothing read yet."""
        self.keys = keys
        self.read: list[str] = []

    def sort_key(self, package: str) -> tuple[Any, ...]:
        """Return the package's key, recording that it was read."""
        self.read.append(package)
        return self.keys[package]

    def take_read(self) -> set[str]:
        """Return the packages read since the last call, and start over."""
        read = set(self.read)
        self.read.clear()
        return read


def test_first_scan_evaluates_every_package_and_picks_the_smallest() -> None:
    book = KeyBook({"a": (0, 2, "a"), "b": (0, 1, "b"), "c": (0, 3, "c")})
    queue: DecisionQueue[str] = DecisionQueue()

    picked = queue.pick({"a", "b", "c"}, book.sort_key, {"a", "b", "c"}, 0)

    assert picked == "b"
    assert book.take_read() == {"a", "b", "c"}


def test_an_unchanged_scan_reads_no_keys() -> None:
    """The saving being measured: a scan that moved nothing re-reads nothing."""
    book = KeyBook({"a": (0, 2, "a"), "b": (0, 1, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0)
    book.take_read()

    picked = queue.pick({"a", "b"}, book.sort_key, set(), 0)

    assert picked == "b"
    assert book.take_read() == set()


def test_a_changed_package_takes_the_lead_on_its_new_key() -> None:
    book = KeyBook({"a": (0, 2, "a"), "b": (0, 1, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0)
    book.take_read()

    book.keys["a"] = (0, 0, "a")
    picked = queue.pick({"a", "b"}, book.sort_key, {"a"}, 0)

    assert picked == "a"
    assert book.take_read() == {"a"}


def test_a_superseded_entry_loses_to_the_key_that_replaced_it() -> None:
    """A leader whose key worsens must not stay on top of the heap."""
    book = KeyBook({"a": (0, 1, "a"), "b": (0, 2, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    assert queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0) == "a"

    book.keys["a"] = (0, 9, "a")

    assert queue.pick({"a", "b"}, book.sort_key, {"a"}, 0) == "b"


def test_a_package_reporting_the_same_key_is_not_pushed_again() -> None:
    book = KeyBook({"a": (0, 1, "a"), "b": (0, 2, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0)
    entries = len(queue._heap)

    queue.pick({"a", "b"}, book.sort_key, {"a"}, 0)

    assert len(queue._heap) == entries


def test_an_unready_package_is_re_evaluated_until_it_is_ready() -> None:
    """An unready package is rescanned until its listing settles.

    A listing lands without the partial solution seeing it, so the ready
    penalty is what keeps the package on the list.
    """
    book = KeyBook({"a": (1, 0, "a"), "b": (0, 2, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0)
    book.take_read()

    assert queue.pick({"a", "b"}, book.sort_key, set(), 0) == "b"
    assert book.take_read() == {"a"}

    book.keys["a"] = (0, 1, "a")
    assert queue.pick({"a", "b"}, book.sort_key, set(), 0) == "a"
    book.take_read()

    assert queue.pick({"a", "b"}, book.sort_key, set(), 0) == "a"
    assert book.take_read() == set()


def test_a_new_epoch_re_evaluates_every_key() -> None:
    """The conflict and culprit counts every key reads move outside ``changed``."""
    book = KeyBook({"a": (0, 2, "a"), "b": (0, 1, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0)
    book.take_read()

    book.keys["a"] = (0, 0, "a")
    picked = queue.pick({"a", "b"}, book.sort_key, set(), 1)

    assert picked == "a"
    assert book.take_read() == {"a", "b"}


def test_a_decided_package_leaves_and_comes_back_on_a_fresh_key() -> None:
    book = KeyBook({"a": (0, 1, "a"), "b": (0, 2, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0)
    book.take_read()

    assert queue.pick({"b"}, book.sort_key, {"a"}, 0) == "b"
    assert book.take_read() == set()

    assert queue.pick({"a", "b"}, book.sort_key, {"a"}, 0) == "a"
    assert book.take_read() == {"a"}


def test_clear_forgets_every_key() -> None:
    """A restart replaces the solution, so the queue starts from its packages."""
    book = KeyBook({"a": (0, 1, "a"), "b": (0, 2, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0)
    book.take_read()

    queue.clear()
    picked = queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0)

    assert picked == "a"
    assert book.take_read() == {"a", "b"}
    assert len(queue._heap) == 2


def test_the_heap_is_rebuilt_once_superseded_entries_pile_up() -> None:
    """Without compaction a long resolve carries every key it ever evaluated.

    The keys improve each round, which is the direction compaction is for: a
    superseded entry then sorts behind the key that replaced it, so it never
    reaches the top and only a rebuild removes it. Keys that worsen leave their
    old entries in front, where the ordinary drain takes them.
    """
    packages = [f"p{index:03d}" for index in range(40)]
    book = KeyBook(
        {package: (0, index, package) for index, package in enumerate(packages)}
    )
    queue: DecisionQueue[str] = DecisionQueue()
    undecided = set(packages)
    queue.pick(undecided, book.sort_key, undecided, 0)

    for round_number in range(1, 11):
        for index, package in enumerate(packages):
            book.keys[package] = (0, index - 100 * round_number, package)
        queue.pick(undecided, book.sort_key, undecided, 0)

    assert len(queue._heap) <= 4 * len(packages) + 32
    assert queue.pick(undecided, book.sort_key, set(), 0) == packages[0]


class ConflictSensitiveProvider(FuzzProvider):
    """A provider whose sort keys move with no assignment behind them.

    Two of the things a sort key reads sit outside the partial solution, and
    this drives both. ``prioritize`` ranks a package by how far its culprit
    count trails the leading one, so crediting the leading culprit moves every
    other package's key; ``is_ready`` holds one package back for the first
    scans, the way a listing still in flight does.
    """

    def __init__(
        self,
        graph: dict[str, dict[int, dict[str, Range[int]]]],
        *,
        unready: str,
        ready_scan: int,
    ) -> None:
        """Hold ``unready`` back until the resolve's ``ready_scan``-th scan."""
        super().__init__(graph)
        self._unready_package = unready
        self._ready_scan = ready_scan
        self.scans = 0

    def begin_decision_scan(self) -> None:
        """Count the scan, which is what ``is_ready`` answers from."""
        self.scans += 1

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Rank the package by how far its culprit count trails the leading one."""
        del version_range, conflict_counts
        counts = culprit_counts or {}
        return max(counts.values(), default=0) - counts.get(package, 0)

    def is_ready(self, package: str) -> bool:
        """Report every package ready but the held-back one, until its scan."""
        return package != self._unready_package or self.scans >= self._ready_scan


class RecheckingDecisionQueue(DecisionQueue[str]):
    """A queue that re-derives every key it holds once the pick is made.

    The cached keys are what the resolver decides on, so a move the queue is
    never told about shows up here as a key the scan's own ``sort_key`` no
    longer agrees with.
    """

    def __init__(self) -> None:
        """Start empty, with no scan yet seen holding an unready package."""
        super().__init__()
        self.unready_picks = 0

    def pick(
        self,
        undecided: AbstractSet[str],
        sort_key: Callable[[str], tuple[Any, ...]],
        changed: set[str],
        epoch: int,
    ) -> str:
        """Pick as usual, then check the cached keys against a fresh scan."""
        picked = super().pick(undecided, sort_key, changed, epoch)

        if self._unready:
            self.unready_picks += 1

        assert self._keys.keys() == undecided
        for package in undecided:
            assert self._keys[package] == sort_key(package), package

        return picked


BACKJUMPING_GRAPH: dict[str, dict[int, dict[str, Range[int]]]] = {
    "root": {1: {"a": Range.full(), "c": Range.full(), "d": Range.full()}},
    "a": {2: {"b": Range.at_least(2)}, 1: {"e": Range.full()}},
    "b": {2: {"c": Range.at_least(3)}, 1: {"e": Range.at_least(2)}},
    "c": {2: {}, 1: {}},
    "d": {2: {"b": Range.less_than(2), "e": Range.less_than(2)}, 1: {}},
    "e": {2: {}, 1: {}},
}


def test_a_resolve_that_backjumps_keeps_every_cached_key_current() -> None:
    """Drives a real resolve rather than handing the queue its signals.

    ``a@2`` and ``d@2`` both dead-end, so the resolve backjumps and credits
    culprits, and ``c`` arrives unready. Both move sort keys of packages the
    partial solution reports as unchanged, so a queue that misses either
    signal decides on a stale key and resolves somewhere else in silence.
    """
    provider = ConflictSensitiveProvider(BACKJUMPING_GRAPH, unready="c", ready_scan=3)
    resolver: Resolver[str, int] = Resolver(provider)
    queue = RecheckingDecisionQueue()
    resolver.decision_queue = queue

    result = resolver.resolve({"root": Range.singleton(1)})

    assert result == {"root": 1, "a": 1, "c": 2, "d": 1, "e": 2}

    # The check inside the queue is vacuous unless the resolve raised each of
    # the signals it is there to catch.
    assert resolver.stats.backjumps > 0
    assert resolver.priority_epoch > 0
    assert queue.unready_picks > 0
