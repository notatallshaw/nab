"""Tests for the resolve_pyproject orchestration function."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, NoReturn
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from nab_index.client import SdistFile, WheelFile
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.transport import AsyncHttpTransport, HttpError
from nab_project._resolve.engine import (
    _augment_resolution_error,
    _raise_for_source_python,
    _ResolveObserver,
    _walk_no_versions_packages,
)
from nab_project._testing.coordinator_fake import FakeFetchPort, make_coordinator
from nab_project.conflicts import (
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSelectionError,
    ConflictSet,
)
from nab_project.fetch import DEFAULT_MAX_CONCURRENCY
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import LockInput, PinShape, build_pylock
from nab_project.pyproject_files import (
    read_pyproject_groups,
    read_pyproject_name,
    read_pyproject_optional_dependencies,
)
from nab_project.resolve import (
    ResolveFork,
    ResolveResult,
    _check_group_disjointness,
    _extra_requirements,
    _find_group_conflicts,
    _group_requirements,
    _group_requirements_by_group,
    _ProjectTables,
    build_lock_input,
    build_resolver_inputs,
    inputs_for_build_requirements,
    resolve_for_targets,
)
from nab_provider._provider import listing_diagnosis
from nab_provider._vendor.packaging.markers import Marker, default_environment
from nab_provider._vendor.packaging.pylock import Pylock
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.diagnostics import Diagnostic
from nab_provider.errors import ConfigError
from nab_provider.marker_holds import dependency_marker_holds
from nab_provider.metadata import WheelMetadata
from nab_provider.overrides import IndexOverride, PackageOverride
from nab_provider.provider import (
    BuildPolicy,
    DistPolicy,
    LocalSource,
    Provider,
    ResolutionStrategy,
    UnsupportedVcsError,
    VcsConfig,
)
from nab_provider.records import IndexConfig
from nab_provider.requirements_file import (
    InvalidProjectRequirementError,
    expand_extra_requirements,
)
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix, ResolveTarget
from nab_provider.vcs_admission import VcsPolicy
from nab_resolver.errors import ResolutionError
from nab_resolver.ranges import Range
from nab_resolver.types import Incompatibility, IncompatibilityCause, Term

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

V = Version

# Specced so aclose() is awaitable: the coordinator awaits it on shutdown.
_FAKE_TRANSPORT = MagicMock(spec=AsyncHttpTransport, name="FakeTransport")

_FORTY = "0123456789abcdef0123456789abcdef01234567"

# The one repo prefix and scheme the VCS tests admit.
_GITHUB_HTTPS = VcsConfig(
    policy=VcsPolicy.ALLOW,
    allowed_schemes=frozenset({"git+https"}),
    allowed_repos=("https://github.com/",),
)


def _resolved(
    path: Path,
    transport: object = _FAKE_TRANSPORT,
    *,
    python_version: str | None = None,
    **kwargs: object,
) -> ResolveResult:
    """Resolve and surface a failed target's error.

    nab-project takes its targets and its settings from the caller, so a
    test that names neither resolves the running interpreter under a
    project that configures nothing.  ``python_version`` moves that one
    target the way ``--python`` does, and has nothing to move once the
    caller passed targets of its own.

    The engine records a target that did not resolve rather than raising,
    so a caller resolving for one environment re-raises it; that is what
    every test below asserts against.
    """
    if "targets" not in kwargs:
        kwargs["targets"] = (
            ResolveTarget.for_host()
            if python_version is None
            else ResolveTarget.for_host_python(python_version),
        )
    elif python_version is not None:
        msg = "python_version retargets a target the caller chose; both were passed"
        raise TypeError(msg)

    result = resolve_for_targets(path, transport, **kwargs)  # type: ignore[arg-type]
    result.raise_for_failure()
    return result


def _extras_conflict(
    *names: str, policy: ConflictPolicy = ConflictPolicy.AT_MOST_ONE
) -> ConflictSet:
    """A conflict set over ``names`` as extras, under ``policy``."""
    return ConflictSet(
        tuple(ConflictMember(ConflictKind.EXTRA, name) for name in names), policy
    )


def _groups_conflict(
    *names: str, policy: ConflictPolicy = ConflictPolicy.AT_MOST_ONE
) -> ConflictSet:
    """A conflict set over ``names`` as dependency groups, under ``policy``."""
    return ConflictSet(
        tuple(ConflictMember(ConflictKind.GROUP, name) for name in names), policy
    )


def _index_route(name: str, index: str) -> PackageOverride:
    """A ``[tool.nab.packages.<name>] index`` entry, over the full range."""
    requirement = Requirement(name)
    return PackageOverride(
        requirement=requirement,
        name=name,
        version_range=requirement.specifier.to_range(),
        index=index,
        name_keyed=True,
    )


def _target(environment: dict[str, str]) -> ResolveTarget:
    """A host target whose marker environment is ``environment``.

    The group-disjointness check reads only ``marker_env``, so an overlay
    onto the host names the environment a group's markers evaluate under.
    """
    return ResolveTarget.for_host().with_marker_overrides(environment)


def _pins(result: ResolveResult) -> dict[str, Version]:
    """The pins of a single-environment resolve."""
    return result.target_results[0].pins


def _root_ranges(mock_resolver: MagicMock) -> dict[str, VersionRange]:
    """The root requirements the resolver was called with, folded per package."""
    folded: dict[str, VersionRange] = {}
    for root in mock_resolver.resolve.call_args.args[0]:
        previous = folded.get(root.package, VersionRange.full())
        folded[root.package] = previous & root.constraint
    return folded


def _scanned_groups(mock_check: MagicMock) -> list[tuple[str, ...]]:
    """The groups each ``_check_group_disjointness`` call scanned, in call order."""
    return [tuple(call.args[0]) for call in mock_check.call_args_list]


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
    inputs: ResolveInputs, *, environment: dict[str, str]
) -> dict[str, Range]:
    """The resolver-input ranges ``inputs``'s constraints fold to.

    The parser is shared with the requirement side; ``kind`` is what
    tells the two apart.
    """
    return build_resolver_inputs(
        [Requirement(text) for text in inputs.constraints],
        inputs.vcs,
        environment=environment,
        marker_holds=dependency_marker_holds,
        kind="constraint",
    ).ranges


def _constraints_pyproject(tmp_path: Path, entries: str) -> Path:
    """A minimal project whose ``[tool.nab].constraints`` holds ``entries``."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
        f"[tool.nab]\nconstraints = [{entries}]\n"
    )
    return pyproject


