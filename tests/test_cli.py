"""Tests for the nab CLI entry point."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import importlib
import io
import runpy
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomli

from nab import _version as nab_version
from nab._download import download
from nab._lock import (
    _determine_lock_anchor,
    _drop_workspace_pins,
    _emit,
    _emit_pylock,
    lock,
    resolve_extra_selection,
    resolve_group_selection,
)
from nab.cli import (
    _default_cache_dir,
    _make_transport,
    _normalize_layered_bool_flags,
    app,
    main,
)
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.transport import HttpError
from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_python._vendor.packaging.pylock import Pylock
from nab_python._vendor.packaging.version import Version
from nab_python.config import ConfigError
from nab_python.config_sources import SourceRoots
from nab_python.download import DownloadError
from nab_python.lockfile import (
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
from nab_python.provider import (
    InvalidUploadTimeError,
    ResolutionStrategy,
    UnsupportedVcsError,
)
from nab_python.requirements_file import InvalidProjectRequirementError
from nab_python.resolve import ResolveResult, TargetResult
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget
from nab_resolver.resolver import ResolutionError

V = Version


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


def _make_pyproject(tmp_path: Path, body: str = "") -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(body or '[project]\ndependencies = ["foo"]\n')
    return pyproject


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
    """Build a successful ResolveResult with two tuples (3.11 and 3.12)."""
    tuples = tuple(_target(py_minor) for py_minor in ("3.11", "3.12"))
    return ResolveResult(
        targets=tuples,
        target_results=[_resolved(tup, {"foo": V("1.0")}) for tup in tuples],
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

    def test_requirements_writes_to_file(self, tmp_path: Path) -> None:
        """`requirements` format renders --hash lines."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=out, format="requirements")
        text = out.read_text()
        assert "foo==1.0" in text
        assert "--hash=sha256:" in text

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
        assert "Resolution failed: conflict" in capsys.readouterr().err

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
        assert "Resolution failed" in capsys.readouterr().err

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
        assert "Cannot lock" in capsys.readouterr().err

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
        assert "Cannot lock" in err
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
        assert "Cannot lock" in capsys.readouterr().err

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

    def test_malformed_toml_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A TOML syntax error reports a clean message, not a traceback."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"\n')
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=Path("-"))
        assert "is not valid TOML" in capsys.readouterr().err

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
        assert "Error in [tool.nab]:" in err
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
        assert "Config error:" in capsys.readouterr().err

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
        assert "Config error:" in err
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
        assert mock_resolve.call_args.kwargs["python_version"] == "3.11"

    def test_absent_flag_leaves_the_host_target(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
        ) as mock_resolve:
            lock(pyproject, output=out)
        assert mock_resolve.call_args.kwargs["python_version"] is None

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
                "nab.cli.download_lock",
                return_value=MagicMock(written=(out / "x.whl",), skipped=()),
            ),
        ):
            download(pyproject, output=out, python="3.11")
        assert mock_resolve.call_args.kwargs["python_version"] == "3.11"

    def test_download_rejects_it_in_universal_mode(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _universal_pyproject(tmp_path)
        with pytest.raises(SystemExit) as exc:
            download(pyproject, output=tmp_path / "wheels", python="3.11")
        assert exc.value.code == 1
        assert "--python is not supported in universal mode" in capsys.readouterr().err


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
        assert "Error in [tool.nab]:" in err
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
            patch("nab.cli.write_lock", side_effect=MissingHashError("no hash")),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        assert "Cannot lock" in capsys.readouterr().err

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
            patch("nab.cli.write_lock", side_effect=DisjointnessError(hint)),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        err = capsys.readouterr().err
        assert f"Error: {hint}\n" in err

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
                "nab.cli.write_lock",
                side_effect=DivergentBaseDependencyError(message),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        assert f"Error: {message}\n" in capsys.readouterr().err

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
        assert "Error:" in err
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
        assert "Cannot lock" in err
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
        assert "# py311-linux_x86_64" in captured.out
        assert "# py312-linux_x86_64" in captured.out
        assert "foo==1.0" in captured.out

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
                "nab.cli.build_lock_input",
                return_value=MagicMock(name="LockInput"),
            ),
            patch(
                "nab.cli.write_requirements_without_hashes",
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
                return_value=_universal_result(
                    success=False, error=ResolutionError("conflict")
                ),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "#   ResolutionError: conflict" in out

    def test_failed_tuple_multi_line_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Multi-line errors render as one comment line per source line."""
        pyproject = _universal_pyproject(tmp_path)
        multi = "first line\nsecond line\nthird line"
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(
                    success=False, error=ResolutionError(multi)
                ),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        out = capsys.readouterr().out
        assert "#   ResolutionError: first line" in out
        assert "#   second line" in out
        assert "#   third line" in out

    def test_failed_tuple_no_error_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failed tuple with no error string still emits the FAILED line."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_for_targets",
                return_value=_universal_result(success=False, error=None),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        assert "FAILED" in capsys.readouterr().out

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
        assert "Cannot lock" in err
        assert "503" in err

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
        assert "Cannot lock" in err
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
                "nab.cli.write_requirements_with_hashes",
                side_effect=MissingHashError("no hash"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements")
        assert "Cannot lock" in capsys.readouterr().err

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
        out = capsys.readouterr().out
        assert "# py311-linux_x86_64" in out
        assert "foo==1.0" in out
        assert "# py311-windows_amd64: FAILED" in out

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
        out = capsys.readouterr().out
        assert "foo==1.0" in out
        # The succeeded base pass contributes no block: only the failed
        # one renders, and the per-tuple labels stay distinct.
        assert "# base/py311-linux_x86_64: FAILED" not in out
        assert "# base/py312-linux_x86_64: FAILED" in out
        assert "#   ResolutionError: base unresolvable" in out
        assert "#   Diagnostics: missing" in out

    def test_template_writes_one_file_per_tuple(self, tmp_path: Path) -> None:
        """``{python_version}`` in --output expands to one file per tuple."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "constraints-{python_version}.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_multi_tuple_universal_result(),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        a = tmp_path / "constraints-3.11.txt"
        b = tmp_path / "constraints-3.12.txt"
        assert a.read_text().strip() == "foo==1.0"
        assert b.read_text().strip() == "foo==1.0"
        assert not (tmp_path / "constraints-{python_version}.txt").exists()

    def test_template_with_platform_id(self, tmp_path: Path) -> None:
        """``{platform_id}`` is also a valid template variable."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "constraints-{python_version}-{platform_id}.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_multi_tuple_universal_result(),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        assert (tmp_path / "constraints-3.11-linux_x86_64.txt").exists()
        assert (tmp_path / "constraints-3.12-linux_x86_64.txt").exists()

    def test_template_with_hashes(self, tmp_path: Path) -> None:
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}.txt"
        with patch(
            "nab.cli.resolve_for_targets",
            return_value=_multi_tuple_universal_result(),
        ):
            lock(pyproject, format="requirements", output=out)
        text = (tmp_path / "req-3.11.txt").read_text()
        assert "foo==1.0" in text
        assert "--hash=sha256:" in text

    def test_multi_tuple_without_template_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Multiple tuples + plain --output fails with a clear message."""
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
        assert "produced multiple tuples" in err
        assert "{python_version}" in err
        assert "{platform_id}" in err
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
                "nab.cli.write_requirements_with_hashes",
                side_effect=MissingHashError("no hash"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements", output=out)
        assert "Cannot lock" in capsys.readouterr().err

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

    def test_drop_filters_dependency_graph(self) -> None:
        """Dropped members vanish as graph keys and as edge targets."""
        target = ResolveTarget.for_host()
        lock_input = LockInput(
            targets={
                target.label: _target_lock(
                    target,
                    {"foo": V("1.0"), "bar": V("2.0")},
                    {
                        "foo": ("alpha", "bar"),
                        "bar": ("alpha",),
                        "alpha": ("bar",),
                    },
                )
            }
        )
        dropped = _drop_workspace_pins(lock_input, frozenset({"alpha"}))
        assert dropped.targets[target.label].dependencies == {"foo": ("bar",)}


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
        assert "Error in [tool.nab]" in capsys.readouterr().err

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
        assert "Workspace discovery error" in capsys.readouterr().err


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

    def test_relock_records_reused_anchor(self, tmp_path: Path) -> None:
        prior = tmp_path / "pylock.toml"
        prior.write_text(
            f"[tool.nab]\ncreated-at = {self._RECORDED.isoformat()}\n",
        )
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n[tool.nab]\nuploaded-prior-to = "P4D"\n',
        )
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=prior)
        # New pylock's [tool.nab].created-at must equal the prior anchor.
        from nab_python.lockfile import read_lockfile_anchor

        assert read_lockfile_anchor(prior) == self._RECORDED

    def test_upgrade_writes_fresh_anchor(self, tmp_path: Path) -> None:
        prior = tmp_path / "pylock.toml"
        prior.write_text(
            f"[tool.nab]\ncreated-at = {self._RECORDED.isoformat()}\n",
        )
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n[tool.nab]\nuploaded-prior-to = "P4D"\n',
        )
        with patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()):
            lock(pyproject, output=prior, upgrade=True)
        from nab_python.lockfile import read_lockfile_anchor

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
        from nab_python.lockfile import read_lockfile_anchor

        assert read_lockfile_anchor(out) == absolute


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


