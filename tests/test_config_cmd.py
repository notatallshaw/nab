"""Tests for the ``nab config`` subcommand and the tyro conformance gate.

The config command is the read-only inspector over the layered registry.
These tests drive it through tyro's ``app.cli`` (so the real flag surface
is exercised) with injected search roots, and assert the tyro CLI surface
matches the registry one-for-one (the conformance test that guards the
one place the CLI surface is not registry-derived: tyro deriving flags
from a function signature).  One class reads the rendered ``config`` help
back and checks it against what each refused source really does.
They also pin the ``--resolution`` -> ``--project-resolution`` rename, the
lock-ladder config-error exit, a byte-identical no-op lock at defaults, and
every override flag reaching the config that ``nab lock`` and ``nab download``
resolve from.
"""

from __future__ import annotations

import inspect
import io
import logging
import re
from collections.abc import Callable, Mapping
from contextlib import (
    AbstractContextManager,
    redirect_stderr,
    redirect_stdout,
    suppress,
)
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import nab._config_cmd as config_cmd
from nab import cli as nab_cli
from nab._config_cmd import config_command
from nab._download import download
from nab._lock import lock
from nab.cli import app, effective_config
from nab_project.config import NabProjectConfig
from nab_project.config_sources import OPTIONS, OptionSpec, Scope, SourceRoots
from nab_project.download import DownloadResult
from nab_project.lockfile import (
    IndexPin,
    SdistArtifact,
    TargetLock,
    WheelArtifact,
    read_lockfile_anchor,
)
from nab_project.resolve import ResolveResult, TargetResult
from nab_provider._vendor.packaging.version import Version
from nab_provider.provider import (
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    ResolutionStrategy,
    ResolveMode,
)
from nab_provider.target import ResolveTarget

# Every registry option carrying a CLI flag, keyed on the option's key.
_CLI_FLAGS: dict[str, str] = {
    spec.key: spec.cli_flag for spec in OPTIONS if spec.cli_flag is not None
}

# Per flagged option: its key, the value passed on argv, and the value
# ``config get`` then prints.
_CLI_FLAG_CASES: tuple[tuple[str, str, str], ...] = (
    ("resolution", "lowest", "lowest"),
    ("decision-order", "stable", "stable"),
    ("mode", "universal", "universal"),
    ("constraints", "foo<2", "foo<2"),
    ("default-groups", "dev", "dev"),
    ("base-group", "Main_Deps", "main-deps"),
    ("build-group", "Build_Reqs", "build-reqs"),
    ("requires-python", ">=3.11", ">=3.11"),
    ("uploaded-prior-to", "2024-06-01T00:00:00Z", "2024-06-01 00:00:00+00:00"),
    ("dist-policy", "sdist-only", "sdist-only"),
    ("build-policy", "never", "never"),
    ("build-requires-depth", "3", "3"),
    ("offline", "True", "true"),
    ("cache-dir", "wheels-cache", "wheels-cache"),
    ("http-backend", "httpx", "httpx"),
    ("max-concurrency", "5", "5"),
)

# The ``--project-*`` flags ``nab download`` is driven with, and the argv value
# each carries.  ``--project-mode`` needs a project declaring a matrix, so it
# gets its own tests and is named in the completeness check instead.
_DOWNLOAD_PROJECT_ARGV: tuple[tuple[str, str], ...] = (
    ("--project-resolution", "lowest"),
    ("--project-decision-order", "arrival"),
    ("--project-constraint", "b<2"),
    ("--project-constraint", "c<3"),
    ("--project-default-group", "dev"),
    ("--project-base-group", "main-deps"),
    ("--project-build-group", "build-reqs"),
    ("--project-requires-python", ">=3.11"),
    ("--project-uploaded-prior-to", "2024-06-01T00:00:00Z"),
    ("--project-dist-policy", "sdist-only"),
    ("--project-build-policy", "never"),
    ("--project-build-requires-depth", "3"),
)

# A ``[tool.nab]`` body declaring universal mode and the matrix it needs.
_UNIVERSAL_TOOL_NAB = (
    'mode = "universal"\n'
    "[tool.nab.matrix]\n"
    'python = "==3.11"\n'
    'platforms = ["linux_x86_64"]\n'
)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _project(tmp_path: Path, tool_nab: str = "") -> Path:
    body = '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
    if tool_nab:
        body += f"[tool.nab]\n{tool_nab}"
    return _write(tmp_path / "pyproject.toml", body)


def _stub_resolve_result() -> ResolveResult:
    pin = IndexPin(
        name="foo",
        version="1.0",
        index="pypi",
        sdist=SdistArtifact(
            filename="foo-1.0.tar.gz",
            url="https://example.com/foo-1.0.tar.gz",
            hashes=(("sha256", "b" * 64),),
        ),
        wheels=(
            WheelArtifact(
                filename="foo-1.0-py3-none-any.whl",
                url="https://example.com/foo-1.0-py3-none-any.whl",
                hashes=(("sha256", "a" * 64),),
            ),
        ),
    )
    target = ResolveTarget.for_host()
    return ResolveResult(
        targets=(target,),
        target_results=[
            TargetResult(
                target=target,
                success=True,
                pins={"foo": Version("1.0")},
                lock=TargetLock(target=target, pins={"foo": pin}),
            )
        ],
    )


def _run_config(args: list[str]) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        app.cli(args=["config", *args], prog="nab")
    return buf.getvalue()


def _config_help() -> str:
    """What ``nab config --help`` prints."""
    buf = io.StringIO()
    with redirect_stdout(buf), suppress(SystemExit):
        app.cli(args=["config", "--help"], prog="nab")
    return buf.getvalue()


def _help_sentences(needle: str) -> list[str]:
    """The help's sentences naming ``needle``, with tyro's wrapping undone.

    tyro renders ``config_command``'s docstring above the flag listing, so
    the prose is everything before that listing.
    """
    prose = _config_help().partition("positional arguments")[0]
    return [s for s in re.split(r"(?<=\.)\s+", " ".join(prose.split())) if needle in s]


