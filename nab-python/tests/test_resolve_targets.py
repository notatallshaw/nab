"""Tests for the per-target resolve engine in :mod:`nab_python.resolve`.

The hot path is exercised via the runtime scenarios in
``run_scenarios.py``.  Unit tests here cover the helper functions and
the in-process orchestration branches that the runtime tests do not
exercise.
"""

from __future__ import annotations

import hashlib
import io
import logging
import subprocess
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from nab_index import vcs as vcs_mod
from nab_index.client import SdistFile, WheelFile
from nab_python import resolve as resolve_mod
from nab_python._provider import listing as listing_mod
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.version import Version
from nab_python.config import (
    ConfigError,
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
    NabProjectConfig,
    conflict_forks,
)
from nab_python.lockfile import (
    DisjointnessError,
    IndexPin,
    LockInput,
    MissingHashError,
    TargetLock,
    build_pylock,
    write_requirements_with_hashes,
    write_requirements_without_hashes,
)
from nab_python.provider import (
    ArchiveSource,
    BuildPolicy,
    DistFile,
    DistPolicy,
    LocalSource,
    MissingExtraError,
    Provider,
    ResolutionStrategy,
    UnsupportedSdistError,
    UnsupportedVcsError,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from nab_python.requirements_file import (
    expand_extra_requirements,
    read_pyproject_dependencies,
    read_pyproject_name,
    read_pyproject_optional_dependencies,
)
from nab_python.resolve import (
    ResolveFork,
    ResolveResult,
    TargetResult,
    _build_resolver_inputs,
    _EngineSettings,
    _resolve_one_target,
    _run_pass,
    build_lock_input,
    resolve_with_coordinator,
)
from nab_python.tags import PlatformSpec
from nab_python.target import Matrix, ResolveTarget
from nab_resolver.errors import ResolutionError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_index.vcs import VcsClone, VcsRequest


def _make_wheel(version: str, *, package: str) -> WheelFile:
    return WheelFile(
        filename=f"{package}-{version}-py3-none-any.whl",
        url=f"https://example.com/{package}-{version}.whl",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=None,
        hashes=(("sha256", "a" * 64),),
    )


def _make_sdist(version: str, *, package: str) -> SdistFile:
    return SdistFile(
        filename=f"{package}-{version}.tar.gz",
        url=f"https://example.com/{package}-{version}.tar.gz",
        version=version,
        requires_python=None,
        upload_time=None,
        hashes=(("sha256", "b" * 64),),
    )


def _make_coordinator(listings: dict[str, list[WheelFile]]) -> MagicMock:
    """Mock FetchCoordinator pre-loaded with each package's listing.

    Metadata fetches return minimal valid METADATA text for whatever
    name/version is requested so look-ahead in ``choose_version``
    passes without stubbing each version explicitly.
    """
    return make_coordinator(listings=listings, auto_metadata=True)


def _reqs(*texts: str) -> list[Requirement]:
    """Parse requirement strings into the objects the engine takes."""
    return [Requirement(text) for text in texts]


def _no_build(**kwargs: object) -> NabProjectConfig:
    """A project config that never builds, so a test resolve stays offline."""
    return NabProjectConfig(build_policy=BuildPolicy.NEVER, **kwargs)  # type: ignore[arg-type]


def _settings(
    coordinator: MagicMock,
    config: NabProjectConfig | None = None,
    *,
    align: bool = True,
    source_root: Path | None = None,
) -> _EngineSettings:
    """The settings one bare ``_resolve_one_target`` or ``_run_pass`` needs."""
    effective = config if config is not None else NabProjectConfig()
    return _EngineSettings(
        coordinator=coordinator,
        config=effective,
        source_root=source_root,
        align=align,
        resolution=effective.resolution,
    )


def _one_target() -> list[ResolveTarget]:
    """The single linux/3.11 target a forked resolve runs against."""
    return Matrix(python="==3.11", platforms=(PlatformSpec("linux_x86_64"),)).expand()


def _linux_311() -> ResolveTarget:
    return ResolveTarget.for_declared(
        python_version="3.11", spec=PlatformSpec("linux_x86_64")
    )


def _windows_311() -> ResolveTarget:
    return ResolveTarget.for_declared(
        python_version="3.11", spec=PlatformSpec("windows_amd64")
    )


def _extra_set(*names: str) -> ConflictSet:
    return ConflictSet(
        members=tuple(ConflictMember(ConflictKind.EXTRA, n) for n in names),
        policy=ConflictPolicy.AT_MOST_ONE,
    )


def _group_set(*names: str) -> ConflictSet:
    return ConflictSet(
        members=tuple(ConflictMember(ConflictKind.GROUP, n) for n in names),
        policy=ConflictPolicy.AT_MOST_ONE,
    )


class TestConflictForks:
    """``conflict_forks`` splits a selection into per-fork resolves."""

    def test_no_conflicts_is_one_unforked_fork(self) -> None:
        forks = conflict_forks(("docs",), ("dev",), ())
        assert len(forks) == 1
        assert forks[0].selection == ()
        assert forks[0].active_extras == ("docs",)
        assert forks[0].active_groups == ("dev",)

    def test_single_selected_member_does_not_engage(self) -> None:
        # Only cpu selected, gpu absent: no conflict, no fork.
        forks = conflict_forks(("cpu",), (), (_extra_set("cpu", "gpu"),))
        assert len(forks) == 1
        assert forks[0].selection == ()
        assert forks[0].active_extras == ("cpu",)

    def test_two_selected_extras_fork_into_two(self) -> None:
        forks = conflict_forks(("cpu", "gpu"), (), (_extra_set("cpu", "gpu"),))
        assert [f.selection for f in forks] == [
            (("extra", "cpu"),),
            (("extra", "gpu"),),
        ]
        assert [f.active_extras for f in forks] == [("cpu",), ("gpu",)]

    def test_three_selected_groups_fork_into_three(self) -> None:
        forks = conflict_forks(
            (),
            ("black22", "black23", "black24"),
            (_group_set("black22", "black23", "black24"),),
        )
        assert [f.selection for f in forks] == [
            (("group", "black22"),),
            (("group", "black23"),),
            (("group", "black24"),),
        ]

    def test_two_engaged_sets_cartesian_product(self) -> None:
        # datamodel-code-generator: black {22,23,24} x isort {5,6} = 6 forks.
        forks = conflict_forks(
            (),
            ("black22", "black23", "black24", "isort5", "isort6"),
            (
                _group_set("black22", "black23", "black24"),
                _group_set("isort5", "isort6"),
            ),
        )
        assert len(forks) == 6
        # Each fork picks exactly one black and one isort.
        for fork in forks:
            chosen = {name for _kind, name in fork.selection}
            assert len(chosen & {"black22", "black23", "black24"}) == 1
            assert len(chosen & {"isort5", "isort6"}) == 1
        # Selections are sorted and unique across forks.
        selections = [f.selection for f in forks]
        assert len(set(selections)) == 6
        assert all(list(s) == sorted(s) for s in selections)

    def test_three_by_three_cartesian_product_is_nine(self) -> None:
        """Three sets-of-three is the headline conflicts.md example: nine forks
        with one member chosen from each set."""
        forks = conflict_forks(
            (),
            (
                "black22",
                "black23",
                "black24",
                "isort5",
                "isort6",
                "isort7",
            ),
            (
                _group_set("black22", "black23", "black24"),
                _group_set("isort5", "isort6", "isort7"),
            ),
        )
        assert len(forks) == 9
        for fork in forks:
            chosen = {name for _kind, name in fork.selection}
            assert len(chosen & {"black22", "black23", "black24"}) == 1
            assert len(chosen & {"isort5", "isort6", "isort7"}) == 1
        assert len({f.selection for f in forks}) == 9

    def test_non_conflicting_selection_present_in_every_fork(self) -> None:
        forks = conflict_forks(
            ("docs", "cpu", "gpu"), ("dev",), (_extra_set("cpu", "gpu"),)
        )
        assert len(forks) == 2
        for fork in forks:
            assert "docs" in fork.active_extras
            assert fork.active_groups == ("dev",)
            # exactly one of cpu/gpu active per fork
            assert len({"cpu", "gpu"} & set(fork.active_extras)) == 1

    def test_mixed_extra_and_group_set(self) -> None:
        members = (
            ConflictMember(ConflictKind.EXTRA, "cpu"),
            ConflictMember(ConflictKind.GROUP, "gpu"),
        )
        cs = ConflictSet(members=members, policy=ConflictPolicy.AT_MOST_ONE)
        forks = conflict_forks(("cpu",), ("gpu",), (cs,))
        assert [f.selection for f in forks] == [
            (("extra", "cpu"),),
            (("group", "gpu"),),
        ]
        assert forks[0].active_extras == ("cpu",)
        assert forks[0].active_groups == ()
        assert forks[1].active_extras == ()
        assert forks[1].active_groups == ("gpu",)

    def test_at_least_one_policy_never_forks(self) -> None:
        cs = ConflictSet(
            members=(
                ConflictMember(ConflictKind.EXTRA, "a"),
                ConflictMember(ConflictKind.EXTRA, "b"),
            ),
            policy=ConflictPolicy.AT_LEAST_ONE,
        )
        forks = conflict_forks(("a", "b"), (), (cs,))
        assert len(forks) == 1
        assert forks[0].selection == ()
        assert forks[0].active_extras == ("a", "b")

    def test_names_canonicalised_before_engagement(self) -> None:
        # Selection spelled differently still engages the conflict.
        forks = conflict_forks(("CPU", "Gpu"), (), (_extra_set("cpu", "gpu"),))
        assert len(forks) == 2
        assert {m for f in forks for _k, m in f.selection} == {"cpu", "gpu"}


class TestConflictForkResolve:
    """End-to-end: forked resolves produce a lock that validates only
    once the conflict is declared (the datamodel-code-generator shape)."""

    def _black_coordinator(self) -> MagicMock:
        return _make_coordinator(
            {
                "black": [
                    _make_wheel("22.1", package="black"),
                    _make_wheel("23.12", package="black"),
                ],
            }
        )

    def _black_forks(self) -> list[ResolveFork]:
        return [
            ResolveFork((("group", "black22"),), tuple(_reqs("black==22.1"))),
            ResolveFork((("group", "black23"),), tuple(_reqs("black==23.12"))),
        ]

    def test_forks_produce_separate_per_label_pins(self) -> None:
        result = resolve_with_coordinator(
            self._black_coordinator(),
            _one_target(),
            forks=self._black_forks(),
            config=_no_build(),
        )
        assert result.success
        by_label = {tr.target.label: tr.pins for tr in result.target_results}
        assert by_label == {
            "py311-linux_x86_64-group-black22": {"black": Version("22.1")},
            "py311-linux_x86_64-group-black23": {"black": Version("23.12")},
        }

    def test_declared_conflict_lock_validates_and_marks_forks(self) -> None:
        result = resolve_with_coordinator(
            self._black_coordinator(),
            _one_target(),
            forks=self._black_forks(),
            config=_no_build(),
        )
        lock_input = build_lock_input(
            result,
            config=_no_build(conflicts=(_group_set("black22", "black23"),)),
            dependency_groups=("black22", "black23"),
        )
        # Must not raise DisjointnessError: the conflict prunes the
        # both-groups-selected context where the two entries collide.
        pylock = build_pylock(lock_input)
        black = sorted(
            (p for p in pylock.packages if str(p.name) == "black"),
            key=lambda p: str(p.version),
        )
        assert [str(p.version) for p in black] == ["22.1", "23.12"]
        assert '"black22" in dependency_groups' in str(black[0].marker)
        assert '"black23" in dependency_groups' in str(black[1].marker)

    def test_same_lock_without_conflict_declaration_is_ambiguous(self) -> None:
        # Identical fork pins, but no conflict declared: the installer
        # could select both groups, so the two black entries collide.
        result = resolve_with_coordinator(
            self._black_coordinator(),
            _one_target(),
            forks=self._black_forks(),
            config=_no_build(),
        )
        lock_input = build_lock_input(
            result,
            dependency_groups=("black22", "black23"),
        )
        with pytest.raises(DisjointnessError, match="black"):
            build_pylock(lock_input)

    def test_default_groups_collision_is_ambiguous(self) -> None:
        # The same undeclared collision, but the groups come from
        # default-groups rather than the CLI selection.  A default
        # install activates both, so the validator must still see it.
        result = resolve_with_coordinator(
            self._black_coordinator(),
            _one_target(),
            forks=self._black_forks(),
            config=_no_build(),
        )
        lock_input = build_lock_input(
            result,
            config=_no_build(default_groups=("black22", "black23")),
        )
        with pytest.raises(DisjointnessError, match="black"):
            build_pylock(lock_input)

    def test_top_level_environments_drop_membership_and_dedupe(self) -> None:
        # Two forks of the one (python, platform) target must collapse to
        # a single top-level environment with no membership clause: that
        # field declares the platform universe, not the group selection.
        result = resolve_with_coordinator(
            self._black_coordinator(),
            _one_target(),
            forks=self._black_forks(),
            config=_no_build(),
        )
        lock_input = build_lock_input(
            result,
            config=_no_build(conflicts=(_group_set("black22", "black23"),)),
            dependency_groups=("black22", "black23"),
        )
        assert len(lock_input.environments) == 1
        env_str = str(lock_input.environments[0])
        assert "in dependency_groups" not in env_str
        assert "in extras" not in env_str
        # The environment is declared from the markers the resolve read.
        # Neither fork read any, so only the always-declared axes remain.
        assert env_str == (
            'python_version == "3.11" and sys_platform == "linux"'
            ' and platform_machine == "x86_64"'
        )

    def _per_fork_preferences(
        self, *, align_across_targets: bool
    ) -> list[dict[str, Version]]:
        """Run the two black forks, recording each fork's preferences.

        Wraps ``_run_pass`` to snapshot the ``preferences`` mapping handed
        to each fork.  Cross-fork accumulation lives in
        ``resolve_with_coordinator``, so the second fork's snapshot
        reveals whether the first fork's pins were threaded forward.
        """
        seen: list[dict[str, Version]] = []
        real_run_pass = resolve_mod._run_pass

        def spy(*args: object) -> object:
            seen.append(dict(args[4]))  # type: ignore[call-overload]
            return real_run_pass(*args)  # type: ignore[arg-type]

        with patch.object(resolve_mod, "_run_pass", spy):
            result = resolve_with_coordinator(
                self._black_coordinator(),
                _one_target(),
                forks=self._black_forks(),
                config=_no_build(),
                align_across_targets=align_across_targets,
            )
        assert result.success
        return seen

    def test_align_across_targets_false_does_not_thread_pins(self) -> None:
        # With alignment off, the second fork's preferences must not
        # carry the first fork's black pin: each fork resolves alone.
        seen = self._per_fork_preferences(align_across_targets=False)
        assert len(seen) == 2
        assert "black" not in seen[0]
        assert "black" not in seen[1]

    def test_align_across_targets_true_threads_pins(self) -> None:
        # The companion case: with alignment on, the first fork's black
        # pin is accumulated into the second fork's preferences, so the
        # assertion above genuinely distinguishes the two modes.
        seen = self._per_fork_preferences(align_across_targets=True)
        assert len(seen) == 2
        assert "black" not in seen[0]
        assert seen[1].get("black") == Version("22.1")


class TestConflictForkBaseNames:
    """A base (no-member) pass names the deps that install unconditionally,
    so a dep required by every member but not the base keeps its
    membership marker (the at_most_one over-install fix)."""

    def _coordinator(self) -> MagicMock:
        return _make_coordinator(
            {
                "base": [_make_wheel("1.0", package="base")],
                "accel": [_make_wheel("5.0", package="accel")],
            }
        )

    def _forks(self) -> list[ResolveFork]:
        # cpu and gpu both pull in accel; the base has only ``base``.
        return [
            ResolveFork((("extra", "cpu"),), tuple(_reqs("base", "accel"))),
            ResolveFork((("extra", "gpu"),), tuple(_reqs("base", "accel"))),
        ]

    def test_env_base_names_excludes_member_only_dep(self) -> None:
        result = resolve_with_coordinator(
            self._coordinator(),
            _one_target(),
            forks=self._forks(),
            base_requirements=_reqs("base"),
            config=_no_build(),
        )
        assert result.success
        (names,) = result.env_base_names.values()
        assert "base" in names
        assert "accel" not in names

    def test_member_only_dep_keeps_membership_marker(self) -> None:
        result = resolve_with_coordinator(
            self._coordinator(),
            _one_target(),
            forks=self._forks(),
            base_requirements=_reqs("base"),
            config=_no_build(),
        )
        lock_input = build_lock_input(
            result,
            config=_no_build(conflicts=(_extra_set("cpu", "gpu"),)),
            extras=("cpu", "gpu"),
        )
        pylock = build_pylock(lock_input)
        by_name = {str(p.name): p for p in pylock.packages}
        env = dict(result.target_results[0].target.marker_env)
        neither = {**env, "extras": frozenset()}
        cpu = {**env, "extras": frozenset({"cpu"})}

        accel = by_name["accel"]
        assert accel.marker is not None
        assert not accel.marker.evaluate(neither)
        assert accel.marker.evaluate(cpu)

        # ``base`` is a true base dep, so it installs unconditionally.
        base = by_name["base"]
        assert base.marker is None or base.marker.evaluate(neither)

    def test_member_only_dep_across_two_sets_keeps_membership_or(self) -> None:
        """Two engaged sets x one member-only dep present in all four forks.

        Tests the conflicts.md claim that "When a single dependency is
        required by every member of two or more engaged sets at once, its
        marker is the conjunction across those sets" by checking the
        emitted marker references every one of the four memberships.
        """
        coordinator = _make_coordinator(
            {
                "base": [_make_wheel("1.0", package="base")],
                "crossdep": [_make_wheel("5.0", package="crossdep")],
            }
        )

        # cpu x mon, cpu x debug, gpu x mon, gpu x debug = 4 forks, each
        # pulling crossdep; base is the only true base-pass dep.
        both = tuple(_reqs("base", "crossdep"))
        forks = [
            ResolveFork((("extra", "cpu"), ("group", "mon")), both),
            ResolveFork((("extra", "cpu"), ("group", "debug")), both),
            ResolveFork((("extra", "gpu"), ("group", "mon")), both),
            ResolveFork((("extra", "gpu"), ("group", "debug")), both),
        ]
        result = resolve_with_coordinator(
            coordinator,
            _one_target(),
            forks=forks,
            base_requirements=_reqs("base"),
            config=_no_build(),
        )
        assert result.success

        lock_input = build_lock_input(
            result,
            config=_no_build(
                conflicts=(
                    _extra_set("cpu", "gpu"),
                    _group_set("mon", "debug"),
                )
            ),
            extras=("cpu", "gpu"),
            dependency_groups=("mon", "debug"),
        )
        pylock = build_pylock(lock_input)
        crossdep = next(p for p in pylock.packages if str(p.name) == "crossdep")

        # The marker must reference every membership clause; the empty
        # selection must not install, but any single (extra, group) pair
        # in the cartesian product must.
        marker_text = str(crossdep.marker)
        for clause in (
            '"cpu" in extras',
            '"gpu" in extras',
            '"mon" in dependency_groups',
            '"debug" in dependency_groups',
        ):
            assert clause in marker_text

        env = dict(result.target_results[0].target.marker_env)
        none = {**env, "extras": frozenset(), "dependency_groups": frozenset()}
        assert crossdep.marker is not None
        assert not crossdep.marker.evaluate(none)
        for extras, groups in (
            (frozenset({"cpu"}), frozenset({"mon"})),
            (frozenset({"gpu"}), frozenset({"debug"})),
        ):
            ctx = {**env, "extras": extras, "dependency_groups": groups}
            assert crossdep.marker.evaluate(ctx)

    def test_base_pass_failure_fails_the_result(self) -> None:
        # The base requirement cannot resolve (no such version), so the
        # writer would lack the data to tell a base dep from a
        # member-only dep.  Surface the failure on ``result.success``.
        result = resolve_with_coordinator(
            self._coordinator(),
            _one_target(),
            forks=self._forks(),
            base_requirements=_reqs("base==9.9"),
            config=_no_build(),
        )
        assert not result.success
        assert result.env_base_names == {}
        assert result.base_results
        assert all(not br.success for br in result.base_results)

    def test_base_pass_failure_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Each failed base pass is announced on the module logger so a
        # caller that ignores ``result.success`` still gets a signal.
        with caplog.at_level(logging.WARNING, logger="nab_python.resolve"):
            resolve_with_coordinator(
                self._coordinator(),
                _one_target(),
                forks=self._forks(),
                base_requirements=_reqs("base==9.9"),
                config=_no_build(),
            )
        assert any("Base attribution skipped" in rec.message for rec in caplog.records)


class TestDroppedRootMarkerWarnedOnce:
    """One mistaken root requirement is reported once per run, however
    many targets, forks and base passes read it."""

    def _coordinator(self) -> MagicMock:
        return _make_coordinator({"base": [_make_wheel("1.0", package="base")]})

    def _two_targets(self) -> list[ResolveTarget]:
        return Matrix(
            python="==3.11",
            platforms=(PlatformSpec("linux_x86_64"), PlatformSpec("windows_amd64")),
        ).expand()

    def _forks(self) -> list[ResolveFork]:
        reqs = tuple(_reqs("base", 'gated ; extra == "test"'))
        return [
            ResolveFork((("extra", "cpu"),), reqs),
            ResolveFork((("extra", "gpu"),), reqs),
        ]

    def test_warned_once_across_targets_forks_and_base_pass(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="nab_python.resolve"):
            result = resolve_with_coordinator(
                self._coordinator(),
                self._two_targets(),
                forks=self._forks(),
                base_requirements=_reqs("base", 'gated ; extra == "test"'),
                config=_no_build(),
            )
        assert result.success
        warnings = [rec for rec in caplog.records if "membership marker" in rec.message]
        assert len(warnings) == 1

    def test_two_offending_requirements_each_warn_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        reqs = _reqs("base", 'gated ; extra == "test"', 'other ; "dev" in extras')
        with caplog.at_level(logging.WARNING, logger="nab_python.resolve"):
            result = resolve_with_coordinator(
                self._coordinator(),
                self._two_targets(),
                reqs,
                config=_no_build(),
            )
        assert result.success
        warned = [
            rec.getMessage()
            for rec in caplog.records
            if "membership marker" in rec.message
        ]
        assert len(warned) == 2
        assert sum('gated; extra == "test"' in msg for msg in warned) == 1
        assert sum('other; "dev" in extras' in msg for msg in warned) == 1


class TestDirectPackages:
    """The direct set handed to the provider: the canonical names of the
    root requirements this target kept."""

    def _direct_packages(self, *texts: str) -> frozenset[str]:
        """The ``direct_packages`` a resolve of ``texts`` hands the provider."""
        coordinator = _make_coordinator({})
        with (
            patch.object(resolve_mod, "Provider") as provider_cls,
            patch.object(resolve_mod, "Resolver") as resolver_cls,
            patch.object(resolve_mod, "build_target_lock"),
        ):
            resolver_cls.return_value.resolve.return_value = {}
            _resolve_one_target(
                _linux_311(), _reqs(*texts), (), _settings(coordinator), {}
            )
        direct: frozenset[str] = provider_cls.call_args.kwargs["direct_packages"]
        return direct

    def test_simple_names_canonicalized(self) -> None:
        """Names are lowercased and underscores become hyphens."""
        assert self._direct_packages("My_Pkg", "Other-Pkg") == {"my-pkg", "other-pkg"}

    def test_specifier_stripped(self) -> None:
        """The version specifier should not appear in the name set."""
        assert self._direct_packages("pkg>=1.0,<2.0") == {"pkg"}

    def test_marker_decides_whether_a_name_is_direct(self) -> None:
        """The set comes from the marker-filtered requirements.

        A win32-gated requirement is direct on a Windows target and gone
        on a Linux one, because that is exactly the set the resolve was
        handed.
        """
        assert self._direct_packages('pywin32; sys_platform == "win32"') == frozenset()
        assert self._direct_packages('pywin32; sys_platform == "linux"') == {"pywin32"}

    def test_extras_proxy_key_is_not_a_direct_name(self) -> None:
        """``pkg[foo]`` is a proxy key, not a package of its own."""
        assert self._direct_packages("pkg[foo]") == {"pkg"}


class TestLowestDirectAcrossTargets:
    """``lowest-direct`` reads the direct set per target, after markers,
    and the floor it gives a direct package survives cross-target alignment.

    ``foo`` is a root gated on windows and a dependency of ``bar``
    everywhere.  A target whose markers drop the root treats ``foo`` as
    transitive; a target that keeps it floors it, whichever target
    resolves first.
    """

    def _coordinator(self) -> MagicMock:
        return make_coordinator(
            listings={
                "bar": [_make_wheel("5.0", package="bar")],
                "foo": [
                    _make_wheel("1.0", package="foo"),
                    _make_wheel("2.0", package="foo"),
                ],
            },
            metadata_by_version={
                "5.0": (
                    "Metadata-Version: 2.1\nName: bar\nVersion: 5.0\n"
                    "Requires-Dist: foo>=1\n\n"
                ),
                "1.0": "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n\n",
                "2.0": "Metadata-Version: 2.1\nName: foo\nVersion: 2.0\n\n",
            },
        )

    def _pins(self, *platform_ids: str) -> dict[str, dict[str, Version]]:
        targets = Matrix(
            python="==3.11",
            platforms=tuple(PlatformSpec(p) for p in platform_ids),
        ).expand()
        result = resolve_with_coordinator(
            self._coordinator(),
            targets,
            _reqs('foo; sys_platform == "win32"', "bar"),
            config=_no_build(),
            resolution_strategy=ResolutionStrategy.LOWEST_DIRECT,
        )
        assert result.success
        return {
            tr.target.platform_spec.platform_id: tr.pins
            for tr in result.target_results
            if tr.target.platform_spec is not None
        }

    def test_marker_excluded_root_takes_the_transitive_newest(self) -> None:
        """On linux ``foo`` reaches the graph only through ``bar``."""
        assert self._pins("linux_x86_64") == {
            "linux_x86_64": {"bar": Version("5.0"), "foo": Version("2.0")},
        }

    def test_marker_included_root_still_takes_its_floor(self) -> None:
        """On windows the same ``foo`` requirement is direct, so it floors."""
        assert self._pins("windows_amd64") == {
            "windows_amd64": {"bar": Version("5.0"), "foo": Version("1.0")},
        }

    def test_floor_survives_a_leading_transitive_target(self) -> None:
        """Linux pins the newest first; windows still floors its direct ``foo``."""
        assert self._pins("linux_x86_64", "windows_amd64") == {
            "linux_x86_64": {"bar": Version("5.0"), "foo": Version("2.0")},
            "windows_amd64": {"bar": Version("5.0"), "foo": Version("1.0")},
        }

    def test_leading_floor_aligns_the_transitive_target(self) -> None:
        """Windows floors first, and linux takes that pin as its preference."""
        assert self._pins("windows_amd64", "linux_x86_64") == {
            "windows_amd64": {"bar": Version("5.0"), "foo": Version("1.0")},
            "linux_x86_64": {"bar": Version("5.0"), "foo": Version("1.0")},
        }


class TestMatrixPerTargetWheelDivergence:
    """A version whose wheels differ per platform resolves per target.

    Each target's tag-filtered listing holds only its own wheel, so the
    sibling divergence check never fires across the matrix boundary and every
    target reads the dependencies of the wheel it installs.
    """

    def _coordinator(self) -> MagicMock:
        linux_wheel = WheelFile(
            filename="pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
            url="https://example.com/pkg-1.0-linux.whl",
            version="1.0",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )
        win_wheel = WheelFile(
            filename="pkg-1.0-cp311-cp311-win_amd64.whl",
            url="https://example.com/pkg-1.0-win.whl",
            version="1.0",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )
        coordinator = make_coordinator(
            listings={
                "pkg": [linux_wheel, win_wheel],
                "linuxdep": [_make_wheel("1.0", package="linuxdep")],
                "windep": [_make_wheel("1.0", package="windep")],
            },
            auto_metadata=True,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: linuxdep\n\n",
            linux_wheel.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: windep\n\n",
            win_wheel.metadata_url,
        )
        return coordinator

    def test_each_target_reads_its_own_wheel(self) -> None:
        targets = Matrix(
            python="==3.11",
            platforms=(PlatformSpec("linux_x86_64"), PlatformSpec("windows_amd64")),
        ).expand()
        result = resolve_with_coordinator(
            self._coordinator(), targets, _reqs("pkg"), config=_no_build()
        )
        assert result.success
        pins = {
            tr.target.platform_spec.platform_id: tr.pins
            for tr in result.target_results
            if tr.target.platform_spec is not None
        }
        assert pins["linux_x86_64"] == {
            "pkg": Version("1.0"),
            "linuxdep": Version("1.0"),
        }
        assert pins["windows_amd64"] == {
            "pkg": Version("1.0"),
            "windep": Version("1.0"),
        }


_FORTY_SHA = "0123456789abcdef0123456789abcdef01234567"


class TestBuildResolverInputs:
    """``_build_resolver_inputs`` builds the resolver-input dict per env."""

    def test_marker_true_keeps_requirement(self) -> None:
        """A requirement whose marker matches the env is kept."""
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs('pkg; sys_platform == "linux"'), NabProjectConfig(), environment=env
        )
        assert "pkg" in out

    def test_marker_false_drops_requirement(self) -> None:
        """A requirement whose marker excludes the env is dropped."""
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs('pkg; sys_platform == "win32"'), NabProjectConfig(), environment=env
        )
        assert out == {}

    def test_set_marker_drops_without_crash(self) -> None:
        """A lockfile-only set marker is empty at resolve time, so the dep drops."""
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs('pkg ; "x" in extras'), NabProjectConfig(), environment=env
        )
        assert out == {}

    def test_extras_get_separate_entries(self) -> None:
        """Extras become ``name[extra]`` entries with any-version range."""
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs("pkg[foo,bar]"), NabProjectConfig(), environment=env
        )
        assert "pkg" in out
        assert "pkg[foo]" in out
        assert "pkg[bar]" in out

    def test_multi_extra_proxy_keys_sorted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Extra proxy keys are inserted in sorted order.

        ``Requirement.extras`` is a set with PYTHONHASHSEED-dependent order, and
        the insertion order of the proxy keys becomes the resolver's root
        package order. A reversed extras order must still give the sorted keys.
        """
        env = _linux_311().marker_env
        req = Requirement("pkg[a,b,c]")
        monkeypatch.setattr(req, "extras", sorted(req.extras, reverse=True))
        out, _ = _build_resolver_inputs([req], NabProjectConfig(), environment=env)
        proxy_keys = [k for k in out if k.startswith("pkg[")]
        assert proxy_keys == ["pkg[a]", "pkg[b]", "pkg[c]"]

    def test_no_specifier_yields_any(self) -> None:
        """An unconstrained requirement gets the any() range."""
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs("pkg"), NabProjectConfig(), environment=env
        )
        assert out["pkg"] == VersionRange.full(admit_arbitrary=False)

    def test_specifier_yields_intervals(self) -> None:
        """A bounded specifier produces the corresponding interval."""
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs("pkg>=1.0,<2.0"), NabProjectConfig(), environment=env
        )
        # The specifier should be stricter than unbounded; we check
        # by confirming a known-out-of-range version is excluded.
        assert Version("0.5") not in out["pkg"]

    def test_arbitrary_equality_specifier_yields_literal_range(self) -> None:
        """``===`` round-trips as a literal-only ``VersionRange``.

        The literal matches the original string but no PEP 440
        ``Version``, so the resolver finds no candidates and fails the
        requirement on its own without any special-casing here.
        Declared extras still flow through.
        """
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs("pkg[ext]===1.0.special"), NabProjectConfig(), environment=env
        )
        assert "pkg" in out
        assert "1.0.special" in out["pkg"]
        assert Version("1.0") not in out["pkg"]
        assert "pkg[ext]" in out

    def test_duplicate_name_intersects(self) -> None:
        """Two requirements for one package combine to their overlap."""
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs("pkg>=2.0", "pkg<3.0"), NabProjectConfig(), environment=env
        )
        assert Version("2.5") in out["pkg"]
        assert Version("1.0") not in out["pkg"]
        assert Version("5.0") not in out["pkg"]

    def test_conflicting_names_raise(self) -> None:
        """Contradictory pins for one package raise ResolutionError."""
        env = _linux_311().marker_env
        with pytest.raises(ResolutionError, match="pkg==1.0"):
            _build_resolver_inputs(
                _reqs("pkg==1.0", "pkg==2.0"), NabProjectConfig(), environment=env
            )

    def test_constraint_extras_rejected(self) -> None:
        """A constraint carrying extras is rejected, matching pip."""
        env = _linux_311().marker_env
        with pytest.raises(ConfigError, match="extras"):
            _build_resolver_inputs(
                _reqs("pkg[dev]<2.0"),
                NabProjectConfig(),
                environment=env,
                kind="constraint",
            )

    def test_marker_false_drops_constraint(self) -> None:
        """A constraint whose marker excludes the env is dropped.

        The marker is evaluated per env for constraints too, so a
        constraint gated off this target never binds. The single-env
        path once enforced such constraints unconditionally (issue #38).
        """
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs('pkg<2.0 ; sys_platform == "win32"'),
            NabProjectConfig(),
            environment=env,
            kind="constraint",
        )
        assert out == {}

    def test_marker_true_keeps_constraint(self) -> None:
        """A constraint whose marker matches the env binds its range."""
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs('pkg<2.0 ; sys_platform == "linux"'),
            NabProjectConfig(),
            environment=env,
            kind="constraint",
        )
        assert Version("1.0") in out["pkg"]
        assert Version("2.0") not in out["pkg"]

    def test_extra_proxy_key_normalized(self) -> None:
        """The proxy key is PEP 685 normalized."""
        env = _linux_311().marker_env
        out, _ = _build_resolver_inputs(
            _reqs("pkg[My_Extra]"), NabProjectConfig(), environment=env
        )
        assert "pkg[my-extra]" in out

    def test_plain_url_requirement_refused(self) -> None:
        """A plain archive URL is refused as an unsupported scheme."""
        env = _linux_311().marker_env
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            _build_resolver_inputs(
                _reqs("pkg @ https://example.com/pkg.whl"),
                NabProjectConfig(),
                environment=env,
            )

    def test_vcs_url_refused_by_default_policy(self) -> None:
        """A git+https requirement is refused under the default BLOCK policy."""
        env = _linux_311().marker_env
        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            _build_resolver_inputs(
                _reqs(f"pkg @ git+https://example.com/pkg.git@{_FORTY_SHA}"),
                NabProjectConfig(),
                environment=env,
            )

    def test_url_constraint_refused(self) -> None:
        """A direct-URL constraint is refused the same way as a requirement."""
        env = _linux_311().marker_env
        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            _build_resolver_inputs(
                _reqs(f"pkg @ git+https://example.com/pkg.git@{_FORTY_SHA}"),
                NabProjectConfig(),
                environment=env,
                kind="constraint",
            )

    def test_admitted_vcs_url_raises_not_implemented(self) -> None:
        """An admitted VCS requirement still has no resolver path."""
        env = _linux_311().marker_env
        config = NabProjectConfig(
            vcs=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
            )
        )
        with pytest.raises(NotImplementedError, match="not implemented"):
            _build_resolver_inputs(
                _reqs(f"pkg @ git+https://example.com/pkg.git@{_FORTY_SHA}"),
                config,
                environment=env,
            )


class TestSelfRefMarker:
    """The extras flatten must keep a self-ref's marker so the per-target
    parse drops its dep on the targets the marker excludes."""

    def test_marker_gated_dep_dropped_on_excluded_target(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'x'\ndependencies = []\n"
            "[project.optional-dependencies]\n"
            "fast = ['some-dep']\n"
            "all = [\"x[fast]; python_version < '3.10'\"]\n"
        )
        reqs = read_pyproject_dependencies(path)
        reqs.extend(
            expand_extra_requirements(
                read_pyproject_optional_dependencies(path),
                read_pyproject_name(path),
                ["all"],
            )
        )
        excluded, _ = _build_resolver_inputs(
            reqs, NabProjectConfig(), environment=_linux_311().marker_env
        )
        assert "some-dep" not in excluded
        included_env = {
            **_linux_311().marker_env,
            "python_version": "3.9",
            "python_full_version": "3.9.0",
        }
        included, _ = _build_resolver_inputs(
            reqs, NabProjectConfig(), environment=included_env
        )
        assert "some-dep" in included


class TestRootExtras:
    """``_build_resolver_inputs`` also reports the extras the root requested."""

    def test_recovers_and_normalizes_extras(self) -> None:
        env = _linux_311().marker_env
        _, root_extras = _build_resolver_inputs(
            _reqs("pkg[My_Extra]", "other"), NabProjectConfig(), environment=env
        )
        assert root_extras == {("pkg", "my-extra")}

    def test_no_extras_yields_empty(self) -> None:
        env = _linux_311().marker_env
        _, root_extras = _build_resolver_inputs(
            _reqs("pkg"), NabProjectConfig(), environment=env
        )
        assert root_extras == set()


class TestResolveOneTarget:
    """``_resolve_one_target`` runs one resolve and reports stats."""

    def test_success_returns_pins(self) -> None:
        """A trivial resolve produces a TargetResult with pins set."""
        coordinator = _make_coordinator({"pkg": [_make_wheel("1.0", package="pkg")]})
        tr = _resolve_one_target(
            _linux_311(), _reqs("pkg"), (), _settings(coordinator, _no_build()), {}
        )
        assert tr.success
        assert tr.pins == {"pkg": Version("1.0")}
        assert tr.target.python_version == "3.11"
        assert tr.target.platform_id == "linux_x86_64"
        assert tr.conflicts == 0
        assert tr.backjumps == 0
        assert tr.metadata_fetched == 1
        assert tr.distributions_seen == 1

    def test_failure_returns_error(self) -> None:
        """A resolve with no candidate version reports failure."""
        coordinator = _make_coordinator({"pkg": []})
        tr = _resolve_one_target(
            _linux_311(), _reqs("pkg"), (), _settings(coordinator, _no_build()), {}
        )
        assert not tr.success
        assert isinstance(tr.error, ResolutionError)

    def test_unhashed_target_defers_hash_check_to_emit(self) -> None:
        """An unhashed wheel resolves; the hash check is per output format."""
        unhashed = WheelFile(
            filename="pkg-1.0-py3-none-any.whl",
            url="https://example.com/pkg-1.0.whl",
            version="1.0",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )
        coordinator = _make_coordinator({"pkg": [unhashed]})
        tr = _resolve_one_target(
            _linux_311(), _reqs("pkg"), (), _settings(coordinator, _no_build()), {}
        )
        assert tr.success
        assert tr.pins == {"pkg": Version("1.0")}
        assert tr.lock is not None
        pin = tr.lock.pins["pkg"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels[0].hashes == ()
        lock_input = LockInput(targets={tr.target.label: tr.lock})
        assert write_requirements_without_hashes(lock_input).strip() == "pkg==1.0"
        with pytest.raises(MissingHashError, match="no acceptable hash"):
            write_requirements_with_hashes(lock_input)

    def test_sdist_install_wheel_only_version_not_a_candidate(self) -> None:
        """A wheel-only version under sdist-install is never a candidate.

        pkg 1.0 publishes only a wheel, so under sdist-install it has no
        installable source and drops out of the listing.  With nothing
        left to try the target fails with a no-versions ResolutionError,
        not a post-resolution MissingSdistError.
        """
        coordinator = _make_coordinator({"pkg": [_make_wheel("1.0", package="pkg")]})
        settings = _settings(
            coordinator, _no_build(dist_policy=DistPolicy.SDIST_INSTALL)
        )
        tr = _resolve_one_target(_linux_311(), _reqs("pkg"), (), settings, {})
        assert not tr.success
        assert isinstance(tr.error, ResolutionError)
        assert tr.pins == {}

    def test_sdist_install_yields_to_older_sdist_version(self) -> None:
        """A wheel-only newest version yields to an older sdist release.

        foo 2.0 publishes only a wheel, so under sdist-install it has no
        source to install; foo 1.0 ships a wheel and an sdist.  The
        resolve pins 1.0 and locks its sdist rather than failing on the
        wheel-only 2.0.
        """
        coordinator = make_coordinator(
            listings={
                "foo": [
                    _make_wheel("2.0", package="foo"),
                    _make_wheel("1.0", package="foo"),
                    _make_sdist("1.0", package="foo"),
                ]
            },
            auto_metadata=True,
        )
        settings = _settings(
            coordinator, _no_build(dist_policy=DistPolicy.SDIST_INSTALL)
        )
        tr = _resolve_one_target(_linux_311(), _reqs("foo"), (), settings, {})
        assert tr.success
        assert tr.pins == {"foo": Version("1.0")}
        assert tr.lock is not None
        pin = tr.lock.pins["foo"]
        assert isinstance(pin, IndexPin)
        assert pin.wheels == ()
        assert pin.sdist is not None


class TestVcsConfigPlumbing:
    """The VCS config and the cache dir flow through to the provider."""

    def _source(self) -> VcsSource:
        return VcsSource(
            name="pkg",
            url=f"git+https://example.com/pkg.git@{_FORTY_SHA}",
        )

    def _allow(self) -> VcsConfig:
        return VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://example.com/",),
        )

    def test_block_policy_rejects_vcs_source_via_one_target(self) -> None:
        """``_resolve_one_target`` surfaces the BLOCK refusal as a ValueError.

        If the VCS config were dropped on the way through the engine the
        default :class:`VcsConfig` is also BLOCK, so we additionally
        check the error string matches the policy diagnostic (not a
        NoneType crash or a generic ``ValueError``).
        """
        settings = _settings(
            _make_coordinator({}),
            _no_build(
                vcs=VcsConfig(policy=VcsPolicy.BLOCK), vcs_sources=(self._source(),)
            ),
        )
        with pytest.raises(ValueError, match="vcs_sources require VcsPolicy.ALLOW"):
            _resolve_one_target(_linux_311(), _reqs("pkg"), (), settings, {})

    def test_allow_policy_admits_vcs_source_via_one_target(
        self, tmp_path: Path
    ) -> None:
        """An ALLOW-configured target registers the VCS source without crashing.

        The resolver requests no VCS package so the materialise path is
        not exercised.  Reaching a non-error :class:`TargetResult` is
        sufficient evidence that the VCS config and the source root both
        reached the provider (the BLOCK fast-fail in
        ``index_vcs_sources`` would have raised at construction time).
        """
        coordinator = _make_coordinator(
            {"other": [_make_wheel("1.0", package="other")]}
        )
        settings = _settings(
            coordinator,
            _no_build(vcs=self._allow(), vcs_sources=(self._source(),)),
            source_root=tmp_path,
        )
        tr = _resolve_one_target(_linux_311(), _reqs("other"), (), settings, {})
        assert tr.success
        assert tr.pins == {"other": Version("1.0")}

    def test_source_root_required_for_vcs_materialize(self) -> None:
        """Without a source root, vcs materialisation raises cleanly.

        When the resolver requests the VCS-backed package the provider
        raises ``UnsupportedSdistError`` mentioning ``vcs_cache_dir``.
        Catching that diagnostic confirms the directory the engine
        derives reaches the provider attribute the materialise path
        reads.  Without the plumbing the resolver would attribute-error
        on ``provider.vcs_cache_dir`` instead.
        """
        settings = _settings(
            _make_coordinator({}),
            _no_build(vcs=self._allow(), vcs_sources=(self._source(),)),
            source_root=None,
        )
        with pytest.raises(UnsupportedSdistError, match="vcs_cache_dir"):
            _resolve_one_target(_linux_311(), _reqs("pkg"), (), settings, {})

    def test_resolve_with_coordinator_threads_vcs_config(self) -> None:
        """``resolve_with_coordinator`` forwards the VCS config end-to-end.

        Failing at the indexing step is the visible signal that the
        config reached the :class:`Provider`.
        """
        with pytest.raises(ValueError, match="vcs_sources require VcsPolicy.ALLOW"):
            resolve_with_coordinator(
                _make_coordinator({}),
                _one_target(),
                _reqs("pkg"),
                config=_no_build(
                    vcs=VcsConfig(policy=VcsPolicy.BLOCK),
                    vcs_sources=(self._source(),),
                ),
            )

    def test_vcs_source_resolves_with_caching_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolve without a cache root clones into scratch space it then drops.

        ``nab lock --no-cache`` arrives with ``cache_dir=None``.  Only
        ``git`` is faked, so the real ``prepare_clone`` has to cope with a
        root that does not exist yet.
        """
        roots: list[Path] = []
        real_prepare_clone = vcs_mod.prepare_clone

        def spy_prepare_clone(
            cache_root: Path,
            request: VcsRequest,
            *,
            require_pin: bool,
            offline: bool = False,
        ) -> VcsClone:
            roots.append(cache_root)
            return real_prepare_clone(
                cache_root, request, require_pin=require_pin, offline=offline
            )

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            if cmd[:2] == ["git", "checkout"]:
                (cwd / "pyproject.toml").write_text(
                    '[project]\nname = "pkg"\nversion = "1.0"\n', encoding="utf-8"
                )
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(vcs_mod, "prepare_clone", spy_prepare_clone)

        result = resolve_with_coordinator(
            _make_coordinator({}),
            _one_target(),
            _reqs("pkg"),
            config=_no_build(vcs=self._allow(), vcs_sources=(self._source(),)),
            cache_dir=None,
        )

        assert result.success
        assert result.target_results[0].pins == {"pkg": Version("1.0")}

        assert len(roots) == 1
        assert not roots[0].exists()


class TestRunPassSerial:
    """``_run_pass`` resolves each target in turn, covering the alignment chain."""

    def test_serial_align_propagates_pins(self) -> None:
        """Each target's pins update the accumulated preferences."""
        wheels = [_make_wheel("1.0", package="pkg"), _make_wheel("2.0", package="pkg")]
        coordinator = _make_coordinator({"pkg": wheels})

        results = _run_pass(
            [_linux_311(), _windows_311()],
            _reqs("pkg"),
            (),
            _settings(coordinator, _no_build(), align=True),
            {},
        )
        assert len(results) == 2
        assert all(r.success for r in results)

    def test_serial_no_align_skips_pin_propagation(self) -> None:
        """With alignment off, each target resolves independently."""
        wheels = [_make_wheel("1.0", package="pkg"), _make_wheel("2.0", package="pkg")]
        coordinator = _make_coordinator({"pkg": wheels})

        results = _run_pass(
            [_linux_311(), _windows_311()],
            _reqs("pkg"),
            (),
            _settings(coordinator, _no_build(), align=False),
            {},
        )
        assert len(results) == 2
        assert all(r.success for r in results)


class TestRunPassConflict:
    """A contradictory root requirement fails each target cleanly."""

    def test_conflicting_requirements_fail_the_target(self) -> None:
        """Pinned-but-different reqs surface as a failed TargetResult.

        ``_build_resolver_inputs`` raises before the resolver runs, so
        the failure has to be caught per target rather than escaping the
        whole pass.
        """
        results = _run_pass(
            [_linux_311()],
            _reqs("pkg==1.0", "pkg==2.0"),
            (),
            _settings(_make_coordinator({}), _no_build()),
            {},
        )
        assert len(results) == 1
        assert not results[0].success
        assert isinstance(results[0].error, ResolutionError)


class TestRunPassConstraintMarker:
    """A constraint's marker gates it per target, not across the matrix."""

    def test_marker_gated_constraint_binds_only_matching_targets(self) -> None:
        """A win32-gated ``pkg<2.0`` pins 1.0 on Windows, leaves Linux at 2.0.

        Each target evaluates the constraint marker against its own
        environment, so the constraint binds only the Windows target.
        Evaluating it once against a shared env, or skipping it for
        constraints, would mis-pin a target's lock.
        """
        wheels = [_make_wheel("1.0", package="pkg"), _make_wheel("2.0", package="pkg")]
        coordinator = _make_coordinator({"pkg": wheels})
        results = _run_pass(
            [_linux_311(), _windows_311()],
            _reqs("pkg"),
            _reqs('pkg<2.0 ; sys_platform == "win32"'),
            _settings(coordinator, _no_build(), align=False),
            {},
        )
        pins = {r.target.platform_id: r.pins["pkg"] for r in results}
        assert pins == {
            "linux_x86_64": Version("2.0"),
            "windows_amd64": Version("1.0"),
        }


class TestResolveResult:
    """``ResolveResult`` aggregates and surfaces per-target info."""

    def test_success_property(self) -> None:
        """``success`` is True only when every target succeeded."""
        ok = TargetResult(target=_linux_311(), success=True)
        bad = TargetResult(target=_windows_311(), success=False)
        assert ResolveResult(
            targets=(_linux_311(),),
            target_results=[ok, ok],
        ).success
        assert not ResolveResult(
            targets=(_linux_311(), _windows_311()),
            target_results=[ok, bad],
        ).success

    def test_merged_pins_skips_failures(self) -> None:
        """Failed targets contribute nothing to the merged pins."""
        ok = TargetResult(
            target=_linux_311(),
            success=True,
            pins={"pkg": Version("1.0")},
        )
        bad = TargetResult(
            target=_windows_311(),
            success=False,
            error=ResolutionError("no solution"),
        )
        result = ResolveResult(
            targets=(_linux_311(), _windows_311()),
            target_results=[ok, bad],
        )
        merged = result.merged_pins()
        assert "pkg" in merged
        labels = {label for _, label in merged["pkg"]}
        # Only the successful target's label appears.
        assert labels == {"py311-linux_x86_64"}


class TestBuildLockInput:
    """``build_lock_input`` collects what each target contributed."""

    def test_skips_targets_without_a_lock(self) -> None:
        """A successful target whose lock is None is skipped.

        ``lock is None`` happens when the resolve succeeded but the
        artefact set lacks a sha256 somewhere; the target's pins are
        still on the result, but the lock cannot record them and so the
        target is omitted entirely.
        """
        linux, windows = _linux_311(), _windows_311()
        with_lock = TargetResult(
            target=linux,
            success=True,
            pins={"pkg": Version("1.0")},
            lock=TargetLock(
                target=linux,
                pins={"pkg": IndexPin(name="pkg", version="1.0", index="pypi")},
            ),
        )
        without_lock = TargetResult(
            target=windows,
            success=True,
            pins={"pkg": Version("1.0")},
            lock=None,
        )
        merged = build_lock_input(
            ResolveResult(
                targets=(linux, windows),
                target_results=[with_lock, without_lock],
            )
        )
        # Only the linux target survives; windows had no lock.
        assert set(merged.targets) == {"py311-linux_x86_64"}

    def test_distinct_platform_specs_do_not_clobber_pins(self) -> None:
        """Two specs sharing a platform_id keep separate per-target pins.

        Both targets share python_version and platform_id, so before the
        label gained a spec discriminator they produced the same label
        and the second target's pins overwrote the first, silently
        dropping a resolved pin.
        """
        older, newer = Matrix(
            python="==3.11",
            platforms=(
                PlatformSpec("linux_x86_64", runs_on_libc=(2, 17)),
                PlatformSpec("linux_x86_64", runs_on_libc=(2, 34)),
            ),
        ).expand()
        results = [
            TargetResult(
                target=older,
                success=True,
                pins={"pkg": Version("1.0")},
                lock=TargetLock(
                    target=older,
                    pins={"pkg": IndexPin(name="pkg", version="1.0", index="pypi")},
                ),
            ),
            TargetResult(
                target=newer,
                success=True,
                pins={"pkg": Version("2.0")},
                lock=TargetLock(
                    target=newer,
                    pins={"pkg": IndexPin(name="pkg", version="2.0", index="pypi")},
                ),
            ),
        ]
        merged = build_lock_input(
            ResolveResult(targets=(older, newer), target_results=results)
        )
        assert len(merged.targets) == 2
        versions = {lock.pins["pkg"].version for lock in merged.targets.values()}
        assert versions == {"1.0", "2.0"}


class TestResolveWithCoordinator:
    """End-to-end orchestration via the testable injected-coordinator entry."""

    def test_first_pass_returns_pins(self) -> None:
        """A single-target resolve produces a ResolveResult with pins."""
        coordinator = _make_coordinator({"pkg": [_make_wheel("1.0", package="pkg")]})
        result = resolve_with_coordinator(coordinator, _one_target(), _reqs("pkg"))
        assert result.success
        assert result.target_results[0].pins == {"pkg": Version("1.0")}

    def test_user_requested_missing_extra_raises(self) -> None:
        """A user-requested extra a package does not provide raises.

        With the default ``ExtrasMode.ERROR_USER``, a missing user extra
        is an error rather than a silent drop.
        """
        coordinator = _make_coordinator({"pkg": [_make_wheel("1.0", package="pkg")]})
        with pytest.raises(MissingExtraError):
            resolve_with_coordinator(coordinator, _one_target(), _reqs("pkg[missing]"))

    @staticmethod
    def _extra_backtrack_coordinator() -> MagicMock:
        """An index where only ``aa==3.0`` provides the ``y`` extra.

        ``aa[y]`` pulls in ``cc<3.0``, so a resolve that decides ``cc``
        at 3.0 first has to backjump off that decision to keep the extra.
        """
        aa_wheels = [_make_wheel(v, package="aa") for v in ("1.0", "2.0", "2.1", "3.0")]
        coordinator = make_coordinator(
            listings={
                "aa": aa_wheels,
                "cc": [_make_wheel(v, package="cc") for v in ("1.1", "2.1", "3.0")],
            },
            auto_metadata=True,
        )
        coordinator.index.store_metadata(
            "aa",
            "3.0",
            "Metadata-Version: 2.1\nName: aa\nVersion: 3.0\n"
            "Provides-Extra: y\n"
            'Requires-Dist: cc<3.0; extra == "y"\n\n',
            aa_wheels[-1].metadata_url,
        )
        return coordinator

    @pytest.mark.parametrize(
        "texts",
        [("aa[y]>=2.0", "cc>1.0"), ("cc>1.0", "aa[y]>=2.0")],
    )
    def test_root_extra_resolves_under_either_root_order(
        self, texts: tuple[str, str]
    ) -> None:
        """A root extra resolves the same however the roots are ordered.

        Root order seeds the decision tiebreak, so deciding ``cc`` first
        narrows ``aa[y]`` onto versions that do not declare ``y``; the
        proxy has to report no version there rather than pin one and
        error out.
        """
        result = resolve_with_coordinator(
            self._extra_backtrack_coordinator(), _one_target(), _reqs(*texts)
        )
        assert result.success
        assert result.target_results[0].pins == {
            "aa": Version("3.0"),
            "cc": Version("2.1"),
        }

    @staticmethod
    def _narrowed_base_coordinator() -> MagicMock:
        """An index where only ``aa==3.0`` provides the ``y`` extra.

        ``bb==2.0`` requires ``aa<3.0``, so a resolve that takes it
        narrows ``aa`` off the extra's only provider before ``aa[y]``
        is ever asked for a version.
        """
        aa_wheels = [_make_wheel(v, package="aa") for v in ("1.0", "2.0", "2.1", "3.0")]
        bb_wheels = [_make_wheel(v, package="bb") for v in ("1.0", "2.0")]
        coordinator = make_coordinator(
            listings={"aa": aa_wheels, "bb": bb_wheels},
            auto_metadata=True,
        )
        coordinator.index.store_metadata(
            "aa",
            "3.0",
            "Metadata-Version: 2.1\nName: aa\nVersion: 3.0\nProvides-Extra: y\n\n",
            aa_wheels[-1].metadata_url,
        )
        coordinator.index.store_metadata(
            "bb",
            "2.0",
            "Metadata-Version: 2.1\nName: bb\nVersion: 2.0\nRequires-Dist: aa<3.0\n\n",
            bb_wheels[-1].metadata_url,
        )
        return coordinator

    @pytest.mark.parametrize("texts", [("bb", "aa[y]"), ("aa[y]", "bb")])
    def test_root_extra_resolves_when_its_base_is_narrowed_first(
        self, texts: tuple[str, str]
    ) -> None:
        """A root extra survives its base being decided before it is asked.

        Whichever root order runs, ``aa`` is narrowed to ``<3.0`` before
        ``aa[y]`` picks a version, so the proxy only sees versions that
        lack ``y`` and has to report none, dropping ``bb==2.0``.
        """
        result = resolve_with_coordinator(
            self._narrowed_base_coordinator(), _one_target(), _reqs(*texts)
        )
        assert result.success
        assert result.target_results[0].pins == {
            "aa": Version("3.0"),
            "bb": Version("1.0"),
        }

    def test_constraints_passed_through(self) -> None:
        """The config's constraints reach the resolver."""
        coordinator = _make_coordinator(
            {
                "pkg": [
                    _make_wheel("1.0", package="pkg"),
                    _make_wheel("2.0", package="pkg"),
                ]
            }
        )
        result = resolve_with_coordinator(
            coordinator,
            _one_target(),
            _reqs("pkg"),
            config=NabProjectConfig(constraints=("pkg<2.0",)),
        )
        assert result.success
        assert result.target_results[0].pins == {"pkg": Version("1.0")}

    def test_resolution_strategy_overrides_the_config(self) -> None:
        """An explicit strategy wins over the config's ``resolution``."""
        coordinator = _make_coordinator(
            {
                "pkg": [
                    _make_wheel("1.0", package="pkg"),
                    _make_wheel("2.0", package="pkg"),
                ]
            }
        )
        result = resolve_with_coordinator(
            coordinator,
            _one_target(),
            _reqs("pkg"),
            config=NabProjectConfig(resolution=ResolutionStrategy.HIGHEST),
            resolution_strategy=ResolutionStrategy.LOWEST,
        )
        assert result.success
        assert result.target_results[0].pins == {"pkg": Version("1.0")}


class TestLocalVcsRequiresPython:
    """A local or VCS pin must satisfy each targeted Python version.

    Index candidates are filtered by Requires-Python while listing;
    local-path and VCS sources skip that filter, so the resolve checks
    them after resolving.
    """

    def _write(self, tmp_path: Path, body: str) -> LocalSource:
        (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
        return LocalSource("foo", str(tmp_path))

    def _resolve(
        self, coordinator: MagicMock, matrix: Matrix, local: LocalSource
    ) -> ResolveResult:
        return resolve_with_coordinator(
            coordinator,
            matrix.expand(),
            _reqs("foo"),
            config=NabProjectConfig(local_sources=(local,)),
        )

    def test_excluding_python_fails_the_target(self, tmp_path: Path) -> None:
        local = self._write(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = ">=3.12"\n',
        )
        result = self._resolve(
            make_coordinator([], package="foo"),
            Matrix(python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)),
            local,
        )
        assert not result.success
        error = result.target_results[0].error
        assert error is not None
        assert "foo 1.0 requires Python" in str(error)
        assert "3.10" in str(error)

    def test_compatible_python_with_index_dep_succeeds(self, tmp_path: Path) -> None:
        local = self._write(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\n'
            'requires-python = ">=3.10"\ndependencies = ["bar"]\n',
        )
        coord = make_coordinator(
            listings={"bar": [_make_wheel("2.0", package="bar")]},
            auto_metadata=True,
        )
        result = self._resolve(
            coord,
            Matrix(python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)),
            local,
        )
        assert result.success
        pins = result.target_results[0].pins
        assert str(pins["foo"]) == "1.0"
        assert str(pins["bar"]) == "2.0"

    def test_no_requires_python_is_unconstrained(self, tmp_path: Path) -> None:
        local = self._write(tmp_path, '[project]\nname = "foo"\nversion = "1.0"\n')
        result = self._resolve(
            make_coordinator([], package="foo"),
            Matrix(python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)),
            local,
        )
        assert result.success
        assert str(result.target_results[0].pins["foo"]) == "1.0"

    def test_patch_satisfies_local_requires_python(self, tmp_path: Path) -> None:
        local = self._write(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = ">=3.13.1"\n',
        )
        result = self._resolve(
            make_coordinator([], package="foo"),
            Matrix(
                python="==3.13",
                platforms=(PlatformSpec("linux_x86_64"),),
                python_patches={"3.13": "3.13.4"},
            ),
            local,
        )
        assert result.success
        assert str(result.target_results[0].pins["foo"]) == "1.0"

    def test_patch_below_local_requires_python_fails(self, tmp_path: Path) -> None:
        local = self._write(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = ">=3.13.5"\n',
        )
        result = self._resolve(
            make_coordinator([], package="foo"),
            Matrix(
                python="==3.13",
                platforms=(PlatformSpec("linux_x86_64"),),
                python_patches={"3.13": "3.13.4"},
            ),
            local,
        )
        assert not result.success
        error = result.target_results[0].error
        assert error is not None
        assert "foo 1.0 requires Python" in str(error)


