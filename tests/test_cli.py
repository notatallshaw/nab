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
    _emit_specific,
    _emit_universal_pylock,
    _resolve_extra_selection,
    _resolve_group_selection,
    lock,
)
from nab.cli import (
    _default_cache_dir,
    _make_transport,
    app,
    main,
)
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.urllib3_async_transport import Urllib3AsyncTransport
from nab_python._vendor.packaging.pylock import Pylock
from nab_python._vendor.packaging.version import Version
from nab_python.download import DownloadError
from nab_python.lockfile import (
    IndexPin,
    LockInput,
    MissingHashError,
    SdistArtifact,
    WheelArtifact,
)
from nab_python.provider import ResolutionStrategy, UnsupportedVcsError
from nab_python.resolve import ResolutionResult
from nab_python.universal.matrix import Matrix, MatrixTuple
from nab_python.universal.resolve import TupleResult, UniversalResult
from nab_python.universal.wheel_selection import PlatformSpec
from nab_resolver.resolver import ResolutionError

V = Version

_LINUX_311_ENV = {
    "python_version": "3.11",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}


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


def _stub_resolution_result(
    *, version: str = "1.0", pins: dict[str, Version] | None = None
) -> ResolutionResult:
    """Build a real :class:`ResolutionResult` with a populated lock input."""
    real_pins = pins if pins is not None else {"foo": V(version)}
    lock_input = LockInput(
        pins={name: _foo_index_pin(str(ver), name) for name, ver in real_pins.items()},
    )
    return ResolutionResult(pins=real_pins, lock_input=lock_input)


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


def _universal_result(*, success: bool, error: str | None = None) -> UniversalResult:
    """Build a real :class:`UniversalResult` with one matrix tuple."""
    matrix = Matrix(python="==3.11", platforms=("linux_x86_64",))
    tup = MatrixTuple(
        python_version="3.11",
        platform_id="linux_x86_64",
        environment=dict(_LINUX_311_ENV),
        platform_spec=PlatformSpec("linux_x86_64"),
    )
    lock_input = LockInput(pins={"foo": _foo_index_pin()}) if success else None
    tr = TupleResult(
        tuple_=tup,
        success=success,
        pins={"foo": V("1.0")} if success else {},
        error=error,
        lock_input=lock_input,
    )
    return UniversalResult(matrix=matrix, tuple_results=[tr])


def _multi_tuple_universal_result() -> UniversalResult:
    """Build a successful UniversalResult with two tuples (3.11 and 3.12)."""
    matrix = Matrix(python=">=3.11,<3.13", platforms=("linux_x86_64",))
    tuples = []
    results = []
    for py_minor in ("3.11", "3.12"):
        env = {**_LINUX_311_ENV, "python_version": py_minor}
        tup = MatrixTuple(
            python_version=py_minor,
            platform_id="linux_x86_64",
            environment=env,
            platform_spec=PlatformSpec("linux_x86_64"),
        )
        tuples.append(tup)
        results.append(
            TupleResult(
                tuple_=tup,
                success=True,
                pins={"foo": V("1.0")},
                error=None,
                lock_input=LockInput(pins={"foo": _foo_index_pin()}),
            )
        )
    return UniversalResult(matrix=matrix, tuple_results=results)


