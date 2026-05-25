"""Tests for the resolve_pyproject orchestration function."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.version import Version
from nab_python.config import (
    MatrixConfig,
    NabProjectConfig,
    ResolveMode,
)
from nab_python.provider import ResolutionStrategy, UnsupportedVcsError
from nab_python.resolve import (
    ResolutionResult,
    UnsupportedModeError,
    _augment_resolution_error,
    _build_constraints,
    _build_resolver_inputs,
    _check_group_disjointness,
    _check_group_disjointness_across_tuples,
    _find_group_conflicts,
    _load_extra_requirements,
    _load_group_requirements,
    _load_group_requirements_by_group,
    _resolve_target_python,
    _walk_no_versions_packages,
    resolve_pyproject,
    resolve_universal_pyproject,
)
from nab_python.universal.matrix import MatrixTuple
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
            patch("nab_python.resolve.build_lock_input_from_provider"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("2.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1

            result = resolve_pyproject(
                pyproject, _FAKE_TRANSPORT, python_version="3.12.0"
            )

        assert result.pins == {"foo": V("2.0")}

    def test_uses_current_python_version(self, tmp_path: Path) -> None:
        """When python_version is None, uses sys.version_info."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["bar"]\n',
        )

        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_lock_input_from_provider"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("1.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1

            resolve_pyproject(pyproject, _FAKE_TRANSPORT)

        call_kwargs = mock_provider_cls.call_args
        pv = call_kwargs.kwargs.get("python_version") or call_kwargs[1].get(
            "python_version"
        )
        assert pv is not None
        parts = pv.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

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
            patch("nab_python.resolve.build_lock_input_from_provider"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("1.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1

            resolve_pyproject(
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
            patch("nab_python.resolve.build_lock_input_from_provider"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_resolver = mock_resolver_cls.return_value
            mock_resolver.resolve.return_value = {
                "foo": V("1.0"),
                "bar": V("1.0"),
            }
            resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

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
            patch("nab_python.resolve.build_lock_input_from_provider"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = resolve_pyproject(
                pyproject, _FAKE_TRANSPORT, python_version="3.12.0"
            )

        assert result.pins == {}

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        call_kwargs = mock_resolver.resolve.call_args
        assert "constraints" in call_kwargs.kwargs
        constraints = call_kwargs.kwargs["constraints"]
        assert "bar" in constraints
        assert "skip" in constraints
        assert "custom" in constraints["skip"]
        assert V("1.0") not in constraints["skip"]

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        call_args = mock_resolver.resolve.call_args
        requirements = call_args.args[0]
        assert "requests" in requirements
        assert "requests[security]" in requirements
        # root_extras passed to provider
        provider_kwargs = mock_provider_cls.call_args.kwargs
        assert ("requests", "security") in provider_kwargs["root_extras"]

    @patch("nab_python.resolve.build_lock_input_from_provider")
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
            "[tool.nab.marker-environment]\n"
            'sys_platform = "linux"\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {"foo": V("1.0")}

        resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        requirements = mock_resolver_cls.return_value.resolve.call_args.args[0]
        assert "foo" in requirements
        assert "windows-only" not in requirements

    @patch("nab_python.resolve.build_lock_input_from_provider")
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
            "[tool.nab.marker-environment]\n"
            'sys_platform = "linux"\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {
            "foo": V("1.0"),
            "linux-only": V("1.0"),
        }

        resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        requirements = mock_resolver_cls.return_value.resolve.call_args.args[0]
        assert "linux-only" in requirements

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.10.0")

        requirements = mock_resolver_cls.return_value.resolve.call_args.args[0]
        assert "newer" not in requirements

    @patch("nab_python.resolve.build_lock_input_from_provider")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_root_marker_with_unparseable_python_version(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Garbled ``python_version`` arg falls back to default environment."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {"foo": V("1.0")}

        resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="not-a-version")

        requirements = mock_resolver_cls.return_value.resolve.call_args.args[0]
        assert "foo" in requirements

    @patch("nab_python.resolve.build_lock_input_from_provider")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_config_requires_python_used_when_set(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """[tool.nab].requires-python derives the resolve target Python.

        ``==3.10.5`` is a single-version specifier, so the resolve
        target is exactly 3.10.5; ``>=3.10`` would resolve to 3.10.0
        (the lowest enumerated candidate the specifier admits).
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab]\n"
            'requires-python = "==3.10.5"\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        resolve_pyproject(pyproject, _FAKE_TRANSPORT)

        assert mock_provider_cls.call_args.kwargs["python_version"] == "3.10.5"

    @patch("nab_python.resolve.build_lock_input_from_provider")
    @patch("nab_python.resolve.Resolver")
    @patch("nab_python.resolve.Provider")
    @patch("nab_python.resolve.FetchCoordinator")
    def test_cli_python_overrides_config(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """An explicit python_version arg wins over the config value."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab]\n"
            'requires-python = "==3.10.5"\n',
        )
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.4")

        assert mock_provider_cls.call_args.kwargs["python_version"] == "3.12.4"

    def test_universal_mode_rejected(self, tmp_path: Path) -> None:
        """resolve_pyproject refuses to handle mode = 'universal'."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\ndependencies = ["foo"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n',
        )

        with pytest.raises(UnsupportedModeError, match="resolve_universal"):
            resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

    @patch("nab_python.resolve.build_lock_input_from_provider")
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
        resolve_pyproject(
            pyproject,
            _FAKE_TRANSPORT,
            config=explicit,
            python_version="3.12.0",
        )

        forwarded = mock_resolver_cls.return_value.resolve.call_args.kwargs[
            "constraints"
        ]
        assert "urllib3" in forwarded

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        kwargs = mock_provider_cls.call_args.kwargs
        assert kwargs["resolution_strategy"] is ResolutionStrategy.LOWEST_DIRECT
        # The direct set holds the canonical names of the project's own deps.
        assert kwargs["direct_packages"] == frozenset({"foo"})

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        resolve_pyproject(
            pyproject,
            _FAKE_TRANSPORT,
            python_version="3.12.0",
            resolution_strategy=ResolutionStrategy.HIGHEST,
        )

        kwargs = mock_provider_cls.call_args.kwargs
        assert kwargs["resolution_strategy"] is ResolutionStrategy.HIGHEST

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        kwargs = mock_provider_cls.call_args.kwargs
        assert kwargs["resolution_strategy"] is ResolutionStrategy.HIGHEST

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        kwargs = mock_provider_cls.call_args.kwargs
        # Only the base canonical names; the extras-proxy key
        # ("requests[security]") must not be in the direct set because
        # the strategy decision is keyed on the underlying package.
        assert kwargs["direct_packages"] == frozenset({"requests", "foo"})

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        resolve_pyproject(
            pyproject, _FAKE_TRANSPORT, python_version="3.12.0", groups=("test",)
        )

        kwargs = mock_build_lock.call_args.kwargs
        assert kwargs["dependency_groups"] == ("test",)
        assert kwargs["default_groups"] == ("dev",)

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        resolve_pyproject(
            pyproject, _FAKE_TRANSPORT, python_version="3.12.0", groups=("test",)
        )

        assert mock_build_lock.call_args.kwargs["default_groups"] == ()


class TestResolveUniversalPyproject:
    @patch("nab_python.resolve.resolve_universal")
    def test_dispatches_to_universal_resolver(
        self,
        mock_resolve_universal: MagicMock,
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
        sentinel = MagicMock()
        mock_resolve_universal.return_value = sentinel

        result = resolve_universal_pyproject(pyproject)

        assert result is sentinel
        kwargs = mock_resolve_universal.call_args.kwargs
        assert kwargs["matrix"].python == ">=3.11,<3.13"
        assert kwargs["matrix"].platforms == ("linux_x86_64", "macos_arm64")
        assert kwargs["requirements"] == ["foo"]

    @patch("nab_python.resolve.resolve_universal")
    def test_passes_python_patches_when_set(
        self,
        mock_resolve_universal: MagicMock,
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
        resolve_universal_pyproject(pyproject)
        kwargs = mock_resolve_universal.call_args.kwargs
        assert kwargs["matrix"].python_patches == {"3.11": "3.11.4"}

    @patch("nab_python.resolve.resolve_universal")
    def test_explicit_config_arg(
        self,
        mock_resolve_universal: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Caller can pass a constructed config rather than reading the file."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')
        config = NabProjectConfig(
            mode=ResolveMode.UNIVERSAL,
            matrix=MatrixConfig(python=">=3.12,<3.14", platforms=("linux_x86_64",)),
        )
        resolve_universal_pyproject(pyproject, config=config)
        kwargs = mock_resolve_universal.call_args.kwargs
        assert kwargs["matrix"].python == ">=3.12,<3.14"

    def test_specific_mode_rejected(self, tmp_path: Path) -> None:
        """Calling with mode != universal raises UnsupportedModeError."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')
        with pytest.raises(UnsupportedModeError, match="requires mode = 'universal'"):
            resolve_universal_pyproject(pyproject)


class TestResolvePyprojectVcs:
    """VCS direct-URL requirements get admission-checked before drop."""

    def test_block_default_refuses_vcs_dependency(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            f"[project]\ndependencies = "
            f'["foo @ git+https://github.com/foo/bar.git@{_FORTY}"]\n',
        )

        with pytest.raises(UnsupportedVcsError, match="VcsPolicy is BLOCK"):
            resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

    def test_block_default_refuses_vcs_constraint(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\ndependencies = []\n"
            "[tool.nab]\nconstraints = "
            f'["foo @ git+https://github.com/foo/bar.git@{_FORTY}"]\n',
        )

        with pytest.raises(UnsupportedVcsError, match="VcsPolicy is BLOCK"):
            resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

    def test_admitted_vcs_dependency_raises_not_implemented(
        self, tmp_path: Path
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            f"[project]\ndependencies = "
            f'["foo @ git+https://github.com/foo/bar.git@{_FORTY}"]\n'
            "[tool.nab.vcs]\n"
            'policy = "allow"\n'
            'allowed-schemes = ["git+https"]\n',
        )

        with pytest.raises(NotImplementedError, match="not implemented"):
            resolve_pyproject(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
            )

    def test_admitted_vcs_constraint_raises_not_implemented(
        self, tmp_path: Path
    ) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\ndependencies = []\n"
            "[tool.nab]\nconstraints = "
            f'["foo @ git+https://github.com/foo/bar.git@{_FORTY}"]\n'
            "[tool.nab.vcs]\n"
            'policy = "allow"\n'
            'allowed-schemes = ["git+https"]\n',
        )

        with pytest.raises(NotImplementedError, match="not implemented"):
            resolve_pyproject(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
            )


class TestResolvePyprojectLockShape:
    """Lock-input plumbing: the result always carries a LockInput."""

    def test_returns_resolution_result(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo>=1.0"]\n')

        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider") as mock_provider_cls,
            patch("nab_python.resolve.build_lock_input_from_provider") as mock_build,
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("2.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            lock_sentinel = MagicMock(name="LockInput")
            mock_build.return_value = lock_sentinel

            result = resolve_pyproject(
                pyproject, _FAKE_TRANSPORT, python_version="3.12.0"
            )

        assert isinstance(result, ResolutionResult)
        assert result.lock_input is lock_sentinel
        assert "foo" in result.pins

    def test_default_indexes_pypi_when_no_indexes(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')
        with (
            patch("nab_python.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_python.resolve.Provider"),
            patch("nab_python.resolve.build_lock_input_from_provider") as mock_build,
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")
        passed = mock_build.call_args.kwargs["indexes"]
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
            patch("nab_python.resolve.build_lock_input_from_provider") as mock_build,
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")
        passed = mock_build.call_args.kwargs["indexes"]
        assert tuple(ix.url for ix in passed) == ("https://custom.index/simple/",)


class TestResolveTargetPython:
    """``_resolve_target_python`` derives a concrete Python version."""

    def test_exact_specifier(self) -> None:
        """``==3.10.5`` resolves to exactly 3.10.5."""
        assert _resolve_target_python("==3.10.5") == "3.10.5"

    def test_lower_bound_only(self) -> None:
        """``>=3.13`` resolves to 3.13.0 (the lowest enumerated match)."""
        assert _resolve_target_python(">=3.13") == "3.13.0"

    def test_range(self) -> None:
        """``>=3.13,<3.14`` resolves to 3.13.0."""
        assert _resolve_target_python(">=3.13,<3.14") == "3.13.0"

    def test_excluded_minor(self) -> None:
        """``>=3.10,!=3.12,<3.15`` skips the excluded minor."""
        # 3.10.0 is the lowest enumerated match; the !=3.12 hole only
        # matters when the lower bound forces the search past 3.12.
        assert _resolve_target_python(">=3.10,!=3.12,<3.15") == "3.10.0"

    def test_unbounded_below_falls_back_to_host(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Open-ended ``<X.Y`` falls back to the host Python with a warning."""
        with caplog.at_level("WARNING", logger="nab_python.resolve"):
            target = _resolve_target_python("<3.14")
        # ``<3.14`` admits ``3.0.0`` (the lowest enumerated candidate),
        # so the helper returns 3.0.0; fallback only fires when the
        # specifier admits *nothing* in the candidate grid.  We verify
        # that with a separate impossible-range case below.
        assert target == "3.0.0"
        assert not caplog.records

    def test_no_match_falls_back_to_host(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A specifier that admits nothing in the grid logs and falls back."""
        with caplog.at_level("WARNING", logger="nab_python.resolve"):
            target = _resolve_target_python(">=99.0")
        # The candidate grid stops at major 4, so ``>=99.0`` admits
        # nothing and the helper falls back to the host (whatever the
        # test runner is using).  We check the warning was emitted.
        assert target  # non-empty
        assert any(
            "matches no enumerated CPython release" in rec.message
            for rec in caplog.records
        )


class TestLoadGroupRequirements:
    """``_load_group_requirements`` reads the ``[dependency-groups]``
    table.  Empty selection short-circuits; missing table raises so
    a typo in ``--group`` does not silently expand to nothing."""

    def test_empty_selection_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'x'\n")
        assert _load_group_requirements(path, []) == []

    def test_missing_table_raises_with_selected_names(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'x'\n")
        with pytest.raises(LookupError, match=r"\[dependency-groups\] is missing"):
            _load_group_requirements(path, ["dev"])

    def test_returns_requirements_for_selected_groups(self, tmp_path: Path) -> None:
        """Selected groups expand into ``Requirement`` instances."""
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'x'\n[dependency-groups]\ndev = ['pytest>=7']\n"
        )
        reqs = _load_group_requirements(path, ["dev"])
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
        assert _load_extra_requirements(path, []) == []

    def test_missing_table_raises_with_selected_names(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'x'\n")
        with pytest.raises(
            LookupError,
            match=r"\[project.optional-dependencies\] is",
        ):
            _load_extra_requirements(path, ["test"])

    def test_returns_requirements_for_selected_extras(self, tmp_path: Path) -> None:
        """Selected extras expand into ``Requirement`` instances, with
        self-references walked transitively to their underlying deps.
        """
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'x'\n"
            "[project.optional-dependencies]\n"
            "test = ['pytest>=7']\n"
            "all = ['x[test]']\n"
        )
        reqs = _load_extra_requirements(path, ["all"])
        names = sorted(r.name for r in reqs)
        # Self-reference walks to ``pytest`` while keeping the
        # original ``x[test]`` placeholder so the resolver still sees
        # the project's own extras-proxy.
        assert "pytest" in names


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


class TestBuildConstraints:
    """``_build_constraints`` folds duplicate constraint lines."""

    def test_duplicate_constraint_intersects(self) -> None:
        """Two constraint lines for one package combine to their overlap."""
        out = _build_constraints(NabProjectConfig(constraints=("foo>=2.0", "foo<3.0")))
        assert V("2.5") in out["foo"]
        assert V("1.0") not in out["foo"]
        assert V("5.0") not in out["foo"]

    def test_conflicting_constraints_raise(self) -> None:
        """Pinned-but-different constraint lines for one package raise."""
        with pytest.raises(ResolutionError, match="conflicting constraints"):
            _build_constraints(NabProjectConfig(constraints=("foo==1.0", "foo==2.0")))


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
            resolve_pyproject(
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
        assert _load_group_requirements_by_group(path, []) == {}

    def test_missing_table_raises_with_selected_names(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'x'\n")
        with pytest.raises(LookupError, match=r"\[dependency-groups\] is missing"):
            _load_group_requirements_by_group(path, ["dev"])

    def test_maps_each_group_to_its_requirements(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'x'\n"
            "[dependency-groups]\n"
            "dev = ['pytest>=7']\n"
            "docs = ['sphinx<7', 'furo']\n"
        )
        per_group = _load_group_requirements_by_group(path, ["dev", "docs"])
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
            _check_group_disjointness(per_group, environment={})
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
            _check_group_disjointness(per_group, environment={})
        message = str(info.value)
        assert message.index("'docs'") < message.index("'test'")

    def test_no_conflict_is_silent(self) -> None:
        per_group = {
            "docs": [Requirement("sphinx>=6,<8")],
            "test": [Requirement("sphinx>=7")],
        }
        _check_group_disjointness(per_group, environment={})

    def test_disjoint_packages_do_not_conflict(self) -> None:
        """Groups touching different packages never conflict."""
        per_group = {
            "docs": [Requirement("sphinx<7")],
            "test": [Requirement("pytest>=8")],
        }
        _check_group_disjointness(per_group, environment={})

    def test_single_group_is_noop(self) -> None:
        per_group = {"docs": [Requirement("sphinx<7"), Requirement("sphinx>=7")]}
        _check_group_disjointness(per_group, environment={})

    def test_empty_mapping_is_noop(self) -> None:
        _check_group_disjointness({}, environment={})

    def test_marker_filtered_requirement_is_skipped(self) -> None:
        """A requirement whose marker is False under the env is ignored."""
        per_group = {
            "docs": [Requirement("sphinx<7")],
            "test": [Requirement("sphinx>=7 ; python_version < '3'")],
        }
        _check_group_disjointness(per_group, environment={"python_version": "3.12"})

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
            _check_group_disjointness(per_group, environment={})
        assert "sphinx" in str(info.value)

    def test_three_groups_names_the_conflicting_pair(self) -> None:
        """With three groups, only the conflicting pair is named."""
        per_group = {
            "docs": [Requirement("sphinx<7")],
            "lint": [Requirement("ruff>=0.5")],
            "test": [Requirement("sphinx>=7")],
        }
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, environment={})
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
        _check_group_disjointness(per_group, environment={})

    def test_extras_only_requirement_does_not_conflict(self) -> None:
        """An extras-only requirement carries a full range, so no conflict."""
        per_group = {
            "docs": [Requirement("sphinx[docs]")],
            "test": [Requirement("sphinx>=7")],
        }
        _check_group_disjointness(per_group, environment={})


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
            resolve_pyproject(
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

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        result = resolve_pyproject(
            pyproject,
            _FAKE_TRANSPORT,
            python_version="3.12.0",
            groups=["docs"],
        )
        assert "foo" in result.pins

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        result = resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")
        assert "foo" in result.pins

    @patch("nab_python.resolve.build_lock_input_from_provider")
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

        result = resolve_pyproject(
            pyproject,
            _FAKE_TRANSPORT,
            python_version="3.12.0",
            groups=["docs", "test"],
        )
        assert "foo" in result.pins


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
                resolve_pyproject(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")
        assert "Diagnostics:" in str(info.value)
        assert "foo: package not found on any configured index" in str(info.value)


def _tuple_for_python(python_version: str) -> MatrixTuple:
    """Build a linux_x86_64 tuple for ``python_version``.

    Only the marker environment matters for the group pre-pass, so the
    platform axis is held constant and the python axis varies; the
    label encodes the python version so a conflict message can be
    asserted against it.
    """
    return MatrixTuple(
        python_version=python_version,
        platform_id="linux_x86_64",
        environment={
            "python_version": python_version,
            "python_full_version": f"{python_version}.0",
            "implementation_name": "cpython",
            "implementation_version": f"{python_version}.0",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_system": "Linux",
            "platform_version": "",
            "sys_platform": "linux",
        },
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
            _check_group_disjointness_across_tuples(per_group, tuples)
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
            _check_group_disjointness_across_tuples(per_group, tuples)
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
        _check_group_disjointness_across_tuples(per_group, tuples)

    def test_single_group_is_noop(self) -> None:
        per_group = {"a": [Requirement("foo<2"), Requirement("foo>=2")]}
        tuples = [_tuple_for_python("3.10"), _tuple_for_python("3.12")]
        _check_group_disjointness_across_tuples(per_group, tuples)

    def test_empty_mapping_is_noop(self) -> None:
        tuples = [_tuple_for_python("3.12")]
        _check_group_disjointness_across_tuples({}, tuples)

    def test_three_groups_names_only_conflicting_pair(self) -> None:
        """With three groups, the message names the conflicting pair only."""
        per_group = {
            "a": [Requirement("foo<2")],
            "b": [Requirement("foo>=2")],
            "c": [Requirement("bar>=1")],
        }
        tuples = [_tuple_for_python("3.12")]
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness_across_tuples(per_group, tuples)
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
            _check_group_disjointness_across_tuples(per_group, tuples)
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
            _check_group_disjointness_across_tuples(per_group, tuples)
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
            patch("nab_python.resolve.resolve_universal") as mock_universal,
            pytest.raises(ResolutionError) as info,
        ):
            resolve_universal_pyproject(pyproject, groups=["a", "b"])
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
            patch("nab_python.resolve.resolve_universal") as mock_universal,
            pytest.raises(ResolutionError) as info,
        ):
            resolve_universal_pyproject(pyproject, groups=["a", "b"])
        mock_universal.assert_not_called()
        message = str(info.value)
        assert "py311-linux_x86_64" in message
        assert "py312-linux_x86_64" in message

    @patch("nab_python.resolve.resolve_universal")
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
        result = resolve_universal_pyproject(pyproject, groups=["a", "b"])
        assert result is sentinel

    @patch("nab_python.resolve.resolve_universal")
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
        result = resolve_universal_pyproject(pyproject, groups=["a"])
        assert result is sentinel

    @patch("nab_python.resolve.resolve_universal")
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
        result = resolve_universal_pyproject(pyproject)
        assert result is sentinel
