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
import tomli

from nab._lock import _join_command, _join_for_cmd, _quote_for_cmd
from nab.cli import app
from nab_project.lockfile import (
    IndexPin,
    SdistArtifact,
    TargetLock,
    WheelArtifact,
)
from nab_project.resolve import ResolveResult, TargetResult
from nab_provider._vendor.packaging.version import Version
from nab_provider.target import ResolveTarget

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


def _remedy(*arguments: str) -> str:
    """Return the command text a --locked failure names for this ``nab lock`` run.

    Joined the way the message is, so a case lists arguments instead of one
    platform's quoting.
    """
    return _join_command(["nab", "lock", *arguments])


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


def test_tightened_build_requirement_fires_without_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--locked proves a build lock stale from [build-system].requires."""
    body = '[project]\nname = "proj"\ndependencies = []\n'
    pyproject = _write_pyproject(
        tmp_path, body + '[build-system]\nrequires = ["foo"]\n'
    )
    out = tmp_path / "pylock.build.toml"
    _write_lock(pyproject, out, _result({"foo": "1.5"}), "--build-requirements")
    capsys.readouterr()
    pyproject.write_text(
        body + '[build-system]\nrequires = ["foo>=2.0"]\n', encoding="utf-8"
    )

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock, "--build-requirements")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "[build-system].requires requires foo>=2.0 but the lock pins foo 1.5" in err
    remedy = _remedy(str(pyproject), "--output", str(out), "--build-requirements")
    assert f"re-run `{remedy}` to update it" in err
    mock.assert_not_called()


def test_locked_build_lock_defaults_to_the_build_lock_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --output, --build-requirements checks pylock.build.toml."""
    monkeypatch.chdir(tmp_path)
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\ndependencies = []\n'
        '[build-system]\nrequires = ["foo"]\n',
    )

    with pytest.raises(SystemExit) as exc:
        app.cli(
            args=["lock", str(pyproject), "--locked", "--build-requirements"],
            prog="nab",
        )

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no lockfile at pylock.build.toml" in err
    assert f"run `{_remedy(str(pyproject), '--build-requirements')}` first" in err


def test_locked_build_lock_checks_its_own_default_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale pylock.toml alongside must not decide the build lock's verdict."""
    monkeypatch.chdir(tmp_path)
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\ndependencies = ["bar"]\n'
        '[build-system]\nrequires = ["foo"]\n',
    )
    _write_lock(pyproject, tmp_path / "pylock.toml", _result({"bar": "9.9"}))
    _write_lock(
        pyproject,
        tmp_path / "pylock.build.toml",
        _result({"foo": "1.0"}),
        "--build-requirements",
    )
    capsys.readouterr()

    with patch("nab.cli.resolve_for_targets", _locked_mock(_result({"foo": "1.0"}))):
        app.cli(
            args=["lock", str(pyproject), "--locked", "--build-requirements"],
            prog="nab",
        )

    assert "Lockfile pylock.build.toml is up to date." in capsys.readouterr().err


