"""Tests for the nab CLI entry point."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import errno
import gc
import hashlib
import importlib
import inspect
import io
import json
import logging
import re
import runpy
import stat
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomli

from nab import _version as nab_version
from nab import cli
from nab._download import download
from nab._lock import (
    _BUILD_DEFAULT_OUTPUT,
    _determine_lock_anchor,
    _emit,
    _emit_pylock,
    lock,
    resolve_extra_selection,
    resolve_group_selection,
)
from nab.cli import (
    _DEFAULT_OUTPUT,
    _default_cache_dir,
    _make_transport,
    _normalize_layered_bool_flags,
    _resolve,
    _system_exit_status,
    app,
    console_entry,
    main,
)
from nab.output import Printer, ProgressReporter, Verbosity
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.local_index import LocalIndexClient, UnreadableLocalIndexError
from nab_index.transport import HttpError
from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.config import ConfigError, read_pyproject_config
from nab_project.config_sources import SourceRoots
from nab_project.download import DownloadError
from nab_project.fetch import FetchCoordinator
from nab_project.lockfile import (
    ArchivePin,
    DisjointnessError,
    DivergentBaseDependencyError,
    IndexPin,
    LocalPin,
    LockInput,
    MissingHashError,
    MissingSdistError,
    PinShape,
    SdistArtifact,
    TargetLock,
    WheelArtifact,
)
from nab_project.resolve import ResolveResult, TargetResult, env_signature
from nab_provider._vendor.packaging.pylock import Pylock
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import (
    InvalidUploadTimeError,
    MissingExtraError,
    ResolutionStrategy,
    SiblingMetadataDivergenceError,
    UnsupportedVcsError,
)
from nab_provider.records import WheelFile
from nab_provider.requirements_file import InvalidProjectRequirementError
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget, host_environment
from nab_resolver.errors import ResolutionError

V = Version

GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _target(
    py_minor: str = "3.11",
    platform_id: str = "linux_x86_64",
    selection: tuple[tuple[str, str], ...] = (),
) -> ResolveTarget:
    """A declared CPython target for the matrix-result fixtures."""
    target = ResolveTarget.for_declared(
        python_version=py_minor, spec=PlatformSpec(platform_id)
    )
    return target.with_selection(selection)


def _foo_index_pin(version: str = "1.0", name: str = "foo") -> IndexPin:
    """Build a fully-formed IndexPin with one wheel + sdist."""
    return IndexPin(
        name=name,
        version=version,
        index="pypi",
        sdist=SdistArtifact(
            filename=f"{name}-{version}.tar.gz",
            url=f"https://example.com/{name}-{version}.tar.gz",
            hashes=(("sha256", "b" * 64),),
        ),
        wheels=(
            WheelArtifact(
                filename=f"{name}-{version}-py3-none-any.whl",
                url=f"https://example.com/{name}-{version}-py3-none-any.whl",
                hashes=(("sha256", "a" * 64),),
            ),
        ),
    )


def _index_pins(pins: dict[str, Version]) -> dict[str, PinShape]:
    """One :class:`IndexPin` per resolved pin."""
    return {name: _foo_index_pin(str(ver), name) for name, ver in pins.items()}


def _target_lock(
    target: ResolveTarget,
    pins: dict[str, Version],
    dependencies: dict[str, tuple[str, ...]] | None = None,
) -> TargetLock:
    """What one target contributes to the lock: its pins and its edges."""
    return TargetLock(
        target=target,
        pins=_index_pins(pins),
        dependencies=dependencies if dependencies is not None else {},
    )


def _resolved(target: ResolveTarget, pins: dict[str, Version]) -> TargetResult:
    """A successful :class:`TargetResult` for ``target``."""
    return TargetResult(
        target=target, success=True, pins=pins, lock=_target_lock(target, pins)
    )


def _failed(target: ResolveTarget, error: ResolutionError | None) -> TargetResult:
    """A failed :class:`TargetResult`: no pins, no lock, just the error."""
    return TargetResult(target=target, success=False, pins={}, error=error, lock=None)


def _lock_input(pins: dict[str, PinShape]) -> LockInput:
    """The lock input one host-target resolve of ``pins`` produces."""
    target = ResolveTarget.for_host()
    return LockInput(targets={target.label: TargetLock(target=target, pins=pins)})


def _stub_lock_input(pins: dict[str, Version] | None = None) -> LockInput:
    """``_lock_input`` over index pins, for the emit helpers."""
    return _lock_input(_index_pins(pins if pins is not None else {"foo": V("1.0")}))


def _stub_resolve_result(
    *, version: str = "1.0", pins: dict[str, Version] | None = None
) -> ResolveResult:
    """Build a real :class:`ResolveResult` for the host target."""
    real_pins = pins if pins is not None else {"foo": V(version)}
    target = ResolveTarget.for_host()
    return ResolveResult(
        targets=(target,), target_results=[_resolved(target, real_pins)]
    )


def _fetchable_resolve_result(count: int) -> tuple[ResolveResult, dict[str, bytes]]:
    """A host resolve of ``count`` wheel-only pins, with the bytes each URL serves.

    Each digest is of the payload its own URL serves, so a real download
    writes every file instead of failing the hash check.
    """
    payloads: dict[str, bytes] = {}
    pins: dict[str, PinShape] = {}
    for index in range(count):
        name = f"pkg{index}"
        filename = f"{name}-1.0-py3-none-any.whl"
        url = f"https://example.com/{filename}"
        payloads[url] = f"wheel {index}".encode()
        pins[name] = IndexPin(
            name=name,
            version="1.0",
            index="pypi",
            wheels=(
                WheelArtifact(
                    filename=filename,
                    url=url,
                    hashes=(("sha256", hashlib.sha256(payloads[url]).hexdigest()),),
                ),
            ),
        )

    target = ResolveTarget.for_host()
    result = ResolveResult(
        targets=(target,),
        target_results=[
            TargetResult(
                target=target,
                success=True,
                pins=dict.fromkeys(pins, V("1.0")),
                lock=TargetLock(target=target, pins=pins),
            )
        ],
    )
    return result, payloads


def _hashless_resolve_result() -> ResolveResult:
    """A host resolve whose one wheel carries no hash at all."""
    target = ResolveTarget.for_host()
    pin = IndexPin(
        name="foo",
        version="1.0",
        index="pypi",
        wheels=(
            WheelArtifact(
                filename="foo-1.0-py3-none-any.whl",
                url="https://example.com/foo-1.0-py3-none-any.whl",
                hashes=(),
            ),
        ),
    )
    return ResolveResult(
        targets=(target,),
        target_results=[
            TargetResult(
                target=target,
                success=True,
                pins={"foo": V("1.0")},
                lock=TargetLock(target=target, pins={"foo": pin}),
            )
        ],
    )


def _sdist_archive(name: str = "foo", version: str = "1.0") -> bytes:
    """Build sdist bytes whose PKG-INFO declares one dependency."""
    pkg_info = (
        f"Metadata-Version: 2.2\nName: {name}\nVersion: {version}\n"
        "Requires-Dist: tampered-dep\n"
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{name}-{version}/PKG-INFO")
        info.size = len(pkg_info)
        tar.addfile(info, io.BytesIO(pkg_info))
    return buf.getvalue()


def _static_sdist_archive(name: str = "foo", version: str = "1.0") -> bytes:
    """Build sdist bytes with static project metadata and no dependencies."""
    pyproject = (
        f'[project]\nname = "{name}"\nversion = "{version}"\ndependencies = []\n'
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{name}-{version}/pyproject.toml")
        info.size = len(pyproject)
        tar.addfile(info, io.BytesIO(pyproject))
    return buf.getvalue()


requires_data_filter = pytest.mark.skipif(
    not hasattr(tarfile, "data_filter"),
    reason="sdist extraction requires the tar data filter (PEP 706)",
)


def _sidecarless_wheel(name: str = "foo", version: str = "1.0") -> bytes:
    """Build wheel bytes whose METADATA sits inside the archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/__init__.py", b"value = 1\n")
        zf.writestr(
            f"{name}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\nBody.\n",
        )
        zf.writestr(f"{name}-{version}.dist-info/WHEEL", b"Wheel-Version: 1.0\n")
    return buf.getvalue()


def _make_pyproject(tmp_path: Path, body: str = "") -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(body or '[project]\ndependencies = ["foo"]\n')
    return pyproject


def _make_archive_source_project(
    tmp_path: Path, *, percent_encoded: bool
) -> tuple[Path, str]:
    """Write a project backed by a local archive whose filename has a space."""
    archive = tmp_path / "foo 1.0.tar.gz"
    data = _static_sdist_archive()
    archive.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    encoded_url = archive.as_uri()
    url = encoded_url if percent_encoded else encoded_url.replace("%20", " ")
    source_url = f"{url}#sha256={digest}"
    pyproject = _make_pyproject(
        tmp_path,
        '[project]\nname = "probe"\nversion = "0.1"\ndependencies = ["foo"]\n'
        '[[tool.nab.archive-sources]]\nname = "foo"\n'
        f'url = "{source_url}"\n',
    )
    return pyproject, source_url


# A well-formed project for tests that stub the resolve; neither list is read.
_BUILD_SYSTEM_PROJECT = (
    '[project]\nname = "proj"\ndependencies = ["runtime-only"]\n'
    '[build-system]\nrequires = ["foo"]\n'
)


def _make_pylock_with_groups(tmp_path: Path) -> Path:
    """Write a minimal PEP 751 lock.

    Its ``dependency-groups`` is an array of names, a shape the PEP 735
    table never has.
    """
    pylock = tmp_path / "pylock.toml"
    pylock.write_text(
        'lock-version = "1.0"\nrequires-python = ">=3.10"\n'
        'dependency-groups = ["dev"]\ncreated-by = "nab"\npackages = []\n'
    )
    return pylock


def _mismatched_local_source_project(tmp_path: Path) -> str:
    """Write a sibling tree named ``bar`` and declare it as local source ``foo``."""
    member = tmp_path / "bar"
    member.mkdir()
    (member / "pyproject.toml").write_text('[project]\nname = "bar"\nversion = "1.0"\n')
    return (
        '[project]\nname = "root"\nversion = "0"\n'
        'dependencies = ["foo"]\n'
        "[[tool.nab.local-sources]]\n"
        'name = "foo"\n'
        'path = "bar"\n'
    )


def _source_name_mismatch_message(tmp_path: Path, prefix: str) -> str:
    target = (tmp_path / "bar").resolve()
    return (
        f"error: {prefix}: local source 'foo' declares package 'foo' but its"
        f" [project].name is 'bar' (at {target}); a source declared for one name"
        " must not provide a different project"
    )


class _SidecarResponse:
    """Minimal HttpResponse for the fake index transport."""

    def __init__(self, body: bytes, url: str) -> None:
        self.content = body
        self.status_code = 200
        self.url = url

    @property
    def headers(self) -> dict[str, str]:
        return {}

    @property
    def text(self) -> str:
        return self.content.decode()

    def json(self) -> object:
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        return None


class _SidecarTransport:
    """Serves Simple-API listing, sidecar, and wheel bytes keyed by URL.

    Every request gets the whole body, ignoring ``Range`` like a host with no
    range support.
    """

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self._bodies = bodies

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _SidecarResponse:
        del headers
        if url not in self._bodies:
            msg = f"unexpected request to {url}"
            raise AssertionError(msg)
        return _SidecarResponse(self._bodies[url], url)

    async def aclose(self) -> None:
        return None


class _ConcurrencyProbeTransport:
    """Serves wheel bytes and records how many fetches ever overlap.

    Every fetch yields to the event loop once per payload before returning,
    so each fetch the download starts is still open when the next one runs
    and ``peak`` measures its concurrency limit, not the loop's interleaving.
    """

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = payloads
        self._in_flight = 0
        self.peak = 0

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> _SidecarResponse:
        del headers
        self._in_flight += 1
        self.peak = max(self.peak, self._in_flight)

        for _ in range(len(self._payloads)):
            await asyncio.sleep(0)

        self._in_flight -= 1
        return _SidecarResponse(self._payloads[url], url)

    async def aclose(self) -> None:
        return None


def _universal_pyproject(tmp_path: Path) -> Path:
    return _make_pyproject(
        tmp_path,
        '[project]\ndependencies = ["foo"]\n'
        "[tool.nab]\n"
        'mode = "universal"\n'
        "[tool.nab.matrix]\n"
        'python = "==3.11"\n'
        'platforms = ["linux_x86_64"]\n',
    )


def _workspace_pyproject(tmp_path: Path, *, universal: bool = False) -> Path:
    """Build a workspace root with one member named ``alpha``."""
    member_dir = tmp_path / "alpha"
    member_dir.mkdir()
    (member_dir / "pyproject.toml").write_text(
        '[project]\nname = "alpha"\nversion = "0"\n'
    )
    body = '[project]\nname = "ws"\nversion = "0"\ndependencies = ["foo"]\n'
    if universal:
        body += (
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
    body += '[tool.nab.workspace]\nmembers = ["alpha"]\n'
    return _make_pyproject(tmp_path, body)


def _universal_result(
    *, success: bool, error: ResolutionError | None = None
) -> ResolveResult:
    """Build a real :class:`ResolveResult` with one matrix tuple."""
    tup = _target()
    tr = _resolved(tup, {"foo": V("1.0")}) if success else _failed(tup, error)
    return ResolveResult(targets=(tup,), target_results=[tr])


def _multi_tuple_universal_result() -> ResolveResult:
    """Build a successful ResolveResult with two tuples (3.11 and 3.12).

    Only 3.11 pins ``bar``, so the two tuples' pins and counts differ.
    """
    pins = {
        "3.11": {"bar": V("2.0"), "foo": V("1.0")},
        "3.12": {"foo": V("1.0")},
    }
    results = [
        _resolved(_target(py_minor), tuple_pins)
        for py_minor, tuple_pins in pins.items()
    ]
    return ResolveResult(
        targets=tuple(result.target for result in results), target_results=results
    )


def _multi_tuple_failed_result(error: ResolutionError | None) -> ResolveResult:
    """Two tuples where the second fails with ``error``; the first resolves."""
    ok, bad = (_target(py_minor) for py_minor in ("3.11", "3.12"))
    return ResolveResult(
        targets=(ok, bad),
        target_results=[_resolved(ok, {"foo": V("1.0")}), _failed(bad, error)],
    )


def _forked_universal_result() -> ResolveResult:
    """One matrix tuple that ``[tool.nab].conflicts`` forked into two.

    Only the ``cpu`` fork pins ``bar``, so the two forks differ.
    """
    pins = {
        "cpu": {"bar": V("2.0"), "foo": V("1.0")},
        "gpu": {"foo": V("1.0")},
    }
    results = [
        _resolved(_target(selection=(("extra", extra),)), fork_pins)
        for extra, fork_pins in pins.items()
    ]
    return ResolveResult(
        targets=tuple(result.target for result in results), target_results=results
    )


def _two_libc_universal_result() -> ResolveResult:
    """Two tuples on one platform_id, differing only in their libc."""
    tuples = tuple(
        ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64", libc=libc),
        )
        for libc in ("glibc", "musl")
    )
    return ResolveResult(
        targets=tuples,
        target_results=[_resolved(tup, {"foo": V("1.0")}) for tup in tuples],
    )


def _two_implementation_universal_result() -> ResolveResult:
    """Two Pythons on one platform, each in a CPython and a PyPy flavour.

    ``python_version`` varies, so a template can name it, but the
    implementation pairs still land on one path.
    """
    tuples = tuple(
        ResolveTarget.for_declared(
            python_version=py_minor,
            spec=PlatformSpec("linux_x86_64"),
            implementation=implementation,
            multi_implementation=True,
        )
        for py_minor in ("3.11", "3.12")
        for implementation in ("cpython", "pypy")
    )
    return ResolveResult(
        targets=tuples,
        target_results=[_resolved(tup, {"foo": V("1.0")}) for tup in tuples],
    )


def _mixed_implementation_universal_result() -> ResolveResult:
    """A CPython 3.11 tuple, plus a CPython and a PyPy 3.12 tuple.

    The first pair to collide under a ``{platform_id}`` template differs
    in ``python_version``, but naming it leaves the two 3.12 tuples on
    one path.
    """
    tuples = tuple(
        ResolveTarget.for_declared(
            python_version=py_minor,
            spec=PlatformSpec("linux_x86_64"),
            implementation=implementation,
            multi_implementation=True,
        )
        for py_minor, implementation in (
            ("3.11", "cpython"),
            ("3.12", "cpython"),
            ("3.12", "pypy"),
        )
    )
    return ResolveResult(
        targets=tuples,
        target_results=[_resolved(tup, {"foo": V("1.0")}) for tup in tuples],
    )


def _late_hashless_universal_result() -> ResolveResult:
    """Two tuples, where only the second one pins a hashless artefact.

    A marker-gated dependency produces this shape.
    """
    first, second = (_target(py_minor) for py_minor in ("3.11", "3.12"))
    hashless = IndexPin(
        name="priv",
        version="1.0",
        index="pypi",
        wheels=(
            WheelArtifact(
                filename="priv-1.0-py3-none-any.whl",
                url="https://example.com/priv-1.0-py3-none-any.whl",
                hashes=(),
            ),
        ),
    )
    return ResolveResult(
        targets=(first, second),
        target_results=[
            _resolved(first, {"foo": V("1.0")}),
            TargetResult(
                target=second,
                success=True,
                pins={"foo": V("1.0"), "priv": V("1.0")},
                lock=TargetLock(
                    target=second,
                    pins={"foo": _foo_index_pin(), "priv": hashless},
                    dependencies={},
                ),
            ),
        ],
    )