class TestLockedFlag:
    """``nab lock --locked`` re-resolves and verifies the committed pylock."""

    def _write_lock(self, pyproject: Path, out: Path, result: ResolveResult) -> None:
        with patch("nab.cli.resolve_for_targets", return_value=result):
            app.cli(args=["lock", str(pyproject), "--output", str(out)], prog="nab")

    def _run_locked(self, pyproject: Path, out: Path, result: ResolveResult) -> None:
        with patch("nab.cli.resolve_for_targets", return_value=result):
            app.cli(
                args=["lock", str(pyproject), "--output", str(out), "--locked"],
                prog="nab",
            )

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
            patch("nab.cli.render_lock", side_effect=MissingHashError("no hash")),
            pytest.raises(SystemExit) as exc,
        ):
            app.cli(
                args=["lock", str(pyproject), "--output", str(out), "--locked"],
                prog="nab",
            )
        assert exc.value.code == 1
        assert "Cannot lock" in capsys.readouterr().err
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
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A relative local-source path is emitted relative to the lock dir on
        # both passes, so an unchanged project stays up to date.
        (tmp_path / "vendor").mkdir()
        pyproject = _make_pyproject(
            tmp_path,
            '[project]\ndependencies = ["foo"]\n'
            "[[tool.nab.local-sources]]\n"
            'name = "foo"\npath = "vendor"\n',
        )
        out = tmp_path / "pylock.toml"
        result = _stub_resolve_result(pins={"foo": V("1.0")})
        self._write_lock(pyproject, out, result)
        capsys.readouterr()
        self._run_locked(pyproject, out, _stub_resolve_result(pins={"foo": V("1.0")}))
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
        _emit_pylock(lock_input, output=out)
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
        _emit_pylock(lock_input, output=tmp_path / "pylock.toml")
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
            patch("nab.cli.write_lock"),
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
            patch("nab.cli.write_lock"),
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
            patch("nab.cli.write_lock"),
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
            patch("nab.cli.write_lock"),
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
            patch("nab.cli.write_lock"),
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
            patch("nab.cli.write_lock"),
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
            patch("nab.cli.write_lock"),
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
        """Ctrl-C during ``app.cli()`` prints ``Aborted.`` and exits 130."""
        monkeypatch.setattr(sys, "argv", ["nab"])
        with patch("nab.cli.app") as mock_app:
            mock_app.cli.side_effect = KeyboardInterrupt
            with pytest.raises(SystemExit) as info:
                main()
        assert info.value.code == 130
        assert "Aborted." in capsys.readouterr().err


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