def test_a_lock_offering_a_build_group_is_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No run selects the build group, so its name must not read as drift."""
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\ndependencies = ["foo"]\n'
        '[build-system]\nrequires = ["foo"]\n'
        '[tool.nab]\nbase-group = "main"\nbuild-group = "build"\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()
    assert tomli.loads(out.read_text())["dependency-groups"] == ["build", "main"]

    _run_locked(pyproject, out, _locked_mock(_result({"foo": "1.0"})))

    assert "is up to date" in capsys.readouterr().err


def test_a_tightened_build_requirement_fires_with_a_build_group(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fast-fail tier sees the build side when build-group carries it."""
    body = '[project]\nname = "proj"\ndependencies = ["bar"]\n[build-system]\n'
    pyproject = _write_pyproject(
        tmp_path,
        body
        + 'requires = ["foo"]\n[tool.nab]\nbase-group = "main"\nbuild-group = "build"\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"bar": "1.0", "foo": "1.5"}))
    capsys.readouterr()
    pyproject.write_text(
        body
        + 'requires = ["foo>=2.0"]\n'
        + '[tool.nab]\nbase-group = "main"\nbuild-group = "build"\n',
        encoding="utf-8",
    )

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "[build-system].requires requires foo>=2.0 but the lock pins foo 1.5" in err
    mock.assert_not_called()


def test_a_renamed_build_group_is_out_of_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Renaming it renames a marker on every package it gates."""
    body = (
        '[project]\nname = "proj"\ndependencies = ["foo"]\n'
        '[build-system]\nrequires = ["foo"]\n'
        "[tool.nab]\n"
    )
    pyproject = _write_pyproject(
        tmp_path, body + 'base-group = "main"\nbuild-group = "build"\n'
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()
    pyproject.write_text(
        body + 'base-group = "main"\nbuild-group = "builder"\n', encoding="utf-8"
    )

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert "does not name 'builder' for the build requirements" in (
        capsys.readouterr().err
    )
    mock.assert_not_called()


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


def test_constraint_not_splitting_the_minor_fires_without_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No boundary inside the minor, so the target's floor decides the marker.
    pyproject = _write_pyproject(
        tmp_path,
        "[project]\n"
        "dependencies = ['foo']\n"
        "[tool.nab]\n"
        "constraints = ['foo<1.0 ; python_full_version >= \"3.9\"']\n"
        "[tool.nab.environment]\n"
        'python = "3.11"\n',
    )
    out = tmp_path / "pylock.toml"
    out.write_text(
        'lock-version = "1.0"\n'
        'created-by = "nab"\n\n'
        + _foo_package("1.5", 'python_full_version >= \\"3.11\\"'),
        encoding="utf-8",
    )

    mock = _locked_mock(_result({"foo": "1.5"}))
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert (
        "the constraint foo<1.0 is violated by the pinned foo 1.5"
        in capsys.readouterr().err
    )
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


def test_python_flag_activates_a_marker_and_fires_without_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # nab needs 3.10, so this marker is inactive on any host that runs the
    # suite: only --python makes the requirement active.
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\ndependencies = ["foo; python_version < \\"3.10\\""]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({}))
    capsys.readouterr()

    mock = _locked_mock(_result({}))
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock, "--python", "3.9")

    assert exc.value.code == 1
    assert (
        "[project].dependencies requires foo and its marker applies here, but the"
        " lock has no foo pin" in capsys.readouterr().err
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


def test_a_reordered_selection_falls_through_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Flag order is no part of the selection, so the lock is still up to date."""
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\ndependencies = ["foo"]\n'
        "[project.optional-dependencies]\n"
        'xb = ["foo"]\nxa = ["foo"]\n'
        "[dependency-groups]\n"
        'gb = ["foo"]\nga = ["foo"]\n',
    )
    out = tmp_path / "pylock.toml"
    selection = ["--groups", "gb", "ga", "--extras", "xb", "xa"]
    _write_lock(pyproject, out, _result({"foo": "1.0"}), *selection)
    capsys.readouterr()
    before = out.read_bytes()

    reordered = ["--groups", "ga", "gb", "--extras", "xa", "xb"]
    mock = _locked_mock()
    _run_locked(pyproject, out, mock, *reordered)

    assert "is up to date" in capsys.readouterr().err
    mock.assert_called_once()
    assert out.read_bytes() == before


def test_all_groups_falls_through_up_to_date_on_a_listed_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--all-groups`` reaches the same names, in the order they are declared."""
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\ndependencies = ["foo"]\n'
        "[dependency-groups]\n"
        'gb = ["foo"]\nga = ["foo"]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}), "--groups", "ga", "gb")
    capsys.readouterr()

    mock = _locked_mock()
    _run_locked(pyproject, out, mock, "--all-groups")

    assert "is up to date" in capsys.readouterr().err
    mock.assert_called_once()


def test_the_base_group_named_in_default_groups_falls_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A declared default-groups keeps the name, so the tier must not fire."""
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\nversion = "0.1"\ndependencies = ["foo"]\n'
        "[dependency-groups]\n"
        'dev = ["bb"]\n'
        "[tool.nab]\n"
        'base-group = "base"\n'
        'default-groups = ["dev", "base"]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0", "bb": "1.0"}))
    capsys.readouterr()
    assert tomli.loads(out.read_text())["default-groups"] == ["base", "dev"]

    mock = _locked_mock(_result({"foo": "1.0", "bb": "1.0"}))
    _run_locked(pyproject, out, mock)

    assert f"Lockfile {out} is up to date." in capsys.readouterr().err
    mock.assert_called_once()


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


