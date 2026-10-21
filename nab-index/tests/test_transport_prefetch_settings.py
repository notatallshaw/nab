"""Built-in clients expose independent prefetch recommendations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from nab_index.httpx2_async_transport import Httpx2AsyncTransport
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.transport import TunedHttpTransport, prefetch_settings_for_backend
from nab_index.urllib3_async_transport import Urllib3AsyncTransport


@pytest.mark.parametrize(
    ("backend", "factory", "metadata", "lookahead"),
    [
        ("httpx2", Httpx2AsyncTransport, None, 0),
        ("httpx", HttpxAsyncTransport, None, 32),
        ("urllib3", Urllib3AsyncTransport, None, 16),
    ],
)
def test_backend_recommendations_match_real_clients(
    backend: str,
    factory: Callable[
        [], HttpxAsyncTransport | Httpx2AsyncTransport | Urllib3AsyncTransport
    ],
    metadata: int | None,
    lookahead: int,
) -> None:
    transport = factory()
    try:
        assert isinstance(transport, TunedHttpTransport)
        settings = transport.prefetch_settings
        assert settings.metadata_versions == metadata
        assert settings.lookahead_versions == lookahead
        other = prefetch_settings_for_backend(backend)
        other.lookahead_versions = 99
        assert transport.prefetch_settings.lookahead_versions == lookahead
    finally:
        asyncio.run(transport.aclose())


def test_unknown_backend_is_refused() -> None:
    with pytest.raises(ValueError, match="missing"):
        prefetch_settings_for_backend("missing")
