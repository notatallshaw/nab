"""Tests for nab_python.download (resolver-driven artefact downloads)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from nab_index.transport import HttpError
from nab_python.download import (
    DownloadEntry,
    DownloadError,
    DownloadResult,
    download_lock,
    iter_artifacts,
)
from nab_python.lockfile import (
    ArchivePin,
    IndexPin,
    LocalPin,
    LockInput,
    PinShape,
    SdistArtifact,
    TargetLock,
    VcsPin,
    WheelArtifact,
)
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget


def _target(
    python_version: str = "3.11", platform: str = "linux_x86_64"
) -> ResolveTarget:
    """One declared (python, platform) target of a resolve."""
    return ResolveTarget.for_declared(
        python_version=python_version, spec=PlatformSpec(platform)
    )


# A resolve always runs against at least one target, so one entry is the
# smallest lock there is.
_HOST = _target()


def _one(pins: Mapping[str, PinShape]) -> dict[str, TargetLock]:
    """Return the one-target map a single-environment resolve produces."""
    return {_HOST.label: TargetLock(target=_HOST, pins=dict(pins))}


def _targets(
    *entries: tuple[ResolveTarget, Mapping[str, PinShape]],
) -> dict[str, TargetLock]:
    """Return the per-target map for the given ``(target, pins)`` pairs."""
    return {
        target.label: TargetLock(target=target, pins=dict(pins))
        for target, pins in entries
    }


def _wheel(name: str, version: str, *, sha256: str) -> WheelArtifact:
    return WheelArtifact(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://example.com/{name}-{version}.whl",
        hashes=(("sha256", sha256),),
        size=4,
    )


def _sdist(name: str, version: str, *, sha256: str) -> SdistArtifact:
    return SdistArtifact(
        filename=f"{name}-{version}.tar.gz",
        url=f"https://example.com/{name}-{version}.tar.gz",
        hashes=(("sha256", sha256),),
        size=4,
    )


def _index_pin(
    name: str = "foo",
    version: str = "1.0",
    *,
    sdist_sha: str | None = None,
    wheel_sha: str | None = None,
) -> IndexPin:
    sdist = _sdist(name, version, sha256=sdist_sha) if sdist_sha else None
    wheels = ()
    if wheel_sha is not None:
        wheels = (_wheel(name, version, sha256=wheel_sha),)
    return IndexPin(
        name=name,
        version=version,
        index="pypi",
        sdist=sdist,
        wheels=wheels,
    )


@dataclass
class _FakeResponse:
    content: bytes
    status_code: int = 200

    @property
    def headers(self) -> Mapping[str, str]:
        return {}

    @property
    def text(self) -> str:
        return self.content.decode()

    def json(self) -> object:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            msg = f"HTTP {self.status_code}"
            raise HttpError(msg)


class _FakeTransport:
    def __init__(self, responses: dict[str, bytes], *, status_code: int = 200) -> None:
        self._responses = responses
        self._status_code = status_code
        self.requested: list[str] = []
        self.closed = False

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        del headers
        self.requested.append(url)
        return _FakeResponse(
            content=self._responses[url], status_code=self._status_code
        )

    async def aclose(self) -> None:
        self.closed = True


class TestIterArtifacts:
    def test_index_pin_yields_sdist_and_wheels(self) -> None:
        pin = _index_pin(sdist_sha="b" * 64, wheel_sha="a" * 64)
        entries = list(iter_artifacts(LockInput(targets=_one({"foo": pin}))))
        assert [e.filename for e in entries] == [
            "foo-1.0.tar.gz",
            "foo-1.0-py3-none-any.whl",
        ]

    def test_local_pin_skipped(self, tmp_path: Path) -> None:
        entries = list(
            iter_artifacts(
                LockInput(
                    targets=_one(
                        {"foo": LocalPin(name="foo", version="1.0", path=str(tmp_path))}
                    )
                )
            )
        )
        assert entries == []

    def test_vcs_pin_skipped(self) -> None:
        entries = list(
            iter_artifacts(
                LockInput(
                    targets=_one(
                        {
                            "foo": VcsPin(
                                name="foo",
                                version="1.0",
                                repo_url="git+https://x/y.git",
                                bare_repo_url="https://x/y.git",
                                commit_id="a" * 40,
                            ),
                        }
                    )
                )
            )
        )
        assert entries == []

    def test_index_pin_without_artefacts(self) -> None:
        pin = _index_pin()
        entries = list(iter_artifacts(LockInput(targets=_one({"foo": pin}))))
        assert entries == []

    def test_universal_unions_every_target(self) -> None:
        linux = _index_pin(name="foo", version="1.0", wheel_sha="a" * 64)
        windows = _index_pin(name="bar", version="2.0", wheel_sha="b" * 64)
        lock = LockInput(
            targets=_targets(
                (_target("3.12", "linux_x86_64"), {"foo": linux}),
                (_target("3.12", "windows_amd64"), {"bar": windows}),
            )
        )
        assert sorted(e.filename for e in iter_artifacts(lock)) == [
            "bar-2.0-py3-none-any.whl",
            "foo-1.0-py3-none-any.whl",
        ]

    def test_universal_dedups_shared_artefact_by_url(self) -> None:
        shared = _index_pin(name="foo", version="1.0", wheel_sha="a" * 64)
        lock = LockInput(
            targets=_targets(
                (_target("3.12", "linux_x86_64"), {"foo": shared}),
                (_target("3.12", "windows_amd64"), {"foo": shared}),
            )
        )
        assert [e.filename for e in iter_artifacts(lock)] == [
            "foo-1.0-py3-none-any.whl"
        ]

    def test_universal_skips_local_and_vcs_pins(self, tmp_path: Path) -> None:
        lock = LockInput(
            targets=_targets(
                (
                    _target("3.12", "linux_x86_64"),
                    {
                        "loc": LocalPin(name="loc", version="1.0", path=str(tmp_path)),
                        "vcs": VcsPin(
                            name="vcs",
                            version="1.0",
                            repo_url="git+https://x/y.git",
                            bare_repo_url="https://x/y.git",
                            commit_id="a" * 40,
                        ),
                        "idx": _index_pin(
                            name="idx", version="1.0", wheel_sha="c" * 64
                        ),
                    },
                )
            )
        )
        assert [e.filename for e in iter_artifacts(lock)] == [
            "idx-1.0-py3-none-any.whl"
        ]


class TestDownloadLock:
    def test_writes_files_and_verifies_hashes(self, tmp_path: Path) -> None:
        wheel_bytes = b"WHEELDATA"
        sdist_bytes = b"SDISTDATA"
        wheel_sha = hashlib.sha256(wheel_bytes).hexdigest()
        sdist_sha = hashlib.sha256(sdist_bytes).hexdigest()
        pin = _index_pin(sdist_sha=sdist_sha, wheel_sha=wheel_sha)
        transport = _FakeTransport(
            {
                "https://example.com/foo-1.0.tar.gz": sdist_bytes,
                "https://example.com/foo-1.0.whl": wheel_bytes,
            }
        )
        result = download_lock(
            LockInput(targets=_one({"foo": pin})),
            transport,
            tmp_path,  # type: ignore[arg-type]
        )
        assert isinstance(result, DownloadResult)
        assert sorted(p.name for p in result.written) == [
            "foo-1.0-py3-none-any.whl",
            "foo-1.0.tar.gz",
        ]
        assert (tmp_path / "foo-1.0.tar.gz").read_bytes() == sdist_bytes
        assert transport.closed

    def test_idempotent_skip_when_sha_matches(self, tmp_path: Path) -> None:
        wheel_bytes = b"BYTES"
        wheel_sha = hashlib.sha256(wheel_bytes).hexdigest()
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(wheel_bytes)
        pin = _index_pin(wheel_sha=wheel_sha)
        transport = _FakeTransport({"https://example.com/foo-1.0.whl": wheel_bytes})
        result = download_lock(
            LockInput(targets=_one({"foo": pin})),
            transport,
            tmp_path,  # type: ignore[arg-type]
        )
        assert result.written == ()
        assert len(result.skipped) == 1
        assert transport.requested == []

    def test_overwrites_when_existing_hash_differs(self, tmp_path: Path) -> None:
        wheel_bytes = b"NEW"
        wheel_sha = hashlib.sha256(wheel_bytes).hexdigest()
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(b"OLD")
        pin = _index_pin(wheel_sha=wheel_sha)
        transport = _FakeTransport({"https://example.com/foo-1.0.whl": wheel_bytes})
        result = download_lock(
            LockInput(targets=_one({"foo": pin})),
            transport,
            tmp_path,  # type: ignore[arg-type]
        )
        assert (tmp_path / "foo-1.0-py3-none-any.whl").read_bytes() == wheel_bytes
        assert len(result.written) == 1

    def test_interrupted_write_leaves_no_partial_at_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wheel_bytes = b"WHEELDATA" * 64
        wheel_sha = hashlib.sha256(wheel_bytes).hexdigest()
        pin = _index_pin(wheel_sha=wheel_sha)
        transport = _FakeTransport({"https://example.com/foo-1.0.whl": wheel_bytes})

        def crash(_src: object, _dst: object) -> None:
            msg = "interrupted mid-write"
            raise OSError(msg)

        monkeypatch.setattr("nab_index.atomic.os.replace", crash)

        with pytest.raises(OSError, match="interrupted mid-write"):
            download_lock(
                LockInput(targets=_one({"foo": pin})),
                transport,  # type: ignore[arg-type]
                tmp_path,
            )
        assert not (tmp_path / "foo-1.0-py3-none-any.whl").exists()
        assert list(tmp_path.iterdir()) == []

    def test_uppercase_recorded_digest_verifies(self, tmp_path: Path) -> None:
        wheel_bytes = b"WHEELDATA"
        wheel_sha = hashlib.sha256(wheel_bytes).hexdigest().upper()
        pin = _index_pin(wheel_sha=wheel_sha)
        transport = _FakeTransport({"https://example.com/foo-1.0.whl": wheel_bytes})
        result = download_lock(
            LockInput(targets=_one({"foo": pin})),
            transport,
            tmp_path,  # type: ignore[arg-type]
        )
        assert len(result.written) == 1
        assert (tmp_path / "foo-1.0-py3-none-any.whl").read_bytes() == wheel_bytes

    def test_idempotent_skip_with_uppercase_recorded_digest(
        self, tmp_path: Path
    ) -> None:
        wheel_bytes = b"BYTES"
        wheel_sha = hashlib.sha256(wheel_bytes).hexdigest().upper()
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(wheel_bytes)
        pin = _index_pin(wheel_sha=wheel_sha)
        transport = _FakeTransport({"https://example.com/foo-1.0.whl": wheel_bytes})
        result = download_lock(
            LockInput(targets=_one({"foo": pin})),
            transport,
            tmp_path,  # type: ignore[arg-type]
        )
        assert result.written == ()
        assert len(result.skipped) == 1
        assert transport.requested == []

    def test_sha_mismatch_raises(self, tmp_path: Path) -> None:
        wheel_bytes = b"REAL"
        pin = _index_pin(wheel_sha="0" * 64)  # advertised sha is wrong
        transport = _FakeTransport({"https://example.com/foo-1.0.whl": wheel_bytes})
        with pytest.raises(DownloadError, match="sha256 mismatch"):
            download_lock(
                LockInput(targets=_one({"foo": pin})),
                transport,  # type: ignore[arg-type]
                tmp_path,
            )

    def test_http_error_becomes_download_error(self, tmp_path: Path) -> None:
        pin = _index_pin(wheel_sha="a" * 64)
        transport = _FakeTransport(
            {"https://example.com/foo-1.0.whl": b"x"}, status_code=503
        )
        with pytest.raises(DownloadError, match="failed to fetch foo-1.0-py3-none-any"):
            download_lock(
                LockInput(targets=_one({"foo": pin})),
                transport,  # type: ignore[arg-type]
                tmp_path,
            )

    def test_failure_cancels_sibling_downloads(self, tmp_path: Path) -> None:
        """One task raising cancels the in-flight siblings, then closes the transport."""
        fail_pin = _index_pin(name="fail", wheel_sha="0" * 64)
        slow_pin = _index_pin(name="slow", wheel_sha="a" * 64)

        cancelled = asyncio.Event()

        class _MixedTransport:
            def __init__(self) -> None:
                self.closed = False
                self.requested: list[str] = []

            async def get(
                self,
                url: str,
                *,
                headers: dict[str, str] | None = None,
            ) -> _FakeResponse:
                del headers
                self.requested.append(url)
                if "fail" in url:
                    return _FakeResponse(content=b"REAL")
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return _FakeResponse(content=b"slow")  # pragma: no cover

            async def aclose(self) -> None:
                self.closed = True

        transport = _MixedTransport()
        with pytest.raises(DownloadError, match="sha256 mismatch"):
            download_lock(
                LockInput(targets=_one({"fail": fail_pin, "slow": slow_pin})),
                transport,  # type: ignore[arg-type]
                tmp_path,
                max_concurrency=2,
            )
        assert cancelled.is_set()
        assert transport.closed

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "vendor"
        download_lock(
            LockInput(targets=_one({})),
            _FakeTransport({}),
            target,  # type: ignore[arg-type]
        )
        assert target.is_dir()

    def test_rejects_parent_escape_filename(self, tmp_path: Path) -> None:
        payload = b"EVIL"
        sha = hashlib.sha256(payload).hexdigest()
        wheel = WheelArtifact(
            filename="../escaped.whl",
            url="https://example.com/foo-1.0.whl",
            hashes=(("sha256", sha),),
            size=4,
        )
        pin = IndexPin(
            name="foo", version="1.0", index="pypi", sdist=None, wheels=(wheel,)
        )
        transport = _FakeTransport({"https://example.com/foo-1.0.whl": payload})
        output_dir = tmp_path / "wheels"
        with pytest.raises(DownloadError, match="unsafe"):
            download_lock(
                LockInput(targets=_one({"foo": pin})),
                transport,  # type: ignore[arg-type]
                output_dir,
            )
        assert not (tmp_path / "escaped.whl").exists()

    def test_rejects_index_injectable_traversal_filename(self, tmp_path: Path) -> None:
        # A malicious index serving "foo" can return this filename: it parses as
        # name "foo" version "1.0" (so the resolver pins it) while the tail carries
        # the traversal into the join at the write site.
        payload = b"EVIL"
        sha = hashlib.sha256(payload).hexdigest()
        wheel = WheelArtifact(
            filename="foo-1.0-py3-none-any/../../../evil.whl",
            url="https://example.com/foo-1.0.whl",
            hashes=(("sha256", sha),),
            size=4,
        )
        pin = IndexPin(
            name="foo", version="1.0", index="pypi", sdist=None, wheels=(wheel,)
        )
        transport = _FakeTransport({"https://example.com/foo-1.0.whl": payload})
        with pytest.raises(DownloadError, match="unsafe"):
            download_lock(
                LockInput(targets=_one({"foo": pin})),
                transport,  # type: ignore[arg-type]
                tmp_path / "wheels",
            )
        assert not (tmp_path.parent / "evil.whl").exists()


class TestLocalIndexArtefacts:
    """Artefacts resolved from a local file:// (find-links) index."""

    def test_entry_carries_local_path(self, tmp_path: Path) -> None:
        wheel = WheelArtifact(
            filename="foo-1.0-py3-none-any.whl",
            url=(tmp_path / "foo-1.0-py3-none-any.whl").as_uri(),
            hashes=(("sha256", "a" * 64),),
            local_path=tmp_path / "foo-1.0-py3-none-any.whl",
        )
        pin = IndexPin(name="foo", version="1.0", index="local", wheels=(wheel,))
        (entry,) = list(iter_artifacts(LockInput(targets=_one({"foo": pin}))))
        assert entry.local_path == tmp_path / "foo-1.0-py3-none-any.whl"

    def test_wheel_and_sdist_copied_from_disk(self, tmp_path: Path) -> None:
        src = tmp_path / "wheelhouse"
        src.mkdir()
        out = tmp_path / "out"
        wheel_bytes = b"WHEELDATA"
        sdist_bytes = b"SDISTDATA"
        wheel_file = src / "foo-1.0-py3-none-any.whl"
        sdist_file = src / "foo-1.0.tar.gz"
        wheel_file.write_bytes(wheel_bytes)
        sdist_file.write_bytes(sdist_bytes)
        wheel = WheelArtifact(
            filename="foo-1.0-py3-none-any.whl",
            url=wheel_file.as_uri(),
            hashes=(("sha256", hashlib.sha256(wheel_bytes).hexdigest()),),
            local_path=wheel_file,
        )
        sdist = SdistArtifact(
            filename="foo-1.0.tar.gz",
            url=sdist_file.as_uri(),
            hashes=(("sha256", hashlib.sha256(sdist_bytes).hexdigest()),),
            local_path=sdist_file,
        )
        pin = IndexPin(
            name="foo", version="1.0", index="local", sdist=sdist, wheels=(wheel,)
        )
        # The transport would serve the wrong bytes; a local artefact must
        # never reach it.
        transport = _FakeTransport(
            {wheel_file.as_uri(): b"WRONG", sdist_file.as_uri(): b"WRONG"}
        )
        result = download_lock(
            LockInput(targets=_one({"foo": pin})),
            transport,  # type: ignore[arg-type]
            out,
        )
        assert (out / "foo-1.0-py3-none-any.whl").read_bytes() == wheel_bytes
        assert (out / "foo-1.0.tar.gz").read_bytes() == sdist_bytes
        assert len(result.written) == 2
        assert transport.requested == []

    def test_file_url_archive_copied_from_disk(self, tmp_path: Path) -> None:
        src = tmp_path / "wheelhouse"
        src.mkdir()
        out = tmp_path / "out"
        archive_bytes = b"ARCHIVEDATA"
        archive_file = src / "foo-1.0.0.tar.gz"
        archive_file.write_bytes(archive_bytes)
        pin = ArchivePin(
            name="foo",
            version="1.0.0",
            url=archive_file.as_uri(),
            hashes=(("sha256", hashlib.sha256(archive_bytes).hexdigest()),),
        )
        transport = _FakeTransport({archive_file.as_uri(): b"WRONG"})
        result = download_lock(
            LockInput(targets=_one({"foo": pin})),
            transport,  # type: ignore[arg-type]
            out,
        )
        assert (out / "foo-1.0.0.tar.gz").read_bytes() == archive_bytes
        assert len(result.written) == 1
        assert transport.requested == []

    def test_missing_local_file_raises_download_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone" / "foo-1.0-py3-none-any.whl"
        wheel = WheelArtifact(
            filename="foo-1.0-py3-none-any.whl",
            url=missing.as_uri(),
            hashes=(("sha256", "a" * 64),),
            local_path=missing,
        )
        pin = IndexPin(name="foo", version="1.0", index="local", wheels=(wheel,))
        with pytest.raises(DownloadError, match="failed to read foo-1.0-py3-none-any"):
            download_lock(
                LockInput(targets=_one({"foo": pin})),
                _FakeTransport({}),  # type: ignore[arg-type]
                tmp_path / "out",
            )


