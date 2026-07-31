"""Tests for the resolve_pyproject orchestration function."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from nab_index.client import WheelFile
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.transport import HttpError
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.markers import Marker, default_environment
from nab_python._vendor.packaging.pylock import Pylock
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.version import Version
from nab_python.config import (
    ConfigError,
    ConflictSelectionError,
    MatrixConfig,
    NabProjectConfig,
    ResolveMode,
    read_pyproject_config,
)
from nab_python.lockfile import LockInput, PinShape, build_pylock
from nab_python.provider import (
    BuildPolicy,
    LocalSource,
    Provider,
    ResolutionStrategy,
    UnsupportedVcsError,
)
from nab_python.requirements_file import (
    read_pyproject_groups,
    read_pyproject_name,
    read_pyproject_optional_dependencies,
)
from nab_python.resolve import (
    ResolveResult,
    _augment_resolution_error,
    _build_resolver_inputs,
    _check_group_disjointness,
    _extra_requirements,
    _find_group_conflicts,
    _group_requirements,
    _group_requirements_by_group,
    _ProjectTables,
    _raise_for_source_python,
    _ResolveObserver,
    _walk_no_versions_packages,
    build_lock_input,
    resolve_for_targets,
)
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget
from nab_resolver.ranges import Range
from nab_resolver.resolver import (
    Incompatibility,
    IncompatibilityCause,
    ResolutionError,
    Term,
)

V = Version

# A sentinel transport. resolve_pyproject just hands it to FetchCoordinator,
# which is mocked in these tests, so the value never gets used.
_FAKE_TRANSPORT = MagicMock(name="FakeTransport")

_FORTY = "0123456789abcdef0123456789abcdef01234567"


def _resolved(path: Path, transport: object = None, **kwargs: object) -> ResolveResult:
    """Resolve and surface a failed target's error.

    The engine records a target that did not resolve rather than raising,
    so a caller resolving for one environment re-raises it; that is what
    every test below asserts against.
    """
    result = resolve_for_targets(path, transport, **kwargs)  # type: ignore[arg-type]
    result.raise_for_failure()
    return result


def _target(environment: dict[str, str]) -> ResolveTarget:
    """A host target whose marker environment is ``environment``.

    The group-disjointness check reads only ``marker_env``, so an overlay
    onto the host names the environment a group's markers evaluate under.
    """
    return ResolveTarget.for_host().with_marker_overrides(environment)


def _pins(result: ResolveResult) -> dict[str, Version]:
    """The pins of a single-environment resolve."""
    return result.target_results[0].pins


def _locked(lock_input: LockInput) -> dict[str, PinShape]:
    """The pins a single-environment lock carries."""
    (lock,) = lock_input.targets.values()
    return dict(lock.pins)


def _tables(path: Path) -> _ProjectTables:
    """The pyproject tables the requirement loaders read."""
    return _ProjectTables(
        dependencies=[],
        groups=read_pyproject_groups(path),
        optional=read_pyproject_optional_dependencies(path),
        project_name=read_pyproject_name(path),
    )


def _build_constraints(
    config: NabProjectConfig, *, environment: dict[str, str]
) -> dict[str, Range]:
    """The resolver-input ranges ``config``'s constraints fold to.

    The parser is shared with the requirement side; ``kind`` is what
    tells the two apart.
    """
    ranges, _ = _build_resolver_inputs(
        [Requirement(text) for text in config.constraints],
        config,
        environment=environment,
        kind="constraint",
    )
    return ranges


class TestSpecificModeConflictValidation:
    """Conflict handling in specific mode: direct co-selection forks, an
    umbrella that reaches two members without selecting either fails fast."""

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_co_selecting_conflicting_extras_forks(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """Two directly co-selected conflicting extras fork, matrix or not."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'cpu = ["numpy==2.1.2"]\n'
            'gpu = ["numpy==2.0.0"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
        )
        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            extras=("cpu", "gpu"),
            python_version="3.12.0",
        )
        forks = mock_engine.call_args.kwargs["forks"]
        assert {f.selection for f in forks} == {
            (("extra", "cpu"),),
            (("extra", "gpu"),),
        }
        by_selection = {f.selection: f.requirements for f in forks}
        assert [str(r) for r in by_selection[(("extra", "cpu"),)]] == ["numpy==2.1.2"]
        assert [str(r) for r in by_selection[(("extra", "gpu"),)]] == ["numpy==2.0.0"]

    def test_single_extra_under_conflict_resolves(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
        )
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("1.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            result = _resolved(
                pyproject, _FAKE_TRANSPORT, extras=("cpu",), python_version="3.12.0"
            )
        # The selected extra's pin reached the Provider as a root
        # requirement; the unselected extra's contradictory pin did not.
        root_reqs = mock_provider_cls.call_args.kwargs["root_requirements"]
        assert "foo" in root_reqs
        assert V("1.0") in root_reqs["foo"]
        assert V("2.0") not in root_reqs["foo"]
        assert _pins(result) == {"foo": V("1.0")}

    def test_direct_co_selection_forks_and_locks(self, tmp_path: Path) -> None:
        """A real specific-mode co-selection resolves to two forks, each
        pinning its own member's version under a distinct selection."""
        coordinator = make_coordinator(
            listings={"numpy": _index_wheels("numpy", "2.0.0", "2.1.2")},
            auto_metadata=True,
        )
        path = tmp_path / "pyproject.toml"
        path.write_text(
            '[project]\nname = "proj"\nversion = "0"\ndependencies = []\n'
            "[project.optional-dependencies]\n"
            'cpu = ["numpy==2.1.2"]\n'
            'gpu = ["numpy==2.0.0"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n',
            encoding="utf-8",
        )
        with patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(
                path, _FAKE_TRANSPORT, extras=("cpu", "gpu"), python_version="3.12.0"
            )
        label_for = {v: label for v, label in result.merged_pins()["numpy"]}
        assert set(label_for) == {"2.0.0", "2.1.2"}
        assert label_for["2.1.2"].endswith("-extra-cpu")
        assert label_for["2.0.0"].endswith("-extra-gpu")
        # Both forks land in the lock under their own selection.
        lock_input = build_lock_input(
            result, config=read_pyproject_config(path), extras=("cpu", "gpu")
        )
        assert len(lock_input.targets) == 2

    def test_unknown_conflict_member_raises(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ extra = "cpu" }, { extra = "gpuu" }]]\n'
        )
        # No FetchCoordinator patch: the existence check raises before
        # any network work is attempted.
        with pytest.raises(ConfigError, match="gpuu"):
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                extras=("cpu",),
                python_version="3.12.0",
            )

    def test_unknown_group_member_raises(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[dependency-groups]\n"
            'a = ["foo==1.0"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ group = "a" }, { group = "missing" }]]\n'
        )
        with pytest.raises(ConfigError, match="missing"):
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                groups=("a",),
                python_version="3.12.0",
            )

    def test_umbrella_extra_co_selecting_conflict_raises(self, tmp_path: Path) -> None:
        """An umbrella extra self-referencing both members fails fast."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'cpu = ["numpy==2.1.2"]\n'
            'gpu = ["numpy==2.0.0"]\n'
            'all = ["x[cpu]", "x[gpu]"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
        )
        with pytest.raises(ConflictSelectionError, match="cannot be selected together"):
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                extras=("all",),
                python_version="3.12.0",
            )

    def test_umbrella_group_co_selecting_conflict_raises(self, tmp_path: Path) -> None:
        """A group whose include-group reaches both members fails fast."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[dependency-groups]\n"
            'b22 = ["black==22.0"]\n'
            'b23 = ["black==23.0"]\n'
            'all-tools = [{ include-group = "b22" },'
            ' { include-group = "b23" }]\n'
            "[tool.nab]\n"
            'conflicts = [[{ group = "b22" }, { group = "b23" }]]\n'
        )
        with pytest.raises(ConflictSelectionError, match="cannot be selected together"):
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                groups=("all-tools",),
                python_version="3.12.0",
            )

    def test_umbrella_extra_transitively_co_selecting_conflict_raises(
        self, tmp_path: Path
    ) -> None:
        """A two-hop umbrella reaches both members through an intermediate
        extra.  ``expand_self_extras`` walks the closure, so the
        co-selection check must trip even though ``all`` does not
        directly name cpu or gpu."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'cpu = ["numpy==2.1.2"]\n'
            'gpu = ["numpy==2.0.0"]\n'
            'middle = ["x[cpu]", "x[gpu]"]\n'
            'all = ["x[middle]"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
        )
        with pytest.raises(ConflictSelectionError, match="cannot be selected together"):
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                extras=("all",),
                python_version="3.12.0",
            )

    def test_umbrella_group_transitively_co_selecting_conflict_raises(
        self, tmp_path: Path
    ) -> None:
        """A two-hop ``include-group`` chain reaches both members.
        ``expand_group_includes`` must follow the chain so the
        co-selection check fires for the umbrella alone."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[dependency-groups]\n"
            'b22 = ["black==22.0"]\n'
            'b23 = ["black==23.0"]\n'
            'middle = [{ include-group = "b22" },'
            ' { include-group = "b23" }]\n'
            'all-tools = [{ include-group = "middle" }]\n'
            "[tool.nab]\n"
            'conflicts = [[{ group = "b22" }, { group = "b23" }]]\n'
        )
        with pytest.raises(ConflictSelectionError, match="cannot be selected together"):
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                groups=("all-tools",),
                python_version="3.12.0",
            )

    def test_umbrella_extra_reaching_one_member_resolves(self, tmp_path: Path) -> None:
        """An umbrella reaching only one member stays satisfiable."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            'accel = ["x[cpu]"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
        )
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("1.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            result = _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                extras=("accel",),
                python_version="3.12.0",
            )
        # Reaching here means the conflict check did not raise; cpu's dep
        # is pulled in through the self-reference.
        assert _pins(result)["foo"] == V("1.0")

    def test_umbrella_extra_disjoint_markers_resolve(self, tmp_path: Path) -> None:
        """Self-refs to both members under disjoint markers do not co-select.

        ``all`` reaches cpu only below 3.10 and gpu only at 3.10+, so on
        Python 3.12 just gpu is active and there is no co-selection. The
        exclusion check must evaluate the self-reference markers in the
        target environment rather than walking every self-reference.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "all = [\n"
            "    \"x[cpu]; python_version < '3.10'\",\n"
            "    \"x[gpu]; python_version >= '3.10'\",\n"
            "]\n"
            "[tool.nab]\n"
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
        )
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("2.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            result = _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                extras=("all",),
                python_version="3.12.0",
            )
        # Only gpu's self-reference marker holds on 3.12, so gpu's pin is
        # the one that reaches the resolver and cpu's is excluded.
        root_reqs = mock_provider_cls.call_args.kwargs["root_requirements"]
        assert V("2.0") in root_reqs["foo"]
        assert V("1.0") not in root_reqs["foo"]
        assert _pins(result) == {"foo": V("2.0")}

    def test_specific_mode_exactly_one_with_no_member_raises(
        self, tmp_path: Path
    ) -> None:
        # ``exactly_one`` requires at least one member active; with no
        # ``--extras`` flag the resolve fails fast before any network work.
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            "cpu = []\n"
            "gpu = []\n"
            "[tool.nab]\n"
            "conflicts = ["
            '{ members = [{ extra = "cpu" }, { extra = "gpu" }], policy = "exactly-one" }'
            "]\n"
        )
        with pytest.raises(ConflictSelectionError, match="exactly one"):
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

    def test_specific_mode_at_least_one_with_no_member_raises(
        self, tmp_path: Path
    ) -> None:
        # ``at_least_one`` requires at least one member active; with no
        # ``--extras`` flag the resolve fails fast before any network work.
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            "cpu = []\n"
            "gpu = []\n"
            "[tool.nab]\n"
            "conflicts = ["
            '{ members = [{ extra = "cpu" }, { extra = "gpu" }], policy = "at-least-one" }'
            "]\n"
        )
        with pytest.raises(ConflictSelectionError, match="at least one"):
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

    def test_specific_mode_exactly_one_with_one_member_resolves(
        self, tmp_path: Path
    ) -> None:
        # The happy path for exactly_one: one member selected, resolve runs.
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "[tool.nab]\n"
            "conflicts = ["
            '{ members = [{ extra = "cpu" }, { extra = "gpu" }], policy = "exactly-one" }'
            "]\n"
        )
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("1.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            result = _resolved(
                pyproject, _FAKE_TRANSPORT, extras=("cpu",), python_version="3.12.0"
            )
        assert _pins(result) == {"foo": V("1.0")}

    def test_default_groups_satisfy_exactly_one(self, tmp_path: Path) -> None:
        # ``default-groups`` activates ``a`` on every default install, so
        # the exactly-one minimum is met without any ``--groups`` flag.
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[dependency-groups]\n"
            'a = ["foo==1.0"]\n'
            "b = []\n"
            "[tool.nab]\n"
            'default-groups = ["a"]\n'
            "conflicts = ["
            '{ members = [{ group = "a" }, { group = "b" }], policy = "exactly-one" }'
            "]\n"
        )
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("1.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            result = _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")
        assert _pins(result) == {"foo": V("1.0")}

    def test_default_groups_deps_are_loaded(self, tmp_path: Path) -> None:
        # ``default-groups`` deps reach the resolver even without ``--groups``.
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[dependency-groups]\n"
            'dev = ["bar==2.0"]\n'
            "[tool.nab]\n"
            'default-groups = ["dev"]\n'
        )
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("2.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")
        root_reqs = mock_provider_cls.call_args.kwargs["root_requirements"]
        assert "bar" in root_reqs

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_default_groups_plus_cli_fork_exclusion(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        # ``default-groups = ["a"]`` plus ``--groups b`` directly selects
        # both members of an at-most-one set, so the resolve forks.
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            "dependencies = []\n"
            "[dependency-groups]\n"
            "a = []\n"
            "b = []\n"
            "[tool.nab]\n"
            'default-groups = ["a"]\n'
            'conflicts = [[{ group = "a" }, { group = "b" }]]\n'
        )
        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            groups=("b",),
            python_version="3.12.0",
        )
        forks = mock_engine.call_args.kwargs["forks"]
        assert {f.selection for f in forks} == {
            (("group", "a"),),
            (("group", "b"),),
        }


