"""End-to-end integration for the HTTP range-metadata fallback (rung 4).

One flow proves the seams between coordinator, client, reader, provider, and
cache: a full resolve through a real :class:`FetchCoordinator` and a
:class:`FakeRangeTransport` locks a package whose only artifact is a
sidecar-less wheel, recovering its METADATA over ranged reads. A warmed-cache
offline replay then serves the recovered METADATA from the cache with no range
request at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from typing import TYPE_CHECKING

import pytest

from nab_index.client import WheelHashMismatchError
from nab_index.lazy_wheel import RangeOutcome
from nab_index.transport import HttpError
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.version import Version
from nab_python.config import NabProjectConfig
from nab_python.fetch import FetchCoordinator
from nab_python.provider import BuildPolicy
from nab_python.resolve import ResolveResult, resolve_with_coordinator
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget

if TYPE_CHECKING:
    from pathlib import Path

_META = b"Metadata-Version: 2.1\nName: widget\nVersion: 1.0\n\nBody text.\n"
_WHEEL_URL = "https://files.example.org/packages/widget-1.0-py3-none-any.whl"
_SIMPLE_URL = "https://pypi.org/simple/widget/"
_JSON_MEDIA = "application/vnd.pypi.simple.v1+json"


def _wheel_bytes(metadata: bytes = _META) -> bytes:
    """A small sidecar-less wheel holding widget's METADATA."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("widget/__init__.py", b"value = 1\n")
        zf.writestr("widget-1.0.dist-info/METADATA", metadata)
        zf.writestr("widget-1.0.dist-info/WHEEL", b"Wheel-Version: 1.0\n")
    return buf.getvalue()


def _listing_body(hashes: dict[str, str] | None = None) -> bytes:
    """A Simple-API listing whose one wheel publishes no PEP 658 sidecar."""
    entry: dict[str, object] = {
        "filename": "widget-1.0-py3-none-any.whl",
        "url": _WHEEL_URL,
    }
    if hashes is not None:
        entry["hashes"] = hashes
    return json.dumps(
        {
            "meta": {"api-version": "1.0"},
            "name": "widget",
            "files": [entry],
        }
    ).encode("utf-8")


def _parse_range(value: str) -> tuple[str, int, int]:
    body = value.removeprefix("bytes=")
    if body.startswith("-"):
        return ("suffix", int(body[1:]), 0)
    start, _, end = body.partition("-")
    return ("absolute", int(start), int(end))


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], content: bytes) -> None:
        self._status = status
        self._headers = headers
        self._content = content

    @property
    def status_code(self) -> int:
        return self._status

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        return self._content.decode("utf-8")

    def json(self) -> object:
        return json.loads(self._content)

    def raise_for_status(self) -> None:
        if self._status >= 400:
            msg = f"HTTP {self._status}"
            raise HttpError(msg)


class FakeRangeTransport:
    """Serve a Simple listing on a plain GET and wheel bytes over ranges.

    A request for the wheel URL is a wheel read; every other GET is the
    listing fetch. Both are counted so a test can assert a warm offline
    replay touched the network zero times. ``wheel_status`` makes every
    wheel request answer that status instead, for failure-path tests.
    """

    def __init__(
        self,
        wheel: bytes,
        listing: bytes,
        *,
        wheel_status: int | None = None,
        ignore_range: bool = False,
    ) -> None:
        self.wheel = wheel
        self.total = len(wheel)
        self.listing = listing
        self.wheel_status = wheel_status
        self.ignore_range = ignore_range
        self.requests: list[tuple[str, str | None]] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        await asyncio.sleep(0)
        headers = headers or {}
        rng = headers.get("Range")
        self.requests.append((url, rng))
        if url != _WHEEL_URL:
            return _FakeResponse(200, {"content-type": _JSON_MEDIA}, self.listing)
        if self.wheel_status is not None:
            return _FakeResponse(self.wheel_status, {}, b"")
        if rng is None or self.ignore_range:
            return _FakeResponse(200, {}, self.wheel)
        return self._range(rng)

    async def aclose(self) -> None:
        return None

    @property
    def range_requests(self) -> list[str]:
        return [url for url, rng in self.requests if rng is not None]

    @property
    def listing_requests(self) -> list[str]:
        return [url for url, rng in self.requests if rng is None]

    def _range(self, value: str) -> _FakeResponse:
        kind, a, b = _parse_range(value)
        if kind == "suffix":
            start, end = max(0, self.total - a), self.total - 1
        else:
            start, end = a, min(b, self.total - 1)
        headers = {"content-range": f"bytes {start}-{end}/{self.total}"}
        return _FakeResponse(206, headers, self.wheel[start : end + 1])


