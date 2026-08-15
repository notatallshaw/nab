"""A lazy min-heap over the undecided packages' decision sort keys.

``choose_package_to_decide`` needs the smallest sort key in the undecided set,
and a scan moves the keys of only the handful of packages it assigns. The queue
holds the keys in a heap and calls ``sort_key`` again only for the packages a
scan marks stale. It still walks the whole undecided set, which keeps those
calls in the order a full scan made them.
"""

from __future__ import annotations

from heapq import heapify, heappop, heappush
from typing import TYPE_CHECKING, Any, Generic

from .types import PackageType

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["DecisionQueue"]

# The heap's rebuild threshold never falls below this.
_REBUILD_MINIMUM = 32


class DecisionQueue(Generic[PackageType]):
    """The undecided packages ordered by sort key, refreshed incrementally.

    A key is re-evaluated when the partial solution reports the package's range
    or decided state as moved, when the caller passes a new ``epoch``, and on
    every scan while the package is not ready, since a listing lands without the
    solution seeing it. Superseded heap entries stay in place until they reach
    the top, or until a rebuild drops the ones that never do.
    """

    def __init__(self) -> None:
        """Start empty; every key arrives through :meth:`pick`."""
        self._heap: list[tuple[tuple[Any, ...], int, PackageType]] = []
        self._keys: dict[PackageType, tuple[Any, ...]] = {}
        self._unready: set[PackageType] = set()
        self._epoch = 0
        self._pushes = 0
        self._rebuild_at = _REBUILD_MINIMUM

    def clear(self) -> None:
        """Drop every key, for a resolve that starts over."""
        self._heap.clear()
        self._keys.clear()
        self._unready.clear()
        self._epoch = 0
        self._rebuild_at = _REBUILD_MINIMUM

    def pick(
        self,
        undecided: set[PackageType],
        sort_key: Callable[[PackageType], tuple[Any, ...]],
        changed: set[PackageType],
        epoch: int,
    ) -> PackageType:
        """Return the undecided package with the smallest sort key.

        ``undecided`` must be non-empty, and a package entering or leaving it
        must reach ``changed`` on the same scan, which is what gives it a key
        or drops the one it left behind. ``changed`` holds the packages whose
        range or decided state the partial solution moved since the previous
        scan; a new ``epoch`` stands for a move in the counts every key reads,
        so it re-evaluates all of them.

        ``sort_key`` must lead with the ready penalty: a truthy first field
        keeps the package on the re-evaluate list until it clears.
        """
        stale = self._stale_packages(undecided, changed, epoch)
        self._refresh(undecided, stale, sort_key)
        self._compact()
        return self._live_top()

    def _stale_packages(
        self, undecided: set[PackageType], changed: set[PackageType], epoch: int
    ) -> set[PackageType]:
        """Return the packages this scan has to evaluate again.

        Also forgets the key of any stale package that has left the undecided set.
        """
        if epoch != self._epoch:
            self._epoch = epoch
            stale = undecided | changed
        elif self._unready:
            stale = changed | self._unready
        else:
            stale = changed

        for package in stale - undecided:
            self._keys.pop(package, None)
            self._unready.discard(package)

        return stale

    def _refresh(
        self,
        undecided: set[PackageType],
        stale: set[PackageType],
        sort_key: Callable[[PackageType], tuple[Any, ...]],
    ) -> None:
        """Re-evaluate the stale keys, pushing an entry for each one that moved.

        Walks ``undecided`` rather than ``stale`` so which packages are stale
        cannot change the order ``sort_key`` is called in, since a provider can
        fetch while answering one.
        """
        keys = self._keys
        heap = self._heap
        unready = self._unready
        pushes = self._pushes

        for package in undecided:
            if package not in stale:
                continue

            key = sort_key(package)
            if key[0]:
                unready.add(package)
            else:
                unready.discard(package)

            # Keep the tuple the heap entry was pushed with rather than paying
            # for a second entry that sorts the same.
            live = keys.get(package)
            if live is not None and live == key:
                continue

            keys[package] = key
            heappush(heap, (key, pushes, package))
            pushes += 1

        self._pushes = pushes

    def _compact(self) -> None:
        """Rebuild the heap from the live keys once it outgrows ``_rebuild_at``."""
        if len(self._heap) <= self._rebuild_at:
            return

        self._heap[:] = [
            (key, index, package)
            for index, (package, key) in enumerate(self._keys.items(), self._pushes)
        ]
        heapify(self._heap)
        self._pushes += len(self._heap)
        self._rebuild_at = 4 * len(self._keys) + _REBUILD_MINIMUM

    def _live_top(self) -> PackageType:
        """Drop superseded entries until the top holds a package's current key.

        Current is identity rather than equality: ``_refresh`` keeps the tuple
        an entry was pushed with whenever a re-evaluated key compares equal, so
        an entry survives here only while ``_keys`` still points at it.
        """
        keys = self._keys
        heap = self._heap

        while True:
            key, _, package = heap[0]
            if keys.get(package) is key:
                return package
            heappop(heap)