def _malformed_group_pyproject(tmp_path: Path) -> Path:
    """A project with conflicting ``cpu`` and ``gpu`` extras, plus a
    ``docs`` group whose value is an integer instead of a list."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\nversion = "0"\n'
        "dependencies = []\n"
        "[project.optional-dependencies]\n"
        'cpu = ["foo==1.0"]\n'
        'gpu = ["foo==2.0"]\n'
        "[dependency-groups]\n"
        "docs = 5\n"
        "[tool.nab]\n"
        'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
    )
    return pyproject


class TestSpecificModeConflictValidation:
    """Conflict handling in specific mode: direct co-selection forks, an
    umbrella that reaches two members without selecting either fails fast."""

    @patch("nab_project.resolve.resolve_with_coordinator")
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
            inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
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
                extras=("cpu",),
                python_version="3.12.0",
                inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
            )
        # The selected extra's pin reached the Provider as a root
        # requirement; the unselected extra's contradictory pin did not.
        root_reqs = mock_provider_cls.call_args.kwargs["root_requirements"]
        assert "foo" in root_reqs
        assert V("1.0") in root_reqs["foo"]
        assert V("2.0") not in root_reqs["foo"]
        assert _pins(result) == {"foo": V("1.0")}

    def test_unselected_malformed_group_still_resolves(self, tmp_path: Path) -> None:
        """Conflict planning closes the group table over ``include-group``,
        so it walks groups the resolve never selects."""
        pyproject = _malformed_group_pyproject(tmp_path)
        with (
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
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
                extras=("cpu",),
                python_version="3.12.0",
                inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
            )
        assert _pins(result) == {"foo": V("1.0")}

    def test_selected_malformed_group_reports_loader_error(
        self, tmp_path: Path
    ) -> None:
        """Selecting the malformed group still fails, with the group
        loader's message rather than a crash in the include walk."""
        pyproject = _malformed_group_pyproject(tmp_path)
        with pytest.raises(InvalidProjectRequirementError, match="not a sequence type"):
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                groups=("docs",),
                python_version="3.12.0",
                inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
            )

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
        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(
                path,
                _FAKE_TRANSPORT,
                extras=("cpu", "gpu"),
                python_version="3.12.0",
                inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
            )
        label_for = {v: label for v, label in result.merged_pins()["numpy"]}
        assert set(label_for) == {"2.0.0", "2.1.2"}
        assert label_for["2.1.2"].endswith("-extra-cpu")
        assert label_for["2.0.0"].endswith("-extra-gpu")
        # Both forks land in the lock under their own selection.
        lock_input = build_lock_input(
            result,
            inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
            extras=("cpu", "gpu"),
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
                inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpuu"),)),
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
                inputs=ResolveInputs(conflicts=(_groups_conflict("a", "missing"),)),
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
                inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
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
                inputs=ResolveInputs(conflicts=(_groups_conflict("b22", "b23"),)),
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
                inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
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
                inputs=ResolveInputs(conflicts=(_groups_conflict("b22", "b23"),)),
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
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
                inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
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
                inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
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
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
                inputs=ResolveInputs(
                    conflicts=(
                        _extras_conflict(
                            "cpu", "gpu", policy=ConflictPolicy.EXACTLY_ONE
                        ),
                    )
                ),
            )

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
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
                inputs=ResolveInputs(
                    conflicts=(
                        _extras_conflict(
                            "cpu", "gpu", policy=ConflictPolicy.AT_LEAST_ONE
                        ),
                    )
                ),
            )

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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
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
                extras=("cpu",),
                python_version="3.12.0",
                inputs=ResolveInputs(
                    conflicts=(
                        _extras_conflict(
                            "cpu", "gpu", policy=ConflictPolicy.EXACTLY_ONE
                        ),
                    )
                ),
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
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
                python_version="3.12.0",
                inputs=ResolveInputs(
                    conflicts=(
                        _groups_conflict("a", "b", policy=ConflictPolicy.EXACTLY_ONE),
                    ),
                    default_groups=("a",),
                ),
            )
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("2.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
                inputs=ResolveInputs(default_groups=("dev",)),
            )
        root_reqs = mock_provider_cls.call_args.kwargs["root_requirements"]
        assert "bar" in root_reqs

    @patch("nab_project.resolve.resolve_with_coordinator")
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
            inputs=ResolveInputs(
                conflicts=(_groups_conflict("a", "b"),), default_groups=("a",)
            ),
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
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
                inputs=ResolveInputs(
                    indexes=(IndexConfig("private", "https://custom.index/simple/"),)
                ),
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
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
                inputs=ResolveInputs(
                    indexes=(
                        IndexConfig("pypi", "https://pypi.org/simple/"),
                        IndexConfig("private", "https://private.example.com/simple/"),
                    ),
                    package_overrides=(_index_route("baz", "private"),),
                ),
            )

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
            patch("nab_project._resolve.engine.Resolver") as mock_resolver_cls,
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider"),
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_resolver = mock_resolver_cls.return_value
            mock_resolver.resolve.return_value = {
                "foo": V("1.0"),
                "bar": V("1.0"),
            }
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        forwarded = _root_ranges(mock_resolver)
        assert "foo" in forwarded
        assert "bar" in forwarded
        assert "custom" in forwarded["foo"]
        assert V("1.0") not in forwarded["foo"]

    def test_empty_dependencies(self, tmp_path: Path) -> None:
        """Project with no dependencies resolves to empty dict."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\ndependencies = []\n")

        with (
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider"),
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        assert _pins(result) == {}

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            python_version="3.12.0",
            inputs=ResolveInputs(constraints=("bar<2.0", "skip===custom")),
        )

        call_kwargs = mock_resolver.resolve.call_args
        assert "constraints" in call_kwargs.kwargs
        constraints = call_kwargs.kwargs["constraints"]
        assert "bar" in constraints
        assert "skip" in constraints
        assert "custom" in constraints["skip"]
        assert V("1.0") not in constraints["skip"]

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        requirements = _root_ranges(mock_resolver)
        assert "requests" in requirements
        assert "requests[security]" in requirements
        # root_extras passed to provider
        provider_kwargs = mock_provider_cls.call_args.kwargs
        assert ("requests", "security") in provider_kwargs["root_extras"]

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            targets=(
                ResolveTarget.for_declared(
                    python_version="3.12",
                    spec=PlatformSpec("linux_x86_64"),
                    python_full_version="3.12.0",
                ),
            ),
        )

        requirements = _root_ranges(mock_resolver_cls.return_value)
        assert "foo" in requirements
        assert "windows-only" not in requirements

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            python_version="3.8",
        )

        requirements = _root_ranges(mock_resolver_cls.return_value)
        assert "foo" in requirements
        assert "legacy" in requirements

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            targets=(
                ResolveTarget.for_declared(
                    python_version="3.12",
                    spec=PlatformSpec("linux_x86_64"),
                    python_full_version="3.12.0",
                ),
            ),
        )

        requirements = _root_ranges(mock_resolver_cls.return_value)
        assert "linux-only" in requirements

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        requirements = _root_ranges(mock_resolver_cls.return_value)
        assert "newer" not in requirements

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            python_version="3.12.4",
            inputs=ResolveInputs(requires_python=">=3.10"),
        )

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target.python_full_version == "3.12.4"

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
    def test_the_constraints_reach_the_resolver(
        self,
        mock_coord_cls: MagicMock,
        mock_provider_cls: MagicMock,
        mock_resolver_cls: MagicMock,
        mock_build_lock: MagicMock,
        tmp_path: Path,
    ) -> None:
        """``inputs`` carries the constraints the search runs under."""
        pyproject = tmp_path / "pyproject.toml"
        # No [tool.nab] on disk, so the constraint can only be the caller's.
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')
        mock_coord_cls.return_value.__enter__ = lambda s: s
        mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolver_cls.return_value.resolve.return_value = {}

        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            inputs=ResolveInputs(constraints=("urllib3<2",)),
            python_version="3.12.0",
        )

        forwarded = mock_resolver_cls.return_value.resolve.call_args.kwargs[
            "constraints"
        ]
        assert "urllib3" in forwarded

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            python_version="3.12.0",
            inputs=ResolveInputs(resolution=ResolutionStrategy.LOWEST_DIRECT),
        )

        kwargs = mock_provider_cls.call_args.kwargs
        assert kwargs["resolution_strategy"] is ResolutionStrategy.LOWEST_DIRECT
        # The direct set holds the canonical names of the project's own deps.
        assert kwargs["direct_packages"] == frozenset({"foo"})

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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
            inputs=ResolveInputs(resolution=ResolutionStrategy.LOWEST),
        )

        kwargs = mock_provider_cls.call_args.kwargs
        assert kwargs["resolution_strategy"] is ResolutionStrategy.HIGHEST

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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
            pyproject,
            _FAKE_TRANSPORT,
            python_version="3.12.0",
            groups=("test",),
            inputs=ResolveInputs(default_groups=("dev",)),
        )

        lock_input = build_lock_input(
            result,
            inputs=ResolveInputs(default_groups=("dev",)),
            dependency_groups=("test",),
        )
        assert lock_input.dependency_groups == ("test",)
        assert lock_input.default_groups == ("dev",)

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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
            inputs=ResolveInputs(),
            dependency_groups=("test",),
        )
        assert lock_input.default_groups == ()


class TestResolveUniversalPyproject:
    @patch("nab_project.resolve.resolve_with_coordinator")
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
        result = _resolved(
            pyproject,
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            targets=Matrix(
                python=">=3.11,<3.13",
                platforms=(
                    PlatformSpec("linux_x86_64"),
                    PlatformSpec("macos_arm64"),
                ),
            ).expand(),
        )

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

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            targets=Matrix(
                python=">=3.11,<3.13",
                platforms=(PlatformSpec("linux_x86_64"),),
                python_patches={"3.11": "3.11.4"},
            ).expand(),
        )
        targets = mock_engine.call_args.args[1]
        assert [t.python_full_version for t in targets] == ["3.11.4", "3.12.0"]

    @patch("nab_project.resolve.resolve_with_coordinator")
    def test_explicit_targets_reach_the_engine(
        self,
        mock_engine: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The caller says which environments to resolve for, not the file."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo"]\n')
        declared = Matrix(
            python=">=3.12,<3.14", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()

        _resolved(pyproject, targets=declared, inputs=ResolveInputs())

        targets = mock_engine.call_args.args[1]
        assert [t.label for t in targets] == [
            "py312-linux_x86_64",
            "py313-linux_x86_64",
        ]

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            extras=["cpu", "gpu"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_extras_conflict("cpu", "gpu"),),
            ),
            targets=Matrix(
                python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
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

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            extras=["cpu", "gpu", "docs"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_extras_conflict("cpu", "gpu"),),
            ),
            targets=Matrix(
                python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="exactly one"),
        ):
            _resolved(
                pyproject,
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(
                        _extras_conflict(
                            "cpu", "gpu", policy=ConflictPolicy.EXACTLY_ONE
                        ),
                    ),
                ),
                targets=Matrix(
                    python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="at least one"),
        ):
            _resolved(
                pyproject,
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(
                        _extras_conflict(
                            "cpu", "gpu", policy=ConflictPolicy.AT_LEAST_ONE
                        ),
                    ),
                ),
                targets=Matrix(
                    python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
        mock_universal.assert_not_called()

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_extras_conflict("cpu", "gpu"),),
            ),
            targets=Matrix(
                python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
        (fork,) = mock_engine.call_args.kwargs["forks"]
        assert fork.selection == ()

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            extras=["cpu", "gpu"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(
                    _extras_conflict("cpu", "gpu", policy=ConflictPolicy.EXACTLY_ONE),
                ),
            ),
            targets=Matrix(
                python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="exactly one"),
        ):
            _resolved(
                pyproject,
                extras=["all"],
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(
                        _extras_conflict(
                            "cpu", "gpu", policy=ConflictPolicy.EXACTLY_ONE
                        ),
                    ),
                ),
                targets=Matrix(
                    python="==3.11",
                    platforms=(
                        PlatformSpec("linux_x86_64"),
                        PlatformSpec("windows_amd64"),
                    ),
                ).expand(),
            )
        mock_universal.assert_not_called()

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            extras=["all"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(
                    _extras_conflict("cpu", "gpu", policy=ConflictPolicy.EXACTLY_ONE),
                ),
            ),
            targets=Matrix(
                python="==3.11",
                platforms=(
                    PlatformSpec("linux_x86_64"),
                    PlatformSpec("windows_amd64"),
                ),
            ).expand(),
        )
        mock_engine.assert_called_once()

    @patch("nab_project.resolve.resolve_with_coordinator")
    @patch(
        "nab_project.resolve._check_group_disjointness",
        wraps=_check_group_disjointness,
    )
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
        _resolved(
            pyproject,
            extras=["cpu", "gpu"],
            groups=["dev", "lint"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_extras_conflict("cpu", "gpu"),),
            ),
            targets=Matrix(
                python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
        assert _scanned_groups(mock_check) == [("dev", "lint")]

    @patch("nab_project.resolve.resolve_with_coordinator")
    @patch(
        "nab_project.resolve._check_group_disjointness",
        wraps=_check_group_disjointness,
    )
    def test_distinct_active_groups_check_each(
        self,
        mock_check: MagicMock,
        mock_engine: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Group-conflict forks carry different group sets, so each is scanned."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[dependency-groups]\n"
            'dev = ["pytest"]\n'
            'cpu = ["torch==2.0+cpu"]\n'
            'gpu = ["torch==2.0+gpu"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ group = "cpu" }, { group = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        _resolved(
            pyproject,
            groups=["dev", "cpu", "gpu"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_groups_conflict("cpu", "gpu"),),
            ),
            targets=Matrix(
                python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
        assert _scanned_groups(mock_check) == [("dev", "cpu"), ("dev", "gpu")]

    def test_conflict_in_a_later_fork_raises(self, tmp_path: Path) -> None:
        """A pair that conflicts only in the second fork raises before the resolve."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[dependency-groups]\n"
            'dev = ["pkg>=2"]\n'
            'cpu = ["other"]\n'
            'gpu = ["pkg<1"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            'conflicts = [[{ group = "cpu" }, { group = "gpu" }]]\n'
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ResolutionError) as info,
        ):
            _resolved(
                pyproject,
                groups=["dev", "cpu", "gpu"],
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(_groups_conflict("cpu", "gpu"),),
                ),
                targets=Matrix(
                    python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
        mock_universal.assert_not_called()

        assert str(info.value) == (
            "Dependency groups 'dev' and 'gpu' conflict on 'pkg': "
            "group 'dev' requires pkg>=2 but group 'gpu' requires pkg<1."
        )

    def test_conflict_in_the_last_of_four_forks_raises(self, tmp_path: Path) -> None:
        """Two conflict sets fork four ways, and the last fork's conflict raises."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[dependency-groups]\n"
            'cpu = ["other"]\n'
            'gpu = ["pkg>=2"]\n'
            'test = ["thing"]\n'
            'docs = ["pkg<1"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "conflicts = [\n"
            '    [{ group = "cpu" }, { group = "gpu" }],\n'
            '    [{ group = "test" }, { group = "docs" }],\n'
            "]\n"
            "[tool.nab.matrix]\n"
            'python = "==3.11"\n'
            'platforms = ["linux_x86_64"]\n'
        )
        with (
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ResolutionError) as info,
        ):
            _resolved(
                pyproject,
                groups=["cpu", "gpu", "test", "docs"],
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(
                        _groups_conflict("cpu", "gpu"),
                        _groups_conflict("test", "docs"),
                    ),
                ),
                targets=Matrix(
                    python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
        mock_universal.assert_not_called()

        assert str(info.value) == (
            "Dependency groups 'docs' and 'gpu' conflict on 'pkg': "
            "group 'docs' requires pkg<1 but group 'gpu' requires pkg>=2."
        )

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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(
                pyproject,
                extras=["all"],
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(_extras_conflict("cpu", "gpu"),),
                ),
                targets=Matrix(
                    python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(
                pyproject,
                groups=["all-tools"],
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(_groups_conflict("b22", "b23"),),
                ),
                targets=Matrix(
                    python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(
                pyproject,
                extras=["all"],
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(_extras_conflict("cpu", "gpu"),),
                ),
                targets=Matrix(
                    python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
        mock_universal.assert_not_called()

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            extras=["all"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_extras_conflict("cpu", "gpu"),),
            ),
            targets=Matrix(
                python=">=3.9,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(
                pyproject,
                extras=["all"],
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(_extras_conflict("cpu", "gpu"),),
                ),
                targets=Matrix(
                    python=">=3.9,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(
                pyproject,
                extras=["all"],
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(_extras_conflict("cpu", "gpu"),),
                ),
                targets=Matrix(
                    python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="exactly one"),
        ):
            _resolved(
                pyproject,
                extras=["all"],
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(
                        _extras_conflict(
                            "cpu", "gpu", policy=ConflictPolicy.EXACTLY_ONE
                        ),
                    ),
                ),
                targets=Matrix(
                    python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
        mock_universal.assert_not_called()

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            extras=["all"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_extras_conflict("cpu", "gpu"),),
            ),
            targets=Matrix(
                python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
        mock_engine.assert_called_once()

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            extras=["all"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_extras_conflict("cpu", "gpu"),),
            ),
            targets=Matrix(
                python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
        mock_engine.assert_called_once()

    @patch("nab_project.resolve.resolve_with_coordinator")
    def test_wildcard_self_ref_marker_off_the_matrix_resolves(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """A ``.*`` literal under an ordering operator is skipped like any
        other self-ref marker the closure carries but no tuple reaches.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = ["base"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["foo==1.0"]\n'
            'gpu = ["foo==2.0"]\n'
            "legacy = [\"x[gpu]; python_full_version < '3.7.*'\"]\n"
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
        _resolved(
            pyproject,
            extras=["all"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_extras_conflict("cpu", "gpu"),),
            ),
            targets=Matrix(
                python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConflictSelectionError, match="cannot be selected together"),
        ):
            _resolved(
                pyproject,
                extras=["all"],
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(_extras_conflict("cpu", "gpu"),),
                ),
                targets=Matrix(
                    python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
        mock_universal.assert_not_called()

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            extras=["accel"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_extras_conflict("cpu", "gpu"),),
            ),
            targets=Matrix(
                python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ConfigError, match="gpuu"),
        ):
            _resolved(
                pyproject,
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    conflicts=(_extras_conflict("cpu", "gpuu"),),
                ),
                targets=Matrix(
                    python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
        mock_universal.assert_not_called()

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(
                    _groups_conflict("a", "b", policy=ConflictPolicy.EXACTLY_ONE),
                ),
                default_groups=("a",),
            ),
            targets=Matrix(
                python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
        (fork,) = mock_engine.call_args.kwargs["forks"]
        assert "foo==1.0" in [str(r) for r in fork.requirements]

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        _resolved(
            pyproject,
            groups=["b23"],
            inputs=ResolveInputs(
                build_policy=BuildPolicy.NEVER,
                conflicts=(_groups_conflict("b22", "b23"),),
                default_groups=("b22",),
            ),
            targets=Matrix(
                python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
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
                inputs=ResolveInputs(vcs=_GITHUB_HTTPS),
            )

    def test_refused_dependency_closes_the_transport(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        """A refusal closes the transport, and the fetcher thread survives it."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\ndependencies = "
            f'["foo @ git+https://github.com/foo/bar.git@{_FORTY}"]\n',
        )
        closes_before = _FAKE_TRANSPORT.aclose.await_count

        with pytest.raises(UnsupportedVcsError):
            _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        fetch_errors = [
            record.getMessage()
            for record in caplog.records
            if record.name == "nab_project.fetch" and record.levelno >= logging.ERROR
        ]

        assert fetch_errors == []
        assert _FAKE_TRANSPORT.aclose.await_count > closes_before


class TestResolvePyprojectLockShape:
    """Lock-input plumbing: the result always carries a LockInput."""

    def test_returns_resolution_result(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["foo>=1.0"]\n')

        with (
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock") as mock_build,
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider"),
            patch("nab_project._resolve.engine.build_target_lock"),
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider"),
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
                inputs=ResolveInputs(
                    indexes=(IndexConfig("private", "https://custom.index/simple/"),)
                ),
            )
        passed = mock_coord_cls.call_args.kwargs["indexes"]
        assert tuple(ix.url for ix in passed) == ("https://custom.index/simple/",)


class TestSpecificModeTargetPlan:
    """The resolve target: the host, or the environment the project declares."""

    @staticmethod
    def _mock_resolve(coord: MagicMock, resolver: MagicMock) -> None:
        coord.return_value.__enter__ = lambda s: s
        coord.return_value.__exit__ = MagicMock(return_value=False)
        resolver.return_value.resolve.return_value = {}

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        result = _resolved(
            pyproject, _FAKE_TRANSPORT, inputs=ResolveInputs(requires_python=">=3.9")
        )

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target == ResolveTarget.for_host()
        assert (
            build_lock_input(result, inputs=ResolveInputs(requires_python=">=3.9"))
        ).requires_python == ">=3.9"

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.10.5")

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target.python_full_version == "3.10.5"
        assert target.platform_id == "host"

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            targets=(
                ResolveTarget.for_declared(
                    python_version="3.11", spec=PlatformSpec("windows_amd64")
                ),
            ),
        )

        target = mock_provider_cls.call_args.kwargs["target"]
        assert target.platform_id == "windows_amd64"
        assert target.marker_env["sys_platform"] == "win32"
        assert target.python_version == "3.11"
        assert not target.host_faithful

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

        _resolved(
            pyproject,
            _FAKE_TRANSPORT,
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            targets=(
                ResolveTarget.for_declared(
                    python_version="3.12",
                    spec=PlatformSpec("linux_aarch64"),
                    python_full_version="3.12.4",
                ),
            ),
        )

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
        ``build_resolver_inputs`` below is where that is asserted.
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

        excluded = build_resolver_inputs(
            [req],
            VcsConfig(),
            environment={"python_version": "3.12", "python_full_version": "3.12.0"},
            marker_holds=dependency_marker_holds,
        ).ranges
        assert "some-dep" not in excluded


class TestBuildResolverInputs:
    """``build_resolver_inputs`` folds duplicate names by intersection."""

    def test_duplicate_name_intersects(self) -> None:
        """Two requirements for one package combine to their overlap."""
        reqs = [Requirement("foo>=2.0"), Requirement("foo<3.0")]
        resolver_requirements = build_resolver_inputs(
            reqs,
            VcsConfig(),
            environment={},
            marker_holds=dependency_marker_holds,
        ).ranges
        foo = resolver_requirements["foo"]
        assert V("2.5") in foo
        assert V("1.0") not in foo
        assert V("5.0") not in foo

    def test_conflicting_names_stay_separate_roots(self) -> None:
        """Contradictory requirements reach the solver as their own clauses."""
        reqs = [Requirement("foo==1.0"), Requirement("foo==2.0")]
        inputs = build_resolver_inputs(
            reqs,
            VcsConfig(),
            environment={},
            marker_holds=dependency_marker_holds,
        )
        assert [(root.package, root.origin) for root in inputs.roots] == [
            ("foo", "foo==1.0"),
            ("foo", "foo==2.0"),
        ]
        assert inputs.ranges["foo"].is_empty

    def test_a_repeated_extra_gets_one_proxy_root(self) -> None:
        """A second mention of the same extra adds no second proxy clause."""
        reqs = [Requirement("foo[dev]>1"), Requirement("foo[dev]<9")]
        inputs = build_resolver_inputs(
            reqs,
            VcsConfig(),
            environment={},
            marker_holds=dependency_marker_holds,
        )
        assert [root.package for root in inputs.roots] == ["foo", "foo[dev]", "foo"]

    def test_root_extra_marker_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """A root requirement gated on ``extra ==`` is dropped with a warning."""
        reqs = [Requirement('foo ; extra == "test"')]
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            resolver_requirements = build_resolver_inputs(
                reqs,
                VcsConfig(),
                environment={},
                marker_holds=dependency_marker_holds,
            ).ranges
        assert "foo" not in resolver_requirements
        assert any("membership marker" in rec.message for rec in caplog.records)

    def test_root_extras_set_marker_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A root ``"x" in extras`` marker is dropped with a warning, not a crash."""
        reqs = [Requirement('foo ; "x" in extras')]
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            resolver_requirements = build_resolver_inputs(
                reqs,
                VcsConfig(),
                environment={},
                marker_holds=dependency_marker_holds,
            ).ranges
        assert "foo" not in resolver_requirements
        assert any("membership marker" in rec.message for rec in caplog.records)

    def test_root_dependency_groups_marker_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A root ``in dependency_groups`` marker is dropped with a warning."""
        reqs = [Requirement('foo ; "dev" in dependency_groups')]
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            resolver_requirements = build_resolver_inputs(
                reqs,
                VcsConfig(),
                environment={},
                marker_holds=dependency_marker_holds,
            ).ranges
        assert "foo" not in resolver_requirements
        assert any("membership marker" in rec.message for rec in caplog.records)

    def test_extra_marker_without_spaces_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """packaging normalises the spelling, so the scan sees one form."""
        reqs = [Requirement('foo ; extra=="test"')]
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            build_resolver_inputs(
                reqs,
                VcsConfig(),
                environment={},
                marker_holds=dependency_marker_holds,
            )
        assert any("membership marker" in rec.message for rec in caplog.records)

    def test_extras_of_package_syntax_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``pkg[redis]`` is the syntax the warning points at; it must not warn."""
        reqs = [Requirement("foo[redis]")]
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            resolver_requirements = build_resolver_inputs(
                reqs,
                VcsConfig(),
                environment={},
                marker_holds=dependency_marker_holds,
            ).ranges
        assert "foo" in resolver_requirements
        assert not caplog.records

    def test_env_gated_drop_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        """A requirement dropped by a plain env marker stays silent."""
        reqs = [Requirement('foo ; python_version < "3.0"')]
        env = {"python_version": "3.11", "python_full_version": "3.11.2"}
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            resolver_requirements = build_resolver_inputs(
                reqs,
                VcsConfig(),
                environment=env,
                marker_holds=dependency_marker_holds,
            ).ranges
        assert "foo" not in resolver_requirements
        assert not caplog.records

    def test_root_requirement_warning_points_at_extras_of_package(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The requirement pass names a root requirement and points at pkg[extra]."""
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            build_resolver_inputs(
                [Requirement('foo<2.0 ; extra == "fast"')],
                VcsConfig(),
                environment={},
                marker_holds=dependency_marker_holds,
            )

        (record,) = caplog.records
        assert record.message.startswith(
            "Root requirement 'foo<2.0; extra == \"fast\"'"
        )
        assert "pkg[extra] (extras-of-package)" in record.message

    def test_warning_cause_holds_when_the_extra_was_selected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The cause it gives is true on a run that did select the extra.

        ``--extras fast`` folds the extra's entries into the requirement list
        and leaves the marker environment alone, so blaming an inactive extra
        would send the user after a flag they already passed.
        """
        selected = expand_extra_requirements(
            {"fast": ['foo<2.0 ; extra == "fast"']}, "proj", ["fast"]
        )
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            build_resolver_inputs(
                selected,
                VcsConfig(),
                environment={},
                marker_holds=dependency_marker_holds,
            )

        (record,) = caplog.records
        assert (
            "folded into the requirements rather than the environment" in record.message
        )
        assert "no extra or dependency group is active" not in record.message

    def test_unknown_kind_is_named_after_itself(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``kind`` is a free string, so one with no subject warns, not raises."""
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            build_resolver_inputs(
                [Requirement('foo<2.0 ; extra == "fast"')],
                VcsConfig(),
                environment={},
                marker_holds=dependency_marker_holds,
                kind="override",
            )

        (record,) = caplog.records
        assert record.message.startswith("Override 'foo<2.0; extra == \"fast\"'")

    def test_repeated_text_warns_once_per_kind(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One warned set covers both passes, and each keeps its own wording.

        A target's requirement pass and constraint pass share the set, so a
        project that writes the same line in both must still get the advice
        that fits a constraint.
        """
        reqs = [Requirement('foo<2.0 ; extra == "fast"')]
        warned: set[tuple[str, str]] = set()
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            for kind in ("requirement", "requirement", "constraint"):
                build_resolver_inputs(
                    reqs,
                    VcsConfig(),
                    environment={},
                    marker_holds=dependency_marker_holds,
                    kind=kind,
                    warned=warned,
                )

        subjects = [rec.message.split(" '", 1)[0] for rec in caplog.records]
        assert subjects == ["Root requirement", "Constraint"]

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
        resolver_requirements = build_resolver_inputs(
            [req],
            VcsConfig(),
            environment={},
            marker_holds=dependency_marker_holds,
        ).ranges
        proxy_keys = [k for k in resolver_requirements if k.startswith("demo[")]
        assert proxy_keys == ["demo[x]", "demo[y]", "demo[z]"]


class TestBuildConstraints:
    """``_build_constraints`` folds duplicate constraint lines."""

    def test_duplicate_constraint_intersects(self) -> None:
        """Two constraint lines for one package combine to their overlap."""
        out = _build_constraints(
            ResolveInputs(constraints=("foo>=2.0", "foo<3.0")), environment={}
        )
        assert V("2.5") in out["foo"]
        assert V("1.0") not in out["foo"]
        assert V("5.0") not in out["foo"]

    def test_conflicting_constraints_raise(self) -> None:
        """Pinned-but-different constraint lines for one package raise."""
        with pytest.raises(ResolutionError, match="conflicting constraints"):
            _build_constraints(
                ResolveInputs(constraints=("foo==1.0", "foo==2.0")), environment={}
            )

    def test_marker_false_constraint_dropped(self) -> None:
        """A constraint whose marker is False is not applied."""
        env = {"python_version": "3.12"}
        out = _build_constraints(
            ResolveInputs(constraints=('foo<2.0 ; python_version < "3.0"',)),
            environment=env,
        )
        assert "foo" not in out

    def test_marker_true_constraint_applied(self) -> None:
        """A constraint whose marker is True still restricts the range."""
        env = {"python_version": "3.12"}
        out = _build_constraints(
            ResolveInputs(constraints=('foo<2.0 ; python_version >= "3.0"',)),
            environment=env,
        )
        assert V("1.0") in out["foo"]
        assert V("5.0") not in out["foo"]

    def test_constraint_with_extras_rejected(self) -> None:
        """A constraint carrying extras is rejected, matching pip."""
        with pytest.raises(ConfigError, match="extras"):
            _build_constraints(
                ResolveInputs(constraints=("foo[dev]<2.0",)), environment={}
            )

    def test_constraint_with_extras_rejected_under_false_marker(self) -> None:
        """Extras on a constraint are rejected even when its marker is False.

        pip rejects constraint extras at parse, before evaluating the marker,
        and the universal path does the same. The extras guard must run before
        the marker drop so a marker-false constraint is not silently accepted.
        """
        with pytest.raises(ConfigError, match="extras"):
            _build_constraints(
                ResolveInputs(constraints=('foo[dev]<2.0 ; python_version < "3.0"',)),
                environment={"python_version": "3.12"},
            )

    def test_set_marker_constraint_dropped(self) -> None:
        """A constraint gated on a lockfile-only set marker drops, not crashes."""
        out = _build_constraints(
            ResolveInputs(constraints=('foo<2.0 ; "x" in extras',)), environment={}
        )
        assert "foo" not in out

    def _drop_warning(
        self,
        caplog: pytest.LogCaptureFixture,
        constraint: str = 'foo<2.0 ; extra == "fast"',
        environment: dict[str, str] | None = None,
    ) -> str:
        """The one warning a membership-gated constraint logs when dropped."""
        with caplog.at_level("WARNING", logger="nab_provider.resolver_inputs"):
            out = _build_constraints(
                ResolveInputs(constraints=(constraint,)),
                environment={} if environment is None else environment,
            )

        assert "foo" not in out
        (record,) = caplog.records
        return record.message

    def test_dropped_constraint_warning_names_a_constraint(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The drop warning names the entry a constraint, not a root requirement."""
        message = self._drop_warning(caplog)

        assert message.startswith("Constraint 'foo<2.0; extra == \"fast\"'")
        assert "Root requirement" not in message

    def test_dropped_constraint_warning_omits_pkg_extra(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """pkg[extra] is the one spelling the constraint parser refuses."""
        message = self._drop_warning(caplog)

        assert "pkg[extra]" not in message
        assert "A constraint cannot carry extras" in message

    def test_dropped_group_constraint_warning_omits_the_extras_rule(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A group membership has no extras spelling, so that sentence stays out."""
        message = self._drop_warning(caplog, 'foo<2.0 ; "docs" in dependency_groups')

        assert "cannot carry extras" not in message
        assert "Drop the membership test from the marker and keep the rest." in message

    def test_dropped_constraint_repair_still_bounds_the_package(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The repair the warning asks for parses and still bounds foo."""
        message = self._drop_warning(caplog)
        assert message.endswith(
            "so drop the membership test from the marker and keep the rest."
        )

        inputs = ResolveInputs(constraints=("foo<2.0",))
        out = _build_constraints(inputs, environment={})
        assert V("1.0") in out["foo"]
        assert V("5.0") not in out["foo"]

    def test_repaired_compound_marker_keeps_its_environment_test(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Keeping the rest of the marker leaves foo unbound on other interpreters.

        Dropping the whole marker instead would bound foo everywhere, which
        is not what the project wrote.
        """
        on_39 = {"python_version": "3.9", "python_full_version": "3.9.18"}
        message = self._drop_warning(
            caplog,
            'foo<2.0 ; extra == "fast" and python_version < "3.10"',
            environment=on_39,
        )

        assert message.endswith(
            "so drop the membership test from the marker and keep the rest."
        )

        inputs = ResolveInputs(constraints=("foo<2.0 ; python_version < '3.10'",))
        out = _build_constraints(inputs, environment=on_39)
        assert V("1.0") in out["foo"]
        assert V("5.0") not in out["foo"]

        on_311 = {"python_version": "3.11", "python_full_version": "3.11.2"}
        assert "foo" not in _build_constraints(inputs, environment=on_311)


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
            patch("nab_project.resolve.FetchCoordinator"),
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

    def test_single_group_pairs_with_nobody(self) -> None:
        per_group = {"docs": [Requirement("sphinx<7")]}
        _check_group_disjointness(per_group, [_target({})])

    def test_single_group_that_cannot_hold_is_named(self) -> None:
        """One group contradicting itself is reported without a partner."""
        per_group = {"docs": [Requirement("sphinx<7"), Requirement("sphinx>=7")]}
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, [_target({})])
        assert str(info.value) == (
            "Dependency group 'docs' has conflicting requirements on 'sphinx':"
            " sphinx<7, sphinx>=7."
        )

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

    def test_self_empty_group_does_not_blame_an_unversioned_group(self) -> None:
        """A group that cannot hold alone is named, and nobody else is."""
        per_group = {
            "alpha": [Requirement("sphinx>=7"), Requirement("sphinx<6")],
            "beta": [Requirement("sphinx")],
        }
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, [_target({})])
        assert str(info.value) == (
            "Dependency group 'alpha' has conflicting requirements on 'sphinx':"
            " sphinx>=7, sphinx<6."
        )

    def test_self_empty_group_does_not_blame_an_overlapping_group(self) -> None:
        """The other groups naming the package are not reported."""
        per_group = {
            "alpha": [Requirement("sphinx>=7"), Requirement("sphinx<6")],
            "beta": [Requirement("sphinx")],
            "gamma": [Requirement("sphinx>=2")],
        }
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, [_target({})])
        message = str(info.value)
        assert "'alpha'" in message
        assert "'beta'" not in message
        assert "'gamma'" not in message

    def test_self_empty_group_on_one_package_still_pairs_on_another(self) -> None:
        """The self-empty skip is per package, not per group."""
        per_group = {
            "a": [Requirement("x>=2"), Requirement("x<1"), Requirement("y<3")],
            "b": [Requirement("y>=4")],
        }
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, [_target({})])
        assert str(info.value) == (
            "Dependency group 'a' has conflicting requirements on 'x':"
            " x>=2, x<1.;"
            " Dependency groups 'a' and 'b' conflict on 'y':"
            " group 'a' requires y<3 but group 'b' requires y>=4."
        )

    def test_umbrella_group_is_not_blamed_for_the_pair_it_includes(self) -> None:
        """An umbrella group is named alone, not paired with its members.

        ``dev`` here is ``lint`` plus ``test`` expanded.
        """
        per_group = {
            "dev": [Requirement("sphinx<6"), Requirement("sphinx>=7")],
            "lint": [Requirement("sphinx>=7")],
            "test": [Requirement("sphinx<6")],
        }
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, [_target({})])
        assert str(info.value) == (
            "Dependency group 'dev' has conflicting requirements on 'sphinx':"
            " sphinx<6, sphinx>=7.;"
            " Dependency groups 'lint' and 'test' conflict on 'sphinx':"
            " group 'lint' requires sphinx>=7 but group 'test' requires sphinx<6."
        )

    def test_umbrella_group_through_include_group_is_named_alone(
        self, tmp_path: Path
    ) -> None:
        """The same shape read through a real ``include-group`` table."""
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'x'\n"
            "[dependency-groups]\n"
            "dev = [{ include-group = 'lint' }, { include-group = 'test' }]\n"
            "lint = ['sphinx>=7']\n"
            "test = ['sphinx<6']\n"
        )
        per_group = _group_requirements_by_group(
            read_pyproject_groups(path), ["dev", "lint", "test"], path
        )
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, [_target({})])
        assert str(info.value) == (
            "Dependency group 'dev' has conflicting requirements on 'sphinx':"
            " sphinx>=7, sphinx<6.;"
            " Dependency groups 'lint' and 'test' conflict on 'sphinx':"
            " group 'lint' requires sphinx>=7 but group 'test' requires sphinx<6."
        )


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

    def test_self_empty_group_pairs_with_nobody(self) -> None:
        """One group's own contradiction stays out of the pairwise walk."""
        per_group = self._disjoint_groups()
        per_group[self._LEFT].extend(
            [Requirement("shared>=2"), Requirement("shared<1")]
        )
        per_group[self._RIGHT].append(Requirement("shared>=1"))
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
            patch("nab_project.resolve.FetchCoordinator"),
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

    def test_self_empty_group_names_only_itself(self, tmp_path: Path) -> None:
        """A group that cannot hold alone is named without its neighbour."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\ndependencies = []\n'
            "[dependency-groups]\n"
            'alpha = ["sphinx>=7", "sphinx<6"]\n'
            'beta = ["sphinx"]\n'
        )
        with (
            patch("nab_project.resolve.FetchCoordinator"),
            pytest.raises(ResolutionError) as info,
        ):
            resolve_for_targets(
                pyproject,
                _FAKE_TRANSPORT,
                targets=(ResolveTarget.for_host_python("3.12.0"),),
                groups=["alpha", "beta"],
            )
        assert str(info.value) == (
            "Dependency group 'alpha' has conflicting requirements on 'sphinx':"
            " sphinx>=7, sphinx<6."
        )

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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

    @patch("nab_project._resolve.engine.build_target_lock")
    @patch("nab_project._resolve.engine.Resolver")
    @patch("nab_project._resolve.engine.Provider")
    @patch("nab_project.resolve.FetchCoordinator")
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


def _diagnostics(error: ResolutionError, *, detailed: bool = True) -> str:
    """Return the ``Diagnostics:`` section, at ``-v`` depth unless told otherwise."""
    text = error.verbose_message if detailed else str(error)
    assert text is not None
    return text.split("Diagnostics:")[1]


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
            Diagnostic("package not found on any configured index")
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
        provider.get_no_versions_reason.return_value = Diagnostic(
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

    def test_walk_names_merged_self_dependency_candidate(self) -> None:
        """A self-dependency merges to one positive term and still names it."""
        clause = Incompatibility(
            [Term("cand", Range.full(), positive=True)],
            cause=IncompatibilityCause.DEPENDENCY,
            dependency_range=Range.singleton(1),
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
            Diagnostic("no version matches the requirement") if pkg == "cand" else None
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.Resolver") as mock_resolver_cls,
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider_cls.return_value.get_no_versions_reason.return_value = (
                Diagnostic("package not found on any configured index")
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

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        diagnostics = _diagnostics(info.value)
        assert "foo: every version in range needs bar in >=2" in diagnostics
        assert "bar" in diagnostics
        assert "foo: no version matches the requirement" not in diagnostics

    def test_blocker_line_is_scoped_to_the_range_the_scan_covered(
        self, tmp_path: Path
    ) -> None:
        """The blocker line says "in range" when the ask excluded working versions.

        ``app`` bounds ``lib`` to ``>=3``, so the look-ahead sees only
        ``lib`` 3.0 and its ``dep==2.2``.  ``lib`` 2.0 and 1.2 declare no
        ``dep``, so an unqualified "every version" would be false of the
        package.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["app", "web"]\n',
            encoding="utf-8",
        )

        coordinator = make_coordinator(
            listings={
                "app": _index_wheels("app", "1.0"),
                "web": _index_wheels("web", "1.1"),
                "lib": _index_wheels("lib", "3.0", "2.0", "1.2"),
                "dep": _index_wheels("dep", "2.2", "2.1"),
            },
            metadata_by_version={
                "1.0": _metadata("app", "1.0", "lib>=3"),
                "1.1": _metadata("web", "1.1", "dep==2.1"),
                "3.0": _metadata("lib", "3.0", "dep==2.2"),
                "2.0": _metadata("lib", "2.0"),
                "1.2": _metadata("lib", "1.2"),
                "2.2": _metadata("dep", "2.2"),
                "2.1": _metadata("dep", "2.1"),
            },
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        diagnostics = _diagnostics(info.value)
        assert (
            "lib: every version in range needs dep in ==2.2, but the resolve"
            " chose dep 2.1" in diagnostics
        )

    def test_filtered_release_is_not_reported_as_a_missing_version(
        self, tmp_path: Path
    ) -> None:
        """A filtered release reads as filtered rather than as absent.

        ``foo`` 2.0 matches ``foo>=2`` and only its ``Requires-Python``
        keeps it off a 3.12 target.  Its 1.0 survives, so the package has a
        version and the whole-listing wording does not fire.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo>=2"]\n',
            encoding="utf-8",
        )

        newer = WheelFile(
            filename="foo-2.0-py3-none-any.whl",
            url="https://example.com/foo-2.0-py3-none-any.whl",
            version="2.0",
            requires_python=">=3.13",
            has_metadata=True,
            upload_time=None,
        )
        coordinator = make_coordinator(
            listings={"foo": [newer, *_index_wheels("foo", "1.0")]},
            metadata_by_version={
                "2.0": _metadata("foo", "2.0"),
                "1.0": _metadata("foo", "1.0"),
            },
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        diagnostics = _diagnostics(info.value)
        assert "foo: no version in range supports Python 3.12" in diagnostics
        assert "no version matches the requirement" not in diagnostics

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

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(
                    pyproject,
                    _FAKE_TRANSPORT,
                    python_version="3.12.0",
                    inputs=ResolveInputs(constraints=("foo<2.0",)),
                )

        diagnostics = _diagnostics(info.value)
        assert "<VersionRange" not in diagnostics
        assert (
            "foo: every version in range needs lib in ==5.0, but the resolve chose"
            " lib 9.0" in diagnostics
        )
        assert "requires lib != 9.0" not in diagnostics
        assert "foo: no version matches the requirement" not in diagnostics

    def test_metadata_ban_outlives_the_scan_that_accepted_an_older_version(
        self, tmp_path: Path
    ) -> None:
        """A ban raised by a scan that succeeded still names its versions.

        ``foo`` 5.0/4.0/3.0 advertise a sidecar the index does not serve and
        have no sdist to fall back on, so the scan bans them and accepts 2.0.
        Backtracking then drops 2.0 and 1.0, leaving the ban to close the
        proof, and only it can say why those three versions went.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n',
            encoding="utf-8",
        )

        coordinator = make_coordinator(
            listings={
                "foo": _index_wheels("foo", "5.0", "4.0", "3.0", "2.0", "1.0"),
                "lib": _index_wheels("lib", "6.0"),
            },
            metadata_by_version={
                "5.0": None,
                "4.0": None,
                "3.0": None,
                "2.0": _metadata("foo", "2.0", "lib==9.9"),
                "1.0": _metadata("foo", "1.0", "lib==9.8"),
                "6.0": _metadata("lib", "6.0"),
            },
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        derivation = str(info.value).split("Diagnostics:")[0]
        diagnostics = _diagnostics(info.value)
        assert "no versions of foo" in derivation
        assert "foo: every version in range was rejected on its metadata" in diagnostics
        assert (
            "No metadata for foo==5.0: no PEP 658 metadata and no sdist"
            " available" in diagnostics
        )

    def test_a_sibling_requirement_does_not_bury_the_metadata_ban(
        self, tmp_path: Path
    ) -> None:
        """A generic reason recorded first still loses to the ban.

        ``bar`` 22.0 asks for ``foo>=6.0``, a range nothing matches, so ``foo``
        gets the bare "no version matches" line before its own scan bans
        5.0/4.0/3.0.  The failure closes on the ban, so that is what the
        diagnostic must report.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo", "bar"]\n',
            encoding="utf-8",
        )

        coordinator = make_coordinator(
            listings={
                "foo": _index_wheels("foo", "5.0", "4.0", "3.0", "2.0", "1.0"),
                "bar": _index_wheels("bar", "22.0", "21.0"),
                "lib": _index_wheels("lib", "6.0"),
            },
            metadata_by_version={
                "5.0": None,
                "4.0": None,
                "3.0": None,
                "2.0": _metadata("foo", "2.0", "lib==9.9"),
                "1.0": _metadata("foo", "1.0", "lib==9.8"),
                "22.0": _metadata("bar", "22.0", "foo>=6.0"),
                "21.0": _metadata("bar", "21.0"),
                "6.0": _metadata("lib", "6.0"),
            },
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        diagnostics = _diagnostics(info.value)
        assert "foo: every version in range was rejected on its metadata" in diagnostics
        assert (
            "No metadata for foo==5.0: no PEP 658 metadata and no sdist"
            " available" in diagnostics
        )
        assert "foo: no version matches the requirement" not in diagnostics

    def test_bans_from_separate_scans_are_counted_together(
        self, tmp_path: Path
    ) -> None:
        """Every scan's ban reaches the diagnostic, not just the first one's.

        ``foo`` 5.0 is unreadable and 4.0 is not, so the first scan bans one
        version; backtracking past 4.0 starts a second scan that bans 3.0.  The
        derivation carries a ban clause from each.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n',
            encoding="utf-8",
        )

        coordinator = make_coordinator(
            listings={
                "foo": _index_wheels("foo", "5.0", "4.0", "3.0", "2.0"),
                "lib": _index_wheels("lib", "6.0"),
            },
            metadata_by_version={
                "5.0": None,
                "4.0": _metadata("foo", "4.0", "lib==9.9"),
                "3.0": None,
                "2.0": _metadata("foo", "2.0", "lib==9.8"),
                "6.0": _metadata("lib", "6.0"),
            },
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        derivation = str(info.value).split("Diagnostics:")[0]
        diagnostics = _diagnostics(info.value)
        assert "no versions of foo <VersionRange '(4.0, +inf)'>" in derivation
        assert "no versions of foo <VersionRange '(2.0, 4.0)'>" in derivation
        assert "foo: every version in range was rejected on its metadata" in diagnostics
        assert (
            "No metadata for foo==5.0: no PEP 658 metadata and no sdist"
            " available" in diagnostics
        )
        assert (
            "No metadata for foo==3.0: no PEP 658 metadata and no sdist"
            " available" in diagnostics
        )

    def test_cutoff_filtered_sdist_is_not_reported_as_never_published(
        self, tmp_path: Path
    ) -> None:
        """An sdist the cutoff dropped is reported as filtered, not as absent.

        ``foo`` 1.0 publishes a sidecar-less wheel and an sdist uploaded
        after the cutoff, so the metadata ladder runs out of rungs.  Moving
        the cutoff is the repair, so a line saying no sdist exists would send
        the user to the index instead.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n'
            '[tool.nab]\nuploaded-prior-to = "2026-01-10T00:00:00Z"\n',
            encoding="utf-8",
        )

        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://example.com/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time="2026-01-01T00:00:00Z",
        )
        sdist = SdistFile(
            filename="foo-1.0.tar.gz",
            url="https://example.com/foo-1.0.tar.gz",
            version="1.0",
            requires_python=None,
            upload_time="2026-01-20T00:00:00Z",
        )
        coordinator = make_coordinator(listings={"foo": [wheel, sdist]})

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(
                    pyproject,
                    _FAKE_TRANSPORT,
                    python_version="3.12.0",
                    inputs=ResolveInputs(
                        uploaded_prior_to=datetime(2026, 1, 10, tzinfo=timezone.utc)
                    ),
                )

        diagnostics = _diagnostics(info.value)
        assert (
            "foo: uploaded-prior-to excluded the sdist nab needed for metadata"
            in diagnostics
        )

    def test_zip_sdist_is_not_reported_as_never_published(self, tmp_path: Path) -> None:
        """The diagnostics name the format, rather than reporting no sdist.

        ``foo`` 1.0 publishes a sidecar-less wheel beside a ``.zip`` sdist,
        which the listing parse drops.  Reading the report as no sdist would
        send the user looking for a release that has one.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n',
            encoding="utf-8",
        )

        wheel = WheelFile(
            filename="foo-1.0-py3-none-any.whl",
            url="https://example.com/foo-1.0-py3-none-any.whl",
            version="1.0",
            requires_python=None,
            has_metadata=False,
            upload_time=None,
        )
        coordinator = make_coordinator()
        coordinator.index.store_listing("foo", [wheel], zip_sdists=frozenset({"1.0"}))

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        diagnostics = _diagnostics(info.value)
        assert "foo: every version in range was rejected on its metadata" in diagnostics
        assert (
            "No metadata for foo==1.0: no PEP 658 metadata and the sdist is a"
            " .zip, a format nab drops" in diagnostics
        )

    def test_a_resolve_that_survives_the_ladder_never_walks_the_listing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The listing walk is a failure-path cost and stays off a resolve that works.

        ``foo`` 2.0 runs the ladder out of rungs, look-ahead takes that as a
        rejection, and 1.0 pins.  Naming the rung that took 2.0's sdist would
        mean walking the whole listing for a sentence nobody reads.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n'
            '[tool.nab]\nuploaded-prior-to = "2026-05-01T00:00:00Z"\n',
            encoding="utf-8",
        )

        coordinator = make_coordinator(
            listings={
                "foo": [
                    WheelFile(
                        filename="foo-2.0-py3-none-any.whl",
                        url="https://example.com/foo-2.0-py3-none-any.whl",
                        version="2.0",
                        requires_python=None,
                        has_metadata=False,
                        upload_time="2026-01-01T00:00:00Z",
                    ),
                    SdistFile(
                        filename="foo-2.0.tar.gz",
                        url="https://example.com/foo-2.0.tar.gz",
                        version="2.0",
                        requires_python=None,
                        upload_time="2030-01-01T00:00:00Z",
                    ),
                    WheelFile(
                        filename="foo-1.0-py3-none-any.whl",
                        url="https://example.com/foo-1.0-py3-none-any.whl",
                        version="1.0",
                        requires_python=None,
                        has_metadata=True,
                        upload_time="2026-01-01T00:00:00Z",
                    ),
                ]
            },
            metadata_by_version={"1.0": _metadata("foo", "1.0")},
        )

        walks: list[str] = []
        real_walk = listing_diagnosis.walk_listing

        def counted_walk(provider: Provider, normalized: str) -> object:
            walks.append(normalized)
            return real_walk(provider, normalized)

        monkeypatch.setattr(listing_diagnosis, "walk_listing", counted_walk)

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                python_version="3.12.0",
                inputs=ResolveInputs(
                    uploaded_prior_to=datetime(2026, 5, 1, tzinfo=timezone.utc)
                ),
            )

        assert _pins(result) == {"foo": Version("1.0")}
        assert walks == []

    def test_blocker_diagnostics_render_readable_ranges(self, tmp_path: Path) -> None:
        """Blocker diagnostics render declared ranges, not the debug repr.

        ``c`` declares ``b==1.0`` while the project requires ``b>=2``, so
        look-ahead rejects every ``c`` candidate against the root requirement.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["b>=2", "c"]\n',
            encoding="utf-8",
        )

        coordinator = make_coordinator(
            listings={
                "b": _index_wheels("b", "1.0", "2.0"),
                "c": _index_wheels("c", "5.0", "6.0"),
            },
            metadata_by_version={
                "1.0": _metadata("b", "1.0"),
                "2.0": _metadata("b", "2.0"),
                "5.0": _metadata("c", "5.0", "b==1.0"),
                "6.0": _metadata("c", "6.0", "b==1.0"),
            },
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        diagnostics = _diagnostics(info.value)
        assert "<VersionRange" not in diagnostics
        assert "AFTER_LOCALS" not in diagnostics
        assert (
            "c: every version in range needs b in ==1.0, but your project requires b >=2"
        ) in diagnostics

    def test_blocker_diagnostics_spell_each_declared_range(
        self, tmp_path: Path
    ) -> None:
        """A blocker line reads as requirements when the pins disagree.

        ``a`` 7.0 pins ``c==1.0`` and 7.1 pins ``c==2.0``, a union no
        specifier set spells, so the line states the two pins one by one.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["a", "b"]\n',
            encoding="utf-8",
        )

        coordinator = make_coordinator(
            listings={
                "a": _index_wheels("a", "7.0", "7.1"),
                "b": _index_wheels("b", "8.0"),
                "c": _index_wheels("c", "1.0", "2.0", "3.0"),
            },
            metadata_by_version={
                "7.0": _metadata("a", "7.0", "c==1.0"),
                "7.1": _metadata("a", "7.1", "c==2.0"),
                "8.0": _metadata("b", "8.0", "c==3.0"),
                "1.0": _metadata("c", "1.0"),
                "2.0": _metadata("c", "2.0"),
                "3.0": _metadata("c", "3.0"),
            },
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        diagnostics = _diagnostics(info.value)
        assert "<VersionRange" not in diagnostics
        assert "AFTER_LOCALS" not in diagnostics
        assert (
            "a: every version in range needs c in ==2.0 or ==1.0, but the resolve chose c 3.0"
        ) in diagnostics

    def test_derivation_renders_readable_ranges(self, tmp_path: Path) -> None:
        """The derivation states ranges as requirements, like the diagnostics do.

        ``a`` and ``b`` pin ``c`` at different versions, so the term the report
        carries for ``b`` is the widened complement of ``b``'s pin.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["a", "b"]\n',
            encoding="utf-8",
        )

        coordinator = make_coordinator(
            listings={
                "a": _index_wheels("a", "7.0"),
                "b": _index_wheels("b", "8.0"),
                "c": _index_wheels("c", "1.0", "2.0"),
            },
            metadata_by_version={
                "7.0": _metadata("a", "7.0", "c==1.0"),
                "8.0": _metadata("b", "8.0", "c==2.0"),
                "1.0": _metadata("c", "1.0"),
                "2.0": _metadata("c", "2.0"),
            },
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        derivation = str(info.value).split("Diagnostics:")[0]
        assert derivation.splitlines() == [
            "because all versions of a depend on c ==1.0",
            "because all versions of b are incompatible with c <=1.0",
            "so all versions of a and all versions of b",
            "because your project depends on b",
            "so all versions of a",
            "because your project depends on a",
            "so your project's requirements cannot be satisfied",
            "",
        ]


class TestConflictingRootRequirements:
    def test_both_requirements_are_named(self, tmp_path: Path) -> None:
        """Two requirements on one package each get their own line."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo>1.0", "foo==1.0"]\n',
            encoding="utf-8",
        )
        coordinator = make_coordinator(
            listings={"foo": _index_wheels("foo", "1.0", "2.0")},
            metadata_by_version={
                "1.0": _metadata("foo", "1.0"),
                "2.0": _metadata("foo", "2.0"),
            },
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(pyproject, _FAKE_TRANSPORT, python_version="3.12.0")

        lines = str(info.value).splitlines()
        named = [line for line in lines if "your project depends on foo" in line]
        assert len(named) == 2
        assert not any("empty" in line for line in lines)

    def test_conflicting_constraints_still_fail_before_the_solve(
        self, tmp_path: Path
    ) -> None:
        """Constraints are not root clauses, so their fold stays pre-checked."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n'
            '[tool.nab]\nconstraints = ["foo<1.0", "foo>2.0"]\n',
            encoding="utf-8",
        )
        coordinator = make_coordinator(
            listings={"foo": _index_wheels("foo", "1.0", "2.0")},
            metadata_by_version={
                "1.0": _metadata("foo", "1.0"),
                "2.0": _metadata("foo", "2.0"),
            },
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError, match="conflicting constraints"):
                _resolved(
                    pyproject,
                    _FAKE_TRANSPORT,
                    python_version="3.12.0",
                    inputs=ResolveInputs(constraints=("foo<1.0", "foo>2.0")),
                )


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

    def test_single_group_that_cannot_hold_names_its_tuples(self) -> None:
        """A self-contradictory group is named per tuple, like a pair."""
        per_group = {"a": [Requirement("foo<2"), Requirement("foo>=2")]}
        tuples = [_tuple_for_python("3.10"), _tuple_for_python("3.12")]
        with pytest.raises(ResolutionError) as info:
            _check_group_disjointness(per_group, tuples)
        assert str(info.value) == (
            "Dependency group 'a' has conflicting requirements on 'foo'"
            " for tuple(s) py310-linux_x86_64, py312-linux_x86_64:"
            " foo<2, foo>=2."
        )

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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ResolutionError) as info,
        ):
            _resolved(
                pyproject,
                groups=["a", "b"],
                inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
                targets=Matrix(
                    python=">=3.11,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
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
            patch("nab_project.resolve.resolve_with_coordinator") as mock_universal,
            pytest.raises(ResolutionError) as info,
        ):
            _resolved(
                pyproject,
                groups=["a", "b"],
                inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
                targets=Matrix(
                    python=">=3.11,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )
        mock_universal.assert_not_called()
        message = str(info.value)
        assert "py311-linux_x86_64" in message
        assert "py312-linux_x86_64" in message

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        result = _resolved(
            pyproject,
            groups=["a", "b"],
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            targets=Matrix(
                python=">=3.11,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
        assert result is sentinel

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        result = _resolved(
            pyproject,
            groups=["a"],
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            targets=Matrix(
                python=">=3.11,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
        assert result is sentinel

    @patch("nab_project.resolve.resolve_with_coordinator")
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
        result = _resolved(
            pyproject,
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            targets=Matrix(
                python=">=3.11,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda _self: fake
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError, match="foo 1.0 requires Python"):
                _resolved(
                    root,
                    _FAKE_TRANSPORT,
                    python_version="3.10.0",
                    inputs=ResolveInputs(
                        local_sources=(LocalSource("foo", str(tmp_path / "foo")),)
                    ),
                )

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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda _self: fake
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(
                root,
                _FAKE_TRANSPORT,
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    local_sources=(LocalSource("foo", str(tmp_path / "foo")),),
                ),
                python_version="3.12.1",
            )
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda _self: fake
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(
                root,
                _FAKE_TRANSPORT,
                inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
                python_version="3.12.1",
            )
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
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda _self: fake
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError) as info:
                _resolved(root, _FAKE_TRANSPORT, python_version="3.8.0")

        # The line names the metadata; which version and which Python is the
        # raiser's own sentence, one depth down.
        assert "rejected on its metadata" in str(info.value)
        assert info.value.verbose_message is not None
        assert "requires Python" in info.value.verbose_message


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
        coordinator: FakeFetchPort,
        python_version: str,
    ) -> ResolveResult:
        """Resolve the root project with each member tree as a local source."""
        (tmp_path / "pyproject.toml").write_text(root_body, encoding="utf-8")
        for name, body in members.items():
            member = tmp_path / name
            member.mkdir()
            (member / "pyproject.toml").write_text(body, encoding="utf-8")
        with (
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            return _resolved(
                tmp_path / "pyproject.toml",
                _FAKE_TRANSPORT,
                python_version=python_version,
                inputs=ResolveInputs(
                    local_sources=tuple(
                        LocalSource(name, str(tmp_path / name)) for name in members
                    )
                ),
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


_LINUX_312 = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("linux_x86_64")
)

_PYPY_312 = ResolveTarget.for_declared(
    python_version="3.12", spec=PlatformSpec("linux_x86_64"), implementation="pypy"
)


class TestLockDeclaresItsEnvironment:
    """A single-environment lock declares the environment it was resolved
    for.  Every dependency whose marker was False here was dropped, so an
    installer that answers one of those markers differently needs a
    different package set: PEP 751 ``environments`` refuses it.
    """

    _INPUTS: ClassVar[ResolveInputs] = ResolveInputs(build_policy=BuildPolicy.NEVER)

    @staticmethod
    def _resolve(
        tmp_path: Path,
        body: str,
        coordinator: FakeFetchPort,
        *,
        inputs: ResolveInputs = _INPUTS,
        target: ResolveTarget = _LINUX_312,
    ) -> LockInput:
        """Resolve ``body`` for one declared target and build its lock input."""
        path = tmp_path / "pyproject.toml"
        path.write_text(body, encoding="utf-8")
        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolved(path, _FAKE_TRANSPORT, targets=(target,), inputs=inputs)
        return build_lock_input(result, inputs=inputs)

    _PYPROJECT = (
        '[project]\nname = "proj"\ndependencies = ["foo"]\n'
        '[tool.nab]\nbuild-policy = "never"\n'
        "[tool.nab.environment]\n"
        'python = "3.12"\nplatform = "linux_x86_64"\n'
    )

    @staticmethod
    def _coordinator(requires_dist: str = "") -> FakeFetchPort:
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
        lock_input = self._resolve(
            tmp_path,
            body,
            self._coordinator(),
            inputs=self._INPUTS.replace(
                constraints=("foo<9; implementation_name == 'cpython'",)
            ),
        )
        (environment,) = lock_input.environments
        assert 'implementation_name == "cpython"' in str(environment)

    def test_an_unboundable_marker_warns_and_stays_undeclared(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A kernel-versioned marker names one machine; the lock cannot bound it."""
        with caplog.at_level(logging.WARNING, logger="nab_project.resolve"):
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
        with caplog.at_level(logging.WARNING, logger="nab_project.resolve"):
            lock_input = self._resolve(
                tmp_path,
                body,
                self._coordinator(
                    'Requires-Dist: bar ; implementation_version >= "7.3"\n'
                ),
                target=_PYPY_312,
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
        with caplog.at_level(logging.WARNING, logger="nab_project.resolve"):
            lock_input = self._resolve(
                tmp_path,
                body,
                self._coordinator('Requires-Dist: bar ; sys_platform == "win32"\n'),
                target=_PYPY_312,
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


_BASE_GROUP = '[tool.nab]\nbase-group = "default"\n'


def _pylock_markers(pylock: Pylock) -> dict[str, str | None]:
    """Each emitted package's marker text, or ``None`` where it carries none."""
    return {
        str(pkg.name): str(pkg.marker) if pkg.marker else None
        for pkg in pylock.packages
    }


def _pylock_selected(pylock: Pylock, **kwargs: list[str]) -> set[str]:
    """The package names an install with ``kwargs`` selected would receive."""
    return {str(pkg.name) for pkg, _ in pylock.select(**kwargs)}


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
        sources: tuple[str, ...] = tuple(_MEMBERS),
        inputs: ResolveInputs | None = None,
        targets: tuple[ResolveTarget, ...] | None = None,
    ) -> Pylock:
        """Resolve the root project and emit its lock.

        ``sources`` names the member trees the root declares as local
        sources; the rest of the project's settings arrive as ``inputs``,
        the way a host hands them over.
        """
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
        inputs = (ResolveInputs() if inputs is None else inputs).replace(
            local_sources=tuple(
                LocalSource(name, str(tmp_path / name)) for name in sources
            )
        )
        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: make_coordinator([])
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            path = tmp_path / "pyproject.toml"
            result = _resolved(
                path,
                _FAKE_TRANSPORT,
                inputs=inputs,
                extras=extras,
                groups=groups,
                targets=(ResolveTarget.for_host(),) if targets is None else targets,
            )
        pylock = build_pylock(
            build_lock_input(
                result,
                inputs=inputs,
                extras=extras,
                dependency_groups=groups,
            ),
            lock_dir=tmp_path,
        )
        pylock.validate()
        return pylock

    def test_extra_only_package_carries_extras_membership(self, tmp_path: Path) -> None:
        pylock = self._lock(tmp_path, extras=("cli",), groups=("dev",))

        assert _pylock_markers(pylock) == {
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

        assert _pylock_selected(pylock) == {"core"}
        assert _pylock_selected(pylock, extras=["cli"]) == {"core", "mytool", "subtool"}
        assert _pylock_selected(pylock, dependency_groups=["dev"]) == {"core", "mydev"}
        assert _pylock_selected(pylock, extras=["cli"], dependency_groups=["dev"]) == {
            "core",
            "mydev",
            "mytool",
            "subtool",
        }

    def test_a_group_can_be_selected_without_the_project_dependencies(
        self, tmp_path: Path
    ) -> None:
        """What naming them buys: a lock can be asked for one group.

        The project's own dependencies answer to their own group name, so
        an installer asked for ``dev`` alone gets the group and nothing
        else.  Naming groups replaces the defaults rather than adding to
        them, so an install that wants both asks for both.
        """
        pylock = self._lock(
            tmp_path,
            extras=("cli",),
            groups=("dev",),
            root=self._ROOT + _BASE_GROUP,
            inputs=ResolveInputs(base_group="default"),
        )

        assert _pylock_markers(pylock) == {
            "core": '"default" in dependency_groups',
            "mydev": '"dev" in dependency_groups',
            "mytool": '"cli" in extras',
            "subtool": '"cli" in extras',
        }

        assert _pylock_selected(pylock, dependency_groups=["dev"]) == {"mydev"}
        assert _pylock_selected(pylock, dependency_groups=["default", "dev"]) == {
            "core",
            "mydev",
        }

    def test_package_reached_by_base_and_group_installs_for_either(
        self, tmp_path: Path
    ) -> None:
        """A group that re-requires a project dependency still gets it.

        The package answers to both names, so selecting the group alone
        installs it even though the project's own dependencies are out.
        """
        root = self._ROOT.replace('dev = ["mydev"]', 'dev = ["mydev", "core"]')
        pylock = self._lock(
            tmp_path,
            groups=("dev",),
            root=root + _BASE_GROUP,
            inputs=ResolveInputs(base_group="default"),
        )

        assert _pylock_markers(pylock)["core"] == (
            '"default" in dependency_groups or "dev" in dependency_groups'
        )
        assert _pylock_selected(pylock, dependency_groups=["dev"]) == {"core", "mydev"}

    def test_package_reached_by_base_and_extra_is_unconditional(
        self, tmp_path: Path
    ) -> None:
        """An extra re-requiring a project dependency does not gate it."""
        root = self._ROOT.replace('cli = ["mytool"]', 'cli = ["mytool", "core"]')
        pylock = self._lock(tmp_path, extras=("cli",), root=root)

        assert _pylock_selected(pylock) == {"core"}

    def test_default_group_still_installs_by_default(self, tmp_path: Path) -> None:
        """A ``default-groups`` member gates on the group but installs by default.

        PEP 751 seeds ``dependency_groups`` from ``default-groups`` when
        the installer is given no group selection, so the membership
        marker holds; an installer that explicitly selects no group
        (``dependency_groups=[]``) drops it.
        """
        root = self._ROOT + '[tool.nab]\ndefault-groups = ["dev"]\n'
        pylock = self._lock(
            tmp_path, root=root, inputs=ResolveInputs(default_groups=("dev",))
        )

        assert pylock.default_groups == ("dev",)
        assert _pylock_selected(pylock) == {"core", "mydev"}
        assert _pylock_selected(pylock, dependency_groups=[]) == {"core"}

    def test_declared_default_groups_replace_rather_than_extend(
        self, tmp_path: Path
    ) -> None:
        """Declaring ``default-groups`` drops the base group from them.

        The project chose that selection, so nab does not add to it; the
        name goes back in by being declared there.
        """
        root = self._ROOT + '[tool.nab]\ndefault-groups = ["dev"]\n'
        replaced = self._lock(
            tmp_path,
            root=root + 'base-group = "base"\n',
            inputs=ResolveInputs(base_group="base", default_groups=("dev",)),
        )

        assert replaced.default_groups == ("dev",)
        assert _pylock_selected(replaced) == {"mydev"}

    def test_naming_the_base_group_in_default_groups_keeps_it(
        self, tmp_path: Path
    ) -> None:
        """It is not a declared group, but ``default-groups`` accepts it."""
        root = self._ROOT + '[tool.nab]\ndefault-groups = ["dev", "base"]\n'
        pylock = self._lock(
            tmp_path,
            root=root + 'base-group = "base"\n',
            inputs=ResolveInputs(base_group="base", default_groups=("dev", "base")),
        )

        assert pylock.default_groups == ("base", "dev")
        assert _pylock_selected(pylock) == {"core", "mydev"}

    def test_selecting_the_base_group_by_name_refuses(self, tmp_path: Path) -> None:
        """It is project policy, not a per-run selection.

        ``default-groups`` takes the name; ``--groups`` does not, and
        being silently accepted there would make the flag a no-op.
        """
        with pytest.raises(LookupError, match="'default' not found"):
            self._lock(
                tmp_path,
                groups=("default",),
                root=self._ROOT + _BASE_GROUP,
                inputs=ResolveInputs(base_group="default"),
            )

    def test_a_name_merely_resembling_it_is_allowed(self, tmp_path: Path) -> None:
        """``de_fault`` normalises to ``de-fault``, which collides with nothing."""
        root = self._ROOT.replace(
            '[dependency-groups]\ndev = ["mydev"]',
            '[dependency-groups]\nde_fault = ["mydev"]',
        )
        pylock = self._lock(
            tmp_path,
            groups=("de_fault",),
            root=root + _BASE_GROUP,
            inputs=ResolveInputs(base_group="default"),
        )

        assert pylock.dependency_groups == ("de-fault", "default")
        assert _pylock_selected(pylock, dependency_groups=["de-fault"]) == {"mydev"}

    def test_no_group_offered_names_nothing(self, tmp_path: Path) -> None:
        """With no group to select, nothing needs a name for the project's own."""
        root = self._ROOT.replace(
            '[dependency-groups]\ndev = ["mydev"]',
            '[dependency-groups]\ndefault = ["mydev"]',
        )
        pylock = self._lock(tmp_path, root=root)

        assert pylock.default_groups is None
        assert _pylock_markers(pylock) == {"core": None}

    def test_naming_them_gates_them_with_nothing_selected(self, tmp_path: Path) -> None:
        """The name means one thing whether or not the run selects a group.

        A lock written with no selection still gates the project's own
        dependencies, so an installer reading two locks of the same
        project does not get two answers to the same request.
        """
        pylock = self._lock(
            tmp_path,
            root=self._ROOT + _BASE_GROUP,
            inputs=ResolveInputs(base_group="default"),
        )

        assert _pylock_markers(pylock) == {"core": '"default" in dependency_groups'}
        assert _pylock_selected(pylock) == {"core"}
        assert _pylock_selected(pylock, dependency_groups=[]) == set()

    def test_dynamic_project_dependencies_still_refuse(self, tmp_path: Path) -> None:
        """Setting the option does not open a path around the refusal.

        There is nothing to name when the project's own dependencies need
        a build to compute, and this run stops before that matters.
        """
        root = self._ROOT.replace(
            'dependencies = ["core"]', 'dynamic = ["dependencies"]'
        )
        with pytest.raises(InvalidProjectRequirementError, match="declared dynamic"):
            self._lock(
                tmp_path,
                groups=("dev",),
                root=root + _BASE_GROUP,
                inputs=ResolveInputs(base_group="default"),
            )

    def test_no_selection_leaves_every_package_unmarked(self, tmp_path: Path) -> None:
        pylock = self._lock(tmp_path)

        assert [pkg.marker for pkg in pylock.packages] == [None]
        assert _pylock_selected(pylock) == {"core"}

    def test_marker_excluded_extra_requirement_is_not_locked(
        self, tmp_path: Path
    ) -> None:
        """A requirement the target Python excludes is in no install context."""
        root = self._ROOT.replace(
            'cli = ["mytool"]',
            'cli = ["mytool", "mydev ; python_version < \'3.9\'"]',
        )
        pylock = self._lock(tmp_path, extras=("cli",), root=root)

        assert set(_pylock_markers(pylock)) == {"core", "mytool", "subtool"}

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

        assert _pylock_markers(pylock) == {"core": None, "subtool": '"cli" in extras'}

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
        pylock = self._lock(
            tmp_path,
            extras=("cli",),
            root=root,
            inputs=ResolveInputs(build_policy=BuildPolicy.NEVER),
            targets=Matrix(
                python=">=3.11,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
        )

        assert _pylock_markers(pylock) == {
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
            sources=self._NAMES,
            inputs=ResolveInputs(conflicts=(_extras_conflict("cpu", "gpu"),)),
        )

    def test_shared_package_names_both_selections_that_reach_it(
        self, tmp_path: Path
    ) -> None:
        markers = _pylock_markers(self._lock(tmp_path))

        assert markers["shared-lib"] == '"cpu" in extras or "docs" in extras'
        assert markers["sphinx"] == '"docs" in extras'
        assert markers["core"] is None

    def test_selecting_the_member_alone_installs_what_it_requires(
        self, tmp_path: Path
    ) -> None:
        """``shared-lib`` is a direct requirement of the ``cpu`` extra."""
        pylock = self._lock(tmp_path)
        selected = _pylock_selected

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


class TestIndexCacheFloorOnAWarmResolve:
    """``[tool.nab.index.<name>] assume-fresh-seconds`` reaches the resolve.

    The index stamps ``max-age=0``, so its listing is stale the moment it is
    cached and a second resolve would revalidate it. The floor serves the
    cached copy instead, hiding a release published inside the window. A
    sibling project without the key is the control.
    """

    _INDEX = "https://internal.example/simple/"
    _FILES = "https://files.example.com/"

    @classmethod
    def _listing(cls, *versions: str) -> httpx.Response:
        """``foo``'s Simple listing, stale as soon as it is cached."""
        return httpx.Response(
            200,
            json={
                "meta": {"api-version": "1.0"},
                "name": "foo",
                "files": [
                    {
                        "filename": f"foo-{version}-py3-none-any.whl",
                        "url": f"{cls._FILES}foo-{version}-py3-none-any.whl",
                        "core-metadata": True,
                    }
                    for version in versions
                ],
            },
            headers={"Cache-Control": "max-age=0"},
        )

    @classmethod
    def _project(cls, root: Path, *, floor: bool) -> Path:
        """Write a project on the ``internal`` index and return its pyproject.

        ``floor`` adds ``assume-fresh-seconds`` for that index.
        """
        root.mkdir()
        text = (
            '[project]\nname = "app"\nversion = "0"\ndependencies = ["foo"]\n'
            f'[[tool.nab.indexes]]\nname = "internal"\nurl = "{cls._INDEX}"\n'
        )
        if floor:
            text += "[tool.nab.index.internal]\nassume-fresh-seconds = 3600\n"
        pyproject = root / "pyproject.toml"
        pyproject.write_text(text, encoding="utf-8")
        return pyproject

    @classmethod
    def _resolve_pins(
        cls, pyproject: Path, cache_dir: Path, *, floor: bool
    ) -> dict[str, Version]:
        """The pins of a resolve whose coordinator reads and writes ``cache_dir``."""
        overrides = (
            {"internal": IndexOverride(assume_fresh_seconds=3600)} if floor else {}
        )
        transport = HttpxAsyncTransport()
        try:
            result = _resolved(
                pyproject,
                transport,
                cache_dir=cache_dir,
                python_version="3.12.0",
                inputs=ResolveInputs(
                    indexes=(IndexConfig("internal", cls._INDEX),),
                    index_overrides=overrides,
                ),
            )
        finally:
            asyncio.run(transport.aclose())
        return _pins(result)

    @respx.mock
    def test_floor_hides_a_release_published_inside_the_window(
        self, tmp_path: Path
    ) -> None:
        """A floored index serves its cached listing; a plain one refetches."""
        listing = respx.get(f"{self._INDEX}foo/").mock(
            return_value=self._listing("1.0")
        )
        for version in ("1.0", "2.0"):
            respx.get(f"{self._FILES}foo-{version}-py3-none-any.whl.metadata").mock(
                return_value=httpx.Response(
                    200,
                    text=f"Metadata-Version: 2.1\nName: foo\nVersion: {version}\n",
                )
            )

        floored = self._project(tmp_path / "floored", floor=True)
        plain = self._project(tmp_path / "plain", floor=False)
        floored_cache = tmp_path / "cache-floored"
        plain_cache = tmp_path / "cache-plain"

        # Warm both caches on the 1.0 listing.
        assert self._resolve_pins(floored, floored_cache, floor=True) == {
            "foo": V("1.0")
        }
        assert self._resolve_pins(plain, plain_cache, floor=False) == {"foo": V("1.0")}
        warm_requests = listing.call_count

        # foo 2.0 is published, inside the floored index's window.
        listing.mock(return_value=self._listing("1.0", "2.0"))

        assert self._resolve_pins(floored, floored_cache, floor=True) == {
            "foo": V("1.0")
        }
        assert listing.call_count == warm_requests

        assert self._resolve_pins(plain, plain_cache, floor=False) == {"foo": V("2.0")}
        assert listing.call_count == warm_requests + 1


class _RecordingSink:
    """A :class:`~nab_project.resolve.ProgressSink` that records its calls."""

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


class TestBuildRequirementsResolve:
    """``build_requirements`` swaps the roots for ``[build-system].requires``."""

    @staticmethod
    def _mocked_resolve(pyproject: Path, **kwargs: object) -> ResolveResult:
        """Resolve ``pyproject`` against a provider that pins everything at 2.0."""
        with (
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("2.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            return _resolved(
                pyproject, _FAKE_TRANSPORT, python_version="3.12.0", **kwargs
            )

    def test_build_requires_replace_project_dependencies(self, tmp_path: Path) -> None:
        """The project's own dependencies are not in a build lock."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["runtime-only"]\n'
            '[build-system]\nrequires = ["hatchling"]\n'
        )

        result = self._mocked_resolve(pyproject, build_requirements=True)

        assert _pins(result) == {"hatchling": V("2.0")}

    def test_project_dependencies_are_locked_without_the_flag(
        self, tmp_path: Path
    ) -> None:
        """The same project locks its runtime deps when the flag is off."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["runtime-only"]\n'
            '[build-system]\nrequires = ["hatchling"]\n'
        )

        result = self._mocked_resolve(pyproject)

        assert _pins(result) == {"runtime-only": V("2.0")}

    def test_default_groups_do_not_reach_a_build_lock(self, tmp_path: Path) -> None:
        """A project's default group describes its runtime, not its build."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = []\n'
            '[build-system]\nrequires = ["hatchling"]\n'
            "[dependency-groups]\n"
            'dev = ["pytest"]\n'
            "[tool.nab]\n"
            'default-groups = ["dev"]\n'
        )

        result = self._mocked_resolve(pyproject, build_requirements=True)

        assert _pins(result) == {"hatchling": V("2.0")}

    def test_conflicts_over_absent_groups_do_not_refuse_the_run(
        self, tmp_path: Path
    ) -> None:
        """Conflicts are declared over a selection a build lock does not have."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = []\n'
            '[build-system]\nrequires = ["hatchling"]\n'
            "[dependency-groups]\n"
            'cpu = ["torch-cpu"]\n'
            'gpu = ["torch-gpu"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ group = "cpu" }, { group = "gpu" }]]\n'
        )

        result = self._mocked_resolve(pyproject, build_requirements=True)

        assert _pins(result) == {"hatchling": V("2.0")}

    @patch("nab_project.resolve.resolve_with_coordinator")
    def test_every_target_gets_the_build_requires(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """A static requires list needs no interpreter to read, so it goes wide."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["runtime-only"]\n'
            '[build-system]\nrequires = ["hatchling"]\n'
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
        )

        resolve_for_targets(
            pyproject,
            _FAKE_TRANSPORT,
            targets=(
                ResolveTarget.for_declared(
                    python_version="3.11", spec=PlatformSpec("linux_x86_64")
                ),
                ResolveTarget.for_declared(
                    python_version="3.12", spec=PlatformSpec("linux_x86_64")
                ),
            ),
            build_requirements=True,
        )

        targets = mock_engine.call_args.args[1]
        assert [t.label for t in targets] == [
            "py311-linux_x86_64",
            "py312-linux_x86_64",
        ]
        (fork,) = mock_engine.call_args.kwargs["forks"]
        assert [str(r) for r in fork.requirements] == ["hatchling"]

    def test_no_build_system_is_an_error(self, tmp_path: Path) -> None:
        """The PEP 517 default backend is a fallback, not a thing to pin."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "proj"\ndependencies = ["foo"]\n')

        with pytest.raises(
            InvalidProjectRequirementError, match=r"declares no \[build-system\]"
        ):
            self._mocked_resolve(pyproject, build_requirements=True)

    @pytest.mark.parametrize("selection", [{"groups": ("dev",)}, {"extras": ("gpu",)}])
    def test_a_selection_is_refused(
        self, tmp_path: Path, selection: dict[str, tuple[str, ...]]
    ) -> None:
        """Neither groups nor extras mean anything to a build lock."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[build-system]\nrequires = ["hatchling"]\n')

        with pytest.raises(ValueError, match="no groups or extras"):
            self._mocked_resolve(pyproject, build_requirements=True, **selection)


class TestBuildRequirementsConfig:
    def test_drops_every_selection_setting(self) -> None:
        """Nothing describing a group or extra survives into a build lock."""
        inputs = ResolveInputs(
            default_groups=("dev",),
            base_group="default",
            conflicts=(
                ConflictSet(
                    members=(
                        ConflictMember(kind=ConflictKind.GROUP, name="cpu"),
                        ConflictMember(kind=ConflictKind.GROUP, name="gpu"),
                    )
                ),
            ),
        )

        pruned = inputs_for_build_requirements(inputs)

        assert pruned.default_groups == ()
        assert pruned.base_group is None
        assert pruned.conflicts == ()

    def test_keeps_the_settings_a_resolve_still_needs(self) -> None:
        """Constraints and the resolve window are not part of the selection."""
        inputs = ResolveInputs(constraints=("urllib3<2",), requires_python=">=3.10")

        pruned = inputs_for_build_requirements(inputs)

        assert pruned.constraints == ("urllib3<2",)
        assert pruned.requires_python == ">=3.10"


_BOTH_GROUPS = '[tool.nab]\nbase-group = "main"\nbuild-group = "build"\n'
"""Naming the build requirements needs a name for the rest, or they would
install alongside every group."""


class TestBuildGroup:
    """``[tool.nab].build-group`` carries the build requirements in the lock."""

    _PYPROJECT = (
        '[project]\nname = "proj"\ndependencies = ["runtime-only"]\n'
        '[build-system]\nrequires = ["hatchling"]\n'
    )

    @staticmethod
    def _mocked_resolve(pyproject: Path, **kwargs: object) -> ResolveResult:
        """Resolve ``pyproject`` against a provider that pins everything at 2.0."""
        with (
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
            patch("nab_project._resolve.engine.Provider") as mock_provider_cls,
            patch("nab_project._resolve.engine.build_target_lock"),
        ):
            mock_coord_cls.return_value.__enter__ = lambda s: s
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_provider = mock_provider_cls.return_value
            mock_provider.choose_version.return_value = V("2.0")
            mock_provider.get_dependencies.return_value = {}
            mock_provider.prioritize.return_value = 1
            return _resolved(
                pyproject, _FAKE_TRANSPORT, python_version="3.12.0", **kwargs
            )

    def test_build_requires_join_the_resolve(self, tmp_path: Path) -> None:
        """Naming a group puts the build requirements in the lock."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(self._PYPROJECT + _BOTH_GROUPS)

        result = self._mocked_resolve(
            pyproject, inputs=ResolveInputs(base_group="main", build_group="build")
        )

        assert _pins(result) == {"runtime-only": V("2.0"), "hatchling": V("2.0")}

    def test_unset_leaves_them_out(self, tmp_path: Path) -> None:
        """A lock says nothing about how the project is built by default."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(self._PYPROJECT)

        result = self._mocked_resolve(pyproject)

        assert _pins(result) == {"runtime-only": V("2.0")}

    def test_no_build_system_is_an_error(self, tmp_path: Path) -> None:
        """Naming a group for requirements the project does not declare."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = []\n'
            "[tool.nab]\n"
            'base-group = "main"\n'
            'build-group = "build"\n'
        )

        with pytest.raises(
            InvalidProjectRequirementError, match=r"declares no \[build-system\]"
        ):
            self._mocked_resolve(
                pyproject,
                inputs=ResolveInputs(base_group="main", build_group="build"),
            )

    def test_a_build_requirements_lock_drops_the_group(self, tmp_path: Path) -> None:
        """Its roots already are the build requirements, so nothing gates them.

        The pins are the same either way, so what the group would change is
        the lock offering a name that gates nothing.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(self._PYPROJECT + _BOTH_GROUPS)
        inputs = inputs_for_build_requirements(
            ResolveInputs(base_group="main", build_group="build")
        )

        result = self._mocked_resolve(pyproject, build_requirements=True)
        lock_input = build_lock_input(result, inputs=inputs)

        assert _pins(result) == {"hatchling": V("2.0")}
        assert lock_input.build_group is None
        assert lock_input.active_groups == ()

    @patch("nab_project.resolve.resolve_with_coordinator")
    def test_build_requires_join_every_conflict_fork(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """A project is built the same way whichever member is selected."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            self._PYPROJECT + "[dependency-groups]\n"
            'cpu = ["torch-cpu"]\n'
            'gpu = ["torch-gpu"]\n'
            "[tool.nab]\n"
            'base-group = "main"\n'
            'build-group = "build"\n'
            'conflicts = [[{ group = "cpu" }, { group = "gpu" }]]\n'
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda coordinator: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                groups=("cpu", "gpu"),
                python_version="3.12.0",
                inputs=ResolveInputs(
                    base_group="main",
                    build_group="build",
                    conflicts=(_groups_conflict("cpu", "gpu"),),
                ),
            )

        forks = mock_engine.call_args.kwargs["forks"]
        assert len(forks) == 2
        for fork in forks:
            assert "hatchling" in {r.name for r in fork.requirements}
            assert ("group", "build") in fork.contexts.selectors

    @patch("nab_project.resolve.resolve_with_coordinator")
    def test_the_matrix_still_expands(
        self, mock_engine: MagicMock, tmp_path: Path
    ) -> None:
        """A static requires list needs no interpreter to read, so it goes wide."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            self._PYPROJECT + "[tool.nab]\n"
            'base-group = "main"\n'
            'build-group = "build"\n'
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n'
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda coordinator: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                inputs=ResolveInputs(
                    base_group="main",
                    build_group="build",
                    build_policy=BuildPolicy.NEVER,
                ),
                targets=Matrix(
                    python=">=3.11,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
                ).expand(),
            )

        targets = mock_engine.call_args.args[1]
        assert [t.label for t in targets] == [
            "py311-linux_x86_64",
            "py312-linux_x86_64",
        ]
        (fork,) = mock_engine.call_args.kwargs["forks"]
        assert [str(r) for r in fork.requirements] == ["runtime-only", "hatchling"]


class TestBuildGroupMarkers:
    """``build-group`` gates ``[build-system].requires`` end to end."""

    _MEMBERS: ClassVar[dict[str, str]] = {
        "core": '[project]\nname = "core"\nversion = "1.0"\n',
        "mydev": '[project]\nname = "mydev"\nversion = "3.0"\n',
        "builder": '[project]\nname = "builder"\nversion = "5.0"\n',
    }

    _ROOT = (
        '[project]\nname = "app"\nversion = "1.0"\ndependencies = ["core"]\n'
        '[dependency-groups]\ndev = ["mydev"]\n'
        '[build-system]\nrequires = ["builder"]\n'
        + "".join(
            f'[[tool.nab.local-sources]]\nname = "{name}"\npath = "{name}"\n'
            for name in ("core", "mydev", "builder")
        )
    )

    def _lock(
        self, tmp_path: Path, *, tool: str, groups: tuple[str, ...] = ()
    ) -> Pylock:
        """Resolve and emit the root project, with ``tool`` appended to it."""
        return TestExtraAndGroupMembershipMarkers._lock(
            tmp_path,
            groups=groups,
            root=self._ROOT + tool,
            members=self._MEMBERS,
            sources=tuple(self._MEMBERS),
            inputs=ResolveInputs(base_group="main", build_group="build"),
        )

    def test_each_side_gates_on_its_own_name(self, tmp_path: Path) -> None:
        """Only the build group reaches the build requirement."""
        pylock = self._lock(tmp_path, tool=_BOTH_GROUPS)

        assert _pylock_markers(pylock) == {
            "core": '"main" in dependency_groups',
            "builder": '"build" in dependency_groups',
        }

    def test_the_build_name_is_selectable_but_not_a_default(
        self, tmp_path: Path
    ) -> None:
        """An install that asks for no group is installing, not building."""
        pylock = self._lock(tmp_path, tool=_BOTH_GROUPS)

        assert pylock.dependency_groups == ("build", "main")
        assert pylock.default_groups == ("main",)

        assert _pylock_selected(pylock) == {"core"}

    def test_the_build_requirements_can_be_asked_for_alone(
        self, tmp_path: Path
    ) -> None:
        """Naming the rest is what makes the build side selectable on its own."""
        pylock = self._lock(tmp_path, tool=_BOTH_GROUPS)

        assert _pylock_selected(pylock, dependency_groups=["build"]) == {"builder"}
        assert _pylock_selected(pylock, dependency_groups=["main"]) == {"core"}
        assert _pylock_selected(pylock, dependency_groups=["main", "build"]) == {
            "core",
            "builder",
        }

    def test_a_selected_group_keeps_its_own_gate(self, tmp_path: Path) -> None:
        """The build group is one selector among the run's own selection."""
        pylock = self._lock(tmp_path, tool=_BOTH_GROUPS, groups=("dev",))

        assert _pylock_markers(pylock)["mydev"] == '"dev" in dependency_groups'
        assert _pylock_selected(pylock, dependency_groups=["dev"]) == {"mydev"}


class TestConfiguredGroupConflicts:
    """A conflict may name ``base-group`` or ``build-group``, which forks."""

    _ROOT = (
        '[project]\nname = "proj"\nversion = "1.0"\n'
        'dependencies = ["packaging<24", "requests"]\n'
        '[build-system]\nrequires = ["setuptools>=70", "packaging>=24"]\n'
        "[dependency-groups]\n"
        'dev = ["pytest"]\n'
        "[tool.nab]\n"
        'base-group = "main"\n'
        'build-group = "build"\n'
    )

    _CONFLICT = 'conflicts = [[{ group = "main" }, { group = "build" }]]\n'

    _INPUTS: ClassVar[ResolveInputs] = ResolveInputs(
        base_group="main", build_group="build"
    )

    @staticmethod
    def _planned(
        pyproject: Path, *, inputs: ResolveInputs = _INPUTS, **kwargs: object
    ) -> MagicMock:
        """Resolve far enough to capture the fork plan, without an index."""
        with (
            patch("nab_project.resolve.resolve_with_coordinator") as mock_engine,
            patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls,
        ):
            mock_coord_cls.return_value.__enter__ = lambda coordinator: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            resolve_for_targets(
                pyproject,
                _FAKE_TRANSPORT,
                targets=(ResolveTarget.for_host_python("3.12.0"),),
                inputs=inputs,
                **kwargs,
            )
        return mock_engine

    def _forks(
        self,
        tmp_path: Path,
        tool: str,
        *,
        conflicts: tuple[ConflictSet, ...] = (),
        **kwargs: object,
    ) -> list[ResolveFork]:
        """The forks the root project plans under ``conflicts``."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(self._ROOT + tool)
        return self._planned(
            pyproject, inputs=self._INPUTS.replace(conflicts=conflicts), **kwargs
        ).call_args.kwargs["forks"]

    def test_the_two_sides_resolve_separately(self, tmp_path: Path) -> None:
        """Each fork carries one context, so neither constrains the other."""
        forks = self._forks(
            tmp_path, self._CONFLICT, conflicts=(_groups_conflict("main", "build"),)
        )

        assert [f.selection for f in forks] == [
            (("group", "main"),),
            (("group", "build"),),
        ]
        assert [str(r) for r in forks[0].requirements] == ["packaging<24", "requests"]
        assert [str(r) for r in forks[1].requirements] == [
            "setuptools>=70",
            "packaging>=24",
        ]

    def test_each_fork_claims_only_the_context_it_walked(self, tmp_path: Path) -> None:
        """A fork that never resolved the build requirements has no gate for them."""
        main_fork, build_fork = self._forks(
            tmp_path, self._CONFLICT, conflicts=(_groups_conflict("main", "build"),)
        )

        assert [str(r) for r in main_fork.contexts.project] == [
            "packaging<24",
            "requests",
        ]
        assert ("group", "build") not in main_fork.contexts.selectors

        assert build_fork.contexts.project == ()
        assert ("group", "build") in build_fork.contexts.selectors

    def test_the_base_pass_carries_neither_side(self, tmp_path: Path) -> None:
        """With both contexts conflicted, no dependency is a base dependency.

        That is what lets the two forks pin one package differently: a
        divergence is only refused for a package the base pass named.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(self._ROOT + self._CONFLICT)

        engine = self._planned(
            pyproject,
            inputs=self._INPUTS.replace(conflicts=(_groups_conflict("main", "build"),)),
        )

        assert engine.call_args.kwargs["base_requirements"] == []

    def test_without_the_conflict_both_sides_share_one_resolve(
        self, tmp_path: Path
    ) -> None:
        """The default is co-resolution, which is what one version space means."""
        (fork,) = self._forks(tmp_path, "")

        assert fork.selection == ()
        assert [str(r) for r in fork.requirements] == [
            "packaging<24",
            "requests",
            "setuptools>=70",
            "packaging>=24",
        ]

    def test_a_build_group_may_conflict_with_a_declared_group(
        self, tmp_path: Path
    ) -> None:
        """The request's other half: build against any other dependency group."""
        forks = self._forks(
            tmp_path,
            'conflicts = [[{ group = "build" }, { group = "dev" }]]\n',
            conflicts=(_groups_conflict("build", "dev"),),
            groups=("dev",),
        )

        assert [f.selection for f in forks] == [
            (("group", "build"),),
            (("group", "dev"),),
        ]
        build_fork, dev_fork = forks
        assert "setuptools>=70" in {str(r) for r in build_fork.requirements}
        assert "pytest" not in {str(r) for r in build_fork.requirements}
        assert "pytest" in {str(r) for r in dev_fork.requirements}
        assert "setuptools>=70" not in {str(r) for r in dev_fork.requirements}

    def test_the_project_dependencies_stay_in_every_fork_of_that_set(
        self, tmp_path: Path
    ) -> None:
        """Only conflicting base-group moves the project's own dependencies."""
        forks = self._forks(
            tmp_path,
            'conflicts = [[{ group = "build" }, { group = "dev" }]]\n',
            conflicts=(_groups_conflict("build", "dev"),),
            groups=("dev",),
        )

        for fork in forks:
            assert "packaging<24" in {str(r) for r in fork.requirements}

    def test_a_three_member_set_forks_three_ways(self, tmp_path: Path) -> None:
        """The spelling the docs give for conflicting all three at once."""
        forks = self._forks(
            tmp_path,
            'conflicts = [[{ group = "main" }, { group = "build" },'
            ' { group = "dev" }]]\n',
            conflicts=(_groups_conflict("main", "build", "dev"),),
            groups=("dev",),
        )

        assert [f.selection for f in forks] == [
            (("group", "main"),),
            (("group", "build"),),
            (("group", "dev"),),
        ]
        assert forks[2].contexts.project == ()

    def test_an_exactly_one_set_is_satisfied_by_a_configured_member(
        self, tmp_path: Path
    ) -> None:
        """A configured member counts towards the minimum without being selected."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            self._ROOT + "conflicts = [{ members = ["
            '{ group = "main" }, { group = "build" }],'
            ' policy = "exactly-one" }]\n'
        )

        forks = self._planned(
            pyproject,
            inputs=self._INPUTS.replace(
                conflicts=(
                    _groups_conflict(
                        "main", "build", policy=ConflictPolicy.EXACTLY_ONE
                    ),
                )
            ),
        ).call_args.kwargs["forks"]

        assert [f.selection for f in forks] == [
            (("group", "main"),),
            (("group", "build"),),
        ]

    def test_an_umbrella_group_reaching_a_conflicted_member_is_refused(
        self, tmp_path: Path
    ) -> None:
        """A fork can only carry a member the run selected directly.

        The umbrella reaches ``dev`` without naming it, so this fork would
        carry the project's own dependencies and ``dev`` together, which
        the declaration says cannot happen.
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\nversion = "1.0"\ndependencies = ["core"]\n'
            "[dependency-groups]\n"
            'dev = ["mydev"]\n'
            'all = [{ include-group = "dev" }]\n'
            "[tool.nab]\n"
            'base-group = "main"\n'
            'conflicts = [[{ group = "main" }, { group = "dev" }]]\n'
        )

        with pytest.raises(ConflictSelectionError, match="cannot be selected together"):
            self._planned(
                pyproject,
                groups=("all",),
                inputs=ResolveInputs(
                    base_group="main",
                    conflicts=(_groups_conflict("main", "dev"),),
                ),
            )

    def test_a_near_miss_for_a_configured_name_still_raises(
        self, tmp_path: Path
    ) -> None:
        """Widening the known names must not let an inert member through."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            self._ROOT
            + 'conflicts = [[{ group = "build-tools" }, { group = "dev" }]]\n'
        )

        with pytest.raises(ConfigError, match="build-tools"):
            self._planned(
                pyproject,
                groups=("dev",),
                inputs=self._INPUTS.replace(
                    conflicts=(_groups_conflict("build-tools", "dev"),)
                ),
            )

    def test_an_unset_configured_name_is_not_known(self, tmp_path: Path) -> None:
        """``build`` names nothing when build-group is unset."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\nversion = "1.0"\ndependencies = []\n'
            "[dependency-groups]\n"
            'dev = ["pytest"]\n'
            "[tool.nab]\n"
            'conflicts = [[{ group = "build" }, { group = "dev" }]]\n'
        )

        with pytest.raises(ConfigError, match="group 'build'"):
            self._planned(
                pyproject,
                groups=("dev",),
                inputs=ResolveInputs(conflicts=(_groups_conflict("build", "dev"),)),
            )


