"""Tests for urllib3 transport pooling and the default request headers."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import pytest

from nab_index.transport import DEFAULT_HEADERS, USER_AGENT, _user_agent
from nab_index.urllib3_async_transport import Urllib3AsyncTransport


def test_pool_reused_within_thread() -> None:
    # The pool is created lazily on first use and cached per worker thread,
    # so a second call on the same thread returns the same PoolManager.
    transport = Urllib3AsyncTransport()
    first = transport._pool()
    second = transport._pool()
    assert first is second


def test_user_agent_names_nab_index_and_its_version() -> None:
    assert f"nab-index/{version('nab-index')}" == USER_AGENT


def test_user_agent_falls_back_without_installed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("nab_index.transport.version", missing)
    assert _user_agent() == "nab-index/0.0.0+unknown"


def test_default_headers_ask_for_gzip_and_name_nab_index() -> None:
    assert DEFAULT_HEADERS == {"Accept-Encoding": "gzip", "User-Agent": USER_AGENT}