def _archive_bytes(name: str, version: str, pyproject: str) -> bytes:
    """Return ``.tar.gz`` bytes for a one-file sdist rooted at name-version."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = pyproject.encode("utf-8")
        info = tarfile.TarInfo(f"{name}-{version}/pyproject.toml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


# Materialising an archive extracts it, so the tar data filter is required.
requires_data_filter = pytest.mark.skipif(
    not hasattr(tarfile, "data_filter"),
    reason="sdist extraction requires the tar data filter (PEP 706)",
)


@requires_data_filter
class TestArchiveSourceAcrossTargets:
    """An archive source threads through the resolve across every target."""

    def test_resolves_and_pins_across_targets(self, tmp_path: Path) -> None:
        pyproject = (
            '[project]\nname = "foo"\nversion = "1.0"\n'
            'requires-python = ">=3.11"\ndependencies = ["bar"]\n'
        )
        data = _archive_bytes("foo", "1.0", pyproject)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.tar.gz#sha256={digest}"
        )
        coord = make_coordinator(
            listings={"bar": [_make_wheel("2.0", package="bar")]},
            auto_metadata=True,
        )
        coord.index.store_sdist_archive("foo", digest, data)

        result = resolve_with_coordinator(
            coord,
            Matrix(
                python=">=3.11, <3.13", platforms=(PlatformSpec("linux_x86_64"),)
            ).expand(),
            _reqs("foo"),
            config=NabProjectConfig(archive_sources=(source,)),
            cache_dir=tmp_path,
        )

        assert result.success
        assert len(result.target_results) == 2
        for target_result in result.target_results:
            assert str(target_result.pins["foo"]) == "1.0"
            assert str(target_result.pins["bar"]) == "2.0"

    def test_resolves_with_caching_off(self) -> None:
        """``nab lock --no-cache`` still extracts and pins the declared archive."""
        pyproject = '[project]\nname = "foo"\nversion = "1.0"\n'
        data = _archive_bytes("foo", "1.0", pyproject)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.tar.gz#sha256={digest}"
        )
        coord = make_coordinator([], package="foo")
        coord.index.store_sdist_archive("foo", digest, data)

        result = resolve_with_coordinator(
            coord,
            _one_target(),
            _reqs("foo"),
            config=NabProjectConfig(archive_sources=(source,)),
            cache_dir=None,
        )

        assert result.success
        assert str(result.target_results[0].pins["foo"]) == "1.0"

    def test_requires_python_excluding_target_fails(self, tmp_path: Path) -> None:
        # An archive skips the listing Requires-Python filter, so the source
        # guard must reject one that excludes the target's Python.
        pyproject = (
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = ">=3.12"\n'
        )
        data = _archive_bytes("foo", "1.0", pyproject)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.tar.gz#sha256={digest}"
        )
        coord = make_coordinator([], package="foo")
        coord.index.store_sdist_archive("foo", digest, data)

        result = resolve_with_coordinator(
            coord,
            Matrix(python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)).expand(),
            _reqs("foo"),
            config=NabProjectConfig(archive_sources=(source,)),
            cache_dir=tmp_path,
        )

        assert not result.success
        error = result.target_results[0].error
        assert error is not None
        assert "foo 1.0 requires Python" in str(error)


class TestSharedListingFilter:
    """The base listing filter is computed once per (package, Python)."""

    def _wheel(
        self,
        version: str,
        *,
        requires_python: str | None = None,
        tag: str = "py3-none-any",
    ) -> WheelFile:
        return WheelFile(
            filename=f"pkg-{version}-{tag}.whl",
            url=f"https://example.com/pkg-{version}.whl",
            version=version,
            requires_python=requires_python,
            has_metadata=True,
            upload_time=None,
            hashes=(("sha256", "a" * 64),),
        )

    def _coordinator(self, wheels: list[WheelFile]) -> MagicMock:
        return _make_coordinator({"pkg": wheels})

    def _targets(self, python: str) -> list[ResolveTarget]:
        return Matrix(
            python=python,
            platforms=(PlatformSpec("linux_x86_64"), PlatformSpec("windows_amd64")),
        ).expand()

    def _count_base_filters(self) -> tuple[list[str], object]:
        calls: list[str] = []
        real = listing_mod._filter_base

        def counting(
            provider: Provider, normalized: str, files: Sequence[WheelFile]
        ) -> list[tuple[Version, DistFile]]:
            calls.append(f"{normalized}@{provider.python_version}")
            return real(provider, normalized, files)

        return calls, counting

    def test_platform_targets_share_one_base_filter(self) -> None:
        """Targets differing only by platform reuse the base filter result."""
        wheels = [self._wheel("1.0"), self._wheel("2.0")]
        calls, counting = self._count_base_filters()

        with patch.object(listing_mod, "_filter_base", counting):
            result = resolve_with_coordinator(
                self._coordinator(wheels),
                self._targets("==3.11"),
                _reqs("pkg"),
                config=_no_build(),
            )

        assert result.success
        assert len(result.target_results) == 2
        assert calls == ["pkg@3.11.0"]

    def test_shared_filter_runs_before_the_wheel_tag_pass(self) -> None:
        """Only the pre-tag list is shared: a linux-only wheel stays off Windows."""
        wheels = [
            self._wheel("1.0"),
            self._wheel("2.0", tag="cp311-cp311-manylinux_2_17_x86_64"),
        ]
        calls, counting = self._count_base_filters()

        with patch.object(listing_mod, "_filter_base", counting):
            result = resolve_with_coordinator(
                self._coordinator(wheels),
                self._targets("==3.11"),
                _reqs("pkg"),
                config=_no_build(),
            )

        assert result.success
        assert calls == ["pkg@3.11.0"]
        pinned = {
            tr.target.platform_id: str(tr.pins["pkg"]) for tr in result.target_results
        }
        assert pinned == {"linux_x86_64": "2.0", "windows_amd64": "1.0"}

    def test_each_python_filters_the_listing_once(self) -> None:
        """The memo is keyed by Python: a Requires-Python bound still applies."""
        wheels = [self._wheel("1.0"), self._wheel("2.0", requires_python=">=3.12")]
        calls, counting = self._count_base_filters()

        with patch.object(listing_mod, "_filter_base", counting):
            result = resolve_with_coordinator(
                self._coordinator(wheels),
                self._targets(">=3.11,<3.13"),
                _reqs("pkg"),
                config=_no_build(),
                align_across_targets=False,
            )

        assert result.success
        assert len(result.target_results) == 4
        assert sorted(calls) == ["pkg@3.11.0", "pkg@3.12.0"]
        pinned = {
            tr.target.python_version: str(tr.pins["pkg"])
            for tr in result.target_results
        }
        assert pinned == {"3.11": "1.0", "3.12": "2.0"}

    def test_reused_filter_still_counts_distributions_per_target(self) -> None:
        """Every target reports the files it saw, memo hit or not."""
        wheels = [self._wheel("1.0"), self._wheel("2.0")]

        result = resolve_with_coordinator(
            self._coordinator(wheels),
            self._targets("==3.11"),
            _reqs("pkg"),
            config=_no_build(),
        )

        assert result.success
        assert [tr.distributions_seen for tr in result.target_results] == [2, 2]


class TestMicroBoundaryNarrowing:
    """A minor a consulted marker cuts is resolved once per micro slice.

    A declared target names a minor and synthesizes ``{minor}.0``.  When a
    marker's python_full_version boundary lies inside that minor, resolving
    the whole minor at ``.0`` would declare it by how ``.0`` read the clause,
    excluding the real interpreters on the other side.  The engine splits the
    minor and resolves each slice instead.
    """

    @staticmethod
    def _coordinator(metadata: dict[str, str]) -> MagicMock:
        listings = {
            name: [_make_wheel(version, package=name)]
            for name, version in (("foo", "1.0"), ("mid", "2.0"), ("top", "3.0"))
        }
        return make_coordinator(listings=listings, metadata_by_version=metadata)

    @staticmethod
    def _meta(name: str, version: str, *requires: str) -> str:
        head = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        return head + "".join(f"Requires-Dist: {req}\n" for req in requires)

    @staticmethod
    def _pins_by_label(result: ResolveResult) -> dict[str, set[str]]:
        return {tr.target.label: set(tr.pins) for tr in result.target_results}

    def test_only_split_targets_are_re_resolved(self) -> None:
        """An unsplit minor keeps its first-pass result; only the split one
        is resolved again, so the whole matrix is not re-run."""
        coordinator = self._coordinator(
            {
                "1.0": self._meta(
                    "foo", "1.0", 'mid ; python_full_version >= "3.10.4"'
                ),
                "2.0": self._meta("mid", "2.0"),
            }
        )
        targets = Matrix(
            python=">=3.10,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()

        with patch.object(
            resolve_mod,
            "_resolve_one_target",
            wraps=resolve_mod._resolve_one_target,
        ) as spy:
            result = resolve_with_coordinator(
                coordinator, targets, _reqs("foo"), config=_no_build()
            )

        assert result.success
        labels = [call.args[0].label for call in spy.call_args_list]
        # 3.11 and 3.12 have no in-minor boundary, so each resolves once.
        assert labels.count("py311-linux_x86_64") == 1
        assert labels.count("py312-linux_x86_64") == 1
        # Three minors resolved once each, plus 3.10's two slices: no full re-run.
        assert len(labels) == 5

    def test_a_host_target_is_not_split(self) -> None:
        """A host target names a real micro, so no ``.0`` is synthesized and a
        full-version marker never splits it."""
        coordinator = self._coordinator(
            {
                "1.0": self._meta(
                    "foo", "1.0", 'mid ; python_full_version >= "3.10.4"'
                ),
                "2.0": self._meta("mid", "2.0"),
            }
        )

        with patch.object(
            resolve_mod,
            "_resolve_one_target",
            wraps=resolve_mod._resolve_one_target,
        ) as spy:
            result = resolve_with_coordinator(
                coordinator,
                [ResolveTarget.for_host()],
                _reqs("foo"),
                config=_no_build(),
            )

        assert result.success
        assert len(result.target_results) == 1
        assert spy.call_count == 1

    @staticmethod
    def _linux_host_env() -> dict[str, str]:
        return {
            "implementation_name": "cpython",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_python_implementation": "CPython",
            "platform_release": "6.8.0",
            "platform_system": "Linux",
            "python_full_version": "3.13.2",
            "python_version": "3.13",
            "sys_platform": "linux",
        }

    def test_a_bare_minor_python_target_splits_and_covers_the_minor(self) -> None:
        """``--python 3.11`` (and a platform-less ``[tool.nab.environment]``)
        synthesizes ``3.11.0`` yet carries no ``platform_spec``.  A dep gated
        below a patch still splits it, so the lock covers every real 3.11
        instead of a row an installer rejects on 3.11.9.
        """
        coordinator = self._coordinator(
            {
                "1.0": self._meta("foo", "1.0", 'mid ; python_full_version < "3.11.4"'),
                "2.0": self._meta("mid", "2.0"),
            }
        )
        target = ResolveTarget.for_host_python("3.11", env_source=self._linux_host_env)

        result = resolve_with_coordinator(
            coordinator, [target], _reqs("foo"), config=_no_build()
        )

        assert result.success
        assert self._pins_by_label(result) == {
            "py311-host-pf3110": {"foo", "mid"},
            "py311-host-pf3114": {"foo"},
        }
        rows = build_lock_input(result, config=_no_build()).environments
        assert len(rows) == 2
        real_3119 = {
            "python_version": "3.11",
            "python_full_version": "3.11.9",
            "sys_platform": "linux",
            "platform_machine": "x86_64",
            "implementation_name": "cpython",
        }
        assert any(row.evaluate(real_3119) for row in rows)

    def _fixpoint_coordinator(self) -> MagicMock:
        return self._coordinator(
            {
                "1.0": self._meta(
                    "foo", "1.0", 'mid ; python_full_version >= "3.10.2"'
                ),
                "2.0": self._meta(
                    "mid", "2.0", 'top ; python_full_version >= "3.10.5"'
                ),
                "3.0": self._meta("top", "3.0"),
            }
        )

    def test_a_boundary_reachable_only_above_a_split_is_found(self) -> None:
        """``top``'s 3.10.5 boundary is consulted only once ``mid`` is present,
        which needs 3.10.2 first.  The fixpoint re-splits until it settles."""
        targets = Matrix(
            python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()

        result = resolve_with_coordinator(
            self._fixpoint_coordinator(), targets, _reqs("foo"), config=_no_build()
        )

        assert result.success
        assert self._pins_by_label(result) == {
            "py310-linux_x86_64-pf3100": {"foo"},
            "py310-linux_x86_64-pf3102": {"foo", "mid"},
            "py310-linux_x86_64-pf3105": {"foo", "mid", "top"},
        }

    def test_a_split_that_does_not_converge_raises(self) -> None:
        """The pass cap turns a graph that never settles into a loud error
        rather than a hang."""
        targets = Matrix(
            python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()

        with (
            patch.object(resolve_mod, "_MAX_MICRO_SPLIT_PASSES", 1),
            pytest.raises(ResolutionError, match="did not converge"),
        ):
            resolve_with_coordinator(
                self._fixpoint_coordinator(),
                targets,
                _reqs("foo"),
                config=_no_build(),
            )

    def test_divergent_slice_pins_validate_as_disjoint(self) -> None:
        """Two slices pinning one package at different versions carry disjoint
        micro markers, so the emitted lock passes disjointness validation."""
        coordinator = make_coordinator(
            listings={
                "bar": [
                    _make_wheel("1.0", package="bar"),
                    _make_wheel("2.0", package="bar"),
                ]
            },
            auto_metadata=True,
        )
        targets = Matrix(
            python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()

        result = resolve_with_coordinator(
            coordinator,
            targets,
            _reqs(
                'bar==1.0 ; python_full_version < "3.10.4"',
                'bar==2.0 ; python_full_version >= "3.10.4"',
            ),
            config=_no_build(),
        )

        assert result.success
        versions = {
            tr.target.label: str(tr.pins["bar"]) for tr in result.target_results
        }
        assert versions == {
            "py310-linux_x86_64-pf3100": "1.0",
            "py310-linux_x86_64-pf3104": "2.0",
        }

        pylock = build_pylock(build_lock_input(result, config=_no_build()))
        pylock.validate()
        bars = [pkg for pkg in pylock.packages if str(pkg.name) == "bar"]
        assert len(bars) == 2

    def test_a_literal_on_the_left_marker_splits_and_covers_the_minor(self) -> None:
        """A dep gated by a literal-first marker (``"3.10.4" > pfv``) splits
        the minor the same as ``pfv < "3.10.4"`` would, so the two rows cover
        every real 3.10 with no interpreter left between them.
        """
        coordinator = self._coordinator(
            {
                "1.0": self._meta("foo", "1.0", 'mid ; "3.10.4" > python_full_version'),
                "2.0": self._meta("mid", "2.0"),
            }
        )
        targets = Matrix(
            python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()

        result = resolve_with_coordinator(
            coordinator, targets, _reqs("foo"), config=_no_build()
        )

        assert result.success
        assert self._pins_by_label(result) == {
            "py310-linux_x86_64-pf3100": {"foo", "mid"},
            "py310-linux_x86_64-pf3104": {"foo"},
        }
        rows = build_lock_input(result, config=_no_build()).environments
        assert len(rows) == 2
        for micro in ("3.10.0", "3.10.3", "3.10.4", "3.10.19"):
            env = {
                "python_version": "3.10",
                "python_full_version": micro,
                "sys_platform": "linux",
                "platform_machine": "x86_64",
                "implementation_name": "cpython",
            }
            assert sum(row.evaluate(env) for row in rows) == 1