class TestConfiguredGroupConflictMarkers:
    """A declared main/build conflict emits one lock with two version spaces."""

    _MEMBERS: ClassVar[dict[str, str]] = {
        "core": '[project]\nname = "core"\nversion = "1.0"\n',
        "builder": '[project]\nname = "builder"\nversion = "5.0"\n',
    }

    _ROOT = (
        '[project]\nname = "app"\nversion = "1.0"\ndependencies = ["core"]\n'
        '[build-system]\nrequires = ["builder"]\n'
        "[tool.nab]\n"
        'base-group = "main"\n'
        'build-group = "build"\n'
        'conflicts = [[{ group = "main" }, { group = "build" }]]\n'
        + "".join(
            f'[[tool.nab.local-sources]]\nname = "{name}"\npath = "{name}"\n'
            for name in ("core", "builder")
        )
    )

    def _lock(self, tmp_path: Path) -> Pylock:
        return TestExtraAndGroupMembershipMarkers._lock(
            tmp_path,
            root=self._ROOT,
            members=self._MEMBERS,
            sources=tuple(self._MEMBERS),
            inputs=ResolveInputs(
                base_group="main",
                build_group="build",
                conflicts=(_groups_conflict("main", "build"),),
            ),
        )

    def test_each_side_gates_on_its_own_name_and_negates_the_other(
        self, tmp_path: Path
    ) -> None:
        """Naming both is what keeps a co-selecting installer to the overlap."""
        markers = _pylock_markers(self._lock(tmp_path))

        assert markers["core"] is not None
        assert '"main" in dependency_groups' in markers["core"]
        assert '"build" not in dependency_groups' in markers["core"]

        assert markers["builder"] is not None
        assert '"build" in dependency_groups' in markers["builder"]
        assert '"main" not in dependency_groups' in markers["builder"]

    def test_an_installer_gets_one_side_or_the_other(self, tmp_path: Path) -> None:
        """The point of the declaration: the two never install together."""
        pylock = self._lock(tmp_path)

        assert _pylock_selected(pylock) == {"core"}
        assert _pylock_selected(pylock, dependency_groups=["main"]) == {"core"}
        assert _pylock_selected(pylock, dependency_groups=["build"]) == {"builder"}

        # Asking for both is the context the declaration says cannot exist,
        # and each side's negation is what leaves it holding neither.
        assert _pylock_selected(pylock, dependency_groups=["main", "build"]) == set()

    def test_the_lock_offers_both_names(self, tmp_path: Path) -> None:
        """Only the project's own dependencies are a default install."""
        pylock = self._lock(tmp_path)

        assert pylock.dependency_groups == ("build", "main")
        assert pylock.default_groups == ("main",)