class TestLockCommandSpecific:
    """Tests for `nab lock` in single-environment mode."""

    def test_pylock_default(self, tmp_path: Path) -> None:
        """Default format writes a real pylock.toml at the requested path."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out)
        text = out.read_text()
        assert 'lock-version = "1.0"' in text
        assert 'name = "foo"' in text

    def test_pylock_default_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No --output: pylock format defaults to pylock.toml."""
        monkeypatch.chdir(tmp_path)
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject)
        assert (tmp_path / "pylock.toml").exists()

    def test_requirements_default_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No --output: requirements format defaults to requirements.txt."""
        monkeypatch.chdir(tmp_path)
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, format="requirements")
        text = (tmp_path / "requirements.txt").read_text()
        assert "foo==1.0" in text
        assert "--hash=sha256:" in text

    def test_requirements_without_hashes_default_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """requirements-without-hashes defaults to requirements.txt."""
        monkeypatch.chdir(tmp_path)
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, format="requirements-without-hashes")
        text = (tmp_path / "requirements.txt").read_text()
        assert "foo==1.0" in text
        assert "--hash" not in text

    def test_build_requirements_pylock_default_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A build lock defaults clear of the runtime lock's name."""
        monkeypatch.chdir(tmp_path)
        pyproject = _make_pyproject(tmp_path, _BUILD_SYSTEM_PROJECT)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, build_requirements=True)
        assert (tmp_path / "pylock.build.toml").exists()
        assert not (tmp_path / "pylock.toml").exists()

    @pytest.mark.parametrize(
        "lock_format", ["requirements", "requirements-without-hashes"]
    )
    def test_build_requirements_requirements_default_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_format: str
    ) -> None:
        """Both requirements formats get their own default name."""
        monkeypatch.chdir(tmp_path)
        pyproject = _make_pyproject(tmp_path, _BUILD_SYSTEM_PROJECT)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, format=lock_format, build_requirements=True)
        assert (tmp_path / "build-requirements.txt").exists()
        assert not (tmp_path / "requirements.txt").exists()

    def test_every_format_has_a_build_default(self) -> None:
        """A format added to one map alone would be a KeyError, not an error."""
        assert _BUILD_DEFAULT_OUTPUT.keys() == _DEFAULT_OUTPUT.keys()

    def test_build_lock_reuses_its_own_prior_anchor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The build lock's cutoff comes from the build lock, not pylock.toml."""
        monkeypatch.chdir(tmp_path)
        recorded = datetime(2024, 1, 1, tzinfo=timezone.utc)
        (tmp_path / "pylock.build.toml").write_text(
            f"[tool.nab]\ncreated-at = {recorded.isoformat()}\n"
        )
        (tmp_path / "pylock.toml").write_text(
            "[tool.nab]\ncreated-at = 2020-01-01T00:00:00+00:00\n"
        )
        pyproject = _make_pyproject(tmp_path, _BUILD_SYSTEM_PROJECT)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, build_requirements=True)
        written = tomli.loads((tmp_path / "pylock.build.toml").read_text())
        assert written["tool"]["nab"]["created-at"] == recorded

    def test_build_lock_records_no_group_selection(self, tmp_path: Path) -> None:
        """The project's group settings describe a selection it does not have."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "proj"\ndependencies = ["runtime-only"]\n'
            '[build-system]\nrequires = ["foo"]\n'
            '[dependency-groups]\ndev = ["foo"]\n'
            '[tool.nab]\ndefault-groups = ["dev"]\nbase-group = "default"\n',
        )
        out = tmp_path / "pylock.build.toml"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out, build_requirements=True)
        written = tomli.loads(out.read_text())
        assert "default-groups" not in written
        assert "dependency-groups" not in written

    def test_build_group_reaches_the_lock(self, tmp_path: Path) -> None:
        """The configured name is what the writer offers and gates on."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "proj"\ndependencies = ["foo"]\n'
            '[build-system]\nrequires = ["foo"]\n'
            '[tool.nab]\nbase-group = "main"\nbuild-group = "build"\n',
        )
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_stub_resolve_result(pins={"foo": V("1.0")}),
        ):
            lock(pyproject, output=out)
        written = tomli.loads(out.read_text())
        assert written["dependency-groups"] == ["main", "build"]
        assert written["default-groups"] == ["main"]

    def test_build_group_naming_a_declared_group_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The name for the build requirements is already a group's own."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "proj"\ndependencies = ["foo"]\n'
            '[build-system]\nrequires = ["foo"]\n'
            '[dependency-groups]\nbuild = ["foo"]\n'
            '[tool.nab]\nbase-group = "main"\nbuild-group = "build"\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=tmp_path / "pylock.toml")
        err = capsys.readouterr().err
        assert "build-group 'build' and [dependency-groups] 'build'" in err
        assert "Traceback" not in err

    def test_build_requirements_locks_the_build_requires(self, tmp_path: Path) -> None:
        """End to end: the emitted lock holds the build requirement alone."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "proj"\ndependencies = ["runtime-only"]\n'
            '[build-system]\nrequires = ["builder"]\n'
            + "".join(
                f'[[tool.nab.local-sources]]\nname = "{name}"\npath = "{name}"\n'
                for name in ("runtime-only", "builder")
            ),
        )
        for name in ("runtime-only", "builder"):
            member = tmp_path / name
            member.mkdir()
            (member / "pyproject.toml").write_text(
                f'[project]\nname = "{name}"\nversion = "1.0"\n'
            )
        out = tmp_path / "pylock.build.toml"
        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: make_coordinator([])
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            lock(pyproject, output=out, build_requirements=True, cache=False)
        written = tomli.loads(out.read_text())
        assert [pkg["name"] for pkg in written["packages"]] == ["builder"]

    @pytest.mark.parametrize(
        "selection",
        [
            {"groups": ("dev",)},
            {"all_groups": True},
            {"extras": ("gpu",)},
            {"all_extras": True},
            {"project_default_group": ("dev",)},
            {"project_base_group": "default"},
            {"project_build_group": "build"},
        ],
    )
    def test_build_requirements_refuses_a_selection(
        self, tmp_path: Path, selection: dict[str, object]
    ) -> None:
        """[build-system].requires is one flat list with nothing to select."""
        pyproject = _make_pyproject(tmp_path, _BUILD_SYSTEM_PROJECT)
        err = io.StringIO()
        with (
            contextlib.redirect_stderr(err),
            pytest.raises(SystemExit) as exc,
        ):
            lock(pyproject, build_requirements=True, **selection)
        assert exc.value.code == 1
        assert "no groups or extras to select" in err.getvalue()

    def test_requirements_writes_to_file(self, tmp_path: Path) -> None:
        """`requirements` format renders --hash lines."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out, format="requirements")
        text = out.read_text()
        assert "foo==1.0" in text
        assert "--hash=sha256:" in text

    def test_archive_source_unescaped_space_exits_cleanly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A URL that requirements syntax would split is rejected at config load."""
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )
        pyproject, source_url = _make_archive_source_project(
            tmp_path, percent_encoded=False
        )
        url = source_url.partition("#")[0]
        out = tmp_path / "requirements.txt"

        with pytest.raises(SystemExit, match="1"):
            lock(
                pyproject,
                output=out,
                format="requirements",
                cache_dir=tmp_path / "cache",
            )

        err = capsys.readouterr().err
        assert f"archive URL {url!r} contains an unescaped space" in err
        assert "percent-encode spaces as %20" in err
        assert "Traceback" not in err
        assert not out.exists()

    @requires_data_filter
    def test_archive_source_percent_encoded_space_renders_requirement(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A percent-encoded local archive resolves to a parseable requirement."""
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )
        pyproject, source_url = _make_archive_source_project(
            tmp_path, percent_encoded=True
        )
        out = tmp_path / "requirements.txt"

        lock(
            pyproject,
            output=out,
            format="requirements",
            cache_dir=tmp_path / "cache",
        )

        rendered = out.read_text().strip()
        assert "%20" in rendered
        requirement = Requirement(rendered)
        assert requirement.url == source_url

    def test_requirements_without_hashes_writes_to_file(self, tmp_path: Path) -> None:
        """requirements-without-hashes renders one name==version per line."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out, format="requirements-without-hashes")
        text = out.read_text()
        assert text.strip() == "foo==1.0"

    def test_hashless_pin_locks_without_hashes(self, tmp_path: Path) -> None:
        """A pin whose artefact lacks a usable hash still locks plain pins."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        result = _hashless_resolve_result()
        with patch("nab.cli.resolve_for_targets", return_value=result):
            lock(pyproject, output=out, format="requirements-without-hashes")
        assert out.read_text().strip() == "foo==1.0"

    def test_hashless_pin_fails_pylock(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The same hashless pin is fatal for the hash-bearing pylock format."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        result = _hashless_resolve_result()
        with (
            patch("nab.cli.resolve_for_targets", return_value=result),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out, format="pylock")
        assert "no acceptable hash" in capsys.readouterr().err

    def test_failed_target_reports_resolution_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One environment has one error, so it is the run's error."""
        pyproject = _make_pyproject(tmp_path)
        target = ResolveTarget.for_host()
        result = ResolveResult(
            targets=(target,),
            target_results=[_failed(target, ResolutionError("conflict"))],
        )
        with (
            patch("nab.cli.resolve_for_targets", return_value=result),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        assert "resolution failed: conflict" in capsys.readouterr().err

    def test_forked_specific_failure_reports_per_fork_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A conflict fork in specific mode reports every fork, not one error.

        Directly co-selecting two members of a declared conflict set
        forks a single-environment resolve into one target per member,
        so a failed fork has a sibling that resolved.  The report labels
        each fork and keeps the succeeded fork's pins, as a matrix does.
        """
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "x"\nversion = "1"\ndependencies = ["torch"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch"]\n'
            'gpu = ["torch"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n',
        )
        host = ResolveTarget.for_host()
        cpu = host.with_selection((("extra", "cpu"),))
        gpu = host.with_selection((("extra", "gpu"),))
        result = ResolveResult(
            targets=(cpu, gpu),
            target_results=[
                _resolved(cpu, {"torch": V("2.3.0")}),
                _failed(gpu, ResolutionError("no compatible torch-cuda wheel")),
            ],
        )
        with (
            patch("nab.cli.resolve_for_targets", return_value=result),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(
                pyproject,
                format="requirements-without-hashes",
                extras=("cpu", "gpu"),
            )
        err = capsys.readouterr().err
        assert "# host-extra-cpu" in err
        assert "torch==2.3.0" in err
        assert "# host-extra-gpu: FAILED" in err
        assert "#   ResolutionError: no compatible torch-cuda wheel" in err

    def test_pylock_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--output -` routes pylock format to stdout."""
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=Path("-"))
        out = capsys.readouterr().out
        assert 'lock-version = "1.0"' in out
        assert 'name = "foo"' in out

    def test_requirements_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--output -` routes requirements format to stdout."""
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=Path("-"), format="requirements")
        out = capsys.readouterr().out
        assert "foo==1.0" in out
        assert "--hash=sha256:" in out

    def test_requirements_without_hashes_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--output -` routes requirements-without-hashes to stdout."""
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=Path("-"), format="requirements-without-hashes")
        assert capsys.readouterr().out.strip() == "foo==1.0"

    def test_resolution_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets", side_effect=ResolutionError("conflict")
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "resolution failed" in capsys.readouterr().err

    def test_unknown_group_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A typo'd --group with a present table exits 1, not a raw traceback."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "x"\nversion = "1"\ndependencies = ["foo"]\n'
            "[dependency-groups]\n"
            'dev = ["ruff"]\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, groups=("typo",), offline=True, cache=False)
        assert "Dependency group 'typo' not found" in capsys.readouterr().err

    def test_unsupported_vcs_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=UnsupportedVcsError("refusing direct-URL requirement"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "refusing direct-URL requirement" in capsys.readouterr().err

    def test_invalid_upload_time_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A naive index upload-time exits 1 with a clean message, not a traceback."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=InvalidUploadTimeError("foo 1.0 has a naive upload time"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "naive upload time" in capsys.readouterr().err

    def test_not_implemented_vcs_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A VCS URL admitted by policy but unimplemented exits 1, not a traceback."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=NotImplementedError("resolver path is not implemented"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "resolver path is not implemented" in capsys.readouterr().err

    def test_config_error_during_resolve_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ConfigError raised mid-resolve (e.g. constraint with extras) exits 1."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=ConfigError("Constraints cannot have extras: idna[foo]<3"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "Constraints cannot have extras" in capsys.readouterr().err

    def test_missing_dependencies_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_for_targets", side_effect=KeyError("dependencies")),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "no [project].dependencies" in capsys.readouterr().err

    def test_string_project_table_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-table [project] exits 1 with a diagnostic, not a traceback."""
        pyproject = _make_pyproject(tmp_path, 'project = "hello"\n')
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject)
        err = capsys.readouterr().err
        assert "[project] must be a table" in err
        assert "Traceback" not in err

    def test_array_project_table_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An array [project] exits 1 with a diagnostic, not a traceback."""
        pyproject = _make_pyproject(tmp_path, 'project = ["a", "b"]\n')
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject)
        err = capsys.readouterr().err
        assert "[project] must be a table" in err
        assert "Traceback" not in err

    def test_missing_hash_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets", side_effect=MissingHashError("no hash")
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "cannot lock" in capsys.readouterr().err

    def test_invalid_requirement_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A malformed dependency string exits 1 instead of tracebacking."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=InvalidProjectRequirementError("invalid requirement 'x y'"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "invalid requirement" in capsys.readouterr().err

    def test_http_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An index HTTP failure during resolve exits 1, not a traceback."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=HttpError("GET https://pypi.org/simple/foo/ failed: 503"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "503" in err

    def test_malformed_simple_response_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A malformed Simple-API listing exits 1 cleanly, not a raw traceback.

        Raises the parser's real error so the test tracks whatever type a
        broken 200 body produces.
        """
        from nab_index.client import _parse_files

        with pytest.raises(HttpError, match="malformed Simple-API") as caught:
            _parse_files(b"<!doctype html>", "https://pypi.org/simple/", "foo")

        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_for_targets", side_effect=caught.value),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        err = capsys.readouterr().err
        assert "malformed Simple-API" in err
        assert "Traceback" not in err

    def test_unreadable_local_index_exits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unreadable file:// index exits 1 cleanly, not a raw traceback.

        A local index makes no request, so its failures are not HTTP errors.
        Raises the local client's real error so the test tracks whatever type
        a wheelhouse the process cannot list produces.
        """
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        (wheelhouse / "foo-1.0-py3-none-any.whl").write_bytes(b"")

        # A real chmod would not do: root ignores the mode bits and Windows
        # has none.
        real_iterdir = Path.iterdir

        def denied(self: Path) -> Iterator[Path]:
            if self == wheelhouse:
                raise PermissionError(errno.EACCES, "Permission denied", str(self))
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", denied)

        client = LocalIndexClient(wheelhouse.as_uri())
        with pytest.raises(UnreadableLocalIndexError) as caught:
            asyncio.run(client.get_files("foo"))

        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_for_targets", side_effect=caught.value),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)

        err = capsys.readouterr().err
        assert "Permission denied" in err
        assert "Traceback" not in err

    def test_missing_sdist_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """MissingSdistError exits 1 with the message instead of a traceback."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=MissingSdistError("foo==1.0 has no sdist"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "cannot lock" in capsys.readouterr().err

    def test_lookup_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``LookupError`` exits 1 with the message instead of a traceback.

        Surface for unknown group / extra selections; the resolver
        raises ``LookupError`` so the user sees the typo immediately.
        """
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=LookupError("unknown group 'ghost'"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "unknown group" in capsys.readouterr().err

    def test_missing_extra_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A root extra the package does not declare exits 1, not a traceback."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=MissingExtraError(
                    "foo==1.0 does not provide extra 'nonexistent'"
                ),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "does not provide extra 'nonexistent'" in err
        assert "Traceback" not in err

    def test_unevaluable_root_marker_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``~= "3"`` is a valid marker no comparison decides.

        PEP 440 gives the compatible-release operator no meaning over a
        single-component release, so PEP 508 accepts the clause and nothing
        evaluates it. It needs no matrix and no ``--python``.
        """
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "probe"\nversion = "0.1.0"\n'
            "dependencies = [\"somepkg; python_full_version ~= '3'\"]\n",
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=Path("-"), offline=True)

        err = capsys.readouterr().err
        assert 'cannot lock: marker python_full_version ~= "3"' in err
        assert "cannot be evaluated" in err
        assert "Traceback" not in err

    def test_unnormalizable_extra_name_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An extra PEP 751 will not accept exits 1, not a traceback.

        ``_cli`` canonicalizes to ``-cli``, which the top-level ``extras``
        array cannot hold. The refusal quotes the key as the pyproject
        writes it, and a lock already on disk survives it.
        """
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "proj"\nversion = "0.1"\ndependencies = []\n'
            "[project.optional-dependencies]\n_cli = []\n",
        )
        out = tmp_path / "pylock.toml"
        out.write_text("prior lock\n")

        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=out, all_extras=True, offline=True)

        err = capsys.readouterr().err
        assert "cannot lock: extra '_cli' normalizes to '-cli'" in err
        assert "Traceback" not in err

        assert out.read_text() == "prior lock\n"

    def test_sibling_metadata_divergence_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A version whose tie-ranked wheels disagree on deps exits 1, not a traceback.

        ``SiblingMetadataDivergenceError`` is not a ``MetadataError``, so the CLI
        must name it in the exit handlers rather than rely on the ``MetadataError``
        branch.
        """
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=SiblingMetadataDivergenceError(
                    "foo 1.0 has tie-ranked wheels foo-1.0-cp311.cp312-none-any.whl "
                    "and foo-1.0-cp312.cp313-none-any.whl that declare different "
                    "dependencies for this target"
                ),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "tie-ranked wheels" in err
        assert "Traceback" not in err

    def test_metadata_hash_mismatch_exits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A PEP 658 sidecar failing its published hash exits 1, not a traceback.

        Drives a real resolve against a fake index that serves a wheel
        advertising a ``core-metadata`` sha256 the sidecar bytes do not match.
        """
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )
        pyproject = _make_pyproject(tmp_path)

        wheel_url = "https://files.example.com/foo-1.0-py3-none-any.whl"
        listing = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": wheel_url,
                    "core-metadata": {"sha256": "0" * 64},
                }
            ]
        }
        transport = _SidecarTransport(
            {
                "https://pypi.org/simple/foo/": json.dumps(listing).encode(),
                f"{wheel_url}.metadata": (
                    b"Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n\n"
                ),
            }
        )

        with (
            patch("nab.cli._make_transport", return_value=transport),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml", cache=False)

        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "sha256 mismatch" in err
        assert "0" * 64 in err
        assert "Traceback" not in err

    def test_metadata_hash_mismatch_below_prefetch_window_exits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A pin no prefetch covers still has its sidecar checked.

        The coordinator warms the newest ``PREFETCH_METADATA_COUNT`` versions
        as soon as a listing lands and forwards the published digest itself.
        Pinning an older version leaves the provider's own request as the only
        carrier of that digest.
        """
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )

        # Guards the premise: the pin has to sit outside the prefetch window.
        versions = ("1.0", "2.0", "3.0")
        pinned = versions[0]
        assert len(versions) > FetchCoordinator.PREFETCH_METADATA_COUNT

        pyproject = _make_pyproject(
            tmp_path, f'[project]\ndependencies = ["foo=={pinned}"]\n'
        )

        files: list[dict[str, object]] = []
        bodies: dict[str, bytes] = {}
        for version in versions:
            url = f"https://files.example.com/foo-{version}-py3-none-any.whl"
            sidecar = f"Metadata-Version: 2.1\nName: foo\nVersion: {version}\n\n"
            bodies[f"{url}.metadata"] = sidecar.encode()

            # Only the pin advertises a digest its sidecar bytes do not match.
            published = (
                "0" * 64
                if version == pinned
                else hashlib.sha256(sidecar.encode()).hexdigest()
            )
            files.append(
                {
                    "filename": f"foo-{version}-py3-none-any.whl",
                    "url": url,
                    "core-metadata": {"sha256": published},
                }
            )

        bodies["https://pypi.org/simple/foo/"] = json.dumps({"files": files}).encode()
        transport = _SidecarTransport(bodies)

        with (
            patch("nab.cli._make_transport", return_value=transport),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml", cache=False)

        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "sha256 mismatch" in err
        assert "0" * 64 in err
        assert not (tmp_path / "pylock.toml").exists()

    def test_wheel_hash_mismatch_exits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A full-body wheel failing its published hash exits 1, not a traceback.

        Drives a real resolve against a fake index that publishes a sha256 for
        a sidecar-less wheel.  The transport ignores ``Range``, so the read
        steps down to the whole body and checks it against that digest.
        """
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )
        pyproject = _make_pyproject(tmp_path)

        wheel_url = "https://files.example.com/foo-1.0-py3-none-any.whl"
        listing = {
            "files": [
                {
                    "filename": "foo-1.0-py3-none-any.whl",
                    "url": wheel_url,
                    "hashes": {"sha256": "0" * 64},
                }
            ]
        }
        transport = _SidecarTransport(
            {
                "https://pypi.org/simple/foo/": json.dumps(listing).encode(),
                wheel_url: _sidecarless_wheel(),
            }
        )

        with (
            patch("nab.cli._make_transport", return_value=transport),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml", cache=False)

        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "wheel sha256 mismatch" in err
        assert "0" * 64 in err
        assert "Traceback" not in err

    def test_sdist_hash_mismatch_exits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An sdist failing its published hash exits 1, not a traceback.

        Drives a real resolve against a fake index whose only file for ``foo``
        is an sdist published with a sha256 the served archive does not match.
        """
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )
        pyproject = _make_pyproject(tmp_path)

        sdist_url = "https://files.example.com/foo-1.0.tar.gz"
        listing = {
            "files": [
                {
                    "filename": "foo-1.0.tar.gz",
                    "url": sdist_url,
                    "hashes": {"sha256": "0" * 64},
                }
            ]
        }
        transport = _SidecarTransport(
            {
                "https://pypi.org/simple/foo/": json.dumps(listing).encode(),
                sdist_url: _sdist_archive(),
            }
        )

        with (
            patch("nab.cli._make_transport", return_value=transport),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml", cache=False)

        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "sdist sha256 mismatch" in err
        assert "0" * 64 in err
        assert "Traceback" not in err

    def test_dynamic_local_source_forbidden_build_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A local source needing a forbidden build exits 1, not a traceback.

        Dynamic metadata under build-policy never raises
        ``UnsupportedSdistError``, a ``MetadataError`` subclass.
        """
        member = tmp_path / "mylocal"
        member.mkdir()
        (member / "pyproject.toml").write_text(
            '[project]\nname = "mylocal"\nversion = "1.0"\ndynamic = ["dependencies"]\n'
        )
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "root"\nversion = "0"\n'
            'dependencies = ["mylocal"]\n'
            "[tool.nab]\n"
            'build-policy = "never"\n'
            "[[tool.nab.local-sources]]\n"
            'name = "mylocal"\n'
            'path = "mylocal"\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, offline=True, output=Path("-"), cache=False)
        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "has dynamic metadata" in err
        assert "Traceback" not in err

    def test_local_source_naming_another_project_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A local source whose tree is a different project exits 1, not a traceback."""
        pyproject = _make_pyproject(
            tmp_path, _mismatched_local_source_project(tmp_path)
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, offline=True, output=Path("-"), cache=False)
        err = capsys.readouterr().err
        assert err.splitlines() == [
            _source_name_mismatch_message(tmp_path, "cannot lock")
        ]

    def test_missing_file_exits(self, tmp_path: Path) -> None:
        """Exit 1 when pyproject.toml doesn't exist."""
        with pytest.raises(SystemExit, match="1"):
            lock(tmp_path / "missing.toml")

    def test_directory_path_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A directory path exits 1 with a clean message, not a traceback."""
        with pytest.raises(SystemExit, match="1"):
            lock(tmp_path)
        assert "is a directory" in capsys.readouterr().err

    def test_pipe_path_exits(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        as_fifo: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        """A pipe is named for what it is, not reported as a missing file.

        ``nab lock <(...)`` hands the CLI a FIFO.  The file is there, so
        calling it absent sends the user looking for the wrong problem.
        """
        pyproject = _make_pyproject(tmp_path)
        with as_fifo(pyproject), pytest.raises(SystemExit, match="1"):
            lock(pyproject)
        err = capsys.readouterr().err
        assert f"{pyproject} exists but is not a regular file" in err
        assert "not found" not in err

    def test_malformed_toml_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A TOML syntax error reports a clean message, not a traceback."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"\n')
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=Path("-"))
        assert "is not valid TOML" in capsys.readouterr().err

    def test_non_utf8_toml_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A byte that will not decode reports a clean message, not a traceback."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_bytes(b'[project]\ndescription = "\xe9"\ndependencies = []\n')
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=Path("-"))
        assert "is not valid TOML" in capsys.readouterr().err

    def test_unreadable_toml_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unreadable pyproject reports a clean message, not a traceback."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "demo"\ndependencies = []\n')
        denied = PermissionError(errno.EACCES, "Permission denied", str(pyproject))
        with (
            patch.object(Path, "open", side_effect=denied),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=Path("-"))
        err = capsys.readouterr().err
        # OSError renders its filename with repr, which doubles the
        # backslashes of a Windows path.
        assert err.splitlines() == [
            (
                f"error: in [tool.nab]: cannot read {pyproject}:"
                f" [Errno {errno.EACCES}] Permission denied: {str(pyproject)!r}"
            )
        ]

    def test_pylock_passed_as_the_project_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A PEP 751 lock handed in where a pyproject belongs says so."""
        pylock = tmp_path / "pylock.toml"
        pylock.write_text(
            'lock-version = "1.0"\ncreated-by = "nab"\n\n'
            '[[packages]]\nname = "idna"\nversion = "3.10"\n'
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pylock)
        assert "is a PEP 751 lockfile" in capsys.readouterr().err

    def test_output_is_directory_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--output naming an existing directory exits cleanly, not a traceback."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        out.mkdir()
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out)
        assert "cannot write output" in capsys.readouterr().err

    def test_output_missing_parent_dir_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--output under a non-existent directory exits cleanly, not a traceback."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "nope" / "pylock.toml"
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out)
        assert "cannot write output" in capsys.readouterr().err

    def test_full_disk_keeps_committed_lock(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        cap_writes: Callable[[int], AbstractContextManager[None]],
    ) -> None:
        """A write that runs out of space leaves the committed lock in place."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        committed = b'lock-version = "1.0"\n'
        out.write_bytes(committed)
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            cap_writes(64),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out)
        assert "cannot write output" in capsys.readouterr().err
        assert out.read_bytes() == committed

    def test_resolution_flag_threads_to_resolver(self, tmp_path: Path) -> None:
        """``--project-resolution lowest`` reaches resolve_for_targets as the enum."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
        ) as mock_resolve:
            lock(pyproject, output=out, project_resolution="lowest")
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs["resolution_strategy"] is ResolutionStrategy.LOWEST

    def test_resolution_flag_default_none(self, tmp_path: Path) -> None:
        """No --project-resolution: resolve_for_targets sees ``None`` (config wins)."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
        ) as mock_resolve:
            lock(pyproject, output=out)
        assert mock_resolve.call_args.kwargs["resolution_strategy"] is None

    def test_cli_project_override_prints_notice(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A CLI PROJECT override on ``nab lock`` is surfaced on stderr."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out, project_resolution="lowest")
        err = capsys.readouterr().err
        assert "does not derive from the committed" in err
        assert "--project-resolution -> lowest" in err

    def test_no_cli_project_override_prints_no_notice(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No CLI PROJECT override: no reproducibility notice."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out)
        assert "does not derive from the committed" not in capsys.readouterr().err

    def test_config_layer_error_exits(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cross-file conflict exits 1 via the shared [tool.nab] map."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n[tool.nab]\nresolution = "highest"\n',
        )
        (tmp_path / "nab.toml").write_text('resolution = "lowest"\n')
        out = tmp_path / "pylock.toml"
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out)
        # The merged config now sources every PROJECT key from the registry
        # ladder, so a cross-file conflict surfaces while loading the config
        # (the shared [tool.nab] error map) rather than later in the
        # run-settings fold.
        err = capsys.readouterr().err
        assert "in [tool.nab]:" in err
        assert "conflicting values" in err

    def test_standalone_nab_toml_malformed_exits(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A malformed standalone user nab.toml exits 1, not a traceback."""
        pyproject = _make_pyproject(tmp_path)
        user = tmp_path / "usr" / "nab" / "nab.toml"
        user.parent.mkdir(parents=True)
        user.write_text("offline = \n")
        out = tmp_path / "pylock.toml"
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(user_toml=user, project_dir=p.parent, pyproject=p),
        )
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out)
        assert "config error:" in capsys.readouterr().err

    def test_standalone_nab_toml_unknown_key_exits(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unknown key in a standalone user nab.toml exits 1, not a traceback."""
        pyproject = _make_pyproject(tmp_path)
        user = tmp_path / "usr" / "nab" / "nab.toml"
        user.parent.mkdir(parents=True)
        user.write_text("typoo = 1\n")
        out = tmp_path / "pylock.toml"
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(user_toml=user, project_dir=p.parent, pyproject=p),
        )
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out)
        err = capsys.readouterr().err
        assert "config error:" in err
        assert "typoo" in err


class TestPythonFlag:
    """``--python`` retargets the resolve for one run."""

    def test_threads_the_python_version_to_the_resolver(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
        ) as mock_resolve:
            lock(pyproject, output=out, python="3.11")
        config = mock_resolve.call_args.kwargs["config"]
        assert config.environment.python == "3.11"

    def test_absent_flag_leaves_the_host_target(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
        ) as mock_resolve:
            lock(pyproject, output=out)
        assert mock_resolve.call_args.kwargs["config"].environment is None

    def test_invalid_value_is_a_flag_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bad value names the flag; there is no [tool.nab] table to fix."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with pytest.raises(SystemExit) as exc:
            lock(pyproject, output=out, python="3.12.x")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert (
            "error: --python must be a version like '3.12' or '3.12.4',"
            " got '3.12.x'" in err
        )
        assert "[tool.nab]" not in err
        assert not out.exists()

    def test_download_invalid_value_is_a_flag_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit) as exc:
            download(pyproject, output=tmp_path / "wheels", python="3.12.x")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "error: --python must be a version like" in err
        assert "[tool.nab]" not in err

    def test_rejected_in_universal_mode(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The matrix declares the python axis, so the flag has nowhere to land."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with pytest.raises(SystemExit) as exc:
            lock(pyproject, output=out, python="3.11")
        assert exc.value.code == 1
        assert "--python is not supported in universal mode" in capsys.readouterr().err
        assert not out.exists()

    def test_download_threads_the_python_version(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "wheels"
        with (
            patch(
                "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
            ) as mock_resolve,
            patch(
                "nab._download.download_lock",
                return_value=MagicMock(written=(out / "x.whl",), skipped=()),
            ),
        ):
            download(pyproject, output=out, python="3.11")
        config = mock_resolve.call_args.kwargs["config"]
        assert config.environment.python == "3.11"

    def test_retargets_a_free_threaded_platform_onto_a_new_python(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--python`` lands before the free-threaded floor is checked.

        Checked against the 3.12 host instead, the floor would reject the
        run the flag retargets onto 3.14.
        """
        env = {**host_environment(), "python_full_version": "3.12.11"}
        monkeypatch.setattr("nab_project.config.host_environment", lambda: env)

        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            '[tool.nab]\nbuild-policy = "never"\n'
            "[tool.nab.environment]\n"
            'platform = { id = "linux_x86_64", free-threaded = true }\n',
        )
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
        ) as mock_resolve:
            lock(pyproject, output=out, python="3.14")
        assert mock_resolve.call_args.kwargs["config"].environment.python == "3.14"

    def test_download_rejects_it_in_universal_mode(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _universal_pyproject(tmp_path)
        with pytest.raises(SystemExit) as exc:
            download(pyproject, output=tmp_path / "wheels", python="3.11")
        assert exc.value.code == 1
        assert "--python is not supported in universal mode" in capsys.readouterr().err


class TestProjectFlagErrors:
    """A bad ``--project-*`` value reads as a flag error, not a table one.

    These overrides fold through the same ``[tool.nab]`` parse the file
    uses, so the error must name the flag rather than the ``[tool.nab]``
    table, which the project may not even have.
    """

    def test_requires_python_bad_value_names_the_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with pytest.raises(SystemExit) as exc:
            lock(pyproject, output=out, project_requires_python="@@@")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert (
            "error: --project-requires-python: requires-python must be a"
            " PEP 440 specifier, got '@@@'" in err
        )
        assert "[tool.nab]" not in err
        assert not out.exists()

    def test_uploaded_prior_to_bad_value_names_the_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with pytest.raises(SystemExit) as exc:
            lock(pyproject, output=out, project_uploaded_prior_to="not-a-date")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--project-uploaded-prior-to: uploaded-prior-to must be an" in err
        assert "[tool.nab]" not in err
        assert not out.exists()

    def test_constraint_bad_value_names_the_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with pytest.raises(SystemExit) as exc:
            lock(pyproject, output=out, project_constraint=("this is not pep508 !!!",))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--project-constraint: constraints[0] is not a valid requirement" in err
        assert "[tool.nab]" not in err
        assert not out.exists()

    def test_requires_python_digit_run_past_int_limit_names_the_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Version() raises a bare ValueError for a digit run past CPython's
        # int limit, so the flag parse must reject it like any bad value.
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with pytest.raises(SystemExit) as exc:
            lock(pyproject, output=out, project_requires_python=">=3." + "9" * 5000)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert (
            "error: --project-requires-python: requires-python must be a"
            " PEP 440 specifier" in err
        )
        assert "[tool.nab]" not in err
        assert not out.exists()

    def test_download_requires_python_bad_value_names_the_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit) as exc:
            download(
                pyproject, output=tmp_path / "wheels", project_requires_python="@@@"
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "error: --project-requires-python: requires-python must be a" in err
        assert "[tool.nab]" not in err

    def test_download_build_group_bad_value_names_the_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--project-build-group`` reaches the registry from this command too."""
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit) as exc:
            download(pyproject, output=tmp_path / "wheels", project_build_group="-no-")
        assert exc.value.code == 1
        assert "error: --project-build-group:" in capsys.readouterr().err

    def test_valid_override_threads_through(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
        ) as mock_resolve:
            lock(pyproject, output=out, project_requires_python="==3.11")
        assert mock_resolve.call_args.kwargs["config"].requires_python == "==3.11"

    def test_bad_file_value_still_reads_as_a_table_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n[tool.nab]\nrequires-python = "@@@"\n',
        )
        out = tmp_path / "pylock.toml"
        with pytest.raises(SystemExit) as exc:
            lock(pyproject, output=out)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "in [tool.nab]: requires-python must be a PEP 440 specifier" in err


class TestLockCommandUniversal:
    """Tests for `nab lock` in universal mode."""

    def test_invalid_requirement_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A malformed dependency string exits 1 instead of tracebacking."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=InvalidProjectRequirementError("invalid requirement 'x y'"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out)
        assert "invalid requirement" in capsys.readouterr().err

    def test_invalid_upload_time_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A naive index upload-time exits 1 with a clean message, not a traceback."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=InvalidUploadTimeError("foo 1.0 has a naive upload time"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out)
        assert "naive upload time" in capsys.readouterr().err

    def test_config_error_during_resolve_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ConfigError raised by the universal resolve exits 1 cleanly."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=ConfigError(
                    "[tool.nab].conflicts names extra 'gpuu', which the project"
                    " does not declare in [project.optional-dependencies]"
                ),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out)
        err = capsys.readouterr().err
        assert "in [tool.nab]:" in err
        assert "gpuu" in err

    def test_not_implemented_vcs_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An admitted VCS dep hits the unimplemented universal path and exits 1, not a traceback."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=NotImplementedError("resolver path is not implemented"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out)
        assert "resolver path is not implemented" in capsys.readouterr().err

    def test_string_project_table_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-table [project] in universal mode exits 1, not a traceback."""
        pyproject = _make_pyproject(
            tmp_path,
            'project = "hello"\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=tmp_path / "pylock.toml")
        err = capsys.readouterr().err
        assert "[project] must be a table" in err
        assert "Traceback" not in err

    def test_conflicting_groups_exit_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two directly conflicting groups exit 1 with a message, not a traceback."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "probe"\nversion = "0.1.0"\ndependencies = []\n'
            "[dependency-groups]\n"
            'alpha = ["idna<3"]\n'
            'beta = ["idna>=3"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=Path("-"), groups=("alpha", "beta"), offline=True)

        err = capsys.readouterr().err
        assert "resolution failed:" in err
        assert "'alpha' and 'beta' conflict on 'idna'" in err
        assert "Traceback" not in err

    def test_untileable_micro_marker_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A consulted marker that cannot tile a minor exits 1, not a traceback."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "probe"\nversion = "0.1.0"\n'
            "dependencies = [\"somepkg; python_full_version in '3.12.1 3.12.2'\"]\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.12,<3.13"\n'
            'platforms = ["linux_x86_64"]\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=Path("-"), offline=True)

        err = capsys.readouterr().err
        assert "cannot lock: consulted marker clause" in err
        assert "cannot tile the py312-linux_x86_64 minor interval" in err
        assert "Traceback" not in err

    def test_wildcard_micro_marker_exits_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``< "3.12.*"`` is a valid marker whose specifier form is not.

        PEP 508 accepts the literal and PEP 440 accepts a ``.*`` suffix only
        under ``==``/``!=``, so the clause parses and the specifier does not.
        """
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "probe"\nversion = "0.1.0"\n'
            "dependencies = [\"somepkg; python_full_version < '3.12.*'\"]\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.12,<3.13"\n'
            'platforms = ["linux_x86_64"]\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=Path("-"), offline=True)

        err = capsys.readouterr().err
        assert "cannot lock: consulted marker clause" in err
        assert 'python_full_version < "3.12.*"' in err
        assert "Traceback" not in err

    def test_pylock_writes_universal_lock(self, tmp_path: Path) -> None:
        """Universal + pylock format runs the real merge + write pipeline."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_universal_result(success=True),
        ):
            lock(pyproject, output=out)
        text = out.read_text()
        assert 'lock-version = "1.0"' in text
        assert 'name = "foo"' in text

    def test_full_disk_keeps_committed_requirements(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        cap_writes: Callable[[int], AbstractContextManager[None]],
    ) -> None:
        """A requirements write that runs out of space keeps the committed file."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        committed = b"foo==0.9\n"
        out.write_bytes(committed)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            cap_writes(4),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=out, format="requirements")
        assert "cannot write output" in capsys.readouterr().err
        assert out.read_bytes() == committed

    def test_default_groups_from_config_not_cli_groups(self, tmp_path: Path) -> None:
        """Universal pylock records ``default-groups`` from config, not ``--groups``."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            "[dependency-groups]\ndev = []\ntest = []\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            'default-groups = ["dev"]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n',
        )
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_universal_result(success=True),
        ):
            lock(pyproject, output=out, groups=("test",))
        pylock = Pylock.from_dict(tomli.loads(out.read_text()))
        assert pylock.dependency_groups == ["test"]
        assert pylock.default_groups == ["dev"]

    def test_offline_and_http_backend_passed_to_universal(self, tmp_path: Path) -> None:
        """--http-backend and --offline reach resolve_for_targets."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_universal_result(success=True),
        ) as mock_resolve:
            lock(pyproject, output=out, http_backend="urllib3", offline=True)
        assert mock_resolve.call_args.kwargs["offline"] is True
        # The transport is the second positional argument.
        assert mock_resolve.call_args.args[1] is not None

    def test_requirements_with_hashes_single_tuple_to_file(
        self, tmp_path: Path
    ) -> None:
        """Single-tuple matrix + fixed output path writes that tuple's pins."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_universal_result(success=True),
        ):
            lock(pyproject, format="requirements", output=out)
        text = out.read_text()
        # No multi-block header; just the pins for the one tuple.
        assert "foo==1.0" in text
        assert "--hash=sha256:" in text
        assert "# py311-linux_x86_64" not in text

    def test_pylock_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Universal + pylock + --output - writes lock text to stdout."""
        pyproject = _universal_pyproject(tmp_path)
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_universal_result(success=True),
        ):
            lock(pyproject, output=Path("-"))
        out = capsys.readouterr().out
        assert 'lock-version = "1.0"' in out

    def test_pylock_default_output_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Universal + pylock with no --output writes pylock.toml in cwd."""
        monkeypatch.chdir(tmp_path)
        pyproject = _universal_pyproject(tmp_path)
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_universal_result(success=True),
        ):
            lock(pyproject)
        assert (tmp_path / "pylock.toml").exists()

    def test_pylock_missing_hash_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A MissingHashError during universal pylock surfaces as exit 1."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            patch("nab._lock.write_lock", side_effect=MissingHashError("no hash")),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        assert "cannot lock" in capsys.readouterr().err

    def test_pylock_disjointness_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A DisjointnessError during universal pylock surfaces as exit 1.

        The conflict hint carried by the error reaches the user as a
        clean ``Error: ...`` line instead of a traceback.
        """
        pyproject = _universal_pyproject(tmp_path)
        hint = (
            "foo: 2 entries fire under env='py311-linux_x86_64'. If these are"
            " intentionally mutually exclusive, declare them in"
            " [tool.nab].conflicts so the colliding context is pruned"
        )
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            patch("nab._lock.write_lock", side_effect=DisjointnessError(hint)),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        err = capsys.readouterr().err
        assert f"error: {hint}\n" in err

    def test_base_group_naming_a_declared_group_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The configured name is already a group of the project's own.

        Refused as the config is read, so nothing here has to stand in
        for a resolve.
        """
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            '[dependency-groups]\ndev = ["foo"]\ndefault = ["foo"]\n'
            '[tool.nab]\nbase-group = "default"\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=tmp_path / "pylock.toml", groups=("dev",))
        err = capsys.readouterr().err
        assert "error: in [tool.nab]: base-group 'default' and" in err
        assert "--project-base-group" not in err
        assert "Traceback" not in err

    def test_the_flag_naming_a_declared_group_names_the_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The project file may not hold the value the run is refusing."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n[dependency-groups]\ndev = ["foo"]\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(
                pyproject,
                output=tmp_path / "pylock.toml",
                groups=("dev",),
                project_base_group="dev",
            )

        assert "--project-base-group 'dev' and" in capsys.readouterr().err

    def test_the_build_group_flag_names_the_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The project file may not hold the value the run is refusing."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            '[build-system]\nrequires = ["foo"]\n'
            '[dependency-groups]\ndev = ["foo"]\n'
            '[tool.nab]\nbase-group = "main"\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(
                pyproject,
                output=tmp_path / "pylock.toml",
                project_build_group="dev",
            )

        assert "--project-build-group 'dev' and" in capsys.readouterr().err

    def test_pylock_divergent_base_dep_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A DivergentBaseDependencyError during universal pylock
        surfaces as exit 1 with a clean ``Error: ...`` line instead of
        a traceback."""
        pyproject = _universal_pyproject(tmp_path)
        message = (
            "shared: the conflict forks of one environment pin this base"
            " dependency differently (cpu -> 1.0, gpu -> 2.0)"
        )
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            patch(
                "nab._lock.write_lock",
                side_effect=DivergentBaseDependencyError(message),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        assert f"error: {message}\n" in capsys.readouterr().err

    def test_universal_lock_collision_without_conflict_shows_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """End-to-end: two conflict-fork pins for the same package with no
        ``[tool.nab].conflicts`` declared.  The real validator fires
        :class:`DisjointnessError` and the hint reaches stderr, so a
        rename of the hint text breaks this test."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n',
        )

        tuples = tuple(
            _target(selection=(("extra", member),)) for member in ("cpu", "gpu")
        )
        result = ResolveResult(
            targets=tuples,
            target_results=[
                _resolved(tup, {"foo": V(version)})
                for tup, version in zip(tuples, ("1.0", "2.0"), strict=True)
            ],
        )

        with (
            patch("nab.cli.resolve_for_targets", return_value=result),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(
                pyproject,
                output=tmp_path / "pylock.toml",
                extras=("cpu", "gpu"),
            )
        err = capsys.readouterr().err
        assert "error:" in err
        assert "foo" in err
        assert "[tool.nab].conflicts" in err

    def test_unsupported_vcs_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A direct-URL requirement refused in universal mode exits cleanly."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=UnsupportedVcsError("refusing direct-URL requirement"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "refusing direct-URL requirement" in err

    def test_per_tuple_pins_to_stdout_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Universal + requirements-without-hashes prints per-tuple blocks.

        A matrix of several tuples has no one installable file, so the
        stdout dump separates them with ``# label`` headers.
        """
        pyproject = _universal_pyproject(tmp_path)
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_multi_tuple_universal_result(),
        ):
            lock(pyproject, format="requirements-without-hashes")
        captured = capsys.readouterr()
        assert "experimental" in captured.err
        assert "lockfile format" in captured.err
        assert "resolver loop" not in captured.err

        assert captured.out == (
            "# py311-linux_x86_64\nbar==2.0\nfoo==1.0\n\n"
            "# py312-linux_x86_64\nfoo==1.0\n"
        )

    def test_per_tuple_pins_to_explicit_file_single_tuple(self, tmp_path: Path) -> None:
        """Single-tuple matrix + fixed path: just the pins, no header.

        Same shape as a single-environment requirements file.
        """
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pins.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_universal_result(success=True),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        text = out.read_text()
        assert "foo==1.0" in text
        assert "# py311-linux_x86_64" not in text

    def test_per_tuple_pins_to_dash_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Universal + --output - writes per-tuple blocks to stdout."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            patch(
                "nab._lock.build_lock_input",
                return_value=MagicMock(name="LockInput"),
            ),
            patch(
                "nab._lock.write_requirements_without_hashes",
                return_value="# py311-linux_x86_64\nfoo==1.0\n",
            ),
        ):
            lock(
                pyproject,
                format="requirements-without-hashes",
                output=Path("-"),
            )
        assert "# py311-linux_x86_64" in capsys.readouterr().out

    def test_failed_tuple_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failed tuple makes the run exit 1."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_multi_tuple_failed_result(ResolutionError("conflict")),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        err = capsys.readouterr().err
        assert "FAILED" in err
        assert "#   ResolutionError: conflict" in err

    def test_failed_tuple_multi_line_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Multi-line errors render as one comment line per source line."""
        pyproject = _universal_pyproject(tmp_path)
        multi = "first line\nsecond line\nthird line"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_multi_tuple_failed_result(ResolutionError(multi)),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        err = capsys.readouterr().err
        assert "#   ResolutionError: first line" in err
        assert "#   second line" in err
        assert "#   third line" in err

    def test_failed_tuple_no_error_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failed tuple with no error string still emits the FAILED line."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_multi_tuple_failed_result(None),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        assert "FAILED" in capsys.readouterr().err

    def test_single_tuple_matrix_failure_is_single_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A one-tuple matrix failure reads as one error, not a block.

        A single-tuple matrix pins under one target on every surface (see
        ``test_per_tuple_pins_to_explicit_file_single_tuple``), so its
        failure is the run's error, matching a single-environment resolve.
        """
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(
                    success=False, error=ResolutionError("conflict")
                ),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        err = capsys.readouterr().err
        assert "resolution failed: conflict" in err
        assert "FAILED" not in err

    def test_missing_dependencies_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """KeyError surfaces as the standard missing-deps message."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=KeyError("dependencies"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        assert "no [project].dependencies" in capsys.readouterr().err

    def test_lookup_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """LookupError (e.g. unknown group) exits 1 with the message."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=LookupError("unknown group 'ghost'"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        assert "unknown group" in capsys.readouterr().err

    def test_http_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An index HTTP failure during a tuple resolve exits 1, not a traceback."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=HttpError("GET https://pypi.org/simple/foo/ failed: 503"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "503" in err

    def test_missing_extra_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A root extra the package does not declare exits 1, not a traceback."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=MissingExtraError(
                    "foo==1.0 does not provide extra 'nonexistent'"
                ),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        err = capsys.readouterr().err
        assert "does not provide extra 'nonexistent'" in err
        assert "Traceback" not in err

    def test_malformed_simple_response_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A malformed Simple-API listing exits 1 cleanly, not a raw traceback."""
        from nab_index.client import _parse_files

        with pytest.raises(HttpError, match="malformed Simple-API") as caught:
            _parse_files(b"<!doctype html>", "https://pypi.org/simple/", "foo")

        pyproject = _universal_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_for_targets", side_effect=caught.value),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        err = capsys.readouterr().err
        assert "cannot lock" in err
        assert "malformed Simple-API" in err

    def test_requirements_missing_hash_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``MissingHashError`` raised by the requirements writer exits 1."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            patch(
                "nab._lock.write_requirements_with_hashes",
                side_effect=MissingHashError("no hash"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements")
        assert "cannot lock" in capsys.readouterr().err

    def test_print_blocks_includes_succeeded_tuples(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When some tuples succeed and one fails, both render as blocks.

        ``_report_failures`` runs whenever any tuple failed; it must
        print the failing tuple's ``# label: FAILED`` block AND each
        successful tuple's ``# label`` + pins block.
        """
        pyproject = _universal_pyproject(tmp_path)
        ok_tuple = _target()
        bad_tuple = _target(platform_id="windows_amd64")
        mixed = ResolveResult(
            targets=(ok_tuple, bad_tuple),
            target_results=[
                _resolved(ok_tuple, {"foo": V("1.0")}),
                _failed(bad_tuple, ResolutionError("conflict")),
            ],
        )
        with (
            patch("nab.cli.resolve_for_targets", return_value=mixed),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        err = capsys.readouterr().err
        assert "# py311-linux_x86_64" in err
        assert "foo==1.0" in err
        assert "# py311-windows_amd64: FAILED" in err

    def test_print_blocks_surfaces_base_pass_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # All per-tuple pins succeeded, the first env's base pass
        # succeeded, and the second env's base pass failed: only the
        # failed one renders a ``base/<label>: FAILED`` block.  One
        # tuple has to fail so ``_report_failures`` runs at all.
        pyproject = _universal_pyproject(tmp_path)
        env_a = _target()
        env_b = _target("3.12")
        mixed = ResolveResult(
            targets=(env_a, env_b),
            target_results=[
                _resolved(env_a, {"foo": V("1.0")}),
                _failed(env_b, ResolutionError("conflict")),
            ],
            base_results=[
                _resolved(env_a, {"foo": V("1.0")}),
                _failed(
                    env_b, ResolutionError("base unresolvable\nDiagnostics: missing")
                ),
            ],
        )
        with (
            patch("nab.cli.resolve_for_targets", return_value=mixed),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        err = capsys.readouterr().err
        assert "foo==1.0" in err
        # The succeeded base pass contributes no block: only the failed
        # one renders, and the per-tuple labels stay distinct.
        assert "# base/py311-linux_x86_64: FAILED" not in err
        assert "# base/py312-linux_x86_64: FAILED" in err
        assert "#   ResolutionError: base unresolvable" in err
        assert "#   Diagnostics: missing" in err

    def _cutoff_refused(self, tmp_path: Path, platforms: str) -> Path:
        """A universal project whose only candidate the upload cutoff refuses."""
        return _make_pyproject(
            tmp_path,
            '[project]\nname = "proj"\nversion = "0"\ndependencies = ["foo"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'uploaded-prior-to = "2026-05-01T00:00:00Z"\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            f"platforms = [{platforms}]\n",
        )

    def _lock_against_a_refused_listing(self, pyproject: Path) -> None:
        """Run a real resolve over one wheel uploaded after the cutoff."""
        coordinator = make_coordinator(
            [
                WheelFile(
                    filename="foo-1.0-py3-none-any.whl",
                    url="https://example.com/foo-1.0-py3-none-any.whl",
                    version="1.0",
                    requires_python=None,
                    has_metadata=True,
                    upload_time="2030-01-01T00:00:00Z",
                )
            ],
            package="foo",
        )
        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(SystemExit, match="1"):
                lock(pyproject, format="requirements-without-hashes", cache=False)

    def test_a_diagnostics_note_reaches_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A one-tuple failure prints the body the provider built, unprefixed.

        The sentence and its indented ``note:`` continuation come from a real
        resolve, so this is the text a user reads, not a fixture of it.
        """
        self._lock_against_a_refused_listing(
            self._cutoff_refused(tmp_path, '"linux_x86_64"')
        )

        assert capsys.readouterr().err.endswith(
            "\nDiagnostics:\n"
            "  - foo: found on index but no distribution is compatible: the"
            " uploaded-prior-to cutoff 2026-05-01T00:00:00+00:00 excluded 1 file"
            " uploaded at 2030-01-01T00:00:00Z (1.0); no sdist is available to"
            " build from\n"
            "    note: the project-level uploaded-prior-to set that cutoff;"
            ' uploaded-prior-to = false under [tool.nab.packages."foo"] lifts it'
            " for this package\n"
        )

    def test_a_diagnostics_note_survives_the_per_tuple_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every line of the body takes the block's comment prefix.

        ``_error_lines`` prefixes each line, so the note arrives indented
        under the package it belongs to rather than at the block's own
        indent.
        """
        self._lock_against_a_refused_listing(
            self._cutoff_refused(tmp_path, '"linux_x86_64", "windows_amd64"')
        )

        err = capsys.readouterr().err
        assert "# py311-linux_x86_64: FAILED" in err
        assert "#   Diagnostics:" in err
        assert (
            "#     - foo: found on index but no distribution is compatible: the"
            " uploaded-prior-to cutoff 2026-05-01T00:00:00+00:00 excluded 1 file"
            " uploaded at 2030-01-01T00:00:00Z (1.0); no sdist is available to"
            " build from"
        ) in err
        assert (
            "#       note: the project-level uploaded-prior-to set that cutoff;"
            ' uploaded-prior-to = false under [tool.nab.packages."foo"] lifts it'
            " for this package"
        ) in err

    def test_template_writes_one_file_per_tuple(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each file holds its own tuple's pins, and its summary line names it."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "constraints-{python_version}.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_multi_tuple_universal_result(),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)

        py311 = tmp_path / "constraints-3.11.txt"
        py312 = tmp_path / "constraints-3.12.txt"
        assert py311.read_text() == "bar==2.0\nfoo==1.0\n"
        assert py312.read_text() == "foo==1.0\n"
        assert not (tmp_path / "constraints-{python_version}.txt").exists()

        err = capsys.readouterr().err
        assert f"Wrote {py311} (2 packages, tuple py311-linux_x86_64)" in err
        assert f"Wrote {py312} (1 packages, tuple py312-linux_x86_64)" in err

    def test_template_with_platform_id(self, tmp_path: Path) -> None:
        """``{platform_id}`` is also a valid template variable."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "constraints-{python_version}-{platform_id}.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_multi_tuple_universal_result(),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        py311 = tmp_path / "constraints-3.11-linux_x86_64.txt"
        py312 = tmp_path / "constraints-3.12-linux_x86_64.txt"
        assert py311.read_text() == "bar==2.0\nfoo==1.0\n"
        assert py312.read_text() == "foo==1.0\n"

    def test_template_with_hashes(self, tmp_path: Path) -> None:
        """With hashes, each file still pins only its own tuple."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_multi_tuple_universal_result(),
        ):
            lock(pyproject, format="requirements", output=out)

        py311_text = (tmp_path / "req-3.11.txt").read_text()
        assert "foo==1.0" in py311_text
        assert "bar==2.0" in py311_text
        assert "--hash=sha256:" in py311_text

        py312_text = (tmp_path / "req-3.12.txt").read_text()
        assert "foo==1.0" in py312_text
        assert "bar==" not in py312_text

    def test_multi_tuple_without_template_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Multiple tuples + plain --output fails with a clear message.

        The two tuples differ only in their Python, so the remedy names
        ``{python_version}`` and not the variables that would add nothing.
        """
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_multi_tuple_universal_result(),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "produced 2 tuples" in err
        assert "{python_version}" in err
        assert "{platform_id}" not in err
        # No partial output written.
        assert not out.exists()

    def test_partial_template_collision_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A template missing ``{platform_id}`` on a multi-platform matrix exits 1."""
        tuples = tuple(
            _target(platform_id=platform_id)
            for platform_id in ("linux_x86_64", "windows_amd64")
        )
        result = ResolveResult(
            targets=tuples,
            target_results=[_resolved(tup, {"foo": V("1.0")}) for tup in tuples],
        )
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "constraints-{python_version}.txt"
        with (
            patch("nab.cli.resolve_for_targets", return_value=result),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "both map to" in err
        assert "{platform_id}" in err
        assert not (tmp_path / "constraints-3.11.txt").exists()

    def test_template_unknown_placeholder_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A stray placeholder in --output exits 1 instead of a traceback."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}-{foo}.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "unknown template placeholder" in err
        assert "{foo}" in err

    def test_template_malformed_braces_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unbalanced brace in --output exits 1 instead of a traceback."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}-{.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        assert "not a valid template" in capsys.readouterr().err

    def test_template_invalid_format_spec_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bad format spec on a tuple var exits 1 instead of a raw ValueError."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}-{platform_id:d}.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        assert "not a valid template" in capsys.readouterr().err
        assert not (tmp_path / "req-3.11-linux_x86_64.txt").exists()

    def test_template_conversion_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``!r`` conversion exits 1 instead of writing a quoted filename."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{platform_id}-{python_version!r}.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        assert "not a valid template" in capsys.readouterr().err
        assert list(tmp_path.glob("req-*.txt")) == []

    def test_template_with_one_tuple_writes_one_file(self, tmp_path: Path) -> None:
        """A template with a single-tuple matrix still works."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "constraints-{python_version}.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_universal_result(success=True),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        assert (tmp_path / "constraints-3.11.txt").read_text().strip() == "foo==1.0"

    def test_template_with_selection_writes_one_file_per_fork(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``{selection}`` in --output gives each conflict fork its own pins."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{selection}.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_forked_universal_result(),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)

        cpu = tmp_path / "req-extra-cpu.txt"
        gpu = tmp_path / "req-extra-gpu.txt"
        assert cpu.read_text() == "bar==2.0\nfoo==1.0\n"
        assert gpu.read_text() == "foo==1.0\n"

        err = capsys.readouterr().err
        assert f"Wrote {cpu} (2 packages, tuple py311-linux_x86_64-extra-cpu)" in err
        assert f"Wrote {gpu} (1 packages, tuple py311-linux_x86_64-extra-gpu)" in err

    def test_forked_collision_points_at_selection(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two forks of one tuple collide; the message names ``{selection}``."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}-{platform_id}.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_forked_universal_result(),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "both map to" in err
        assert "{selection}" in err
        assert not (tmp_path / "req-3.11-linux_x86_64.txt").exists()

    def test_forked_plain_output_points_at_selection(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A plain --output over a forked resolve names ``{selection}`` only."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_forked_universal_result(),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "2 tuples" in err
        assert "{selection}" in err
        assert "{python_version}" not in err
        assert not out.exists()

    def test_collision_with_no_variable_to_add_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Tuples differing only in a knob no variable names cannot be written.

        A musl and a glibc target share one ``platform_id`` and one
        ``python_version``, so no template can tell them apart.
        """
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}-{platform_id}-{selection}.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_two_libc_universal_result(),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "both map to" in err
        assert "tells them apart" in err
        assert list(tmp_path.glob("req-*.txt")) == []

    def test_plain_output_with_no_variable_to_add_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A plain --output over indistinguishable tuples says no variable helps."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_two_libc_universal_result(),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "tells them apart" in err
        assert not out.exists()

    def test_plain_output_offers_no_template_that_would_collide(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A variable that varies but leaves a collision is not offered as a fix."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_two_implementation_universal_result(),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "4 tuples" in err
        assert "tells them apart" in err
        assert "Emit pylock output instead" in err
        assert "{python_version}" not in err
        assert not out.exists()

    def test_collision_offers_no_template_that_would_collide(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A variable that separates the colliding pair only is not offered."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{platform_id}.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_mixed_implementation_universal_result(),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "both map to" in err
        assert "tells them apart" in err
        assert "Emit pylock output instead" in err
        assert "{python_version}" not in err
        assert list(tmp_path.glob("req-*.txt")) == []

    def test_template_missing_hash_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A MissingHashError during a per-tuple write surfaces as exit 1."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}.txt"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            patch(
                "nab._lock.write_requirements_with_hashes",
                side_effect=MissingHashError("no hash"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements", output=out)
        assert "cannot lock" in capsys.readouterr().err

    def test_template_missing_hash_writes_no_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refusal on the second tuple leaves the first tuple's file alone.

        The hashless pin is reachable only from the 3.12 tuple, so the run
        must refuse before req-3.11.txt has been rewritten.
        """
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}.txt"
        first = tmp_path / "req-3.11.txt"
        first.write_text("stale==0.1\n")
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_late_hashless_universal_result(),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements", output=out)
        err = capsys.readouterr().err
        assert "no acceptable hash" in err
        assert "Wrote" not in err
        assert first.read_text() == "stale==0.1\n"
        assert not (tmp_path / "req-3.12.txt").exists()

    def test_template_unwritable_path_writes_no_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unwritable path on the second tuple leaves the first alone.

        A directory sits where req-3.12.txt would go, so the first tuple's
        file keeps its contents and no stage is left behind.
        """
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}.txt"
        first = tmp_path / "req-3.11.txt"
        first.write_text("stale==0.1\n")
        (tmp_path / "req-3.12.txt").mkdir()
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_multi_tuple_universal_result(),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "cannot write output" in err
        assert "Wrote" not in err
        assert first.read_text() == "stale==0.1\n"
        assert not list(tmp_path.glob("*.tmp"))

    def test_template_files_keep_the_permissions_a_write_would_give(
        self, tmp_path: Path
    ) -> None:
        """Staged files carry the mode a direct write would have left."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}.txt"
        first = tmp_path / "req-3.11.txt"
        first.write_text("stale==0.1\n")
        before = stat.S_IMODE(first.stat().st_mode)
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_multi_tuple_universal_result(),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        assert first.read_text() == "bar==2.0\nfoo==1.0\n"
        assert stat.S_IMODE(first.stat().st_mode) == before
        fresh = stat.S_IMODE((tmp_path / "req-3.12.txt").stat().st_mode)
        assert fresh == before

    def test_failed_tuples_are_skipped_in_template_emit(self, tmp_path: Path) -> None:
        """Only successful tuples produce a file."""
        pyproject = _universal_pyproject(tmp_path)
        # Build a mixed matrix: 3.11 succeeds, 3.12 fails.
        good_tup = _target()
        bad_tup = _target("3.12")
        mixed = ResolveResult(
            targets=(good_tup, bad_tup),
            target_results=[
                _resolved(good_tup, {"foo": V("1.0")}),
                _failed(bad_tup, ResolutionError("boom")),
            ],
        )
        out = tmp_path / "constraints-{python_version}.txt"
        with (
            patch("nab.cli.resolve_for_targets", return_value=mixed),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        # A failed result triggers the stdout-block emit path, so neither
        # file is written.
        assert not (tmp_path / "constraints-3.11.txt").exists()
        assert not (tmp_path / "constraints-3.12.txt").exists()


class TestNoEmitWorkspace:
    """``--no-emit-workspace`` drops workspace pins from the lockfile."""

    @staticmethod
    def _alpha_and_foo_result() -> ResolveResult:
        """A single-environment result with a workspace pin (alpha) and foo."""
        return _stub_resolve_result(pins={"alpha": V("0"), "foo": V("1.0")})

    @staticmethod
    def _alpha_and_foo_universal() -> ResolveResult:
        """A universal result with alpha + foo on a single tuple."""
        tup = _target()
        return ResolveResult(
            targets=(tup,),
            target_results=[_resolved(tup, {"alpha": V("0"), "foo": V("1.0")})],
        )

    def test_specific_pylock_drops_workspace_pin(self, tmp_path: Path) -> None:
        """Specific mode + pylock with the flag set drops the workspace pin."""
        pyproject = _workspace_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=self._alpha_and_foo_result()
        ):
            lock(pyproject, output=out, no_emit_workspace=True)
        text = out.read_text()
        assert 'name = "foo"' in text
        assert 'name = "alpha"' not in text

    def test_specific_pylock_flag_off_keeps_workspace_pin(self, tmp_path: Path) -> None:
        """Without the flag, workspace pins remain in the lockfile."""
        pyproject = _workspace_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=self._alpha_and_foo_result()
        ):
            lock(pyproject, output=out)
        text = out.read_text()
        assert 'name = "alpha"' in text

    def test_specific_count_message_reflects_filter(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The ``Wrote ... (N packages)`` count drops by the filtered amount."""
        pyproject = _workspace_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=self._alpha_and_foo_result()
        ):
            lock(pyproject, output=out, no_emit_workspace=True)
        assert "(1 packages)" in capsys.readouterr().err

    def test_specific_requirements_drops_workspace_pin(self, tmp_path: Path) -> None:
        """Requirements format also honours --no-emit-workspace."""
        pyproject = _workspace_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with patch(
            "nab.cli.resolve_for_targets", return_value=self._alpha_and_foo_result()
        ):
            lock(pyproject, output=out, format="requirements", no_emit_workspace=True)
        text = out.read_text()
        assert "foo==1.0" in text
        assert "alpha==" not in text

    def test_universal_pylock_drops_workspace_pin(self, tmp_path: Path) -> None:
        """Universal pylock drops workspace pins from the merged output."""
        pyproject = _workspace_pyproject(tmp_path, universal=True)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=self._alpha_and_foo_universal(),
        ):
            lock(pyproject, output=out, no_emit_workspace=True)
        text = out.read_text()
        assert 'name = "foo"' in text
        assert 'name = "alpha"' not in text

    def test_universal_requirements_stdout_drops_workspace_pin(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Universal requirements + stdout drops workspace pin lines."""
        pyproject = _workspace_pyproject(tmp_path, universal=True)
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=self._alpha_and_foo_universal(),
        ):
            lock(
                pyproject,
                output=Path("-"),
                format="requirements-without-hashes",
                no_emit_workspace=True,
            )
        out = capsys.readouterr().out
        assert "foo==1.0" in out
        assert "alpha==" not in out

    def test_universal_requirements_template_drops_workspace_pin(
        self, tmp_path: Path
    ) -> None:
        """Universal requirements + templated --output drops workspace pins."""
        pyproject = _workspace_pyproject(tmp_path, universal=True)
        out = tmp_path / "constraints-{python_version}.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=self._alpha_and_foo_universal(),
        ):
            lock(
                pyproject,
                output=out,
                format="requirements-without-hashes",
                no_emit_workspace=True,
            )
        text = (tmp_path / "constraints-3.11.txt").read_text()
        assert "foo==1.0" in text
        assert "alpha==" not in text

    def test_universal_requirements_single_tuple_file_drops_workspace_pin(
        self, tmp_path: Path
    ) -> None:
        """Single-tuple universal + plain --output path filters workspace pins."""
        pyproject = _workspace_pyproject(tmp_path, universal=True)
        out = tmp_path / "requirements.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=self._alpha_and_foo_universal(),
        ):
            lock(
                pyproject,
                output=out,
                format="requirements-without-hashes",
                no_emit_workspace=True,
            )
        text = out.read_text()
        assert "foo==1.0" in text
        assert "alpha==" not in text

    def test_flag_without_workspace_is_a_noop(self, tmp_path: Path) -> None:
        """No workspace declared: --no-emit-workspace leaves pins intact."""
        # _make_pyproject builds a plain project with no [tool.nab.workspace].
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=self._alpha_and_foo_result()
        ):
            lock(pyproject, output=out, no_emit_workspace=True)
        text = out.read_text()
        assert 'name = "alpha"' in text
        assert 'name = "foo"' in text

    def test_specific_pylock_drops_dependency_edge_to_workspace(
        self, tmp_path: Path
    ) -> None:
        """A retained package keeps no forward edge to the dropped member."""
        target = ResolveTarget.for_host()
        pins = {"alpha": V("0"), "foo": V("1.0")}
        result = ResolveResult(
            targets=(target,),
            target_results=[
                TargetResult(
                    target=target,
                    success=True,
                    pins=pins,
                    lock=_target_lock(target, pins, {"foo": ("alpha",)}),
                )
            ],
        )
        pyproject = _workspace_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch("nab.cli.resolve_for_targets", return_value=result):
            lock(pyproject, output=out, no_emit_workspace=True)
        text = out.read_text()
        assert 'name = "foo"' in text
        # alpha's [[packages]] row and the dangling forward edge are both gone.
        assert 'name = "alpha"' not in text


class TestRelockDiffSummary:
    """``_emit`` reports what changed against the prior pylock."""

    def test_first_lock_prints_plain_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "pylock.toml"
        _emit(_stub_lock_input({"foo": V("1.0")}), format="pylock", output=out)
        err = capsys.readouterr().err
        assert err.strip().endswith("(1 packages)")

    def test_relock_reports_added_upgraded_removed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "pylock.toml"
        _emit(
            _stub_lock_input({"foo": V("1.0"), "bar": V("1.0")}),
            format="pylock",
            output=out,
        )
        capsys.readouterr()
        # foo upgraded 1.0 -> 2.0, bar removed, baz added.
        _emit(
            _stub_lock_input({"foo": V("2.0"), "baz": V("1.0")}),
            format="pylock",
            output=out,
        )
        err = capsys.readouterr().err.strip()
        assert err.endswith("(2 packages: 1 added, 1 upgraded, 1 removed)")

    def test_relock_reports_downgrade(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "pylock.toml"
        _emit(_stub_lock_input({"foo": V("2.0")}), format="pylock", output=out)
        capsys.readouterr()
        _emit(_stub_lock_input({"foo": V("1.0")}), format="pylock", output=out)
        assert capsys.readouterr().err.strip().endswith("(1 packages: 1 downgraded)")

    def test_relock_unchanged_prints_plain_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A re-lock with identical pins prints no diff suffix."""
        out = tmp_path / "pylock.toml"
        for _ in range(2):
            _emit(_stub_lock_input({"foo": V("1.0")}), format="pylock", output=out)
        assert capsys.readouterr().err.strip().endswith("(1 packages)")

    def test_relock_unchanged_with_local_pin_prints_plain_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A local pin has no recorded version, so an unchanged relock
        must not count it as added."""
        out = tmp_path / "pylock.toml"
        src = tmp_path / "alpha"
        src.mkdir()
        lock_input = _lock_input(
            {
                "foo": _foo_index_pin("1.0", "foo"),
                "alpha": LocalPin(name="alpha", version="0", path=str(src)),
            }
        )
        for _ in range(2):
            _emit(lock_input, format="pylock", output=out)
        assert capsys.readouterr().err.strip().endswith("(2 packages)")

    def test_relock_unchanged_with_archive_pin_prints_plain_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An archive pin records a version, so an unchanged relock must
        diff it (not count it as removed)."""
        out = tmp_path / "pylock.toml"
        lock_input = _lock_input(
            {
                "foo": ArchivePin(
                    name="foo",
                    version="1.0",
                    url="https://ex.com/foo-1.0.tar.gz",
                    hashes=(("sha256", "e" * 64),),
                ),
            }
        )
        for _ in range(2):
            _emit(lock_input, format="pylock", output=out)
        assert capsys.readouterr().err.strip().endswith("(1 packages)")

    def test_unparseable_prior_falls_back_to_plain_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "pylock.toml"
        out.write_text("this is not valid toml === {[\n")
        _emit(_stub_lock_input({"foo": V("1.0")}), format="pylock", output=out)
        assert capsys.readouterr().err.strip().endswith("(1 packages)")

    def test_stdout_emits_no_diff(self, capsys: pytest.CaptureFixture[str]) -> None:
        _emit(_stub_lock_input({"foo": V("1.0")}), format="pylock", output=Path("-"))
        captured = capsys.readouterr()
        assert "added" not in captured.err
        assert "packages" not in captured.err

    def test_requirements_format_emits_no_diff(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A requirements re-lock keeps the plain line; only pylock diffs."""
        out = tmp_path / "requirements.txt"
        for _ in range(2):
            _emit(
                _stub_lock_input({"foo": V("1.0")}), format="requirements", output=out
            )
        assert capsys.readouterr().err.strip().endswith("(1 packages)")


class TestPylockOutputNameValidation:
    """``--output`` is validated against the PEP 751 file-name rule."""

    def test_specific_rejects_hyphen_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=tmp_path / "pylock-dev.toml")
        err = capsys.readouterr().err
        assert "PEP 751" in err
        assert "pylock.dev.toml" in err

    def test_universal_rejects_hyphen_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=True),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock-dev.toml")
        assert "PEP 751" in capsys.readouterr().err

    def test_specific_accepts_named_pylock(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.dev.toml"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out)
        assert out.exists()

    def test_unparseable_name_suggests_pylock_toml(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A name with no recoverable label suggests the bare ``pylock.toml``."""
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=tmp_path / "lock.toml")
        assert "pylock.toml" in capsys.readouterr().err

    @pytest.mark.parametrize("raw", [".", "/", ""])
    def test_empty_name_rejected_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], raw: str
    ) -> None:
        """A directory-like ``--output`` has no file name, so it exits 1."""
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=Path(raw))
        err = capsys.readouterr().err
        assert "names a directory, not a file" in err
        assert "pylock.toml" in err

    @pytest.mark.parametrize("raw", ["sub/-", "a/b/-"])
    def test_hyphen_name_rejected_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], raw: str
    ) -> None:
        """A non-stdout ``-`` component whose dotted form is invalid still exits 1."""
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=tmp_path / raw)
        err = capsys.readouterr().err
        assert "PEP 751" in err
        assert "pylock.toml" in err

    def test_stdout_skips_validation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=Path("-"))
        assert 'lock-version = "1.0"' in capsys.readouterr().out

    def test_requirements_format_skips_validation(self, tmp_path: Path) -> None:
        """A non-pylock format is free to use any output name."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "constraints.txt"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out, format="requirements")
        assert out.exists()


class TestConfigErrors:
    """Errors in [tool.nab] surface as exit 1 with a clear message."""

    def test_invalid_config_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Bad TOML in [tool.nab] prints the parse error and exits 1."""
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            '[tool.nab]\ndist-policy = "wrong-value"\n',
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject)
        assert "in [tool.nab]" in capsys.readouterr().err

    def test_workspace_discovery_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A malformed workspace surfaces as exit 1 with a clear prefix."""
        # Workspace root with a glob in members; nab refuses globs.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "ws"\nversion = "0"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkg/*"]\n',
        )
        member_dir = tmp_path / "pkg" / "alpha"
        member_dir.mkdir(parents=True)
        member = member_dir / "pyproject.toml"
        member.write_text('[project]\nname = "alpha"\nversion = "0"\n')
        with pytest.raises(SystemExit, match="1"):
            lock(member)
        assert "workspace discovery error" in capsys.readouterr().err


class TestCliDocstringCommandModules:
    """``nab.cli``'s docstring names every module that registers a subcommand."""

    def test_docstring_names_every_command_module(self) -> None:
        # A command module only registers if cli.py imports it for the side
        # effect, so cli's own module bindings are the registration set.
        imported = {
            module.__name__
            for _, module in inspect.getmembers(cli, inspect.ismodule)
            if module.__name__.startswith("nab._")
        }
        docstring = cli.__doc__
        assert docstring is not None

        named = set(re.findall(r"nab\._\w+", docstring))

        assert named == imported, (
            f"docstring misses {imported - named}, "
            f"names unregistered {named - imported}"
        )


class TestDetermineLockAnchor:
    """``_determine_lock_anchor`` chooses between fresh and reused anchors."""

    _RECORDED = datetime(2024, 1, 1, tzinfo=timezone.utc)

    _ABSOLUTE = datetime(2026, 5, 1, tzinfo=timezone.utc)

    def _write_prior(self, target: Path) -> None:
        target.write_text(
            f"[tool.nab]\ncreated-at = {self._RECORDED.isoformat()}\n",
        )

    def test_upgrade_returns_fresh(self, tmp_path: Path) -> None:
        # Even with a prior pylock present, --upgrade re-anchors.
        target = tmp_path / "pylock.toml"
        self._write_prior(target)
        pyproject = _make_pyproject(tmp_path)
        anchor = _determine_lock_anchor(
            pyproject, output=target, format="pylock", upgrade=True
        )
        assert anchor != self._RECORDED
        assert (datetime.now(timezone.utc) - anchor).total_seconds() < 60

    def test_upgrade_notices_when_it_drops_a_cutoff(self, tmp_path: Path) -> None:
        # Re-anchoring over a reusable cutoff changes the resolve window, so
        # --upgrade names the cutoff it dropped instead of doing it silently.
        target = tmp_path / "pylock.toml"
        self._write_prior(target)
        pyproject = _make_pyproject(tmp_path)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _determine_lock_anchor(
                pyproject, output=target, format="pylock", upgrade=True
            )
        notice = err.getvalue()
        assert "--upgrade re-anchored" in notice
        assert self._RECORDED.isoformat() in notice

    def test_upgrade_silent_on_fresh_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With nothing to reuse, --upgrade has no window to drop, so no notice.
        monkeypatch.chdir(tmp_path)
        pyproject = _make_pyproject(tmp_path)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _determine_lock_anchor(
                pyproject, output=None, format="pylock", upgrade=True
            )
        assert err.getvalue() == ""

    def test_upgrade_silent_for_absolute_cutoff(self, tmp_path: Path) -> None:
        # An absolute uploaded-prior-to governs the resolve regardless of
        # --upgrade, so --upgrade does not drop it and prints no notice.
        target = tmp_path / "pylock.toml"
        self._write_prior(target)
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            f'[tool.nab]\nuploaded-prior-to = "{self._ABSOLUTE.isoformat()}"\n',
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            anchor = _determine_lock_anchor(
                pyproject, output=target, format="pylock", upgrade=True
            )
        assert err.getvalue() == ""
        assert (datetime.now(timezone.utc) - anchor).total_seconds() < 60

    def test_stdout_returns_fresh(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        anchor = _determine_lock_anchor(
            pyproject, output=Path("-"), format="pylock", upgrade=False
        )
        assert (datetime.now(timezone.utc) - anchor).total_seconds() < 60

    def test_non_pylock_returns_fresh(self, tmp_path: Path) -> None:
        # requirements format has no [tool.nab] block to read from.
        pyproject = _make_pyproject(tmp_path)
        anchor = _determine_lock_anchor(
            pyproject,
            output=tmp_path / "requirements.txt",
            format="requirements",
            upgrade=False,
        )
        assert (datetime.now(timezone.utc) - anchor).total_seconds() < 60

    def test_missing_lockfile_returns_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        pyproject = _make_pyproject(tmp_path)
        anchor = _determine_lock_anchor(
            pyproject, output=None, format="pylock", upgrade=False
        )
        assert (datetime.now(timezone.utc) - anchor).total_seconds() < 60

    def test_existing_lockfile_anchor_is_reused(self, tmp_path: Path) -> None:
        target = tmp_path / "pylock.toml"
        self._write_prior(target)
        pyproject = _make_pyproject(tmp_path)
        anchor = _determine_lock_anchor(
            pyproject, output=target, format="pylock", upgrade=False
        )
        assert anchor == self._RECORDED

    def test_default_output_path_used_when_output_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No --output but a pylock.toml in cwd -> reuse.
        monkeypatch.chdir(tmp_path)
        self._write_prior(tmp_path / "pylock.toml")
        pyproject = _make_pyproject(tmp_path)
        anchor = _determine_lock_anchor(
            pyproject, output=None, format="pylock", upgrade=False
        )
        assert anchor == self._RECORDED

    def test_absolute_uploaded_prior_to_is_the_anchor(self, tmp_path: Path) -> None:
        # An absolute cutoff pins the anchor regardless of any prior lock.
        target = tmp_path / "pylock.toml"
        self._write_prior(target)
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            f'[tool.nab]\nuploaded-prior-to = "{self._ABSOLUTE.isoformat()}"\n',
        )
        anchor = _determine_lock_anchor(
            pyproject, output=target, format="pylock", upgrade=False
        )
        assert anchor == self._ABSOLUTE

    def test_absolute_cutoff_from_project_nab_toml_is_the_anchor(
        self, tmp_path: Path
    ) -> None:
        # An absolute cutoff set in the project-dir nab.toml (not pyproject)
        # must pin the anchor too: the resolve honours it, so the lock must.
        target = tmp_path / "pylock.toml"
        self._write_prior(target)
        pyproject = _make_pyproject(tmp_path)
        (tmp_path / "nab.toml").write_text(
            f'uploaded-prior-to = "{self._ABSOLUTE.isoformat()}"\n'
        )
        anchor = _determine_lock_anchor(
            pyproject, output=target, format="pylock", upgrade=False
        )
        assert anchor == self._ABSOLUTE

    def test_invalid_config_falls_through_to_prior(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A category-gated value (uploaded-prior-to in a USER source) makes
        # the best-effort anchor read raise SourceConfigError; it is
        # swallowed so the full resolve parse reports it, and the anchor
        # falls through to the prior lock.
        target = tmp_path / "pylock.toml"
        self._write_prior(target)
        pyproject = _make_pyproject(tmp_path)
        user_toml = tmp_path / "user.toml"
        user_toml.write_text(f'uploaded-prior-to = "{self._ABSOLUTE.isoformat()}"\n')

        def fake_roots(p: Path) -> SourceRoots:
            return SourceRoots(
                system_toml=None,
                user_toml=user_toml,
                project_dir=p.parent.resolve(),
                pyproject=p.resolve(),
            )

        monkeypatch.setattr("nab.cli._config_search_roots", fake_roots)
        anchor = _determine_lock_anchor(
            pyproject, output=target, format="pylock", upgrade=False
        )
        assert anchor == self._RECORDED

    def test_absolute_cutoff_ignored_under_upgrade(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            f'[tool.nab]\nuploaded-prior-to = "{self._ABSOLUTE.isoformat()}"\n',
        )
        anchor = _determine_lock_anchor(
            pyproject, output=None, format="pylock", upgrade=True
        )
        assert anchor != self._ABSOLUTE
        assert (datetime.now(timezone.utc) - anchor).total_seconds() < 60

    def test_relative_cutoff_falls_through_to_prior(self, tmp_path: Path) -> None:
        target = tmp_path / "pylock.toml"
        self._write_prior(target)
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n[tool.nab]\nuploaded-prior-to = "P4D"\n',
        )
        anchor = _determine_lock_anchor(
            pyproject, output=target, format="pylock", upgrade=False
        )
        assert anchor == self._RECORDED


class TestLockAnchorReuse:
    """End-to-end: re-locking with an existing pylock reuses its anchor."""

    _RECORDED = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def _relative_cutoff_relock(self, tmp_path: Path) -> tuple[Path, Path]:
        """A pylock recording ``_RECORDED`` and a project with a ``P4D`` cutoff."""
        prior = tmp_path / "pylock.toml"
        prior.write_text(
            f"[tool.nab]\ncreated-at = {self._RECORDED.isoformat()}\n",
        )
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n[tool.nab]\nuploaded-prior-to = "P4D"\n',
        )
        return prior, pyproject

    def _relock_cutoff(self, tmp_path: Path, *, upgrade: bool = False) -> datetime:
        """The ``P4D`` window a re-lock over a recorded anchor resolves against."""
        prior, pyproject = self._relative_cutoff_relock(tmp_path)
        with patch(
            "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
        ) as mock_resolve:
            lock(pyproject, output=prior, upgrade=upgrade)

        cutoff = mock_resolve.call_args.kwargs["config"].uploaded_prior_to
        assert isinstance(cutoff, datetime)
        return cutoff

    def test_relock_records_reused_anchor(self, tmp_path: Path) -> None:
        prior, pyproject = self._relative_cutoff_relock(tmp_path)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=prior)
        # New pylock's [tool.nab].created-at must equal the prior anchor.
        from nab_project.lockfile import read_lockfile_anchor

        assert read_lockfile_anchor(prior) == self._RECORDED

    def test_upgrade_writes_fresh_anchor(self, tmp_path: Path) -> None:
        prior, pyproject = self._relative_cutoff_relock(tmp_path)
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=prior, upgrade=True)
        from nab_project.lockfile import read_lockfile_anchor

        new_anchor = read_lockfile_anchor(prior)
        assert new_anchor is not None
        assert new_anchor != self._RECORDED
        assert (datetime.now(timezone.utc) - new_anchor).total_seconds() < 60

    def test_absolute_cutoff_records_itself(self, tmp_path: Path) -> None:
        # An absolute uploaded-prior-to makes created-at deterministic.
        absolute = datetime(2026, 5, 1, tzinfo=timezone.utc)
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            f'[tool.nab]\nuploaded-prior-to = "{absolute.isoformat()}"\n',
        )
        out = tmp_path / "pylock.toml"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out)
        from nab_project.lockfile import read_lockfile_anchor

        assert read_lockfile_anchor(out) == absolute

    def test_sub_minute_offset_cutoff_stays_readable(self, tmp_path: Path) -> None:
        # A cutoff offset carrying seconds is valid ISO 8601 but not valid TOML.
        absolute = datetime(
            2026, 5, 1, tzinfo=timezone(timedelta(minutes=19, seconds=32))
        )
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            f'[tool.nab]\nuploaded-prior-to = "{absolute.isoformat()}"\n',
        )
        out = tmp_path / "pylock.toml"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out)
        from nab_project.lockfile import read_lockfile_anchor

        assert tomli.loads(out.read_text())["tool"]["nab"]["created-at"] == absolute
        assert read_lockfile_anchor(out) == absolute

    def test_relative_cutoff_window_uses_reused_anchor(self, tmp_path: Path) -> None:
        # The reused created-at sets the resolve window, not just the recorded
        # provenance.
        assert self._relock_cutoff(tmp_path) == self._RECORDED - timedelta(days=4)

    def test_upgrade_moves_relative_cutoff_window(self, tmp_path: Path) -> None:
        fresh = self._relock_cutoff(tmp_path, upgrade=True)

        window = datetime.now(timezone.utc) - timedelta(days=4)
        assert abs((window - fresh).total_seconds()) < 60


