"""Flow tests for the fast-fail tier in ``nab lock --locked``.

Each case drives the real CLI against a committed pylock on disk.  Most mock
``nab.cli.resolve_for_targets``, so whether the resolver was called shows
whether the tier fired: a disqualifier never calls it, a fall-through does.
The invalid-invocation cases keep the real resolve, to pin the error it
reports when the tier defers.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nab.cli import app
from nab_python._vendor.packaging.version import Version
from nab_python.lockfile import (
    IndexPin,
    SdistArtifact,
    TargetLock,
    WheelArtifact,
)
from nab_python.resolve import ResolveResult, TargetResult
from nab_python.target import ResolveTarget

V = Version


def _index_pin(name: str, version: str) -> IndexPin:
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


def _result(pins: dict[str, str]) -> ResolveResult:
    target = ResolveTarget.for_host()
    lock = TargetLock(
        target=target,
        pins={name: _index_pin(name, version) for name, version in pins.items()},
    )
    return ResolveResult(
        targets=(target,),
        target_results=[
            TargetResult(
                target=target,
                success=True,
                pins={name: V(version) for name, version in pins.items()},
                lock=lock,
            )
        ],
    )


def _write_pyproject(tmp_path: Path, body: str) -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(body, encoding="utf-8")
    return pyproject


def _write_lock(pyproject: Path, out: Path, result: ResolveResult, *extra: str) -> None:
    with patch("nab.cli.resolve_for_targets", return_value=result):
        app.cli(args=["lock", str(pyproject), "--output", str(out), *extra], prog="nab")


def _locked_mock(result: ResolveResult | None = None) -> MagicMock:
    return MagicMock(
        return_value=result if result is not None else _result({"foo": "1.0"})
    )


def _run_locked(pyproject: Path, out: Path, mock: MagicMock, *extra: str) -> None:
    with patch("nab.cli.resolve_for_targets", mock):
        app.cli(
            args=["lock", str(pyproject), "--output", str(out), "--locked", *extra],
            prog="nab",
        )


def _run_locked_unmocked(pyproject: Path, out: Path, *extra: str) -> None:
    """Drive ``--locked`` against the real resolve, to pin what it reports.

    Runs offline with the cache off: these cases fail while reading the
    project, before the resolve would reach an index.
    """
    app.cli(
        args=[
            "lock",
            str(pyproject),
            "--output",
            str(out),
            "--locked",
            "--offline",
            "True",
            "--no-cache",
            *extra,
        ],
        prog="nab",
    )


# --- fire cases: the resolver is never called ---


def test_tightened_direct_specifier_fires_without_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo"]\n')
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.5"}))
    capsys.readouterr()
    before = out.read_bytes()
    pyproject.write_text('[project]\ndependencies = ["foo>=2.0"]\n', encoding="utf-8")

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "[project].dependencies requires foo>=2.0 but the lock pins foo 1.5" in err
    assert "is out of date" in err
    mock.assert_not_called()
    assert out.read_bytes() == before


def test_tightened_constraint_fires_without_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo"]\n')
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "3.1"}))
    capsys.readouterr()
    pyproject.write_text(
        '[project]\ndependencies = ["foo"]\n[tool.nab]\nconstraints = ["foo<3"]\n',
        encoding="utf-8",
    )

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert (
        "the constraint foo<3 is violated by the pinned foo 3.1"
        in capsys.readouterr().err
    )
    mock.assert_not_called()


def test_added_declared_default_group_fires_without_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        '[project]\nname = "proj"\nversion = "0"\ndependencies = ["foo"]\n'
        "[dependency-groups]\n"
        'test = ["bb"]\n'
    )
    pyproject = _write_pyproject(tmp_path, body)
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()
    pyproject.write_text(
        f'{body}[tool.nab]\ndefault-groups = ["test"]\n', encoding="utf-8"
    )

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "built with default-groups {} but this run selects {test}" in err
    assert "is out of date" in err
    mock.assert_not_called()


def test_changed_requires_python_fires_without_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\ndependencies = ["foo"]\n[tool.nab]\nrequires-python = ">=3.8"\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()
    pyproject.write_text(
        '[project]\ndependencies = ["foo"]\n[tool.nab]\nrequires-python = ">=3.9"\n',
        encoding="utf-8",
    )

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert (
        "the lockfile requires-python >=3.8 does not match this run's >=3.9"
        in capsys.readouterr().err
    )
    mock.assert_not_called()


def test_selected_extra_specifier_fires_without_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\ndependencies = ["foo"]\n'
        "[project.optional-dependencies]\n"
        'dev = ["bar>=2"]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(
        pyproject, out, _result({"foo": "1.0", "bar": "1.0"}), "--extras", "dev"
    )
    capsys.readouterr()

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock, "--extras", "dev")

    assert exc.value.code == 1
    assert "the 'dev' extra requires bar>=2 but the lock pins bar 1.0" in (
        capsys.readouterr().err
    )
    mock.assert_not_called()


def test_selected_group_specifier_fires_without_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\ndependencies = ["foo"]\n[dependency-groups]\ntest = ["bar>=2"]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(
        pyproject, out, _result({"foo": "1.0", "bar": "1.0"}), "--groups", "test"
    )
    capsys.readouterr()

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock, "--groups", "test")

    assert exc.value.code == 1
    assert "the 'test' dependency group requires bar>=2 but the lock pins bar 1.0" in (
        capsys.readouterr().err
    )
    mock.assert_not_called()


def test_missing_direct_requirement_fires_without_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo"]\n')
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()
    pyproject.write_text('[project]\ndependencies = ["foo", "bar"]\n', encoding="utf-8")

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert (
        "[project].dependencies requires bar and its marker applies here, but the"
        " lock has no bar pin" in capsys.readouterr().err
    )
    mock.assert_not_called()


# --- fall-through cases: the resolver is always called ---


def test_satisfiable_and_reproducible_falls_through_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo>=1.0"]\n')
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()
    before = out.read_bytes()

    mock = _locked_mock(_result({"foo": "1.0"}))
    _run_locked(pyproject, out, mock)

    assert "is up to date" in capsys.readouterr().err
    mock.assert_called_once()
    assert out.read_bytes() == before


def test_non_sticky_stale_falls_through_out_of_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo>=1.0"]\n')
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()

    mock = _locked_mock(_result({"foo": "2.0"}))
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert "out of date" in capsys.readouterr().err
    mock.assert_called_once()


def test_prerelease_pin_falls_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The pin satisfies the direct specifier and the constraint, so neither fires.
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\ndependencies = ["foo>=2.0b1"]\n'
        '[tool.nab]\nconstraints = ["foo<3"]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "2.0b1"}))
    capsys.readouterr()

    mock = _locked_mock(_result({"foo": "2.0b1"}))
    _run_locked(pyproject, out, mock)

    assert "is up to date" in capsys.readouterr().err
    mock.assert_called_once()


def test_marker_inactive_absent_requirement_falls_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\ndependencies = ["foo", "bar; python_version < \\"2.0\\""]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()

    mock = _locked_mock(_result({"foo": "1.0"}))
    _run_locked(pyproject, out, mock)

    assert "is up to date" in capsys.readouterr().err
    mock.assert_called_once()


def test_unreadable_requirement_falls_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo"]\n')
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()
    pyproject.write_text('[project]\ndependencies = ["foo (=="]\n', encoding="utf-8")

    mock = _locked_mock(_result({"foo": "1.0"}))
    _run_locked(pyproject, out, mock)

    mock.assert_called_once()


def test_requires_python_excludes_host_falls_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # requires-python that excludes the host cannot build a target, so
    # validity is skipped and the resolve runs.
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\ndependencies = ["foo"]\n[tool.nab]\nrequires-python = ">=99"\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()

    mock = _locked_mock(_result({"foo": "1.0"}))
    _run_locked(pyproject, out, mock)

    mock.assert_called_once()


def test_dropped_workspace_member_direct_dep_falls_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --no-emit-workspace drops the member from the lock, so the presence
    # check must fall through rather than fire on its absence.
    member = tmp_path / "alpha"
    member.mkdir()
    (member / "pyproject.toml").write_text(
        '[project]\nname = "alpha"\nversion = "0"\n', encoding="utf-8"
    )
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "ws"\nversion = "0"\ndependencies = ["alpha", "foo"]\n'
        '[tool.nab.workspace]\nmembers = ["alpha"]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(
        pyproject,
        out,
        _result({"alpha": "0", "foo": "1.0"}),
        "--no-emit-workspace",
    )
    capsys.readouterr()

    mock = _locked_mock(_result({"alpha": "0", "foo": "1.0"}))
    _run_locked(pyproject, out, mock, "--no-emit-workspace")

    assert "is up to date" in capsys.readouterr().err
    mock.assert_called_once()


def _foo_package(version: str, marker: str) -> str:
    return (
        "[[packages]]\n"
        'name = "foo"\n'
        f'version = "{version}"\n'
        'index = "pypi"\n'
        f'marker = "{marker}"\n\n'
        "[packages.sdist]\n"
        f'name = "foo-{version}.tar.gz"\n'
        f'url = "https://example.com/foo-{version}.tar.gz"\n'
        "[packages.sdist.hashes]\n"
        f'sha256 = "{"b" * 64}"\n\n'
        "[[packages.wheels]]\n"
        f'name = "foo-{version}-py3-none-any.whl"\n'
        f'url = "https://example.com/foo-{version}-py3-none-any.whl"\n'
        "[packages.wheels.hashes]\n"
        f'sha256 = "{"a" * 64}"\n\n'
    )


def test_conflict_fork_duplicate_pins_fall_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The lock holds two foo pins under disjoint conflict-fork markers, so no
    # single version stands for foo and the tier falls through.
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\nversion = "0"\ndependencies = []\n'
        "[project.optional-dependencies]\n"
        'old = ["foo<2"]\n'
        'new = ["foo>=2"]\n'
        "[tool.nab]\n"
        'conflicts = [[{extra = "old"}, {extra = "new"}]]\n',
    )
    out = tmp_path / "pylock.toml"
    out.write_text(
        'lock-version = "1.0"\n'
        "extras = [\n"
        '    "new",\n'
        '    "old",\n'
        "]\n"
        'created-by = "nab"\n\n'
        + _foo_package("1.5", "'old' in extras")
        + _foo_package("2.5", "'new' in extras"),
        encoding="utf-8",
    )

    mock = _locked_mock(_result({"foo": "1.5"}))
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock, "--extras", "old", "new")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "the lock pins foo" not in err
    mock.assert_called_once()


# --- invalid invocation: the tier defers and the resolve reports why ---


def test_undeclared_group_reports_the_group_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\nversion = "0"\ndependencies = ["foo"]\n'
        "[dependency-groups]\n"
        'test = ["bb"]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run_locked_unmocked(pyproject, out, "--groups", "dev")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Dependency group 'dev' not found" in err
    assert "is out of date" not in err
    assert "re-run `nab lock`" not in err


def test_undeclared_default_group_reports_the_group_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        '[project]\nname = "proj"\nversion = "0"\ndependencies = ["foo"]\n'
        "[dependency-groups]\n"
        'test = ["bb"]\n'
    )
    pyproject = _write_pyproject(tmp_path, body)
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()
    pyproject.write_text(
        f'{body}[tool.nab]\ndefault-groups = ["dev"]\n', encoding="utf-8"
    )

    with pytest.raises(SystemExit) as exc:
        _run_locked_unmocked(pyproject, out)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Dependency group 'dev' not found" in err
    assert "is out of date" not in err
    assert "re-run `nab lock`" not in err


def test_undeclared_project_default_group_reports_the_group_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\nversion = "0"\ndependencies = ["foo"]\n'
        "[dependency-groups]\n"
        'test = ["bb"]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run_locked_unmocked(pyproject, out, "--project-default-group", "dev")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Dependency group 'dev' not found" in err
    assert "is out of date" not in err
    assert "re-run `nab lock`" not in err


def test_undeclared_extra_reports_the_extra_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\nversion = "0"\ndependencies = ["foo"]\n'
        "[project.optional-dependencies]\n"
        'x = ["bb"]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        _run_locked_unmocked(pyproject, out, "--extras", "typo")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert (
        "extra 'typo' is not declared in [project.optional-dependencies];"
        " defined: ['x']" in err
    )
    assert "is out of date" not in err
    assert "re-run `nab lock`" not in err


def test_unreadable_requirement_with_changed_envelope_reports_the_parse_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\nversion = "0"\ndependencies = ["foo"]\n'
        "[project.optional-dependencies]\n"
        'x = ["bb"]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}), "--extras", "x")
    capsys.readouterr()
    pyproject.write_text(
        '[project]\nname = "proj"\nversion = "0"\ndependencies = ["foo (=="]\n',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        _run_locked_unmocked(pyproject, out)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "[project].dependencies" in err
    assert "is out of date" not in err


# --- precondition cases: no resolve runs ---


def test_missing_lock_precondition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo"]\n')
    out = tmp_path / "pylock.toml"

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert "no lockfile" in capsys.readouterr().err
    mock.assert_not_called()


def test_invalid_toml_precondition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo"]\n')
    out = tmp_path / "pylock.toml"
    out.write_text('lock-version = "1.0"\n<<<<<<< HEAD\n', encoding="utf-8")

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert "is not valid TOML" in capsys.readouterr().err
    mock.assert_not_called()


def test_non_pep751_precondition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo"]\n')
    out = tmp_path / "pylock.toml"
    out.write_text('title = "not a pylock"\n', encoding="utf-8")

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert "not a valid PEP 751 lockfile" in capsys.readouterr().err
    mock.assert_not_called()


def test_oversized_requires_python_precondition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # PEP 440 puts no cap on release digits; int() refuses them only at compare time.
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\ndependencies = ["foo"]\n[tool.nab]\nrequires-python = ">=3.10"\n',
    )
    out = tmp_path / "pylock.toml"
    oversized = "9" * (sys.get_int_max_str_digits() + 1)
    out.write_text(
        'lock-version = "1.0"\n'
        f'requires-python = ">={oversized}"\n'
        'created-by = "nab"\n'
        "packages = []\n",
        encoding="utf-8",
    )

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not a valid PEP 751 lockfile" in err
    assert "requires-python" in err
    mock.assert_not_called()


def test_unreadable_lock_precondition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Reading a directory raises OSError, exercising the unreadable-lock branch.
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo"]\n')
    out = tmp_path / "pylock.toml"
    out.mkdir()

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert "cannot read lockfile" in capsys.readouterr().err
    mock.assert_not_called()


def test_unsearchable_parent_precondition(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    deny_access: Callable[[Path], AbstractContextManager[None]],
) -> None:
    # EACCES lands on the presence check's stat, before any open.
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo"]\n')
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()

    mock = _locked_mock()
    with deny_access(out), pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no lockfile" not in err
    assert "cannot read lockfile" in err
    assert "Permission denied" in err
    mock.assert_not_called()