class TestResolvePyproject:
    def test_resolves_simple_project(self, tmp_path: Path) -> None:
        """End-to-end with mocked PyPI: resolve a single dependency."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo>=1.0"]\n',
        )

        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("2.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1

            result = _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        assert _pins(result) == {"foo": V("2.0")}

    def test_uses_current_python_version(self, tmp_path: Path) -> None:
        """When python_version is None, uses sys.version_info."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["bar"]\n',
        )

        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("1.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1

            _resolved(pyproject, _FAKE_TRANSPORT)

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target.host_faithful
        assert target.marker_env == default_environment()

    def test_indexes_from_config_passed_to_coordinator(self, tmp_path: Path) -> None:
        """[tool.nab.indexes] reaches FetchCoordinator as the indexes list."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["baz"]\n'
            "[[tool.nab.indexes]]\n"
            'name = "private"\n'
            'url = "https://custom.index/simple/"\n',
        )

        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("1.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1

            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
            )

        call_kwargs = mock_coord_cls.call_args
        assert "indexes" in call_kwargs.kwargs
        forwarded = call_kwargs.kwargs["indexes"]
        assert len(forwarded) == 1
        assert forwarded[0].name == "private"
        assert forwarded[0].url == "https://custom.index/simple/"

    def test_routing_override_passed_as_index_route(self, tmp_path: Path) -> None:
        """A per-package routing override reaches the coordinator as a route."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["baz"]\n'
            "[[tool.nab.indexes]]\n"
            'name = "pypi"\n'
            'url = "https://pypi.org/simple/"\n'
            "[[tool.nab.indexes]]\n"
            'name = "private"\n'
            'url = "https://private.example.com/simple/"\n'
            '[tool.nab.packages.baz]\nindex = "private"\n',
        )

        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("1.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1

            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        routes = mock_coord_cls.call_args.kwargs["index_routes"]
        assert [(r.name, r.index) for r in routes] == [("baz", "private")]

    def test_arbitrary_equality_dep_passed_through(self, tmp_path: Path) -> None:
        """``===`` deps reach the resolver as literal-only ranges.

        Unlike PEP 440 specifiers, ``===`` cannot match any
        ``Version``, so a real resolve would fail to find candidates.
        The mocked ``Resolver.resolve`` here returns a canned answer,
        so we just check that the requirement is forwarded rather than
        silently dropped.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo===custom", "bar>=1.0"]\n',
        )

        with (
            patch("nab_python.resolve.Resolver") as mock_resolver_cls,
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider"),
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_resolver = mock_resolver_cls.return_value
            mock_resolver.resolve.return_value = {
                "foo": V("1.0"),
                "bar": V("1.0"),
            }
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        forwarded = mock_resolver.resolve.call_args.args[0]
        assert "foo" in forwarded
        assert "bar" in forwarded
        assert "custom" in forwarded["foo"]
        assert V("1.0") not in forwarded["foo"]

    def test_empty_dependencies(self, tmp_path: Path) -> None:
        """Project with no dependencies resolves to empty dict."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\ndependencies = []\n")

        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider"),
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        assert _pins(result) == {}

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_passes_constraints_to_resolver(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Constraints from [tool.nab] are passed to resolver.resolve()."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo>=1.0"]\n'
            '[tool.nab]\nconstraints = ["bar<2.0", "skip===custom"]\n',
        )

        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver = mock_resolver_cls.return_value
        mock_resolver.resolve.return_value = {"foo": V("2.0")}

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        call_kwargs = mock_resolver.resolve.call_args
        assert "constraints" in call_kwargs.kwargs
        constraints = call_kwargs.kwargs["constraints"]
        assert "bar" in constraints
        assert "skip" in constraints
        assert "custom" in constraints["skip"]
        assert V("1.0") not in constraints["skip"]

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_extras_create_proxy_packages(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Requirements with extras create proxy package entries."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["requests[security]>=2.0"]\n',
        )

        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver = mock_resolver_cls.return_value
        mock_resolver.resolve.return_value = {"requests": V("2.0")}

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        call_args = mock_resolver.resolve.call_args
        requirements = call_args.args[0]
        assert "requests" in requirements
        assert "requests[security]" in requirements
        # root_extras passed to provider
        provider_kwargs = mock_provider_cls.call_args.kwargs
        assert ("requests", "security") in provider_kwargs["root_extras"]

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_root_marker_evaluates_against_environment(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A root dep whose marker evaluates False is dropped from the resolve."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\n"
            'dependencies = ["foo", "windows-only; sys_platform == \'win32\'"]\n'
            '[tool.nab]\nbuild-policy = "never"\n'
            "[tool.nab.environment]\n"
            'platform = "linux_x86_64"\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {"foo": V("1.0")}

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        requirements = mock_resolver_cls.return_value.resolve.call_args.args[0]
        assert "foo" in requirements
        assert "windows-only" not in requirements

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_python_version_overlay_keeps_full_version_gated_dep(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A python_full_version-gated dep follows the declared Python.

        Declaring only ``python = "3.8"`` on a 3.12 host must keep
        ``legacy; python_full_version < '3.10'``: the resolve targets Python
        3.8, so the marker holds. The host patch level must not leak in
        through ``python_full_version``.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\n"
            "dependencies = ["
            '"foo", "legacy; python_full_version < \'3.10\'"]\n'
            '[tool.nab]\nbuild-policy = "never"\n'
            "[tool.nab.environment]\n"
            'python = "3.8"\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {
            "foo": V("1.0"),
            "legacy": V("1.0"),
        }

        _resolved(pyproject, _FAKE_TRANSPORT)

        requirements = mock_resolver_cls.return_value.resolve.call_args.args[0]
        assert "foo" in requirements
        assert "legacy" in requirements

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_root_marker_true_keeps_requirement(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A root dep whose marker evaluates True is kept."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\n"
            'dependencies = ["foo", "linux-only; sys_platform == \'linux\'"]\n'
            '[tool.nab]\nbuild-policy = "never"\n'
            "[tool.nab.environment]\n"
            'platform = "linux_x86_64"\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {
            "foo": V("1.0"),
            "linux-only": V("1.0"),
        }

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        requirements = mock_resolver_cls.return_value.resolve.call_args.args[0]
        assert "linux-only" in requirements

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_root_marker_uses_effective_python(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """``python_version`` in markers reflects the resolve target Python."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo", "newer; python_version >= \'3.12\'"]\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {"foo": V("1.0")}

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.10.0")

        requirements = mock_resolver_cls.return_value.resolve.call_args.args[0]
        assert "newer" not in requirements

    def test_unparseable_python_version_raises(self, tmp_path: Path) -> None:
        """Garbled ``python_version`` arg raises instead of silently
        resolving against the host interpreter.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n',
        )
        with pytest.raises(ConfigError, match="'not-a-version'"):
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="not-a-version")

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_python_version_arg_retargets_the_python_axis(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The ``python_version`` arg (``--python``) sets the target Python.

        ``requires-python`` is only a declaration, so it neither steers the
        target nor conflicts with an override it admits.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab]\n"
            'requires-python = ">=3.10"\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.4")

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target.python_full_version == "3.12.4"

    def test_python_version_arg_outside_requires_python_raises(
        self, tmp_path: Path
    ) -> None:
        """A retarget the declaration excludes fails loud, not silently."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab]\n"
            'requires-python = ">=3.11"\n',
        )
        with pytest.raises(ConfigError, match="excludes the resolve target"):
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.9")

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_explicit_config_arg_skips_file_read(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """An explicit config arg bypasses [tool.nab] parsing on disk."""
        pyproject = tmp_path / "pyproject.toml"
        # File has no [tool.nab] table on disk; the explicit config wins.
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        explicit = NabProjectConfig(constraints=("urllib3<2",))
        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            config=explicit,
            python_version="3.12.0",
        )

        forwarded = mock_resolver_cls.return_value.resolve.call_args.kwargs[
            "constraints"
        ]
        assert "urllib3" in forwarded

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_strategy_from_config_reaches_provider(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """[tool.nab].resolution threads to the provider as the strategy enum."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab]\n"
            'resolution = "lowest-direct"\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        kwargs = mock_provider_cls.call_args.kwargs
        assert kwargs["resolution_strategy"] is ResolutionStrategy.LOWEST_DIRECT
        # The direct set holds the canonical names of the project's own deps.
        assert kwargs["direct_packages"] == frozenset({"foo"})

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_cli_strategy_overrides_config(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """An explicit resolution_strategy arg wins over [tool.nab].resolution."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n[tool.nab]\nresolution = "lowest"\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            python_version="3.12.0",
            resolution_strategy=ResolutionStrategy.HIGHEST,
        )

        kwargs = mock_provider_cls.call_args.kwargs
        assert kwargs["resolution_strategy"] is ResolutionStrategy.HIGHEST

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_default_strategy_is_highest(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Without config or override, the strategy defaults to HIGHEST."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        kwargs = mock_provider_cls.call_args.kwargs
        assert kwargs["resolution_strategy"] is ResolutionStrategy.HIGHEST

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_extras_excluded_from_direct_packages(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """``foo[bar]`` proxy keys do not appear in the direct-packages set."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["requests[security]>=2.0", "foo"]\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        kwargs = mock_provider_cls.call_args.kwargs
        # Only the base canonical names; the extras-proxy key
        # ("requests[security]") must not be in the direct set because
        # the strategy decision is keyed on the underlying package.
        assert kwargs["direct_packages"] == frozenset({"requests", "foo"})

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_default_groups_from_config_not_cli_groups(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """``default_groups`` is the project config value, not ``--groups``."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo>=1.0"]\n'
            "[dependency-groups]\ndev = []\ntest = []\n"
            '[tool.nab]\ndefault-groups = ["dev"]\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        result = _resolved(
            pyproject, _FAKE_TRANSPORT, python_version="3.12.0", groups=("test",)
        )

        lock_input = build_lock_input(
            result,
            config=read_pyproject_config(pyproject),
            dependency_groups=("test",),
        )
        assert lock_input.dependency_groups == ("test",)
        assert lock_input.default_groups == ("dev",)

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_default_groups_empty_when_config_omits_it(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """No ``default-groups`` in config: ``--groups`` does not leak in."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo>=1.0"]\n[dependency-groups]\ntest = []\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        result = _resolved(
            pyproject, _FAKE_TRANSPORT, python_version="3.12.0", groups=("test",)
        )

        lock_input = build_lock_input(
            result,
            config=read_pyproject_config(pyproject),
            dependency_groups=("test",),
        )
        assert lock_input.default_groups == ()


class TestResolveUniversalPyproject:
    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_dispatches_to_universal_resolver(
        self,
        mock_engine: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The wrapper builds a Matrix and forwards to resolve_universal."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64", "macos_arm64"]\n',
        )
        result = _resolved(pyproject)

        assert result is mock_engine.return_value
        targets = mock_engine.call_args.args[1]
        assert [t.label for t in targets] == [
            "py311-linux_x86_64",
            "py311-macos_arm64",
            "py312-linux_x86_64",
            "py312-macos_arm64",
        ]
        # No conflicts: a single unforked fork carrying the base deps.
        (fork,) = mock_engine.call_args.kwargs["forks"]
        assert fork.selection == ()
        assert [str(r) for r in fork.requirements] == ["foo"]

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_passes_python_patches_when_set(
        self,
        mock_engine: MagicMock,
        tmp_path: Path,
    ) -> None:
        """python-patches in the matrix table flow to the Matrix object."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
            'python-patches = { "3.11" = "3.11.4" }\n',
        )
        _resolved(pyproject)
        targets = mock_engine.call_args.args[1]
        assert [t.python_full_version for t in targets] == ["3.11.4", "3.12.0"]

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_explicit_config_arg(
        self,
        mock_engine: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Caller can pass a constructed config rather than reading the file."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')
        config = NabProjectConfig(
            mode=ResolveMode.UNIVERSAL,
            matrix=MatrixConfig(
                python=">=3.12,<3.14", platforms=(PlatformSpec("linux_x86_64"),)
            ),
        )
        _resolved(pyproject, config=config)
        targets = mock_engine.call_args.args[1]
        assert [t.label for t in targets] == [
            "py312-linux_x86_64",
            "py313-linux_x86_64",
        ]

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_co_selected_members_build_multiple_forks(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """Two co-selected conflicting extras fork into two resolves."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch==2.0+cpu"]\n'
            'gpu = ["torch==2.0+gpu"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject, extras=["cpu", "gpu"])
        forks = mock_engine.call_args.kwargs["forks"]
        assert {f.selection for f in forks} == {
            (("extra", "cpu"),),
            (("extra", "gpu"),),
        }
        by_selection = {f.selection: f.requirements for f in forks}
        assert [str(r) for r in by_selection[(("extra", "cpu"),)]] == [
            "base",
            "torch==2.0+cpu",
        ]
        assert [str(r) for r in by_selection[(("extra", "gpu"),)]] == [
            "base",
            "torch==2.0+gpu",
        ]

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_fork_selects_on_its_own_member_too(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """A fork's own member is one of the selections its lock gates on.

        ``shared`` is required by the conflicting ``cpu`` extra and by the
        non-conflicting ``docs`` extra, so the cpu fork has to attribute it
        to both or the lock names only ``docs``.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["shared", "cpu-only"]\n'
            'gpu = ["gpu-only"]\n'
            'docs = ["shared", "sphinx"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject, extras=["cpu", "gpu", "docs"])
        forks = mock_engine.call_args.kwargs["forks"]
        cpu = next(f for f in forks if f.selection == (("extra", "cpu"),))
        assert cpu.contexts is not None
        selectors = {
            member: [str(r) for r in reqs]
            for member, reqs in cpu.contexts.selectors.items()
        }
        assert selectors == {
            ("extra", "cpu"): ["shared", "cpu-only"],
            ("extra", "docs"): ["shared", "sphinx"],
        }

    def test_exactly_one_with_no_member_raises(self, tmp_path: Path) -> None:
        """A universal exactly-one set with no active member raises."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch==2.0+cpu"]\n'
            'gpu = ["torch==2.0+gpu"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [{ members = [{ extra = "cpu" },'
            ' { extra = "gpu" }], policy = "exactly-one" }]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="exactly one"),
        ):
            _resolved(pyproject)
        mock_universal.assert_not_called()

    def test_at_least_one_with_no_member_raises(self, tmp_path: Path) -> None:
        """A universal at-least-one set with no active member raises."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch==2.0+cpu"]\n'
            'gpu = ["torch==2.0+gpu"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [{ members = [{ extra = "cpu" },'
            ' { extra = "gpu" }], policy = "at-least-one" }]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="at least one"),
        ):
            _resolved(pyproject)
        mock_universal.assert_not_called()

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_at_most_one_empty_does_not_raise(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """An at-most-one set with no member selected is fine."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch==2.0+cpu"]\n'
            'gpu = ["torch==2.0+gpu"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject)
        (fork,) = mock_engine.call_args.kwargs["forks"]
        assert fork.selection == ()

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_co_selection_does_not_raise_minimum(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """Co-selecting an exactly-one set forks rather than raising."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch==2.0+cpu"]\n'
            'gpu = ["torch==2.0+gpu"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [{ members = [{ extra = "cpu" },'
            ' { extra = "gpu" }], policy = "exactly-one" }]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject, extras=["cpu", "gpu"])
        forks = mock_engine.call_args.kwargs["forks"]
        assert len(forks) == 2

    def test_marker_gated_member_unreachable_on_a_tuple_raises(
        self, tmp_path: Path
    ) -> None:
        """A require-one member reachable only on a win32-gated tuple
        leaves the non-win32 tuple with no member, so the resolve raises."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "myproj"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            "all = ['myproj[gpu]; sys_platform == \"win32\"']\n"
            'gpu = ["cupy"]\n'
            'cpu = ["numpy"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [{ members = [{ extra = "cpu" },'
            ' { extra = "gpu" }], policy = "exactly-one" }]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64", "windows_amd64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="exactly one"),
        ):
            _resolved(pyproject, extras=["all"])
        mock_universal.assert_not_called()

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_marker_gated_member_reachable_on_every_tuple_passes(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """A self reference whose marker holds on every tuple keeps the
        require-one set satisfied, so the resolve proceeds."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "myproj"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            "all = [\"myproj[gpu]; python_version >= '3.0'\"]\n"
            'gpu = ["cupy"]\n'
            'cpu = ["numpy"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [{ members = [{ extra = "cpu" },'
            ' { extra = "gpu" }], policy = "exactly-one" }]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64", "windows_amd64"]\n'
        )
        _resolved(pyproject, extras=["all"])
        mock_engine.assert_called_once()

    @patch("nab_python.resolve.resolve_with_coordinator")
    @patch("nab_python.resolve._check_group_disjointness")
    def test_repeated_active_groups_check_once(
        self,
        mock_check: MagicMock,
        mock_engine: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Forks sharing the same multi-group active set check disjointness once."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch==2.0+cpu"]\n'
            'gpu = ["torch==2.0+gpu"]\n'
            "[dependency-groups]\n"
            'dev = ["pytest"]\n'
            'lint = ["ruff"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject, extras=["cpu", "gpu"], groups=["dev", "lint"])
        # Two forks, both with active_groups=("dev", "lint"): scan once.
        assert mock_check.call_count == 1

    def test_umbrella_extra_co_selecting_conflict_raises(self, tmp_path: Path) -> None:
        """An umbrella extra forcing both members cannot fork, so it raises."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch==2.0+cpu"]\n'
            'gpu = ["torch==2.0+gpu"]\n'
            'all = ["x[cpu]", "x[gpu]"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(pyproject, extras=["all"])
        mock_universal.assert_not_called()

    def test_umbrella_group_co_selecting_conflict_raises(self, tmp_path: Path) -> None:
        """A group whose includes force both members raises, never forks."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[dependency-groups]\n"
            'b22 = ["black==22.0"]\n'
            'b23 = ["black==23.0"]\n'
            'all-tools = [{ include-group = "b22" },'
            ' { include-group = "b23" }]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ group = "b22" }, { group = "b23" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(pyproject, groups=["all-tools"])
        mock_universal.assert_not_called()

    def test_universal_umbrella_extra_transitively_co_selects_conflict_raises(
        self, tmp_path: Path
    ) -> None:
        """The two-hop transitive case in universal mode.  No fork can
        separate cpu and gpu when both flow from one umbrella, so the
        resolve is refused before any tuple runs."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch==2.0+cpu"]\n'
            'gpu = ["torch==2.0+gpu"]\n'
            'middle = ["x[cpu]", "x[gpu]"]\n'
            'all = ["x[middle]"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(pyproject, extras=["all"])
        mock_universal.assert_not_called()

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_umbrella_extra_disjoint_markers_resolve(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """Self-refs to both members under disjoint markers do not co-select.

        ``all`` reaches cpu only below 3.10 and gpu only at 3.10+, so no
        single tuple activates both. The exclusion check runs per tuple
        against each tuple's environment, so the resolve proceeds.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "all = [\n"
            "    \"x[cpu]; python_version < '3.10'\",\n"
            "    \"x[gpu]; python_version >= '3.10'\",\n"
            "]\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.9,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject, extras=["all"])
        mock_engine.assert_called_once()

    def test_umbrella_extra_co_selecting_on_one_tuple_raises(
        self, tmp_path: Path
    ) -> None:
        """Overlapping self-ref markers co-activate both members on a tuple.

        ``all`` reaches cpu at 3.10+ and gpu at every version, so the 3.11
        tuple activates both. The per-tuple exclusion check must refuse the
        resolve even though the 3.9 tuple reaches only gpu.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "all = [\n"
            "    \"x[cpu]; python_version >= '3.10'\",\n"
            '    "x[gpu]",\n'
            "]\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.9,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(pyproject, extras=["all"])
        mock_universal.assert_not_called()

    def test_umbrella_extra_co_selecting_above_a_micro_boundary_raises(
        self, tmp_path: Path
    ) -> None:
        """A self-ref gated on a micro of the tuple's own minor still refuses.

        ``all`` reaches cpu everywhere and gpu from 3.10.4 up, so every real
        3.10.4+ interpreter activates both. The tuple's synthesized 3.10.0
        answers the gate false, so the check has to split the minor before it
        reads the clause.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "all = [\n"
            '    "x[cpu]",\n'
            "    \"x[gpu]; python_full_version >= '3.10.4'\",\n"
            "]\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.10"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(pyproject, extras=["all"])
        mock_universal.assert_not_called()

    def test_require_one_member_lost_above_a_micro_boundary_raises(
        self, tmp_path: Path
    ) -> None:
        """An exactly-one member reachable only below a micro leaves the
        slice above it with no member, so the resolve is refused."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "all = [\"x[cpu]; python_full_version < '3.10.4'\"]\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [{ members = [{ extra = "cpu" },'
            ' { extra = "gpu" }], policy = "exactly-one" }]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.10"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="exactly one"),
        ):
            _resolved(pyproject, extras=["all"])
        mock_universal.assert_not_called()

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_umbrella_extra_disjoint_micro_markers_resolve(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """Self-refs split at the same micro reach one member per slice.

        cpu below 3.10.4 and gpu from 3.10.4 up never meet on one
        interpreter, so splitting the minor must not turn into a refusal.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "all = [\n"
            "    \"x[cpu]; python_full_version < '3.10.4'\",\n"
            "    \"x[gpu]; python_full_version >= '3.10.4'\",\n"
            "]\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.10"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject, extras=["all"])
        mock_engine.assert_called_once()

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_untileable_self_ref_marker_off_the_matrix_resolves(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """A self-ref no tuple can reach does not have to tile a minor.

        The closure is walked without an environment, so legacy's 3.7-only
        membership marker is scanned on a 3.10 tuple that never reaches it.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "legacy = [\"x[gpu]; python_full_version in '3.7.1 3.7.2'\"]\n"
            "all = [\n"
            '    "x[cpu]",\n'
            "    \"x[legacy]; python_version < '3.8'\",\n"
            "]\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.10"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject, extras=["all"])
        mock_engine.assert_called_once()

    def test_untileable_self_ref_marker_keeps_the_other_boundaries(
        self, tmp_path: Path
    ) -> None:
        """Skipping an untileable marker leaves the cuts the rest make.

        gpu is still reached from 3.10.4 up alongside cpu, so the refusal
        stands with legacy's untileable marker in the same closure.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "legacy = [\"x[gpu]; python_full_version in '3.7.1 3.7.2'\"]\n"
            "all = [\n"
            '    "x[cpu]",\n'
            "    \"x[legacy]; python_version < '3.8'\",\n"
            "    \"x[gpu]; python_full_version >= '3.10.4'\",\n"
            "]\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.10"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(pyproject, extras=["all"])
        mock_universal.assert_not_called()

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_umbrella_extra_reaching_one_member_single_fork(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """An umbrella reaching one member yields one fork with its deps."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch==2.0+cpu"]\n'
            'gpu = ["torch==2.0+gpu"]\n'
            'accel = ["x[cpu]"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject, extras=["accel"])
        (fork,) = mock_engine.call_args.kwargs["forks"]
        assert fork.selection == ()
        rendered = [str(r) for r in fork.requirements]
        assert "torch==2.0+cpu" in rendered
        assert "torch==2.0+gpu" not in rendered

    def test_unknown_conflict_member_raises(self, tmp_path: Path) -> None:
        """A conflict member naming an undeclared extra raises ConfigError."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch==2.0+cpu"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ extra = "cpu" }, { extra = "gpuu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConfigError, match="gpuu"),
        ):
            _resolved(pyproject)
        mock_universal.assert_not_called()

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_default_groups_satisfy_exactly_one(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        # ``default-groups`` activates ``a`` on every default install, so
        # ``nab lock`` without ``--groups`` clears the exactly-one minimum.
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[dependency-groups]\n"
            'a = ["foo==1.0"]\n'
            "b = []\n"
            "[tool.nab]\n"
            'mode = "universal"\n'
            'default-groups = ["a"]\n'
            "conflicts = ["
            '{ members = [{ group = "a" }, { group = "b" }], policy = "exactly-one" }'
            "]\n"
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject)
        (fork,) = mock_engine.call_args.kwargs["forks"]
        assert "foo==1.0" in [str(r) for r in fork.requirements]

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_default_groups_drive_forking_with_cli_groups(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        # ``default-groups = ["b22"]`` plus ``--groups b23`` activates two
        # members of an at-most-one set: each fork carries one member.
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[dependency-groups]\n"
            'b22 = ["black==22.0"]\n'
            'b23 = ["black==23.0"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'default-groups = ["b22"]\n'
            'conflicts = [[{ group = "b22" }, { group = "b23" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(pyproject, groups=["b23"])
        forks = mock_engine.call_args.kwargs["forks"]
        assert len(forks) == 2
        selections = {f.selection for f in forks}
        assert selections == {
            (("group", "b22"),),
            (("group", "b23"),),
        }


class TestResolvePyprojectVcs:
    """VCS direct-URL requirements get admission-checked before drop."""

    def test_block_default_refuses_vcs_dependency(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            f"[project]\ndependencies = "
            f'["foo @ git+https://github.com/foo/bar.git@{_FORTY}"]\n',
        )

        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

    def test_admitted_vcs_dependency_raises_not_implemented(
        self, tmp_path: Path
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            f"[project]\ndependencies = "
            f'["foo @ git+https://github.com/foo/bar.git@{_FORTY}"]\n'
            "[tool.nab.vcs]\n"
            'policy = "allow"\n'
            'allowed-schemes = ["git+https"]\n'
            'allowed-repos = ["https://github.com/"]\n',
        )

        with pytest.raises(NotImplementedError, match="not implemented"):
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
            )

    def test_vcs_constraint_refused_at_config_load(self, tmp_path: Path) -> None:
        """An admitting VCS policy does not matter: config load refuses it first."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\ndependencies = []\n"
            "[tool.nab]\nconstraints = "
            f'["foo @ git+https://github.com/foo/bar.git@{_FORTY}"]\n'
            "[tool.nab.vcs]\n"
            'policy = "allow"\n'
            'allowed-schemes = ["git+https"]\n'
            'allowed-repos = ["https://github.com/"]\n',
        )

        with pytest.raises(ConfigError, match="cannot be a direct reference"):
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")


