"""Tests for the per-target resolve engine in :mod:`nab_project._resolve.engine`.

Covers the helper functions and the in-process orchestration branches; the full
resolve path runs in ``nab-project/tests/universal/test_external_scenarios.py``.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import logging
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import patch

import pytest

from nab_index import vcs as vcs_mod
from nab_index.cache import ARCHIVE_BUCKET, VCS_BUCKET
from nab_index.client import SdistFile, WheelFile
from nab_index.multi_index import IndexConfig
from nab_project import resolve as resolve_mod
from nab_project._resolve import engine as engine_mod
from nab_project._resolve.engine import _EngineSettings, _resolve_one_target, _run_pass
from nab_project._testing.coordinator_fake import FakeFetchPort, make_coordinator
from nab_project.conflicts import (
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
    conflict_forks,
)
from nab_project.fetch import DEFAULT_INDEX_URL
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import (
    DisjointnessError,
    IndexPin,
    LockInput,
    MissingHashError,
    TargetLock,
    build_pylock,
    write_requirements_with_hashes,
    write_requirements_without_hashes,
)
from nab_project.pyproject_files import (
    read_pyproject_dependencies,
    read_pyproject_name,
    read_pyproject_optional_dependencies,
)
from nab_project.resolve import (
    InstallContexts,
    ResolveFork,
    ResolveResult,
    TargetResult,
    build_lock_input,
    build_resolver_inputs,
    resolve_with_coordinator,
)
from nab_provider._provider import listing as listing_mod
from nab_provider._vendor.packaging.markers import Marker
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.errors import ConfigError
from nab_provider.marker_holds import dependency_marker_holds
from nab_provider.overrides import IndexOverride, PackageOverride
from nab_provider.provider import (
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
from nab_provider.requirements_file import expand_extra_requirements
from nab_provider.resolver_inputs import ProxyConstraints
from nab_provider.tags import PlatformSpec
from nab_provider.target import Matrix, ResolveTarget
from nab_resolver.errors import ResolutionError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nab_index.vcs import VcsClone, VcsRequest


def _make_wheel(
    version: str, *, package: str, upload_time: str | None = None
) -> WheelFile:
    return WheelFile(
        filename=f"{package}-{version}-py3-none-any.whl",
        url=f"https://example.com/{package}-{version}.whl",
        version=version,
        requires_python=None,
        has_metadata=True,
        upload_time=upload_time,
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


def _make_coordinator(listings: dict[str, list[WheelFile]]) -> FakeFetchPort:
    """Fetch port pre-loaded with each package's listing.

    Metadata fetches return minimal valid METADATA text for whatever
    name/version is requested so look-ahead in ``choose_version``
    passes without stubbing each version explicitly.
    """
    return make_coordinator(listings=listings, auto_metadata=True)


def _reqs(*texts: str) -> list[Requirement]:
    """Parse requirement strings into the objects the engine takes."""
    return [Requirement(text) for text in texts]


def _no_build(**kwargs: object) -> ResolveInputs:
    """Settings that never build, so a test resolve stays offline."""
    return ResolveInputs(build_policy=BuildPolicy.NEVER, **kwargs)  # type: ignore[arg-type]


def _settings(
    coordinator: FakeFetchPort,
    inputs: ResolveInputs | None = None,
    *,
    align: bool = True,
    source_root: Path | None = None,
) -> _EngineSettings:
    """The settings one bare ``_resolve_one_target`` or ``_run_pass`` needs."""
    effective = inputs if inputs is not None else ResolveInputs()
    return _EngineSettings(
        coordinator=coordinator,
        inputs=effective,
        source_root=source_root,
        align=align,
        resolution=effective.resolution,
        marker_holds=dependency_marker_holds,
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

    def test_a_configured_name_is_active_without_being_selected(self) -> None:
        """A configured group is active whenever it is set, so its set engages."""
        forks = conflict_forks(
            (), (), (_group_set("main", "build"),), ("main", "build")
        )

        assert [f.selection for f in forks] == [
            (("group", "main"),),
            (("group", "build"),),
        ]
        assert [f.active_configured for f in forks] == [("main",), ("build",)]
        assert [f.active_groups for f in forks] == [(), ()]

    def test_configured_names_stay_out_of_the_unforked_group_list(self) -> None:
        """``active_groups`` stays what the [dependency-groups] loader can read."""
        forks = conflict_forks((), ("dev",), (), ("main", "build"))

        assert len(forks) == 1
        assert forks[0].active_groups == ("dev",)
        assert forks[0].active_configured == ("main", "build")

    def test_a_configured_name_in_a_dormant_set_is_still_carried(self) -> None:
        """The set does not engage, so every fork keeps both configured names."""
        forks = conflict_forks((), (), (_group_set("main", "dev"),), ("main", "build"))

        assert len(forks) == 1
        assert forks[0].selection == ()
        assert forks[0].active_configured == ("main", "build")

    def test_a_configured_and_a_declared_name_fork_together(self) -> None:
        """Selecting the declared member engages the set the configured one is in."""
        forks = conflict_forks((), ("dev",), (_group_set("build", "dev"),), ("build",))

        assert [f.selection for f in forks] == [
            (("group", "build"),),
            (("group", "dev"),),
        ]
        assert [f.active_configured for f in forks] == [("build",), ()]
        assert [f.active_groups for f in forks] == [(), ("dev",)]

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

    def _black_coordinator(self) -> FakeFetchPort:
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
            inputs=_no_build(),
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
            inputs=_no_build(),
        )
        lock_input = build_lock_input(
            result,
            inputs=_no_build(conflicts=(_group_set("black22", "black23"),)),
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
            inputs=_no_build(),
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
            inputs=_no_build(),
        )
        lock_input = build_lock_input(
            result,
            inputs=_no_build(default_groups=("black22", "black23")),
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
            inputs=_no_build(),
        )
        lock_input = build_lock_input(
            result,
            inputs=_no_build(conflicts=(_group_set("black22", "black23"),)),
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
        real_run_pass = engine_mod._run_pass

        def spy(*args: object) -> object:
            seen.append(dict(args[4]))  # type: ignore[call-overload]
            return real_run_pass(*args)  # type: ignore[arg-type]

        with patch.object(engine_mod, "_run_pass", spy):
            result = resolve_with_coordinator(
                self._black_coordinator(),
                _one_target(),
                forks=self._black_forks(),
                inputs=_no_build(),
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

    def _coordinator(self) -> FakeFetchPort:
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
            inputs=_no_build(),
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
            inputs=_no_build(),
        )
        lock_input = build_lock_input(
            result,
            inputs=_no_build(conflicts=(_extra_set("cpu", "gpu"),)),
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
            inputs=_no_build(),
        )
        assert result.success

        lock_input = build_lock_input(
            result,
            inputs=_no_build(
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
            inputs=_no_build(),
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
        with caplog.at_level(logging.WARNING, logger="nab_project._resolve.engine"):
            resolve_with_coordinator(
                self._coordinator(),
                _one_target(),
                forks=self._forks(),
                base_requirements=_reqs("base==9.9"),
                inputs=_no_build(),
            )
        assert any("Base attribution skipped" in rec.message for rec in caplog.records)


class TestTwoConflictSetsPartialInstall:
    """Two engaged conflict sets serve an install that draws from one.

    ``at-most-one`` lets an install pick no member of a set, so the lock of a
    four-fork resolve has to install the a1 packages for ``--extra a1`` alone,
    not only for a full ``(a, b)`` pair. The resolve runs through the engine so
    the gates come from the resolved graph rather than a hand-written
    ``package_gates``.
    """

    _GRAPH: ClassVar[dict[str, tuple[str, ...]]] = {
        "base": (),
        "pkga1": ("shared==1.0",),
        "pkga2": ("shared==2.0",),
        "pkgb1": (),
        "pkgb2": (),
    }

    def _coordinator(self) -> FakeFetchPort:
        listings = {name: [_make_wheel("1.0", package=name)] for name in self._GRAPH}
        listings["shared"] = [
            _make_wheel(version, package="shared") for version in ("1.0", "2.0")
        ]

        metadata: dict[str, str | None] = {}
        for name, wheels in listings.items():
            for wheel in wheels:
                assert wheel.metadata_url is not None
                requires = "".join(
                    f"Requires-Dist: {dep}\n" for dep in self._GRAPH.get(name, ())
                )
                metadata[wheel.metadata_url] = (
                    "Metadata-Version: 2.1\n"
                    f"Name: {name}\nVersion: {wheel.version}\n{requires}\n"
                )

        return make_coordinator(listings=listings, metadata_by_url=metadata)

    def _forks(self) -> list[ResolveFork]:
        return [
            ResolveFork(
                selection=(("extra", first), ("extra", second)),
                requirements=tuple(_reqs("base", f"pkg{first}", f"pkg{second}")),
                contexts=InstallContexts(
                    project=tuple(_reqs("base")),
                    selectors={
                        ("extra", first): tuple(_reqs(f"pkg{first}")),
                        ("extra", second): tuple(_reqs(f"pkg{second}")),
                    },
                ),
            )
            for first, second in itertools.product(("a1", "a2"), ("b1", "b2"))
        ]

    def _installed(
        self, extras: list[str], *, b_members: tuple[str, ...] = ("b1", "b2")
    ) -> set[str]:
        """Lock all four extras, then select ``extras`` from the emitted lock.

        ``b_members`` is what the b-set declares; only b1 and b2 are ever
        selected, so a longer tuple names a member no fork carries.
        """
        result = resolve_with_coordinator(
            self._coordinator(),
            _one_target(),
            forks=self._forks(),
            base_requirements=_reqs("base"),
            inputs=_no_build(),
        )
        assert result.success

        pylock = build_pylock(
            build_lock_input(
                result,
                inputs=_no_build(
                    conflicts=(_extra_set("a1", "a2"), _extra_set(*b_members))
                ),
                extras=("a1", "a2", "b1", "b2"),
            )
        )

        return {
            f"{pkg.name}=={pkg.version}"
            for pkg, _ in pylock.select(
                environment=dict(result.target_results[0].target.marker_env),  # type: ignore[arg-type]
                extras=extras,
                dependency_groups=(),
            )
        }

    def test_one_member_installs_its_own_packages(self) -> None:
        assert self._installed(["a1"]) == {
            "base==1.0",
            "pkga1==1.0",
            "shared==1.0",
        }

    def test_a_member_of_the_other_set_installs_alone(self) -> None:
        assert self._installed(["b1"]) == {"base==1.0", "pkgb1==1.0"}

    def test_one_member_of_each_set_installs_both(self) -> None:
        assert self._installed(["a1", "b1"]) == {
            "base==1.0",
            "pkga1==1.0",
            "pkgb1==1.0",
            "shared==1.0",
        }

    def test_no_extras_installs_the_base_alone(self) -> None:
        assert self._installed([]) == {"base==1.0"}

    def test_a_declared_member_no_fork_carries_gates_nothing(self) -> None:
        # b3 is declared conflicting but never selected, so the resolve
        # never forked over it and the lock does not offer it; the a1
        # packages install for a1 alone just as they do without it.
        assert self._installed(["a1"], b_members=("b1", "b2", "b3")) == {
            "base==1.0",
            "pkga1==1.0",
            "shared==1.0",
        }


class TestDroppedMembershipMarkerWarnedOnce:
    """One mistaken top-level entry is reported once per run, however
    many targets, forks and base passes read it."""

    def _coordinator(self) -> FakeFetchPort:
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
        with caplog.at_level(logging.WARNING, logger="nab_provider.resolver_inputs"):
            result = resolve_with_coordinator(
                self._coordinator(),
                self._two_targets(),
                forks=self._forks(),
                base_requirements=_reqs("base", 'gated ; extra == "test"'),
                inputs=_no_build(),
            )
        assert result.success
        warnings = [rec for rec in caplog.records if "membership marker" in rec.message]
        assert len(warnings) == 1

    def test_two_offending_requirements_each_warn_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        reqs = _reqs("base", 'gated ; extra == "test"', 'other ; "dev" in extras')
        with caplog.at_level(logging.WARNING, logger="nab_provider.resolver_inputs"):
            result = resolve_with_coordinator(
                self._coordinator(),
                self._two_targets(),
                reqs,
                inputs=_no_build(),
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

    def test_dropped_constraint_warns_once_across_targets_and_forks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The constraint pass shares the run's warned set with the root pass."""
        plain = tuple(_reqs("base"))
        forks = [
            ResolveFork((("extra", "cpu"),), plain),
            ResolveFork((("extra", "gpu"),), plain),
        ]
        with caplog.at_level(logging.WARNING, logger="nab_provider.resolver_inputs"):
            result = resolve_with_coordinator(
                self._coordinator(),
                self._two_targets(),
                forks=forks,
                base_requirements=plain,
                inputs=_no_build(constraints=('base<2.0 ; extra == "test"',)),
            )
        assert result.success
        warned = [
            rec.getMessage()
            for rec in caplog.records
            if "membership marker" in rec.message
        ]
        assert len(warned) == 1
        assert warned[0].startswith("Constraint 'base<2.0; extra == \"test\"'")