class TestConfiguredGroupConflictDivergentPins:
    """The case the declaration exists for: one package, two pins, one lock."""

    _ROOT = (
        '[project]\nname = "app"\nversion = "1.0"\n'
        'dependencies = ["packaging<24"]\n'
        '[build-system]\nrequires = ["packaging>=24"]\n'
        "[tool.nab]\n"
        'base-group = "main"\n'
        'build-group = "build"\n'
    )

    _CONFLICT = 'conflicts = [[{ group = "main" }, { group = "build" }]]\n'

    @staticmethod
    def _wheel(version: str) -> WheelFile:
        return WheelFile(
            filename=f"packaging-{version}-py3-none-any.whl",
            url=f"https://example.com/packaging-{version}.whl",
            version=version,
            requires_python=None,
            has_metadata=True,
            upload_time=None,
            hashes=(("sha256", "a" * 64),),
        )

    def _resolved_lock(self, tmp_path: Path) -> Pylock:
        """Resolve against an index carrying both versions, and emit the lock."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(self._ROOT + self._CONFLICT)
        coordinator = make_coordinator(
            [self._wheel("23.2"), self._wheel("24.2")],
            package="packaging",
            auto_metadata=True,
        )
        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            inputs = ResolveInputs(
                base_group="main",
                build_group="build",
                conflicts=(_groups_conflict("main", "build"),),
            )
            result = _resolved(pyproject, _FAKE_TRANSPORT, inputs=inputs)
        pylock = build_pylock(
            build_lock_input(result, inputs=inputs), lock_dir=tmp_path
        )
        pylock.validate()
        return pylock

    def test_without_the_conflict_one_version_space_cannot_hold_both(
        self, tmp_path: Path
    ) -> None:
        """Co-resolution is the default, and this project has no solution."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(self._ROOT)
        coordinator = make_coordinator(
            [self._wheel("23.2"), self._wheel("24.2")],
            package="packaging",
            auto_metadata=True,
        )

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ResolutionError):
                _resolved(
                    pyproject,
                    _FAKE_TRANSPORT,
                    inputs=ResolveInputs(base_group="main", build_group="build"),
                )

    def test_the_conflict_gives_each_side_its_own_pin(self, tmp_path: Path) -> None:
        """Two entries for one name, disjoint on the group clause."""
        pylock = self._resolved_lock(tmp_path)

        entries = sorted(
            (str(pkg.version), str(pkg.marker))
            for pkg in pylock.packages
            if str(pkg.name) == "packaging"
        )
        assert len(entries) == 2

        runtime, build = entries
        assert runtime[0] == "23.2"
        assert '"main" in dependency_groups' in runtime[1]
        assert build[0] == "24.2"
        assert '"build" in dependency_groups' in build[1]

    def test_an_installer_gets_the_right_one(self, tmp_path: Path) -> None:
        """The two sides never install together, which is what was declared."""
        pylock = self._resolved_lock(tmp_path)

        def versions(**kwargs: list[str]) -> set[str]:
            return {str(pkg.version) for pkg, _ in pylock.select(**kwargs)}

        assert versions() == {"23.2"}
        assert versions(dependency_groups=["main"]) == {"23.2"}
        assert versions(dependency_groups=["build"]) == {"24.2"}