class TestResolvePyprojectLockShape:
    """Lock-input plumbing: the result always carries a LockInput."""

    def test_returns_resolution_result(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo>=1.0"]\n')

        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_target_lock") as mock_build,
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("2.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            lock_sentinel = MagicMock(name="TargetLock")
            mock_build.return_value = lock_sentinel

            result = _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        assert isinstance(result, ResolveResult)
        assert result.target_results[0].lock is lock_sentinel
        assert "foo" in _pins(result)

    def test_default_indexes_pypi_when_no_indexes(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider"),
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")
        passed = mock_coord_cls.call_args.kwargs["indexes"]
        assert tuple(ix.url for ix in passed) == ("https://pypi.org/simple/",)

    def test_configured_indexes_passed_through(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[[tool.nab.indexes]]\n"
            'name = "private"\n'
            'url = "https://custom.index/simple/"\n',
        )
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider"),
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")
        passed = mock_coord_cls.call_args.kwargs["indexes"]
        assert tuple(ix.url for ix in passed) == ("https://custom.index/simple/",)


class TestSpecificModeTargetPlan:
    """The resolve target: the host, or the environment the project declares."""

    @staticmethod
    def _mock_resolve(coord: MagicMock, resolver: MagicMock) -> None:
        coord.return_value.__enter__ = lambda s: s
        coord.return_value.__exit__ = MagicMock(return_value=False)
        resolver.return_value.resolve.return_value = {}

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_host_is_the_default_target(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A project that declares no environment resolves for the host."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')
        self._mock_resolve(mock_coord_cls, mock_resolver_cls)

        _resolved(pyproject, _FAKE_TRANSPORT)

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target == ResolveTarget.for_host()
        assert target.host_faithful

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_requires_python_does_not_steer_the_target(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """``requires-python`` is a declaration: the host stays the target.

        It is recorded as the lock's ``requires-python`` and nothing else,
        so a project supporting 3.9 and up still resolves for the host.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n[tool.nab]\nrequires-python = ">=3.9"\n'
        )
        self._mock_resolve(mock_coord_cls, mock_resolver_cls)

        result = _resolved(pyproject, _FAKE_TRANSPORT)

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target == ResolveTarget.for_host()
        assert (
            build_lock_input(result, config=read_pyproject_config(pyproject))
        ).requires_python == ">=3.9"

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_environment_python_retargets_the_host(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """``[tool.nab.environment].python`` moves the python axis only."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab.environment]\n"
            'python = "3.10.5"\n'
        )
        self._mock_resolve(mock_coord_cls, mock_resolver_cls)

        _resolved(pyproject, _FAKE_TRANSPORT)

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target.python_full_version == "3.10.5"
        assert target.platform_id == "host"

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_environment_platform_declares_the_target(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A declared platform builds a synthesized (non-host) target."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab.environment]\n"
            'python = "3.11"\n'
            'platform = "windows_amd64"\n'
        )
        self._mock_resolve(mock_coord_cls, mock_resolver_cls)

        _resolved(pyproject, _FAKE_TRANSPORT)

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target.platform_id == "windows_amd64"
        assert target.marker_env["sys_platform"] == "win32"
        assert target.python_version == "3.11"
        assert not target.host_faithful

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_python_override_wins_over_the_environment(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """``--python`` moves the python axis of a declared environment."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab]\n"
            'build-policy = "never"\n'
            "[tool.nab.environment]\n"
            'python = "3.11"\n'
            'platform = "linux_aarch64"\n'
        )
        self._mock_resolve(mock_coord_cls, mock_resolver_cls)

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.4")

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target.python_full_version == "3.12.4"
        assert target.platform_id == "linux_aarch64"


class TestLoadGroupRequirements:
    """``_load_group_requirements`` reads the ``[dependency-groups]``
    table.  Empty selection short-circuits; missing table raises so
    a typo in ``--group`` does not silently expand to nothing."""

    def test_empty_selection_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'x'\n")
        assert _group_requirements(read_pyproject_groups(path), [], path) == []

    def test_missing_table_raises_with_selected_names(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'x'\n")
        with pytest.raises(LookupError, match=r"\[dependency-groups\] is missing"):
            _group_requirements(read_pyproject_groups(path), ["dev"], path)

    def test_returns_requirements_for_selected_groups(self, tmp_path: Path) -> None:
        """Selected groups expand into ``Requirement`` instances."""
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'x'\n[dependency-groups]\ndev = ['pytest>=7']\n"
        )
        reqs = _group_requirements(read_pyproject_groups(path), ["dev"], path)
        assert [str(r) for r in reqs] == ["pytest>=7"]


class TestLoadExtraRequirements:
    """``_load_extra_requirements`` reads
    ``[project.optional-dependencies]``.  Empty selection short-circuits;
    missing table raises so a typo in ``--extra`` cannot silently
    expand to nothing.
    """

    def test_empty_selection_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'x'\n")
        assert _extra_requirements(_tables(path), [], path) == []

    def test_missing_table_raises_with_selected_names(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'x'\n")
        with pytest.raises(
            LookupError,
            match=r"\[project.optional-dependencies\] is",
        ):
            _extra_requirements(_tables(path), ["test"], path)

    def test_returns_requirements_for_selected_extras(self, tmp_path: Path) -> None:
        """Selected extras expand into ``Requirement`` instances, with
        self-references walked transitively to their underlying deps.
        The self-reference itself does not survive as a requirement: the
        project is the root, not an index candidate.
        """
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'x'\n"
            "[project.optional-dependencies]\n"
            "test = ['pytest>=7']\n"
            "all = ['x[test]']\n"
        )
        reqs = _extra_requirements(_tables(path), ["all"], path)
        names = sorted(r.name for r in reqs)
        assert "pytest" in names
        assert "x" not in names

    def test_selected_extra_name_canonicalized(self, tmp_path: Path) -> None:
        """A --extra spelling differing only by case/separator still resolves."""
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'x'\n"
            "[project.optional-dependencies]\n"
            "my-extra = ['requests']\n"
        )
        reqs = _extra_requirements(_tables(path), ["My_Extra"], path)
        assert [r.name for r in reqs] == ["requests"]

    def test_self_ref_marker_rides_onto_the_deps_it_reaches(
        self, tmp_path: Path
    ) -> None:
        """A marker-gated self-ref gates the deps its extra pulls in.

        The gate is carried rather than evaluated here, so the per-target
        parse is what drops the dep on an environment the marker excludes;
        ``_build_resolver_inputs`` below is where that is asserted.
        """
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'x'\n"
            "[project.optional-dependencies]\n"
            "fast = ['some-dep']\n"
            "all = [\"x[fast]; python_version < '3.10'\"]\n"
        )
        (req,) = _extra_requirements(_tables(path), ["all"], path)
        assert req.name == "some-dep"
        assert str(req.marker) == 'python_version < "3.10"'

        excluded, _ = _build_resolver_inputs(
            [req],
            NabProjectConfig(),
            environment={"python_version": "3.12", "python_full_version": "3.12.0"},
        )
        assert "some-dep" not in excluded


class TestBuildResolverInputs:
    """``_build_resolver_inputs`` folds duplicate names by intersection."""

    def test_duplicate_name_intersects(self) -> None:
        """Two requirements for one package combine to their overlap."""
        reqs = [Requirement("foo>=2.0"), Requirement("foo<3.0")]
        resolver_requirements, _ = _build_resolver_inputs(
            reqs, NabProjectConfig(), environment={}
        )
        foo = resolver_requirements["foo"]
        assert V("2.5") in foo
        assert V("1.0") not in foo
        assert V("5.0") not in foo

    def test_conflicting_names_raise(self) -> None:
        """Pinned-but-different requirements for one package raise."""
        reqs = [Requirement("foo==1.0"), Requirement("foo==2.0")]
        with pytest.raises(ResolutionError, match="foo==1.0"):
            _build_resolver_inputs(reqs, NabProjectConfig(), environment={})

    def test_root_extra_marker_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """A root requirement gated on ``extra ==`` is dropped with a warning."""
        reqs = [Requirement('foo ; extra == "test"')]
        with caplog.at_level("WARNING", logger="nab_python.resolve"):
            resolver_requirements, _ = _build_resolver_inputs(
                reqs, NabProjectConfig(), environment={}
            )
        assert "foo" not in resolver_requirements
        assert any("membership marker" in rec.message for rec in caplog.records)

    def test_root_extras_set_marker_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A root ``"x" in extras`` marker is dropped with a warning, not a crash."""
        reqs = [Requirement('foo ; "x" in extras')]
        with caplog.at_level("WARNING", logger="nab_python.resolve"):
            resolver_requirements, _ = _build_resolver_inputs(
                reqs, NabProjectConfig(), environment={}
            )
        assert "foo" not in resolver_requirements
        assert any("membership marker" in rec.message for rec in caplog.records)

    def test_root_dependency_groups_marker_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A root ``in dependency_groups`` marker is dropped with a warning."""
        reqs = [Requirement('foo ; "dev" in dependency_groups')]
        with caplog.at_level("WARNING", logger="nab_python.resolve"):
            resolver_requirements, _ = _build_resolver_inputs(
                reqs, NabProjectConfig(), environment={}
            )
        assert "foo" not in resolver_requirements
        assert any("membership marker" in rec.message for rec in caplog.records)

    def test_extra_marker_without_spaces_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """packaging normalises the spelling, so the scan sees one form."""
        reqs = [Requirement('foo ; extra=="test"')]
        with caplog.at_level("WARNING", logger="nab_python.resolve"):
            _build_resolver_inputs(reqs, NabProjectConfig(), environment={})
        assert any("membership marker" in rec.message for rec in caplog.records)

    def test_extras_of_package_syntax_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``pkg[redis]`` is the syntax the warning points at; it must not warn."""
        reqs = [Requirement("foo[redis]")]
        with caplog.at_level("WARNING", logger="nab_python.resolve"):
            resolver_requirements, _ = _build_resolver_inputs(
                reqs, NabProjectConfig(), environment={}
            )
        assert "foo" in resolver_requirements
        assert not caplog.records

    def test_env_gated_drop_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        """A requirement dropped by a plain env marker stays silent."""
        reqs = [Requirement('foo ; python_version < "3.0"')]
        env = {"python_version": "3.11", "python_full_version": "3.11.2"}
        with caplog.at_level("WARNING", logger="nab_python.resolve"):
            resolver_requirements, _ = _build_resolver_inputs(
                reqs, NabProjectConfig(), environment=env
            )
        assert "foo" not in resolver_requirements
        assert not caplog.records

    def test_multi_extra_proxy_keys_sorted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Extra proxy keys are inserted in sorted order.

        ``Requirement.extras`` is a set with PYTHONHASHSEED-dependent order, and
        the insertion order of the proxy keys becomes the resolver's
        ``root_package_order`` tiebreak. A reversed extras order must still give
        the sorted keys.
        """
        req = Requirement("demo[x,y,z]")
        monkeypatch.setattr(req, "extras", ["z", "y", "x"])
        resolver_requirements, _ = _build_resolver_inputs(
            [req], NabProjectConfig(), environment={}
        )
        proxy_keys = [k for k in resolver_requirements if k.startswith("demo[")]
        assert proxy_keys == ["demo[x]", "demo[y]", "demo[z]"]


class TestBuildConstraints:
    """``_build_constraints`` folds duplicate constraint lines."""

    def test_duplicate_constraint_intersects(self) -> None:
        """Two constraint lines for one package combine to their overlap."""
        out = _build_constraints(
            NabProjectConfig(constraints=("foo>=2.0", "foo<3.0")), environment={}
        )
        assert V("2.5") in out["foo"]
        assert V("1.0") not in out["foo"]
        assert V("5.0") not in out["foo"]

    def test_conflicting_constraints_raise(self) -> None:
        """Pinned-but-different constraint lines for one package raise."""
        with pytest.raises(ResolutionError, match="conflicting constraints"):
            _build_constraints(
                NabProjectConfig(constraints=("foo==1.0", "foo==2.0")), environment={}
            )

    def test_marker_false_constraint_dropped(self) -> None:
        """A constraint whose marker is False is not applied."""
        env = {"python_version": "3.12"}
        out = _build_constraints(
            NabProjectConfig(constraints=('foo<2.0 ; python_version < "3.0"',)),
            environment=env,
        )
        assert "foo" not in out

    def test_marker_true_constraint_applied(self) -> None:
        """A constraint whose marker is True still restricts the range."""
        env = {"python_version": "3.12"}
        out = _build_constraints(
            NabProjectConfig(constraints=('foo<2.0 ; python_version >= "3.0"',)),
            environment=env,
        )
        assert V("1.0") in out["foo"]
        assert V("5.0") not in out["foo"]

    def test_constraint_with_extras_rejected(self) -> None:
        """A constraint carrying extras is rejected, matching pip."""
        with pytest.raises(ConfigError, match="extras"):
            _build_constraints(
                NabProjectConfig(constraints=("foo[dev]<2.0",)), environment={}
            )

    def test_constraint_with_extras_rejected_under_false_marker(self) -> None:
        """Extras on a constraint are rejected even when its marker is False.

        pip rejects constraint extras at parse, before evaluating the marker,
        and the universal path does the same. The extras guard must run before
        the marker drop so a marker-false constraint is not silently accepted.
        """
        with pytest.raises(ConfigError, match="extras"):
            _build_constraints(
                NabProjectConfig(
                    constraints=('foo[dev]<2.0 ; python_version < "3.0"',)
                ),
                environment={"python_version": "3.12"},
            )

    def test_set_marker_constraint_dropped(self) -> None:
        """A constraint gated on a lockfile-only set marker drops, not crashes."""
        out = _build_constraints(
            NabProjectConfig(constraints=('foo<2.0 ; "x" in extras',)), environment={}
        )
        assert "foo" not in out


class TestResolvePyprojectConflicts:
    """End-to-end: contradictory folded requirements fail loudly."""

    def test_conflicting_groups_raise_resolution_error(self, tmp_path: Path) -> None:
        """Two groups pinning one package to different versions raise.

        The ``nab lock --all-groups`` shape: every group folds into the
        root, so two groups that pin one package differently must fail.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["foo"]\n'
            "[dependency-groups]\n"
            'g1 = ["bar==1.0"]\n'
            'g2 = ["bar==2.0"]\n'
        )
        # The conflict is caught while building resolver inputs, before
        # any fetch; FetchCoordinator is patched so a regression fails
        # offline instead of reaching PyPI.
        with (
            patch("nab_python.resolve.FetchCoordinator"),
            pytest.raises(ResolutionError, match="bar=="),
        ):
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
                groups=["g1", "g2"],
            )


class TestLoadGroupRequirementsByGroup:
    """``_load_group_requirements_by_group`` keeps group origin.

    Same expansion path as ``_load_group_requirements`` but returns a
    mapping of group name to its own list of requirements so a later
    check can name the group a requirement came from.
    """

    def test_empty_selection_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'x'\n")
        assert _group_requirements_by_group(read_pyproject_groups(path), [], path) == {}

    def test_missing_table_raises_with_selected_names(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'x'\n")
        with pytest.raises(LookupError, match=r"\[dependency-groups\] is missing"):
            _group_requirements_by_group(read_pyproject_groups(path), ["dev"], path)

    def test_maps_each_group_to_its_requirements(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'x'\n"
            "[dependency-groups]\n"
            "dev = ['pytest>=7']\n"
            "docs = ['sphinx<7', 'furo']\n"
        )
        per_group = _group_requirements_by_group(
            read_pyproject_groups(path), ["dev", "docs"], path
        )
        assert [str(r) for r in per_group["dev"]] == ["pytest>=7"]
        assert [str(r) for r in per_group["docs"]] == ["sphinx<7", "furo"]


class TestCheckGroupDisjointness:
    """``_check_group_disjointness`` names the two conflicting groups."""

    def test_direct_conflict_names_both_groups(self) -> None:
        per_group = {
            "docs": [Requirement("sphinx<7")],
            "test": [Requirement("sphinx>=7")],
        }
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, [_target({})])
        message = str(info.value)
        assert "'docs'" in message
        assert "'test'" in message
        assert "sphinx" in message
        assert "sphinx<7" in message
        assert "sphinx>=7" in message

    def test_message_sorts_group_names(self) -> None:
        """Group order in the message is sorted, not insertion order."""
        per_group = {
            "test": [Requirement("sphinx>=7")],
            "docs": [Requirement("sphinx<7")],
        }
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, [_target({})])
        message = str(info.value)
        assert message.index("'docs'") < message.index("'test'")

    def test_no_conflict_is_silent(self) -> None:
        per_group = {
            "docs": [Requirement("sphinx>=6,<8")],
            "test": [Requirement("sphinx>=7")],
        }
        _check_group_disjointness(per_group, [_target({})])

    def test_disjoint_packages_do_not_conflict(self) -> None:
        """Groups touching different packages never conflict."""
        per_group = {
            "docs": [Requirement("sphinx<7")],
            "test": [Requirement("pytest>=8")],
        }
        _check_group_disjointness(per_group, [_target({})])

    def test_single_group_is_noop(self) -> None:
        per_group = {"docs": [Requirement("sphinx<7"), Requirement("sphinx>=7")]}
        _check_group_disjointness(per_group, [_target({})])

    def test_empty_mapping_is_noop(self) -> None:
        _check_group_disjointness({}, [_target({})])

    def test_marker_filtered_requirement_is_skipped(self) -> None:
        """A requirement whose marker is False under the env is ignored."""
        per_group = {
            "docs": [Requirement("sphinx<7")],
            "test": [Requirement("sphinx>=7 ; python_version < '3'")],
        }
        _check_group_disjointness(per_group, [_target({"python_version": "3.12"})])

    def test_within_group_intersection_before_pairwise(self) -> None:
        """A group's own two ranges fold before the cross-group check.

        ``docs`` folds to ``>=6,<7``; ``test`` pins ``>=7``; the pair is
        empty so the conflict is reported.
        """
        per_group = {
            "docs": [Requirement("sphinx>=6"), Requirement("sphinx<7")],
            "test": [Requirement("sphinx>=7")],
        }
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, [_target({})])
        assert "sphinx" in str(info.value)

    def test_three_groups_names_the_conflicting_pair(self) -> None:
        """With three groups, only the conflicting pair is named."""
        per_group = {
            "docs": [Requirement("sphinx<7")],
            "lint": [Requirement("ruff>=0.5")],
            "test": [Requirement("sphinx>=7")],
        }
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, [_target({})])
        message = str(info.value)
        assert "'docs'" in message
        assert "'test'" in message
        assert "'lint'" not in message

    def test_url_requirement_does_not_conflict(self) -> None:
        """A URL requirement has no version range, so it never conflicts."""
        per_group = {
            "docs": [Requirement("sphinx @ https://example.com/sphinx.whl")],
            "test": [Requirement("sphinx>=7")],
        }
        _check_group_disjointness(per_group, [_target({})])

    def test_extras_only_requirement_does_not_conflict(self) -> None:
        """An extras-only requirement carries a full range, so no conflict."""
        per_group = {
            "docs": [Requirement("sphinx[docs]")],
            "test": [Requirement("sphinx>=7")],
        }
        _check_group_disjointness(per_group, [_target({})])


