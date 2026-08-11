"""Tests for the layered config registry (:mod:`nab_python.config_sources`).

The registry drives the toml loader, env reader, merge, category gate,
and the ``nab config`` renderers.  These tests exercise the full
precedence ladder for each option, every category-gate error, the
pyproject-vs-project-nab.toml conflict rule, the NAB_ env layer and the
renamed-env gate, the parser-fold helpers, and the list/get/explain
renderers.  Search roots are injected so nothing reads the real
``~/.config``.
"""

from __future__ import annotations

import errno
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import pytest

from nab_index.multi_index import IndexConfig
from nab_index.serialization import SimpleSerialization
from nab_python.config import (
    _INDEX_KEYS,
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
    MatrixConfig,
)
from nab_python.config_sources import (
    OPTIONS,
    EffectiveValue,
    Layer,
    Origin,
    RejectedLayer,
    Scope,
    SourceConfigError,
    SourceKind,
    SourceRoots,
    _load_toml_layer,
    build_cli_layer,
    build_cli_overrides,
    discover_layers,
    inspector_anchor,
    orphan_rejections,
    project_cli_override_notice,
    project_cli_override_records,
    pyproject_registry_keys,
    read_env_layer,
    reject_user_keys_in_pyproject,
    render_explain,
    render_get,
    render_list,
    resolve_config,
)
from nab_python.provider import (
    ArchiveSource,
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    ResolutionStrategy,
    ResolveMode,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from nab_python.tags import PlatformSpec
from nab_python.workspace import WorkspaceConfig


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _resolve_collect(
    roots: SourceRoots,
    *,
    environ: dict[str, str] | None = None,
    cli: dict[str, object] | None = None,
    collect_rejected: bool = False,
) -> tuple[dict[str, EffectiveValue], list[RejectedLayer]]:
    rejected: list[RejectedLayer] = []
    sink = rejected if collect_rejected else None
    layers = discover_layers(roots, rejections=sink)
    env_layer = read_env_layer(environ or {}, rejections=sink)
    cli_layer = build_cli_layer(cli or {})
    return resolve_config(layers, env_layer, cli_layer, rejected=rejected), rejected


def _resolve(
    roots: SourceRoots,
    *,
    environ: dict[str, str] | None = None,
    cli: dict[str, object] | None = None,
    collect_rejected: bool = False,
) -> dict[str, EffectiveValue]:
    eff, _ = _resolve_collect(
        roots, environ=environ, cli=cli, collect_rejected=collect_rejected
    )
    return eff


def _project(tmp_path: Path, tool_nab: str = "") -> Path:
    body = '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
    if tool_nab:
        body += f"[tool.nab]\n{tool_nab}"
    return _write(tmp_path / "pyproject.toml", body)


class TestDefaults:
    def test_all_defaults(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["resolution"].value is ResolutionStrategy.HIGHEST
        assert eff["offline"].value is False
        assert eff["cache-dir"].value is None
        for ev in eff.values():
            assert ev.origin.kind is SourceKind.DEFAULT
            assert ev.origin.scope == "default"


class TestResolutionLadder:
    def test_pyproject_sets_resolution(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "lowest"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["resolution"].value is ResolutionStrategy.LOWEST
        assert eff["resolution"].origin.kind is SourceKind.PYPROJECT

    def test_project_nab_toml_sets_resolution(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(tmp_path / "nab.toml", 'resolution = "lowest-direct"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["resolution"].value is ResolutionStrategy.LOWEST_DIRECT
        assert eff["resolution"].origin.kind is SourceKind.PROJECT_TOML

    def test_cli_project_resolution_wins(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "lowest"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path), cli={"resolution": "highest"})
        assert eff["resolution"].value is ResolutionStrategy.HIGHEST
        assert eff["resolution"].origin.kind is SourceKind.CLI

    def test_resolution_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", 'resolution = "lowest"\n')
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_resolution_in_system_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        sys_toml = _write(tmp_path / "etc" / "nab.toml", 'resolution = "lowest"\n')
        roots = SourceRoots(system_toml=sys_toml, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="cannot be set in a system"):
            _resolve(roots)

    def test_nab_resolution_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            eff = _resolve(
                SourceRoots(project_dir=tmp_path),
                environ={"NAB_RESOLUTION": "lowest"},
            )
        assert eff["resolution"].value is ResolutionStrategy.HIGHEST
        assert "NAB_RESOLUTION" in caplog.text
        assert "not env-settable" in caplog.text

    def test_bad_resolution_value_errors(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "sideways"\n')
        with pytest.raises(SourceConfigError, match="must be one of"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_resolution_non_string_errors(self, tmp_path: Path) -> None:
        _project(tmp_path, "resolution = 3\n")
        with pytest.raises(SourceConfigError, match="must be a string"):
            _resolve(SourceRoots(project_dir=tmp_path))


class TestDecisionOrderLadder:
    def test_default_is_arrival(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["decision-order"].value is DecisionOrder.ARRIVAL
        assert eff["decision-order"].origin.kind is SourceKind.DEFAULT

    def test_pyproject_sets_decision_order(self, tmp_path: Path) -> None:
        _project(tmp_path, 'decision-order = "stable"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["decision-order"].value is DecisionOrder.STABLE
        assert eff["decision-order"].origin.kind is SourceKind.PYPROJECT

    def test_project_nab_toml_sets_decision_order(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(tmp_path / "nab.toml", 'decision-order = "stable"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["decision-order"].value is DecisionOrder.STABLE
        assert eff["decision-order"].origin.kind is SourceKind.PROJECT_TOML

    def test_cli_project_decision_order_wins(self, tmp_path: Path) -> None:
        _project(tmp_path, 'decision-order = "stable"\n')
        eff = _resolve(
            SourceRoots(project_dir=tmp_path), cli={"decision-order": "arrival"}
        )
        assert eff["decision-order"].value is DecisionOrder.ARRIVAL
        assert eff["decision-order"].origin.kind is SourceKind.CLI

    def test_decision_order_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab" / "nab.toml", 'decision-order = "stable"\n'
        )
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_bad_decision_order_value_errors(self, tmp_path: Path) -> None:
        _project(tmp_path, 'decision-order = "whenever"\n')
        with pytest.raises(SourceConfigError, match="must be one of"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_decision_order_non_string_errors(self, tmp_path: Path) -> None:
        _project(tmp_path, "decision-order = 3\n")
        with pytest.raises(SourceConfigError, match="must be a string"):
            _resolve(SourceRoots(project_dir=tmp_path))


class TestOfflineLadder:
    def test_system_then_user_then_project(self, tmp_path: Path) -> None:
        _project(tmp_path)
        sys_toml = _write(tmp_path / "etc" / "nab.toml", "offline = true\n")
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", "offline = false\n")
        roots = SourceRoots(system_toml=sys_toml, user_toml=user, project_dir=tmp_path)
        eff = _resolve(roots)
        # user shadows system
        assert eff["offline"].value is False
        assert eff["offline"].origin.kind is SourceKind.USER_TOML

    def test_project_nab_toml_sets_offline(self, tmp_path: Path) -> None:
        # offline is USER-scope but the project-dir nab.toml accepts both
        # scopes, so a USER key there is allowed.
        _project(tmp_path)
        _write(tmp_path / "nab.toml", "offline = true\n")
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["offline"].value is True
        assert eff["offline"].origin.kind is SourceKind.PROJECT_TOML

    def test_env_overrides_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", "offline = false\n")
        eff = _resolve(
            SourceRoots(user_toml=user, project_dir=tmp_path),
            environ={"NAB_OFFLINE": "1"},
        )
        assert eff["offline"].value is True
        assert eff["offline"].origin.kind is SourceKind.ENV

    def test_cli_overrides_env(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(
            SourceRoots(project_dir=tmp_path),
            environ={"NAB_OFFLINE": "true"},
            cli={"offline": False},
        )
        assert eff["offline"].value is False
        assert eff["offline"].origin.kind is SourceKind.CLI

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1", True), ("0", False), ("true", True), ("FALSE", False)],
    )
    def test_env_bool_forms(self, tmp_path: Path, raw: str, expected: bool) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path), environ={"NAB_OFFLINE": raw})
        assert eff["offline"].value is expected

    def test_env_bad_bool_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        with pytest.raises(SourceConfigError, match="1/0/true/false"):
            _resolve(
                SourceRoots(project_dir=tmp_path),
                environ={"NAB_OFFLINE": "maybe"},
            )

    def test_unknown_nab_env_var_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A typo'd NAB_* var (e.g. NAB_OFLINE) warns and is ignored.
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            eff = _resolve(
                SourceRoots(project_dir=tmp_path),
                environ={"NAB_OFLINE": "true"},
            )
        assert eff["offline"].value is False
        assert "NAB_OFLINE" in caplog.text
        assert "ignored" in caplog.text

    def test_offline_in_pyproject_errors(self, tmp_path: Path) -> None:
        _project(tmp_path, "offline = true\n")
        with pytest.raises(SourceConfigError, match="project-scope only"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_offline_non_bool_in_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", 'offline = "yes"\n')
        with pytest.raises(SourceConfigError, match="1/0/true/false"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))

    def test_offline_wrong_type_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", "offline = 3\n")
        with pytest.raises(SourceConfigError, match="must be a boolean"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))


class TestCacheDirLadder:
    def test_user_nab_toml_sets_cache_dir(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", 'cache-dir = "/c/user"\n')
        eff = _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))
        assert eff["cache-dir"].value == Path("/c/user")
        assert eff["cache-dir"].origin.kind is SourceKind.USER_TOML

    def test_env_then_cli(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(
            SourceRoots(project_dir=tmp_path),
            environ={"NAB_CACHE_DIR": "/c/env"},
            cli={"cache-dir": Path("/c/cli")},
        )
        assert eff["cache-dir"].value == Path("/c/cli")
        assert eff["cache-dir"].origin.kind is SourceKind.CLI

    def test_env_sets_cache_dir(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(
            SourceRoots(project_dir=tmp_path),
            environ={"NAB_CACHE_DIR": "/c/env"},
        )
        assert eff["cache-dir"].value == Path("/c/env")
        assert eff["cache-dir"].origin.kind is SourceKind.ENV

    def test_cache_dir_in_pyproject_errors(self, tmp_path: Path) -> None:
        _project(tmp_path, 'cache-dir = "/x"\n')
        with pytest.raises(SourceConfigError, match="project-scope only"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_cache_dir_non_string_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", "cache-dir = 3\n")
        with pytest.raises(SourceConfigError, match="must be a string path"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))

    def test_empty_env_cache_dir_errors(self, tmp_path: Path) -> None:
        # An exported-but-blank NAB_CACHE_DIR resolves Path("") to the cwd;
        # reject it instead of silently caching into ".".
        _project(tmp_path)
        with pytest.raises(SourceConfigError, match="must be a non-empty path"):
            _resolve(
                SourceRoots(project_dir=tmp_path),
                environ={"NAB_CACHE_DIR": ""},
            )

    def test_cache_dir_with_nul_errors(self, tmp_path: Path) -> None:
        # An embedded NUL is valid TOML, so the parser hands it through.
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab" / "nab.toml", 'cache-dir = "/c/\\u0000x"\n'
        )
        with pytest.raises(SourceConfigError, match="is not a usable filesystem path"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))


class TestHttpBackendLadder:
    def test_default(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["http-backend"].value == "urllib3"
        assert eff["http-backend"].origin.kind is SourceKind.DEFAULT

    def test_user_nab_toml_then_env_then_cli(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", 'http-backend = "httpx"\n')
        eff = _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))
        assert eff["http-backend"].value == "httpx"
        assert eff["http-backend"].origin.kind is SourceKind.USER_TOML
        eff = _resolve(
            SourceRoots(user_toml=user, project_dir=tmp_path),
            environ={"NAB_HTTP_BACKEND": "urllib3"},
        )
        assert eff["http-backend"].value == "urllib3"
        assert eff["http-backend"].origin.kind is SourceKind.ENV
        eff = _resolve(
            SourceRoots(user_toml=user, project_dir=tmp_path),
            environ={"NAB_HTTP_BACKEND": "urllib3"},
            cli={"http-backend": "httpx"},
        )
        assert eff["http-backend"].value == "httpx"
        assert eff["http-backend"].origin.kind is SourceKind.CLI

    def test_unknown_value_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", 'http-backend = "curl"\n')
        with pytest.raises(SourceConfigError, match="must be one of"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))

    def test_non_string_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", "http-backend = 3\n")
        with pytest.raises(SourceConfigError, match="must be a string"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))

    def test_in_pyproject_errors(self, tmp_path: Path) -> None:
        _project(tmp_path, 'http-backend = "httpx"\n')
        with pytest.raises(SourceConfigError, match="project-scope only"):
            _resolve(SourceRoots(project_dir=tmp_path))


class TestMaxConcurrencyLadder:
    def test_default(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["max-concurrency"].value == 8
        assert eff["max-concurrency"].origin.kind is SourceKind.DEFAULT

    def test_toml_int_then_env_string_then_cli(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", "max-concurrency = 4\n")
        eff = _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))
        assert eff["max-concurrency"].value == 4
        eff = _resolve(
            SourceRoots(user_toml=user, project_dir=tmp_path),
            environ={"NAB_MAX_CONCURRENCY": "2"},
        )
        assert eff["max-concurrency"].value == 2
        assert eff["max-concurrency"].origin.kind is SourceKind.ENV
        eff = _resolve(
            SourceRoots(user_toml=user, project_dir=tmp_path),
            environ={"NAB_MAX_CONCURRENCY": "2"},
            cli={"max-concurrency": 16},
        )
        assert eff["max-concurrency"].value == 16
        assert eff["max-concurrency"].origin.kind is SourceKind.CLI

    def test_bool_rejected(self, tmp_path: Path) -> None:
        # bool is an int subclass; a TOML ``true`` must not read as 1.
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", "max-concurrency = true\n")
        with pytest.raises(SourceConfigError, match="must be an integer"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))

    def test_non_numeric_string_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        with pytest.raises(SourceConfigError, match="must be an integer"):
            _resolve(
                SourceRoots(project_dir=tmp_path),
                environ={"NAB_MAX_CONCURRENCY": "lots"},
            )

    def test_non_int_type_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", "max-concurrency = 1.5\n")
        with pytest.raises(SourceConfigError, match="must be an integer"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))

    def test_below_one_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", "max-concurrency = 0\n")
        with pytest.raises(SourceConfigError, match="must be at least 1"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))


class TestProjectFileScalarConflict:
    """Same PROJECT key, conflicting values in both files, is an error."""

    def test_conflicting_values_error(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "highest"\n')
        _write(tmp_path / "nab.toml", 'resolution = "lowest"\n')
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_identical_values_ok(self, tmp_path: Path) -> None:
        # Co-presence with the same value is fine; the project-dir nab.toml
        # is reported as the winner (it sorts last on the tie).
        _project(tmp_path, 'resolution = "lowest"\n')
        _write(tmp_path / "nab.toml", 'resolution = "lowest"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["resolution"].value is ResolutionStrategy.LOWEST
        assert eff["resolution"].origin.kind is SourceKind.PROJECT_TOML
        stack_kinds = [origin.kind for origin, _ in eff["resolution"].stack]
        assert stack_kinds == [SourceKind.PYPROJECT, SourceKind.PROJECT_TOML]

    def test_different_keys_merge(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "lowest"\n')
        _write(tmp_path / "nab.toml", "offline = true\n")
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["resolution"].value is ResolutionStrategy.LOWEST
        assert eff["offline"].value is True

    def test_only_pyproject_no_conflict(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "lowest"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["resolution"].origin.kind is SourceKind.PYPROJECT


class TestPyprojectRoot:
    """The pyproject layer reads the named file, not a fixed name."""

    def test_custom_pyproject_name_is_read(self, tmp_path: Path) -> None:
        custom = _write(
            tmp_path / "app.toml",
            '[project]\nname = "x"\n[tool.nab]\nresolution = "lowest"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path, pyproject=custom))
        assert eff["resolution"].value is ResolutionStrategy.LOWEST
        assert eff["resolution"].origin.kind is SourceKind.PYPROJECT

    def test_default_pyproject_name_used_when_unset(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "lowest"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["resolution"].value is ResolutionStrategy.LOWEST


class TestRejectedCollection:
    def test_include_rejected_captures_gate_casualty(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "lowest"\n')
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", 'resolution = "highest"\n')
        eff = _resolve(
            SourceRoots(user_toml=user, project_dir=tmp_path),
            collect_rejected=True,
        )
        rejected = eff["resolution"].rejected
        assert len(rejected) == 1
        assert rejected[0].origin.kind is SourceKind.USER_TOML
        assert "project-scope" in rejected[0].reason

    def test_include_rejected_captures_renamed_env(self, tmp_path: Path) -> None:
        # A real run raises on NAB_RESOLUTION; collecting records it under
        # the resolution key instead, so explain --include-rejected lists it.
        _project(tmp_path, 'resolution = "lowest"\n')
        eff = _resolve(
            SourceRoots(project_dir=tmp_path),
            environ={"NAB_RESOLUTION": "highest"},
            collect_rejected=True,
        )
        rejected = eff["resolution"].rejected
        assert len(rejected) == 1
        assert rejected[0].origin.kind is SourceKind.ENV
        assert rejected[0].origin.label == "NAB_RESOLUTION"
        assert "not env-settable" in rejected[0].reason

    def test_include_rejected_captures_unknown_env(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff, rejected = _resolve_collect(
            SourceRoots(project_dir=tmp_path),
            environ={"NAB_OFLINE": "1"},
            collect_rejected=True,
        )
        # The unknown var does not crash; it is recorded as an orphan
        # (its key names no option) so render_list can surface it.
        assert eff["offline"].value is False
        orphans = orphan_rejections(rejected)
        assert len(orphans) == 1
        assert orphans[0].origin.label == "NAB_OFLINE"

    def test_include_rejected_captures_unknown_toml_key(self, tmp_path: Path) -> None:
        # An unknown standalone nab.toml key crashes a real run but, when a
        # rejections sink is supplied (explain --include-rejected), is
        # recorded as an orphan instead of crashing the inspector.
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", "typoo = 1\n")
        eff, rejected = _resolve_collect(
            SourceRoots(user_toml=user, project_dir=tmp_path),
            collect_rejected=True,
        )
        assert eff["offline"].value is False
        orphans = orphan_rejections(rejected)
        assert len(orphans) == 1
        assert orphans[0].key == "typoo"
        assert orphans[0].origin.kind is SourceKind.USER_TOML


class TestLoadTomlLayerDirect:
    def test_unknown_key_in_nab_toml_errors(self, tmp_path: Path) -> None:
        # A standalone nab.toml has no other parser, so a typo'd top-level
        # key must crash naming the file, the same way an unknown NAB_*
        # env var does, rather than be silently dropped.
        path = _write(tmp_path / "nab.toml", 'resolutionn = "lowest"\n')
        with pytest.raises(SourceConfigError, match="resolutionn.*not a valid"):
            _load_toml_layer(path, SourceKind.USER_TOML)

    def test_unknown_key_in_pyproject_errors(self, tmp_path: Path) -> None:
        # A typo'd [tool.nab] key is rejected by the loader too, so the
        # inspector reports it rather than silently dropping it (the resolve
        # path rejects it earlier, in read_pyproject_config).
        path = _write(
            tmp_path / "pyproject.toml",
            '[tool.nab]\nresolutionn = "lowest"\n',
        )
        with pytest.raises(SourceConfigError, match="resolutionn.*not a valid"):
            _load_toml_layer(path, SourceKind.PYPROJECT)

    def test_unknown_key_in_pyproject_recorded_when_collecting(
        self, tmp_path: Path
    ) -> None:
        # With a rejections sink (explain/list --include-rejected) the typo
        # is recorded and the valid keys still parse.
        path = _write(
            tmp_path / "pyproject.toml",
            '[tool.nab]\nresolutionn = "lowest"\nresolution = "lowest"\n',
        )
        rejections: list[RejectedLayer] = []
        layer = _load_toml_layer(path, SourceKind.PYPROJECT, rejections=rejections)
        assert layer.values == {"resolution": ResolutionStrategy.LOWEST}
        assert [r.key for r in rejections] == ["resolutionn"]

    def test_malformed_toml_errors(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "nab.toml", "offline = \n")
        with pytest.raises(SourceConfigError, match="not valid TOML"):
            _load_toml_layer(path, SourceKind.USER_TOML)

    def test_non_utf8_nab_toml_errors(self, tmp_path: Path) -> None:
        # TOML is UTF-8, so a latin-1 byte makes the file invalid TOML.
        path = tmp_path / "nab.toml"
        path.write_bytes(b'cache-dir = "\xe9"\n')
        with pytest.raises(SourceConfigError, match="not valid TOML"):
            _load_toml_layer(path, SourceKind.USER_TOML)

    def test_non_utf8_pyproject_errors(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_bytes(b'[project]\ndescription = "\xe9"\n')
        with pytest.raises(SourceConfigError, match="not valid TOML"):
            _load_toml_layer(path, SourceKind.PYPROJECT)

    def test_unreadable_file_errors(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "nab.toml", 'resolution = "lowest"\n')
        denied = PermissionError(errno.EACCES, "Permission denied", str(path))
        with (
            patch.object(Path, "open", side_effect=denied),
            pytest.raises(SourceConfigError, match="cannot read .*Permission denied"),
        ):
            _load_toml_layer(path, SourceKind.USER_TOML)

    def test_pyproject_without_tool_nab_is_empty(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "pyproject.toml", '[project]\nname = "x"\n')
        layer = _load_toml_layer(path, SourceKind.PYPROJECT)
        assert layer.values == {}

    def test_pyproject_tool_not_table(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "pyproject.toml", "tool = 3\n")
        layer = _load_toml_layer(path, SourceKind.PYPROJECT)
        assert layer.values == {}

    def test_pyproject_tool_nab_not_table(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "pyproject.toml", "[tool]\nnab = 3\n")
        with pytest.raises(SourceConfigError, match="must be a table, got int"):
            _load_toml_layer(path, SourceKind.PYPROJECT)


class TestDiscoverAndMissing:
    def test_missing_files_skipped(self, tmp_path: Path) -> None:
        roots = SourceRoots(
            system_toml=tmp_path / "nope-sys.toml",
            user_toml=tmp_path / "nope-user.toml",
            project_dir=tmp_path / "nope-dir",
        )
        layers = discover_layers(roots)
        assert layers == []

    def test_none_roots_skipped(self, tmp_path: Path) -> None:
        eff = _resolve(SourceRoots())
        assert eff["offline"].value is False

    def test_directory_named_config_errors(self, tmp_path: Path) -> None:
        # A nab.toml that exists as a directory (an accidental `mkdir`)
        # must crash naming it, not be silently dropped by an is_file()
        # filter.
        _project(tmp_path)
        (tmp_path / "nab.toml").mkdir()
        with pytest.raises(SourceConfigError, match="not a regular file"):
            discover_layers(SourceRoots(project_dir=tmp_path))

    def test_unsearchable_parent_reports_the_errno(
        self,
        tmp_path: Path,
        deny_access: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        # An unsearchable parent directory lands EACCES on the presence
        # check's stat.  The source is present, so it must reach the read
        # and be named there, neither skipped as absent nor escaping as a
        # raw PermissionError.
        pyproject = _project(tmp_path)
        with (
            deny_access(pyproject),
            pytest.raises(SourceConfigError, match="cannot read .*Permission denied"),
        ):
            discover_layers(SourceRoots(project_dir=tmp_path))

    def test_read_pyproject_false_keeps_project_nab_toml(self, tmp_path: Path) -> None:
        _write(tmp_path / "pyproject.toml", "[project\n")
        _write(tmp_path / "nab.toml", 'resolution = "lowest"\n')
        layers = discover_layers(
            SourceRoots(project_dir=tmp_path), read_pyproject=False
        )
        assert [layer.origin.kind for layer in layers] == [SourceKind.PROJECT_TOML]
        assert layers[0].values["resolution"] is ResolutionStrategy.LOWEST


class TestRenderers:
    def test_render_list(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "lowest"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_list(eff)
        assert "resolution" in out
        assert "lowest" in out
        assert "offline" in out
        assert "cache-dir" in out
        assert "<computed>" in out  # cache-dir default render

    def test_renderers_label_pyproject_as_project(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "lowest"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))

        row = next(
            line
            for line in render_list(eff).splitlines()
            if line.startswith("resolution")
        )
        assert row.split()[2] == "project"

        winner = next(
            line
            for line in render_explain(eff, "resolution").splitlines()
            if line.startswith(">")
        )
        assert winner.split()[1] == "project"

    def test_render_list_surfaces_orphan_rejection(self, tmp_path: Path) -> None:
        # An unknown NAB_* var attaches to no option, so it is unreachable
        # from explain; render_list surfaces it in a trailing section.
        _project(tmp_path)
        eff, rejected = _resolve_collect(
            SourceRoots(project_dir=tmp_path),
            environ={"NAB_OFLINE": "1"},
            collect_rejected=True,
        )
        out = render_list(eff, rejected=rejected)
        assert "rejected:" in out
        assert "NAB_OFLINE" in out

    def test_render_list_surfaces_known_key_gate_rejection(
        self, tmp_path: Path
    ) -> None:
        # A PROJECT key set in a user nab.toml attaches to its option, so it
        # is reachable from explain; render_list also lists it (it was
        # previously shown only by explain, not by list).
        _project(tmp_path)
        user_toml = tmp_path / "user.toml"
        user_toml.write_text('resolution = "lowest"\n', encoding="utf-8")
        eff, rejected = _resolve_collect(
            SourceRoots(project_dir=tmp_path, user_toml=user_toml),
            collect_rejected=True,
        )
        out = render_list(eff, rejected=rejected)
        assert "rejected:" in out
        assert "resolution" in out
        assert str(user_toml) in out

    def test_render_list_no_rejected_section_when_clean(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_list(eff)
        assert "rejected:" not in out

    def test_render_get(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert render_get(eff, "offline") == "false\n"

    def test_render_get_true(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path), cli={"offline": True})
        assert render_get(eff, "offline") == "true\n"

    def test_render_get_cache_dir_set(self, tmp_path: Path) -> None:
        _project(tmp_path)
        cache_dir = Path("/c/cli")
        eff = _resolve(SourceRoots(project_dir=tmp_path), cli={"cache-dir": cache_dir})
        assert render_get(eff, "cache-dir") == f"{cache_dir}\n"

    def test_render_get_unknown_key(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        with pytest.raises(SourceConfigError, match="unknown config key"):
            render_get(eff, "bogus")

    def test_render_explain_winner_and_shadowed(self, tmp_path: Path) -> None:
        # A scalar shadow across two precedence levels (pyproject then a
        # CLI override) shows winner/shadowed.
        _project(tmp_path, 'resolution = "lowest"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path), cli={"resolution": "highest"})
        out = render_explain(eff, "resolution")
        lines = out.splitlines()
        assert lines[0].startswith("resolution (project,")
        assert any(line.startswith(">") and "winner" in line for line in lines)
        assert any("shadowed" in line for line in lines)

    def test_render_explain_default_only(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_explain(eff, "cache-dir")
        assert "builtin-default" in out
        assert "> " in out

    def test_render_explain_unknown_key(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        with pytest.raises(SourceConfigError, match="unknown config key"):
            render_explain(eff, "bogus")

    def test_render_explain_include_rejected(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "lowest"\n')
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", 'resolution = "highest"\n')
        eff = _resolve(
            SourceRoots(user_toml=user, project_dir=tmp_path),
            collect_rejected=True,
        )
        out = render_explain(eff, "resolution", include_rejected=True)
        assert "rejected" in out
        assert "project-scope" in out

    def test_render_explain_docstring_names_every_status(self, tmp_path: Path) -> None:
        # One source per status: the user file is rejected (project-scope
        # key), the pyproject binding is shadowed, and the CLI wins.
        _project(tmp_path, 'resolution = "lowest"\n')
        user = _write(tmp_path / "usr" / "nab.toml", 'resolution = "highest"\n')

        eff = _resolve(
            SourceRoots(user_toml=user, project_dir=tmp_path),
            cli={"resolution": "highest"},
            collect_rejected=True,
        )
        printed = render_explain(eff, "resolution", include_rejected=True)

        doc = render_explain.__doc__ or ""
        for status in ("winner", "shadowed", "rejected"):
            assert status in printed, status
            assert f"``{status}``" in doc, status


class TestReproducibilityNotice:
    """A CLI PROJECT override is never silent; the lock can be reproduced."""

    def test_notice_lists_cli_project_overrides(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path), cli={"resolution": "lowest"})
        notice = project_cli_override_notice(eff)
        assert notice is not None
        assert "does not derive from the committed" in notice
        assert "--project-resolution -> lowest" in notice

    def test_notice_inspection_wording(self, tmp_path: Path) -> None:
        # produces_lock=False (the nab config inspector) makes no lock claim.
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path), cli={"resolution": "lowest"})
        notice = project_cli_override_notice(eff, produces_lock=False)
        assert notice is not None
        assert "the lock they produce" not in notice
        assert "reflect that override" in notice
        assert "--project-resolution -> lowest" in notice

    def test_no_notice_without_cli_project_override(self, tmp_path: Path) -> None:
        _project(tmp_path, 'resolution = "lowest"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert project_cli_override_notice(eff) is None

    def test_records_lists_cli_project_override_pairs(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path), cli={"resolution": "lowest"})
        assert project_cli_override_records(eff) == (
            ("--project-resolution", "lowest"),
        )

    def test_records_empty_without_cli_project_override(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path), cli={"offline": True})
        assert project_cli_override_records(eff) == ()

    def test_user_cli_override_does_not_trigger_notice(self, tmp_path: Path) -> None:
        # offline is a USER option; setting it on the CLI never changes the
        # resolved set, so it is not a reproducibility hazard.
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path), cli={"offline": True})
        assert project_cli_override_notice(eff) is None

    def test_build_cli_overrides_keyed_by_registry(self, tmp_path: Path) -> None:
        # build_cli_overrides maps cli_param locals to registry keys, drops
        # the unset scalars (None) and the unset arrays (empty tuple), and
        # keeps a non-empty array.
        overrides = build_cli_overrides(
            {
                "project_resolution": "lowest",
                "offline": None,
                "cache_dir": Path("/c"),
                "http_backend": None,
                "max_concurrency": 16,
                "project_mode": None,
                "project_requires_python": None,
                "project_uploaded_prior_to": None,
                "project_dist_policy": "sdist-only",
                "project_build_policy": None,
                "project_build_requires_depth": None,
                "project_decision_order": None,
                "project_base_group": None,
                "project_build_group": None,
                "project_constraint": (),
                "project_default_group": ("dev",),
            }
        )
        assert overrides == {
            "resolution": "lowest",
            "cache-dir": Path("/c"),
            "max-concurrency": 16,
            "dist-policy": "sdist-only",
            "default-groups": ("dev",),
        }


class TestParserFoldHelpers:
    def test_pyproject_registry_keys_are_project_scope(self) -> None:
        # Every PROJECT-scope row is legitimate in pyproject [tool.nab];
        # the USER rows (offline, cache-dir) are not.
        assert pyproject_registry_keys() == frozenset(
            {
                "resolution",
                "decision-order",
                "base-group",
                "build-group",
                "mode",
                "constraints",
                "default-groups",
                "requires-python",
                "uploaded-prior-to",
                "dist-policy",
                "build-policy",
                "build-requires-depth",
                "environment",
                "marker-environment",
                "vcs",
                "workspace",
                "indexes",
                "local-sources",
                "vcs-sources",
                "archive-sources",
                "packages",
                "package-rules",
                "index",
                "conflicts",
                "matrix",
            }
        )

    def test_reject_user_key_offline(self) -> None:
        with pytest.raises(SourceConfigError, match="project-scope only"):
            reject_user_keys_in_pyproject({"offline": True})

    def test_reject_user_key_cache_dir(self) -> None:
        with pytest.raises(SourceConfigError, match="project-scope only"):
            reject_user_keys_in_pyproject({"cache-dir": "/x"})

    def test_project_key_allowed(self) -> None:
        # resolution is PROJECT-scope, so it is legitimate in pyproject.
        reject_user_keys_in_pyproject({"resolution": "lowest"})

    def test_unknown_key_ignored(self) -> None:
        # A PROJECT-scope registry key (mode) is legitimate in pyproject,
        # so the USER-key gate leaves it for the pyproject parser.
        reject_user_keys_in_pyproject({"mode": "universal"})

    def test_unowned_key_ignored(self) -> None:
        # A key the registry does not own is left to the pyproject parser.
        reject_user_keys_in_pyproject({"trust-unverified-sdist-deps": True})


class TestRegistryShape:
    """The registry rows themselves: invariants the conformance gate relies on."""

    def test_keys_unique(self) -> None:
        keys = [spec.key for spec in OPTIONS]
        assert len(keys) == len(set(keys))

    def test_project_options_have_no_env_var(self) -> None:
        for spec in OPTIONS:
            if spec.scope is Scope.PROJECT:
                assert spec.env_var is None

    def test_user_options_have_env_var(self) -> None:
        for spec in OPTIONS:
            if spec.scope is Scope.USER:
                assert spec.env_var is not None

    def test_user_run_knobs_registered(self) -> None:
        by_key = {spec.key: spec for spec in OPTIONS}
        for key, env in (
            ("http-backend", "NAB_HTTP_BACKEND"),
            ("max-concurrency", "NAB_MAX_CONCURRENCY"),
        ):
            assert by_key[key].scope is Scope.USER
            assert by_key[key].env_var == env

    def test_origin_scope_for_every_kind(self) -> None:
        # PYPROJECT reports "project": it shares the project precedence level.
        assert {kind: Origin(kind, "x").scope for kind in SourceKind} == {
            SourceKind.DEFAULT: "default",
            SourceKind.SYSTEM_TOML: "system",
            SourceKind.USER_TOML: "user",
            SourceKind.PYPROJECT: "project",
            SourceKind.PROJECT_TOML: "project",
            SourceKind.ENV: "env",
            SourceKind.CLI: "cli",
        }

    def test_layer_dataclass_roundtrip(self) -> None:
        layer = Layer(Origin(SourceKind.CLI, "cli"), {"offline": True})
        assert layer.values["offline"] is True

    def test_allowed_in_toml_project(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "resolution")
        assert spec.allowed_in_toml(SourceKind.PYPROJECT)
        assert spec.allowed_in_toml(SourceKind.PROJECT_TOML)
        assert not spec.allowed_in_toml(SourceKind.USER_TOML)

    def test_allowed_in_toml_user(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "offline")
        assert not spec.allowed_in_toml(SourceKind.PYPROJECT)
        assert spec.allowed_in_toml(SourceKind.USER_TOML)
        assert spec.allowed_in_toml(SourceKind.SYSTEM_TOML)
        assert spec.allowed_in_toml(SourceKind.PROJECT_TOML)


class TestScalarProjectOptions:
    """The scalar PROJECT options: mode, requires-python, uploaded-prior-to,
    dist-policy, build-policy.  Each reuses the single-environment parser,
    so the value and validation messages match the pyproject path; the new
    surface is the project-dir nab.toml home, the category gate, the
    cross-file conflict check, and nab config visibility.
    """

    def test_mode_from_pyproject(self, tmp_path: Path) -> None:
        _project(tmp_path, 'mode = "universal"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["mode"].value is ResolveMode.UNIVERSAL
        assert eff["mode"].origin.kind is SourceKind.PYPROJECT

    def test_mode_from_project_nab_toml(self, tmp_path: Path) -> None:
        # New capability: a PROJECT scalar set in the project-dir nab.toml.
        _project(tmp_path)
        _write(tmp_path / "nab.toml", 'mode = "universal"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["mode"].value is ResolveMode.UNIVERSAL
        assert eff["mode"].origin.kind is SourceKind.PROJECT_TOML

    def test_mode_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", 'mode = "universal"\n')
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_mode_in_system_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        sys_toml = _write(tmp_path / "etc" / "nab.toml", 'mode = "universal"\n')
        roots = SourceRoots(system_toml=sys_toml, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_mode_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(
                SourceRoots(project_dir=tmp_path), environ={"NAB_MODE": "universal"}
            )
        assert "NAB_MODE" in caplog.text
        assert "not env-settable" in caplog.text

    def test_mode_bad_value_keeps_existing_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'mode = "sideways"\n')
        with pytest.raises(SourceConfigError, match="mode must be one of"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_mode_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(tmp_path, 'mode = "universal"\n')
        _write(tmp_path / "nab.toml", 'mode = "specific"\n')
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_mode_identical_across_files_ok(self, tmp_path: Path) -> None:
        _project(tmp_path, 'mode = "universal"\n')
        _write(tmp_path / "nab.toml", 'mode = "universal"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["mode"].value is ResolveMode.UNIVERSAL

    def test_requires_python_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(tmp_path / "nab.toml", 'requires-python = ">=3.11"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["requires-python"].value == ">=3.11"
        assert eff["requires-python"].origin.kind is SourceKind.PROJECT_TOML

    def test_requires_python_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab" / "nab.toml", 'requires-python = ">=3.11"\n'
        )
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))

    def test_requires_python_bad_specifier_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'requires-python = "3.11"\n')
        with pytest.raises(SourceConfigError, match="PEP 440 specifier"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_dist_policy_scalar_from_pyproject(self, tmp_path: Path) -> None:
        _project(tmp_path, 'dist-policy = "sdist-only"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["dist-policy"].value == (DistPolicy.SDIST_ONLY, False)

    def test_dist_policy_table_folds_trust(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            "[tool.nab.dist-policy]\n"
            'policy = "sdist-only"\n'
            "trust-unverified-deps = true\n",
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["dist-policy"].value == (DistPolicy.SDIST_ONLY, True)

    def test_dist_policy_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab" / "nab.toml", 'dist-policy = "sdist-only"\n'
        )
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))

    def test_dist_policy_bad_value_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'dist-policy = "nonsense"\n')
        with pytest.raises(SourceConfigError, match="dist-policy must be one of"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_build_policy_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(tmp_path / "nab.toml", 'build-policy = "never"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["build-policy"].value is BuildPolicy.NEVER

    def test_build_policy_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab" / "nab.toml", 'build-policy = "never"\n')
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))

    def test_build_policy_bad_value_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'build-policy = "nonsense"\n')
        with pytest.raises(SourceConfigError, match="build-policy must be one of"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_uploaded_prior_to_absolute_normalised(self, tmp_path: Path) -> None:
        _project(tmp_path, 'uploaded-prior-to = "2026-05-01T00:00:00Z"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert str(eff["uploaded-prior-to"].value) == "2026-05-01 00:00:00+00:00"

    def test_uploaded_prior_to_duration_carried_raw(self, tmp_path: Path) -> None:
        # Hazard: a P<n>D duration must not re-anchor to now in the registry
        # path, so it is carried as its raw string (anchor-free).
        _project(tmp_path, 'uploaded-prior-to = "P4D"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["uploaded-prior-to"].value == "P4D"

    def test_uploaded_prior_to_duration_identical_across_files_ok(
        self, tmp_path: Path
    ) -> None:
        # Two identical raw durations across the project files must not read
        # as conflicting (they would if each re-anchored to now).
        _project(tmp_path, 'uploaded-prior-to = "P4D"\n')
        _write(tmp_path / "nab.toml", 'uploaded-prior-to = "P4D"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["uploaded-prior-to"].value == "P4D"

    def test_uploaded_prior_to_duration_cross_file_conflict(
        self, tmp_path: Path
    ) -> None:
        _project(tmp_path, 'uploaded-prior-to = "P4D"\n')
        _write(tmp_path / "nab.toml", 'uploaded-prior-to = "P9D"\n')
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_uploaded_prior_to_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab" / "nab.toml", 'uploaded-prior-to = "P4D"\n'
        )
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(SourceRoots(user_toml=user, project_dir=tmp_path))

    def test_uploaded_prior_to_bad_value_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'uploaded-prior-to = "not-a-date"\n')
        with pytest.raises(SourceConfigError, match="ISO 8601 datetime"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_scalar_keys_visible_in_list(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            'mode = "universal"\n'
            'requires-python = ">=3.11"\n'
            'build-policy = "never"\n'
            'dist-policy = "sdist-only"\n'
            'uploaded-prior-to = "P4D"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11"\n'
            'platforms = ["linux_x86_64"]\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_list(eff)
        for key in (
            "mode",
            "requires-python",
            "uploaded-prior-to",
            "dist-policy",
            "build-policy",
        ):
            assert key in out

    def test_dist_policy_default_render(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["dist-policy"].spec.render(eff["dist-policy"].value) == (
            "wheel-or-sdist"
        )

    def test_uploaded_prior_to_default_render(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["uploaded-prior-to"].spec.render(None) == "<none>"

    def test_requires_python_default_render(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["requires-python"].spec.render(None) == "<none>"


class TestTableProjectOptions:
    """The PROJECT table options: marker-environment, vcs, workspace.

    Each is a file-only PROJECT row (no CLI flag): settable from
    pyproject [tool.nab] or a project-dir nab.toml, gated out of
    user/system files and NAB_*, subject to the cross-file conflict check,
    and visible in ``nab config``.  Each reuses the single-environment
    parser so the parsed value and every validation message match the
    pyproject path.
    """

    def test_marker_environment_from_pyproject(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.marker-environment]\nplatform_system = "Linux"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["marker-environment"].value == {"platform_system": "Linux"}
        assert eff["marker-environment"].origin.kind is SourceKind.PYPROJECT

    def test_marker_environment_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            '[marker-environment]\nplatform_system = "Darwin"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["marker-environment"].value == {"platform_system": "Darwin"}
        assert eff["marker-environment"].origin.kind is SourceKind.PROJECT_TOML

    def test_marker_environment_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab.toml",
            '[marker-environment]\nplatform_system = "Linux"\n',
        )
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_marker_environment_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(
                SourceRoots(project_dir=tmp_path),
                environ={"NAB_MARKER_ENVIRONMENT": "x"},
            )
        assert "NAB_MARKER_ENVIRONMENT" in caplog.text
        assert "not env-settable" in caplog.text

    def test_marker_environment_bad_value_keeps_message(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.marker-environment]\nnonsense_var = "x"\n',
        )
        with pytest.raises(SourceConfigError, match="unknown marker-environment"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_marker_environment_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.marker-environment]\nplatform_system = "Linux"\n',
        )
        _write(
            tmp_path / "nab.toml",
            '[marker-environment]\nplatform_system = "Darwin"\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_marker_environment_identical_across_files_ok(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.marker-environment]\nplatform_system = "Linux"\n',
        )
        _write(
            tmp_path / "nab.toml",
            '[marker-environment]\nplatform_system = "Linux"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["marker-environment"].value == {"platform_system": "Linux"}
        assert eff["marker-environment"].origin.kind is SourceKind.PROJECT_TOML

    def test_marker_environment_disjoint_vars_conflict(self, tmp_path: Path) -> None:
        # The table is compared whole, so two project files setting
        # different marker vars conflict rather than folding into one
        # environment neither file declares.
        _project(
            tmp_path,
            '[tool.nab.marker-environment]\nplatform_system = "Linux"\n',
        )
        _write(
            tmp_path / "nab.toml",
            '[marker-environment]\nsys_platform = "linux"\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_marker_environment_default_render(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["marker-environment"].spec.render({}) == "<none>"

    def test_marker_environment_render_sorted(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "marker-environment")
        rendered = spec.render({"sys_platform": "linux", "platform_system": "Linux"})
        assert rendered == "platform_system=Linux, sys_platform=linux"

    def test_vcs_from_pyproject(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.vcs]\npolicy = "allow"\nallowed-schemes = ["git+https"]\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        value = eff["vcs"].value
        assert value.policy is VcsPolicy.ALLOW
        assert value.allowed_schemes == frozenset({"git+https"})
        assert eff["vcs"].origin.kind is SourceKind.PYPROJECT

    def test_vcs_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(tmp_path / "nab.toml", '[vcs]\npolicy = "allow"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["vcs"].value.policy is VcsPolicy.ALLOW
        assert eff["vcs"].origin.kind is SourceKind.PROJECT_TOML

    def test_vcs_in_system_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        sys_toml = _write(tmp_path / "etc" / "nab.toml", '[vcs]\npolicy = "allow"\n')
        roots = SourceRoots(system_toml=sys_toml, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_vcs_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(SourceRoots(project_dir=tmp_path), environ={"NAB_VCS": "x"})
        assert "NAB_VCS" in caplog.text
        assert "not env-settable" in caplog.text

    def test_vcs_bad_value_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.vcs]\npolicy = "sometimes"\n')
        with pytest.raises(SourceConfigError, match="vcs.policy must be one of"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_vcs_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.vcs]\npolicy = "allow"\n')
        _write(tmp_path / "nab.toml", '[vcs]\npolicy = "block"\n')
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_vcs_cross_file_disjoint_sub_keys_still_conflict(
        self, tmp_path: Path
    ) -> None:
        # vcs is compared as one value, not folded sub-key by sub-key, so
        # disjoint sub-keys across the two project files still conflict.
        _project(tmp_path, '[tool.nab.vcs]\npolicy = "allow"\n')
        _write(tmp_path / "nab.toml", "[vcs]\nrequire-pin = true\n")
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_vcs_identical_across_files_ok(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.vcs]\npolicy = "allow"\n')
        _write(tmp_path / "nab.toml", '[vcs]\npolicy = "allow"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["vcs"].value.policy is VcsPolicy.ALLOW
        assert eff["vcs"].origin.kind is SourceKind.PROJECT_TOML

    def test_vcs_default_render(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["vcs"].spec.render(VcsConfig()) == "policy=block"

    def test_vcs_render_full(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "vcs")
        rendered = spec.render(
            VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.test/repo",),
                require_pin=False,
            )
        )
        assert "policy=allow" in rendered
        assert "allowed-schemes=['git+https']" in rendered
        assert "allowed-repos=['https://example.test/repo']" in rendered
        assert "require-pin=false" in rendered

    def test_workspace_from_pyproject(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.workspace]\nmembers = ["pkgs/a"]\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["workspace"].value == WorkspaceConfig(members=("pkgs/a",))
        assert eff["workspace"].origin.kind is SourceKind.PYPROJECT

    def test_workspace_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(tmp_path / "nab.toml", '[workspace]\nmembers = ["pkgs/b"]\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["workspace"].value == WorkspaceConfig(members=("pkgs/b",))
        assert eff["workspace"].origin.kind is SourceKind.PROJECT_TOML

    def test_workspace_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab.toml", '[workspace]\nmembers = ["pkgs/a"]\n'
        )
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_workspace_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(SourceRoots(project_dir=tmp_path), environ={"NAB_WORKSPACE": "x"})
        assert "NAB_WORKSPACE" in caplog.text
        assert "not env-settable" in caplog.text

    def test_workspace_bad_value_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.workspace]\nmember = ["pkgs/a"]\n')
        with pytest.raises(SourceConfigError, match="unknown .tool.nab.workspace"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_workspace_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.workspace]\nmembers = ["pkgs/a"]\n')
        _write(tmp_path / "nab.toml", '[workspace]\nmembers = ["pkgs/b"]\n')
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_workspace_identical_across_files_ok(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.workspace]\nmembers = ["pkgs/a"]\n')
        _write(tmp_path / "nab.toml", '[workspace]\nmembers = ["pkgs/a"]\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["workspace"].value == WorkspaceConfig(members=("pkgs/a",))
        assert eff["workspace"].origin.kind is SourceKind.PROJECT_TOML

    def test_workspace_default_render(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["workspace"].value is None
        assert eff["workspace"].spec.render(None) == "<none>"

    def test_workspace_render_members(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "workspace")
        assert spec.render(WorkspaceConfig(members=("a", "b"))) == "members=['a', 'b']"

    def test_table_keys_visible_in_config(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.marker-environment]\nplatform_system = "Linux"\n'
            "[tool.nab.vcs]\n"
            'policy = "allow"\n'
            "[tool.nab.workspace]\n"
            'members = ["pkgs/a"]\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_list(eff)
        for key in ("marker-environment", "vcs", "workspace"):
            assert key in out
        assert render_get(eff, "vcs").strip().startswith("policy=allow")
        explained = render_explain(eff, "workspace")
        assert "members=['pkgs/a']" in explained

    def test_table_rows_are_file_only(self) -> None:
        for key in ("marker-environment", "vcs", "workspace"):
            spec = next(s for s in OPTIONS if s.key == key)
            assert spec.cli_flag is None
            assert spec.cli_param is None


class TestArrayProjectOptions:
    """The PROJECT array options: constraints, default-groups.

    Each reuses the single-environment list parser, is gated PROJECT-only,
    and is visible in ``nab config``.  A list is one value like any other
    row, so the highest source supplies the whole of it and the two
    project files setting it differently is a conflict.
    """

    def test_constraints_from_pyproject(self, tmp_path: Path) -> None:
        _project(tmp_path, 'constraints = ["urllib3<2"]\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["constraints"].value == ("urllib3<2",)
        assert eff["constraints"].origin.kind is SourceKind.PYPROJECT

    def test_constraints_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(tmp_path / "nab.toml", 'constraints = ["certifi>=2024"]\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["constraints"].value == ("certifi>=2024",)
        assert eff["constraints"].origin.kind is SourceKind.PROJECT_TOML

    def test_constraints_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", 'constraints = ["urllib3<2"]\n')
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_constraints_in_system_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        sys_toml = _write(tmp_path / "etc" / "nab.toml", 'constraints = ["a"]\n')
        roots = SourceRoots(system_toml=sys_toml, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_constraints_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(
                SourceRoots(project_dir=tmp_path), environ={"NAB_CONSTRAINTS": "x"}
            )
        assert "NAB_CONSTRAINTS" in caplog.text
        assert "not env-settable" in caplog.text

    def test_constraints_bad_pep508_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'constraints = ["not a valid !! req"]\n')
        with pytest.raises(SourceConfigError, match="is not a valid requirement"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_constraints_non_list_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'constraints = "urllib3<2"\n')
        with pytest.raises(SourceConfigError, match="must be a list of strings"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_constraints_cross_file_conflict(self, tmp_path: Path) -> None:
        # A list at the same rung raises the hard error a scalar does.
        _project(tmp_path, 'constraints = ["urllib3<2"]\n')
        _write(tmp_path / "nab.toml", 'constraints = ["certifi>=2024"]\n')
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_constraints_identical_lists_across_files_ok(self, tmp_path: Path) -> None:
        _project(tmp_path, 'constraints = ["urllib3<2"]\n')
        _write(tmp_path / "nab.toml", 'constraints = ["urllib3<2"]\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["constraints"].value == ("urllib3<2",)
        assert eff["constraints"].origin.kind is SourceKind.PROJECT_TOML

    def test_default_groups_from_pyproject(self, tmp_path: Path) -> None:
        _project(tmp_path, 'default-groups = ["dev"]\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["default-groups"].value == ("dev",)
        assert eff["default-groups"].origin.kind is SourceKind.PYPROJECT

    def test_default_groups_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(tmp_path / "nab.toml", 'default-groups = ["docs"]\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["default-groups"].value == ("docs",)
        assert eff["default-groups"].origin.kind is SourceKind.PROJECT_TOML

    def test_default_groups_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", 'default-groups = ["dev"]\n')
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_default_groups_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(
                SourceRoots(project_dir=tmp_path),
                environ={"NAB_DEFAULT_GROUPS": "x"},
            )
        assert "NAB_DEFAULT_GROUPS" in caplog.text
        assert "not env-settable" in caplog.text

    def test_default_groups_non_string_item_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, "default-groups = [1]\n")
        with pytest.raises(SourceConfigError, match="must be a string"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_default_groups_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(tmp_path, 'default-groups = ["dev"]\n')
        _write(tmp_path / "nab.toml", 'default-groups = ["docs"]\n')
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_array_defaults_empty(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["constraints"].value == ()
        assert eff["constraints"].origin.kind is SourceKind.DEFAULT
        assert eff["default-groups"].value == ()

    def test_array_default_render(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "constraints")
        assert spec.render(()) == "<none>"
        assert spec.render(("a", "b")) == "a, b"

    def test_array_keys_visible_in_config(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            'constraints = ["urllib3<2"]\ndefault-groups = ["dev"]\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_list(eff)
        for key in ("constraints", "default-groups"):
            assert key in out
        assert render_get(eff, "constraints").strip() == "urllib3<2"
        explained = render_explain(eff, "default-groups")
        assert "dev" in explained

    def test_array_explain_shows_both_project_files(self, tmp_path: Path) -> None:
        # Both project-file bindings show in the explain stack, ordered
        # low -> high, even though only the higher one is the value.
        _project(tmp_path, 'constraints = ["urllib3<2"]\n')
        _write(tmp_path / "nab.toml", 'constraints = ["urllib3<2"]\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        stack_kinds = [origin.kind for origin, _ in eff["constraints"].stack]
        assert stack_kinds == [SourceKind.PYPROJECT, SourceKind.PROJECT_TOML]

    def test_array_cli_flag_replaces_the_file_list(self, tmp_path: Path) -> None:
        # The CLI is the highest rung, so its list is the whole value; the
        # file's list is shadowed rather than appended to.
        _project(tmp_path, 'constraints = ["urllib3<2"]\n')
        eff = _resolve(
            SourceRoots(project_dir=tmp_path), cli={"constraints": ("certifi>=2024",)}
        )
        assert eff["constraints"].value == ("certifi>=2024",)
        assert eff["constraints"].origin.kind is SourceKind.CLI
        explained = render_explain(eff, "constraints")
        assert "urllib3<2" in explained
        assert "shadowed" in explained
        assert render_get(eff, "constraints").strip() == "certifi>=2024"

    def test_array_project_rows_have_repeatable_cli_flags(self) -> None:
        for key, flag in (
            ("constraints", "--project-constraint"),
            ("default-groups", "--project-default-group"),
        ):
            spec = next(s for s in OPTIONS if s.key == key)
            assert spec.cli_flag == flag


class TestArrayOfTablesSources:
    """The array-of-tables PROJECT sources: indexes, local-sources,
    vcs-sources.  Each reuses the single-environment parser per layer
    (shape/key/dup messages match the pyproject path), is gated
    PROJECT-only and file-only, and is visible in ``nab config``.  The
    declaring file owns the whole list, so the same-name dup check is a
    within-file check and the two project files declaring different lists
    is a conflict.
    """

    def test_indexes_from_pyproject(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[[tool.nab.indexes]]\nname = "pypi"\nurl = "https://pypi.org/simple/"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        value = eff["indexes"].value
        assert [i.name for i in value] == ["pypi"]
        assert eff["indexes"].origin.kind is SourceKind.PYPROJECT

    def test_indexes_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            '[[indexes]]\nname = "extra"\nurl = "https://extra/simple/"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert [i.name for i in eff["indexes"].value] == ["extra"]
        assert eff["indexes"].origin.kind is SourceKind.PROJECT_TOML

    def test_indexes_default_is_pypi(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        value = eff["indexes"].value
        assert [i.name for i in value] == ["pypi"]
        assert eff["indexes"].origin.kind is SourceKind.DEFAULT

    def test_indexes_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab.toml",
            '[[indexes]]\nname = "pypi"\nurl = "https://pypi.org/simple/"\n',
        )
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_indexes_in_system_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        sys_toml = _write(
            tmp_path / "etc" / "nab.toml",
            '[[indexes]]\nname = "pypi"\nurl = "https://pypi.org/simple/"\n',
        )
        roots = SourceRoots(system_toml=sys_toml, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_indexes_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(SourceRoots(project_dir=tmp_path), environ={"NAB_INDEXES": "x"})
        assert "NAB_INDEXES" in caplog.text
        assert "not env-settable" in caplog.text

    def test_indexes_bad_shape_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'indexes = "pypi"\n')
        with pytest.raises(SourceConfigError, match="must be an array of tables"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_indexes_unknown_key_keeps_message(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[[tool.nab.indexes]]\nname = "x"\nurl = "u"\nbogus = "y"\n',
        )
        with pytest.raises(SourceConfigError, match="unknown indexes"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_indexes_cross_file_conflict(self, tmp_path: Path) -> None:
        # A second index is added by editing the file that declares them,
        # not by declaring a rival list in the other project file.
        _project(
            tmp_path,
            '[[tool.nab.indexes]]\nname = "pypi"\nurl = "https://pypi.org/simple/"\n',
        )
        _write(
            tmp_path / "nab.toml",
            '[[indexes]]\nname = "extra"\nurl = "https://extra/simple/"\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_indexes_per_file_duplicate_name(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[[tool.nab.indexes]]\nname = "dup"\nurl = "u1"\n'
            '[[tool.nab.indexes]]\nname = "dup"\nurl = "u2"\n',
        )
        with pytest.raises(SourceConfigError, match="duplicate index name"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_indexes_render(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "indexes")
        assert spec.render(()) == "<none>"
        rendered = spec.render(spec.default)
        assert rendered == "pypi=https://pypi.org/simple/"

    def test_indexes_render_shows_a_pin(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "indexes")
        value = (
            IndexConfig("pypi", "https://pypi.org/simple/"),
            IndexConfig(
                "internal",
                "https://internal/simple/",
                serialization=SimpleSerialization.JSON,
            ),
        )
        assert spec.render(value) == (
            "pypi=https://pypi.org/simple/,"
            " internal=https://internal/simple/ serialization=json"
        )

    def test_indexes_type_label_lists_every_accepted_key(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "indexes")
        assert spec.type_label == "array-of-tables(name,url,serialization)"
        listed = spec.type_label.partition("(")[2].rstrip(")").split(",")
        assert set(listed) == _INDEX_KEYS

    def test_indexes_bad_serialization_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            '[[indexes]]\nname = "x"\nurl = "https://a/"\nserialization = "xml"\n',
        )
        with pytest.raises(SourceConfigError, match="must be one of"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_indexes_file_url_pin_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            '[[indexes]]\nname = "local"\nurl = "file:///x"\nserialization = "html"\n',
        )
        with pytest.raises(SourceConfigError, match="not settable on a file:// index"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_indexes_pin_is_per_entry(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            '[[indexes]]\nname = "pypi"\nurl = "https://pypi.org/simple/"\n'
            '[[indexes]]\nname = "extra"\nurl = "https://extra/simple/"\n'
            'serialization = "html"\n',
        )
        value = _resolve(SourceRoots(project_dir=tmp_path))["indexes"].value
        assert [i.name for i in value] == ["pypi", "extra"]
        assert value[0].serialization is SimpleSerialization.NEGOTIATE
        assert value[1].serialization is SimpleSerialization.HTML

    def test_local_sources_from_pyproject(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "pkg"\npath = "./libs/pkg"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        value = eff["local-sources"].value
        assert [s.name for s in value] == ["pkg"]
        # Path resolves relative to the declaring file's directory.
        assert value[0].path == str((tmp_path / "libs" / "pkg").resolve())
        assert eff["local-sources"].origin.kind is SourceKind.PYPROJECT

    def test_local_sources_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            '[[local-sources]]\nname = "pkg"\npath = "./pkg"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        value = eff["local-sources"].value
        assert value[0].path == str((tmp_path / "pkg").resolve())
        assert eff["local-sources"].origin.kind is SourceKind.PROJECT_TOML

    def test_local_sources_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab.toml",
            '[[local-sources]]\nname = "pkg"\npath = "./pkg"\n',
        )
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_local_sources_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(
                SourceRoots(project_dir=tmp_path),
                environ={"NAB_LOCAL_SOURCES": "x"},
            )
        assert "NAB_LOCAL_SOURCES" in caplog.text
        assert "not env-settable" in caplog.text

    def test_local_sources_bad_shape_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'local-sources = "pkg"\n')
        with pytest.raises(SourceConfigError, match="must be an array of tables"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_local_sources_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "a"\npath = "./a"\n',
        )
        _write(
            tmp_path / "nab.toml",
            '[[local-sources]]\nname = "b"\npath = "./b"\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_local_sources_render(self, tmp_path: Path) -> None:
        spec = next(s for s in OPTIONS if s.key == "local-sources")
        assert spec.render(()) == "<none>"
        _project(
            tmp_path,
            '[[tool.nab.local-sources]]\nname = "pkg"\npath = "./pkg"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_get(eff, "local-sources")
        assert out.startswith("pkg@")

    def test_vcs_sources_from_pyproject(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[[tool.nab.vcs-sources]]\nname = "pkg"\n'
            'url = "git+https://example/pkg.git@deadbeef"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert [s.name for s in eff["vcs-sources"].value] == ["pkg"]
        assert eff["vcs-sources"].origin.kind is SourceKind.PYPROJECT

    def test_vcs_sources_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            '[[vcs-sources]]\nname = "pkg"\nurl = "git+https://example/pkg.git@abc"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert [s.name for s in eff["vcs-sources"].value] == ["pkg"]
        assert eff["vcs-sources"].origin.kind is SourceKind.PROJECT_TOML

    def test_vcs_sources_in_system_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        sys_toml = _write(
            tmp_path / "etc" / "nab.toml",
            '[[vcs-sources]]\nname = "pkg"\nurl = "git+https://e/p.git@a"\n',
        )
        roots = SourceRoots(system_toml=sys_toml, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_vcs_sources_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(
                SourceRoots(project_dir=tmp_path),
                environ={"NAB_VCS_SOURCES": "x"},
            )
        assert "NAB_VCS_SOURCES" in caplog.text
        assert "not env-settable" in caplog.text

    def test_vcs_sources_bad_shape_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'vcs-sources = "pkg"\n')
        with pytest.raises(SourceConfigError, match="must be an array of tables"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_vcs_sources_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[[tool.nab.vcs-sources]]\nname = "a"\nurl = "git+https://e/a.git@1"\n',
        )
        _write(
            tmp_path / "nab.toml",
            '[[vcs-sources]]\nname = "b"\nurl = "git+https://e/b.git@2"\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_vcs_sources_render(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "vcs-sources")
        assert spec.render(()) == "<none>"
        rendered = spec.render((VcsSource(name="pkg", url="git+https://e/p.git@a"),))
        assert rendered == "pkg@git+https://e/p.git@a"

    def test_archive_sources_render(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "archive-sources")
        assert spec.render(()) == "<none>"
        url = "https://e/p.tar.gz#sha256=abc"
        rendered = spec.render((ArchiveSource(name="pkg", url=url),))
        assert rendered == f"pkg@{url}"

    def test_array_of_tables_rows_are_file_only(self) -> None:
        for key in ("indexes", "local-sources", "vcs-sources"):
            spec = next(s for s in OPTIONS if s.key == key)
            assert spec.cli_flag is None
            assert spec.cli_param is None

    def test_array_of_tables_keys_visible_in_config(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[[tool.nab.indexes]]\nname = "pypi"\nurl = "https://pypi.org/simple/"\n'
            '[[tool.nab.local-sources]]\nname = "pkg"\npath = "./pkg"\n'
            '[[tool.nab.vcs-sources]]\nname = "v"\nurl = "git+https://e/v.git@a"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_list(eff)
        for key in ("indexes", "local-sources", "vcs-sources"):
            assert key in out


class TestOverrideTables:
    """The per-package override tables: packages, package-rules, index.

    ``packages`` (name-keyed sugar) and ``package-rules`` (array of tables)
    both desugar into the same ``PackageOverride`` tuple; each is its own
    file-only PROJECT row that carries the same-field overlap check over
    its own surface.  ``index`` is a name-keyed table.  Each reuses the
    single-environment parser per layer so shape/key/body/overlap messages
    match the pyproject path; the cross-key checks (packages-vs-package-rules
    overlap, route or key names a declared index) run over the merged config
    instead.
    """

    def test_packages_from_pyproject(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.packages.lxml]\ndist-policy = "sdist-only"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        value = eff["packages"].value
        assert [o.name for o in value] == ["lxml"]
        assert eff["packages"].origin.kind is SourceKind.PYPROJECT

    def test_packages_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            '[packages.lxml]\ndist-policy = "sdist-only"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert [o.name for o in eff["packages"].value] == ["lxml"]
        assert eff["packages"].origin.kind is SourceKind.PROJECT_TOML

    def test_packages_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab.toml",
            '[packages.lxml]\ndist-policy = "sdist-only"\n',
        )
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_packages_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(SourceRoots(project_dir=tmp_path), environ={"NAB_PACKAGES": "x"})
        assert "NAB_PACKAGES" in caplog.text
        assert "not env-settable" in caplog.text

    def test_packages_bad_shape_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, '[[tool.nab.packages]]\nmatch = ["foo"]\n')
        with pytest.raises(SourceConfigError, match="name-keyed table form"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_packages_intra_file_overlap_keeps_message(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.packages.foo]\ndist-policy = "sdist-only"\n'
            '[tool.nab.packages."Foo"]\nbuild-policy = "build-remote"\n'
            '[tool.nab.packages."FOO"]\ndist-policy = "wheel-only"\n',
        )
        with pytest.raises(SourceConfigError, match="overlapping versions"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_packages_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.packages.lxml]\ndist-policy = "sdist-only"\n')
        _write(
            tmp_path / "nab.toml",
            '[packages.numpy]\ndist-policy = "wheel-only"\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_packages_identical_across_files_ok(self, tmp_path: Path) -> None:
        # One file's table is the whole value, so the two entries never
        # land in the same overlap check.
        _project(tmp_path, '[tool.nab.packages.lxml]\ndist-policy = "sdist-only"\n')
        _write(
            tmp_path / "nab.toml",
            '[packages.lxml]\ndist-policy = "sdist-only"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert [o.name for o in eff["packages"].value] == ["lxml"]
        assert eff["packages"].origin.kind is SourceKind.PROJECT_TOML

    def test_packages_disjoint_ranges_in_one_file_ok(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.packages."lxml <= 2"]\ndist-policy = "sdist-only"\n'
            '[tool.nab.packages."lxml > 2"]\ndist-policy = "wheel-only"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert [o.name for o in eff["packages"].value] == ["lxml", "lxml"]

    def test_packages_index_route_no_declared_check(self, tmp_path: Path) -> None:
        # The route-names-a-declared-index cross-key check stays on the
        # resolve path, so the registry accepts a route to an index it
        # cannot see from the per-row hook.
        _project(tmp_path, '[tool.nab.packages.foo]\nindex = "internal"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        (override,) = eff["packages"].value
        assert override.index == "internal"

    def test_package_rules_from_pyproject(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            "[[tool.nab.package-rules]]\n"
            'match = ["lxml", "xmlsec"]\n'
            'dist-policy = "sdist-only"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert sorted(o.name for o in eff["package-rules"].value) == ["lxml", "xmlsec"]
        assert eff["package-rules"].origin.kind is SourceKind.PYPROJECT

    def test_package_rules_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            '[[package-rules]]\nmatch = ["lxml"]\ndist-policy = "sdist-only"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert [o.name for o in eff["package-rules"].value] == ["lxml"]
        assert eff["package-rules"].origin.kind is SourceKind.PROJECT_TOML

    def test_package_rules_in_system_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        sys_toml = _write(
            tmp_path / "etc" / "nab.toml",
            '[[package-rules]]\nmatch = ["lxml"]\ndist-policy = "sdist-only"\n',
        )
        roots = SourceRoots(system_toml=sys_toml, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_package_rules_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(
                SourceRoots(project_dir=tmp_path),
                environ={"NAB_PACKAGE_RULES": "x"},
            )
        assert "NAB_PACKAGE_RULES" in caplog.text
        assert "not env-settable" in caplog.text

    def test_package_rules_bad_shape_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'package-rules = ["x"]\n')
        with pytest.raises(SourceConfigError, match=r"package-rules\[0\] must be"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_package_rules_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[[tool.nab.package-rules]]\nmatch = ["a"]\ndist-policy = "sdist-only"\n',
        )
        _write(
            tmp_path / "nab.toml",
            '[[package-rules]]\nmatch = ["b"]\ndist-policy = "wheel-only"\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_package_rules_intra_file_overlap_keeps_message(
        self, tmp_path: Path
    ) -> None:
        _project(
            tmp_path,
            '[[tool.nab.package-rules]]\nmatch = ["a"]\ndist-policy = "sdist-only"\n'
            '[[tool.nab.package-rules]]\nmatch = ["a"]\ndist-policy = "wheel-only"\n',
        )
        with pytest.raises(SourceConfigError, match="overlapping versions"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_index_from_pyproject(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[[tool.nab.indexes]]\nname = "internal"\nurl = "https://i/simple/"\n'
            '[tool.nab.index.internal]\ndist-policy = "wheel-only"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert set(eff["index"].value) == {"internal"}
        assert eff["index"].origin.kind is SourceKind.PYPROJECT

    def test_index_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            '[index.internal]\ndist-policy = "wheel-only"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert set(eff["index"].value) == {"internal"}
        assert eff["index"].origin.kind is SourceKind.PROJECT_TOML

    def test_index_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(
            tmp_path / "usr" / "nab.toml",
            '[index.pypi]\ndist-policy = "wheel-only"\n',
        )
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_index_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(SourceRoots(project_dir=tmp_path), environ={"NAB_INDEX": "x"})
        assert "NAB_INDEX" in caplog.text
        assert "not env-settable" in caplog.text

    def test_index_bad_shape_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'index = "pypi"\n')
        with pytest.raises(SourceConfigError, match="must be a table keyed by index"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_index_no_declared_check(self, tmp_path: Path) -> None:
        # The key-names-a-declared-index cross-key check stays on the
        # resolve path; the registry accepts an undeclared name here.
        _project(tmp_path, '[tool.nab.index.notdeclared]\ndist-policy = "wheel-only"\n')
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert set(eff["index"].value) == {"notdeclared"}

    def test_index_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.index.pypi]\ndist-policy = "wheel-only"\n')
        _write(
            tmp_path / "nab.toml",
            '[index.pypi]\ndist-policy = "sdist-only"\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_index_identical_across_files_ok(self, tmp_path: Path) -> None:
        _project(tmp_path, '[tool.nab.index.pypi]\ndist-policy = "wheel-only"\n')
        _write(
            tmp_path / "nab.toml",
            '[index.pypi]\ndist-policy = "wheel-only"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert set(eff["index"].value) == {"pypi"}

    def test_index_disjoint_names_conflict(self, tmp_path: Path) -> None:
        # The table is one value, so each project file naming a different
        # index is a conflict.
        _project(tmp_path, '[tool.nab.index.pypi]\ndist-policy = "wheel-only"\n')
        _write(
            tmp_path / "nab.toml",
            '[index.internal]\ndist-policy = "sdist-only"\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_index_assume_fresh_seconds_is_per_index(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml",
            "[index.pypi]\nassume-fresh-seconds = 3600\n"
            "[index.internal]\nassume-fresh-seconds = 30\n",
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        overrides = eff["index"].value
        assert overrides["pypi"].assume_fresh_seconds == 3600
        assert overrides["internal"].assume_fresh_seconds == 30

    def test_index_identical_duration_across_files_ok(self, tmp_path: Path) -> None:
        # An identical relative P<n>D in an index override body across both
        # project files must not read as conflicting: the inspector pins one
        # ``now`` so both layers anchor the duration identically.
        _project(
            tmp_path,
            '[tool.nab.index.pypi]\nuploaded-prior-to = "P4D"\n',
        )
        _write(
            tmp_path / "nab.toml",
            '[index.pypi]\nuploaded-prior-to = "P4D"\n',
        )
        with inspector_anchor():
            eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert set(eff["index"].value) == {"pypi"}

    def test_packages_identical_duration_across_files_ok(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.packages.lxml]\nuploaded-prior-to = "P4D"\n',
        )
        _write(
            tmp_path / "nab.toml",
            '[packages.lxml]\nuploaded-prior-to = "P4D"\n',
        )
        with inspector_anchor():
            eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert {str(o.requirement) for o in eff["packages"].value} == {"lxml"}

    def test_override_defaults_empty(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["packages"].value == ()
        assert eff["package-rules"].value == ()
        assert eff["index"].value == {}

    def test_override_default_render(self) -> None:
        packages = next(s for s in OPTIONS if s.key == "packages")
        index = next(s for s in OPTIONS if s.key == "index")
        assert packages.render(()) == "<none>"
        assert index.render({}) == "<none>"

    def test_packages_render_lists_requirements(self, tmp_path: Path) -> None:
        _project(
            tmp_path, '[tool.nab.packages."lxml <= 2"]\ndist-policy = "sdist-only"\n'
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_get(eff, "packages")
        assert "lxml" in out

    def test_index_render_lists_names(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.index.a]\ndist-policy = "wheel-only"\n'
            '[tool.nab.index.b]\ndist-policy = "sdist-only"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert render_get(eff, "index").strip() == "a, b"

    def test_override_rows_shape(self) -> None:
        for key in ("packages", "package-rules", "index"):
            spec = next(s for s in OPTIONS if s.key == key)
            assert spec.cli_flag is None
            assert spec.cli_param is None

    def test_override_keys_visible_in_config(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            '[tool.nab.packages.lxml]\ndist-policy = "sdist-only"\n'
            "[[tool.nab.package-rules]]\n"
            'match = ["numpy"]\ndist-policy = "wheel-only"\n'
            '[tool.nab.index.pypi]\ndist-policy = "wheel-only"\n',
        )
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_list(eff)
        for key in ("packages", "package-rules", "index"):
            assert key in out


_CONFLICTS_PYPROJECT = 'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
_MATRIX_PYPROJECT = (
    '[tool.nab.matrix]\npython = ">=3.11,<3.12"\nplatforms = ["linux_x86_64"]\n'
)
_MATRIX_PROJECT_TOML = (
    '[matrix]\npython = ">=3.11,<3.12"\nplatforms = ["linux_x86_64"]\n'
)


class TestCrossFieldProjectOptions:
    """The cross-field PROJECT keys: conflicts, matrix.

    Each reuses the single-environment parser, is gated PROJECT-only and
    file-only, and is visible in ``nab config``.  ``conflicts`` is an array
    that concatenates additively across the two project files and re-runs
    its member-uniqueness check over the merged whole; ``matrix`` is a
    nested table that is scalar last-wins.  The cross-field rules they take
    part in (mode/matrix, default-groups-vs-conflicts,
    marker-environment-under-universal, the build-policy host-build gate)
    run as whole-config transforms over the merged config, not registry rows.
    """

    def test_conflicts_from_pyproject(self, tmp_path: Path) -> None:
        _project(tmp_path, _CONFLICTS_PYPROJECT)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        value = eff["conflicts"].value
        assert value == (
            ConflictSet(
                members=(
                    ConflictMember(ConflictKind.EXTRA, "cpu"),
                    ConflictMember(ConflictKind.EXTRA, "gpu"),
                ),
                policy=ConflictPolicy.AT_MOST_ONE,
            ),
        )
        assert eff["conflicts"].origin.kind is SourceKind.PYPROJECT

    def test_conflicts_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(tmp_path / "nab.toml", _CONFLICTS_PYPROJECT)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["conflicts"].origin.kind is SourceKind.PROJECT_TOML
        assert len(eff["conflicts"].value) == 1

    def test_conflicts_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", _CONFLICTS_PYPROJECT)
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_conflicts_in_system_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        sys_toml = _write(tmp_path / "etc" / "nab.toml", _CONFLICTS_PYPROJECT)
        roots = SourceRoots(system_toml=sys_toml, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_conflicts_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(SourceRoots(project_dir=tmp_path), environ={"NAB_CONFLICTS": "x"})
        assert "NAB_CONFLICTS" in caplog.text
        assert "not env-settable" in caplog.text

    def test_conflicts_bad_shape_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, 'conflicts = "cpu"\n')
        with pytest.raises(
            SourceConfigError, match="must be an array of conflict sets"
        ):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_conflicts_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(tmp_path, _CONFLICTS_PYPROJECT)
        _write(
            tmp_path / "nab.toml",
            'conflicts = [[{ group = "test" }, { group = "docs" }]]\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_conflicts_per_file_duplicate_member(self, tmp_path: Path) -> None:
        _project(
            tmp_path,
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }],'
            ' [{ extra = "cpu" }, { extra = "tpu" }]]\n',
        )
        with pytest.raises(SourceConfigError, match="in more than one set"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_conflicts_default_render(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["conflicts"].value == ()
        assert eff["conflicts"].spec.render(()) == "<none>"

    def test_conflicts_render_lists_sets(self, tmp_path: Path) -> None:
        _project(tmp_path, _CONFLICTS_PYPROJECT)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_get(eff, "conflicts")
        assert "at-most-one" in out
        assert "cpu" in out

    def test_matrix_from_pyproject(self, tmp_path: Path) -> None:
        _project(tmp_path, _MATRIX_PYPROJECT)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        value = eff["matrix"].value
        assert value == MatrixConfig(
            python=">=3.11,<3.12", platforms=(PlatformSpec("linux_x86_64"),)
        )
        assert eff["matrix"].origin.kind is SourceKind.PYPROJECT

    def test_matrix_from_project_nab_toml(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(tmp_path / "nab.toml", _MATRIX_PROJECT_TOML)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["matrix"].value == MatrixConfig(
            python=">=3.11,<3.12", platforms=(PlatformSpec("linux_x86_64"),)
        )
        assert eff["matrix"].origin.kind is SourceKind.PROJECT_TOML

    def test_matrix_in_user_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        user = _write(tmp_path / "usr" / "nab.toml", _MATRIX_PROJECT_TOML)
        roots = SourceRoots(user_toml=user, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_matrix_in_system_nab_toml_errors(self, tmp_path: Path) -> None:
        _project(tmp_path)
        sys_toml = _write(tmp_path / "etc" / "nab.toml", _MATRIX_PROJECT_TOML)
        roots = SourceRoots(system_toml=sys_toml, project_dir=tmp_path)
        with pytest.raises(SourceConfigError, match="project-scope option"):
            _resolve(roots)

    def test_nab_matrix_env_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _project(tmp_path)
        with caplog.at_level(logging.WARNING, logger="nab_python"):
            _resolve(SourceRoots(project_dir=tmp_path), environ={"NAB_MATRIX": "x"})
        assert "NAB_MATRIX" in caplog.text
        assert "not env-settable" in caplog.text

    def test_matrix_bad_shape_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path, "matrix = 3\n")
        with pytest.raises(
            SourceConfigError, match=r"\[tool.nab.matrix\] must be a table"
        ):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_matrix_python_order_bad_type_keeps_message(self, tmp_path: Path) -> None:
        _project(tmp_path)
        _write(
            tmp_path / "nab.toml", _MATRIX_PROJECT_TOML + 'python-order = ["desc"]\n'
        )
        with pytest.raises(
            SourceConfigError, match="matrix.python-order must be a string, got list"
        ):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_matrix_cross_file_conflict(self, tmp_path: Path) -> None:
        _project(tmp_path, _MATRIX_PYPROJECT)
        _write(
            tmp_path / "nab.toml",
            '[matrix]\npython = ">=3.12,<3.13"\nplatforms = ["linux_x86_64"]\n',
        )
        with pytest.raises(SourceConfigError, match="conflicting values"):
            _resolve(SourceRoots(project_dir=tmp_path))

    def test_matrix_identical_across_files_ok(self, tmp_path: Path) -> None:
        _project(tmp_path, _MATRIX_PYPROJECT)
        _write(tmp_path / "nab.toml", _MATRIX_PROJECT_TOML)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["matrix"].value == MatrixConfig(
            python=">=3.11,<3.12", platforms=(PlatformSpec("linux_x86_64"),)
        )
        assert eff["matrix"].origin.kind is SourceKind.PROJECT_TOML

    def test_matrix_default_render(self, tmp_path: Path) -> None:
        _project(tmp_path)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        assert eff["matrix"].value is None
        assert eff["matrix"].spec.render(None) == "<none>"

    def test_matrix_render_axes(self) -> None:
        spec = next(s for s in OPTIONS if s.key == "matrix")
        rendered = spec.render(
            MatrixConfig(
                python=">=3.11",
                platforms=(PlatformSpec("linux_x86_64"), PlatformSpec("macos_arm64")),
            )
        )
        assert rendered == "python=>=3.11, platforms=['linux_x86_64', 'macos_arm64']"

    def test_cross_field_rows_shape(self) -> None:
        for key in ("conflicts", "matrix"):
            spec = next(s for s in OPTIONS if s.key == key)
            assert spec.cli_flag is None
            assert spec.cli_param is None

    def test_cross_field_keys_visible_in_config(self, tmp_path: Path) -> None:
        _project(tmp_path, _CONFLICTS_PYPROJECT + _MATRIX_PYPROJECT)
        eff = _resolve(SourceRoots(project_dir=tmp_path))
        out = render_list(eff)
        for key in ("conflicts", "matrix"):
            assert key in out
        explained = render_explain(eff, "matrix")
        assert "linux_x86_64" in explained