class _NoIndexTransport:
    """Transport that fails the test on any request: this resolve needs no index."""

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> NoReturn:
        msg = f"unexpected index request to {url}"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        return None


class TestBuildConfigPlumbing:
    """A resolve's own config is the one its PEP 517 builds run under.

    Only a dynamic-metadata source reaches a build: the static reader
    returns ``None`` for it, so materialising the source falls through to
    ``build_backend.extract_metadata``, which refuses without a config.
    The backend run itself is stubbed, so no build venv is created.
    """

    def _project(self, tmp_path: Path) -> Path:
        """Write a project whose only dependency is a dynamic local source."""
        source = tmp_path / "dyn"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            '[project]\nname = "dyn"\ndynamic = ["version"]\n', encoding="utf-8"
        )

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\nversion = "0"\ndependencies = ["dyn"]\n'
            "[tool.nab]\n"
            'build-policy = "build-local"\n'
            "[[tool.nab.local-sources]]\n"
            'name = "dyn"\n'
            'path = "dyn"\n',
            encoding="utf-8",
        )
        return pyproject

    def test_dynamic_local_source_builds_under_the_project_config(
        self, tmp_path: Path
    ) -> None:
        """``resolve_for_targets`` hands its own settings to the build."""
        pyproject = self._project(tmp_path)
        inputs = ResolveInputs(
            local_sources=(LocalSource("dyn", str(tmp_path / "dyn")),)
        )
        built = WheelMetadata(name="dyn", version=Version("7.0"))

        with patch(
            "nab_project._build.runner.run_build_backend", return_value=built
        ) as runner:
            result = resolve_for_targets(
                pyproject,
                _NoIndexTransport(),
                targets=(ResolveTarget.for_host(),),
                inputs=inputs,
                cache_dir=tmp_path / "cache",
            )

        assert result.success
        assert _pins(result) == {"dyn": Version("7.0")}

        assert runner.call_args.kwargs["config"] == inputs