class TestFindGroupConflictsManyGroups:
    """``_find_group_conflicts`` only compares groups sharing a package.

    With many groups that touch distinct packages, the package-inverted
    walk must compare just the groups that name the same package, so the
    one true conflict is found and the unrelated groups produce nothing.
    """

    _GROUP_COUNT = 50
    _LEFT = "g10"
    _RIGHT = "g37"

    def _disjoint_groups(self) -> dict[str, list[Requirement]]:
        """Build groups that each require only their own unique package."""
        return {
            f"g{i:02d}": [Requirement(f"pkg{i:02d}>=1")]
            for i in range(self._GROUP_COUNT)
        }

    def test_single_conflicting_pair_is_found(self) -> None:
        """One incompatible shared package yields exactly one conflict."""
        per_group = self._disjoint_groups()
        per_group[self._LEFT].append(Requirement("shared<2"))
        per_group[self._RIGHT].append(Requirement("shared>=2"))
        conflicts = _find_group_conflicts(per_group, environment={})
        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert conflict.left_group == self._LEFT
        assert conflict.right_group == self._RIGHT
        assert conflict.left_group < conflict.right_group
        assert conflict.package == "shared"
        assert conflict.left_req == "shared<2"
        assert conflict.right_req == "shared>=2"

    def test_disjoint_variant_finds_nothing(self) -> None:
        """The same shared package with overlapping ranges is no conflict."""
        per_group = self._disjoint_groups()
        per_group[self._LEFT].append(Requirement("shared>=1"))
        per_group[self._RIGHT].append(Requirement("shared<5"))
        assert _find_group_conflicts(per_group, environment={}) == []