class TestOffline:
    """``offline=True`` refuses every artefact that needs an HTTP fetch."""

    def test_refuses_before_touching_the_transport(self, tmp_path: Path) -> None:
        wheel_bytes = b"WHEELDATA"
        pin = _index_pin(wheel_sha=hashlib.sha256(wheel_bytes).hexdigest())
        transport = _FakeTransport({"https://example.com/foo-1.0.whl": wheel_bytes})
        with pytest.raises(
            DownloadError,
            match=(
                r"foo==1\.0: artefact fetch unavailable in offline mode"
                r" \(foo-1\.0-py3-none-any\.whl\)"
            ),
        ):
            download_lock(
                LockInput(targets=_one({"foo": pin})),
                transport,  # type: ignore[arg-type]
                tmp_path,
                offline=True,
            )
        assert transport.requested == []
        assert list(tmp_path.iterdir()) == []

    def test_already_present_file_still_succeeds(self, tmp_path: Path) -> None:
        wheel_bytes = b"BYTES"
        (tmp_path / "foo-1.0-py3-none-any.whl").write_bytes(wheel_bytes)
        pin = _index_pin(wheel_sha=hashlib.sha256(wheel_bytes).hexdigest())
        transport = _FakeTransport({})
        result = download_lock(
            LockInput(targets=_one({"foo": pin})),
            transport,  # type: ignore[arg-type]
            tmp_path,
            offline=True,
        )
        assert len(result.skipped) == 1
        assert result.written == ()
        assert transport.requested == []

    def test_local_path_artefact_still_succeeds(self, tmp_path: Path) -> None:
        src = tmp_path / "wheelhouse"
        src.mkdir()
        out = tmp_path / "out"
        wheel_bytes = b"WHEELDATA"
        wheel_file = src / "foo-1.0-py3-none-any.whl"
        wheel_file.write_bytes(wheel_bytes)
        wheel = WheelArtifact(
            filename="foo-1.0-py3-none-any.whl",
            url=wheel_file.as_uri(),
            hashes=(("sha256", hashlib.sha256(wheel_bytes).hexdigest()),),
            local_path=wheel_file,
        )
        pin = IndexPin(name="foo", version="1.0", index="local", wheels=(wheel,))
        result = download_lock(
            LockInput(targets=_one({"foo": pin})),
            _FakeTransport({}),  # type: ignore[arg-type]
            out,
            offline=True,
        )
        assert (out / "foo-1.0-py3-none-any.whl").read_bytes() == wheel_bytes
        assert len(result.written) == 1


def test_download_entry_is_a_dataclass() -> None:
    e = DownloadEntry(
        package="foo",
        version="1.0",
        filename="x.whl",
        url="https://x",
        hash_algo="sha256",
        digest="a" * 64,
    )
    # frozen dataclass guarantees structural equality and hashability
    assert e == DownloadEntry(
        package="foo",
        version="1.0",
        filename="x.whl",
        url="https://x",
        hash_algo="sha256",
        digest="a" * 64,
    )
    assert hash(e)


def test_download_event_loop_runs(tmp_path: Path) -> None:
    """Cover the asyncio.run() branch by exercising it with no work."""
    # download_lock spins up its own loop; make sure repeated calls work.
    asyncio.run(asyncio.sleep(0))
    download_lock(LockInput(targets=_one({})), _FakeTransport({}), tmp_path)  # type: ignore[arg-type]