class TestTrustUnverifiedSdistDeps:
    """``dist-policy.trust-unverified-deps`` reaches the resolve.

    ``foo`` is served only as an sdist, its ``PKG-INFO`` predates :pep:`643`, and it
    carries no ``pyproject.toml`` to fall back on. ``build-policy = "never"`` then bars
    the build, so the flag decides whether ``foo``'s ``Requires-Dist`` line is read.
    """

    _PKG_INFO = "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar\n"

    @staticmethod
    def _pyproject(tmp_path: Path, *, trust: bool) -> Path:
        """A project depending on ``foo``, with the opt-out set to ``trust``."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\ndependencies = ["foo"]\n'
            '[tool.nab]\nbuild-policy = "never"\n'
            'dist-policy = { policy = "wheel-or-sdist",'
            f" trust-unverified-deps = {str(trust).lower()}"
            " }\n",
            encoding="utf-8",
        )
        return pyproject

    @classmethod
    def _coordinator(cls) -> FakeFetchPort:
        """An index serving ``foo`` as a lone sdist and ``bar`` as a wheel."""
        sdist = SdistFile(
            filename="foo-1.0.tar.gz",
            url="https://example.com/foo-1.0.tar.gz",
            version="1.0",
            requires_python=None,
            upload_time=None,
        )
        return make_coordinator(
            listings={"foo": [sdist], "bar": _index_wheels("bar", "1.0")},
            sdist_pkg_info=cls._PKG_INFO,
            auto_metadata=True,
        )

    def _resolve(self, tmp_path: Path, *, trust: bool) -> ResolveResult:
        """Resolve the project against that index, with the opt-out set to ``trust``."""
        pyproject = self._pyproject(tmp_path, trust=trust)
        coordinator = self._coordinator()

        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = lambda _self: coordinator
            mock_coord_cls.return_value.__exit__ = MagicMock(return_value=False)
            return _resolved(
                pyproject,
                _FAKE_TRANSPORT,
                inputs=ResolveInputs(
                    build_policy=BuildPolicy.NEVER,
                    dist_policy=DistPolicy.WHEEL_OR_SDIST,
                    trust_unverified_sdist_deps=trust,
                ),
            )

    def test_trusting_the_pkg_info_reads_its_requires_dist(
        self, tmp_path: Path
    ) -> None:
        """With the opt-out set, ``foo``'s unverified ``Requires-Dist`` is honoured."""
        result = self._resolve(tmp_path, trust=True)

        assert _pins(result) == {"foo": V("1.0"), "bar": V("1.0")}

    def test_without_the_opt_out_the_sdist_has_no_usable_metadata(
        self, tmp_path: Path
    ) -> None:
        """With the opt-out off, the pre-2.2 ``PKG-INFO`` supplies nothing.

        The sdist's own reason lands in the detailed diagnostics block, the one
        ``nab lock -v`` prints, not in ``str(exc)``.
        """
        with pytest.raises(ResolutionError) as info:
            self._resolve(tmp_path, trust=False)

        detail = info.value.verbose_message
        assert detail is not None
        assert (
            "foo==1.0 sdist has dynamic dependencies and no static"
            " pyproject.toml fallback" in detail
        )


