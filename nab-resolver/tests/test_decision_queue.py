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
    """A provider whose sort keys move with and without the solution.

    ``prioritize`` ranks on how far a package's culprit count trails the
    leading one and then on how many versions its range leaves, so a key moves
    both when crediting a culprit leaves the solution alone and when the
    solution narrows the range. ``is_ready`` holds one package back for the
    first scans, the way a listing still in flight does.
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

    def begin_decision_scan(self) -> Callable[[str], bool]:
        """Count the scan, then offer ``is_ready``: nothing else moves it here."""
        self.scans += 1
        return self.is_ready

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[int],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> int:
        """Rank on the culprit gap, then on how many versions the range leaves.

        Scaling the gap orders the two the way a tuple would, since no package
        in this graph has more than two versions.
        """
        counts = culprit_counts or {}
        trailing = max(counts.values(), default=0) - counts.get(package, 0)
        matching = super().prioritize(
            package, version_range, conflict_counts, culprit_counts
        )
        return trailing * 4 + matching

    def is_ready(self, package: str) -> bool:
        """Report every package ready but the held-back one, until its scan."""
        return package != self._unready_package or self.scans >= self._ready_scan


class RecheckingDecisionQueue(DecisionQueue[str]):
    """A queue that re-derives every key it holds once the pick is made.

    The cached keys are what the resolver decides on, so a move the queue is
    never told about shows up here as a key the scan's own ``sort_key`` no
    longer agrees with. Also counts the keys the probe held back, so a resolve
    that never took the skip cannot pass as a check of it.
    """

    def __init__(self) -> None:
        """Start empty, with no unready package seen and no key held back."""
        super().__init__()
        self.unready_picks = 0
        self.held_keys = 0

    def pick(
        self,
        undecided: AbstractSet[str],
        sort_key: Callable[[str], tuple[Any, ...]],
        changed: set[str],
        epoch: int,
        key_inputs_arrived: Callable[[str], bool] | None = None,
    ) -> str:
        """Pick as usual, then check the cached keys against a fresh scan."""
        evaluated: set[str] = set()

        def watched(package: str) -> tuple[Any, ...]:
            """Return the package's key, recording that the scan built it."""
            evaluated.add(package)
            return sort_key(package)

        picked = super().pick(undecided, watched, changed, epoch, key_inputs_arrived)

        if self._unready:
            self.unready_picks += 1

        # Every unready package is stale, so one the scan never evaluated is
        # one the probe held back.
        self.held_keys += len(self._unready - evaluated)

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
    signal decides on a stale key and resolves somewhere else in silence. The
    provider hands back a probe, so ``c``'s key is one the queue keeps across
    scans rather than rebuilds.
    """
    provider = ConflictSensitiveProvider(BACKJUMPING_GRAPH, unready="c", ready_scan=12)
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
    assert queue.held_keys > 0


class Landings:
    """A probe over packages whose listings land on cue."""

    def __init__(self) -> None:
        """Start with nothing landed and nothing probed."""
        self.landed: set[str] = set()
        self.probed: list[str] = []

    def probe(self, package: str) -> bool:
        """Report whether the package's listing has landed, recording the ask."""
        self.probed.append(package)
        return package in self.landed


def test_a_probe_that_stays_false_leaves_the_unready_key_unread() -> None:
    """An in-flight package's key is not rebuilt while its probe stays false."""
    book = KeyBook({"a": (1, 0, "a"), "b": (0, 2, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    landings = Landings()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0, landings.probe)
    book.take_read()

    picked = queue.pick({"a", "b"}, book.sort_key, set(), 0, landings.probe)

    assert picked == "b"
    assert book.take_read() == set()
    assert landings.probed == ["a"]


def test_a_probe_turning_true_rebuilds_the_key_it_held() -> None:
    book = KeyBook({"a": (1, 0, "a"), "b": (0, 2, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    landings = Landings()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0, landings.probe)
    queue.pick({"a", "b"}, book.sort_key, set(), 0, landings.probe)
    book.take_read()

    landings.landed.add("a")
    book.keys["a"] = (0, 1, "a")

    picked = queue.pick({"a", "b"}, book.sort_key, set(), 0, landings.probe)

    assert picked == "a"
    assert book.take_read() == {"a"}


def test_a_changed_package_is_re_evaluated_while_its_probe_is_false() -> None:
    """A moved range changes the key of a package still waiting on a listing."""
    book = KeyBook({"a": (1, 0, "a"), "b": (0, 2, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    landings = Landings()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0, landings.probe)
    book.take_read()

    book.keys["a"] = (1, 5, "a")
    queue.pick({"a", "b"}, book.sort_key, {"a"}, 0, landings.probe)

    assert book.take_read() == {"a"}
    assert landings.probed == []


def test_a_new_epoch_re_evaluates_an_unready_key_the_probe_denies() -> None:
    """A new epoch stands for a count move the probe knows nothing about."""
    book = KeyBook({"a": (1, 0, "a"), "b": (0, 2, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    landings = Landings()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0, landings.probe)
    book.take_read()

    queue.pick({"a", "b"}, book.sort_key, set(), 1, landings.probe)

    assert book.take_read() == {"a", "b"}
    assert landings.probed == []


def test_a_listing_landing_mid_scan_reaches_the_package_behind_it() -> None:
    """The probe runs at the package's own place in the walk.

    One snapshot taken at the head of the scan would hold every later arrival
    back a whole scan, and could pick a different package.
    """
    book = KeyBook({"a": (1, 0, "a"), "b": (1, 0, "b")})
    queue: DecisionQueue[str] = DecisionQueue()
    landings = Landings()
    queue.pick({"a", "b"}, book.sort_key, {"a", "b"}, 0, landings.probe)
    book.take_read()

    def land_the_others(package: str) -> bool:
        """Publish every other package's listing, then answer for this one."""
        for other in ("a", "b"):
            if other != package:
                landings.landed.add(other)
                book.keys[other] = (0, 1, other)
        return landings.probe(package)

    picked = queue.pick({"a", "b"}, book.sort_key, set(), 0, land_the_others)

    _, second = landings.probed
    assert book.take_read() == {second}
    assert picked == second