def _hashed_pin(version: str, name: str, *, sha: str) -> IndexPin:
    """Like ``_foo_index_pin`` but with caller-chosen artifact hashes."""
    return IndexPin(
        name=name,
        version=version,
        index="pypi",
        sdist=SdistArtifact(
            filename=f"{name}-{version}.tar.gz",
            url=f"https://example.com/{name}-{version}.tar.gz",
            hashes=(("sha256", sha),),
        ),
        wheels=(
            WheelArtifact(
                filename=f"{name}-{version}-py3-none-any.whl",
                url=f"https://example.com/{name}-{version}-py3-none-any.whl",
                hashes=(("sha256", sha),),
            ),
        ),
    )


def _hashed_resolve_result(*, sha: str) -> ResolveResult:
    """A host resolve pinning foo 1.0 with caller-chosen artifact hashes."""
    target = ResolveTarget.for_host()
    return ResolveResult(
        targets=(target,),
        target_results=[
            TargetResult(
                target=target,
                success=True,
                pins={"foo": V("1.0")},
                lock=TargetLock(
                    target=target, pins={"foo": _hashed_pin("1.0", "foo", sha=sha)}
                ),
            )
        ],
    )


_CONFLICT_PROJECT = (
    '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["shared"]\n'
    "[project.optional-dependencies]\n"
    "cpu = []\n"
    "gpu = []\n"
    "[tool.nab]\n"
    'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
)