def test_a_stale_build_lock_names_the_build_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The post-resolve verdict must send the user to the file it just read."""
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\ndependencies = []\n'
        '[build-system]\nrequires = ["foo>=1.0"]\n',
    )
    out = tmp_path / "pylock.build.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}), "--build-requirements")
    capsys.readouterr()

    mock = _locked_mock(_result({"foo": "2.0"}))
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock, "--build-requirements")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    remedy = _remedy(str(pyproject), "--output", str(out), "--build-requirements")
    assert f"is out of date; re-run `{remedy}` to update it" in err
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


def test_python_flag_deactivates_a_marker_and_falls_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The mirror of the fire case: active on any host that runs the suite,
    # inactive at 3.9, so the lock having no foo pin does not disqualify it.
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\ndependencies = ["foo; python_version >= \\"3.10\\""]\n',
    )
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({}))
    capsys.readouterr()

    mock = _locked_mock(_result({}))
    _run_locked(pyproject, out, mock, "--python", "3.9")

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


def test_micro_gated_constraint_falls_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A bare-minor target resolves once per micro slice, so a constraint gated
    # below the boundary says nothing about the pin the slice above produced.
    pyproject = _write_pyproject(
        tmp_path,
        "[project]\n"
        "dependencies = ['foo ; python_full_version >= \"3.11.4\"']\n"
        "[tool.nab]\n"
        "constraints = ['foo<1.0 ; python_full_version < \"3.11.4\"']\n"
        "[tool.nab.environment]\n"
        'python = "3.11"\n',
    )
    out = tmp_path / "pylock.toml"
    out.write_text(
        'lock-version = "1.0"\n'
        'created-by = "nab"\n\n'
        + _foo_package("1.5", 'python_full_version >= \\"3.11.4.dev0\\"'),
        encoding="utf-8",
    )

    mock = _locked_mock(_result({"foo": "1.5"}))
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock)

    assert exc.value.code == 1
    assert "the constraint foo<1.0" not in capsys.readouterr().err
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
    assert "re-run `nab lock" not in err


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
    assert "re-run `nab lock" not in err


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
    assert "re-run `nab lock" not in err


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
    assert "re-run `nab lock" not in err


def test_missing_build_system_reports_the_project_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\nversion = "0"\ndependencies = []\n',
    )
    out = tmp_path / "pylock.build.toml"

    with pytest.raises(SystemExit) as exc:
        _run_locked_unmocked(pyproject, out, "--build-requirements")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "declares no [build-system]" in err
    assert "run `nab lock" not in err


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


# --- --python cases: the flag is named, never the lock ---


def test_invalid_python_value_reports_the_flag_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed --python names the flag, not the stale lock."""
    body = '[project]\ndependencies = ["foo"]\n[tool.nab]\nrequires-python = ">=3.8"\n'
    pyproject = _write_pyproject(tmp_path, body)
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()
    pyproject.write_text(body.replace(">=3.8", ">=3.9"), encoding="utf-8")

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock, "--python", "3.x")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "error: --python must be a version like '3.12' or '3.12.4', got '3.x'" in err
    assert "is out of date" not in err
    assert "re-run `nab lock" not in err
    mock.assert_not_called()


def test_invalid_python_value_is_reported_before_a_missing_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The missing-lock remedy would hand the rejected value back."""
    pyproject = _write_pyproject(tmp_path, '[project]\ndependencies = ["foo"]\n')
    out = tmp_path / "pylock.toml"

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock, "--python", "3.x")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "error: --python must be a version like" in err
    assert "no lockfile" not in err
    assert "run `nab lock" not in err
    mock.assert_not_called()


def test_python_outside_requires_python_reports_the_declaration_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A --python the declaration excludes names requires-python, not the lock."""
    body = '[project]\ndependencies = ["foo"]\n[tool.nab]\nrequires-python = ">=3.8"\n'
    pyproject = _write_pyproject(tmp_path, body)
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.0"}))
    capsys.readouterr()
    pyproject.write_text(body.replace(">=3.8", ">=3.9"), encoding="utf-8")

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock, "--python", "3.7")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "requires-python = '>=3.9' excludes the resolve target Python 3.7" in err
    assert "is out of date" not in err
    assert "re-run `nab lock" not in err
    mock.assert_not_called()


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


# --- remedy cases: the failure names the run that rewrites the file ---


def test_remedy_keeps_the_extras_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bare `nab lock` would rewrite the lock without the extra it was built for."""
    body = (
        '[project]\nname = "proj"\nversion = "0"\ndependencies = []\n'
        "[project.optional-dependencies]\n"
    )
    pyproject = _write_pyproject(tmp_path, body + 'gpu = ["foo"]\n')
    out = tmp_path / "pylock.toml"
    _write_lock(pyproject, out, _result({"foo": "1.5"}), "--extras", "gpu")
    capsys.readouterr()
    pyproject.write_text(body + 'gpu = ["foo>=2.0"]\n', encoding="utf-8")

    mock = _locked_mock()
    with pytest.raises(SystemExit) as exc:
        _run_locked(pyproject, out, mock, "--extras", "gpu")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "the 'gpu' extra requires foo>=2.0 but the lock pins foo 1.5" in err
    remedy = _remedy(str(pyproject), "--output", str(out), "--extras", "gpu")
    assert f"re-run `{remedy}` to update it" in err
    mock.assert_not_called()


def test_remedy_names_a_custom_output_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock kept off the default path must not send the user to ./pylock.toml."""
    monkeypatch.chdir(tmp_path)
    _write_pyproject(tmp_path, '[project]\nname = "proj"\ndependencies = []\n')
    out = Path("locks/pylock.toml")

    with pytest.raises(SystemExit) as exc:
        app.cli(args=["lock", "--output", str(out), "--locked"], prog="nab")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    remedy = _remedy("--output", str(out))
    assert f"no lockfile at {out} to check; run `{remedy}` first." in err


