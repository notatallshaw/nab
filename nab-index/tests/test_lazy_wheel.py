"""Tests for nab_index.lazy_wheel, the HTTP range metadata reader."""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from typing import TYPE_CHECKING

import pytest
from packaging.utils import canonicalize_name

from nab_index.client import MalformedSimpleResponseError, WheelHashMismatchError
from nab_index.lazy_wheel import (
    RangeCapability,
    RangeCapabilityMemo,
    RangeMetadataResult,
    RangeOutcome,
    _SparseFile,
    read_wheel_metadata_over_range,
)
from nab_index.transport import HttpError

if TYPE_CHECKING:
    from collections.abc import Mapping

_META = b"Metadata-Version: 2.1\nName: widget\nVersion: 1.0\n\nBody text.\n"
_URL = "https://files.example.org/packages/widget-1.0-py3-none-any.whl"


def build_wheel(
    *,
    dist_info_dirs: tuple[str, ...] = ("widget-1.0.dist-info",),
    metadata: bytes | None = _META,
    compression: int = zipfile.ZIP_DEFLATED,
    padding: int = 0,
    force_zip64: bool = False,
) -> bytes:
    """Build a wheel zip in memory with controllable layout."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        if padding:
            zf.writestr("widget/_pad.bin", b"\x00" * padding)
        zf.writestr("widget/__init__.py", b"value = 1\n")
        for info in dist_info_dirs:
            if metadata is not None:
                if force_zip64:
                    with zf.open(
                        zipfile.ZipInfo(f"{info}/METADATA"),
                        mode="w",
                        force_zip64=True,
                    ) as handle:
                        handle.write(metadata)
                else:
                    zf.writestr(f"{info}/METADATA", metadata)
            zf.writestr(f"{info}/WHEEL", b"Wheel-Version: 1.0\n")
    return buf.getvalue()


def build_wheel_member_front(padding: int = 20000) -> bytes:
    """Build a wheel whose METADATA sits at offset 0 and a big stored blob after.

    With a default tail the central directory is inside the window but the
    front-loaded METADATA member is not, forcing a member-range fetch.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("widget-1.0.dist-info/METADATA", _META)
        zf.writestr("widget-1.0.dist-info/WHEEL", b"Wheel-Version: 1.0\n")
        zf.writestr("widget/_pad.bin", b"\x00" * padding)
    return buf.getvalue()


