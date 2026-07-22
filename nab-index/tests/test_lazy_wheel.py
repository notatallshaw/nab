"""Tests for nab_index.lazy_wheel, the HTTP range metadata reader."""

from __future__ import annotations

import asyncio
import io
import zipfile
from typing import TYPE_CHECKING

import pytest
from packaging.utils import canonicalize_name

from nab_index.client import MalformedSimpleResponseError
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
        assert rng is not None
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
    ("mode", "outcome"),
    [
        ("well_behaved", RangeOutcome.PARTIAL),
        ("suffix_501", RangeOutcome.PARTIAL),
        ("ignore_range_200", RangeOutcome.FULL_BODY),
        ("no_ranges", RangeOutcome.FULL_BODY),
    ],
)
def test_recovers_metadata_per_mode(mode: str, outcome: RangeOutcome) -> None:
    wheel = build_wheel()
    transport = FakeRangeTransport(mode, wheel)
    result = _read(transport)
    assert result.outcome is outcome
    assert result.text == _META.decode("utf-8")
    assert all(enc == "identity" for _, enc in transport.requests)


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
        rng = headers["Range"]
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
    script: object, wheel: bytes | None = None, **kwargs: object
) -> RangeMetadataResult:
    transport = _ScriptedTransport(
        wheel if wheel is not None else build_wheel(), script
    )
    return asyncio.run(
        read_wheel_metadata_over_range(
            transport,  # type: ignore[arg-type]
            _URL,
            _NAME,
            RangeCapabilityMemo(),
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


def test_suffix_206_without_content_range_is_full_body() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        return _FakeResponse(206, {}, t.wheel)

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.FULL_BODY
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


def test_absolute_probe_206_no_content_range_is_unsupported() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        return _FakeResponse(206, {}, b"\x00")

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.UNSUPPORTED


def test_absolute_probe_error_raises() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        return _FakeResponse(404, {}, b"")

    with pytest.raises(HttpError):
        _run_scripted(script)


def test_absolute_tail_200_full_body() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return _FakeResponse(501, {}, b"")
        if a == 0 and b == 0:
            return t.partial(0, 0)
        return t.full()

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.FULL_BODY


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


def test_growth_non_206_is_missing() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return t.full()

    result = _run_scripted(script, wheel=build_wheel(padding=4000), tail_size=64)
    assert result.outcome is RangeOutcome.MISSING


def test_member_fetch_success() -> None:
    transport = FakeRangeTransport("well_behaved", build_wheel_member_front())
    result = _read(transport)
    assert result.outcome is RangeOutcome.PARTIAL
    assert result.text == _META.decode("utf-8")


def test_member_fetch_non_206_is_missing() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        if kind == "suffix":
            return t.partial(max(0, t.total - a), t.total - 1)
        return _FakeResponse(500, {}, b"")

    result = _run_scripted(script, wheel=build_wheel_member_front())
    assert result.outcome is RangeOutcome.MISSING


def test_suffix_206_unparseable_content_range_is_full_body() -> None:
    def script(t: _ScriptedTransport, kind: str, a: int, b: int) -> _FakeResponse:
        return _FakeResponse(206, {"content-range": "bogus"}, t.wheel)

    result = _run_scripted(script)
    assert result.outcome is RangeOutcome.FULL_BODY
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


def test_memo_unsupported_skips_all_requests() -> None:
    async def run() -> int:
        memo = RangeCapabilityMemo()
        transport = FakeRangeTransport("ignore_range_200", build_wheel())
        await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        assert memo.capability("files.example.org") is RangeCapability.UNSUPPORTED
        first = len(transport.requests)
        result = await read_wheel_metadata_over_range(transport, _URL, _NAME, memo)  # type: ignore[arg-type]
        assert result.outcome is RangeOutcome.UNSUPPORTED
        return len(transport.requests) - first

    assert asyncio.run(run()) == 0


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