class TestResolvePyprojectGroupConflict:
    """End-to-end: a two-group direct conflict names the groups."""

    def test_conflict_message_names_groups(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["foo"]\n'
            "[dependency-groups]\n"
            'docs = ["sphinx<7"]\n'
            'test = ["sphinx>=7"]\n'
        )
        with (
            patch("nab_python.resolve.FetchCoordinator"),
            pytest.raises(ResolutionError) as info,
        ):
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
                groups=["docs", "test"],
            )
        message = str(info.value)
        assert "Dependency groups" in message
        assert "'docs'" in message
        assert "'test'" in message
        assert "sphinx" in message

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_single_group_skips_check(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """One group means no pair to check; the resolve proceeds."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["foo>=1.0"]\n'
            "[dependency-groups]\n"
            'docs = ["sphinx<7"]\n'
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {"foo": V("2.0")}

        result = _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            python_version="3.12.0",
            groups=["docs"],
        )
        assert "foo" in _pins(result)

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_no_groups_skips_check(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Zero groups means the check is a no-op and the resolve runs."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo>=1.0"]\n')
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {"foo": V("2.0")}

        result = _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")
        assert "foo" in _pins(result)

    @patch("nab_python.resolve.build_target_lock")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_no_conflict_multi_group_resolves(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Two compatible groups pass the check and resolve normally."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["foo>=1.0"]\n'
            "[dependency-groups]\n"
            'docs = ["sphinx>=6"]\n'
            'test = ["pytest>=8"]\n'
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {"foo": V("2.0")}

        result = _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            python_version="3.12.0",
            groups=["docs", "test"],
        )
        assert "foo" in _pins(result)


class TestAugmentResolutionError:
    """``resolve_pyproject`` enriches errors with provider hints."""

    def _no_versions_clause(self, package: str) -> Incompatibility:
        return Incompatibility(
            [Term(package, Range.full(), positive=True)],
            cause=IncompatibilityCause.NO_VERSIONS,
        )

    def test_appends_diagnostics_for_no_versions_packages(self) -> None:
        """A NO_VERSIONS clause whose package has a recorded reason is surfaced."""
        clause = self._no_versions_clause("missing-pkg")
        exc = ResolutionError("base message", incompatibility=clause)
        provider = MagicMock()
        provider.get_no_versions_reason.side_effect = lambda pkg: (
            "package not found on any configured index"
            if pkg == "missing-pkg"
            else None
        )
        _augment_resolution_error(exc, provider)
        text = str(exc)
        assert "Diagnostics:" in text
        assert "missing-pkg: package not found on any configured index" in text

    def test_no_op_when_incompatibility_is_none(self) -> None:
        """An exception without a derivation tree is returned unchanged."""
        exc = ResolutionError("base", incompatibility=None)
        provider = MagicMock()
        _augment_resolution_error(exc, provider)
        assert str(exc) == "base"

    def test_no_op_when_no_reasons_recorded(self) -> None:
        """The exception is unchanged when the provider has no hints."""
        clause = self._no_versions_clause("missing-pkg")
        exc = ResolutionError("base", incompatibility=clause)
        provider = MagicMock()
        provider.get_no_versions_reason.return_value = None
        _augment_resolution_error(exc, provider)
        assert str(exc) == "base"

    def test_dedupes_packages_seen_multiple_times(self) -> None:
        """Each package's reason is appended at most once."""
        first = self._no_versions_clause("foo")
        second = self._no_versions_clause("foo")
        derived = Incompatibility(
            [Term("foo", Range.full(), positive=True)],
            cause=IncompatibilityCause.DERIVED,
            cause_left=first,
            cause_right=second,
        )
        exc = ResolutionError("base", incompatibility=derived)
        provider = MagicMock()
        provider.get_no_versions_reason.return_value = (
            "no version matches the requirement"
        )
        _augment_resolution_error(exc, provider)
        text = str(exc)
        assert text.count("foo: no version matches the requirement") == 1

    def test_walks_through_derived_clauses(self) -> None:
        """The walk follows ``cause_left`` and ``cause_right`` recursively."""
        leaf = self._no_versions_clause("buried-pkg")
        derived = Incompatibility(
            [Term("buried-pkg", Range.full(), positive=True)],
            cause=IncompatibilityCause.DERIVED,
            cause_left=leaf,
            cause_right=None,
        )
        packages = _walk_no_versions_packages(derived)
        assert packages == ["buried-pkg"]

    def test_walk_skips_non_string_packages(self) -> None:
        """Non-str term packages (e.g. the root sentinel) are filtered out."""
        sentinel = object()
        clause = Incompatibility(
            [Term(sentinel, Range.full(), positive=True)],
            cause=IncompatibilityCause.NO_VERSIONS,
        )
        assert _walk_no_versions_packages(clause) == []

    def test_walk_names_lookahead_grouped_clause_candidate(self) -> None:
        """A look-ahead grouped clause (two positive terms) names its
        candidate package; the blocker package is not collected."""
        clause = Incompatibility(
            [
                Term("cand", Range.full(), positive=True),
                Term("blocker", Range.singleton(1), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert _walk_no_versions_packages(clause) == ["cand"]

    def test_grouped_clause_hint_survives_a_later_range(self) -> None:
        """Accepted tolerance: reasons are keyed by package name, so a later
        range still surfaces the reason an earlier ask recorded."""
        clause = Incompatibility(
            [
                Term("cand", Range.full(), positive=True),
                Term("blocker", Range.singleton(1), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        exc = ResolutionError("base message", incompatibility=clause)
        provider = MagicMock()
        provider.get_no_versions_reason.side_effect = lambda pkg: (
            "no version matches the requirement" if pkg == "cand" else None
        )
        _augment_resolution_error(exc, provider)
        assert "cand: no version matches the requirement" in str(exc)

    def test_walk_skips_real_dependency_clauses(self) -> None:
        """A dependency clause with a negative dep term is not collected."""
        clause = Incompatibility(
            [
                Term("parent", Range.singleton(1), positive=True),
                Term("dep", Range.full(), positive=False),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert _walk_no_versions_packages(clause) == []

    def test_walk_skips_non_string_grouped_candidate(self) -> None:
        """Grouped clauses with a non-str candidate package are filtered."""
        clause = Incompatibility(
            [
                Term(object(), Range.full(), positive=True),
                Term("blocker", Range.singleton(1), positive=True),
            ],
            cause=IncompatibilityCause.DEPENDENCY,
        )
        assert _walk_no_versions_packages(clause) == []

    def test_walk_visits_each_node_once(self) -> None:
        """A diamond-shaped derivation visits the shared leaf only once."""
        leaf = self._no_versions_clause("shared")
        left = Incompatibility(
            [Term("shared", Range.full(), positive=True)],
            cause=IncompatibilityCause.DERIVED,
            cause_left=leaf,
            cause_right=None,
        )
        right = Incompatibility(
            [Term("shared", Range.full(), positive=True)],
            cause=IncompatibilityCause.DERIVED,
            cause_left=leaf,
            cause_right=None,
        )
        top = Incompatibility(
            [Term("shared", Range.full(), positive=True)],
            cause=IncompatibilityCause.DERIVED,
            cause_left=left,
            cause_right=right,
        )
        # Walk should record ``shared`` exactly once even though ``leaf``
        # is reachable from two branches of the derivation DAG.
        assert _walk_no_versions_packages(top) == ["shared"]

    def test_walks_a_chain_deeper_than_the_recursion_limit(self) -> None:
        """A derivation longer than the recursion limit is still walked."""
        node: Incompatibility = self._no_versions_clause("buried-pkg")
        for _ in range(sys.getrecursionlimit() + 100):
            node = Incompatibility(
                [Term("buried-pkg", Range.full(), positive=True)],
                cause=IncompatibilityCause.DERIVED,
                cause_left=node,
                cause_right=None,
            )

        assert _walk_no_versions_packages(node) == ["buried-pkg"]

    def test_resolve_pyproject_re_raises_with_diagnostic(self, tmp_path: Path) -> None:
        """``resolve_pyproject`` re-raises the augmented ResolutionError."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')

        clause = self._no_versions_clause("foo")
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.Resolver") as mock_resolver_cls,
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider_cls.return_value.get_no_versions_reason.return_value = (
                "package not found on any configured index"
            )
            mock_resolver_cls.return_value.resolve.side_effect = ResolutionError(
                "base", incompatibility=clause
            )
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")
        assert "Diagnostics:" in str(info.value)
        assert "foo: package not found on any configured index" in str(info.value)

    def test_transitive_conflict_reports_the_blocker(self, tmp_path: Path) -> None:
        """A transitive conflict names the blocker instead of a bare no-match.

        ``foo`` needs ``bar>=2`` and ``baz`` needs ``bar==1.0``, so
        look-ahead rejects every ``foo`` candidate.  The project wrote no
        specifier on ``foo``, so "no version matches the requirement"
        would point the user at a fix that does not exist.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo", "baz"]\n',
            encoding="utf-8",
        )

        coordinator = make_coordinator(
            listings={
                "foo": _index_wheels("foo", "3.0", "3.1"),
                "bar": _index_wheels("bar", "1.0", "2.0"),
                "baz": _index_wheels("baz", "5.0"),
            },
            metadata_by_version={
                "3.0": _metadata("foo", "3.0", "bar>=2"),
                "3.1": _metadata("foo", "3.1", "bar>=2"),
                "1.0": _metadata("bar", "1.0"),
                "2.0": _metadata("bar", "2.0"),
                "5.0": _metadata("baz", "5.0", "bar==1.0"),
            },
        )

        with patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        diagnostics = str(info.value).split("Diagnostics:")[1]
        assert "foo: every version in range was rejected" in diagnostics
        assert "bar" in diagnostics
        assert "foo: no version matches the requirement" not in diagnostics

    def test_constraint_does_not_hide_the_transitive_blocker(
        self, tmp_path: Path
    ) -> None:
        """A user constraint keeps the blocker reason, not a bare no-match.

        ``foo<2.0`` clips away foo's only versions, which each need
        ``lib==5.0`` and so conflict with ``app``'s ``lib==9.0``.  The
        constraint-attribution probe recomputes the blocker reason, so it
        must survive: the failure names the ``lib`` conflict rather than
        reporting "no version matches the requirement", which would point
        the user at a missing release that does not exist.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo", "app"]\n'
            '[tool.nab]\nconstraints = ["foo<2.0"]\n',
            encoding="utf-8",
        )

        coordinator = make_coordinator(
            listings={
                "foo": _index_wheels("foo", "3.0", "4.0"),
                "app": _index_wheels("app", "1.0"),
                "lib": _index_wheels("lib", "5.0", "9.0"),
            },
            metadata_by_version={
                "3.0": _metadata("foo", "3.0", "lib==5.0"),
                "4.0": _metadata("foo", "4.0", "lib==5.0"),
                "1.0": _metadata("app", "1.0", "lib==9.0"),
                "5.0": _metadata("lib", "5.0"),
                "9.0": _metadata("lib", "9.0"),
            },
        )

        with patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        diagnostics = str(info.value).split("Diagnostics:")[1]
        assert (
            "foo: every version in range was rejected: requires lib != 9.0"
            in diagnostics
        )
        assert "foo: no version matches the requirement" not in diagnostics


def _tuple_for_python(python_version: str) -> ResolveTarget:
    """Build a linux_x86_64 target for ``python_version``.

    Only the marker environment matters for the group pre-pass, so the
    platform axis is held constant and the python axis varies; the
    label encodes the python version so a conflict message can be
    asserted against it.
    """
    return ResolveTarget.for_declared(
        python_version=python_version,
        spec=PlatformSpec("linux_x86_64"),
    )


class TestCheckGroupDisjointnessAcrossTuples:
    """``_check_group_disjointness_across_tuples`` runs the per-group
    range check under each tuple's marker environment and raises early
    when any tuple shows an empty intersection."""

    def test_conflict_gated_to_some_tuples_names_them(self) -> None:
        """A marker-gated conflict names only the tuples it holds on.

        ``foo<2`` is live only on the 3.10 tuple; ``foo>=2`` is live
        everywhere.  The intersection is empty on 3.10 and full on
        3.12, so the message names the 3.10 tuple and not the 3.12 one.
        """
        per_group = {
            "a": [Requirement("foo<2 ; python_version < '3.11'")],
            "b": [Requirement("foo>=2")],
        }
        tuples = [_tuple_for_python("3.10"), _tuple_for_python("3.12")]
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, tuples)
        message = str(info.value)
        assert "'a'" in message
        assert "'b'" in message
        assert "foo" in message
        assert "py310-linux_x86_64" in message
        assert "py312-linux_x86_64" not in message

    def test_conflict_on_all_tuples_names_all(self) -> None:
        """An unconditional conflict names every targeted tuple."""
        per_group = {
            "a": [Requirement("foo<2")],
            "b": [Requirement("foo>=2")],
        }
        tuples = [_tuple_for_python("3.10"), _tuple_for_python("3.12")]
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, tuples)
        message = str(info.value)
        assert "py310-linux_x86_64" in message
        assert "py312-linux_x86_64" in message

    def test_no_conflict_is_silent(self) -> None:
        """Compatible groups across all tuples raise nothing."""
        per_group = {
            "a": [Requirement("foo>=1")],
            "b": [Requirement("foo<5")],
        }
        tuples = [_tuple_for_python("3.10"), _tuple_for_python("3.12")]
        _check_group_disjointness(per_group, tuples)

    def test_single_group_is_noop(self) -> None:
        per_group = {"a": [Requirement("foo<2"), Requirement("foo>=2")]}
        tuples = [_tuple_for_python("3.10"), _tuple_for_python("3.12")]
        _check_group_disjointness(per_group, tuples)

    def test_empty_mapping_is_noop(self) -> None:
        tuples = [_tuple_for_python("3.12")]
        _check_group_disjointness({}, tuples)

    def test_three_groups_names_only_conflicting_pair(self) -> None:
        """With three groups, the message names the conflicting pair only."""
        per_group = {
            "a": [Requirement("foo<2")],
            "b": [Requirement("foo>=2")],
            "c": [Requirement("bar>=1")],
        }
        tuples = [_tuple_for_python("3.12")]
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, tuples)
        message = str(info.value)
        assert "'a'" in message
        assert "'b'" in message
        assert "'c'" not in message

    def test_message_sorts_group_names(self) -> None:
        """Group names print sorted, not in insertion order."""
        per_group = {
            "b": [Requirement("foo>=2")],
            "a": [Requirement("foo<2")],
        }
        tuples = [_tuple_for_python("3.12")]
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, tuples)
        message = str(info.value)
        assert message.index("'a'") < message.index("'b'")

    def test_tuple_labels_sorted_in_message(self) -> None:
        """Affected tuple labels appear in sorted order.

        The tuples are passed newest-first; the message must still list
        the labels in sorted order so the diagnostic is deterministic
        regardless of matrix ordering.
        """
        per_group = {
            "a": [Requirement("foo<2")],
            "b": [Requirement("foo>=2")],
        }
        tuples = [_tuple_for_python("3.12"), _tuple_for_python("3.10")]
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, tuples)
        message = str(info.value)
        assert message.index("py310-linux_x86_64") < message.index("py312-linux_x86_64")


class TestResolveUniversalPyprojectGroupConflict:
    """``resolve_universal_pyproject`` raises early for a direct group
    conflict instead of returning per-tuple ``TupleResult.error``
    strings."""

    def test_marker_gated_conflict_raises_early(self, tmp_path: Path) -> None:
        """A conflict live on only one matrix tuple raises before resolve.

        ``resolve_universal`` is patched so a regression that lets the
        pre-pass slip through would call it (and the assertion that it
        was never called fails) rather than reaching PyPI.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[dependency-groups]\n"
            "a = [\"foo<2 ; python_version < '3.12'\"]\n"
            'b = ["foo>=2"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ResolutionError) as info,
        ):
            _resolved(pyproject, groups=["a", "b"])
        mock_universal.assert_not_called()
        message = str(info.value)
        assert "Dependency groups" in message
        assert "'a'" in message
        assert "'b'" in message
        assert "foo" in message
        assert "py311-linux_x86_64" in message
        assert "py312-linux_x86_64" not in message

    def test_conflict_on_all_tuples_raises_early(self, tmp_path: Path) -> None:
        """An unconditional conflict raises naming every tuple."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[dependency-groups]\n"
            'a = ["foo<2"]\n'
            'b = ["foo>=2"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_python.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ResolutionError) as info,
        ):
            _resolved(pyproject, groups=["a", "b"])
        mock_universal.assert_not_called()
        message = str(info.value)
        assert "py311-linux_x86_64" in message
        assert "py312-linux_x86_64" in message

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_no_conflict_proceeds_to_resolve(
        self, mock_universal: MagicMock, tmp_path: Path
    ) -> None:
        """Two compatible groups skip the pre-pass and reach resolution."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[dependency-groups]\n"
            'a = ["foo>=1"]\n'
            'b = ["foo<5"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        sentinel = MagicMock()
        mock_universal.return_value = sentinel
        result = _resolved(pyproject, groups=["a", "b"])
        assert result is sentinel

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_single_group_skips_prepass(
        self, mock_universal: MagicMock, tmp_path: Path
    ) -> None:
        """One group means no pair to check; resolution proceeds."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[dependency-groups]\n"
            'a = ["foo<2"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        sentinel = MagicMock()
        mock_universal.return_value = sentinel
        result = _resolved(pyproject, groups=["a"])
        assert result is sentinel

    @patch("nab_python.resolve.resolve_with_coordinator")
    def test_no_groups_skips_prepass(
        self, mock_universal: MagicMock, tmp_path: Path
    ) -> None:
        """Zero groups means the pre-pass is a no-op; resolution proceeds."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        sentinel = MagicMock()
        mock_universal.return_value = sentinel
        result = _resolved(pyproject)
        assert result is sentinel


class TestLocalVcsRequiresPython:
    """A local or VCS pin must satisfy the resolve's target Python.

    Index candidates are filtered by Requires-Python while listing;
    local-path and VCS sources skip that filter, so the single-env
    resolve checks them after resolving, mirroring the universal path.
    """

    def _provider_with_local(
        self, tmp_path: Path, body: str, *, python_version: str = "3.10.0"
    ) -> Provider:
        (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
        coordinator = make_coordinator([], package="foo")
        provider = Provider(
            coordinator,
            target=ResolveTarget.for_host_python(python_version),
            local_sources=[LocalSource("foo", str(tmp_path))],
            build_policy=BuildPolicy.NEVER,
        )
        provider.fetch_versions("foo")
        return provider

    def test_excluding_python_raises(self, tmp_path: Path) -> None:
        provider = self._provider_with_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = ">=3.12"\n',
        )
        with pytest.raises(ResolutionError, match="foo 1.0 requires Python"):
            _raise_for_source_python(provider, provider.target, {"foo": V("1.0")})

    def test_compatible_python_does_not_raise(self, tmp_path: Path) -> None:
        provider = self._provider_with_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = ">=3.10"\n',
        )
        _raise_for_source_python(provider, provider.target, {"foo": V("1.0")})

    def test_no_requires_python_does_not_raise(self, tmp_path: Path) -> None:
        provider = self._provider_with_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\n',
        )
        _raise_for_source_python(provider, provider.target, {"foo": V("1.0")})

    def test_non_managed_pin_is_skipped(self, tmp_path: Path) -> None:
        provider = self._provider_with_local(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = ">=3.12"\n',
        )
        _raise_for_source_python(provider, provider.target, {"bar": V("9.0")})

    def test_resolve_pyproject_excluding_python_raises(self, tmp_path: Path) -> None:
        member = tmp_path / "foo"
        member.mkdir()
        (member / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = ">=3.12"\n',
            encoding="utf-8",
        )
        root = tmp_path / "pyproject.toml"
        root.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n'
            "[[tool.nab.local-sources]]\n"
            'name = "foo"\npath = "foo"\n',
            encoding="utf-8",
        )
        fake = make_coordinator([], package="foo")
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda _self: fake
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError, match="foo 1.0 requires Python"):
                _resolved(root, _FAKE_TRANSPORT, python_version="3.10.0")

    def test_resolve_pyproject_declared_target_satisfies_local(
        self, tmp_path: Path
    ) -> None:
        """The declared environment's Python is the local-source target.

        A source valid only for the declared Python must not be refused for
        failing the host Python. Mirrors the universal per-tuple check.
        """
        member = tmp_path / "foo"
        member.mkdir()
        (member / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = ">=3.12"\n',
            encoding="utf-8",
        )
        root = tmp_path / "pyproject.toml"
        root.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n'
            '[tool.nab]\nbuild-policy = "never"\n'
            "[tool.nab.environment]\n"
            'python = "3.12.1"\n'
            '[[tool.nab.local-sources]]\nname = "foo"\npath = "foo"\n',
            encoding="utf-8",
        )
        fake = make_coordinator([], package="foo")
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda _self: fake
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(root, _FAKE_TRANSPORT)
        assert _pins(result)["foo"] == V("1.0")

    def test_resolve_pyproject_declared_target_satisfies_index(
        self, tmp_path: Path
    ) -> None:
        """The declared environment's Python also gates index candidates.

        An index candidate whose Requires-Python admits only the declared
        Python must survive the listing filter, matching the local-source
        check so both judge the same Requires-Python identically.
        """
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://example.com/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=">=3.12",
            has_metadata=True,
            upload_time=None,
        )
        root = tmp_path / "pyproject.toml"
        root.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n'
            '[tool.nab]\nbuild-policy = "never"\n'
            "[tool.nab.environment]\n"
            'python = "3.12.1"\n',
            encoding="utf-8",
        )
        fake = make_coordinator([wheel], package="foo", auto_metadata=True)
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda _self: fake
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(root, _FAKE_TRANSPORT)
        assert _pins(result)["foo"] == V("1.0")

    def test_resolve_index_metadata_requires_python_rejects_omitted_listing(
        self, tmp_path: Path
    ) -> None:
        """The wheel METADATA gates an index candidate the listing does not.

        The Simple-API requires-python hint is optional; when the listing omits
        it the wheel's METADATA carries the authoritative Requires-Python, and a
        candidate it marks incompatible with the target is rejected rather than
        written to the lock.
        """
        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://example.com/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )
        root = tmp_path / "pyproject.toml"
        root.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n',
            encoding="utf-8",
        )
        fake = make_coordinator(
            [wheel],
            package="foo",
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                "Requires-Python: >=3.12\n\n"
            ),
        )
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda _self: fake
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError, match="requires Python"):
                _resolved(root, _FAKE_TRANSPORT, python_version="3.8.0")


