"""Tests for nab_index.multi_index.MultiIndexClient forwarding."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

from packaging.utils import canonicalize_name as canonical

from nab_index.client import WheelFile
from nab_index.multi_index import MultiIndexClient

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

_T = TypeVar("_T")


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coro)


def _wheel(name: str, version: str = "1.0") -> WheelFile:
    return WheelFile(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://example.com/{name}-{version}-py3-none-any.whl",
        version=version,
        requires_python=None,
        has_metadata=False,
        upload_time=None,
    )


class _RecordingClient:
    """Client stub that records get_sdist_archive calls."""

    def __init__(self, listing: dict[str, list[WheelFile]], payload: bytes) -> None:
        self._listing = listing
        self._payload = payload
        self.archive_calls: list[tuple[str, str, str]] = []

    async def get_files(self, package: str) -> list[WheelFile]:
        return list(self._listing.get(canonical(package), []))

    def served_unreadable_only(self, package: str) -> bool:
        return False

    def served_all_yanked(self, package: str) -> bool:
        return False

    def served_zip_sdists(self, package: str) -> frozenset[str]:
        return frozenset()

    async def get_sdist_archive(
        self,
        package: str,
        version: str,
        sdist_url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> bytes:
        self.archive_calls.append((package, version, sdist_url))
        return self._payload


def test_get_sdist_archive_forwards_to_routed_client() -> None:
    empty = _RecordingClient({}, b"")
    holder = _RecordingClient({"foo": [_wheel("foo")]}, b"ARCHIVE")
    client = MultiIndexClient({"a": empty, "b": holder}, ["a", "b"], {})

    # get_files routes "foo" to the index that lists it; get_sdist_archive
    # must then reuse that route rather than probing every index again.
    run(client.get_files("foo"))
    data = run(client.get_sdist_archive("foo", "1.0", "https://x/s.tar.gz"))

    assert data == b"ARCHIVE"
    assert holder.archive_calls == [("foo", "1.0", "https://x/s.tar.gz")]
    assert empty.archive_calls == []
