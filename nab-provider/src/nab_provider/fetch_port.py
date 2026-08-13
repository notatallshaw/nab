"""The fetch interface a host supplies to :class:`~nab_provider.provider.Provider`.

The provider reaches the world only through this interface: the index, the
directory read, the VCS clone, the archive download and the :pep:`517` build
all sit behind it, so a host that implements the port owns all of the
provider's I/O.

nab's own implementation is :class:`~nab_python.fetch.FetchCoordinator`, which
fetches on a background asyncio thread; the tests use
:class:`~nab_python._testing.coordinator_fake.FakeFetchPort`, which serves from
memory.

Every host needs the store, the listing request and the metadata requests. The
archive, source and build requests are reached only through a declared source
or a remote sdist build, so a host that offers neither may implement them as a
raise.

Two members are the exception and say so on themselves:
:meth:`FetchPort.request_source_listing` and
:meth:`FetchPort.request_built_metadata` run a build backend, so they are
answered inline and report failure by raising rather than by writing an error
slot.  There is nothing for the provider to do while one is outstanding.

A request registers its waiter under a pending key.
:func:`~nab_provider.store.metadata_pending_key` and
:func:`~nab_provider.store.range_pending_key` build the ``metadata:`` and
``range:`` keys; the ``listing:``, ``sdist:`` and ``sdist-archive:`` keys have
no builder.

The store is a class, not a protocol: a protocol over it would restate
:class:`~nab_provider.store.InMemoryIndex` method for method. A reader wanting one
slot declares that slice itself, as :mod:`nab_python._lockfile.builder` does for
the serving-index label.
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

    def request_source_listing(self, request: SourceRequest) -> Waitable:
        """Materialise a declared local, VCS or archive source into a listing.

        The host reads the directory, clones the repository or downloads and
        extracts the archive, reads the metadata it declares (running a
        :pep:`517` backend when ``request.build_policy`` permits it and the
        static read yields nothing), and stores the result under
        :meth:`~nab_provider.store.InMemoryIndex.store_source`.  Answered inline:
        a failure raises :class:`~nab_provider.errors.UnsupportedSdistError`, or
        :class:`~nab_provider.errors.SourceBuildPolicyError` when the policy is
        what refused it.  A host that passes no declared sources never reaches
        it.
        """
        ...

    def request_built_metadata(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...],
    ) -> Waitable:
        """Build an sdist and store the METADATA the build produced.

        ``url`` names the sdist the provider picked out of the listing and
        ``sdist_hashes`` are the digests the index published for it.  The
        result lands under
        :meth:`~nab_provider.store.InMemoryIndex.store_built_metadata` and the
        provider checks it against the candidate it asked for.  Answered
        inline, like :meth:`request_source_listing`.  A host that owns building
        above the port never reaches it.
        """
        ...
