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
import tarfile
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from nab_index.client import WheelFile
from nab_python import resolve as resolve_mod
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
    MissingSdistError,
    TargetLock,
    build_pylock,
    write_requirements_with_hashes,
    write_requirements_without_hashes,
)
from nab_python.provider import (
    ArchiveSource,
    BuildPolicy,
    DistPolicy,
    LocalSource,
    MissingExtraError,
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
    from pathlib import Path


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
    cache_dir: Path | None = None,
) -> _EngineSettings:
    """The settings one bare ``_resolve_one_target`` or ``_run_pass`` needs."""
    effective = config if config is not None else NabProjectConfig()
    return _EngineSettings(
        coordinator=coordinator,
        config=effective,
        cache_dir=cache_dir,
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
        assert tr.metadata_fetched >= 0
        assert tr.distributions_seen >= 0

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

    def test_sdist_install_without_sdist_raises(self) -> None:
        """A wheel-only version under sdist-install fails the resolve.

        The pin is the one the lock cannot record, so the refusal comes
        from the lock builder and propagates out rather than landing on
        the target as a resolution failure.
        """
        coordinator = _make_coordinator({"pkg": [_make_wheel("1.0", package="pkg")]})
        settings = _settings(
            coordinator, _no_build(dist_policy=DistPolicy.SDIST_INSTALL)
        )
        with pytest.raises(MissingSdistError, match="pkg==1.0 has no sdist"):
            _resolve_one_target(_linux_311(), _reqs("pkg"), (), settings, {})


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
        sufficient evidence that the VCS config and the cache dir both
        reached the provider (the BLOCK fast-fail in
        ``index_vcs_sources`` would have raised at construction time).
        """
        coordinator = _make_coordinator(
            {"other": [_make_wheel("1.0", package="other")]}
        )
        settings = _settings(
            coordinator,
            _no_build(vcs=self._allow(), vcs_sources=(self._source(),)),
            cache_dir=tmp_path,
        )
        tr = _resolve_one_target(_linux_311(), _reqs("other"), (), settings, {})
        assert tr.success
        assert tr.pins == {"other": Version("1.0")}

    def test_cache_dir_required_for_vcs_materialize(self) -> None:
        """Without a cache dir, vcs materialisation raises cleanly.

        When the resolver requests the VCS-backed package the provider
        raises ``UnsupportedSdistError`` mentioning ``vcs_cache_dir``.
        Catching that diagnostic confirms the cache dir the engine
        derives reaches the provider attribute the materialise path
        reads.  Without the plumbing the resolver would attribute-error
        on ``provider.vcs_cache_dir`` instead.
        """
        settings = _settings(
            _make_coordinator({}),
            _no_build(vcs=self._allow(), vcs_sources=(self._source(),)),
            cache_dir=None,
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
                PlatformSpec("linux_x86_64", libc_version=(2, 17)),
                PlatformSpec("linux_x86_64", libc_version=(2, 34)),
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