def test_remedy_carries_every_run_shaping_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything that decides what the lock holds belongs in the remedy.

    Driving the CLI with the same arguments the remedy renders proves tyro
    still accepts them, multi-value ``--groups``/``--extras`` included: a
    parse failure would exit 2 with a usage error instead of the remedy.
    """
    monkeypatch.chdir(tmp_path)
    pyproject = _write_pyproject(
        tmp_path,
        '[project]\nname = "proj"\nversion = "0"\ndependencies = []\n'
        "[project.optional-dependencies]\n"
        'cpu = ["foo"]\ngpu = ["foo"]\n'
        "[dependency-groups]\n"
        'dev = ["bar"]\ntest = ["bar"]\n',
    )
    out = Path("locks/pylock.toml")

    refresh = [
        str(pyproject),
        *["--output", str(out)],
        *["--python", "3.12"],
        *["--groups", "dev", "test"],
        *["--extras", "cpu", "gpu"],
        *["--offline", "True"],
        "--no-workspace-discovery",
        "--no-emit-workspace",
        *["--project-resolution", "lowest"],
        *["--project-constraint", "foo<2"],
        *["--project-constraint", "bar<3"],
        *["--project-requires-python", ">=3.9"],
        "--upgrade",
    ]

    with pytest.raises(SystemExit) as exc:
        app.cli(args=["lock", *refresh, "--locked"], prog="nab")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert f"no lockfile at {out} to check; run `{_remedy(*refresh)}` first." in err


def test_remedy_for_a_default_run_stays_bare(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that shapes nothing gets the short command, not its own defaults."""
    monkeypatch.chdir(tmp_path)
    _write_pyproject(tmp_path, '[project]\nname = "proj"\ndependencies = []\n')

    with pytest.raises(SystemExit) as exc:
        app.cli(args=["lock", "--locked"], prog="nab")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no lockfile at pylock.toml to check; run `nab lock` first." in err


def test_following_the_remedy_makes_the_check_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the printed remedy makes the next --locked check pass."""
    monkeypatch.chdir(tmp_path)
    body = (
        '[project]\nname = "proj"\nversion = "0"\ndependencies = []\n'
        "[project.optional-dependencies]\n"
    )
    pyproject = _write_pyproject(tmp_path, body + 'gpu = ["foo"]\n')
    out = tmp_path / "locks" / "pylock.toml"
    out.parent.mkdir()
    _write_lock(pyproject, out, _result({"foo": "1.5"}), "--extras", "gpu")
    pyproject.write_text(body + 'gpu = ["foo>=2.0"]\n', encoding="utf-8")
    capsys.readouterr()
    refresh = [str(pyproject), "--output", str(out), "--extras", "gpu"]

    with pytest.raises(SystemExit):
        _run_locked(
            pyproject, out, _locked_mock(_result({"foo": "2.0"})), "--extras", "gpu"
        )

    assert f"re-run `{_remedy(*refresh)}` to update it" in capsys.readouterr().err

    with patch("nab.cli.resolve_for_targets", _locked_mock(_result({"foo": "2.0"}))):
        app.cli(args=["lock", *refresh], prog="nab")
    capsys.readouterr()

    _run_locked(
        pyproject, out, _locked_mock(_result({"foo": "2.0"})), "--extras", "gpu"
    )

    assert f"Lockfile {out} is up to date." in capsys.readouterr().err


def test_the_windows_remedy_quotes_what_cmd_reads_as_syntax() -> None:
    """A cmd.exe remedy wraps what the shell would otherwise read as syntax.

    ``<`` is the case that forces the choice: cmd reads it as a redirect
    unless it sits inside double quotes, and ``--project-constraint foo<2``
    is a remedy that carries one.
    """
    assert _quote_for_cmd(r"C:\proj\pyproject.toml") == r"C:\proj\pyproject.toml"

    assert _quote_for_cmd(r"C:\Program Files\proj") == r'"C:\Program Files\proj"'
    assert _quote_for_cmd("foo<2") == '"foo<2"'
    assert _quote_for_cmd("") == '""'
    assert _quote_for_cmd('foo; sys_platform == "win32"') == (
        '"foo; sys_platform == ""win32"""'
    )

    assert _join_for_cmd(["nab", "lock", "--extras", "gpu"]) == "nab lock --extras gpu"