class TestDirectPackages:
    """The direct set handed to the provider: the canonical names of the
    root requirements this target kept."""

    def _direct_packages(self, *texts: str) -> frozenset[str]:
        """The ``direct_packages`` a resolve of ``texts`` hands the provider."""
        coordinator = _make_coordinator({})
        with (
            patch.object(engine_mod, "Provider") as provider_cls,
            patch.object(engine_mod, "Resolver") as resolver_cls,
            patch.object(engine_mod, "build_target_lock"),
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

    def _coordinator(self) -> FakeFetchPort:
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
            inputs=_no_build(),
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

    def _coordinator(self) -> FakeFetchPort:
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
            metadata_url=linux_wheel.metadata_url,
        )
        coordinator.index.store_metadata(
            "pkg",
            "1.0",
            "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\nRequires-Dist: windep\n\n",
            metadata_url=win_wheel.metadata_url,
        )
        return coordinator

    def test_each_target_reads_its_own_wheel(self) -> None:
        targets = Matrix(
            python="==3.11",
            platforms=(PlatformSpec("linux_x86_64"), PlatformSpec("windows_amd64")),
        ).expand()
        result = resolve_with_coordinator(
            self._coordinator(), targets, _reqs("pkg"), inputs=_no_build()
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


class TestMatrixMetadataReadGranularity:
    """Which metadata a matrix reads per wheel, and which per version.

    A wheel's metadata comes from its own sidecar, so a matrix asks for one
    URL per wheel its targets pick between them.  An sdist's ``PKG-INFO``
    stands for the version, so one read serves the whole matrix.  Collapsing
    repeat requests for one URL is the coordinator's job, covered by
    ``property_python/test_fetch_coordinator.py``.
    """

    def _wheel(self, tag: str) -> WheelFile:
        """A ``pkg`` 1.0 wheel tagged ``tag``, advertising a sidecar."""
        return WheelFile(
            filename=f"pkg-1.0-{tag}.whl",
            url=f"https://example.com/pkg-1.0-{tag}.whl",
            version="1.0",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )

    def _resolve_three_targets(self, coordinator: FakeFetchPort) -> None:
        """Resolve ``pkg`` for 3.11 through 3.13 on one platform, and expect success."""
        targets = Matrix(
            python=">=3.11,<3.14", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()
        assert len(targets) == 3

        result = resolve_with_coordinator(
            coordinator, targets, _reqs("pkg"), inputs=_no_build()
        )
        assert result.success

    def _metadata_urls(self, wheels: list[WheelFile]) -> set[str]:
        """The distinct sidecar URLs the three targets ask for, given ``wheels``."""
        coordinator = make_coordinator(listings={"pkg": wheels}, auto_metadata=True)
        self._resolve_three_targets(coordinator)

        calls = coordinator.calls_to("request_metadata")
        return {url for _package, _version, url, _hash in calls}

    def test_two_wheels_across_three_targets_name_two_urls(self) -> None:
        """3.11 picks its own wheel; 3.12 and 3.13 both pick the universal one."""
        universal = self._wheel("py3-none-any")
        for_311 = self._wheel("cp311-cp311-manylinux_2_17_x86_64")

        urls = self._metadata_urls([universal, for_311])

        assert urls == {universal.metadata_url, for_311.metadata_url}

    def test_a_wheel_per_interpreter_is_a_url_per_target(self) -> None:
        """Nothing is shared when every target ranks a different wheel first."""
        wheels = [
            self._wheel(f"cp3{minor}-cp3{minor}-manylinux_2_17_x86_64")
            for minor in (11, 12, 13)
        ]

        assert self._metadata_urls(wheels) == {wheel.metadata_url for wheel in wheels}

    def test_an_sdist_is_read_once_for_the_whole_matrix(self) -> None:
        """No wheel to pick, so all three targets read the one PKG-INFO."""
        coordinator = make_coordinator(
            listings={"pkg": [_make_sdist("1.0", package="pkg")]},
            sdist_pkg_info="Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\n",
        )

        self._resolve_three_targets(coordinator)

        assert len(coordinator.calls_to("request_sdist")) == 1


_FORTY_SHA = "0123456789abcdef0123456789abcdef01234567"


class TestBuildResolverInputs:
    """``build_resolver_inputs`` builds the resolver-input dict per env."""

    def test_marker_true_keeps_requirement(self) -> None:
        """A requirement whose marker matches the env is kept."""
        env = _linux_311().marker_env
        out = build_resolver_inputs(
            _reqs('pkg; sys_platform == "linux"'),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).ranges
        assert "pkg" in out

    def test_marker_false_drops_requirement(self) -> None:
        """A requirement whose marker excludes the env is dropped."""
        env = _linux_311().marker_env
        out = build_resolver_inputs(
            _reqs('pkg; sys_platform == "win32"'),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).ranges
        assert out == {}

    def test_set_marker_drops_without_crash(self) -> None:
        """A lockfile-only set marker is empty at resolve time, so the dep drops."""
        env = _linux_311().marker_env
        out = build_resolver_inputs(
            _reqs('pkg ; "x" in extras'),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).ranges
        assert out == {}

    def test_extras_get_separate_entries(self) -> None:
        """Extras become ``name[extra]`` entries with any-version range."""
        env = _linux_311().marker_env
        out = build_resolver_inputs(
            _reqs("pkg[foo,bar]"),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).ranges
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
        out = build_resolver_inputs(
            [req],
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).ranges
        proxy_keys = [k for k in out if k.startswith("pkg[")]
        assert proxy_keys == ["pkg[a]", "pkg[b]", "pkg[c]"]

    def test_no_specifier_yields_any(self) -> None:
        """An unconstrained requirement gets the any() range."""
        env = _linux_311().marker_env
        out = build_resolver_inputs(
            _reqs("pkg"),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).ranges
        assert out["pkg"] == VersionRange.full(admit_arbitrary=False)

    def test_specifier_yields_intervals(self) -> None:
        """A bounded specifier produces the corresponding interval."""
        env = _linux_311().marker_env
        out = build_resolver_inputs(
            _reqs("pkg>=1.0,<2.0"),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).ranges
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
        out = build_resolver_inputs(
            _reqs("pkg[ext]===1.0.special"),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).ranges
        assert "pkg" in out
        assert "1.0.special" in out["pkg"]
        assert Version("1.0") not in out["pkg"]
        assert "pkg[ext]" in out

    def test_duplicate_name_intersects(self) -> None:
        """Two requirements for one package combine to their overlap."""
        env = _linux_311().marker_env
        out = build_resolver_inputs(
            _reqs("pkg>=2.0", "pkg<3.0"),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).ranges
        assert Version("2.5") in out["pkg"]
        assert Version("1.0") not in out["pkg"]
        assert Version("5.0") not in out["pkg"]

    def test_conflicting_names_stay_separate_roots(self) -> None:
        """Contradictory pins reach the solver as their own clauses."""
        env = _linux_311().marker_env
        inputs = build_resolver_inputs(
            _reqs("pkg==1.0", "pkg==2.0"),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        )
        assert [root.origin for root in inputs.roots] == ["pkg==1.0", "pkg==2.0"]
        assert inputs.ranges["pkg"].is_empty

    def test_constraint_extras_rejected(self) -> None:
        """A constraint carrying extras is rejected, matching pip."""
        env = _linux_311().marker_env
        with pytest.raises(ConfigError, match="extras"):
            build_resolver_inputs(
                _reqs("pkg[dev]<2.0"),
                VcsConfig(),
                environment=env,
                marker_holds=dependency_marker_holds,
                kind="constraint",
            )

    def test_marker_false_drops_constraint(self) -> None:
        """A constraint whose marker excludes the env is dropped.

        The marker is evaluated per env for constraints too, so a
        constraint gated off this target never binds. The single-env
        path once enforced such constraints unconditionally (issue #38).
        """
        env = _linux_311().marker_env
        out = build_resolver_inputs(
            _reqs('pkg<2.0 ; sys_platform == "win32"'),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
            kind="constraint",
        ).ranges
        assert out == {}

    def test_marker_true_keeps_constraint(self) -> None:
        """A constraint whose marker matches the env binds its range."""
        env = _linux_311().marker_env
        out = build_resolver_inputs(
            _reqs('pkg<2.0 ; sys_platform == "linux"'),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
            kind="constraint",
        ).ranges
        assert Version("1.0") in out["pkg"]
        assert Version("2.0") not in out["pkg"]

    def test_extra_proxy_key_normalized(self) -> None:
        """The proxy key is PEP 685 normalized."""
        env = _linux_311().marker_env
        out = build_resolver_inputs(
            _reqs("pkg[My_Extra]"),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).ranges
        assert "pkg[my-extra]" in out

    def test_plain_url_requirement_refused(self) -> None:
        """A plain archive URL is refused as an unsupported scheme."""
        env = _linux_311().marker_env
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            build_resolver_inputs(
                _reqs("pkg @ https://example.com/pkg.whl"),
                VcsConfig(),
                environment=env,
                marker_holds=dependency_marker_holds,
            )

    def test_vcs_url_refused_by_default_policy(self) -> None:
        """A git+https requirement is refused under the default BLOCK policy."""
        env = _linux_311().marker_env
        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            build_resolver_inputs(
                _reqs(f"pkg @ git+https://example.com/pkg.git@{_FORTY_SHA}"),
                VcsConfig(),
                environment=env,
                marker_holds=dependency_marker_holds,
            )

    def test_url_constraint_refused(self) -> None:
        """A direct-URL constraint is refused the same way as a requirement."""
        env = _linux_311().marker_env
        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            build_resolver_inputs(
                _reqs(f"pkg @ git+https://example.com/pkg.git@{_FORTY_SHA}"),
                VcsConfig(),
                environment=env,
                marker_holds=dependency_marker_holds,
                kind="constraint",
            )

    def test_admitted_vcs_url_raises_not_implemented(self) -> None:
        """An admitted VCS requirement still has no resolver path."""
        env = _linux_311().marker_env
        vcs = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://example.com/",),
        )
        with pytest.raises(NotImplementedError, match="not implemented"):
            build_resolver_inputs(
                _reqs(f"pkg @ git+https://example.com/pkg.git@{_FORTY_SHA}"),
                vcs,
                environment=env,
                marker_holds=dependency_marker_holds,
            )


class TestProxyConstraints:
    """``ProxyConstraints`` answers an extras proxy with its base's bound."""

    def test_a_proxy_key_reads_the_base_bound(self) -> None:
        """``aaa[x]`` reads ``aaa``'s bound; an unconstrained base has none."""
        bound = Requirement("aaa<3.0").specifier.to_range()
        constraints = ProxyConstraints({"aaa": bound})

        assert constraints["aaa[x]"] == bound
        assert constraints.get("bbb[x]") is None

        # Iteration stays over the keys the user wrote.
        assert dict(constraints) == {"aaa": bound}


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
        excluded = build_resolver_inputs(
            reqs,
            VcsConfig(),
            environment=_linux_311().marker_env,
            marker_holds=dependency_marker_holds,
        ).ranges
        assert "some-dep" not in excluded
        included_env = {
            **_linux_311().marker_env,
            "python_version": "3.9",
            "python_full_version": "3.9.0",
        }
        included = build_resolver_inputs(
            reqs,
            VcsConfig(),
            environment=included_env,
            marker_holds=dependency_marker_holds,
        ).ranges
        assert "some-dep" in included


class TestRootExtras:
    """``build_resolver_inputs`` also reports the extras the root requested."""

    def test_recovers_and_normalizes_extras(self) -> None:
        env = _linux_311().marker_env
        root_extras = build_resolver_inputs(
            _reqs("pkg[My_Extra]", "other"),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).extras
        assert root_extras == {("pkg", "my-extra")}

    def test_no_extras_yields_empty(self) -> None:
        env = _linux_311().marker_env
        root_extras = build_resolver_inputs(
            _reqs("pkg"),
            VcsConfig(),
            environment=env,
            marker_holds=dependency_marker_holds,
        ).extras
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
                inputs=_no_build(
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
            inputs=_no_build(vcs=self._allow(), vcs_sources=(self._source(),)),
            cache_dir=None,
        )

        assert result.success
        assert result.target_results[0].pins == {"pkg": Version("1.0")}

        assert len(roots) == 1
        assert not roots[0].exists()

    def test_vcs_source_resolves_under_a_relative_cache_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``nab lock --cache-dir relcache`` materialises a declared VCS source.

        ``cache-dir`` is cwd-relative, so the clone root can be relative.
        """

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
        monkeypatch.chdir(tmp_path)

        result = resolve_with_coordinator(
            _make_coordinator({}),
            _one_target(),
            _reqs("pkg"),
            inputs=_no_build(vcs=self._allow(), vcs_sources=(self._source(),)),
            cache_dir=Path("relcache"),
        )

        assert result.success
        assert result.target_results[0].pins == {"pkg": Version("1.0")}

    def test_poisoned_legacy_cached_clone_is_not_reused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clone written before source-cache isolation cannot affect a resolve."""
        repo_key = "repo-key"
        legacy = tmp_path / "vcs" / "vcs" / repo_key / _FORTY_SHA
        (legacy / ".git").mkdir(parents=True)
        (legacy / ".git" / "nab-complete").touch()
        legacy_pyproject = legacy / "pyproject.toml"
        legacy_pyproject.write_text(
            '[project]\nname = "pkg"\nversion = "99.0"\n', encoding="utf-8"
        )

        git_commands: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            git_commands.append(cmd[1])
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            if cmd[:2] == ["git", "checkout"]:
                (cwd / "pyproject.toml").write_text(
                    '[project]\nname = "pkg"\nversion = "1.0"\n', encoding="utf-8"
                )
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(vcs_mod, "_repo_key", lambda _url: repo_key)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = resolve_with_coordinator(
            _make_coordinator({}),
            _one_target(),
            _reqs("pkg"),
            inputs=_no_build(vcs=self._allow(), vcs_sources=(self._source(),)),
            cache_dir=tmp_path,
        )

        current = tmp_path / VCS_BUCKET / "vcs" / repo_key / _FORTY_SHA
        assert result.success
        assert result.target_results[0].pins == {"pkg": Version("1.0")}
        assert git_commands == ["init", "fetch", "checkout"]
        assert (current / ".git" / "nab-complete").is_file()
        assert 'version = "1.0"' in (current / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        assert 'version = "99.0"' in legacy_pyproject.read_text(encoding="utf-8")


_MARCH_2024 = datetime(2024, 3, 1, tzinfo=timezone.utc)

_NO_BUILD = ResolveInputs(build_policy=BuildPolicy.NEVER)


class TestCutoffAndOverridePlumbing:
    """The upload cutoff and the two override tables reach the provider.

    Each setting is driven through ``resolve_with_coordinator``, so the
    pins show whether it arrived.
    """

    def _coordinator(self) -> FakeFetchPort:
        """Two foo releases either side of March 2024, plus a bar to depend on."""
        return _make_coordinator(
            {
                "foo": [
                    _make_wheel(
                        "2.0", package="foo", upload_time="2024-06-01T00:00:00Z"
                    ),
                    _make_wheel(
                        "1.0", package="foo", upload_time="2024-01-01T00:00:00Z"
                    ),
                ],
                "bar": [_make_wheel("1.0", package="bar")],
            }
        )

    def _pins(self, inputs: ResolveInputs = _NO_BUILD) -> dict[str, Version]:
        """Resolve ``foo`` under ``inputs``, from a project that builds nothing."""
        result = resolve_with_coordinator(
            self._coordinator(),
            _one_target(),
            _reqs("foo"),
            inputs=inputs,
        )
        assert result.success

        return result.target_results[0].pins

    def test_project_cutoff_excludes_the_newer_release(self) -> None:
        """``[tool.nab] uploaded-prior-to`` narrows the candidate listing."""
        assert self._pins() == {"foo": Version("2.0")}

        cutoff = _NO_BUILD.replace(uploaded_prior_to=_MARCH_2024)
        assert self._pins(cutoff) == {"foo": Version("1.0")}

    def test_package_override_replaces_declared_dependencies(self) -> None:
        """``[tool.nab.packages.<name>] dependencies`` reaches the resolve.

        The listing declares no dependencies, so bar is pinned only
        because the override supplies it.
        """
        assert self._pins() == {"foo": Version("2.0")}

        requirement = Requirement("foo")
        override = _NO_BUILD.replace(
            package_overrides=(
                PackageOverride(
                    requirement=requirement,
                    name="foo",
                    version_range=requirement.specifier.to_range(),
                    dependencies=(Requirement("bar"),),
                    name_keyed=True,
                ),
            )
        )
        assert self._pins(override) == {
            "foo": Version("2.0"),
            "bar": Version("1.0"),
        }

    def test_index_override_cutoff_excludes_the_newer_release(self) -> None:
        """``[tool.nab.index.<name>] uploaded-prior-to`` reaches the resolve."""
        assert self._pins() == {"foo": Version("2.0")}

        override = _NO_BUILD.replace(
            index_overrides={"pypi": IndexOverride(uploaded_prior_to=_MARCH_2024)}
        )
        assert self._pins(override) == {"foo": Version("1.0")}


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

        ``build_resolver_inputs`` raises before the resolver runs, so
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

    def test_a_marker_consulted_on_every_target_is_read_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Declaring environments calls marker_variables once per distinct text."""
        linux, windows = _linux_311(), _windows_311()
        marker = Marker('platform_system == "Linux"')
        results = [
            TargetResult(
                target=target,
                success=True,
                pins={"pkg": Version("1.0")},
                consulted=frozenset({marker}),
                lock=TargetLock(
                    target=target,
                    pins={"pkg": IndexPin(name="pkg", version="1.0", index="pypi")},
                ),
            )
            for target in (linux, windows)
        ]

        texts: list[str] = []
        real_marker_variables = resolve_mod.marker_variables

        def spy_marker_variables(text: str) -> frozenset[str]:
            texts.append(text)
            return real_marker_variables(text)

        monkeypatch.setattr(resolve_mod, "marker_variables", spy_marker_variables)
        build_lock_input(
            ResolveResult(targets=(linux, windows), target_results=results)
        )

        assert texts == ['platform_system == "Linux"']

    def test_a_double_quote_in_a_consulted_marker_value_still_locks(self) -> None:
        """A marker value carrying a double quote still locks.

        urllib3 1.11 gates a dependency on
        ``extra == 'secure;python_version>"2.7"'``, and the lock's
        ``environments`` come from re-parsing the markers the resolve read,
        so the value has to survive that round trip.
        """
        metadata = (
            "Metadata-Version: 2.1\n"
            "Name: foo\n"
            "Version: 1.0\n"
            'Provides-Extra: secure;python_version>"2.7"\n'
            "Requires-Dist: mid ; extra == 'secure;python_version>\"2.7\"'\n"
        )

        coordinator = make_coordinator(
            listings={
                "foo": [_make_wheel("1.0", package="foo")],
                "mid": [_make_wheel("2.0", package="mid")],
            },
            metadata_by_version={
                "1.0": metadata,
                "2.0": "Metadata-Version: 2.1\nName: mid\nVersion: 2.0\n",
            },
        )
        result = resolve_with_coordinator(
            coordinator, [_linux_311()], _reqs("foo"), inputs=_no_build()
        )

        assert result.success
        assert set(result.target_results[0].pins) == {"foo"}

        merged = build_lock_input(result, inputs=_no_build())
        assert set(merged.targets) == {"py311-linux_x86_64"}
        assert [str(row) for row in merged.environments] == [
            (
                'python_version == "3.11" and sys_platform == "linux"'
                ' and platform_machine == "x86_64"'
            )
        ]


class TestServingIndexInLock:
    """A resolve records each pin's index URL from the coordinator's indexes."""

    def test_pin_records_the_index_that_served_it(self) -> None:
        """``foo`` is served by a private index, ``bar`` by the default one."""
        internal_url = "https://internal.example/simple/"
        coordinator = _make_coordinator(
            {
                "foo": [_make_wheel("1.0", package="foo")],
                "bar": [_make_wheel("1.0", package="bar")],
            }
        )

        # A resolve that never passed its indexes on would still record PyPI
        # for every pin, so one listing has to come from a second index.
        coordinator.indexes = [
            *coordinator.indexes,
            IndexConfig("internal", internal_url),
        ]
        coordinator.index.store_listing_index("foo", "internal")

        result = resolve_with_coordinator(
            coordinator, _one_target(), _reqs("foo", "bar"), inputs=_no_build()
        )

        assert result.success
        lock = result.target_results[0].lock
        assert lock is not None

        foo_pin, bar_pin = lock.pins["foo"], lock.pins["bar"]
        assert isinstance(foo_pin, IndexPin)
        assert isinstance(bar_pin, IndexPin)

        assert foo_pin.index == internal_url
        assert bar_pin.index == DEFAULT_INDEX_URL


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
    def _extra_backtrack_coordinator() -> FakeFetchPort:
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
            metadata_url=aa_wheels[-1].metadata_url,
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
    def _narrowed_base_coordinator() -> FakeFetchPort:
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
            metadata_url=aa_wheels[-1].metadata_url,
        )
        coordinator.index.store_metadata(
            "bb",
            "2.0",
            "Metadata-Version: 2.1\nName: bb\nVersion: 2.0\nRequires-Dist: aa<3.0\n\n",
            metadata_url=bb_wheels[-1].metadata_url,
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
            inputs=ResolveInputs(constraints=("pkg<2.0",)),
        )
        assert result.success
        assert result.target_results[0].pins == {"pkg": Version("1.0")}

    @staticmethod
    def _dropped_extra_coordinator() -> FakeFetchPort:
        """An index whose newest ``aaa`` no longer declares the ``x`` extra.

        1.0 and 2.0 declare ``x`` and pull ``bbb`` through it; 3.0
        provides no extras at all.
        """
        aaa_wheels = [_make_wheel(v, package="aaa") for v in ("1.0", "2.0", "3.0")]
        coordinator = make_coordinator(
            listings={"aaa": aaa_wheels, "bbb": [_make_wheel("1.0", package="bbb")]},
            auto_metadata=True,
        )
        for wheel in aaa_wheels[:-1]:
            coordinator.index.store_metadata(
                "aaa",
                wheel.version,
                f"Metadata-Version: 2.1\nName: aaa\nVersion: {wheel.version}\n"
                "Provides-Extra: x\n"
                'Requires-Dist: bbb; extra == "x"\n\n',
                metadata_url=wheel.metadata_url,
            )
        return coordinator

    @pytest.mark.parametrize(
        "constraint", ["aaa==2.0", "aaa<=2.0", "aaa<3.0", "aaa!=3.0"]
    )
    def test_constraint_bounds_an_extras_proxy(self, constraint: str) -> None:
        """A constraint on the base bounds ``aaa[x]`` as well.

        The proxy decides before its base, so a constraint it cannot see
        lets it pin ``aaa==3.0`` and abort on the extra 3.0 dropped.
        """
        result = resolve_with_coordinator(
            self._dropped_extra_coordinator(),
            _one_target(),
            _reqs("aaa[x]"),
            inputs=ResolveInputs(constraints=(constraint,)),
        )
        assert result.success
        assert result.target_results[0].pins == {
            "aaa": Version("2.0"),
            "bbb": Version("1.0"),
        }

    def test_constraint_that_empties_an_extras_proxy_is_blamed(self) -> None:
        """A constraint that leaves the proxy nothing is named in the failure.

        Three versions of ``aaa`` exist, so blaming the proxy's own
        listing would be wrong.
        """
        result = resolve_with_coordinator(
            self._dropped_extra_coordinator(),
            _one_target(),
            _reqs("aaa[x]"),
            inputs=ResolveInputs(constraints=("aaa<0.5",)),
        )
        assert not result.success
        message = str(result.target_results[0].error)
        assert "the user constrained aaa[x]" in message
        assert "0.5" in message
        assert "no versions of aaa[x]" not in message

    def test_constraint_off_the_extra_still_reports_the_miss(self) -> None:
        """A constraint pinning a version without the extra reports it.

        The constraint leaves ``aaa==3.0`` the only candidate, so the
        proxy pins it and the miss is reported against it.
        """
        with pytest.raises(MissingExtraError, match="aaa==3.0"):
            resolve_with_coordinator(
                self._dropped_extra_coordinator(),
                _one_target(),
                _reqs("aaa[x]"),
                inputs=ResolveInputs(constraints=("aaa==3.0",)),
            )

    @staticmethod
    def _transitive_proxy_coordinator(*, newest_dep: str = "") -> FakeFetchPort:
        """An index where ``ccc`` reaches ``aaa`` only through ``aaa[x]``.

        ``aaa`` 1.0 and 2.0 declare ``x`` and pull ``bbb`` through it; 3.0
        declares no extras and appends ``newest_dep`` to its METADATA.
        """
        aaa_wheels = [_make_wheel(v, package="aaa") for v in ("1.0", "2.0", "3.0")]
        ccc_wheel = _make_wheel("1.0", package="ccc")
        coordinator = make_coordinator(
            listings={
                "aaa": aaa_wheels,
                "bbb": [_make_wheel("1.0", package="bbb")],
                "ccc": [ccc_wheel],
            },
            auto_metadata=True,
        )

        for wheel in aaa_wheels[:-1]:
            coordinator.index.store_metadata(
                "aaa",
                wheel.version,
                f"Metadata-Version: 2.1\nName: aaa\nVersion: {wheel.version}\n"
                "Provides-Extra: x\n"
                'Requires-Dist: bbb; extra == "x"\n\n',
                metadata_url=wheel.metadata_url,
            )

        coordinator.index.store_metadata(
            "aaa",
            "3.0",
            f"Metadata-Version: 2.1\nName: aaa\nVersion: 3.0\n{newest_dep}\n",
            metadata_url=aaa_wheels[-1].metadata_url,
        )

        coordinator.index.store_metadata(
            "ccc",
            "1.0",
            "Metadata-Version: 2.1\nName: ccc\nVersion: 1.0\nRequires-Dist: aaa[x]\n\n",
            metadata_url=ccc_wheel.metadata_url,
        )
        return coordinator

    def test_constraint_bounds_a_transitive_extras_proxy(self) -> None:
        """A constraint bounds a proxy that arrived through a dependency.

        3.0 is out of bounds, so the resolve must not read its metadata:
        the direct-URL dependency it declares would be refused and abort
        the resolve.
        """
        result = resolve_with_coordinator(
            self._transitive_proxy_coordinator(
                newest_dep="Requires-Dist: zzz @ https://example.com/zzz-1.0.zip\n"
            ),
            _one_target(),
            _reqs("ccc"),
            inputs=ResolveInputs(constraints=("aaa<3.0",)),
        )
        assert result.success
        assert result.target_results[0].pins == {
            "aaa": Version("2.0"),
            "bbb": Version("1.0"),
            "ccc": Version("1.0"),
        }

    def test_constraint_keeps_a_transitive_proxy_off_the_missing_extra(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The bound is applied before the proxy checks ``Provides-Extra``.

        3.0 drops the extra, and warning about it would name a version
        the lock cannot contain.
        """
        with caplog.at_level(logging.WARNING):
            result = resolve_with_coordinator(
                self._transitive_proxy_coordinator(),
                _one_target(),
                _reqs("ccc"),
                inputs=ResolveInputs(constraints=("aaa<3.0",)),
            )

        assert result.success
        assert result.target_results[0].pins == {
            "aaa": Version("2.0"),
            "bbb": Version("1.0"),
            "ccc": Version("1.0"),
        }

        assert [r.getMessage() for r in caplog.records if "aaa" in r.getMessage()] == []

    def test_constraint_that_empties_a_transitive_proxy_is_blamed(self) -> None:
        """The failure names the constraint, not the proxy's listing.

        Three versions of ``aaa`` exist, so blaming ``aaa[x]``'s own
        listing would be wrong.
        """
        result = resolve_with_coordinator(
            self._transitive_proxy_coordinator(),
            _one_target(),
            _reqs("ccc"),
            inputs=ResolveInputs(constraints=("aaa<0.5",)),
        )
        assert not result.success

        message = str(result.target_results[0].error)
        assert "the user constrained aaa[x]" in message
        assert "0.5" in message
        assert "no versions of aaa[x]" not in message

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
            inputs=ResolveInputs(resolution=ResolutionStrategy.HIGHEST),
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
        self, coordinator: FakeFetchPort, matrix: Matrix, local: LocalSource
    ) -> ResolveResult:
        return resolve_with_coordinator(
            coordinator,
            matrix.expand(),
            _reqs("foo"),
            inputs=ResolveInputs(local_sources=(local,)),
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

    def test_patch_below_local_requires_python_resolves(self, tmp_path: Path) -> None:
        """A floor above the pinned patch is still the 3.13 language.

        The pin says which micro the lock's markers describe, not which
        micros the source supports, so the check runs at the minor.
        """
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
        assert result.success
        assert str(result.target_results[0].pins["foo"]) == "1.0"


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
            inputs=ResolveInputs(archive_sources=(source,)),
            cache_dir=tmp_path,
        )

        assert result.success
        assert len(result.target_results) == 2
        for target_result in result.target_results:
            assert str(target_result.pins["foo"]) == "1.0"
            assert str(target_result.pins["bar"]) == "2.0"

    def test_poisoned_legacy_cached_tree_is_not_reused(self, tmp_path: Path) -> None:
        """A tree written before source-cache isolation cannot affect a resolve."""
        fresh = '[project]\nname = "foo"\nversion = "1.0"\n'
        data = _archive_bytes("foo", "1.0", fresh)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.tar.gz#sha256={digest}"
        )

        legacy_entry = tmp_path / "archive" / digest
        legacy_tree = legacy_entry / "tree"
        legacy_tree.mkdir(parents=True)
        (legacy_tree / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "99.0"\n', encoding="utf-8"
        )
        (legacy_entry / ".nab-hashes").write_text(f"sha256={digest}", encoding="utf-8")
        (legacy_entry / ".nab-complete").touch()

        coord = make_coordinator([], package="foo")
        coord.index.store_sdist_archive("foo", digest, data)

        result = resolve_with_coordinator(
            coord,
            _one_target(),
            _reqs("foo"),
            inputs=ResolveInputs(archive_sources=(source,)),
            cache_dir=tmp_path,
        )

        assert result.success
        assert str(result.target_results[0].pins["foo"]) == "1.0"
        assert (tmp_path / ARCHIVE_BUCKET / digest / ".nab-complete").is_file()
        assert 'version = "99.0"' in (legacy_tree / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        assert len(coord.calls_to("request_direct_archive")) == 1

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
            inputs=ResolveInputs(archive_sources=(source,)),
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
            inputs=ResolveInputs(archive_sources=(source,)),
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

    def _coordinator(self, wheels: list[WheelFile]) -> FakeFetchPort:
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

    def _record_parse_passes(self) -> tuple[list[bool], object]:
        """Return each parse pass's ``target_drops``, and the patch that records them.

        False is the pass a matrix's Pythons share; True is the pass that
        carries the drops, which a resolve with nothing to share runs per
        Python.
        """
        passes: list[bool] = []
        real = listing_mod._prepare_listing

        def recording(*args: Any, **kwargs: Any) -> Any:
            passes.append(bool(kwargs["target_drops"]))
            return real(*args, **kwargs)

        return passes, recording

    def test_platform_targets_share_one_base_filter(self) -> None:
        """Targets differing only by platform reuse the base filter result."""
        wheels = [self._wheel("1.0"), self._wheel("2.0")]
        calls, counting = self._count_base_filters()

        with patch.object(listing_mod, "_filter_base", counting):
            result = resolve_with_coordinator(
                self._coordinator(wheels),
                self._targets("==3.11"),
                _reqs("pkg"),
                inputs=_no_build(),
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
                inputs=_no_build(),
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
                inputs=_no_build(),
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

    def test_a_matrix_of_pythons_parses_the_listing_once(self) -> None:
        """The Pythons filter separately over one shared parse-and-policy pass."""
        wheels = [self._wheel("1.0"), self._wheel("2.0", requires_python=">=3.12")]
        calls, counting = self._count_base_filters()
        passes, recording = self._record_parse_passes()

        with (
            patch.object(listing_mod, "_filter_base", counting),
            patch.object(listing_mod, "_prepare_listing", recording),
        ):
            result = resolve_with_coordinator(
                self._coordinator(wheels),
                self._targets(">=3.11,<3.13"),
                _reqs("pkg"),
                inputs=_no_build(),
                align_across_targets=False,
            )

        assert result.success
        assert sorted(calls) == ["pkg@3.11.0", "pkg@3.12.0"]
        assert passes == [False]

    def test_two_micros_of_one_minor_are_two_pythons(self) -> None:
        """Sharing follows the release the memo keys on, not the PEP 508 minor."""
        wheels = [self._wheel("1.0"), self._wheel("2.0")]
        calls, counting = self._count_base_filters()
        passes, recording = self._record_parse_passes()

        with (
            patch.object(listing_mod, "_filter_base", counting),
            patch.object(listing_mod, "_prepare_listing", recording),
        ):
            result = resolve_with_coordinator(
                self._coordinator(wheels),
                [
                    ResolveTarget.for_declared(
                        python_version="3.11",
                        spec=PlatformSpec(platform),
                        python_full_version=micro,
                    )
                    for platform, micro in (
                        ("linux_x86_64", "3.11.2"),
                        ("windows_amd64", "3.11.5"),
                    )
                ],
                _reqs("pkg"),
                inputs=_no_build(),
                align_across_targets=False,
            )

        assert result.success
        assert sorted(calls) == ["pkg@3.11.2", "pkg@3.11.5"]
        assert passes == [False]

    def test_reused_filter_still_counts_distributions_per_target(self) -> None:
        """Every target reports the files it saw, memo hit or not."""
        wheels = [self._wheel("1.0"), self._wheel("2.0")]

        result = resolve_with_coordinator(
            self._coordinator(wheels),
            self._targets("==3.11"),
            _reqs("pkg"),
            inputs=_no_build(),
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
    def _coordinator(metadata: dict[str, str]) -> FakeFetchPort:
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
            engine_mod,
            "_resolve_one_target",
            wraps=engine_mod._resolve_one_target,
        ) as spy:
            result = resolve_with_coordinator(
                coordinator, targets, _reqs("foo"), inputs=_no_build()
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
            engine_mod,
            "_resolve_one_target",
            wraps=engine_mod._resolve_one_target,
        ) as spy:
            result = resolve_with_coordinator(
                coordinator,
                [ResolveTarget.for_host()],
                _reqs("foo"),
                inputs=_no_build(),
            )

        assert result.success
        assert len(result.target_results) == 1
        assert spy.call_count == 1

    @pytest.mark.parametrize(
        "value",
        [
            'x;python_full_version>="3.10.4"',
            'a python_full_version in "3.10.4"',
        ],
    )
    def test_a_quote_in_a_marker_value_does_not_cut_the_minor(self, value: str) -> None:
        """A single-quoted value is consumed whole by the micro scanner, so
        the clause inside it neither splits 3.10 at a boundary no dependency
        gates on nor trips the splitter on an operator it cannot tile."""
        coordinator = self._coordinator(
            {
                "1.0": self._meta("foo", "1.0", f"mid ; platform_release == '{value}'"),
                "2.0": self._meta("mid", "2.0"),
            }
        )
        targets = Matrix(
            python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()

        result = resolve_with_coordinator(
            coordinator, targets, _reqs("foo"), inputs=_no_build()
        )

        assert result.success
        assert self._pins_by_label(result) == {"py310-linux_x86_64": {"foo"}}

        merged = build_lock_input(result, inputs=_no_build())
        assert [str(row) for row in merged.environments] == [
            (
                'python_version == "3.10" and sys_platform == "linux"'
                ' and platform_machine == "x86_64"'
            )
        ]

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
            coordinator, [target], _reqs("foo"), inputs=_no_build()
        )

        assert result.success
        assert self._pins_by_label(result) == {
            "py311-host-pf3110": {"foo", "mid"},
            "py311-host-pf3114": {"foo"},
        }
        rows = build_lock_input(result, inputs=_no_build()).environments
        assert len(rows) == 2
        real_3119 = {
            "python_version": "3.11",
            "python_full_version": "3.11.9",
            "sys_platform": "linux",
            "platform_machine": "x86_64",
            "implementation_name": "cpython",
        }
        assert any(row.evaluate(real_3119) for row in rows)

    def test_an_epoch_tagged_marker_leaves_the_minor_whole(self) -> None:
        """``>= "1!3.10.4"`` is False on every interpreter, since none reports
        an epoch. Cutting the minor there would resolve a slice at
        ``1!3.10.4`` and gate ``mid`` behind a row nothing matches.
        """
        coordinator = self._coordinator(
            {
                "1.0": self._meta(
                    "foo", "1.0", 'mid ; python_full_version >= "1!3.10.4"'
                ),
                "2.0": self._meta("mid", "2.0"),
            }
        )
        targets = Matrix(
            python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()

        result = resolve_with_coordinator(
            coordinator, targets, _reqs("foo"), inputs=_no_build()
        )

        assert result.success
        assert self._pins_by_label(result) == {"py310-linux_x86_64": {"foo"}}
        rows = build_lock_input(result, inputs=_no_build()).environments
        assert len(rows) == 1

    def test_an_epoch_tagged_marker_does_not_fail_the_resolve(self) -> None:
        """A dep the index does not serve, gated on an epoch, is inactive on
        every real 3.10, so the resolve succeeds rather than failing on a
        slice no interpreter reaches.
        """
        coordinator = self._coordinator(
            {
                "1.0": self._meta(
                    "foo", "1.0", 'absent ; python_full_version >= "1!3.10.4"'
                )
            }
        )
        targets = Matrix(
            python="==3.10", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()

        result = resolve_with_coordinator(
            coordinator, targets, _reqs("foo"), inputs=_no_build()
        )

        assert result.success
        assert self._pins_by_label(result) == {"py310-linux_x86_64": {"foo"}}

    def _fixpoint_coordinator(self) -> FakeFetchPort:
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
            self._fixpoint_coordinator(), targets, _reqs("foo"), inputs=_no_build()
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
            patch.object(engine_mod, "_MAX_MICRO_SPLIT_PASSES", 1),
            pytest.raises(ResolutionError, match="did not converge"),
        ):
            resolve_with_coordinator(
                self._fixpoint_coordinator(),
                targets,
                _reqs("foo"),
                inputs=_no_build(),
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
            inputs=_no_build(),
        )

        assert result.success
        versions = {
            tr.target.label: str(tr.pins["bar"]) for tr in result.target_results
        }
        assert versions == {
            "py310-linux_x86_64-pf3100": "1.0",
            "py310-linux_x86_64-pf3104": "2.0",
        }

        pylock = build_pylock(build_lock_input(result, inputs=_no_build()))
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
            coordinator, targets, _reqs("foo"), inputs=_no_build()
        )

        assert result.success
        assert self._pins_by_label(result) == {
            "py310-linux_x86_64-pf3100": {"foo", "mid"},
            "py310-linux_x86_64-pf3104": {"foo"},
        }
        rows = build_lock_input(result, inputs=_no_build()).environments
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


class TestMicroSliceAlignmentDirection:
    """Splitting a minor does not turn cross-target alignment around.

    Within a fork's pass alignment threads forward: a target's slices start
    from what the targets before it settled on, and a target that resolves
    later leaves them alone.
    """

    @staticmethod
    def _meta(name: str, version: str, *requires: str) -> str:
        head = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        return head + "".join(f"Requires-Dist: {req}\n" for req in requires)

    @classmethod
    def _coordinator(cls, *split_markers: str) -> FakeFetchPort:
        """A graph whose ``foo`` splits every minor ``split_markers`` cuts.

        ``bar`` is the package under test: nothing in the split concerns it,
        and both its versions resolve everywhere, so whichever one a slice
        pins came from that slice's preferences.
        """
        requires = {"foo": [f"mid ; {marker}" for marker in split_markers]}
        listings = {
            "foo": [_make_wheel("1.0", package="foo")],
            "mid": [_make_wheel("1.0", package="mid")],
            "bar": [
                _make_wheel("1.0", package="bar"),
                _make_wheel("2.0", package="bar"),
            ],
        }
        metadata: dict[str, str | None] = {}
        for name, wheels in listings.items():
            for wheel in wheels:
                assert wheel.metadata_url is not None
                metadata[wheel.metadata_url] = cls._meta(
                    name, wheel.version, *requires.get(name, ())
                )
        return make_coordinator(listings=listings, metadata_by_url=metadata)

    @staticmethod
    def _bar_versions(result: ResolveResult) -> dict[str, str]:
        return {tr.target.label: str(tr.pins["bar"]) for tr in result.target_results}

    def test_a_later_target_does_not_seed_the_slices_before_it(self) -> None:
        """Windows resolves after linux and caps ``bar`` at 1.0, so the linux
        slices keep 2.0."""
        targets = Matrix(
            python="==3.11",
            platforms=(PlatformSpec("linux_x86_64"), PlatformSpec("windows_amd64")),
        ).expand()

        result = resolve_with_coordinator(
            self._coordinator('python_full_version >= "3.11.4"'),
            targets,
            _reqs("foo", "bar", 'bar<2.0 ; sys_platform == "win32"'),
            inputs=_no_build(),
        )

        assert result.success
        assert self._bar_versions(result) == {
            "py311-linux_x86_64-pf3110": "2.0",
            "py311-linux_x86_64-pf3114": "2.0",
            "py311-windows_amd64-pf3110": "1.0",
            "py311-windows_amd64-pf3114": "1.0",
        }

    def test_a_desc_matrix_keeps_the_newest_python_pin_on_its_slices(self) -> None:
        """``python_order = "desc"`` resolves 3.12 first, so 3.11's narrower
        answer must not propagate back onto the 3.12 slices."""
        targets = Matrix(
            python=">=3.11,<3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
            python_order="desc",
        ).expand()

        result = resolve_with_coordinator(
            self._coordinator(
                'python_full_version >= "3.11.4"', 'python_full_version >= "3.12.3"'
            ),
            targets,
            _reqs("foo", "bar", 'bar<2.0 ; python_version == "3.11"'),
            inputs=_no_build(),
        )

        assert result.success
        assert self._bar_versions(result) == {
            "py312-linux_x86_64-pf3120": "2.0",
            "py312-linux_x86_64-pf3123": "2.0",
            "py311-linux_x86_64-pf3110": "1.0",
            "py311-linux_x86_64-pf3114": "1.0",
        }

    def test_an_earlier_unsplit_target_still_seeds_a_later_split_one(self) -> None:
        """3.10 does not split and caps ``bar`` at 1.0.  It resolves first, so
        the 3.11 slices still align onto it rather than jumping to 2.0."""
        targets = Matrix(
            python=">=3.10,<3.12", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()

        result = resolve_with_coordinator(
            self._coordinator('python_full_version >= "3.11.4"'),
            targets,
            _reqs("foo", "bar", 'bar<2.0 ; python_version == "3.10"'),
            inputs=_no_build(),
        )

        assert result.success
        assert self._bar_versions(result) == {
            "py310-linux_x86_64": "1.0",
            "py311-linux_x86_64-pf3110": "1.0",
            "py311-linux_x86_64-pf3114": "1.0",
        }

    def test_slices_align_within_their_own_fork(self) -> None:
        """Only group ``b`` caps ``bar``, and only on 3.11.  Group ``a``'s
        slices walk the matrix under its own answers, so they keep 2.0
        throughout."""
        targets = Matrix(
            python=">=3.11,<3.13", platforms=(PlatformSpec("linux_x86_64"),)
        ).expand()
        forks = [
            ResolveFork((("group", "a"),), tuple(_reqs("foo", "bar"))),
            ResolveFork(
                (("group", "b"),),
                tuple(_reqs("foo", "bar", 'bar<2.0 ; python_version == "3.11"')),
            ),
        ]

        result = resolve_with_coordinator(
            self._coordinator(
                'python_full_version >= "3.11.4"', 'python_full_version >= "3.12.3"'
            ),
            targets,
            forks=forks,
            inputs=_no_build(),
        )

        assert result.success
        assert self._bar_versions(result) == {
            "py311-linux_x86_64-pf3110-group-a": "2.0",
            "py311-linux_x86_64-pf3114-group-a": "2.0",
            "py312-linux_x86_64-pf3120-group-a": "2.0",
            "py312-linux_x86_64-pf3123-group-a": "2.0",
            "py311-linux_x86_64-pf3110-group-b": "1.0",
            "py311-linux_x86_64-pf3114-group-b": "1.0",
            "py312-linux_x86_64-pf3120-group-b": "1.0",
            "py312-linux_x86_64-pf3123-group-b": "1.0",
        }