def _conflict_fork_result(*, shared: tuple[str, str]) -> ResolveResult:
    """Two conflict forks of the host environment, each pinning ``shared``.

    ``shared`` is a base dependency of both forks, so equal pins collapse to
    one entry and divergent pins are a refusal.
    """
    host = ResolveTarget.for_host()
    forks = tuple(
        host.with_selection((("extra", member),)) for member in ("cpu", "gpu")
    )

    return ResolveResult(
        targets=forks,
        target_results=[
            _resolved(fork, {"shared": V(version)})
            for fork, version in zip(forks, shared, strict=True)
        ],
        env_base_names={env_signature(forks[0]): frozenset({"shared"})},
    )


class TestLockedFlag:
    """``nab lock --locked`` re-resolves and verifies the committed pylock."""

    def _write_lock(
        self,
        pyproject: Path,
        out: Path,
        result: ResolveResult,
        *extra_args: str,
    ) -> None:
        with patch("nab.cli.resolve_for_targets", return_value=result):
            app.cli(
                args=["lock", str(pyproject), "--output", str(out), *extra_args],
                prog="nab",
            )

    def _run_locked(
        self,
        pyproject: Path,
        out: Path,
        result: ResolveResult,
        *extra_args: str,
    ) -> None:
        with patch("nab.cli.resolve_for_targets", return_value=result):
            app.cli(
                args=[
                    "lock",
                    str(pyproject),
                    "--output",
                    str(out),
                    "--locked",
                    *extra_args,
                ],
                prog="nab",
            )

    def test_unevaluable_root_marker_leaves_the_error_to_the_resolve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The pre-resolve validity check reads the root requirements too.

        A self-referencing extra gated on a marker no comparison decides is a
        project nab cannot read, not a stale lock, so the checks are skipped
        and the resolve reports it.
        """
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "probe"\nversion = "0.1.0"\ndependencies = []\n'
            "[project.optional-dependencies]\n"
            'fast = ["somepkg"]\n'
            "all = [\"probe[fast]; python_full_version ~= '3'\"]\n",
        )
        out = tmp_path / "pylock.toml"
        out.write_text(
            'lock-version = "1.0"\ncreated-by = "nab"\n'
            'extras = ["all"]\npackages = []\n'
        )
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=out, locked=True, offline=True, extras=("all",))

        err = capsys.readouterr().err
        assert 'cannot lock: marker python_full_version ~= "3"' in err
        assert "--locked" not in err
        assert "Traceback" not in err

    def test_up_to_date_exits_zero_without_writing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        self._write_lock(pyproject, out, _stub_resolve_result(pins={"foo": V("1.0")}))
        capsys.readouterr()
        before = out.read_bytes()
        self._run_locked(pyproject, out, _stub_resolve_result(pins={"foo": V("1.0")}))
        assert "is up to date" in capsys.readouterr().err
        assert out.read_bytes() == before

    def test_lock_offering_groups_is_up_to_date(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The name lands in ``default-groups`` while no run selects it.

        A checker comparing that array as it stands would call every lock
        that names the project's own dependencies out of date.
        """
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n[dependency-groups]\ndev = ["foo"]\n'
            '[tool.nab]\nbase-group = "default"\n',
        )
        out = tmp_path / "pylock.toml"
        result = _stub_resolve_result(pins={"foo": V("1.0")})
        self._write_lock(pyproject, out, result, "--groups", "dev")
        capsys.readouterr()
        assert "default" in tomli.loads(out.read_text())["default-groups"]

        self._run_locked(pyproject, out, result, "--groups", "dev")
        assert "is up to date" in capsys.readouterr().err

    def test_a_renamed_base_group_is_out_of_date(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Renaming the group renames a marker on every package it gates.

        Nothing else about the lock changes, so only the name the writer
        would now use can tell the checker it is stale.
        """
        body = (
            '[project]\ndependencies = ["foo"]\n'
            '[dependency-groups]\ndev = ["foo"]\n'
            "[tool.nab]\n"
        )
        pyproject = _make_pyproject(tmp_path, body + 'base-group = "default"\n')
        out = tmp_path / "pylock.toml"
        result = _stub_resolve_result(pins={"foo": V("1.0")})
        self._write_lock(pyproject, out, result, "--groups", "dev")
        capsys.readouterr()
        assert tomli.loads(out.read_text())["default-groups"] == ["default"]

        pyproject.write_text(body + 'base-group = "base"\n', encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            self._run_locked(pyproject, out, result, "--groups", "dev")

        assert exc.value.code == 1
        assert "out of date" in capsys.readouterr().err

    def test_out_of_date_version_exits_one_without_writing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        self._write_lock(pyproject, out, _stub_resolve_result(pins={"foo": V("1.0")}))
        capsys.readouterr()
        before = out.read_bytes()
        with pytest.raises(SystemExit) as exc:
            self._run_locked(
                pyproject, out, _stub_resolve_result(pins={"foo": V("2.0")})
            )
        assert exc.value.code == 1
        assert "out of date" in capsys.readouterr().err
        assert out.read_bytes() == before

    def test_out_of_date_hash_change_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Same version, different artifact hash: the compare looks past the
        # version, so a re-upload is still caught.
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        first = _hashed_resolve_result(sha="a" * 64)
        self._write_lock(pyproject, out, first)
        capsys.readouterr()
        changed = _hashed_resolve_result(sha="c" * 64)
        with pytest.raises(SystemExit) as exc:
            self._run_locked(pyproject, out, changed)
        assert exc.value.code == 1
        assert "out of date" in capsys.readouterr().err

    def test_missing_lockfile_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with pytest.raises(SystemExit) as exc:
            self._run_locked(pyproject, out, _stub_resolve_result())
        assert exc.value.code == 1
        assert "no lockfile" in capsys.readouterr().err

    def test_unhashable_pin_during_render_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A pin that cannot be hashed makes the render fail; --locked reports
        # it and exits without touching the committed lock, like a normal lock.
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        self._write_lock(pyproject, out, _stub_resolve_result(pins={"foo": V("1.0")}))
        capsys.readouterr()
        before = out.read_bytes()
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_stub_resolve_result(pins={"foo": V("1.0")}),
            ),
            patch("nab._lock.render_lock", side_effect=MissingHashError("no hash")),
            pytest.raises(SystemExit) as exc,
        ):
            app.cli(
                args=["lock", str(pyproject), "--output", str(out), "--locked"],
                prog="nab",
            )
        assert exc.value.code == 1
        assert "cannot lock" in capsys.readouterr().err
        assert out.read_bytes() == before

    def test_divergent_base_dep_during_render_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The forks agreed when the lock was committed and now pin the shared
        # base dependency differently.
        pyproject = _make_pyproject(tmp_path, _CONFLICT_PROJECT)
        out = tmp_path / "pylock.toml"
        agreed = _conflict_fork_result(shared=("1.0", "1.0"))
        self._write_lock(pyproject, out, agreed, "--extras", "cpu", "gpu")
        capsys.readouterr()
        before = out.read_bytes()

        diverged = _conflict_fork_result(shared=("1.0", "2.0"))
        with pytest.raises(SystemExit) as exc:
            self._run_locked(pyproject, out, diverged, "--extras", "cpu", "gpu")

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "error: shared: the conflict forks of one environment pin" in err
        assert out.read_bytes() == before

    def test_disjointness_during_render_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        self._write_lock(pyproject, out, _stub_resolve_result(pins={"foo": V("1.0")}))
        capsys.readouterr()
        before = out.read_bytes()

        hint = "foo: 2 entries fire under env='py311-linux_x86_64'"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_stub_resolve_result(pins={"foo": V("1.0")}),
            ),
            patch("nab._lock.render_lock", side_effect=DisjointnessError(hint)),
            pytest.raises(SystemExit) as exc,
        ):
            app.cli(
                args=["lock", str(pyproject), "--output", str(out), "--locked"],
                prog="nab",
            )

        assert exc.value.code == 1
        assert f"error: {hint}\n" in capsys.readouterr().err
        assert out.read_bytes() == before

    def test_malformed_committed_lock_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A committed pylock that is not valid TOML exits with a message,
        # not a raw TOMLDecodeError.
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        self._write_lock(pyproject, out, _stub_resolve_result(pins={"foo": V("1.0")}))
        capsys.readouterr()
        out.write_text('lock-version = "1.0"\n<<<<<<< HEAD\n', encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            self._run_locked(
                pyproject, out, _stub_resolve_result(pins={"foo": V("1.0")})
            )
        assert exc.value.code == 1
        assert "is not valid TOML" in capsys.readouterr().err

    def test_non_utf8_committed_lock_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A committed pylock that is not valid UTF-8 exits the same way,
        # not a raw UnicodeDecodeError.
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        self._write_lock(pyproject, out, _stub_resolve_result(pins={"foo": V("1.0")}))
        capsys.readouterr()
        out.write_bytes(b"\xff\xfe not utf-8")
        with pytest.raises(SystemExit) as exc:
            self._run_locked(
                pyproject, out, _stub_resolve_result(pins={"foo": V("1.0")})
            )
        assert exc.value.code == 1
        assert "is not valid TOML" in capsys.readouterr().err

    def test_requirements_format_unsupported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with pytest.raises(SystemExit) as exc:
            app.cli(
                args=[
                    "lock",
                    str(pyproject),
                    "--output",
                    str(out),
                    "--format",
                    "requirements",
                    "--locked",
                ],
                prog="nab",
            )
        assert exc.value.code == 1
        assert "only supported for pylock" in capsys.readouterr().err
        assert not out.exists()

    def test_stdout_unsupported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit) as exc:
            app.cli(
                args=["lock", str(pyproject), "--output", "-", "--locked"], prog="nab"
            )
        assert exc.value.code == 1
        assert "only supported for pylock" in capsys.readouterr().err

    def test_universal_unsupported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with pytest.raises(SystemExit) as exc:
            app.cli(
                args=["lock", str(pyproject), "--output", str(out), "--locked"],
                prog="nab",
            )
        assert exc.value.code == 1
        assert "not supported in universal mode" in capsys.readouterr().err
        assert not out.exists()

    def test_local_source_paths_do_not_false_mismatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A local-source path is relativized against the lock dir on both passes.

        The resolve is real, since a local source needs no index.  The lock
        goes in a subdirectory while the cwd stays at the project root, so a
        check pass relativizing against the cwd would render ``vendor`` where
        the committed lock holds ``../vendor`` and call a current lock stale.
        """
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.0"\ndependencies = []\n'
        )

        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "root"\nversion = "0"\ndependencies = ["foo"]\n'
            "[[tool.nab.local-sources]]\n"
            'name = "foo"\npath = "vendor"\n',
        )

        out = tmp_path / "sub" / "pylock.toml"
        out.parent.mkdir()
        monkeypatch.chdir(tmp_path)

        lock(pyproject, output=out, offline=True, cache=False)
        packages = tomli.loads(out.read_text())["packages"]
        assert [package["directory"]["path"] for package in packages] == ["../vendor"]

        capsys.readouterr()
        lock(pyproject, output=out, offline=True, cache=False, locked=True)
        assert "is up to date" in capsys.readouterr().err


