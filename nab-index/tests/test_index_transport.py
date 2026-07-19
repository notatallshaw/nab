"""Tests for nab_index.urllib3_async_transport pooling."""

from __future__ import annotations

from nab_index.urllib3_async_transport import Urllib3AsyncTransport


def test_pool_reused_within_thread() -> None:
    # The pool is created lazily on first use and cached per worker thread,
    # so a second call on the same thread returns the same PoolManager.
    transport = Urllib3AsyncTransport()
    first = transport._pool()
    second = transport._pool()
    assert first is second
