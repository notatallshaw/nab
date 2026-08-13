"""The fetch interface a host supplies to :class:`~nab_provider.provider.Provider`.

The provider reaches the world only through this interface: the index, the
directory read, the VCS clone, the archive download and the :pep:`517` build
all sit behind it.  nab's own implementation is
:class:`~nab_project.fetch.FetchCoordinator`, which fetches on a background
asyncio thread; the tests use :class:`~nab_provider.testing.FakeFetchPort`,
which serves from memory.

The source, build and two archive requests are reached only through a declared
source or a remote sdist build, so a host that offers neither may raise
:class:`NotImplementedError` from them.

The store is :class:`~nab_provider.store.InMemoryIndex` itself, not a protocol:
the provider reads its slots, a host's fetcher writes them.

A request registers its waiter under a pending key.
:func:`~nab_provider.store.metadata_pending_key` and
:func:`~nab_provider.store.range_pending_key` build the ``metadata:`` and
``range:`` keys; the ``listing:``, ``sdist:`` and ``sdist-archive:`` keys have
no builder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .policy import SourceRequest
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

    A request writes its result into :attr:`index` under the matching
    ``store_`` method, then releases its waitable.  A failed fetch records the
    error there rather than raising; only :meth:`request_source_listing` and
    :meth:`request_built_metadata` raise.

    ``indexes`` is not here: nab's coordinator carries it, to label each
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

    def request_source_listing(self, request: SourceRequest) -> Waitable:
        """Materialise a declared local, VCS or archive source into a listing.

        The host reads the directory, clones the repo or downloads and extracts
        the archive, reads the metadata it declares (running a :pep:`517`
        backend when ``request.build_policy`` permits it and the static read
        yields nothing), and stores the result under
        :meth:`~nab_provider.store.InMemoryIndex.store_source`.

        Answered inline: a failure raises
        :class:`~nab_provider.errors.UnsupportedSdistError`, or
        :class:`~nab_provider.errors.SourceBuildPolicyError` when the policy
        refused the build.
        """
        ...

    def request_built_metadata(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...],
    ) -> Waitable:
        """Build the sdist at ``url`` and store the METADATA it produced.

        The result lands under
        :meth:`~nab_provider.store.InMemoryIndex.store_built_metadata`.

        Answered inline: a failure raises
        :class:`~nab_provider.errors.UnsupportedSdistError`, or the integrity
        error the sdist fetch recorded.
        """
        ...