class TestLockProvenanceCliOverrides:
    """A --project-* CLI override is recorded in the lockfile provenance."""

    def test_cli_override_recorded_in_pylock(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            app.cli(
                args=[
                    "lock",
                    str(pyproject),
                    "--output",
                    str(out),
                    "--project-resolution",
                    "lowest",
                ],
                prog="nab",
            )
        block = tomli.loads(out.read_text())["tool"]["nab"]
        assert block["cli-project-overrides"] == ["--project-resolution=lowest"]

    def test_no_cli_override_omits_key(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            app.cli(args=["lock", str(pyproject), "--output", str(out)], prog="nab")
        block = tomli.loads(out.read_text())["tool"]["nab"]
        assert "cli-project-overrides" not in block

    def test_command_line_records_the_program_name(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        argv = ["/somewhere/on/this/machine/src/nab/__main__.py", "lock", "--offline"]
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            patch.object(sys, "argv", argv),
        ):
            app.cli(args=["lock", str(pyproject), "--output", str(out)], prog="nab")
        recorded = tomli.loads(out.read_text())["tool"]["nab"]["command-line"]
        assert recorded == ["nab", "lock", "--offline"]


class TestGroupAndExtraSelection:
    """Selection guards for ``--all-groups`` / ``--all-extras``.

    Tests cover the mutually-exclusive guards and the
    ``FileNotFoundError`` fallback when the pyproject is missing.
    Both surface through ``lock(...)`` so the exit-on-error path
    is exercised end-to-end.
    """

    def test_all_groups_with_explicit_groups_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, all_groups=True, groups=("dev",))
        assert "mutually exclusive" in capsys.readouterr().err

    def test_all_extras_with_explicit_extras_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, all_extras=True, extras=("test",))
        assert "mutually exclusive" in capsys.readouterr().err

    def test_all_groups_missing_file_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit 1 with a descriptive message if the pyproject vanished.

        ``--all-groups`` reads the file between the outer guard and
        the inner read; the helper guards against the race.
        """
        with pytest.raises(SystemExit, match="1"):
            resolve_group_selection(
                tmp_path / "missing.toml", groups=(), all_groups=True
            )
        assert "not found" in capsys.readouterr().err

    def test_all_extras_missing_file_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Symmetric guard for ``--all-extras``."""
        with pytest.raises(SystemExit, match="1"):
            resolve_extra_selection(
                tmp_path / "missing.toml", extras=(), all_extras=True
            )
        assert "not found" in capsys.readouterr().err

    def test_all_groups_unreadable_file_surfaces_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A regular file that still raises OSError reports the real error."""
        pyproject = _make_pyproject(tmp_path)
        denied = PermissionError(errno.EACCES, "Permission denied", str(pyproject))
        with (
            patch("nab._lock.read_pyproject_groups", side_effect=denied),
            pytest.raises(SystemExit, match="1"),
        ):
            resolve_group_selection(pyproject, groups=(), all_groups=True)
        err = capsys.readouterr().err
        assert "not found" not in err
        assert "Permission denied" in err

    def test_all_extras_unreadable_file_surfaces_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Symmetric guard for ``--all-extras`` on an unreadable pyproject."""
        pyproject = _make_pyproject(tmp_path)
        denied = PermissionError(errno.EACCES, "Permission denied", str(pyproject))
        with (
            patch("nab._lock.read_pyproject_optional_dependencies", side_effect=denied),
            pytest.raises(SystemExit, match="1"),
        ):
            resolve_extra_selection(pyproject, extras=(), all_extras=True)
        err = capsys.readouterr().err
        assert "not found" not in err
        assert "Permission denied" in err

    def test_all_extras_unsearchable_parent_surfaces_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        deny_access: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        """An unsearchable parent must not fail the is-a-directory guard.

        EACCES lands on the stat behind that guard as well as on the read,
        so classifying the path has to be raise-free.
        """
        pyproject = _make_pyproject(tmp_path)
        with deny_access(pyproject), pytest.raises(SystemExit, match="1"):
            resolve_extra_selection(pyproject, extras=(), all_extras=True)
        err = capsys.readouterr().err
        assert "cannot read" in err
        assert "Permission denied" in err

    def test_all_groups_on_a_pylock_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A lock passed as the project is named as one, not read for groups."""
        pylock = _make_pylock_with_groups(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            resolve_group_selection(pylock, groups=(), all_groups=True)
        assert "is a PEP 751 lockfile" in capsys.readouterr().err

    def test_all_extras_on_a_pylock_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Symmetric guard for ``--all-extras`` on a lock."""
        pylock = _make_pylock_with_groups(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            resolve_extra_selection(pylock, extras=(), all_extras=True)
        assert "is a PEP 751 lockfile" in capsys.readouterr().err

    def test_all_groups_reads_defined_groups(self, tmp_path: Path) -> None:
        """Selection equals the keys of ``[dependency-groups]``.

        When the file exists and defines groups, ``--all-groups``
        expands to every key in the table.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\nname = 'x'\n[dependency-groups]\ndev = []\nlint = []\n",
        )
        result = resolve_group_selection(pyproject, groups=(), all_groups=True)
        assert sorted(result) == ["dev", "lint"]

    def test_all_extras_reads_defined_extras(self, tmp_path: Path) -> None:
        """Symmetric to ``--all-groups`` for optional-dependencies.

        ``--all-extras`` returns the union of keys in
        ``[project.optional-dependencies]``.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\nname = 'x'\n"
            "[project.optional-dependencies]\ntest = []\nci = []\n",
        )
        result = resolve_extra_selection(pyproject, extras=(), all_extras=True)
        assert sorted(result) == ["ci", "test"]

    def test_no_group_selection_skips_read(self, tmp_path: Path) -> None:
        result = resolve_group_selection(
            tmp_path / "missing.toml", groups=(), all_groups=False
        )
        assert result == ()

    def test_no_extra_selection_skips_read(self, tmp_path: Path) -> None:
        result = resolve_extra_selection(
            tmp_path / "missing.toml", extras=(), all_extras=False
        )
        assert result == ()

    def test_explicit_groups_returns_deduplicated(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\nname = 'x'\n[dependency-groups]\ndev = []\nlint = []\n",
        )
        result = resolve_group_selection(
            pyproject, groups=("dev", "lint", "dev"), all_groups=False
        )
        assert result == ("dev", "lint")

    def test_explicit_extras_returns_deduplicated(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\nname = 'x'\n"
            "[project.optional-dependencies]\ntest = []\nci = []\n",
        )
        result = resolve_extra_selection(
            pyproject, extras=("test", "ci", "test"), all_extras=False
        )
        assert result == ("test", "ci")

    def test_all_groups_non_table_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("dependency-groups = 'oops'\n[project]\nname = 'x'\n")
        with pytest.raises(SystemExit, match="1"):
            resolve_group_selection(pyproject, groups=(), all_groups=True)
        assert "[dependency-groups] must be a table" in capsys.readouterr().err

    def test_all_groups_malformed_toml_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'x'\n[dependency-groups]\ndev = ['a'\n")
        with pytest.raises(SystemExit, match="1"):
            resolve_group_selection(pyproject, groups=(), all_groups=True)
        assert "is not valid TOML" in capsys.readouterr().err

    def test_all_groups_non_utf8_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_bytes(b"[project]\nname = '\xe9'\n[dependency-groups]\n")
        with pytest.raises(SystemExit, match="1"):
            resolve_group_selection(pyproject, groups=(), all_groups=True)
        assert "is not valid TOML" in capsys.readouterr().err

    def test_all_extras_malformed_toml_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\nname = 'x'\n[project.optional-dependencies]\ntest = ['a'\n"
        )
        with pytest.raises(SystemExit, match="1"):
            resolve_extra_selection(pyproject, extras=(), all_extras=True)
        assert "is not valid TOML" in capsys.readouterr().err

    def test_all_extras_non_utf8_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_bytes(b"[project]\nname = '\xe9'\n")
        with pytest.raises(SystemExit, match="1"):
            resolve_extra_selection(pyproject, extras=(), all_extras=True)
        assert "is not valid TOML" in capsys.readouterr().err

    def test_explicit_groups_non_table_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("dependency-groups = 'oops'\n[project]\nname = 'x'\n")
        with pytest.raises(SystemExit, match="1"):
            resolve_group_selection(pyproject, groups=("dev",), all_groups=False)
        assert "[dependency-groups] must be a table" in capsys.readouterr().err

    def test_all_extras_non_table_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'x'\noptional-dependencies = 'oops'\n")
        with pytest.raises(SystemExit, match="1"):
            resolve_extra_selection(pyproject, extras=(), all_extras=True)
        assert "[project.optional-dependencies] must be a table" in (
            capsys.readouterr().err
        )

    def test_explicit_extras_non_table_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'x'\noptional-dependencies = 'oops'\n")
        with pytest.raises(SystemExit, match="1"):
            resolve_extra_selection(pyproject, extras=("foo",), all_extras=False)
        assert "[project.optional-dependencies] must be a table" in (
            capsys.readouterr().err
        )


class TestEmitHelpers:
    """The emit helpers accept a lock input with no provenance.

    ``LockInput.provenance`` defaults to ``None``, so a caller that does
    not want a ``[tool.nab]`` block simply never sets it.
    """

    def test_emit_without_provenance(self, tmp_path: Path) -> None:
        out = tmp_path / "pylock.toml"
        _emit(_stub_lock_input(), format="pylock", output=out)
        # The output is a valid pylock without [tool.nab] provenance.
        text = out.read_text()
        assert 'lock-version = "1.0"' in text
        assert "[tool.nab]" not in text

    def test_emit_pylock_without_provenance(self, tmp_path: Path) -> None:
        tup = _target()
        lock_input = LockInput(
            targets={tup.label: _target_lock(tup, {"foo": V("1.0")})}
        )
        out = tmp_path / "pylock.toml"
        _emit_pylock(lock_input, output=out, default_output=Path("pylock.toml"))
        text = out.read_text()
        assert 'lock-version = "1.0"' in text

    def test_a_matrix_lock_reports_its_tuples(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two tuples may disagree on a version, so there is no diff to report."""
        first, second = _target("3.11"), _target("3.12")
        lock_input = LockInput(
            targets={
                first.label: _target_lock(first, {"foo": V("1.0")}),
                second.label: _target_lock(second, {"foo": V("2.0")}),
            }
        )
        _emit_pylock(
            lock_input,
            output=tmp_path / "pylock.toml",
            default_output=Path("pylock.toml"),
        )
        assert "(2 tuples)" in capsys.readouterr().err


class TestCacheFlags:
    """Tests for --cache-dir, --no-cache, --offline."""

    def test_default_cache_dir_passed_through(self, tmp_path: Path) -> None:
        """No flags: cache_dir defaults to a real path, offline is False."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_stub_resolve_result(pins={}),
            ) as mock_resolve,
            patch("nab._lock.write_lock"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs["cache_dir"] is not None
        assert kwargs["offline"] is False

    def test_explicit_cache_dir_passed_through(self, tmp_path: Path) -> None:
        """An explicit --cache-dir flows through to resolve_for_targets."""
        pyproject = _make_pyproject(tmp_path)
        cache = tmp_path / "mycache"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_stub_resolve_result(pins={}),
            ) as mock_resolve,
            patch("nab._lock.write_lock"),
        ):
            lock(pyproject, cache_dir=cache, output=tmp_path / "pylock.toml")
        assert mock_resolve.call_args.kwargs["cache_dir"] == cache

    def test_no_cache_disables_cache(self, tmp_path: Path) -> None:
        """``cache=False`` wins over --cache-dir and disables persistence."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_stub_resolve_result(pins={}),
            ) as mock_resolve,
            patch("nab._lock.write_lock"),
        ):
            lock(pyproject, cache=False, output=tmp_path / "pylock.toml")
        assert mock_resolve.call_args.kwargs["cache_dir"] is None

    def test_offline_passed_through(self, tmp_path: Path) -> None:
        """--offline flows through to resolve_for_targets."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_stub_resolve_result(pins={}),
            ) as mock_resolve,
            patch("nab._lock.write_lock"),
        ):
            lock(pyproject, offline=True, output=tmp_path / "pylock.toml")
        assert mock_resolve.call_args.kwargs["offline"] is True

    def test_default_cache_dir_uses_xdg_when_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default uses XDG_CACHE_HOME when set."""
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert _default_cache_dir() == tmp_path / "xdg" / "nab"

    def test_default_cache_dir_falls_back_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default falls back to ~/.cache/nab when XDG is unset."""
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr("nab.cli.Path.home", lambda: tmp_path)
        assert _default_cache_dir() == tmp_path / ".cache" / "nab"