class TestLockCommandSpecific:
    """Tests for `nab lock` in single-environment mode."""

    def test_pylock_default(self, tmp_path: Path) -> None:
        """Default format writes a real pylock.toml at the requested path."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
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
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
            lock(pyproject)
        assert (tmp_path / "pylock.toml").exists()

    def test_requirements_default_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No --output: requirements format defaults to requirements.txt."""
        monkeypatch.chdir(tmp_path)
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
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
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
            lock(pyproject, format="requirements-without-hashes")
        text = (tmp_path / "requirements.txt").read_text()
        assert "foo==1.0" in text
        assert "--hash" not in text

    def test_requirements_writes_to_file(self, tmp_path: Path) -> None:
        """`requirements` format renders --hash lines."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
            lock(pyproject, output=out, format="requirements")
        text = out.read_text()
        assert "foo==1.0" in text
        assert "--hash=sha256:" in text

    def test_requirements_without_hashes_writes_to_file(self, tmp_path: Path) -> None:
        """requirements-without-hashes renders one name==version per line."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
            lock(pyproject, output=out, format="requirements-without-hashes")
        text = out.read_text()
        assert text.strip() == "foo==1.0"

    def test_pylock_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--output -` routes pylock format to stdout."""
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
            lock(pyproject, output=Path("-"))
        out = capsys.readouterr().out
        assert 'lock-version = "1.0"' in out
        assert 'name = "foo"' in out

    def test_requirements_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--output -` routes requirements format to stdout."""
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
            lock(pyproject, output=Path("-"), format="requirements")
        out = capsys.readouterr().out
        assert "foo==1.0" in out
        assert "--hash=sha256:" in out

    def test_requirements_without_hashes_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--output -` routes requirements-without-hashes to stdout."""
        pyproject = _make_pyproject(tmp_path)
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
            lock(pyproject, output=Path("-"), format="requirements-without-hashes")
        assert capsys.readouterr().out.strip() == "foo==1.0"

    def test_resolution_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_pyproject", side_effect=ResolutionError("conflict")),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "Resolution failed" in capsys.readouterr().err

    def test_unsupported_vcs_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_pyproject",
                side_effect=UnsupportedVcsError("refusing direct-URL requirement"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "refusing direct-URL requirement" in capsys.readouterr().err

    def test_missing_dependencies_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_pyproject", side_effect=KeyError("dependencies")),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject)
        assert "no [project].dependencies" in capsys.readouterr().err

    def test_missing_hash_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_pyproject", side_effect=MissingHashError("no hash")),
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
                "nab.cli.resolve_pyproject",
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

    def test_malformed_toml_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A TOML syntax error reports a clean message, not a traceback."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"\n')
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, output=Path("-"))
        assert "is not valid TOML" in capsys.readouterr().err

    def test_resolution_flag_threads_to_resolver(self, tmp_path: Path) -> None:
        """``--resolution lowest`` reaches resolve_pyproject as the enum."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_pyproject", return_value=_stub_resolution_result()
        ) as mock_resolve:
            lock(pyproject, output=out, resolution="lowest")
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs["resolution_strategy"] is ResolutionStrategy.LOWEST

    def test_resolution_flag_default_none(self, tmp_path: Path) -> None:
        """No --resolution: resolve_pyproject sees ``None`` (config wins)."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_pyproject", return_value=_stub_resolution_result()
        ) as mock_resolve:
            lock(pyproject, output=out)
        assert mock_resolve.call_args.kwargs["resolution_strategy"] is None


