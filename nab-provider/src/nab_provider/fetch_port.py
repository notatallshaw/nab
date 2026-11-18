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

Queued fetches publish their results in :class:`~nab_provider.store.InMemoryIndex`.
Declared sources and build metadata are returned directly to the caller.

A queued request registers its waiter under a pending key.
:func:`~nab_provider.store.metadata_pending_key` and
:func:`~nab_provider.store.range_pending_key` build the ``metadata:`` and
``range:`` keys; the ``listing:``, ``sdist:`` and ``sdist-archive:`` keys have
no builder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from .metadata import WheelMetadata
    from .policy import SourceMaterialization, SourceRequest
    from .records import WheelFile
    from .store import InMemoryIndex

__all__ = [
    "FetchPort",
    "MetadataArtifactHints",
    "MetadataPrefetchAdmission",
    "MetadataPrefetchDeferral",
    "MetadataPrefetchHints",
    "PrefetchSettings",
    "Waitable",
]


class PrefetchSettings:
    """Version windows to prefetch; None leaves the caller's default in place."""

    __slots__ = ("lookahead_versions", "metadata_versions")

    def __init__(
        self,
        *,
        metadata_versions: int | None = None,
        lookahead_versions: int | None = None,
    ) -> None:
        """Set listing and initial lookahead windows; zero disables that prefetch."""
        if metadata_versions is not None and metadata_versions < 0:
            msg = "metadata_versions must be non-negative"
            raise ValueError(msg)
        if lookahead_versions is not None and lookahead_versions < 0:
            msg = "lookahead_versions must be non-negative"
            raise ValueError(msg)
        self.metadata_versions = metadata_versions
        self.lookahead_versions = lookahead_versions


@runtime_checkable
class MetadataArtifactHints(Protocol):
    """Optional wheel ranking for listing-arrival metadata guesses."""

    def set_metadata_prefetch_selector(
        self, selector: Callable[[list[WheelFile]], WheelFile]
    ) -> None:
        """Use a pure selector over a non-empty candidate list on the fetch loop."""
        ...


@runtime_checkable
class MetadataPrefetchAdmission(Protocol):
    """Optional admission of default listing-arrival metadata guesses."""

    def set_metadata_prefetch_admission(
        self, admit: Callable[[list[WheelFile]], bool] | None
    ) -> None:
        """Use a pure candidate-group predicate; explicit windows take priority."""
        ...


@runtime_checkable
class MetadataPrefetchHints(Protocol):
    """Optional version filters for listing-arrival metadata guesses."""

    def set_metadata_prefetch_filters(
        self, filters: Mapping[str, Callable[[str], bool]]
    ) -> None:
        """Replace the speculative filters without restricting required fetches."""
        ...


@runtime_checkable
class MetadataPrefetchDeferral(Protocol):
    """Optional deferral of default guesses until the provider has constraints."""

    def set_metadata_prefetch_deferral(
        self, defer: Callable[[str], bool] | None
    ) -> bool:
        """Return whether automatic deferral applies; explicit windows take priority."""
        ...


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

    Queued requests publish a result or fetch error in :attr:`index` before
    releasing the waiter. :meth:`materialize_source` and :meth:`build_sdist_metadata`
    return a result or raise on the calling thread.

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
        """Request the PKG-INFO of the sdist at ``url``.

        Publish metadata and pyproject slots using
        :func:`~nab_provider.store.sdist_artifact_key` on ``version`` and ``url``.
        """
        ...

    def request_sdist_archive(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> Waitable:
        """Request the bytes of the sdist at ``url``, for a local build.

        Publish the archive slot using
        :func:`~nab_provider.store.sdist_artifact_key` on ``version`` and ``url``.
        """
        ...

    def request_direct_archive(self, package: str, version: str, url: str) -> Waitable:
        """Request the bytes of an archive named by URL rather than by an index."""
        ...

    def materialize_source(self, request: SourceRequest) -> SourceMaterialization:
        """Return the tree and metadata for this declared local, VCS or archive source.

        The host reads, clones or downloads the source and may run a :pep:`517`
        backend if its policy permits. A build refusal raises
        :class:`~nab_provider.errors.SourceBuildPolicyError`.
        """
        ...

    def build_sdist_metadata(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...],
    ) -> WheelMetadata:
        """Return the built metadata, propagating build and integrity errors."""
        ...