def _command_help(command: str) -> str:
    """Return the ``--help`` text tyro renders for a subcommand."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        app.cli(args=[command, "--help"], prog="nab")
    return buf.getvalue()


class TestHelpText:
    """Boolean flags render without tyro's double-negated aliases."""

    def test_lock_cache_flag_has_no_double_negative(self) -> None:
        help_text = _command_help("lock")
        assert "--no-no-cache" not in help_text
        assert "--cache" in help_text
        assert "--no-cache" in help_text

    def test_lock_workspace_discovery_has_no_double_negative(self) -> None:
        help_text = _command_help("lock")
        assert "--no-no-workspace-discovery" not in help_text
        assert "--workspace-discovery" in help_text
        assert "--no-workspace-discovery" in help_text

    def test_download_cache_flag_has_no_double_negative(self) -> None:
        help_text = _command_help("download")
        assert "--no-no-cache" not in help_text
        assert "--no-cache" in help_text


class TestOfflineFlagContract:
    """Pin the tyro argv contract for the layered ``--offline`` flag.

    ``offline`` is layered (env / nab.toml may set it) and the CLI is the
    top rung, so the flag has to distinguish ``--offline True`` /
    ``--offline False`` from being absent (``None``, let the lower layers
    decide).  tyro renders that tri-state as a value-taking choice, so the
    value form is the canonical surface app.cli parses; :func:`main`
    rewrites the bare ``--offline`` / ``--no-offline`` forms into it (see
    :class:`TestMainNormalizesOfflineFlag`).
    """

    @pytest.mark.parametrize("command", ["lock", "download", "config"])
    def test_offline_help_shows_value_and_bare_forms(self, command: str) -> None:
        help_text = _command_help(command)
        assert "--offline {True,False}" in help_text
        assert "--no-offline" in help_text

    def _run_offline_argv(self, tmp_path: Path, value: str) -> object:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_stub_resolve_result(pins={}),
            ) as mock_resolve,
            patch("nab._lock.write_lock"),
        ):
            app.cli(
                args=[
                    "lock",
                    str(pyproject),
                    "--offline",
                    value,
                    "--output",
                    str(tmp_path / "pylock.toml"),
                ],
                prog="nab",
            )
        return mock_resolve.call_args.kwargs["offline"]

    def test_offline_true_parses(self, tmp_path: Path) -> None:
        assert self._run_offline_argv(tmp_path, "True") is True

    def test_offline_false_parses(self, tmp_path: Path) -> None:
        assert self._run_offline_argv(tmp_path, "False") is False

    def test_bare_offline_needs_value_at_tyro_layer(self, tmp_path: Path) -> None:
        """The value form is required below main(); main() bridges the gap."""
        pyproject = _make_pyproject(tmp_path)
        with (
            contextlib.redirect_stderr(io.StringIO()),
            pytest.raises(SystemExit) as exc,
        ):
            app.cli(args=["lock", str(pyproject), "--offline"], prog="nab")
        assert exc.value.code == 2