class TestLockCommandUniversal:
    """Tests for `nab lock` in universal mode."""

    def test_pylock_writes_universal_lock(self, tmp_path: Path) -> None:
        """Universal + pylock format runs the real merge + write pipeline."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_universal_pyproject",
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
            "nab.cli.resolve_universal_pyproject",
            return_value=_universal_result(success=True),
        ):
            lock(pyproject, output=out, groups=("test",))
        pylock = Pylock.from_dict(tomli.loads(out.read_text()))
        assert pylock.dependency_groups == ["test"]
        assert pylock.default_groups == ["dev"]

    def test_offline_and_http_backend_passed_to_universal(self, tmp_path: Path) -> None:
        """--http-backend and --offline reach resolve_universal_pyproject."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_universal_pyproject",
            return_value=_universal_result(success=True),
        ) as mock_resolve:
            lock(pyproject, output=out, http_backend="urllib3", offline=True)
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs["offline"] is True
        assert kwargs["transport"] is not None

    def test_requirements_with_hashes_single_tuple_to_file(
        self, tmp_path: Path
    ) -> None:
        """Single-tuple matrix + fixed output path writes that tuple's pins."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with patch(
            "nab.cli.resolve_universal_pyproject",
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
            "nab.cli.resolve_universal_pyproject",
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
            "nab.cli.resolve_universal_pyproject",
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
                "nab.cli.resolve_universal_pyproject",
                return_value=_universal_result(success=True),
            ),
            patch("nab.cli.write_lock", side_effect=MissingHashError("no hash")),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        assert "Cannot lock" in capsys.readouterr().err

    def test_per_tuple_pins_to_stdout_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Universal + requirements-without-hashes prints per-tuple blocks."""
        pyproject = _universal_pyproject(tmp_path)
        with patch(
            "nab.cli.resolve_universal_pyproject",
            return_value=_universal_result(success=True),
        ):
            lock(pyproject, format="requirements-without-hashes")
        captured = capsys.readouterr()
        assert "experimental" in captured.err
        assert "# py311-linux_x86_64" in captured.out
        assert "foo==1.0" in captured.out

    def test_per_tuple_pins_to_explicit_file_single_tuple(self, tmp_path: Path) -> None:
        """Single-tuple matrix + fixed path: just the pins, no header.

        Same shape as a single-environment requirements file.
        """
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "pins.txt"
        with patch(
            "nab.cli.resolve_universal_pyproject",
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
                "nab.cli.resolve_universal_pyproject",
                return_value=_universal_result(success=True),
            ),
            patch(
                "nab.cli.merge_universal_lock_inputs",
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
                "nab.cli.resolve_universal_pyproject",
                return_value=_universal_result(success=False, error="conflict"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "#   conflict" in out

    def test_failed_tuple_multi_line_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Multi-line errors render as one comment line per source line."""
        pyproject = _universal_pyproject(tmp_path)
        multi = "first line\nsecond line\nthird line"
        with (
            patch(
                "nab.cli.resolve_universal_pyproject",
                return_value=_universal_result(success=False, error=multi),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        out = capsys.readouterr().out
        assert "#   first line" in out
        assert "#   second line" in out
        assert "#   third line" in out

    def test_failed_tuple_no_error_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failed tuple with no error string still emits the FAILED line."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_universal_pyproject",
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
                "nab.cli.resolve_universal_pyproject",
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
                "nab.cli.resolve_universal_pyproject",
                side_effect=LookupError("unknown group 'ghost'"),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        assert "unknown group" in capsys.readouterr().err

    def test_requirements_missing_hash_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``MissingHashError`` raised by the requirements writer exits 1."""
        pyproject = _universal_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_universal_pyproject",
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

        ``_print_universal_blocks`` runs whenever any tuple failed; it
        must print the failing tuple's ``# label: FAILED`` block AND
        each successful tuple's ``# label`` + pins block.
        """
        pyproject = _universal_pyproject(tmp_path)
        ok_tuple = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment=dict(_LINUX_311_ENV),
            platform_spec=PlatformSpec("linux_x86_64"),
        )
        bad_tuple = MatrixTuple(
            python_version="3.11",
            platform_id="windows_amd64",
            environment=dict(_LINUX_311_ENV),
            platform_spec=PlatformSpec("windows_amd64"),
        )
        ok_tr = TupleResult(
            tuple_=ok_tuple,
            success=True,
            pins={"foo": V("1.0")},
            lock_input=LockInput(pins={"foo": _foo_index_pin()}),
        )
        bad_tr = TupleResult(
            tuple_=bad_tuple,
            success=False,
            pins={},
            error="conflict",
            lock_input=None,
        )
        mixed = UniversalResult(
            matrix=Matrix(
                python="==3.11",
                platforms=("linux_x86_64", "windows_amd64"),
            ),
            tuple_results=[ok_tr, bad_tr],
        )
        with (
            patch("nab.cli.resolve_universal_pyproject", return_value=mixed),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes")
        out = capsys.readouterr().out
        assert "# py311-linux_x86_64" in out
        assert "foo==1.0" in out
        assert "# py311-windows_amd64: FAILED" in out

    def test_template_writes_one_file_per_tuple(self, tmp_path: Path) -> None:
        """``{python_version}`` in --output expands to one file per tuple."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "constraints-{python_version}.txt"
        with patch(
            "nab.cli.resolve_universal_pyproject",
            return_value=_multi_tuple_universal_result(),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        a = tmp_path / "constraints-3.11.txt"
        b = tmp_path / "constraints-3.12.txt"
        assert a.read_text().strip() == "foo==1.0"
        assert b.read_text().strip() == "foo==1.0"
        # The literal templated path is NOT written.
        assert not (tmp_path / "constraints-{python_version}.txt").exists()

    def test_template_with_platform_id(self, tmp_path: Path) -> None:
        """``{platform_id}`` is also a valid template variable."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "constraints-{python_version}-{platform_id}.txt"
        with patch(
            "nab.cli.resolve_universal_pyproject",
            return_value=_multi_tuple_universal_result(),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        assert (tmp_path / "constraints-3.11-linux_x86_64.txt").exists()
        assert (tmp_path / "constraints-3.12-linux_x86_64.txt").exists()

    def test_template_with_hashes(self, tmp_path: Path) -> None:
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "req-{python_version}.txt"
        with patch(
            "nab.cli.resolve_universal_pyproject",
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
                "nab.cli.resolve_universal_pyproject",
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
        matrix = Matrix(python="==3.11", platforms=("linux_x86_64", "windows_amd64"))
        tuples_results = []
        for platform_id in ("linux_x86_64", "windows_amd64"):
            env = {**_LINUX_311_ENV, "python_version": "3.11"}
            tup = MatrixTuple(
                python_version="3.11",
                platform_id=platform_id,
                environment=env,
                platform_spec=PlatformSpec(platform_id),
            )
            tuples_results.append(
                TupleResult(
                    tuple_=tup,
                    success=True,
                    pins={"foo": V("1.0")},
                    error=None,
                    lock_input=LockInput(pins={"foo": _foo_index_pin()}),
                )
            )
        result = UniversalResult(matrix=matrix, tuple_results=tuples_results)
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "constraints-{python_version}.txt"
        with (
            patch("nab.cli.resolve_universal_pyproject", return_value=result),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, format="requirements-without-hashes", output=out)
        err = capsys.readouterr().err
        assert "both map to" in err
        assert "{platform_id}" in err
        assert not (tmp_path / "constraints-3.11.txt").exists()

    def test_template_with_one_tuple_writes_one_file(self, tmp_path: Path) -> None:
        """A template with a single-tuple matrix still works."""
        pyproject = _universal_pyproject(tmp_path)
        out = tmp_path / "constraints-{python_version}.txt"
        with patch(
            "nab.cli.resolve_universal_pyproject",
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
                "nab.cli.resolve_universal_pyproject",
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
        good_tup = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={**_LINUX_311_ENV, "python_version": "3.11"},
            platform_spec=PlatformSpec("linux_x86_64"),
        )
        bad_tup = MatrixTuple(
            python_version="3.12",
            platform_id="linux_x86_64",
            environment={**_LINUX_311_ENV, "python_version": "3.12"},
            platform_spec=PlatformSpec("linux_x86_64"),
        )
        good_tr = TupleResult(
            tuple_=good_tup,
            success=True,
            pins={"foo": V("1.0")},
            error=None,
            lock_input=LockInput(pins={"foo": _foo_index_pin()}),
        )
        bad_tr = TupleResult(
            tuple_=bad_tup,
            success=False,
            pins={},
            error="boom",
            lock_input=None,
        )
        mixed = UniversalResult(
            matrix=Matrix(python=">=3.11,<3.13", platforms=("linux_x86_64",)),
            tuple_results=[good_tr, bad_tr],
        )
        out = tmp_path / "constraints-{python_version}.txt"
        # The lock fails because the matrix has a failure; check the file
        # for the successful tuple is still printed via the stdout
        # multi-block path (not via this test).
        with (
            patch("nab.cli.resolve_universal_pyproject", return_value=mixed),
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
    def _alpha_and_foo_result() -> ResolutionResult:
        """A specific-mode result with a workspace pin (alpha) and foo."""
        return ResolutionResult(
            pins={"alpha": V("0"), "foo": V("1.0")},
            lock_input=LockInput(
                pins={
                    "alpha": _foo_index_pin("0", "alpha"),
                    "foo": _foo_index_pin("1.0", "foo"),
                }
            ),
        )

    @staticmethod
    def _alpha_and_foo_universal() -> UniversalResult:
        """A universal result with alpha + foo on a single tuple."""
        matrix = Matrix(python="==3.11", platforms=("linux_x86_64",))
        tup = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment=dict(_LINUX_311_ENV),
            platform_spec=PlatformSpec("linux_x86_64"),
        )
        pins = {"alpha": V("0"), "foo": V("1.0")}
        lock_input = LockInput(
            pins={
                "alpha": _foo_index_pin("0", "alpha"),
                "foo": _foo_index_pin("1.0", "foo"),
            }
        )
        tr = TupleResult(
            tuple_=tup, success=True, pins=pins, error=None, lock_input=lock_input
        )
        return UniversalResult(matrix=matrix, tuple_results=[tr])

    def test_specific_pylock_drops_workspace_pin(self, tmp_path: Path) -> None:
        """Specific mode + pylock with the flag set drops the workspace pin."""
        pyproject = _workspace_pyproject(tmp_path)
        out = tmp_path / "pylock.toml"
        with patch(
            "nab.cli.resolve_pyproject", return_value=self._alpha_and_foo_result()
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
            "nab.cli.resolve_pyproject", return_value=self._alpha_and_foo_result()
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
            "nab.cli.resolve_pyproject", return_value=self._alpha_and_foo_result()
        ):
            lock(pyproject, output=out, no_emit_workspace=True)
        assert "(1 packages)" in capsys.readouterr().err

    def test_specific_requirements_drops_workspace_pin(self, tmp_path: Path) -> None:
        """Requirements format also honours --no-emit-workspace."""
        pyproject = _workspace_pyproject(tmp_path)
        out = tmp_path / "requirements.txt"
        with patch(
            "nab.cli.resolve_pyproject", return_value=self._alpha_and_foo_result()
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
            "nab.cli.resolve_universal_pyproject",
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
            "nab.cli.resolve_universal_pyproject",
            return_value=self._alpha_and_foo_universal(),
        ):
            lock(
                pyproject,
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
            "nab.cli.resolve_universal_pyproject",
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
            "nab.cli.resolve_universal_pyproject",
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
            "nab.cli.resolve_pyproject", return_value=self._alpha_and_foo_result()
        ):
            lock(pyproject, output=out, no_emit_workspace=True)
        text = out.read_text()
        assert 'name = "alpha"' in text
        assert 'name = "foo"' in text


class TestRelockDiffSummary:
    """``_emit_specific`` reports what changed against the prior pylock."""

    def test_first_lock_prints_plain_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "pylock.toml"
        _emit_specific(
            _stub_resolution_result(pins={"foo": V("1.0")}),
            format="pylock",
            output=out,
            provenance=None,
        )
        err = capsys.readouterr().err
        assert err.strip().endswith("(1 packages)")

    def test_relock_reports_added_upgraded_removed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "pylock.toml"
        _emit_specific(
            _stub_resolution_result(pins={"foo": V("1.0"), "bar": V("1.0")}),
            format="pylock",
            output=out,
            provenance=None,
        )
        capsys.readouterr()
        # foo upgraded 1.0 -> 2.0, bar removed, baz added.
        _emit_specific(
            _stub_resolution_result(pins={"foo": V("2.0"), "baz": V("1.0")}),
            format="pylock",
            output=out,
            provenance=None,
        )
        err = capsys.readouterr().err.strip()
        assert err.endswith("(2 packages: 1 added, 1 upgraded, 1 removed)")

    def test_relock_reports_downgrade(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "pylock.toml"
        _emit_specific(
            _stub_resolution_result(pins={"foo": V("2.0")}),
            format="pylock",
            output=out,
            provenance=None,
        )
        capsys.readouterr()
        _emit_specific(
            _stub_resolution_result(pins={"foo": V("1.0")}),
            format="pylock",
            output=out,
            provenance=None,
        )
        assert capsys.readouterr().err.strip().endswith("(1 packages: 1 downgraded)")

    def test_relock_unchanged_prints_plain_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A re-lock with identical pins prints no diff suffix."""
        out = tmp_path / "pylock.toml"
        for _ in range(2):
            _emit_specific(
                _stub_resolution_result(pins={"foo": V("1.0")}),
                format="pylock",
                output=out,
                provenance=None,
            )
        assert capsys.readouterr().err.strip().endswith("(1 packages)")

    def test_unparseable_prior_falls_back_to_plain_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "pylock.toml"
        out.write_text("this is not valid toml === {[\n")
        _emit_specific(
            _stub_resolution_result(pins={"foo": V("1.0")}),
            format="pylock",
            output=out,
            provenance=None,
        )
        assert capsys.readouterr().err.strip().endswith("(1 packages)")

    def test_stdout_emits_no_diff(self, capsys: pytest.CaptureFixture[str]) -> None:
        _emit_specific(
            _stub_resolution_result(pins={"foo": V("1.0")}),
            format="pylock",
            output=Path("-"),
            provenance=None,
        )
        captured = capsys.readouterr()
        assert "added" not in captured.err
        assert "packages" not in captured.err

    def test_requirements_format_emits_no_diff(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A requirements re-lock keeps the plain line; only pylock diffs."""
        out = tmp_path / "requirements.txt"
        for _ in range(2):
            _emit_specific(
                _stub_resolution_result(pins={"foo": V("1.0")}),
                format="requirements",
                output=out,
                provenance=None,
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
                "nab.cli.resolve_universal_pyproject",
                return_value=_universal_result(success=True),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            lock(pyproject, output=tmp_path / "pylock-dev.toml")
        assert "PEP 751" in capsys.readouterr().err

    def test_specific_accepts_named_pylock(self, tmp_path: Path) -> None:
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "pylock.dev.toml"
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
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
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
            lock(pyproject, output=Path("-"))
        assert 'lock-version = "1.0"' in capsys.readouterr().out

    def test_requirements_format_skips_validation(self, tmp_path: Path) -> None:
        """A non-pylock format is free to use any output name."""
        pyproject = _make_pyproject(tmp_path)
        out = tmp_path / "constraints.txt"
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
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
            '[project]\ndependencies = ["foo"]\n'
            '[tool.nab]\nuploaded-prior-to = "P4D"\n',
        )
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
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
            '[project]\ndependencies = ["foo"]\n'
            '[tool.nab]\nuploaded-prior-to = "P4D"\n',
        )
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
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
        with patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()):
            lock(pyproject, output=out)
        from nab_python.lockfile import read_lockfile_anchor

        assert read_lockfile_anchor(out) == absolute


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
            _resolve_group_selection(
                tmp_path / "missing.toml", groups=(), all_groups=True
            )
        assert "not found" in capsys.readouterr().err

    def test_all_extras_missing_file_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Symmetric guard for ``--all-extras``."""
        with pytest.raises(SystemExit, match="1"):
            _resolve_extra_selection(
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
        result = _resolve_group_selection(pyproject, groups=(), all_groups=True)
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
        result = _resolve_extra_selection(pyproject, extras=(), all_extras=True)
        assert sorted(result) == ["ci", "test"]


class TestEmitHelpers:
    """Helpers accept ``provenance=None``.

    ``_emit_specific`` and ``_emit_universal_pylock`` skip the
    provenance assignment when called with ``None`` so callers
    that do not want a ``[tool.nab]`` block can pass nothing.
    """

    def test_emit_specific_without_provenance(self, tmp_path: Path) -> None:
        out = tmp_path / "pylock.toml"
        _emit_specific(
            _stub_resolution_result(),
            format="pylock",
            output=out,
            provenance=None,
        )
        # The output is a valid pylock without [tool.nab] provenance.
        text = out.read_text()
        assert 'lock-version = "1.0"' in text
        assert "[tool.nab]" not in text

    def test_emit_universal_pylock_without_provenance(self, tmp_path: Path) -> None:
        result = _universal_result(success=True)
        out = tmp_path / "pylock.toml"
        _emit_universal_pylock(result, output=out, provenance=None)
        text = out.read_text()
        assert 'lock-version = "1.0"' in text


class TestCacheFlags:
    """Tests for --cache-dir, --no-cache, --offline."""

    def test_default_cache_dir_passed_through(self, tmp_path: Path) -> None:
        """No flags: cache_dir defaults to a real path, offline is False."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_pyproject",
                return_value=_stub_resolution_result(pins={}),
            ) as mock_resolve,
            patch("nab.cli.write_lock"),
        ):
            lock(pyproject, output=tmp_path / "pylock.toml")
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs["cache_dir"] is not None
        assert kwargs["offline"] is False

    def test_explicit_cache_dir_passed_through(self, tmp_path: Path) -> None:
        """An explicit --cache-dir flows through to resolve_pyproject."""
        pyproject = _make_pyproject(tmp_path)
        cache = tmp_path / "mycache"
        with (
            patch(
                "nab.cli.resolve_pyproject",
                return_value=_stub_resolution_result(pins={}),
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
                "nab.cli.resolve_pyproject",
                return_value=_stub_resolution_result(pins={}),
            ) as mock_resolve,
            patch("nab.cli.write_lock"),
        ):
            lock(pyproject, cache=False, output=tmp_path / "pylock.toml")
        assert mock_resolve.call_args.kwargs["cache_dir"] is None

    def test_offline_passed_through(self, tmp_path: Path) -> None:
        """--offline flows through to resolve_pyproject."""
        pyproject = _make_pyproject(tmp_path)
        with (
            patch(
                "nab.cli.resolve_pyproject",
                return_value=_stub_resolution_result(pins={}),
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
        """main() delegates to app.cli()."""
        monkeypatch.setattr(sys, "argv", ["nab"])
        with patch("nab.cli.app") as mock_app:
            main()
        mock_app.cli.assert_called_once_with(prog="nab")

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
            patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()),
            patch("nab.cli.download_lock", return_value=download_result) as mock_dl,
        ):
            download(pyproject, output=out)
        mock_dl.assert_called_once()

    def test_universal_mode_rejected(self, tmp_path: Path) -> None:
        pyproject = _universal_pyproject(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            download(pyproject)

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
            patch("nab.cli.resolve_pyproject", side_effect=ResolutionError("conflict")),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        assert "Resolution failed" in capsys.readouterr().err

    def test_missing_dependencies_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_pyproject", side_effect=KeyError("dependencies")),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        assert "no [project].dependencies" in capsys.readouterr().err

    def test_missing_hash_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_pyproject", side_effect=MissingHashError("no hash")),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        assert "Cannot download" in capsys.readouterr().err

    def test_download_error_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pyproject = _make_pyproject(tmp_path)
        with (
            patch("nab.cli.resolve_pyproject", return_value=_stub_resolution_result()),
            patch("nab.cli.download_lock", side_effect=DownloadError("sha mismatch")),
            pytest.raises(SystemExit, match="1"),
        ):
            download(pyproject)
        assert "Download failed" in capsys.readouterr().err