_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)
_NO_BUILD = NabProjectConfig(build_policy=BuildPolicy.NEVER)


def _resolve(coordinator: FetchCoordinator) -> ResolveResult:
    result = resolve_with_coordinator(
        coordinator,
        [_TARGET],
        [Requirement("widget")],
        config=_NO_BUILD,
    )
    result.raise_for_failure()
    return result


def test_cold_resolve_recovers_metadata_over_range(tmp_path: Path) -> None:
    """A sidecar-less wheel locks by recovering METADATA over ranged reads."""
    transport = FakeRangeTransport(_wheel_bytes(), _listing_body())
    with FetchCoordinator(transport, cache_dir=tmp_path) as coordinator:  # type: ignore[arg-type]
        result = _resolve(coordinator)
        assert result.success
        assert result.target_results[0].pins == {"widget": Version("1.0")}
        assert coordinator.index.get_range_outcome("widget", "1.0", _WHEEL_URL) is (
            RangeOutcome.PARTIAL
        )

    # The listing was fetched once, and the small wheel's whole METADATA came
    # back in the single suffix read: any extra request here is a regression
    # toward the request amplification pip's fast-deps suffers.
    assert transport.listing_requests == [_SIMPLE_URL]
    assert transport.range_requests == [_WHEEL_URL]


def test_resolve_fails_when_wheel_url_unserved(tmp_path: Path) -> None:
    """An advertised wheel the index cannot serve fails the resolve loudly."""
    transport = FakeRangeTransport(_wheel_bytes(), _listing_body(), wheel_status=404)
    with (
        FetchCoordinator(transport, cache_dir=tmp_path) as coordinator,  # type: ignore[arg-type]
        pytest.raises(HttpError),
    ):
        _resolve(coordinator)


def test_full_body_wheel_matching_published_hash_resolves(tmp_path: Path) -> None:
    """A full-body wheel matching its published hash locks over rung 4."""
    served = _wheel_bytes()
    published = hashlib.sha256(served).hexdigest()
    transport = FakeRangeTransport(
        served, _listing_body({"sha256": published}), ignore_range=True
    )
    with FetchCoordinator(transport, cache_dir=tmp_path) as coordinator:  # type: ignore[arg-type]
        result = _resolve(coordinator)
        assert result.success
        assert result.target_results[0].pins == {"widget": Version("1.0")}
        assert coordinator.index.get_range_outcome("widget", "1.0", _WHEEL_URL) is (
            RangeOutcome.FULL_BODY
        )


def test_full_body_wheel_failing_published_hash_aborts(tmp_path: Path) -> None:
    """A full-body wheel whose bytes disagree with the published hash aborts.

    The host ignores Range and returns the whole wheel; its bytes fail the
    listing's sha256 (mirror skew, a stale hash after a rebuild), so its
    METADATA never drives the resolve.
    """
    served = _wheel_bytes()
    published = hashlib.sha256(_wheel_bytes(_META + b"skew\n")).hexdigest()
    transport = FakeRangeTransport(
        served, _listing_body({"sha256": published}), ignore_range=True
    )
    with (
        FetchCoordinator(transport, cache_dir=tmp_path) as coordinator,  # type: ignore[arg-type]
        pytest.raises(WheelHashMismatchError),
    ):
        _resolve(coordinator)


def test_warm_offline_replay_serves_cache_without_ranging(tmp_path: Path) -> None:
    """A warmed cache serves the recovered METADATA offline with no range read."""
    warm = FakeRangeTransport(_wheel_bytes(), _listing_body())
    with FetchCoordinator(warm, cache_dir=tmp_path) as coordinator:  # type: ignore[arg-type]
        _resolve(coordinator)
    assert warm.range_requests

    offline = FakeRangeTransport(_wheel_bytes(), _listing_body())
    with FetchCoordinator(  # type: ignore[arg-type]
        offline, cache_dir=tmp_path, offline=True
    ) as coordinator:
        result = _resolve(coordinator)
        assert result.success
        assert result.target_results[0].pins == {"widget": Version("1.0")}
        assert coordinator.index.get_range_outcome("widget", "1.0", _WHEEL_URL) is (
            RangeOutcome.PARTIAL
        )

    # Offline over a warm cache: neither the listing nor the wheel was fetched.
    assert offline.requests == []