def _metadata(name: str, version: str, *requires: str) -> str:
    """METADATA text for ``name`` ``version`` with one Requires-Dist per entry."""
    body = "".join(f"Requires-Dist: {req}\n" for req in requires)
    return f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{body}\n"


def _index_wheels(name: str, *versions: str) -> list[WheelFile]:
    """One pure-python wheel per version, dependency-free in minimal METADATA."""
    return [
        WheelFile(
            filename=f"{name}-{v}-py3-none-any.whl",
            url=f"https://example.com/{name}-{v}-py3-none-any.whl",
            version=v,
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )
        for v in versions
    ]


class TestLocalSourceExtrasMarkers:
    """Extras and markers on a local-source package resolve like an index one.

    A local source is materialised into a synthetic single-version listing
    from its pyproject metadata, then flows through the same extras-proxy and
    marker machinery as an index package. These end-to-end checks pin that
    parity, plus the invariant that the single synthetic version is still
    subject to the requirement's range (no short-circuit past an unsatisfying
    pin).
    """

    @staticmethod
    def _resolve(
        tmp_path: Path,
        root_body: str,
        members: dict[str, str],
        coordinator: MagicMock,
        python_version: str,
    ) -> ResolveResult:
        (tmp_path / "pyproject.toml").write_text(root_body, encoding="utf-8")
        for name, body in members.items():
            member = tmp_path / name
            member.mkdir()
            (member / "pyproject.toml").write_text(body, encoding="utf-8")
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            return _resolved(
                tmp_path / "pyproject.toml",
                _FAKE_TRANSPORT,
                python_version=python_version,
            )

    _ROOT_FOO_GPU = (
        '[project]\nname = "proj"\ndependencies = ["foo[gpu]"]\n'
        '[[tool.nab.local-sources]]\nname = "foo"\npath = "foo"\n'
    )

    def test_extra_pulls_its_dependency(self, tmp_path: Path) -> None:
        coordinator = make_coordinator(
            listings={"bar": _index_wheels("bar", "2.0", "3.0")},
            auto_metadata=True,
        )
        result = self._resolve(
            tmp_path,
            self._ROOT_FOO_GPU,
            {
                "foo": '[project]\nname = "foo"\nversion = "1.0"\n'
                '[project.optional-dependencies]\ngpu = ["bar>=2"]\n',
            },
            coordinator,
            "3.12.0",
        )
        assert _pins(result) == {"foo": V("1.0"), "bar": V("3.0")}

    @pytest.mark.parametrize(
        ("python_version", "expects_bar"),
        [("3.12.0", True), ("3.9.0", False)],
    )
    def test_extra_dependency_marker_gated_by_target(
        self, tmp_path: Path, python_version: str, expects_bar: bool
    ) -> None:
        coordinator = make_coordinator(
            listings={"bar": _index_wheels("bar", "2.0")}, auto_metadata=True
        )
        result = self._resolve(
            tmp_path,
            self._ROOT_FOO_GPU,
            {
                "foo": '[project]\nname = "foo"\nversion = "1.0"\n'
                "[project.optional-dependencies]\n"
                "gpu = [\"bar ; python_version >= '3.11'\"]\n",
            },
            coordinator,
            python_version,
        )
        expected = {"foo": V("1.0")}
        if expects_bar:
            expected["bar"] = V("2.0")
        assert _pins(result) == expected

    @pytest.mark.parametrize(
        ("python_version", "expects_bar"),
        [("3.9.0", True), ("3.12.0", False)],
    )
    def test_local_dependency_marker_gated_by_target(
        self, tmp_path: Path, python_version: str, expects_bar: bool
    ) -> None:
        coordinator = make_coordinator(
            listings={"bar": _index_wheels("bar", "1.0")}, auto_metadata=True
        )
        result = self._resolve(
            tmp_path,
            '[project]\nname = "proj"\ndependencies = ["foo"]\n'
            '[[tool.nab.local-sources]]\nname = "foo"\npath = "foo"\n',
            {
                "foo": '[project]\nname = "foo"\nversion = "1.0"\n'
                "dependencies = [\"bar ; python_version < '3.10'\"]\n",
            },
            coordinator,
            python_version,
        )
        expected = {"foo": V("1.0")}
        if expects_bar:
            expected["bar"] = V("1.0")
        assert _pins(result) == expected

    def test_version_mismatch_is_unsat_not_a_wrong_pin(self, tmp_path: Path) -> None:
        coordinator = make_coordinator(listings={})
        with pytest.raises(ResolutionError):
            self._resolve(
                tmp_path,
                '[project]\nname = "proj"\ndependencies = ["foo>=2.0"]\n'
                '[[tool.nab.local-sources]]\nname = "foo"\npath = "foo"\n',
                {"foo": '[project]\nname = "foo"\nversion = "1.0"\n'},
                coordinator,
                "3.12.0",
            )


