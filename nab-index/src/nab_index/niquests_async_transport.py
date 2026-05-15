"""niquests-based async HTTP transport for nab-index."""

from __future__ import annotations

from typing import TYPE_CHECKING

import niquests

if TYPE_CHECKING:
    from .transport import HttpResponse

__all__ = [
    "NiquestsAsyncTransport",
]


class NiquestsAsyncTransport:
    """Async HTTP transport using niquests.

    HTTP/2 and HTTP/3 are enabled by default. OCSP/CRL revocation
    checks are disabled because (a) other Python HTTP libraries
    do not perform them and including them skews benchmark
    comparisons, and (b) PyPI's CDN serves a small set of
    well-known certs whose revocation we care about less than
    request throughput.
    """

    def __init__(self, *, pool_maxsize: int = 50) -> None:
        """Create a transport."""
        self._client = niquests.AsyncSession(
            revocation_configuration=None,
            pool_connections=pool_maxsize,
            pool_maxsize=pool_maxsize,
        )

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        """Send a GET request."""
        # niquests.Response satisfies the HttpResponse protocol structurally;
        # the headers slot is niquests's CaseInsensitiveDict rather than a
        # literal Mapping[str, str].
        return await self._client.get(url, headers=headers)  # type: ignore[return-value]

    async def aclose(self) -> None:
        """Close the underlying client."""
        await self._client.close()