class TestPyprojectParsedOnce:
    """Every table a resolve reads comes off one parse of the file."""

    def test_a_specific_resolve_parses_the_pyproject_once(
        self,
        record_parses: Callable[[], AbstractContextManager[list[str]]],
        tmp_path: Path,
    ) -> None:
        """Dependencies, groups, extras, name and build requires share a parse."""
        body = (
            '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
            "[project.optional-dependencies]\n"
            "cpu = []\n"
            "[dependency-groups]\n"
            "dev = []\n"
            "[build-system]\n"
            'requires = []\nbuild-backend = "hatchling.build"\n'
            "[tool.nab]\n"
            'base-group = "runtime"\nbuild-group = "build"\n'
        )
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_bytes(body.encode())
        with record_parses() as parsed:
            _resolved(
                pyproject,
                inputs=ResolveInputs(base_group="runtime", build_group="build"),
            )

        assert parsed.count(body) == 1

    def test_a_build_requirements_resolve_parses_the_pyproject_once(
        self,
        record_parses: Callable[[], AbstractContextManager[list[str]]],
        tmp_path: Path,
    ) -> None:
        """The build-requirements path reads its one table off that parse too."""
        body = (
            '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
            "[build-system]\n"
            'requires = []\nbuild-backend = "hatchling.build"\n'
        )
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_bytes(body.encode())
        with record_parses() as parsed:
            resolve_for_targets(
                pyproject,
                _FAKE_TRANSPORT,  # type: ignore[arg-type]
                targets=(ResolveTarget.for_host(),),
                inputs=ResolveInputs(),
                build_requirements=True,
            ).raise_for_failure()

        assert parsed.count(body) == 1


class TestFetchWidth:
    """The width the resolve opens its fetches at is the caller's to set."""

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
        )
        return pyproject

    @staticmethod
    def _width(path: Path, **kwargs: object) -> int:
        """The ``max_concurrency`` the resolve hands its coordinator."""
        with patch("nab_project.resolve.FetchCoordinator") as mock_coord_cls:
            mock_coord_cls.return_value.__enter__ = MagicMock(
                side_effect=RuntimeError("stop after construction")
            )
            try:
                _resolved(path, **kwargs)
            except RuntimeError:
                pass

        return mock_coord_cls.call_args.kwargs["max_concurrency"]

    def test_an_unset_width_is_the_named_default(self, tmp_path: Path) -> None:
        path = self._project(tmp_path)

        assert self._width(path) == DEFAULT_MAX_CONCURRENCY

    def test_the_callers_width_reaches_the_coordinator(self, tmp_path: Path) -> None:
        path = self._project(tmp_path)

        assert self._width(path, max_concurrency=3) == 3