class TestNormalizeLayeredBoolFlags:
    """Unit-test the argv rewrite that gives ``--offline`` its bare forms."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            ([], []),
            (["lock"], ["lock"]),
            (["lock", "--offline"], ["lock", "--offline", "True"]),
            (["lock", "--offline", "True"], ["lock", "--offline", "True"]),
            (["lock", "--offline", "False"], ["lock", "--offline", "False"]),
            (["lock", "--offline", "None"], ["lock", "--offline", "None"]),
            (["lock", "--no-offline"], ["lock", "--offline", "False"]),
            (
                ["lock", "--offline", "--output", "pylock.toml"],
                ["lock", "--offline", "True", "--output", "pylock.toml"],
            ),
            (
                ["lock", "--no-cache", "--offline"],
                ["lock", "--no-cache", "--offline", "True"],
            ),
        ],
    )
    def test_rewrites(self, argv: list[str], expected: list[str]) -> None:
        assert _normalize_layered_bool_flags(argv) == expected


class TestMainNormalizesOfflineFlag:
    """main() applies the bare-form rewrite before tyro parses."""

    def _offline_seen_by_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *offline_args: str
    ) -> object:
        pyproject = _make_pyproject(tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "nab",
                "lock",
                str(pyproject),
                "--output",
                str(tmp_path / "pylock.toml"),
                *offline_args,
            ],
        )
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_stub_resolve_result(pins={}),
            ) as mock_resolve,
            patch("nab._lock.write_lock"),
        ):
            main()
        return mock_resolve.call_args.kwargs["offline"]

    def test_bare_offline_forces_offline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._offline_seen_by_resolve(tmp_path, monkeypatch, "--offline") is True

    def test_no_offline_forces_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (
            self._offline_seen_by_resolve(tmp_path, monkeypatch, "--no-offline")
            is False
        )

    def test_explicit_value_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (
            self._offline_seen_by_resolve(tmp_path, monkeypatch, "--offline", "False")
            is False
        )


class TestLayeredRunKnobFlagContract:
    """Pin the tyro argv contract for the layered USER run knobs.

    ``http-backend`` and ``max-concurrency`` are layered (env / nab.toml may
    set them), so each CLI flag is tri-state: a value distinguishes an
    explicit override from being absent (``None``, let the lower layers
    decide).  These tests lock how tyro renders that so it cannot drift.
    """

    def test_http_backend_renders_as_tristate_choice(self) -> None:
        for command in ("lock", "download", "config"):
            assert "--http-backend {None,urllib3,httpx}" in _command_help(command)

    def test_max_concurrency_renders_as_tristate_value(self) -> None:
        for command in ("download", "config"):
            assert "--max-concurrency {None}|INT" in _command_help(command)

    def test_project_scalar_override_renders_choices(self) -> None:
        for command in ("lock", "download", "config"):
            help_text = _command_help(command)
            assert "--project-mode {None,specific,universal}" in help_text
            assert "--project-build-policy {None,never,build-local,build-remote}" in (
                help_text
            )

    def test_project_array_override_is_repeatable_single_value(self) -> None:
        for command in ("lock", "download", "config"):
            assert "--project-constraint STR" in _command_help(command)

    def test_http_backend_value_reaches_transport(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_stub_resolve_result(pins={}),
            ),
            patch("nab._lock.write_lock"),
            patch("nab.cli._make_transport") as mock_transport,
        ):
            app.cli(
                args=[
                    "lock",
                    str(pyproject),
                    "--http-backend",
                    "httpx",
                    "--output",
                    str(tmp_path / "pylock.toml"),
                ],
                prog="nab",
            )
        assert mock_transport.call_args.args[0] == "httpx"


class TestPackageVersion:
    """Tests for nab._version.__version__ and the python -m nab entry point."""

    def test_version_attribute_exposed(self) -> None:
        """``nab._version.__version__`` resolves to the installed package version."""
        assert isinstance(nab_version.__version__, str)
        assert nab_version.__version__

    def test_version_falls_back_when_metadata_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An uninstalled package surfaces a sentinel rather than a hard fail."""

        def boom(_: str) -> str:
            msg = "nab"
            raise PackageNotFoundError(msg)

        monkeypatch.setattr("importlib.metadata.version", boom)
        importlib.reload(nab_version)
        try:
            assert nab_version.__version__ == "0.0.0+unknown"
        finally:
            monkeypatch.undo()
            importlib.reload(nab_version)

    def test_python_dash_m_runs_main(self) -> None:
        """``python -m nab`` invokes the CLI's main() entry point."""
        with patch("nab.cli.main") as mock_main:
            runpy.run_module("nab", run_name="__main__")
        mock_main.assert_called_once()


class TestMain:
    """Tests for the main() entry point."""

    def test_calls_app_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() delegates to app.cli() with the normalized argv."""
        monkeypatch.setattr(sys, "argv", ["nab"])
        with patch("nab.cli.app") as mock_app:
            main()
        mock_app.cli.assert_called_once_with(prog="nab", args=[])

    def test_version_flag_prints_and_returns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--version`` prints ``nab <version>`` without delegating to tyro."""
        monkeypatch.setattr(sys, "argv", ["nab", "--version"])
        with patch("nab.cli.app") as mock_app:
            main()
        mock_app.cli.assert_not_called()
        captured = capsys.readouterr()
        assert captured.out.startswith("nab ")
        assert captured.out.endswith("\n")

    def test_short_version_flag(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``-V`` is the short alias for ``--version``."""
        monkeypatch.setattr(sys, "argv", ["nab", "-V"])
        with patch("nab.cli.app") as mock_app:
            main()
        mock_app.cli.assert_not_called()
        assert capsys.readouterr().out.startswith("nab ")

    def test_keyboard_interrupt_aborts_clean(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ctrl-C during ``app.cli()`` reports an interrupt and exits 130."""
        monkeypatch.setattr(sys, "argv", ["nab"])
        with patch("nab.cli.app") as mock_app:
            mock_app.cli.side_effect = KeyboardInterrupt
            with pytest.raises(SystemExit) as info:
                main()
        assert info.value.code == 130
        assert "error: interrupted" in capsys.readouterr().err

    def test_bad_color_flag_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A malformed --color value is reported and exits 2 before tyro runs."""
        monkeypatch.setattr(sys, "argv", ["nab", "--color", "rainbow", "lock"])
        with pytest.raises(SystemExit) as info:
            main()
        assert info.value.code == 2
        assert "error:" in capsys.readouterr().err

    def test_resolve_without_progress_reporter(self, tmp_path: Path) -> None:
        """_resolve tolerates progress=None (the clear step is skipped)."""
        pyproject = _make_pyproject(tmp_path)
        config = read_pyproject_config(pyproject)
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_stub_resolve_result(pins={}),
        ):
            result = _resolve(
                pyproject,
                config=config,
                cache_dir=None,
                offline=False,
                transport=MagicMock(),
                failure_prefix="cannot lock",
            )
        assert result.success

    @pytest.mark.usefixtures("restored_gc_state")
    def test_resolve_pauses_the_collector(self, tmp_path: Path) -> None:
        """The cyclic collector is off while the resolver runs, back on after."""
        pyproject = _make_pyproject(tmp_path)
        config = read_pyproject_config(pyproject)
        enabled_during: list[bool] = []

        def record_gc_state(*args: object, **kwargs: object) -> ResolveResult:
            enabled_during.append(gc.isenabled())
            return _stub_resolve_result(pins={})

        with patch("nab.cli.resolve_for_targets", side_effect=record_gc_state):
            _resolve(
                pyproject,
                config=config,
                cache_dir=None,
                offline=False,
                transport=MagicMock(),
                failure_prefix="cannot lock",
            )
        assert enabled_during == [False]
        assert gc.isenabled()

    @pytest.mark.usefixtures("restored_gc_state")
    def test_resolve_promotes_what_it_allocated(self, tmp_path: Path) -> None:
        """Objects the resolve allocated end in generation 2, not generation 0.

        Reads generations directly, so it needs a build whose
        ``gc.get_objects(generation=...)`` filters by generation; the
        free-threaded builds return every tracked object for any of them.
        """
        pyproject = _make_pyproject(tmp_path)
        config = read_pyproject_config(pyproject)
        allocated: list[object] = []

        def allocate_tracked(*args: object, **kwargs: object) -> ResolveResult:
            """Stand in for the resolve, leaving one tracked object behind."""
            allocated.append(["resolve", "graph"])
            return _stub_resolve_result(pins={})

        # Zeroed generation counters keep the collection the re-enable triggers
        # at generation 1, so without the promotion the object cannot reach
        # generation 2 on the session's own schedule.
        gc.collect()

        with patch("nab.cli.resolve_for_targets", side_effect=allocate_tracked):
            _resolve(
                pyproject,
                config=config,
                cache_dir=None,
                offline=False,
                transport=MagicMock(),
                failure_prefix="cannot lock",
            )

        # gc.get_objects allocates, and a collection it triggers would move the
        # object out of the generation the resolve left it in.
        gc.disable()

        graph = allocated[0]
        assert not any(obj is graph for obj in gc.get_objects(generation=0))
        assert any(obj is graph for obj in gc.get_objects(generation=2))

    @pytest.mark.usefixtures("restored_gc_state")
    def test_resolve_without_gc_freeze_still_reenables_the_collector(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The collector comes back on an interpreter without ``gc.freeze``.

        The resolve fails here because the promotion sits in a ``finally``: an
        ``AttributeError`` raised there would replace the ``ResolutionError``,
        and the command would print a traceback instead of exiting 1.
        """
        monkeypatch.delattr(gc, "freeze")
        pyproject = _make_pyproject(tmp_path)
        config = read_pyproject_config(pyproject)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=ResolutionError("no solution"),
            ),
            pytest.raises(SystemExit),
        ):
            _resolve(
                pyproject,
                config=config,
                cache_dir=None,
                offline=False,
                transport=MagicMock(),
                failure_prefix="cannot lock",
            )
        assert gc.isenabled()

    @pytest.mark.usefixtures("restored_gc_state")
    def test_resolve_reenables_the_collector_on_failure(self, tmp_path: Path) -> None:
        """A resolve that exits on an error still leaves the collector enabled."""
        pyproject = _make_pyproject(tmp_path)
        config = read_pyproject_config(pyproject)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=ResolutionError("no solution"),
            ),
            pytest.raises(SystemExit),
        ):
            _resolve(
                pyproject,
                config=config,
                cache_dir=None,
                offline=False,
                transport=MagicMock(),
                failure_prefix="cannot lock",
            )
        assert gc.isenabled()


class TestConsoleEntry:
    """Tests for the installed ``nab`` command's entry point.

    ``os._exit`` ends the process outright, so every test here patches it.
    """

    def test_exits_zero_when_main_returns(self) -> None:
        """A command that returns normally exits 0 through ``os._exit``."""
        with (
            patch("nab.cli.main") as mock_main,
            patch("nab.cli.os._exit") as mock_exit,
        ):
            console_entry()
        mock_main.assert_called_once()
        mock_exit.assert_called_once_with(0)

    def test_carries_the_exit_code_through(self) -> None:
        """``sys.exit(2)`` inside the command becomes exit status 2."""
        with (
            patch("nab.cli.main", side_effect=SystemExit(2)),
            patch("nab.cli.os._exit") as mock_exit,
        ):
            console_entry()
        mock_exit.assert_called_once_with(2)

    def test_flushes_the_streams_before_exiting(self) -> None:
        """Output written by the command is flushed, since ``os._exit`` will not."""
        flushed: list[str] = []
        with (
            patch("nab.cli.main"),
            patch.object(sys.stdout, "flush", lambda: flushed.append("out")),
            patch.object(sys.stderr, "flush", lambda: flushed.append("err")),
            patch("nab.cli.os._exit"),
        ):
            console_entry()
        assert flushed == ["out", "err"]

    def test_unflushed_output_exits_120(self) -> None:
        """A stdout flush that fails still flushes stderr and exits 120."""
        with (
            patch("nab.cli.main"),
            patch.object(sys.stdout, "flush", side_effect=OSError(28, "No space")),
            patch.object(sys.stderr, "flush") as mock_stderr_flush,
            patch("nab.cli.os._exit") as mock_exit,
        ):
            console_entry()
        mock_stderr_flush.assert_called_once()
        mock_exit.assert_called_once_with(120)

    def test_a_crash_is_left_to_the_interpreter(self) -> None:
        """Only ``SystemExit`` takes the fast exit; anything else propagates."""
        with (
            patch("nab.cli.main", side_effect=ValueError("boom")),
            patch("nab.cli.os._exit") as mock_exit,
            pytest.raises(ValueError, match="boom"),
        ):
            console_entry()
        mock_exit.assert_not_called()