def build_wheel_member_last(metadata: bytes) -> bytes:
    """Build a wheel whose METADATA is the last member by offset.

    A stored blob and WHEEL precede METADATA, so METADATA has the largest
    header offset and _member_span bounds it with the central-directory start
    rather than a following entry.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("widget/_pad.bin", b"\x00" * 20000)
        zf.writestr("widget-1.0.dist-info/WHEEL", b"Wheel-Version: 1.0\n")
        zf.writestr("widget-1.0.dist-info/METADATA", metadata)
    return buf.getvalue()


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
    def headers(self) -> Mapping[str, str]:
        return self._headers

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        return self._content.decode("utf-8")

    def json(self) -> object:
        import json

        return json.loads(self._content)

    def raise_for_status(self) -> None:
        if self._status >= 400:
            msg = f"HTTP {self._status}"
            raise HttpError(msg)


class FakeRangeTransport:
    """Serve wheel bytes over ranges per a named server-quirk mode."""

    def __init__(self, mode: str, wheel_bytes: bytes) -> None:
        self.mode = mode
        self.wheel = wheel_bytes
        self.total = len(wheel_bytes)
        self.requests: list[tuple[str | None, str | None]] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        await asyncio.sleep(0)
        headers = headers or {}
        rng = headers.get("Range")
        enc = headers.get("Accept-Encoding")
        self.requests.append((rng, enc))
        if rng is None:
            return self._full()
        return self._respond(rng)

    async def aclose(self) -> None:
        return None

    @property
    def negative_requests(self) -> int:
        return sum(1 for rng, _ in self.requests if rng and "=-" in rng)

    def _partial(self, start: int, end: int, *, gzip: bool = False) -> _FakeResponse:
        end = min(end, self.total - 1)
        data = self.wheel[start : end + 1]
        headers = {"content-range": f"bytes {start}-{end}/{self.total}"}
        if gzip:
            headers["content-encoding"] = "gzip"
        return _FakeResponse(206, headers, data)

    def _full(self, *, gzip: bool = False) -> _FakeResponse:
        headers = {"content-encoding": "gzip"} if gzip else {}
        return _FakeResponse(200, headers, self.wheel)

    def _respond(self, rng: str) -> _FakeResponse:
        kind, a, b = _parse_range(rng)
        if self.mode == "well_behaved":
            if kind == "suffix":
                return self._partial(max(0, self.total - a), self.total - 1)
            return self._partial(a, b)
        if self.mode == "suffix_501":
            if kind == "suffix":
                return _FakeResponse(501, {}, b"")
            return self._partial(a, b)
        if self.mode == "ignore_range_200":
            return self._full()
        if self.mode == "misread_suffix":
            if kind == "suffix":
                data = self.wheel[:a]
                headers = {"content-range": f"bytes 0-{len(data) - 1}/{self.total}"}
                return _FakeResponse(206, headers, data)
            return self._partial(a, b)
        if self.mode == "no_ranges":
            if kind == "suffix":
                return _FakeResponse(416, {}, b"")
            return self._full()
        if self.mode.startswith("reject_"):
            return _FakeResponse(int(self.mode.removeprefix("reject_")), {}, b"")
        if self.mode == "miss_200_then_206":
            ranged = sum(1 for rng, _ in self.requests if rng is not None)
            if ranged == 1:
                return self._full()
            if kind == "suffix":
                return self._partial(max(0, self.total - a), self.total - 1)
            return self._partial(a, b)
        if self.mode == "gzip_range":
            if kind == "suffix":
                return self._partial(max(0, self.total - a), self.total - 1, gzip=True)
            return self._partial(a, b, gzip=True)
        if self.mode == "error_404":
            return _FakeResponse(404, {}, b"")
        if self.mode == "error_500":
            return _FakeResponse(500, {}, b"")
        msg = f"unknown mode {self.mode}"
        raise AssertionError(msg)


_NAME = canonicalize_name("widget")


def _read(transport: object, url: str = _URL, **kwargs: object) -> RangeMetadataResult:
    return asyncio.run(
        read_wheel_metadata_over_range(
            transport,  # type: ignore[arg-type]
            url,
            _NAME,
            RangeCapabilityMemo(),
            **kwargs,  # type: ignore[arg-type]
        )
    )


@pytest.mark.parametrize(
    ("mode", "outcome", "expected_requests"),
    [
        ("well_behaved", RangeOutcome.PARTIAL, 1),
        ("suffix_501", RangeOutcome.PARTIAL, 3),
        ("ignore_range_200", RangeOutcome.FULL_BODY, 1),
        ("no_ranges", RangeOutcome.FULL_BODY, 2),
        ("reject_400", RangeOutcome.FULL_BODY, 3),
        ("reject_403", RangeOutcome.FULL_BODY, 3),
        ("reject_416", RangeOutcome.FULL_BODY, 3),
        ("reject_501", RangeOutcome.FULL_BODY, 3),
    ],
)
def test_recovers_metadata_per_mode(
    mode: str, outcome: RangeOutcome, expected_requests: int
) -> None:
    wheel = build_wheel()
    transport = FakeRangeTransport(mode, wheel)
    result = _read(transport)
    assert result.outcome is outcome
    assert result.text == _META.decode("utf-8")
    assert all(enc == "identity" for _, enc in transport.requests)
    # Exact counts: creeping per-wheel requests are the amplification failure
    # mode this reader exists to avoid.
    assert len(transport.requests) == expected_requests


def test_full_body_matching_published_hash_is_consumed() -> None:
    wheel = build_wheel()
    published = ("sha256", hashlib.sha256(wheel).hexdigest())
    transport = FakeRangeTransport("ignore_range_200", wheel)
    result = _read(transport, wheel_hash=published)
    assert result.outcome is RangeOutcome.FULL_BODY
    assert result.text == _META.decode("utf-8")


def test_full_body_failing_published_hash_is_rejected() -> None:
    # The host ignores Range and returns the whole wheel, but its bytes disagree
    # with the published digest (mirror skew, a stale hash after a rebuild).
    served = build_wheel(
        metadata=b"Metadata-Version: 2.1\nName: widget\nVersion: 1.0\n"
        b"Requires-Dist: attacker-dep>=9\n\n"
    )
    published = ("sha256", hashlib.sha256(build_wheel()).hexdigest())
    transport = FakeRangeTransport("ignore_range_200", served)
    with pytest.raises(WheelHashMismatchError):
        _read(transport, wheel_hash=published)


def test_misread_suffix_downgrades_to_absolute() -> None:
    wheel = build_wheel_member_front()
    transport = FakeRangeTransport("misread_suffix", wheel)
    result = _read(transport, tail_size=64)
    assert result.outcome is RangeOutcome.PARTIAL
    assert result.text == _META.decode("utf-8")


def test_gzip_range_is_unsupported() -> None:
    transport = FakeRangeTransport("gzip_range", build_wheel())
    result = _read(transport)
    assert result.outcome is RangeOutcome.UNSUPPORTED
    assert result.text is None


def test_range_rejecting_host_memoizes_full_body() -> None:
    async def run() -> tuple[RangeMetadataResult, int]:
        memo = RangeCapabilityMemo()
        transport = FakeRangeTransport("reject_403", build_wheel())
        await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        capability = memo.capability("files.example.org")
        assert capability is RangeCapability.FULL_BODY_ONLY
        before = len(transport.requests)
        result = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        return result, len(transport.requests) - before

    result, second_requests = asyncio.run(run())
    assert result.outcome is RangeOutcome.FULL_BODY
    assert result.text == _META.decode("utf-8")
    # The refused probes are not repeated: later wheels go straight to the GET.
    assert second_requests == 1


def test_cdn_miss_200_then_206_returns_to_ranges() -> None:
    async def run() -> tuple[RangeMetadataResult, RangeMetadataResult, int]:
        memo = RangeCapabilityMemo()
        transport = FakeRangeTransport("miss_200_then_206", build_wheel_member_front())
        first = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        assert first.text == _META.decode("utf-8")
        assert memo.capability("files.example.org") is RangeCapability.SUFFIX_OK
        after_first = len(transport.requests)
        second = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        return first, second, len(transport.requests) - after_first

    first, second, second_requests = asyncio.run(run())
    assert first.outcome is RangeOutcome.FULL_BODY
    assert second.outcome is RangeOutcome.PARTIAL
    assert second.text == _META.decode("utf-8")
    assert second_requests == 2


@pytest.mark.parametrize("mode", ["error_404", "error_500"])
def test_wheel_url_error_raises(mode: str) -> None:
    transport = FakeRangeTransport(mode, build_wheel())
    with pytest.raises(HttpError):
        _read(transport)


def test_growth_loop_when_directory_beyond_tail() -> None:
    wheel = build_wheel(padding=4000)
    transport = FakeRangeTransport("well_behaved", wheel)
    result = _read(transport, tail_size=64)
    assert result.outcome is RangeOutcome.PARTIAL
    assert result.text == _META.decode("utf-8")


def test_growth_cap_returns_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import nab_index.lazy_wheel as lw

    monkeypatch.setattr(lw, "_MAX_TAIL", 48)
    wheel = build_wheel(padding=4000)
    transport = FakeRangeTransport("well_behaved", wheel)
    result = _read(transport, tail_size=16)
    assert result.outcome is RangeOutcome.MISSING
    assert result.text is None


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_compression_methods(compression: int) -> None:
    wheel = build_wheel(compression=compression)
    transport = FakeRangeTransport("well_behaved", wheel)
    result = _read(transport)
    assert result.text == _META.decode("utf-8")


def test_zip64_member() -> None:
    wheel = build_wheel(force_zip64=True)
    transport = FakeRangeTransport("well_behaved", wheel)
    result = _read(transport)
    assert result.text == _META.decode("utf-8")


@pytest.mark.parametrize("mode", ["well_behaved", "ignore_range_200"])
def test_no_dist_info_is_missing(mode: str) -> None:
    wheel = build_wheel(dist_info_dirs=())
    transport = FakeRangeTransport(mode, wheel)
    result = _read(transport)
    assert result.outcome is RangeOutcome.MISSING
    assert result.text is None


def test_dist_info_without_metadata_is_missing() -> None:
    wheel = build_wheel(metadata=None)
    transport = FakeRangeTransport("well_behaved", wheel)
    result = _read(transport)
    assert result.outcome is RangeOutcome.MISSING


def test_multiple_dist_info_is_missing() -> None:
    wheel = build_wheel(
        dist_info_dirs=("widget-1.0.dist-info", "widget-1.0.data.dist-info")
    )
    transport = FakeRangeTransport("well_behaved", wheel)
    result = _read(transport)
    assert result.outcome is RangeOutcome.MISSING


def test_foreign_dist_info_is_missing() -> None:
    wheel = build_wheel(dist_info_dirs=("gadget-2.0.dist-info",))
    transport = FakeRangeTransport("well_behaved", wheel)
    result = _read(transport)
    assert result.outcome is RangeOutcome.MISSING


@pytest.mark.parametrize("mode", ["well_behaved", "ignore_range_200"])
def test_non_utf8_metadata_raises(mode: str) -> None:
    wheel = build_wheel(metadata=b"\xff\xfe not utf-8")
    transport = FakeRangeTransport(mode, wheel)
    with pytest.raises(MalformedSimpleResponseError):
        _read(transport)


def test_corrupt_member_is_missing() -> None:
    wheel = bytearray(build_wheel(compression=zipfile.ZIP_STORED))
    marker = _META[:8]
    idx = wheel.find(marker)
    assert idx != -1
    wheel[idx] ^= 0xFF
    transport = FakeRangeTransport("well_behaved", bytes(wheel))
    result = _read(transport)
    assert result.outcome is RangeOutcome.MISSING


def test_full_body_not_a_zip_is_missing() -> None:
    transport = FakeRangeTransport("ignore_range_200", b"this is not a zip file")
    result = _read(transport)
    assert result.outcome is RangeOutcome.MISSING


class _ScriptedTransport:
    """Serve responses from per-request-shape callables."""

    def __init__(self, wheel: bytes, script: object) -> None:
        self.wheel = wheel
        self.total = len(wheel)
        self.script = script
        self.requests: list[str] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        await asyncio.sleep(0)
        headers = headers or {}
        rng = headers.get("Range")
        if rng is None:
            self.requests.append("full")
            return self.script(self, "full", 0, 0)  # type: ignore[operator]
        self.requests.append(rng)
        kind, a, b = _parse_range(rng)
        return self.script(self, kind, a, b)  # type: ignore[operator]

    async def aclose(self) -> None:
        return None

    def partial(self, start: int, end: int, *, gzip: bool = False) -> _FakeResponse:
        end = min(end, self.total - 1)
        headers = {"content-range": f"bytes {start}-{end}/{self.total}"}
        if gzip:
            headers["content-encoding"] = "gzip"
        return _FakeResponse(206, headers, self.wheel[start : end + 1])

    def full(self, *, gzip: bool = False) -> _FakeResponse:
        headers = {"content-encoding": "gzip"} if gzip else {}
        return _FakeResponse(200, headers, self.wheel)


def _run_scripted(
    script: object,
    wheel: bytes | None = None,
    memo: RangeCapabilityMemo | None = None,
    **kwargs: object,
) -> RangeMetadataResult:
    transport = _ScriptedTransport(
        wheel if wheel is not None else build_wheel(), script
    )
    return asyncio.run(
        read_wheel_metadata_over_range(
            transport,  # type: ignore[arg-type]
            _URL,
            _NAME,
            memo if memo is not None else RangeCapabilityMemo(),
            **kwargs,  # type: ignore[arg-type]
        )
    )


def test_suffix_200_gzip_downgrades_to_absolute() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.full(gzip=True)
        return t.partial(a, b)

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.PARTIAL
    assert result.text == _META.decode("utf-8")


def test_suffix_206_without_content_range_downgrades() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(206, {}, t.wheel)
        return t.partial(a, b)

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.PARTIAL
    assert result.text == _META.decode("utf-8")


def test_suffix_206_star_total_downgrades() -> None:
    """An honoured suffix with ``bytes a-b/*`` is unanchored, not a full body.

    The tail slice mistaken for a whole wheel used to leak a ValueError out
    of the zip machinery once METADATA sat in front of the window.
    """

    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            start = max(0, t.total - a)
            headers = {"content-range": f"bytes {start}-{t.total - 1}/*"}
            return _FakeResponse(206, headers, t.wheel[start:])
        return t.partial(a, b)

    result = _run_scripted(script, wheel=build_wheel_member_front())
    assert result.outcome is RangeOutcome.PARTIAL
    assert result.text == _META.decode("utf-8")


def test_absolute_probe_200_full_body() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        return t.full()

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.FULL_BODY


def test_absolute_probe_200_gzip_is_unsupported() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        return t.full(gzip=True)

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.UNSUPPORTED


@pytest.mark.parametrize(
    "probe_headers",
    [{}, {"content-range": "bytes 0-0/*"}],
    ids=["absent", "unknown-length"],
)
def test_absolute_probe_206_without_total_falls_back_to_plain_get(
    probe_headers: dict[str, str],
) -> None:
    """An honoured probe that reports no length steps down to the plain GET.

    RFC 9110 section 14.4 allows ``*`` as the complete-length. It leaves
    nothing to range against, and says nothing about fetching the wheel whole.
    """

    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        if kind == "full":
            return t.full()
        return _FakeResponse(206, probe_headers, t.wheel[:1])

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.FULL_BODY
    assert result.text == _META.decode("utf-8")


def test_absolute_probe_error_raises() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        return _FakeResponse(404, {}, b"")

    with pytest.raises(HttpError):
        _run_scripted(script)


def test_range_rejected_plain_get_error_raises() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "full":
            return _FakeResponse(404, {}, b"")
        return _FakeResponse(416, {}, b"")

    with pytest.raises(HttpError):
        _run_scripted(script)


def test_range_rejected_gzip_plain_get_is_unsupported() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "full":
            return t.full(gzip=True)
        return _FakeResponse(416, {}, b"")

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.UNSUPPORTED
    assert result.text is None


def test_absolute_probe_weird_success_is_unsupported() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        return _FakeResponse(204, {}, b"")

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.UNSUPPORTED


@pytest.mark.parametrize("status", [400, 403, 416, 501])
def test_absolute_tail_rejected_recovers_full_body(status: int) -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        if kind == "full":
            return t.full()
        if a == 0 and b == 0:
            return t.partial(0, 0)
        return _FakeResponse(status, {}, b"")

    memo = RangeCapabilityMemo()
    result = _run_scripted(script, memo=memo)
    assert result.outcome is RangeOutcome.FULL_BODY
    assert result.text == _META.decode("utf-8")
    expected = (
        RangeCapability.UNKNOWN if status == 416 else RangeCapability.FULL_BODY_ONLY
    )
    assert memo.capability("files.example.org") is expected


def test_absolute_tail_weird_success_is_unsupported() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        if a == 0 and b == 0:
            return t.partial(0, 0)
        return _FakeResponse(204, {}, b"")

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.UNSUPPORTED


def test_absolute_tail_200_full_body() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        if a == 0 and b == 0:
            return t.partial(0, 0)
        return t.full()

    memo = RangeCapabilityMemo()
    result = _run_scripted(script, memo=memo)
    assert result.outcome is RangeOutcome.FULL_BODY
    assert memo.capability("files.example.org") is RangeCapability.FULL_BODY_ONLY


def test_absolute_tail_200_gzip_is_unsupported() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        if a == 0 and b == 0:
            return t.partial(0, 0)
        return t.full(gzip=True)

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.UNSUPPORTED


def test_absolute_tail_206_gzip_is_unsupported() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        if a == 0 and b == 0:
            return t.partial(0, 0)
        return t.partial(a, b, gzip=True)

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.UNSUPPORTED


def test_absolute_tail_error_raises() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        if a == 0 and b == 0:
            return t.partial(0, 0)
        return _FakeResponse(500, {}, b"")

    with pytest.raises(HttpError):
        _run_scripted(script)


def test_growth_200_full_body_recovers() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return t.full()

    result = _run_scripted(script, wheel=build_wheel(padding=4000), tail_size=64)
    assert result.text == _META.decode("utf-8")


def test_growth_200_full_body_matching_hash_is_consumed() -> None:
    served = build_wheel(padding=4000)
    published = ("sha256", hashlib.sha256(served).hexdigest())

    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return t.full()

    result = _run_scripted(script, wheel=served, tail_size=64, wheel_hash=published)
    assert result.text == _META.decode("utf-8")


def test_growth_200_full_body_failing_hash_is_rejected() -> None:
    # A growth range comes back as the whole wheel but disagrees with the
    # published digest.
    served = build_wheel(padding=4000)
    published = ("sha256", hashlib.sha256(build_wheel()).hexdigest())

    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return t.full()

    with pytest.raises(WheelHashMismatchError):
        _run_scripted(script, wheel=served, tail_size=64, wheel_hash=published)


def test_growth_5xx_raises() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return _FakeResponse(500, {}, b"")

    with pytest.raises(HttpError):
        _run_scripted(script, wheel=build_wheel(padding=4000), tail_size=64)


def test_growth_gzip_full_body_is_missing() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return t.full(gzip=True)

    result = _run_scripted(script, wheel=build_wheel(padding=4000), tail_size=64)
    assert result.outcome is RangeOutcome.MISSING


@pytest.mark.parametrize("status", [403, 416])
def test_growth_rejection_recovers_full_body(status: int) -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        if kind == "full":
            return t.full()
        return _FakeResponse(status, {}, b"")

    memo = RangeCapabilityMemo()
    result = _run_scripted(
        script, wheel=build_wheel(padding=4000), memo=memo, tail_size=64
    )
    assert result.outcome is RangeOutcome.FULL_BODY
    assert result.text == _META.decode("utf-8")
    expected = (
        RangeCapability.SUFFIX_OK if status == 416 else RangeCapability.FULL_BODY_ONLY
    )
    assert memo.capability("files.example.org") is expected


def test_member_fetch_success() -> None:
    transport = FakeRangeTransport("well_behaved", build_wheel_member_front())
    result = _read(transport)
    assert result.outcome is RangeOutcome.PARTIAL
    assert result.text == _META.decode("utf-8")


def test_member_fetch_5xx_raises() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return _FakeResponse(500, {}, b"")

    with pytest.raises(HttpError):
        _run_scripted(script, wheel=build_wheel_member_front())


def test_member_fetch_200_full_body_recovers() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return t.full()

    result = _run_scripted(script, wheel=build_wheel_member_front())
    assert result.text == _META.decode("utf-8")


def test_member_fetch_200_full_body_failing_hash_is_rejected() -> None:
    served = build_wheel_member_front()
    published = ("sha256", hashlib.sha256(build_wheel()).hexdigest())

    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return t.full()

    with pytest.raises(WheelHashMismatchError):
        _run_scripted(script, wheel=served, wheel_hash=published)


def test_member_fetch_gzip_is_missing() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return t.partial(a, b, gzip=True)

    result = _run_scripted(script, wheel=build_wheel_member_front())
    assert result.outcome is RangeOutcome.MISSING


@pytest.mark.parametrize("status", [400, 501])
def test_member_fetch_rejection_recovers_full_body(status: int) -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        if kind == "full":
            return t.full()
        return _FakeResponse(status, {}, b"")

    memo = RangeCapabilityMemo()
    result = _run_scripted(script, wheel=build_wheel_member_front(), memo=memo)
    assert result.outcome is RangeOutcome.FULL_BODY
    assert result.text == _META.decode("utf-8")
    assert memo.capability("files.example.org") is RangeCapability.FULL_BODY_ONLY


def test_member_fetch_rejected_plain_get_error_raises() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        if kind == "full":
            return _FakeResponse(404, {}, b"")
        return _FakeResponse(416, {}, b"")

    with pytest.raises(HttpError):
        _run_scripted(script, wheel=build_wheel_member_front())


def test_member_fetch_rejected_gzip_plain_get_is_missing() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        if kind == "full":
            return t.full(gzip=True)
        return _FakeResponse(416, {}, b"")

    result = _run_scripted(script, wheel=build_wheel_member_front())
    assert result.outcome is RangeOutcome.MISSING
    assert result.text is None


@pytest.mark.parametrize(
    "build", [build_wheel_member_front, lambda: build_wheel(padding=4000)]
)
def test_mid_read_404_raises(build: object) -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return _FakeResponse(404, {}, b"")

    with pytest.raises(HttpError):
        _run_scripted(script, wheel=build(), tail_size=64)  # type: ignore[operator]


def test_member_last_uses_directory_upper_bound() -> None:
    big_meta = (
        b"Metadata-Version: 2.1\nName: widget\nVersion: 1.0\n\n"
        + b"filler line\n" * 400
    )
    wheel = build_wheel_member_last(big_meta)
    with zipfile.ZipFile(io.BytesIO(wheel)) as zf:
        meta_offset = zf.getinfo("widget-1.0.dist-info/METADATA").header_offset
        directory_start = zf.start_dir
    transport = FakeRangeTransport("well_behaved", wheel)
    result = _read(transport, tail_size=64)
    assert result.outcome is RangeOutcome.PARTIAL
    assert result.text == big_meta.decode("utf-8")
    assert any(
        rng == f"bytes={meta_offset}-{directory_start - 1}"
        for rng, _ in transport.requests
    )


def test_suffix_206_unparseable_content_range_downgrades() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(206, {"content-range": "bogus"}, t.wheel)
        return t.partial(a, b)

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.PARTIAL
    assert result.text == _META.decode("utf-8")


def test_memo_suffix_ok_no_reprobe() -> None:
    async def run() -> RangeCapability:
        memo = RangeCapabilityMemo()
        transport = FakeRangeTransport("well_behaved", build_wheel())
        await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        assert memo.capability("files.example.org") is RangeCapability.SUFFIX_OK
        await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        return memo.capability("files.example.org")

    assert asyncio.run(run()) is RangeCapability.SUFFIX_OK


def test_memo_absolute_only_skips_reprobe() -> None:
    async def run() -> tuple[RangeCapability, int]:
        memo = RangeCapabilityMemo()
        transport = FakeRangeTransport("suffix_501", build_wheel())
        await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        assert memo.capability("files.example.org") is RangeCapability.ABSOLUTE_ONLY
        await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        return memo.capability("files.example.org"), transport.negative_requests

    capability, negatives = asyncio.run(run())
    assert capability is RangeCapability.ABSOLUTE_ONLY
    assert negatives == 1


def test_memo_range_ignoring_host_stays_suffix_ok() -> None:
    async def run() -> tuple[RangeMetadataResult, int]:
        memo = RangeCapabilityMemo()
        transport = FakeRangeTransport("ignore_range_200", build_wheel())
        first = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        assert first.text == _META.decode("utf-8")
        # A volunteered 200 answers the suffix request with the whole body, so
        # leading with the suffix form again costs the same single request on a
        # host that keeps ignoring ranges, while a proxy that only ignored them
        # on a cache miss gets to serve cheap partial reads again.
        assert memo.capability("files.example.org") is RangeCapability.SUFFIX_OK
        before = len(transport.requests)
        second = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        return second, len(transport.requests) - before

    result, second_requests = asyncio.run(run())
    assert result.text == _META.decode("utf-8")
    assert result.outcome is RangeOutcome.FULL_BODY
    assert second_requests == 1


def test_memo_gzip_stays_unsupported() -> None:
    async def run() -> int:
        memo = RangeCapabilityMemo()
        transport = FakeRangeTransport("gzip_range", build_wheel())
        result = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        assert result.outcome is RangeOutcome.UNSUPPORTED
        assert memo.capability("files.example.org") is RangeCapability.UNSUPPORTED
        first = len(transport.requests)
        again = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        assert again.outcome is RangeOutcome.UNSUPPORTED
        return len(transport.requests) - first

    assert asyncio.run(run()) == 0


def _read_full_body_only(script: object) -> RangeMetadataResult:
    """Read against a host already memoed ``FULL_BODY_ONLY`` (a plain GET)."""
    memo = RangeCapabilityMemo()
    memo.record("files.example.org", RangeCapability.FULL_BODY_ONLY)
    transport = _ScriptedTransport(build_wheel(), script)
    return asyncio.run(
        read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
    )


def test_full_body_only_gzip_is_unsupported() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        return t.full(gzip=True)

    result = _read_full_body_only(script)
    assert result.text is None
    assert result.outcome is RangeOutcome.UNSUPPORTED


def test_full_body_only_error_raises() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        return _FakeResponse(500, {}, b"")

    with pytest.raises(HttpError):
        _read_full_body_only(script)


def test_memo_concurrent_single_flight() -> None:
    async def run() -> int:
        memo = RangeCapabilityMemo()
        transport = FakeRangeTransport("suffix_501", build_wheel())
        await asyncio.gather(
            read_wheel_metadata_over_range(transport, _URL, _NAME, memo),  # type: ignore[arg-type]
            read_wheel_metadata_over_range(transport, _URL, _NAME, memo),  # type: ignore[arg-type]
        )
        return transport.negative_requests

    assert asyncio.run(run()) == 1


def test_memo_ignored_absolute_latches_full_body() -> None:
    async def run() -> tuple[RangeMetadataResult, int]:
        memo = RangeCapabilityMemo()
        transport = FakeRangeTransport("no_ranges", build_wheel())
        await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        capability = memo.capability("files.example.org")
        assert capability is RangeCapability.FULL_BODY_ONLY
        before = len(transport.requests)
        result = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        return result, len(transport.requests) - before

    result, second_requests = asyncio.run(run())
    assert result.outcome is RangeOutcome.FULL_BODY
    assert result.text == _META.decode("utf-8")
    assert second_requests == 1


def test_unknown_length_host_latches_full_body_only() -> None:
    """A host that never reports a total is fetched whole, not given up on."""

    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "full":
            return t.full()
        start = max(0, t.total - a) if kind == "suffix" else a
        end = t.total - 1 if kind == "suffix" else min(b, t.total - 1)
        headers = {"content-range": f"bytes {start}-{end}/*"}
        return _FakeResponse(206, headers, t.wheel[start : end + 1])

    async def run() -> tuple[RangeMetadataResult, int]:
        memo = RangeCapabilityMemo()
        transport = _ScriptedTransport(build_wheel(), script)
        first = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        assert first.text == _META.decode("utf-8")
        assert memo.capability("files.example.org") is RangeCapability.FULL_BODY_ONLY
        before = len(transport.requests)
        second = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        return second, len(transport.requests) - before

    result, second_requests = asyncio.run(run())
    assert result.outcome is RangeOutcome.FULL_BODY
    assert result.text == _META.decode("utf-8")
    assert second_requests == 1


def test_zero_length_artifact_does_not_latch_netloc() -> None:
    """A zero complete-length speaks for the one artifact, not for the host."""

    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        if kind == "full":
            return _FakeResponse(200, {}, b"")
        return _FakeResponse(206, {"content-range": "bytes 0-0/0"}, b"")

    async def run() -> tuple[RangeMetadataResult, RangeCapability]:
        memo = RangeCapabilityMemo()
        transport = _ScriptedTransport(b"", script)
        result = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        return result, memo.capability("files.example.org")

    result, capability = asyncio.run(run())
    assert result.outcome is RangeOutcome.MISSING
    assert result.text is None
    assert capability is RangeCapability.UNKNOWN


def test_reject_416_does_not_latch_netloc() -> None:
    async def run() -> tuple[RangeMetadataResult, int]:
        memo = RangeCapabilityMemo()
        transport = FakeRangeTransport("reject_416", build_wheel())
        first = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        assert first.outcome is RangeOutcome.FULL_BODY
        # A 416 speaks for the one file, so the host is probed afresh.
        assert memo.capability("files.example.org") is RangeCapability.UNKNOWN
        before = len(transport.requests)
        second = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        return second, len(transport.requests) - before

    result, second_requests = asyncio.run(run())
    assert result.outcome is RangeOutcome.FULL_BODY
    assert result.text == _META.decode("utf-8")
    assert second_requests == 3


def test_reject_statuses_disjoint_from_transport_retries() -> None:
    """A refused range must come back on the first answer, never retried."""
    from nab_index.lazy_wheel import _RANGE_REJECT_STATUSES
    from nab_index.retry import RETRY_STATUSES

    assert _RANGE_REJECT_STATUSES.isdisjoint(RETRY_STATUSES)


def _patch_member(
    wheel: bytes, member: str, *, flag_or: int | None = None, method: int | None = None
) -> bytes:
    """Rewrite one member's local and central headers in place."""
    import struct

    data = bytearray(wheel)
    with zipfile.ZipFile(io.BytesIO(wheel)) as zf:
        local_off = zf.getinfo(member).header_offset
    name_bytes = member.encode()
    if flag_or is not None:
        flags = struct.unpack_from("<H", data, local_off + 6)[0] | flag_or
        struct.pack_into("<H", data, local_off + 6, flags)
    if method is not None:
        struct.pack_into("<H", data, local_off + 8, method)
    pos = 0
    while (pos := data.find(b"PK\x01\x02", pos)) != -1:
        name_len = struct.unpack_from("<H", data, pos + 28)[0]
        if bytes(data[pos + 46 : pos + 46 + name_len]) == name_bytes:
            if flag_or is not None:
                flags = struct.unpack_from("<H", data, pos + 8)[0] | flag_or
                struct.pack_into("<H", data, pos + 8, flags)
            if method is not None:
                struct.pack_into("<H", data, pos + 10, method)
        pos += 4
    return bytes(data)


def _wheel_bad_utf8_name() -> bytes:
    """A wheel whose UTF-8-flagged member name does not decode."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("widget-1.0.dist-info/METADATA", _META)
        zf.writestr("widget/pÄd.py", b"x")
    needle = "widget/pÄd.py".encode()
    wheel = buf.getvalue()
    assert wheel.count(needle) == 2
    return wheel.replace(needle, b"widget/p\xff\xffd.py")


def _wheel_encrypted_metadata() -> bytes:
    return _patch_member(
        build_wheel(compression=zipfile.ZIP_STORED),
        "widget-1.0.dist-info/METADATA",
        flag_or=0x1,
    )


def _wheel_method_99() -> bytes:
    return _patch_member(
        build_wheel(compression=zipfile.ZIP_STORED),
        "widget-1.0.dist-info/METADATA",
        method=99,
    )


@pytest.mark.parametrize("mode", ["well_behaved", "ignore_range_200"])
@pytest.mark.parametrize(
    "shape", [_wheel_bad_utf8_name, _wheel_encrypted_metadata, _wheel_method_99]
)
def test_unreadable_wheel_is_missing(mode: str, shape: object) -> None:
    """Zip shapes the machinery cannot read step the ladder on, never raise."""
    transport = FakeRangeTransport(mode, shape())  # type: ignore[operator]
    result = _read(transport)
    assert result.outcome is RangeOutcome.MISSING
    assert result.text is None


def test_probe_releases_when_owner_acquisition_fails() -> None:
    """A failed owner still releases parked waiters, who probe for themselves."""
    wheel = build_wheel()
    url_b = "https://files.example.org/packages/other/widget-1.0-py3-none-any.whl"

    async def run() -> RangeMetadataResult:
        memo = RangeCapabilityMemo()
        total = len(wheel)
        owner_started = asyncio.Event()
        release_owner = asyncio.Event()

        class GatedTransport:
            async def get(
                self, url: str, *, headers: dict[str, str] | None = None
            ) -> _FakeResponse:
                kind, a, b = _parse_range((headers or {})["Range"])
                if url == _URL:
                    owner_started.set()
                    await release_owner.wait()
                    return _FakeResponse(404, {}, b"")
                if kind == "suffix":
                    start, end = max(0, total - a), total - 1
                else:
                    start, end = a, min(b, total - 1)
                headers_out = {"content-range": f"bytes {start}-{end}/{total}"}
                return _FakeResponse(206, headers_out, wheel[start : end + 1])

            async def aclose(self) -> None:
                return None

        transport = GatedTransport()
        owner = asyncio.create_task(
            read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        )
        await owner_started.wait()
        waiter = asyncio.create_task(
            read_wheel_metadata_over_range(transport, url_b, _NAME, memo)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
        release_owner.set()
        with pytest.raises(HttpError):
            await owner
        return await waiter

    waiter_result = asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert waiter_result.text == _META.decode("utf-8")


def test_probe_releases_once_capability_settles() -> None:
    """A waiter proceeds as soon as the owner's capability is settled.

    The owner's member read is gated on the waiter finishing first, so the
    test deadlocks (and times out) if the probe were held for the owner's
    whole read rather than released after acquisition.
    """
    wheel = build_wheel_member_front()
    url_b = "https://files.example.org/packages/other/widget-1.0-py3-none-any.whl"

    async def run() -> tuple[RangeMetadataResult, RangeMetadataResult]:
        memo = RangeCapabilityMemo()
        total = len(wheel)
        owner_suffix_started = asyncio.Event()
        release_owner_suffix = asyncio.Event()
        waiter_done = asyncio.Event()

        class GatedTransport:
            async def get(
                self, url: str, *, headers: dict[str, str] | None = None
            ) -> _FakeResponse:
                kind, a, b = _parse_range((headers or {})["Range"])
                if kind == "suffix":
                    if url == _URL:
                        owner_suffix_started.set()
                        await release_owner_suffix.wait()
                    start, end = max(0, total - a), total - 1
                else:
                    if url == _URL:
                        await waiter_done.wait()
                    start, end = a, min(b, total - 1)
                headers_out = {"content-range": f"bytes {start}-{end}/{total}"}
                return _FakeResponse(206, headers_out, wheel[start : end + 1])

            async def aclose(self) -> None:
                return None

        transport = GatedTransport()
        owner = asyncio.create_task(
            read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        )
        await owner_suffix_started.wait()
        waiter = asyncio.create_task(
            read_wheel_metadata_over_range(transport, url_b, _NAME, memo)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
        release_owner_suffix.set()
        waiter_result = await waiter
        waiter_done.set()
        return await owner, waiter_result

    owner_result, waiter_result = asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert owner_result.text == _META.decode("utf-8")
    assert waiter_result.text == _META.decode("utf-8")


def test_sparse_file_seek_and_read() -> None:
    sf = _SparseFile(100)
    sf.add_span(10, b"abcdef")
    assert sf.seekable() is True
    sf.seek(10)
    assert sf.tell() == 10
    assert sf.read(3) == b"abc"
    sf.seek(1, 1)
    assert sf.read(2) == b"ef"
    sf.seek(-90, 2)
    assert sf.read(4) == b"abcd"
    assert sf.read() == b"ef"


def test_sparse_file_gap_returns_partial() -> None:
    sf = _SparseFile(100)
    sf.add_span(10, b"abc")
    sf.seek(0)
    assert sf.read(20) == b""
    sf.seek(10)
    assert sf.read(20) == b"abc"


def test_sparse_file_invalid_whence() -> None:
    sf = _SparseFile(100)
    with pytest.raises(ValueError, match="whence"):
        sf.seek(0, 5)


def test_sparse_file_add_empty_span_ignored() -> None:
    sf = _SparseFile(100)
    sf.add_span(10, b"")
    sf.seek(10)
    assert sf.read(4) == b""


def test_sparse_file_duplicate_span_ignored() -> None:
    sf = _SparseFile(100)
    sf.add_span(10, b"abc")
    sf.add_span(10, b"abc")
    sf.seek(10)
    assert sf.read(3) == b"abc"