class TestDownloadCommand:
    """Tests for the download subcommand."""

    def test_invokes_download_lock(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "vendor"
        download_result = MagicMock(written=(out / "x.whl",), skipped=())
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            patch("nab.cli.download_lock", return_value=download_result) as mock_dl,
        ):
            download(pyproject, output=out)
        mock_dl.assert_called_once()

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
            patch("nab.cli.download_lock", return_value=download_result),
        ):
            download(pyproject, output=out, project_resolution="lowest")
        err = capsys.readouterr().err
        assert "the values below reflect that override" in err
        assert "the lock they produce" not in err
        assert "--project-resolution -> lowest" in err

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
            patch("nab.cli.download_lock", return_value=download_result) as mock_dl,
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
        assert "Error in [tool.nab]:" in err
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
        assert "Resolution failed" in capsys.readouterr().err

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
        assert "Cannot download" in capsys.readouterr().err

    def test_download_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_for_targets", return_value=_stub_resolve_result()),
            patch("nab.cli.download_lock", side_effect=DownloadError("sha mismatch")),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        assert "Download failed" in capsys.readouterr().err

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
            patch("nab.cli.download_lock", return_value=download_result),
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
            patch("nab.cli.download_lock", return_value=download_result),
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
            patch("nab.cli.download_lock", return_value=download_result),
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
            patch("nab.cli.download_lock", return_value=download_result),
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
            patch("nab.cli.download_lock", return_value=download_result),
        ):
            download(pyproject, extras=("docs",))
        assert mock_resolve.call_args.kwargs["extras"] == ("docs",)