class TestSystemExitStatus:
    """Tests for mapping a ``SystemExit`` code to a process status."""

    def test_bare_exit_is_zero(self) -> None:
        """``sys.exit()`` raises ``SystemExit(None)``, which means success."""
        assert _system_exit_status(None) == 0

    def test_integer_passes_through(self) -> None:
        """``sys.exit(3)`` becomes exit status 3."""
        assert _system_exit_status(3) == 3

    def test_message_prints_and_exits_one(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A string code is the message, printed to stderr as CPython does."""
        assert _system_exit_status("boom") == 1
        assert capsys.readouterr().err == "boom\n"


class _TtyStderr(io.StringIO):
    """A stderr stand-in that claims to be a terminal, so progress is allowed."""

    def isatty(self) -> bool:
        return True


class TestMainWiresOutputOptions:
    """main() turns the parsed global output flags into the run's output state.

    test_output.py covers the flags and the ``NAB_*`` variables on their own.
    These run the real entry point, so which parsed option reaches the printer
    and the log handler is pinned too.
    """

    def _run_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *flags: str
    ) -> tuple[Printer, _TtyStderr]:
        """Run ``main()`` over a stubbed lock; return its printer and stderr."""
        pyproject = _make_pyproject(tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "nab",
                *flags,
                "lock",
                str(pyproject),
                "--output",
                str(tmp_path / "pylock.toml"),
            ],
        )

        stderr = _TtyStderr()
        monkeypatch.setattr(sys, "stderr", stderr)

        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_stub_resolve_result(pins={}),
            ),
            patch("nab._lock.write_lock"),
        ):
            main()

        assert cli._printer is not None
        return cli._printer, stderr

    def test_default_run_reports_the_written_lockfile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no flags the run summary reaches stderr at the normal level."""
        printer, stderr = self._run_lock(tmp_path, monkeypatch)
        assert printer.verbosity is Verbosity.NORMAL
        assert "Wrote" in stderr.getvalue()

    def test_quiet_drops_the_run_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``-q`` puts the printer below the level ``done()`` writes at."""
        printer, stderr = self._run_lock(tmp_path, monkeypatch, "-q")
        assert printer.verbosity is Verbosity.QUIET
        assert "Wrote" not in stderr.getvalue()

    def test_default_run_keeps_the_engine_at_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without ``-v`` an engine INFO record is dropped and a WARNING shows."""
        _printer, stderr = self._run_lock(tmp_path, monkeypatch)
        logger = logging.getLogger("nab_project")
        logger.info("engine detail")
        logger.warning("engine note")
        assert "engine detail" not in stderr.getvalue()
        assert "warning: engine note" in stderr.getvalue()

    def test_debug_verbosity_lowers_the_engine_log_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``-vv`` reaches the log handler, not just the printer."""
        printer, _stderr = self._run_lock(tmp_path, monkeypatch, "-vv")
        assert printer.verbosity is Verbosity.DEBUG
        assert logging.getLogger("nab_project").getEffectiveLevel() == logging.DEBUG

    def test_color_always_paints_the_run_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--color always`` reaches the printer and overrides ``NO_COLOR``."""
        printer, stderr = self._run_lock(tmp_path, monkeypatch, "--color", "always")
        assert printer.color_enabled is True
        assert f"{GREEN}Wrote{RESET}" in stderr.getvalue()

    def test_color_always_reaches_the_log_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The handler is installed with the printer's colour decision."""
        _printer, stderr = self._run_lock(tmp_path, monkeypatch, "--color", "always")
        logging.getLogger("nab_project").warning("engine note")
        assert f"{YELLOW}warning:{RESET} engine note" in stderr.getvalue()

    def test_log_records_stay_plain_with_color_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With colour off the handler emits a plain ``warning:`` token."""
        _printer, stderr = self._run_lock(tmp_path, monkeypatch)
        logging.getLogger("nab_project").warning("engine note")
        assert "warning: engine note" in stderr.getvalue()
        assert "\033[" not in stderr.getvalue()

    def test_progress_allowed_on_a_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A normal-level run against a tty stderr allows the progress line."""
        printer, _stderr = self._run_lock(tmp_path, monkeypatch)
        assert printer.progress_allowed is True

    def test_no_progress_flag_blocks_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-progress`` reaches the printer that gates the progress line."""
        printer, _stderr = self._run_lock(tmp_path, monkeypatch, "--no-progress")
        assert printer.progress_allowed is False

    def test_env_verbosity_reaches_the_printer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``NAB_VERBOSITY`` applies, so ``main`` reads the real environment."""
        monkeypatch.setenv("NAB_VERBOSITY", "debug")
        printer, _stderr = self._run_lock(tmp_path, monkeypatch)
        assert printer.verbosity is Verbosity.DEBUG
        assert logging.getLogger("nab_project").getEffectiveLevel() == logging.DEBUG

    def test_env_no_progress_blocks_progress(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``NAB_NO_PROGRESS`` blocks the progress line without a flag."""
        monkeypatch.setenv("NAB_NO_PROGRESS", "1")
        printer, _stderr = self._run_lock(tmp_path, monkeypatch)
        assert printer.progress_allowed is False


class TestProgressReachesTheResolve:
    """``nab lock`` and ``nab download`` hand the resolve a progress reporter.

    test_output.py covers the reporter and the flags that decide whether it
    draws; nab-project's TestProgressReporting covers the sink
    ``resolve_for_targets`` threads to the coordinator. These run the real
    entry point against a terminal stderr, so the hand-off between the two is
    pinned as well.
    """

    def _run_main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
        command_patch: AbstractContextManager[object],
    ) -> tuple[list[ProgressReporter | None], str]:
        """Run ``main()`` over ``argv`` with stderr a terminal.

        Returns the reporters the resolve was handed, ``None`` for a command
        that passed none, and everything the run wrote to stderr.
        """
        received: list[ProgressReporter | None] = []

        def resolve(
            *_args: object,
            progress: ProgressReporter | None = None,
            **_kwargs: object,
        ) -> ResolveResult:
            """Stand in for ``resolve_for_targets``, driving the reporter.

            One fetch is enough: repaints are throttled, so a second would
            not reach stderr.
            """
            received.append(progress)
            if progress is not None:
                progress.on_fetch()
            return _stub_resolve_result(pins={})

        monkeypatch.setattr(sys, "argv", argv)
        stderr = _TtyStderr()
        monkeypatch.setattr(sys, "stderr", stderr)

        with (
            patch("nab.cli.resolve_for_targets", side_effect=resolve),
            command_patch,
        ):
            main()

        return received, stderr.getvalue()

    def test_lock_reporter_draws_the_resolve_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``nab lock`` paints the live resolve line on a terminal stderr."""
        pyproject = _make_pyproject(tmp_path)
        received, stderr = self._run_main(
            monkeypatch,
            ["nab", "lock", str(pyproject), "--output", str(tmp_path / "pylock.toml")],
            patch("nab._lock.write_lock"),
        )

        assert len(received) == 1
        assert isinstance(received[0], ProgressReporter)

        assert "Resolving... 1 fetched, 0 pinned" in stderr

    def test_download_reporter_draws_the_resolve_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``nab download`` paints the same live line while it resolves."""
        pyproject = _make_pyproject(tmp_path)
        received, stderr = self._run_main(
            monkeypatch,
            ["nab", "download", str(pyproject), "--output", str(tmp_path / "vendor")],
            patch(
                "nab._download.download_lock",
                return_value=MagicMock(written=(), skipped=()),
            ),
        )

        assert len(received) == 1
        assert isinstance(received[0], ProgressReporter)

        assert "Resolving... 1 fetched, 0 pinned" in stderr

    def test_no_progress_keeps_lock_from_drawing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-progress`` reaches the reporter ``nab lock`` hands over."""
        pyproject = _make_pyproject(tmp_path)
        received, stderr = self._run_main(
            monkeypatch,
            [
                "nab",
                "--no-progress",
                "lock",
                str(pyproject),
                "--output",
                str(tmp_path / "pylock.toml"),
            ],
            patch("nab._lock.write_lock"),
        )

        assert isinstance(received[0], ProgressReporter)
        assert "Resolving..." not in stderr

    def test_no_progress_keeps_download_from_drawing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-progress`` reaches the reporter ``nab download`` hands over."""
        pyproject = _make_pyproject(tmp_path)
        received, stderr = self._run_main(
            monkeypatch,
            [
                "nab",
                "--no-progress",
                "download",
                str(pyproject),
                "--output",
                str(tmp_path / "vendor"),
            ],
            patch(
                "nab._download.download_lock",
                return_value=MagicMock(written=(), skipped=()),
            ),
        )

        assert isinstance(received[0], ProgressReporter)
        assert "Resolving..." not in stderr

    def test_line_is_wiped_when_the_resolve_ends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live line is cleared, not left on the terminal.

        ``--output -`` sends the lock to stdout, so the wipe is the last
        thing written to stderr.
        """
        pyproject = _make_pyproject(tmp_path)
        _received, stderr = self._run_main(
            monkeypatch,
            ["nab", "lock", str(pyproject), "--output", "-"],
            patch("nab._lock.write_lock", return_value=""),
        )

        assert "Resolving... 1 fetched, 0 pinned" in stderr
        assert stderr.endswith("\r\033[K")


class TestMakeTransport:
    """Each HttpBackend value resolves to its corresponding transport class."""

    def test_httpx(self) -> None:
        """``"httpx"`` resolves to :class:`HttpxAsyncTransport`."""
        transport = _make_transport("httpx")
        try:
            assert isinstance(transport, HttpxAsyncTransport)
        finally:
            asyncio.run(transport.aclose())

    def test_urllib3(self) -> None:
        """``"urllib3"`` resolves to :class:`Urllib3AsyncTransport`."""
        transport = _make_transport("urllib3")
        try:
            assert isinstance(transport, Urllib3AsyncTransport)
        finally:
            asyncio.run(transport.aclose())

    def test_httpx_missing_exits_with_hint(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When httpx isn't installed, the CLI exits cleanly with a hint."""
        original_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "nab_index.httpx_async_transport":
                raise ImportError(name)
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(SystemExit) as info:
            _make_transport("httpx")
        assert info.value.code == 1
        assert "nab[httpx]" in capsys.readouterr().err

    def test_httpx_without_h2_exits_with_hint(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When httpx is installed without ``h2``, the CLI exits with a hint."""
        original_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "h2":
                raise ImportError(name)
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(SystemExit) as info:
            _make_transport("httpx")
        assert info.value.code == 1
        err = capsys.readouterr().err
        assert "nab[httpx]" in err
        assert "HTTP/2" in err
        assert "httpx is not installed" not in err


_HTTP_LIBRARY_PROBE = """
import sys

import nab.cli

roots = {name.partition(".")[0] for name in sys.modules}
leaked = sorted(roots & {"truststore", "urllib3"})
assert not leaked, f"importing nab.cli loaded {leaked}"
"""


class TestCliImportPath:
    """What importing :mod:`nab.cli` is allowed to pull in."""

    def test_urllib3_and_truststore_stay_unimported(self) -> None:
        """Neither library belongs on the path of a command that never fetches."""
        subprocess.run(  # noqa: S603 - the probe is this file's own source
            [sys.executable, "-c", _HTTP_LIBRARY_PROBE], check=True
        )


class TestDownloadCommand:
    """Tests for the download subcommand."""

    def test_invokes_download_lock(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "vendor"
        download_result = MagicMock(written=(out / "x.whl",), skipped=())
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            patch(
                "nab._download.download_lock", return_value=download_result
            ) as mock_dl,
        ):
            download(pyproject, output=out)
        mock_dl.assert_called_once()

    @pytest.mark.parametrize("offline", [True, False])
    def test_threads_offline_into_the_artefact_fetch(
        self, tmp_path: Path, offline: bool
    ) -> None:
        """``--offline`` must reach the download, not just the resolve."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "vendor"
        download_result = MagicMock(written=(), skipped=())
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            patch(
                "nab._download.download_lock", return_value=download_result
            ) as mock_dl,
        ):
            download(pyproject, output=out, offline=offline)
        assert mock_dl.call_args.kwargs["offline"] is offline

    def test_http_backend_reaches_both_transports(self, tmp_path: Path) -> None:
        """``--http-backend`` must reach the download, not just the resolve."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "vendor"
        download_result = MagicMock(written=(), skipped=())

        resolve_transport = MagicMock(name="resolve_transport")
        fetch_transport = MagicMock(name="fetch_transport")

        with (
            patch(
                "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
            ) as mock_resolve,
            patch(
                "nab._download.download_lock", return_value=download_result
            ) as mock_dl,
            patch(
                "nab.cli._make_transport",
                side_effect=[resolve_transport, fetch_transport],
            ) as mock_transport,
        ):
            download(pyproject, output=out, http_backend="httpx")

        backends = [call.args[0] for call in mock_transport.call_args_list]
        assert backends == ["httpx", "httpx"]

        assert mock_resolve.call_args.args[1] is resolve_transport
        assert mock_dl.call_args.args[1] is fetch_transport

    @pytest.mark.parametrize("cap", [1, 2])
    def test_max_concurrency_flag_caps_parallel_fetches(
        self, tmp_path: Path, cap: int
    ) -> None:
        """``--max-concurrency N`` holds the download to N fetches at a time."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "vendor"
        result, payloads = _fetchable_resolve_result(4)
        transport = _ConcurrencyProbeTransport(payloads)

        with (
            patch("nab.cli.resolve_for_targets", return_value=result),
            patch("nab.cli._make_transport", return_value=transport),
        ):
            download(pyproject, output=out, max_concurrency=cap)

        assert transport.peak == cap
        assert len(list(out.iterdir())) == len(payloads)

    def test_env_max_concurrency_caps_parallel_fetches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``NAB_MAX_CONCURRENCY`` caps the download the way the flag does."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "vendor"
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )
        monkeypatch.setenv("NAB_MAX_CONCURRENCY", "2")

        result, payloads = _fetchable_resolve_result(4)
        transport = _ConcurrencyProbeTransport(payloads)
        with (
            patch("nab.cli.resolve_for_targets", return_value=result),
            patch("nab.cli._make_transport", return_value=transport),
        ):
            download(pyproject, output=out)

        assert transport.peak == 2

    def test_project_override_uses_download_wording(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A CLI PROJECT override on ``nab download`` uses the no-lock wording."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "vendor"
        download_result = MagicMock(written=(out / "x.whl",), skipped=())
        monkeypatch.setattr(
            "nab.cli._config_search_roots",
            lambda p: SourceRoots(project_dir=p.parent, pyproject=p),
        )
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            patch("nab._download.download_lock", return_value=download_result),
        ):
            download(pyproject, output=out, project_resolution="lowest")
        err = capsys.readouterr().err
        assert "the values below reflect that override" in err
        assert "the lock they produce" not in err
        assert "--project-resolution -> lowest" in err

    def test_local_source_naming_another_project_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A local source whose tree is a different project exits 1, not a traceback."""
        pyproject = _make_pyproject(
            tmp_path, _mismatched_local_source_project(tmp_path)
        )
        with pytest.raises(SystemExit, match="1"):
            download(pyproject, offline=True, output=tmp_path / "vendor", cache=False)
        err = capsys.readouterr().err
        assert err.splitlines() == [
            _source_name_mismatch_message(tmp_path, "cannot download")
        ]

    def test_universal_mode_downloads_all_tuples(self, tmp_path: Path) -> None:
        """Universal mode re-resolves the matrix and downloads the union."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "vendor"
        download_result = MagicMock(written=(out / "foo.whl",), skipped=())
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_multi_tuple_universal_result(),
            ),
            patch(
                "nab._download.download_lock", return_value=download_result
            ) as mock_dl,
        ):
            download(pyproject, output=out)
        lock_input = mock_dl.call_args.args[0]
        assert set(lock_input.targets) == {
            "py311-linux_x86_64",
            "py312-linux_x86_64",
        }

    def test_universal_config_error_during_resolve_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ConfigError out of the universal download path exits 1 cleanly."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=ConfigError(
                    "exactly one of [extra 'cpu', extra 'gpu'] must be selected"
                ),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        err = capsys.readouterr().err
        assert "in [tool.nab]:" in err
        assert "exactly one" in err

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_max_concurrency_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: int
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            download(pyproject, max_concurrency=bad)
        assert "--max-concurrency must be at least 1" in capsys.readouterr().err

    def test_resolution_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets", side_effect=ResolutionError("conflict")
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        assert "resolution failed" in capsys.readouterr().err

    def test_missing_extra_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A root extra the package does not declare exits 1, not a traceback."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                side_effect=MissingExtraError(
                    "foo==1.0 does not provide extra 'nonexistent'"
                ),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        err = capsys.readouterr().err
        assert "does not provide extra 'nonexistent'" in err
        assert "Traceback" not in err

    def test_missing_dependencies_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_for_targets", side_effect=KeyError("dependencies")),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        assert "no [project].dependencies" in capsys.readouterr().err

    def test_missing_hash_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets", side_effect=MissingHashError("no hash")
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        assert "cannot download" in capsys.readouterr().err

    def test_download_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            patch(
                "nab._download.download_lock", side_effect=DownloadError("sha mismatch")
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        assert "download failed" in capsys.readouterr().err

    def test_output_is_existing_file_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--output colliding with an existing file exits cleanly, not a traceback."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "wheels"
        out.write_text("not a directory")
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject, output=out)
        assert "cannot write to output directory" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"all_groups": True},
            {"all_extras": True},
            {"groups": ("g",)},
            {"extras": ("e",)},
        ],
    )
    def test_directory_path_exits(
        self,
        kwargs: dict[str, object],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A directory path exits 1 with a clean message, not a traceback.

        The group/extra selection flags read the path before the config
        load, so they must reject a directory just as cleanly.
        """
        with pytest.raises(SystemExit, match="1"):
            download(tmp_path, **kwargs)
        assert "is a directory" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"all_groups": True},
            {"all_extras": True},
            {"groups": ("dev",)},
            {"extras": ("e",)},
        ],
    )
    def test_pylock_path_exits(
        self,
        kwargs: dict[str, object],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``nab download`` names a lock as one under every selection flag.

        Only the bare arm reaches the guard through the config load; the
        selection flags read the path first and have to say the same thing.
        """
        pylock = _make_pylock_with_groups(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            download(pylock, **kwargs)
        assert "is a PEP 751 lockfile" in capsys.readouterr().err

    def test_extras_flag_forwarded_to_resolver(self, tmp_path: Path) -> None:
        # ``--extras`` is required for ``exactly_one`` / ``at_least_one``
        # conflict projects; the resolver must see the selected names.
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\ndependencies = ["foo"]\n'
            "[project.optional-dependencies]\ncpu = []\ngpu = []\n",
        )
        download_result = MagicMock(written=(), skipped=())
        with (
            patch(
                "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
            ) as mock_resolve,
            patch("nab._download.download_lock", return_value=download_result),
        ):
            download(pyproject, extras=("cpu",))
        assert mock_resolve.call_args.kwargs["extras"] == ("cpu",)

    def test_groups_flag_forwarded_to_resolver(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\ndependencies = ["foo"]\n'
            "[dependency-groups]\ndev = []\n",
        )
        download_result = MagicMock(written=(), skipped=())
        with (
            patch(
                "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
            ) as mock_resolve,
            patch("nab._download.download_lock", return_value=download_result),
        ):
            download(pyproject, groups=("dev",))
        assert mock_resolve.call_args.kwargs["groups"] == ("dev",)

    def test_all_extras_expands_to_every_extra(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\ndependencies = ["foo"]\n'
            "[project.optional-dependencies]\ncpu = []\ngpu = []\n",
        )
        download_result = MagicMock(written=(), skipped=())
        with (
            patch(
                "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
            ) as mock_resolve,
            patch("nab._download.download_lock", return_value=download_result),
        ):
            download(pyproject, all_extras=True)
        assert set(mock_resolve.call_args.kwargs["extras"]) == {"cpu", "gpu"}

    def test_all_groups_expands_to_every_group(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\ndependencies = ["foo"]\n'
            "[dependency-groups]\ndev = []\ntest = []\n",
        )
        download_result = MagicMock(written=(), skipped=())
        with (
            patch(
                "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
            ) as mock_resolve,
            patch("nab._download.download_lock", return_value=download_result),
        ):
            download(pyproject, all_groups=True)
        assert set(mock_resolve.call_args.kwargs["groups"]) == {"dev", "test"}

    def test_universal_forwards_selection(self, tmp_path: Path) -> None:
        pyproject = _universal_pyproject(tmp_path)
        download_result = MagicMock(written=(), skipped=())
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_multi_tuple_universal_result(),
            ) as mock_resolve,
            patch("nab._download.download_lock", return_value=download_result),
        ):
            download(pyproject, extras=("docs",))
        assert mock_resolve.call_args.kwargs["extras"] == ("docs",)