def test_config_search_roots_uses_symlink_dir_not_target(tmp_path: Path) -> None:
    # A symlinked pyproject keeps the symlink's own directory as the
    # local-sources base, matching the resolve path (not the target's dir).
    real = tmp_path / "real"
    real.mkdir()
    (real / "pyproject.toml").write_text('[project]\nname = "x"\n')
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    link = link_dir / "pyproject.toml"
    try:
        link.symlink_to(real / "pyproject.toml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    roots = nab_cli._config_search_roots(link)
    assert roots.project_dir == link_dir.resolve()
    assert roots.pyproject == link_dir.resolve() / "pyproject.toml"


class TestConfigList:
    def test_list_shows_all_options(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots, 'resolution = "lowest"\n')
        out = _run_config(["list", "--path", str(hermetic_roots / "pyproject.toml")])
        assert "resolution" in out
        assert "lowest" in out
        assert "offline" in out
        assert "cache-dir" in out

    @pytest.mark.parametrize(
        ("var", "value"),
        [("NAB_VERBOSITY", "verbose"), ("NAB_NO_PROGRESS", "1")],
    )
    def test_output_env_vars_do_not_trip_config_layer(
        self,
        hermetic_roots: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        var: str,
        value: str,
    ) -> None:
        # NAB_VERBOSITY and NAB_NO_PROGRESS belong to the output layer, so the
        # config env-layer skips them: no warning, and no rejected entry.
        _project(hermetic_roots)
        monkeypatch.setenv(var, value)
        path = str(hermetic_roots / "pyproject.toml")

        with caplog.at_level(logging.WARNING, logger="nab_project"):
            out = _run_config(["list", "--path", path])
            rejected = _run_config(["list", "--include-rejected", "--path", path])

        assert "offline" in out
        assert var not in caplog.text
        assert var not in rejected

    def test_unknown_env_var_warns_and_runs(
        self,
        hermetic_roots: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # An unrecognized NAB_* var warns and is ignored; the run completes.
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFLINE", "1")
        with caplog.at_level(logging.WARNING, logger="nab_project"):
            out = _run_config(
                ["list", "--path", str(hermetic_roots / "pyproject.toml")]
            )
        assert "offline" in out
        assert "NAB_OFLINE" in caplog.text
        assert "ignored" in caplog.text


class TestConfigGet:
    def test_get_value(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots)
        out = _run_config(
            ["get", "offline", "--path", str(hermetic_roots / "pyproject.toml")]
        )
        assert out == "false\n"

    def test_get_http_backend_and_max_concurrency_defaults(
        self, hermetic_roots: Path
    ) -> None:
        _project(hermetic_roots)
        base = ["--path", str(hermetic_roots / "pyproject.toml")]
        assert _run_config(["get", "http-backend", *base]) == "urllib3\n"
        assert _run_config(["get", "max-concurrency", *base]) == "8\n"

    def test_get_build_group_reads_the_cli_override(self, hermetic_roots: Path) -> None:
        """``--project-build-group`` reaches the registry from this command too."""
        _project(hermetic_roots)
        base = ["get", "build-group", "--path", str(hermetic_roots / "pyproject.toml")]
        assert _run_config(base) == "<none>\n"
        assert _run_config([*base, "--project-build-group", "Build_Reqs"]) == (
            "build-reqs\n"
        )

    def test_get_reads_new_user_env_vars(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_HTTP_BACKEND", "httpx")
        monkeypatch.setenv("NAB_MAX_CONCURRENCY", "5")
        base = ["--path", str(hermetic_roots / "pyproject.toml")]
        assert _run_config(["get", "http-backend", *base]) == "httpx\n"
        assert _run_config(["get", "max-concurrency", *base]) == "5\n"

    def test_list_shows_new_user_knobs(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots)
        out = _run_config(["list", "--path", str(hermetic_roots / "pyproject.toml")])
        assert "http-backend" in out
        assert "max-concurrency" in out

    def test_get_requires_key(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots)
        with pytest.raises(SystemExit):
            _run_config(["get", "--path", str(hermetic_roots / "pyproject.toml")])

    def test_get_unknown_key_exits(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _project(hermetic_roots)
        with pytest.raises(SystemExit):
            config_command("get", "bogus", path=hermetic_roots / "pyproject.toml")
        assert "unknown config key" in capsys.readouterr().err


class TestConfigExplain:
    def test_explain_resolution_stack(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots, 'resolution = "lowest"\n')
        out = _run_config(
            [
                "explain",
                "resolution",
                "--path",
                str(hermetic_roots / "pyproject.toml"),
                "--project-resolution",
                "highest",
            ]
        )
        assert out.splitlines()[0].startswith("resolution (project,")
        assert any(line.startswith(">") for line in out.splitlines())
        assert "shadowed" in out

    def test_explain_requires_key(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots)
        with pytest.raises(SystemExit):
            _run_config(["explain", "--path", str(hermetic_roots / "pyproject.toml")])

    def test_explain_include_rejected(
        self, hermetic_roots: Path, tmp_path: Path
    ) -> None:
        _project(hermetic_roots, 'resolution = "lowest"\n')
        _write(tmp_path / "usr" / "nab.toml", 'resolution = "highest"\n')
        out = _run_config(
            [
                "explain",
                "resolution",
                "--include-rejected",
                "--path",
                str(hermetic_roots / "pyproject.toml"),
            ]
        )
        assert "rejected" in out

    def test_explain_include_rejected_records_renamed_env(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NAB_RESOLUTION (a renamed PROJECT name) crashes a real run, but
        # explain --include-rejected records the env layer instead.
        _project(hermetic_roots, 'resolution = "lowest"\n')
        monkeypatch.setenv("NAB_RESOLUTION", "highest")
        out = _run_config(
            [
                "explain",
                "resolution",
                "--include-rejected",
                "--path",
                str(hermetic_roots / "pyproject.toml"),
            ]
        )
        assert "rejected" in out
        assert "NAB_RESOLUTION" in out

    def test_explain_include_rejected_records_unknown_env(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFLINE", "1")
        out = _run_config(
            [
                "explain",
                "offline",
                "--include-rejected",
                "--path",
                str(hermetic_roots / "pyproject.toml"),
            ]
        )
        # An unknown NAB_* var no longer crashes the inspector.
        assert "offline (user" in out

    def test_list_include_rejected_surfaces_unknown_env(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An unknown NAB_* var attaches to no option, so explain cannot
        # reach it; `nab config list --include-rejected` surfaces it.
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFLINE", "1")
        out = _run_config(
            [
                "list",
                "--include-rejected",
                "--path",
                str(hermetic_roots / "pyproject.toml"),
            ]
        )
        assert "rejected:" in out
        assert "NAB_OFLINE" in out

    def test_list_include_rejected_surfaces_known_key_gate(
        self, hermetic_roots: Path
    ) -> None:
        # A PROJECT key set in a user nab.toml attaches to its option, so it
        # is reachable from explain; `list --include-rejected` lists it too.
        _project(hermetic_roots)
        _write(hermetic_roots.parent / "usr" / "nab.toml", 'resolution = "lowest"\n')
        out = _run_config(
            [
                "list",
                "--include-rejected",
                "--path",
                str(hermetic_roots / "pyproject.toml"),
            ]
        )
        assert "rejected:" in out
        assert "resolution" in out

    def test_list_reports_unknown_pyproject_key(self, hermetic_roots: Path) -> None:
        # The inspector reports a typo'd [tool.nab] key the resolve would
        # reject, rather than silently accepting it.
        _project(hermetic_roots, 'resolutionn = "lowest"\n')
        err = io.StringIO()
        with redirect_stderr(err), pytest.raises(SystemExit) as exc:
            _run_config(["list", "--path", str(hermetic_roots / "pyproject.toml")])
        assert exc.value.code == 1
        assert "not a valid nab setting" in err.getvalue()

    def test_explain_unknown_key_exits(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _project(hermetic_roots)
        with pytest.raises(SystemExit):
            config_command("explain", "bogus", path=hermetic_roots / "pyproject.toml")
        assert "unknown config key" in capsys.readouterr().err


class TestConfigHelpRefusalSeverity:
    """The ``config`` help states what each refused source really does.

    A refused key in a config file is fatal without ``--include-rejected``,
    an unknown or renamed ``NAB_*`` var never is, and a refusal naming no
    option reaches ``list`` only.
    """

    def test_only_a_file_refusal_is_fatal(
        self,
        hermetic_roots: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFLINE", "1")
        path = str(hermetic_roots / "pyproject.toml")

        # A typo'd NAB_* var warns and the command still runs.
        with caplog.at_level(logging.WARNING, logger="nab_project"):
            listed = _run_config(["list", "--path", path])

        assert "offline" in listed
        assert "NAB_OFLINE" in caplog.text

        # The same typo in a nab.toml exits instead.
        _write(hermetic_roots / "nab.toml", "ofline = true\n")
        with pytest.raises(SystemExit) as exc:
            _run_config(["list", "--path", path])

        assert exc.value.code == 1
        assert "not a valid nab setting" in capsys.readouterr().err

        # The help says which of the two is fatal.
        [fatal] = _help_sentences("fatal config error")
        assert "config file" in fatal
        assert "NAB_" not in fatal

        [env] = _help_sentences("``NAB_*``")
        assert "never fatal" in env
        assert "warning" in env

    def test_a_refusal_naming_no_option_reaches_list_only(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFLINE", "1")
        monkeypatch.setenv("NAB_RESOLUTION", "lowest")
        path = str(hermetic_roots / "pyproject.toml")
        flag = "--include-rejected"

        listed = _run_config(["list", flag, "--path", path])
        named = _run_config(["explain", "resolution", flag, "--path", path])
        unnamed = _run_config(["explain", "offline", flag, "--path", path])

        # NAB_RESOLUTION names a registry key, NAB_OFLINE names none.
        assert "NAB_OFLINE" in listed
        assert "NAB_RESOLUTION" in named
        assert "NAB_OFLINE" not in unnamed

        [row] = _help_sentences("``rejected`` row")
        assert "only" in row
        assert "``list``" in row


_STRUCTURED_PROJECT_TABLES = frozenset(
    {
        "archive-sources",
        "conflicts",
        "environment",
        "index",
        "indexes",
        "local-sources",
        "marker-environment",
        "matrix",
        "package-rules",
        "packages",
        "vcs",
        "vcs-sources",
        "workspace",
    }
)


def test_only_structured_tables_lack_a_project_flag() -> None:
    """The configuration reference's rule: only the tables stay file-only.

    The keys are listed rather than derived from ``type_label`` so that a
    new file-only option has to be added here deliberately.
    """
    file_only = {
        spec.key
        for spec in OPTIONS
        if spec.scope is Scope.PROJECT and spec.cli_flag is None
    }
    assert file_only == _STRUCTURED_PROJECT_TABLES


class TestConfigErrors:
    def test_unknown_action_exits(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _project(hermetic_roots)
        with pytest.raises(SystemExit):
            config_command("frobnicate", path=hermetic_roots / "pyproject.toml")
        assert "unknown config action" in capsys.readouterr().err

    def test_missing_path_exits(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A typo'd --path must fail like nab lock, not print all-defaults.
        missing = hermetic_roots / "nope" / "pyproject.toml"
        with pytest.raises(SystemExit):
            config_command("list", path=missing)
        err = capsys.readouterr().err
        assert "not found" in err
        assert str(missing) in err

    def test_directory_path_exits(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            config_command("list", path=hermetic_roots)
        err = capsys.readouterr().err
        assert "is a directory" in err

    def test_pipe_path_exits(
        self,
        hermetic_roots: Path,
        capsys: pytest.CaptureFixture[str],
        as_fifo: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        # The shared guard's wording reaches every command that takes a
        # project path, so a pipe is named here the same way nab lock names it.
        path = _project(hermetic_roots)
        with as_fifo(path), pytest.raises(SystemExit):
            config_command("list", path=path)
        err = capsys.readouterr().err
        assert f"{path} exists but is not a regular file" in err
        assert "not found" not in err

    def test_unsearchable_parent_reports_the_errno(
        self,
        hermetic_roots: Path,
        capsys: pytest.CaptureFixture[str],
        deny_access: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        # An unsearchable parent lands EACCES on the presence check's stat.
        # The file is there, so the guard must not call it missing: the
        # read reports it, naming the errno.
        path = _project(hermetic_roots)
        with deny_access(path), pytest.raises(SystemExit):
            config_command("list", path=path)
        err = capsys.readouterr().err
        assert "not found" not in err
        assert "cannot read" in err
        assert "Permission denied" in err

    def test_gate_error_exits(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # offline in pyproject [tool.nab] is a category error.
        _project(hermetic_roots, "offline = true\n")
        with pytest.raises(SystemExit):
            config_command("list", path=hermetic_roots / "pyproject.toml")
        assert "project-scope only" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("body", "type_name"),
        [
            ('[tool]\nnab = "oops"\n', "str"),
            ("[tool]\nnab = 5\n", "int"),
            ("[[tool.nab]]\nx = 1\n", "list"),
        ],
    )
    @pytest.mark.parametrize("action", ["list", "get"])
    def test_non_table_tool_nab_exits(
        self,
        hermetic_roots: Path,
        capsys: pytest.CaptureFixture[str],
        action: str,
        body: str,
        type_name: str,
    ) -> None:
        _write(
            hermetic_roots / "pyproject.toml",
            '[project]\nname = "x"\nversion = "0"\ndependencies = []\n' + body,
        )
        args = (action, "mode") if action == "get" else (action,)
        with pytest.raises(SystemExit) as exc:
            config_command(*args, path=hermetic_roots / "pyproject.toml")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "[tool.nab] must be a table" in err
        assert type_name in err

    def test_version_digit_run_past_int_limit_exits(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The read-only inspector hits the same validators as the resolve, so
        # a digit run past CPython's int limit exits with the one-line error.
        _project(hermetic_roots, 'environment = { python = "3.' + "9" * 5000 + '" }\n')
        with pytest.raises(SystemExit) as exc:
            config_command("list", path=hermetic_roots / "pyproject.toml")
        assert exc.value.code == 1
        assert "environment.python must be a version like" in capsys.readouterr().err

    def test_cli_offline_override_layer(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots)
        buf = io.StringIO()
        with redirect_stdout(buf):
            config_command(
                "get",
                "offline",
                path=hermetic_roots / "pyproject.toml",
                offline=True,
            )
        assert buf.getvalue() == "true\n"

    def test_cli_resolution_and_cache_dir_overrides(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _project(hermetic_roots)
        cache_dir = Path("/c/cli")
        config_command(
            "get",
            "cache-dir",
            path=hermetic_roots / "pyproject.toml",
            project_resolution="lowest",
            cache_dir=cache_dir,
        )
        captured = capsys.readouterr()
        assert captured.out == f"{cache_dir}\n"
        # The PROJECT resolution override emits the reproducibility notice.
        # The inspector produces no lock, so it makes no lock claim.
        assert "the lock they produce" not in captured.err
        assert "reflect that override" in captured.err
        assert "--project-resolution -> lowest" in captured.err


class TestConfigProjectFileConflict:
    """The cross-file conflict surfaced through the config command."""

    def test_conflicting_project_files_exit(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _project(hermetic_roots, 'resolution = "highest"\n')
        _write(hermetic_roots / "nab.toml", 'resolution = "lowest"\n')
        with pytest.raises(SystemExit):
            config_command("list", path=hermetic_roots / "pyproject.toml")
        assert "conflicting values" in capsys.readouterr().err


class TestTyroConformance:
    """The tyro CLI surface must match the registry one-for-one.

    tyro derives flags from the ``config`` function signature, which the
    registry cannot generate.  This test pins them together: every
    registry CLI flag must appear in the rendered ``config`` help and the
    backing parameter names must match the rows.
    """

    def test_every_registry_flag_present_in_help(self) -> None:
        help_text = _config_help()
        for spec in OPTIONS:
            if spec.cli_flag is None:
                # File-only rows (vcs/workspace/marker-environment) carry
                # no CLI flag, so there is nothing to assert in the help.
                continue
            assert spec.cli_flag in help_text, (
                f"registry flag {spec.cli_flag} missing from `nab config` help"
            )

    def test_every_registry_param_present(self) -> None:
        sig = inspect.signature(config_command)
        params = set(sig.parameters)
        for spec in OPTIONS:
            if spec.cli_param is None:
                continue
            assert spec.cli_param in params

    def test_param_types_match_registry(self) -> None:
        sig = inspect.signature(config_command)
        # offline carries the shared OfflineFlag alias, like lock/download, so
        # the layered tri-state flag presents identically across subcommands.
        assert "OfflineFlag" in str(sig.parameters["offline"].annotation)
        assert "Path" in str(sig.parameters["cache_dir"].annotation)
        # project_resolution carries the same constrained ResolutionFlag
        # type as lock/download, so tyro rejects a bad choice identically
        # across subcommands.
        assert "ResolutionFlag" in str(sig.parameters["project_resolution"].annotation)

    def test_lock_exposes_project_resolution_not_bare(self) -> None:
        # The hard rename: lock carries --project-resolution and the bare
        # resolution parameter no longer exists.
        sig = inspect.signature(lock)
        assert "project_resolution" in sig.parameters
        assert "resolution" not in sig.parameters

    def test_lock_help_has_project_resolution_flag(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf), suppress(SystemExit):
            app.cli(args=["lock", "--help"], prog="nab")
        help_text = buf.getvalue()
        assert "--project-resolution" in help_text

    def test_every_cli_param_present_on_run_commands(self) -> None:
        # Every registry option with a CLI flag is exposed on lock, download,
        # and config, driven from OPTIONS so the run surface cannot silently
        # drift from the registry. The one deliberate omission is
        # max-concurrency, a download-only knob (lock has no parallel-fetch
        # step to bound); the allowlist asserts it stays absent from lock so
        # the carve-out is intentional, not accidental drift.
        omitted = {"lock": {"max_concurrency"}}
        cli_params = [s.cli_param for s in OPTIONS if s.cli_param is not None]
        for command in (lock, download, config_command):
            params = set(inspect.signature(command).parameters)
            allowed_missing = omitted.get(command.__name__, set())
            for cli_param in cli_params:
                if cli_param in allowed_missing:
                    assert cli_param not in params, (command.__name__, cli_param)
                else:
                    assert cli_param in params, (command.__name__, cli_param)

    def test_conformance_catches_a_deliberate_mismatch(self) -> None:
        """Prove the gate is real: a registry flag with no CLI param fails."""
        from nab_project.config_sources import _parse_bool

        bogus = OptionSpec(
            key="made-up",
            scope=Scope.USER,
            type_label="bool",
            default=False,
            env_var=None,
            cli_flag="--made-up",
            cli_param="made_up",
            parse=_parse_bool,
            render=str,
        )
        patched = (*OPTIONS, bogus)
        sig = inspect.signature(config_command)
        params = set(sig.parameters)
        help_text = _config_help()

        def check_params() -> None:
            for spec in patched:
                if spec.cli_param is None:
                    continue
                assert spec.cli_param in params, spec.cli_flag

        def check_flags() -> None:
            for spec in patched:
                if spec.cli_flag is None:
                    continue
                assert spec.cli_flag in help_text, spec.cli_flag

        with pytest.raises(AssertionError):
            check_params()
        with pytest.raises(AssertionError):
            check_flags()


class TestCliFlagValues:
    """``config get`` prints the value each registry flag was given.

    The conformance tests above pin the flag surface; these pin what it
    carries.  Every case's value differs from what the key reads with no
    flag, so a flag that stopped forwarding changes the printed output.
    """

    @pytest.mark.parametrize(("key", "argv_value", "printed"), _CLI_FLAG_CASES)
    def test_flag_value_wins(
        self, hermetic_roots: Path, key: str, argv_value: str, printed: str
    ) -> None:
        _project(hermetic_roots)
        base = ["get", key, "--path", str(hermetic_roots / "pyproject.toml")]

        assert _run_config(base) != f"{printed}\n"
        assert _run_config([*base, _CLI_FLAGS[key], argv_value]) == f"{printed}\n"

    def test_every_flagged_option_has_a_case(self) -> None:
        covered = {key for key, _, _ in _CLI_FLAG_CASES}
        assert covered == set(_CLI_FLAGS)


class TestEffectiveConfigBridge:
    def test_effective_config_default_roots_callable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exercise the real _config_search_roots (no monkeypatch) but with a
        # project that has no nab.toml, so only defaults/pyproject apply.
        proj = _project(tmp_path)
        monkeypatch.delenv("NAB_OFFLINE", raising=False)
        monkeypatch.delenv("NAB_CACHE_DIR", raising=False)
        monkeypatch.delenv("NAB_RESOLUTION", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        effective = effective_config(proj)
        assert "resolution" in effective
        assert effective["resolution"].rejected == ()

    def test_layered_run_settings_defaults_noop(self, tmp_path: Path) -> None:
        proj = _project(tmp_path)
        settings, _effective = nab_cli._layered_run_settings(
            proj,
            nab_cli._cli_overrides(
                cli_resolution=None, cli_offline=None, cli_cache_dir=None
            ),
        )
        assert settings.resolution is None
        assert settings.offline is False
        assert settings.cache_dir is None
        assert settings.http_backend == "urllib3"
        assert settings.max_concurrency == 8

    def test_layered_run_settings_reads_project_toml(
        self, hermetic_roots: Path
    ) -> None:
        _project(hermetic_roots, 'resolution = "lowest"\n')
        settings, _effective = nab_cli._layered_run_settings(
            hermetic_roots / "pyproject.toml",
            nab_cli._cli_overrides(
                cli_resolution=None, cli_offline=None, cli_cache_dir=None
            ),
        )
        assert settings.resolution is ResolutionStrategy.LOWEST

    def test_layered_run_settings_cli_offline_set(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots)
        settings, _effective = nab_cli._layered_run_settings(
            hermetic_roots / "pyproject.toml",
            nab_cli._cli_overrides(
                cli_resolution=None, cli_offline=True, cli_cache_dir=None
            ),
        )
        assert settings.offline is True

    def test_cli_no_offline_beats_env(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CLI is the highest rung: --no-offline (offline=False) must override
        # a lower NAB_OFFLINE=1 env value.
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFFLINE", "1")
        settings, _effective = nab_cli._layered_run_settings(
            hermetic_roots / "pyproject.toml",
            nab_cli._cli_overrides(
                cli_resolution=None, cli_offline=False, cli_cache_dir=None
            ),
        )
        assert settings.offline is False

    def test_cli_no_offline_beats_project_toml(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots)
        _write(hermetic_roots / "nab.toml", "offline = true\n")
        settings, _effective = nab_cli._layered_run_settings(
            hermetic_roots / "pyproject.toml",
            nab_cli._cli_overrides(
                cli_resolution=None, cli_offline=False, cli_cache_dir=None
            ),
        )
        assert settings.offline is False

    def test_layered_run_settings_cli_cache_dir(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots)
        settings, _effective = nab_cli._layered_run_settings(
            hermetic_roots / "pyproject.toml",
            nab_cli._cli_overrides(
                cli_resolution=None, cli_offline=None, cli_cache_dir=Path("/c/cli")
            ),
        )
        assert settings.cache_dir == Path("/c/cli")

    def test_layered_run_settings_http_backend_and_max_concurrency(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both new USER knobs layer from env and from a CLI override.
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_HTTP_BACKEND", "httpx")
        monkeypatch.setenv("NAB_MAX_CONCURRENCY", "3")
        settings, _effective = nab_cli._layered_run_settings(
            hermetic_roots / "pyproject.toml",
            nab_cli._cli_overrides(
                cli_resolution=None, cli_offline=None, cli_cache_dir=None
            ),
        )
        assert settings.http_backend == "httpx"
        assert settings.max_concurrency == 3
        cli_settings, _ = nab_cli._layered_run_settings(
            hermetic_roots / "pyproject.toml",
            nab_cli._cli_overrides(
                cli_resolution=None,
                cli_offline=None,
                cli_cache_dir=None,
                cli_http_backend="urllib3",
                cli_max_concurrency=16,
            ),
        )
        assert cli_settings.http_backend == "urllib3"
        assert cli_settings.max_concurrency == 16


def test_default_config_search_roots_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    pyproject = tmp_path / "pyproject.toml"
    roots = nab_cli._config_search_roots(pyproject)
    assert roots.user_toml == tmp_path / "xdg" / "nab" / "nab.toml"
    assert roots.system_toml == Path("/etc/nab/nab.toml")
    assert roots.project_dir == tmp_path.resolve()
    assert roots.pyproject == pyproject.resolve()


def test_config_search_roots_threads_custom_pyproject_name(tmp_path: Path) -> None:
    # A non-default pyproject name is threaded through so the registry's
    # pyproject layer reads the file the user actually pointed at.
    custom = tmp_path / "app.toml"
    roots = nab_cli._config_search_roots(custom)
    assert roots.pyproject == custom.resolve()
    assert roots.project_dir == tmp_path.resolve()


def test_default_config_search_roots_no_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(nab_cli.Path, "home", lambda: tmp_path)
    roots = nab_cli._config_search_roots(tmp_path / "pyproject.toml")
    assert roots.user_toml == tmp_path / ".config" / "nab" / "nab.toml"


def test_config_module_imports_cleanly() -> None:
    assert config_cmd.config_command is not None


def test_lock_exits_cleanly_on_layered_config_error(
    hermetic_roots: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A category-gate error reached via the lock ladder exits 1, not traceback."""
    # A user nab.toml setting the PROJECT resolution key is a category
    # error the pyproject parser never sees, so it surfaces through the
    # lock ladder's SourceConfigError handler.
    _project(hermetic_roots)
    _write(tmp_path / "usr" / "nab.toml", 'resolution = "lowest"\n')
    with pytest.raises(SystemExit):
        lock(hermetic_roots / "pyproject.toml", output=hermetic_roots / "pylock.toml")
    assert "config error" in capsys.readouterr().err


def test_lock_exits_on_pyproject_user_key_via_fold(
    hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The parser fold: a USER key in pyproject [tool.nab] exits 1 with the
    registry category message, not the old unknown-key error."""
    _project(hermetic_roots, "offline = true\n")
    with pytest.raises(SystemExit):
        lock(hermetic_roots / "pyproject.toml", output=hermetic_roots / "pylock.toml")
    err = capsys.readouterr().err
    assert "in [tool.nab]" in err
    assert "user-scope option" in err


class TestNoOpLock:
    """A representative `nab lock` at defaults is byte-identical pre/post the
    config layer: the layered ladder is a pure no-op at defaults."""

    def _lock_bytes(self, proj: Path, out: Path) -> bytes:
        with patch(
            "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
        ) as mock_resolve:
            lock(proj, output=out, cache=False)
        # The config layer must not perturb the resolve knobs at defaults.
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs["offline"] is False
        assert kwargs["resolution_strategy"] is None
        return out.read_bytes()

    def test_lock_output_byte_identical_at_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NAB_OFFLINE", raising=False)
        monkeypatch.delenv("NAB_CACHE_DIR", raising=False)
        monkeypatch.delenv("NAB_RESOLUTION", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
        # Inject hermetic roots so the no-op guarantee does not depend on
        # the host's /etc/nab/nab.toml (which the real roots would read).
        sys_root = tmp_path / "sys" / "nab.toml"
        usr_root = tmp_path / "usr" / "nab.toml"
        monkeypatch.setattr(
            nab_cli,
            "_config_search_roots",
            lambda pyproject: SourceRoots(
                system_toml=sys_root,
                user_toml=usr_root,
                project_dir=pyproject.parent,
                pyproject=pyproject,
            ),
        )
        # A fixed uploaded-prior-to pins the lock anchor so created-at is
        # deterministic; the remaining run-to-run stability is then purely
        # the config layer's no-op-ness.
        proj = _project(tmp_path, 'uploaded-prior-to = "2024-01-01T00:00:00Z"\n')
        # Same project, locked twice: the layered config ladder is a pure
        # no-op at defaults, so the output is byte-identical run to run.
        first = self._lock_bytes(proj, tmp_path / "pylock.first.toml")
        second = self._lock_bytes(proj, tmp_path / "pylock.second.toml")
        assert first == second
        # The lock carries the resolved pin: the config layer is a no-op,
        # not a no-output.
        assert b"foo" in first
        assert b"1.0" in first


class TestProjectCliOverrides:
    """``--project-*`` overrides flow through ``nab lock`` into the resolve."""

    def _lock_config(
        self, proj: Path, out: Path, extra: list[str]
    ) -> tuple[NabProjectConfig, Path]:
        with (
            patch(
                "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
            ) as mock_resolve,
            patch("nab_project.lockfile.write_lock"),
        ):
            app.cli(
                args=["lock", str(proj), "--no-cache", "--output", str(out), *extra],
                prog="nab",
            )
        call = mock_resolve.call_args
        return call.kwargs["config"], call.args[0]

    def test_project_dist_policy_reaches_resolve(self, hermetic_roots: Path) -> None:
        proj = _project(hermetic_roots)
        config, _ = self._lock_config(
            proj,
            hermetic_roots / "pylock.toml",
            ["--project-dist-policy", "sdist-only"],
        )
        assert config.dist_policy is DistPolicy.SDIST_ONLY

    def test_project_dist_policy_override_replaces_trust(
        self, hermetic_roots: Path
    ) -> None:
        # The CLI dist-policy flag is a bare policy, so it replaces the whole
        # policy and resets the sdist-trust flag a lower layer set.
        proj = _project(
            hermetic_roots,
            'dist-policy = { policy = "wheel-or-sdist",'
            " trust-unverified-deps = true }\n",
        )
        config, _ = self._lock_config(
            proj,
            hermetic_roots / "pylock.toml",
            ["--project-dist-policy", "sdist-only"],
        )
        assert config.dist_policy is DistPolicy.SDIST_ONLY
        assert config.trust_unverified_sdist_deps is False

    def test_project_build_requires_depth_reaches_resolve(
        self, hermetic_roots: Path
    ) -> None:
        # The file declares 1, so a resolve seeing 3 read it off the flag.
        proj = _project(hermetic_roots, "build-requires-depth = 1\n")
        config, _ = self._lock_config(
            proj,
            hermetic_roots / "pylock.toml",
            ["--project-build-requires-depth", "3"],
        )
        assert config.build_requires_depth == 3

    def test_project_build_policy_reaches_resolve(self, hermetic_roots: Path) -> None:
        # The file declares build-remote, so a resolve seeing never read it
        # off the flag.
        proj = _project(hermetic_roots, 'build-policy = "build-remote"\n')
        config, _ = self._lock_config(
            proj,
            hermetic_roots / "pylock.toml",
            ["--project-build-policy", "never"],
        )
        assert config.build_policy is BuildPolicy.NEVER

    def test_project_mode_specific_takes_one_lock_from_a_matrix(
        self, hermetic_roots: Path
    ) -> None:
        # How a universal project takes one single-environment lock:
        # --project-mode outranks [tool.nab], so the declared matrix does
        # not apply and --python names the environment to resolve for.
        proj = _project(hermetic_roots, _UNIVERSAL_TOOL_NAB)
        config, _ = self._lock_config(
            proj,
            hermetic_roots / "pylock.toml",
            ["--project-mode", "specific", "--python", "3.13"],
        )
        assert config.mode is ResolveMode.SPECIFIC
        assert config.matrix is None
        assert config.environment is not None
        assert config.environment.python == "3.13"

    def test_project_mode_universal_needs_a_declared_matrix(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The other direction of the same flag: with no [tool.nab.matrix]
        # declared there are no targets, so universal mode is refused.
        proj = _project(hermetic_roots)
        with pytest.raises(SystemExit):
            self._lock_config(
                proj, hermetic_roots / "pylock.toml", ["--project-mode", "universal"]
            )
        err = capsys.readouterr().err
        assert "--project-mode universal needs a matrix table" in err
        assert "a top-level [matrix] table to the project's nab.toml" in err

    def test_project_decision_order_reaches_resolve(self, hermetic_roots: Path) -> None:
        # The file declares stable, so a resolve seeing arrival read the flag.
        proj = _project(hermetic_roots, 'decision-order = "stable"\n')
        config, _ = self._lock_config(
            proj,
            hermetic_roots / "pylock.toml",
            ["--project-decision-order", "arrival"],
        )
        assert config.decision_order is DecisionOrder.ARRIVAL

    def test_project_constraint_repeats_replace_the_file_list(
        self, hermetic_roots: Path
    ) -> None:
        # Repeats accumulate into the flag's own value, which then replaces
        # the declared list rather than extending it.
        proj = _project(hermetic_roots, 'constraints = ["a<1"]\n')
        config, _ = self._lock_config(
            proj,
            hermetic_roots / "pylock.toml",
            ["--project-constraint", "b<2", "--project-constraint", "c<3"],
        )
        assert config.constraints == ("b<2", "c<3")

    def test_append_flag_does_not_swallow_positional_path(
        self, hermetic_roots: Path
    ) -> None:
        # An append flag on either side of the PATH must not consume it.
        proj = _project(hermetic_roots)
        out = hermetic_roots / "pylock.toml"
        with (
            patch(
                "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
            ) as mock_resolve,
            patch("nab_project.lockfile.write_lock"),
        ):
            app.cli(
                args=[
                    "lock",
                    "--project-constraint",
                    "a<1",
                    str(proj),
                    "--project-constraint",
                    "b<2",
                    "--no-cache",
                    "--output",
                    str(out),
                ],
                prog="nab",
            )
        call = mock_resolve.call_args
        assert call.args[0] == proj
        assert call.kwargs["config"].constraints == ("a<1", "b<2")

    def test_project_override_prints_reproducibility_notice(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        proj = _project(hermetic_roots)
        self._lock_config(
            proj,
            hermetic_roots / "pylock.toml",
            ["--project-dist-policy", "sdist-only"],
        )
        assert "--project-dist-policy -> sdist-only" in capsys.readouterr().err

    def test_absolute_uploaded_prior_to_override_keeps_anchor_in_sync(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An absolute --project-uploaded-prior-to sets the resolve window and
        # becomes the lock anchor, so the written created-at matches the
        # cutoff the resolve used.
        proj = _project(hermetic_roots)
        out = hermetic_roots / "pylock.toml"
        with patch(
            "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
        ) as mock_resolve:
            app.cli(
                args=[
                    "lock",
                    str(proj),
                    "--no-cache",
                    "--output",
                    str(out),
                    "--project-uploaded-prior-to",
                    "2024-06-01T00:00:00Z",
                ],
                prog="nab",
            )
        expected = datetime(2024, 6, 1, tzinfo=timezone.utc)
        assert mock_resolve.call_args.kwargs["config"].uploaded_prior_to == expected
        assert read_lockfile_anchor(out) == expected
        assert "--project-uploaded-prior-to -> 2024-06-01" in capsys.readouterr().err

    def test_lock_anchor_honors_absolute_cli_override(
        self, hermetic_roots: Path
    ) -> None:
        proj = _project(hermetic_roots)
        anchor = nab_cli.lock_anchor(
            proj, {"uploaded-prior-to": "2024-06-01T00:00:00Z"}
        )
        assert anchor == datetime(2024, 6, 1, tzinfo=timezone.utc)

    def test_relative_uploaded_prior_to_override_pins_no_anchor(
        self, hermetic_roots: Path
    ) -> None:
        # A P<n>D override sets the resolve window relative to the run but is
        # not a reusable absolute cutoff, so it pins no lock anchor.
        proj = _project(hermetic_roots)
        assert nab_cli.lock_anchor(proj, {"uploaded-prior-to": "P7D"}) is None

    def test_lock_anchor_swallows_digit_run_past_int_limit(
        self, hermetic_roots: Path
    ) -> None:
        # A digit run past CPython's int limit raises a bare ValueError;
        # the best-effort anchor read swallows it like any config error.
        proj = _project(
            hermetic_roots, 'environment = { python = "3.' + "9" * 5000 + '" }\n'
        )
        assert nab_cli.lock_anchor(proj) is None


class TestDownloadLadder:
    """``nab download`` shares ``nab lock``'s config ladder, not raw flags."""

    def _resolve_kwargs(
        self, proj: Path, out: Path, *, offline: bool | None = None
    ) -> Mapping[str, object]:
        download_result = DownloadResult(written=(), skipped=())
        with (
            patch(
                "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
            ) as mock_resolve,
            patch("nab._download.download_lock", return_value=download_result),
        ):
            download(proj / "pyproject.toml", output=out, cache=False, offline=offline)
        return mock_resolve.call_args.kwargs

    def test_project_toml_offline_honored(self, hermetic_roots: Path) -> None:
        # A project-dir nab.toml offline reaches the download resolve.
        _project(hermetic_roots)
        _write(hermetic_roots / "nab.toml", "offline = true\n")
        kwargs = self._resolve_kwargs(hermetic_roots, hermetic_roots / "out")
        assert kwargs["offline"] is True

    def test_env_offline_honored(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NAB_OFFLINE reaches the download resolve.
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFFLINE", "1")
        kwargs = self._resolve_kwargs(hermetic_roots, hermetic_roots / "out")
        assert kwargs["offline"] is True

    def test_project_toml_resolution_honored(self, hermetic_roots: Path) -> None:
        # A project-dir nab.toml resolution reaches the download resolve.
        _project(hermetic_roots, 'resolution = "lowest"\n')
        kwargs = self._resolve_kwargs(hermetic_roots, hermetic_roots / "out")
        assert kwargs["resolution_strategy"] is ResolutionStrategy.LOWEST

    def test_cli_offline_false_beats_env(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CLI is the highest rung: --offline False overrides NAB_OFFLINE=1.
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFFLINE", "1")
        kwargs = self._resolve_kwargs(
            hermetic_roots, hermetic_roots / "out", offline=False
        )
        assert kwargs["offline"] is False

    def test_layered_config_error_exits(
        self, hermetic_roots: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A category-gate error reached via the download ladder exits 1.
        _project(hermetic_roots)
        # The fixture's user_toml lives at <tmp>/usr; project_dir is <tmp>/proj.
        _write(hermetic_roots.parent / "usr" / "nab.toml", 'resolution = "lowest"\n')
        with pytest.raises(SystemExit):
            download(hermetic_roots / "pyproject.toml", output=hermetic_roots / "out")
        assert "config error" in capsys.readouterr().err


class TestDownloadCliOverrides:
    """``nab download``'s own flags reach the config the resolve reads."""

    def _resolve_kwargs(self, proj: Path, extra: list[str]) -> Mapping[str, object]:
        """The kwargs a ``nab download`` argv hands ``resolve_for_targets``."""
        download_result = DownloadResult(written=(), skipped=())
        with (
            patch(
                "nab.cli.resolve_for_targets", return_value=_stub_resolve_result()
            ) as mock_resolve,
            patch("nab._download.download_lock", return_value=download_result),
        ):
            app.cli(
                args=[
                    "download",
                    str(proj),
                    "--output",
                    str(proj.parent / "out"),
                    *extra,
                ],
                prog="nab",
            )
        return mock_resolve.call_args.kwargs

    def _config(self, proj: Path, extra: list[str]) -> NabProjectConfig:
        """The merged config the resolve reads for this argv."""
        config = self._resolve_kwargs(proj, extra)["config"]
        assert isinstance(config, NabProjectConfig)
        return config

    def test_project_overrides_reach_the_config(self, hermetic_roots: Path) -> None:
        # The file declares something other than each flag's value, so a value
        # the resolve sees came off the command line.  ``default-groups`` is
        # left undeclared: its default, the empty tuple, already differs.
        proj = _project(
            hermetic_roots,
            'resolution = "highest"\n'
            'decision-order = "stable"\n'
            'constraints = ["a<1"]\n'
            'base-group = "file-base"\n'
            'build-group = "file-build"\n'
            'requires-python = ">=3.9"\n'
            "uploaded-prior-to = 2020-01-01T00:00:00Z\n"
            'dist-policy = "wheel-only"\n'
            'build-policy = "build-remote"\n'
            "build-requires-depth = 1\n",
        )

        kwargs = self._resolve_kwargs(
            proj, [arg for pair in _DOWNLOAD_PROJECT_ARGV for arg in pair]
        )
        config = kwargs["config"]
        assert isinstance(config, NabProjectConfig)

        # --project-resolution keeps its own path into the resolver, so it is
        # the one flag here that does not land in the merged config.
        assert kwargs["resolution_strategy"] is ResolutionStrategy.LOWEST
        assert config.decision_order is DecisionOrder.ARRIVAL

        # Repeated --project-constraint accumulates, then replaces the list.
        assert config.constraints == ("b<2", "c<3")
        assert config.default_groups == ("dev",)
        assert config.base_group == "main-deps"
        assert config.build_group == "build-reqs"

        assert config.requires_python == ">=3.11"
        assert config.uploaded_prior_to == datetime(2024, 6, 1, tzinfo=timezone.utc)

        assert config.dist_policy is DistPolicy.SDIST_ONLY
        assert config.build_policy is BuildPolicy.NEVER
        assert config.build_requires_depth == 3

    def test_every_project_flag_has_a_case(self) -> None:
        exercised = {flag for flag, _ in _DOWNLOAD_PROJECT_ARGV} | {"--project-mode"}
        assert exercised == {
            spec.cli_flag
            for spec in OPTIONS
            if spec.cli_flag is not None and spec.scope is Scope.PROJECT
        }

    def test_project_mode_specific_downloads_one_environment(
        self, hermetic_roots: Path
    ) -> None:
        # Same as the lock path: --project-mode specific shadows the declared
        # matrix, and --python names the environment to download for.
        proj = _project(hermetic_roots, _UNIVERSAL_TOOL_NAB)
        config = self._config(proj, ["--project-mode", "specific", "--python", "3.13"])
        assert config.mode is ResolveMode.SPECIFIC
        assert config.matrix is None
        assert config.environment is not None
        assert config.environment.python == "3.13"

    def test_cache_dir_flag_reaches_the_resolve(self, hermetic_roots: Path) -> None:
        proj = _project(hermetic_roots)
        cache_dir = hermetic_roots / "wheels-cache"
        kwargs = self._resolve_kwargs(proj, ["--cache-dir", str(cache_dir)])
        assert kwargs["cache_dir"] == cache_dir

    def test_no_cache_drops_the_cache_dir(self, hermetic_roots: Path) -> None:
        # --no-cache wins over a --cache-dir on the same command line.
        proj = _project(hermetic_roots)
        kwargs = self._resolve_kwargs(
            proj, ["--no-cache", "--cache-dir", str(hermetic_roots / "wheels-cache")]
        )
        assert kwargs["cache_dir"] is None

    def test_no_workspace_discovery_leaves_members_undiscovered(
        self, hermetic_roots: Path
    ) -> None:
        proj = _project(hermetic_roots, '[tool.nab.workspace]\nmembers = ["alpha"]\n')
        _write(
            hermetic_roots / "alpha" / "pyproject.toml",
            '[project]\nname = "alpha"\nversion = "0"\ndependencies = []\n',
        )
        assert self._config(proj, []).workspace_member_names == frozenset({"alpha"})

        assert (
            self._config(proj, ["--no-workspace-discovery"]).workspace_member_names
            == frozenset()
        )
