"""The fetch interface a host supplies to :class:`~nab_python.provider.Provider`.

The provider reaches an index only through this interface; it still reads local
directories, clones VCS references and runs :pep:`517` builds itself.

nab's own implementation is :class:`~nab_python.fetch.FetchCoordinator`, which
fetches on a background asyncio thread; the tests use
:class:`~nab_python._testing.coordinator_fake.FakeFetchPort`, which serves from
memory.

The two archive requests are reached only through a declared archive source or
a remote sdist build, so a host that offers neither may raise from them.

A request registers its waiter under a pending key.
:func:`~nab_python.store.metadata_pending_key` and
:func:`~nab_python.store.range_pending_key` build the ``metadata:`` and
``range:`` keys; the ``listing:``, ``sdist:`` and ``sdist-archive:`` keys have
no builder.

The store is a class, not a protocol: a protocol over it would restate
:class:`~nab_python.store.InMemoryIndex` method for method. A reader wanting one
slot declares that slice itself, as :mod:`nab_python._lockfile.builder` does for
the serving-index label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .store import InMemoryIndex

__all__ = [
    "FetchPort",
    "Waitable",
]


@runtime_checkable
class Waitable(Protocol):
    """What a request hands back for the caller to block on."""

    def wait(self) -> object:
        """Block until the requested data has landed in the store.

        Nothing reads the return value, so :class:`threading.Event` satisfies
        this.
        """
        ...


@runtime_checkable
class FetchPort(Protocol):
    """The fetch handle the provider holds.

    ``indexes`` is not here: nab's engine reads it at one site, to label each
    package's serving index in a lock file.
    """

    @property
    def index(self) -> InMemoryIndex:
        """The store this port writes fetched data into."""
        ...

    @property
    def offline(self) -> bool:
        """Whether this run may read a cache only, never the network."""
        ...

    def request_listing(self, package: str, *, speculative: bool = False) -> Waitable:
        """Request the files an index publishes for ``package``.

        ``speculative`` marks a prefetch the provider will not wait on, so a
        host may serve it behind the requests that block.
        """
        ...

    def request_metadata(
        self,
        package: str,
        version: str,
        url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> Waitable:
        """Request the :pep:`658` sidecar at ``url`` for ``(package, version)``."""
        ...

    def request_metadata_batch(
        self, items: list[tuple[str, str, str, tuple[str, str] | None]]
    ) -> Sequence[tuple[str, str, Waitable]]:
        """Request several sidecars at once.

        Each item is ``(package, version, url, metadata_hash)``; each result is
        ``(package, version, waitable)``.
        """
        ...

    def request_range_metadata(
        self,
        package: str,
        version: str,
        wheel_url: str,
        wheel_hashes: tuple[tuple[str, str], ...] = (),
    ) -> Waitable:
        """Request the METADATA of a wheel that publishes no sidecar."""
        ...

    def request_sdist(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> Waitable:
        """Request the PKG-INFO of the sdist at ``url``."""
        ...

    def request_sdist_archive(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> Waitable:
        """Request the bytes of the sdist at ``url``, for a local build."""
        ...

    def request_direct_archive(self, package: str, version: str, url: str) -> Waitable:
        """Request the bytes of an archive named by URL rather than by an index."""
        ...