# A real linux CPython 3.12.3, the kind of interpreter a lock resolved for
# ``python = "3.12"`` has to install on.
_PY312_ENV: dict[str, str] = {
    "implementation_name": "cpython",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_system": "Linux",
    "python_full_version": "3.12.3",
    "python_version": "3.12",
    "sys_platform": "linux",
}


class TestLockDeclaresItsEnvironment:
    """A single-environment lock declares the environment it was resolved
    for.  Every dependency whose marker was False here was dropped, so an
    installer that answers one of those markers differently needs a
    different package set: PEP 751 ``environments`` refuses it.
    """

    @staticmethod
    def _resolve(tmp_path: Path, body: str, coordinator: MagicMock) -> LockInput:
        path = tmp_path / "pyproject.toml"
        path.write_text(body, encoding="utf-8")
        with patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(path, _FAKE_TRANSPORT)
        return build_lock_input(result, config=read_pyproject_config(path))

    _PYPROJECT = (
        '[project]\nname = "proj"\ndependencies = ["foo"]\n'
        '[tool.nab]\nbuild-policy = "never"\n'
        "[tool.nab.environment]\n"
        'python = "3.12"\nplatform = "linux_x86_64"\n'
    )

    @staticmethod
    def _coordinator(requires_dist: str = "") -> MagicMock:
        return make_coordinator(
            _index_wheels("foo", "1.0"),
            package="foo",
            metadata_text=(
                "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n" + requires_dist
            ),
        )

    def test_the_three_axes_are_always_declared(self, tmp_path: Path) -> None:
        lock_input = self._resolve(tmp_path, self._PYPROJECT, self._coordinator())
        (environment,) = lock_input.environments
        assert str(environment) == (
            'python_version == "3.12" and sys_platform == "linux"'
            ' and platform_machine == "x86_64"'
        )

    def test_a_dependency_marker_declares_its_variable(self, tmp_path: Path) -> None:
        """tqdm's ``colorama ; platform_system == "Windows"`` is the real case."""
        lock_input = self._resolve(
            tmp_path,
            self._PYPROJECT,
            self._coordinator(
                'Requires-Dist: colorama ; platform_system == "Windows"\n'
            ),
        )
        (environment,) = lock_input.environments
        assert 'platform_system == "Linux"' in str(environment)
        assert "colorama" not in _locked(lock_input)

    def test_a_root_requirement_marker_declares_its_variable(
        self, tmp_path: Path
    ) -> None:
        """Root markers are evaluated before the provider exists."""
        body = self._PYPROJECT.replace(
            'dependencies = ["foo"]',
            'dependencies = ["foo", "winonly; os_name == \'nt\'"]',
        )
        lock_input = self._resolve(tmp_path, body, self._coordinator())
        (environment,) = lock_input.environments
        assert 'os_name == "posix"' in str(environment)

    def test_a_constraint_marker_declares_its_variable(self, tmp_path: Path) -> None:
        body = self._PYPROJECT.replace(
            '[tool.nab]\nbuild-policy = "never"\n',
            '[tool.nab]\nbuild-policy = "never"\n'
            "constraints = [\"foo<9; implementation_name == 'cpython'\"]\n",
        )
        lock_input = self._resolve(tmp_path, body, self._coordinator())
        (environment,) = lock_input.environments
        assert 'implementation_name == "cpython"' in str(environment)

    def test_an_unboundable_marker_warns_and_stays_undeclared(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A kernel-versioned marker names one machine; the lock cannot bound it."""
        with caplog.at_level(logging.WARNING, logger="nab_python.resolve"):
            lock_input = self._resolve(
                tmp_path,
                self._PYPROJECT,
                self._coordinator('Requires-Dist: bar ; platform_release >= "5.10"\n'),
            )
        (environment,) = lock_input.environments
        assert "platform_release" not in str(environment)
        assert "platform_release" in caplog.text

    def test_pypy_implementation_version_marker_warns_and_stays_undeclared(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """PyPy reports 7.3.x there, not its Python level, so the synthetic
        value cannot be bounded; the lock leaves the axis open and warns.
        """
        body = self._PYPROJECT.replace(
            'platform = "linux_x86_64"\n',
            'platform = "linux_x86_64"\nimplementation = "pypy"\n',
        )
        with caplog.at_level(logging.WARNING, logger="nab_python.resolve"):
            lock_input = self._resolve(
                tmp_path,
                body,
                self._coordinator(
                    'Requires-Dist: bar ; implementation_version >= "7.3"\n'
                ),
            )
        (environment,) = lock_input.environments
        assert "implementation_version" not in str(environment)
        assert "bar" not in _locked(lock_input)
        assert "implementation_version" in caplog.text
        real_pypy = {
            **_PY312_ENV,
            "implementation_name": "pypy",
            "platform_python_implementation": "PyPy",
            "implementation_version": "7.3.17",
            "python_full_version": "3.12.9",
        }
        assert Marker(str(environment)).evaluate(real_pypy)

    def test_a_pypy_target_without_the_axis_declares_and_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-CPython resolve that never reads implementation_version keeps
        its ordinary declaration and raises no axis warning.
        """
        body = self._PYPROJECT.replace(
            'platform = "linux_x86_64"\n',
            'platform = "linux_x86_64"\nimplementation = "pypy"\n',
        )
        with caplog.at_level(logging.WARNING, logger="nab_python.resolve"):
            lock_input = self._resolve(
                tmp_path,
                body,
                self._coordinator('Requires-Dist: bar ; sys_platform == "win32"\n'),
            )
        (environment,) = lock_input.environments
        assert 'sys_platform == "linux"' in str(environment)
        assert "bar" not in _locked(lock_input)
        assert "implementation_version" not in caplog.text

    def test_a_uniform_full_version_marker_leaves_a_plain_row(
        self, tmp_path: Path
    ) -> None:
        """A ``3.12`` minor is a micro interval, but ``<= "3.11.0a6"`` reads the
        same (false) on every real 3.12, so it names no in-minor boundary.  The
        minor is not split, ``tomli`` is dropped, and the row is the plain minor
        with no ``python_full_version`` clause.
        """
        lock_input = self._resolve(
            tmp_path,
            self._PYPROJECT,
            self._coordinator(
                'Requires-Dist: tomli ; python_full_version <= "3.11.0a6"\n'
            ),
        )
        (environment,) = lock_input.environments
        assert "python_full_version" not in str(environment)
        assert str(environment) == (
            'python_version == "3.12" and sys_platform == "linux"'
            ' and platform_machine == "x86_64"'
        )
        assert "tomli" not in _locked(lock_input)
        assert Marker(str(environment)).evaluate(_PY312_ENV)

    def test_a_marker_that_splits_the_micros_resolves_each_side(
        self, tmp_path: Path
    ) -> None:
        """A micro that changes the pins splits the minor into two slices.

        ``foo`` needs ``bar`` only on 3.12.4 and up, so the minor cannot be
        declared by how ``3.12.0`` read the clause: that would exclude every
        real 3.12.  The engine resolves one slice per side of the boundary, each
        with its own environment row and pins, and ``bar`` joins only the upper
        slice.
        """
        coordinator = make_coordinator(
            listings={
                "foo": _index_wheels("foo", "1.0"),
                "bar": _index_wheels("bar", "2.0"),
            },
            metadata_by_version={
                "1.0": (
                    "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
                    'Requires-Dist: bar ; python_full_version >= "3.12.4"\n'
                ),
                "2.0": "Metadata-Version: 2.1\nName: bar\nVersion: 2.0\n",
            },
        )
        lock_input = self._resolve(tmp_path, self._PYPROJECT, coordinator)

        below = lock_input.targets["py312-linux_x86_64-pf3120"]
        above = lock_input.targets["py312-linux_x86_64-pf3124"]
        assert set(below.pins) == {"foo"}
        assert set(above.pins) == {"foo", "bar"}

        rows = {
            "below" if 'python_full_version < "3.12.4"' in str(m) else "above": str(m)
            for m in lock_input.environments
        }
        assert set(rows) == {"below", "above"}
        assert 'python_full_version >= "3.12.4.dev0"' in rows["above"]

        # The two rows are disjoint and together cover the whole minor.
        low = {**_PY312_ENV, "python_full_version": "3.12.1"}
        high = {**_PY312_ENV, "python_full_version": "3.12.5"}
        assert Marker(rows["below"]).evaluate(low)
        assert not Marker(rows["below"]).evaluate(high)
        assert Marker(rows["above"]).evaluate(high)
        assert not Marker(rows["above"]).evaluate(low)


class TestExtraAndGroupMembershipMarkers:
    """A selected extra or group gates the packages only it reaches.

    End to end from ``pyproject.toml`` to the emitted lock.  PEP 751
    defaults an install to no extras and to ``default-groups``, so a
    package a selection alone pulls in has to carry ``'name' in extras``
    / ``'name' in dependency_groups``.
    """

    _MEMBERS: ClassVar[dict[str, str]] = {
        "core": '[project]\nname = "core"\nversion = "1.0"\n',
        "mytool": '[project]\nname = "mytool"\nversion = "2.0"\n'
        'dependencies = ["subtool"]\n',
        "mydev": '[project]\nname = "mydev"\nversion = "3.0"\n',
        "subtool": '[project]\nname = "subtool"\nversion = "4.0"\n',
    }

    _ROOT = (
        '[project]\nname = "app"\nversion = "1.0"\ndependencies = ["core"]\n'
        '[project.optional-dependencies]\ncli = ["mytool"]\n'
        '[dependency-groups]\ndev = ["mydev"]\n'
        + "".join(
            f'[[tool.nab.local-sources]]\nname = "{name}"\npath = "{name}"\n'
            for name in ("core", "mytool", "mydev", "subtool")
        )
    )

    @staticmethod
    def _lock(
        tmp_path: Path,
        *,
        extras: tuple[str, ...] = (),
        groups: tuple[str, ...] = (),
        root: str | None = None,
        members: dict[str, str] | None = None,
    ) -> Pylock:
        (tmp_path / "pyproject.toml").write_text(
            root if root is not None else TestExtraAndGroupMembershipMarkers._ROOT,
            encoding="utf-8",
        )
        bodies = (
            members
            if members is not None
            else TestExtraAndGroupMembershipMarkers._MEMBERS
        )
        for name, body in bodies.items():
            member = tmp_path / name
            member.mkdir()
            (member / "pyproject.toml").write_text(body, encoding="utf-8")
        with patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: make_coordinator([])
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            path = tmp_path / "pyproject.toml"
            config = read_pyproject_config(path)
            result = _resolved(
                path,
                _FAKE_TRANSPORT,
                config=config,
                extras=extras,
                groups=groups,
            )
        pylock = build_pylock(
            build_lock_input(
                result,
                config=config,
                extras=extras,
                dependency_groups=groups,
            ),
            lock_dir=tmp_path,
        )
        pylock.validate()
        return pylock

    @staticmethod
    def _markers(pylock: Pylock) -> dict[str, str | None]:
        return {
            str(pkg.name): str(pkg.marker) if pkg.marker else None
            for pkg in pylock.packages
        }

    @staticmethod
    def _selected(pylock: Pylock, **kwargs: list[str]) -> set[str]:
        return {str(pkg.name) for pkg, _ in pylock.select(**kwargs)}

    def test_extra_only_package_carries_extras_membership(self, tmp_path: Path) -> None:
        pylock = self._lock(tmp_path, extras=("cli",), groups=("dev",))

        assert self._markers(pylock) == {
            "core": None,
            "mydev": '"dev" in dependency_groups',
            "mytool": '"cli" in extras',
            "subtool": '"cli" in extras',
        }

    def test_default_install_skips_extra_and_group_packages(
        self, tmp_path: Path
    ) -> None:
        """The spec's default install context: no extras, no groups."""
        pylock = self._lock(tmp_path, extras=("cli",), groups=("dev",))

        assert self._selected(pylock) == {"core"}
        assert self._selected(pylock, extras=["cli"]) == {"core", "mytool", "subtool"}
        assert self._selected(pylock, dependency_groups=["dev"]) == {"core", "mydev"}
        assert self._selected(pylock, extras=["cli"], dependency_groups=["dev"]) == {
            "core",
            "mydev",
            "mytool",
            "subtool",
        }

    def test_package_reached_by_base_and_extra_is_unconditional(
        self, tmp_path: Path
    ) -> None:
        """An extra re-requiring a project dependency does not gate it."""
        root = self._ROOT.replace('cli = ["mytool"]', 'cli = ["mytool", "core"]')
        pylock = self._lock(tmp_path, extras=("cli",), root=root)

        assert self._selected(pylock) == {"core"}

    def test_default_group_still_installs_by_default(self, tmp_path: Path) -> None:
        """A ``default-groups`` member gates on the group but installs by default.

        PEP 751 seeds ``dependency_groups`` from ``default-groups`` when
        the installer is given no group selection, so the membership
        marker holds; an installer that explicitly selects no group
        (``dependency_groups=[]``) drops it.
        """
        root = self._ROOT + '[tool.nab]\ndefault-groups = ["dev"]\n'
        pylock = self._lock(tmp_path, root=root)

        assert pylock.default_groups == ("dev",)
        assert self._selected(pylock) == {"core", "mydev"}
        assert self._selected(pylock, dependency_groups=[]) == {"core"}

    def test_no_selection_leaves_every_package_unmarked(self, tmp_path: Path) -> None:
        pylock = self._lock(tmp_path)

        assert [pkg.marker for pkg in pylock.packages] == [None]
        assert self._selected(pylock) == {"core"}

    def test_marker_excluded_extra_requirement_is_not_locked(
        self, tmp_path: Path
    ) -> None:
        """A requirement the target Python excludes is in no install context."""
        root = self._ROOT.replace(
            'cli = ["mytool"]',
            'cli = ["mytool", "mydev ; python_version < \'3.9\'"]',
        )
        pylock = self._lock(tmp_path, extras=("cli",), root=root)

        assert set(self._markers(pylock)) == {"core", "mytool", "subtool"}

    def test_extra_requiring_an_extra_of_a_base_package(self, tmp_path: Path) -> None:
        """``cli = ["core[fancy]"]``: core stays unconditional, fancy's dep is gated."""
        root = self._ROOT.replace('cli = ["mytool"]', 'cli = ["core[fancy]"]')
        pylock = self._lock(
            tmp_path,
            extras=("cli",),
            root=root,
            members={
                "core": '[project]\nname = "core"\nversion = "1.0"\n'
                '[project.optional-dependencies]\nfancy = ["subtool"]\n',
                "subtool": TestExtraAndGroupMembershipMarkers._MEMBERS["subtool"],
                "mytool": TestExtraAndGroupMembershipMarkers._MEMBERS["mytool"],
                "mydev": TestExtraAndGroupMembershipMarkers._MEMBERS["mydev"],
            },
        )

        assert self._markers(pylock) == {"core": None, "subtool": '"cli" in extras'}

    def test_matrix_gates_the_extra_on_every_target(self, tmp_path: Path) -> None:
        """A matrix folds the extra into every target, and gates it there too.

        The gate is a property of the install context, not of the
        platform, so an extra every target reaches the same way carries
        the bare membership clause.
        """
        root = self._ROOT + (
            '[tool.nab]\nmode = "universal"\n'
            '[tool.nab.matrix]\npython = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        pylock = self._lock(tmp_path, extras=("cli",), root=root)

        assert self._markers(pylock) == {
            "core": None,
            "mytool": '"cli" in extras',
            "subtool": '"cli" in extras',
        }


class TestConflictMemberMembershipMarkers:
    """A conflict fork's own extra gates the packages only it reaches.

    End to end from ``pyproject.toml`` to the emitted lock, for
    ``nab lock --extra cpu --extra gpu --extra docs`` over an
    ``at-most-one`` cpu/gpu set.  ``shared-lib`` is a direct requirement
    of both ``cpu`` and ``docs``, so its ``packages.marker`` has to name
    both selections.
    """

    _NAMES: ClassVar[tuple[str, ...]] = (
        "core",
        "shared-lib",
        "cpu-only",
        "gpu-only",
        "sphinx",
    )

    _MEMBERS: ClassVar[dict[str, str]] = {
        name: f'[project]\nname = "{name}"\nversion = "1.0"\n' for name in _NAMES
    }

    _ROOT: ClassVar[str] = (
        '[project]\nname = "app"\nversion = "1.0"\ndependencies = ["core"]\n'
        "[project.optional-dependencies]\n"
        'cpu = ["shared-lib", "cpu-only"]\n'
        'gpu = ["gpu-only"]\n'
        'docs = ["shared-lib", "sphinx"]\n'
        "[tool.nab]\n"
        'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
        + "".join(
            f'[[tool.nab.local-sources]]\nname = "{name}"\npath = "{name}"\n'
            for name in _NAMES
        )
    )

    def _lock(self, tmp_path: Path) -> Pylock:
        return TestExtraAndGroupMembershipMarkers._lock(
            tmp_path,
            extras=("cpu", "gpu", "docs"),
            root=self._ROOT,
            members=self._MEMBERS,
        )

    def test_shared_package_names_both_selections_that_reach_it(
        self, tmp_path: Path
    ) -> None:
        markers = TestExtraAndGroupMembershipMarkers._markers(self._lock(tmp_path))

        assert markers["shared-lib"] == '"cpu" in extras or "docs" in extras'
        assert markers["sphinx"] == '"docs" in extras'
        assert markers["core"] is None

    def test_selecting_the_member_alone_installs_what_it_requires(
        self, tmp_path: Path
    ) -> None:
        """``shared-lib`` is a direct requirement of the ``cpu`` extra."""
        pylock = self._lock(tmp_path)
        selected = TestExtraAndGroupMembershipMarkers._selected

        assert selected(pylock, extras=["cpu"]) == {"core", "cpu-only", "shared-lib"}
        assert selected(pylock, extras=["gpu"]) == {"core", "gpu-only"}
        assert selected(pylock, extras=["docs"]) == {"core", "shared-lib", "sphinx"}
        assert selected(pylock) == {"core"}


class TestSidecarFetchFailure:
    """An advertised sidecar the index fails to serve fails the resolve."""

    @staticmethod
    def _wheel_only_listing(*versions: str) -> dict[str, object]:
        return {
            "meta": {"api-version": "1.0"},
            "name": "pkg",
            "files": [
                {
                    "filename": f"pkg-{v}-py3-none-any.whl",
                    "url": f"https://files.example.com/pkg-{v}-py3-none-any.whl",
                    "core-metadata": True,
                }
                for v in versions
            ],
        }

    @respx.mock
    def test_persistent_503_on_a_sidecar_does_not_pin_an_older_version(
        self, tmp_path: Path
    ) -> None:
        """A 503 that outlives the retries must not read as "no sidecar here".

        pkg 2.0 is wheel-only, so a sidecar recorded absent leaves the version
        with no metadata source: the resolver would drop 2.0 and quietly pin 1.0.
        """
        respx.get("https://pypi.org/simple/pkg/").mock(
            return_value=httpx.Response(
                200, json=self._wheel_only_listing("1.0", "2.0")
            )
        )
        respx.get("https://files.example.com/pkg-1.0-py3-none-any.whl.metadata").mock(
            return_value=httpx.Response(
                200, text="Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
            )
        )
        respx.get("https://files.example.com/pkg-2.0-py3-none-any.whl.metadata").mock(
            return_value=httpx.Response(503)
        )

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\nversion = "0"\ndependencies = ["pkg"]\n'
        )
        transport = HttpxAsyncTransport()
        try:
            with pytest.raises(HttpError, match="503"):
                _resolved(pyproject, transport, python_version="3.12.0")
        finally:
            asyncio.run(transport.aclose())


class _RecordingSink:
    """A :class:`~nab_python.resolve.ProgressSink` that records its calls."""

    def __init__(self) -> None:
        self.fetches = 0
        self.pins: list[int] = []

    def on_fetch(self) -> None:
        self.fetches += 1

    def on_pin(self, decided: int) -> None:
        self.pins.append(decided)


class TestProgressReporting:
    """The progress sink is threaded to the coordinator and the resolver."""

    @staticmethod
    def _serve(name: str, requires: str = "") -> None:
        """Serve ``name`` 1.0 as one wheel with a sidecar metadata file."""
        wheel = f"{name}-1.0-py3-none-any.whl"
        respx.get(f"https://pypi.org/simple/{name}/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "meta": {"api-version": "1.0"},
                    "name": name,
                    "files": [
                        {
                            "filename": wheel,
                            "url": f"https://files.example.com/{wheel}",
                            "core-metadata": True,
                        }
                    ],
                },
            )
        )

        respx.get(f"https://files.example.com/{wheel}.metadata").mock(
            return_value=httpx.Response(
                200,
                text=f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n{requires}",
            )
        )

    @respx.mock
    def test_sink_counts_one_fetch_per_listing_and_records_pins(
        self, tmp_path: Path
    ) -> None:
        """A real coordinator fires ``on_fetch`` once per listing it reads."""
        self._serve("foo", requires="Requires-Dist: bar\n")
        self._serve("bar")

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\nversion = "0"\ndependencies = ["foo"]\n',
            encoding="utf-8",
        )

        sink = _RecordingSink()
        transport = HttpxAsyncTransport()
        try:
            result = _resolved(
                pyproject, transport, python_version="3.12.0", progress=sink
            )
        finally:
            asyncio.run(transport.aclose())

        assert _pins(result) == {"foo": V("1.0"), "bar": V("1.0")}
        assert sink.fetches == 2

        # The root project is decided as well, so the pin gauge reads three.
        assert sink.pins[-1] == 3

    def test_observer_reports_decision_and_backjump_levels(self) -> None:
        sink = _RecordingSink()
        observer = _ResolveObserver(sink)
        observer.on_decision("foo", V("1.0"), 3)
        observer.on_backjump(3, 1)
        assert sink.pins == [3, 1]

    def test_observer_without_sink_only_logs(self) -> None:
        observer = _ResolveObserver(None)
        observer.on_decision("foo", V("1.0"), 3)
        observer.on_backjump(3, 1)
