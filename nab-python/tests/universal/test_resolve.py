"""Tests for :mod:`resolve` orchestration logic.

The hot path is exercised via the runtime scenarios in
``run_scenarios.py``.  Unit tests here cover the helper functions and
the in-process orchestration branches that the runtime tests do not
exercise.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from nab_index.client import WheelFile
from nab_python._testing.coordinator_fake import make_coordinator
from nab_python._vendor.packaging.ranges import VersionRange
from nab_python._vendor.packaging.version import Version
from nab_python.config import (
    ConfigError,
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
    conflict_forks,
)
from nab_python.lockfile import DisjointnessError, IndexPin, LockInput, build_pylock
from nab_python.provider import (
    BuildPolicy,
    DistPolicy,
    LocalSource,
    MissingExtraError,
    UnsupportedSdistError,
    UnsupportedVcsError,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from nab_python.universal import resolve as resolve_mod
from nab_python.universal.matrix import Matrix, MatrixTuple
from nab_python.universal.resolve import (
    ResolveFork,
    TupleResult,
    UniversalResult,
    _direct_package_names,
    _parse_requirements,
    _resolve_one_tuple,
    _root_extras,
    _run_pass,
    _warn_extra_marker_at_root,
    merge_universal_lock_inputs,
    resolve_with_coordinator,
)
from nab_python.universal.wheel_selection import PlatformSpec
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


def _linux_311() -> MatrixTuple:
    return MatrixTuple(
        python_version="3.11",
        platform_id="linux_x86_64",
        environment={
            "python_version": "3.11",
            "python_full_version": "3.11.0",
            "implementation_name": "cpython",
            "implementation_version": "3.11.0",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_system": "Linux",
            "platform_version": "",
            "sys_platform": "linux",
        },
    )


def _windows_311() -> MatrixTuple:
    return MatrixTuple(
        python_version="3.11",
        platform_id="windows_amd64",
        environment={
            "python_version": "3.11",
            "python_full_version": "3.11.0",
            "implementation_name": "cpython",
            "implementation_version": "3.11.0",
            "os_name": "nt",
            "platform_machine": "AMD64",
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_system": "Windows",
            "platform_version": "",
            "sys_platform": "win32",
        },
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

    def _one_tuple_matrix(self) -> Matrix:
        return Matrix(python="==3.11", platforms=("linux_x86_64",))

    def _black_forks(self) -> list:
        return [
            ResolveFork((("group", "black22"),), ["black==22.1"]),
            ResolveFork((("group", "black23"),), ["black==23.12"]),
        ]

    def test_forks_produce_separate_per_label_pins(self) -> None:
        result = resolve_with_coordinator(
            self._black_coordinator(),
            self._one_tuple_matrix(),
            [],
            forks=self._black_forks(),
            build_policy=BuildPolicy.NEVER,
        )
        assert result.success
        by_label = {tr.tuple_.label: tr.pins for tr in result.tuple_results}
        assert by_label == {
            "py311-linux_x86_64-group-black22": {"black": Version("22.1")},
            "py311-linux_x86_64-group-black23": {"black": Version("23.12")},
        }

    def test_declared_conflict_lock_validates_and_marks_forks(self) -> None:
        result = resolve_with_coordinator(
            self._black_coordinator(),
            self._one_tuple_matrix(),
            [],
            forks=self._black_forks(),
            build_policy=BuildPolicy.NEVER,
        )
        lock_input = merge_universal_lock_inputs(
            result,
            dependency_groups=("black22", "black23"),
            conflicts=(_group_set("black22", "black23"),),
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
            self._one_tuple_matrix(),
            [],
            forks=self._black_forks(),
            build_policy=BuildPolicy.NEVER,
        )
        lock_input = merge_universal_lock_inputs(
            result,
            dependency_groups=("black22", "black23"),
        )
        with pytest.raises(DisjointnessError, match="black"):
            build_pylock(lock_input)

    def test_top_level_environments_drop_membership_and_dedupe(self) -> None:
        # Two forks of the one (python, platform) tuple must collapse to
        # a single top-level environment with no membership clause: that
        # field declares the platform universe, not the group selection.
        result = resolve_with_coordinator(
            self._black_coordinator(),
            self._one_tuple_matrix(),
            forks=self._black_forks(),
            build_policy=BuildPolicy.NEVER,
        )
        lock_input = merge_universal_lock_inputs(
            result,
            dependency_groups=("black22", "black23"),
            conflicts=(_group_set("black22", "black23"),),
        )
        assert len(lock_input.environments) == 1
        env_str = str(lock_input.environments[0])
        assert "in dependency_groups" not in env_str
        assert "in extras" not in env_str

    def _per_fork_preferences(
        self, *, align_across_tuples: bool
    ) -> list[dict[str, Version]]:
        """Run the two black forks, recording each fork's preferences.

        Wraps ``_run_pass`` to snapshot the ``preferences`` dict handed
        to each fork.  Cross-fork accumulation lives in
        ``resolve_with_coordinator``, so the second fork's snapshot
        reveals whether the first fork's pins were threaded forward.
        """
        seen: list[dict[str, Version]] = []
        real_run_pass = resolve_mod._run_pass

        def spy(*args: object, **kwargs: object) -> object:
            seen.append(dict(kwargs["preferences"]))  # type: ignore[arg-type]
            return real_run_pass(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(resolve_mod, "_run_pass", spy):
            result = resolve_with_coordinator(
                self._black_coordinator(),
                self._one_tuple_matrix(),
                [],
                forks=self._black_forks(),
                build_policy=BuildPolicy.NEVER,
                align_across_tuples=align_across_tuples,
            )
        assert result.success
        return seen

    def test_align_across_tuples_false_does_not_thread_pins(self) -> None:
        # With alignment off, the second fork's preferences must not
        # carry the first fork's black pin: each fork resolves alone.
        seen = self._per_fork_preferences(align_across_tuples=False)
        assert len(seen) == 2
        assert "black" not in seen[0]
        assert "black" not in seen[1]

    def test_align_across_tuples_true_threads_pins(self) -> None:
        # The companion case: with alignment on, the first fork's black
        # pin is accumulated into the second fork's preferences, so the
        # assertion above genuinely distinguishes the two modes.
        seen = self._per_fork_preferences(align_across_tuples=True)
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

    def _matrix(self) -> Matrix:
        return Matrix(python="==3.11", platforms=("linux_x86_64",))

    def _forks(self) -> list[ResolveFork]:
        # cpu and gpu both pull in accel; the base has only ``base``.
        return [
            ResolveFork((("extra", "cpu"),), ["base", "accel"]),
            ResolveFork((("extra", "gpu"),), ["base", "accel"]),
        ]

    def test_env_base_names_excludes_member_only_dep(self) -> None:
        result = resolve_with_coordinator(
            self._coordinator(),
            self._matrix(),
            forks=self._forks(),
            base_requirements=["base"],
            build_policy=BuildPolicy.NEVER,
        )
        assert result.success
        (names,) = result.env_base_names.values()
        assert "base" in names
        assert "accel" not in names

    def test_member_only_dep_keeps_membership_marker(self) -> None:
        result = resolve_with_coordinator(
            self._coordinator(),
            self._matrix(),
            forks=self._forks(),
            base_requirements=["base"],
            build_policy=BuildPolicy.NEVER,
        )
        lock_input = merge_universal_lock_inputs(
            result,
            extras=("cpu", "gpu"),
            conflicts=(_extra_set("cpu", "gpu"),),
        )
        pylock = build_pylock(lock_input)
        by_name = {str(p.name): p for p in pylock.packages}
        env = dict(result.tuple_results[0].tuple_.environment)
        neither = {**env, "extras": frozenset()}
        cpu = {**env, "extras": frozenset({"cpu"})}

        accel = by_name["accel"]
        assert accel.marker is not None
        assert not accel.marker.evaluate(neither)
        assert accel.marker.evaluate(cpu)

        # ``base`` is a true base dep, so it installs unconditionally.
        base = by_name["base"]
        assert base.marker is None or base.marker.evaluate(neither)

    def test_base_pass_failure_leaves_env_base_names_empty(self) -> None:
        # The base requirement cannot resolve (no such version), so its
        # environment contributes no base names and the lock falls back
        # to the present-in-all collapse for that environment.
        result = resolve_with_coordinator(
            self._coordinator(),
            self._matrix(),
            forks=self._forks(),
            base_requirements=["base==9.9"],
            build_policy=BuildPolicy.NEVER,
        )
        assert result.success
        assert result.env_base_names == {}


class TestDirectPackageNames:
    """``_direct_package_names`` extracts canonical names from req strings."""

    def test_simple_names_canonicalized(self) -> None:
        """Names are lowercased and underscores become hyphens."""
        out = _direct_package_names(["My_Pkg", "Other-Pkg"])
        assert out == {"my-pkg", "other-pkg"}

    def test_specifier_stripped(self) -> None:
        """The version specifier should not appear in the name set."""
        out = _direct_package_names(["pkg>=1.0,<2.0"])
        assert out == {"pkg"}

    def test_marker_does_not_affect_name(self) -> None:
        """A marker on the requirement does not change the canonical name."""
        out = _direct_package_names(['pywin32; sys_platform == "win32"'])
        assert out == {"pywin32"}


class TestWarnExtraMarkerAtRoot:
    """Hole 1.6 plug: warn on root requirements with ``extra`` markers."""

    def test_extra_eq_marker_is_flagged(self) -> None:
        """``pkg ; extra == "test"`` triggers the diagnostic."""
        flagged = _warn_extra_marker_at_root(['pkg ; extra == "test"'])
        assert flagged == ['pkg ; extra == "test"']

    def test_extra_eq_no_space_also_flagged(self) -> None:
        """``extra=="test"`` (no space) is flagged too."""
        flagged = _warn_extra_marker_at_root(['pkg ; extra=="test"'])
        assert flagged == ['pkg ; extra=="test"']

    def test_clean_requirement_not_flagged(self) -> None:
        """A normal requirement passes silently."""
        flagged = _warn_extra_marker_at_root(
            ["pkg>=1.0", 'pkg ; sys_platform == "win32"']
        )
        assert flagged == []

    def test_extras_syntax_not_flagged(self) -> None:
        """``pkg[extra]`` is the correct form and must NOT be flagged."""
        flagged = _warn_extra_marker_at_root(["pkg[redis]"])
        assert flagged == []

    def test_caplog_records_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """The warning is emitted via the module logger."""
        with caplog.at_level(logging.WARNING, logger="nab_python.universal.resolve"):
            _warn_extra_marker_at_root(['pkg ; extra == "test"'])
        assert any("extra" in rec.message.lower() for rec in caplog.records)


_FORTY_SHA = "0123456789abcdef0123456789abcdef01234567"


class TestParseRequirements:
    """``_parse_requirements`` builds the resolver-input dict per env."""

    def test_marker_true_keeps_requirement(self) -> None:
        """A requirement whose marker matches the env is kept."""
        env = _linux_311().environment
        out = _parse_requirements(['pkg; sys_platform == "linux"'], env)
        assert "pkg" in out

    def test_marker_false_drops_requirement(self) -> None:
        """A requirement whose marker excludes the env is dropped."""
        env = _linux_311().environment
        out = _parse_requirements(['pkg; sys_platform == "win32"'], env)
        assert out == {}

    def test_extras_get_separate_entries(self) -> None:
        """Extras become ``name[extra]`` entries with any-version range."""
        env = _linux_311().environment
        out = _parse_requirements(["pkg[foo,bar]"], env)
        assert "pkg" in out
        assert "pkg[foo]" in out
        assert "pkg[bar]" in out

    def test_no_specifier_yields_any(self) -> None:
        """An unconstrained requirement gets the any() range."""
        env = _linux_311().environment
        out = _parse_requirements(["pkg"], env)
        assert out["pkg"] == VersionRange.full()

    def test_specifier_yields_intervals(self) -> None:
        """A bounded specifier produces the corresponding interval."""
        env = _linux_311().environment
        out = _parse_requirements(["pkg>=1.0,<2.0"], env)
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
        env = _linux_311().environment
        out = _parse_requirements(["pkg[ext]===1.0.special"], env)
        assert "pkg" in out
        assert "1.0.special" in out["pkg"]
        assert Version("1.0") not in out["pkg"]
        assert "pkg[ext]" in out

    def test_duplicate_name_intersects(self) -> None:
        """Two requirements for one package combine to their overlap."""
        env = _linux_311().environment
        out = _parse_requirements(["pkg>=2.0", "pkg<3.0"], env)
        assert Version("2.5") in out["pkg"]
        assert Version("1.0") not in out["pkg"]
        assert Version("5.0") not in out["pkg"]

    def test_conflicting_names_raise(self) -> None:
        """Contradictory pins for one package raise ResolutionError."""
        env = _linux_311().environment
        with pytest.raises(ResolutionError, match="pkg==1.0"):
            _parse_requirements(["pkg==1.0", "pkg==2.0"], env)

    def test_constraint_extras_rejected(self) -> None:
        """A constraint carrying extras is rejected, matching pip."""
        env = _linux_311().environment
        with pytest.raises(ConfigError, match="extras"):
            _parse_requirements(["pkg[dev]<2.0"], env, kind="constraint")

    def test_extra_proxy_key_normalized(self) -> None:
        """The proxy key is PEP 685 normalized, matching the single-env path."""
        env = _linux_311().environment
        out = _parse_requirements(["pkg[My_Extra]"], env)
        assert "pkg[my-extra]" in out

    def test_plain_url_requirement_refused(self) -> None:
        """A plain archive URL is refused as an unsupported scheme."""
        env = _linux_311().environment
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            _parse_requirements(["pkg @ https://example.com/pkg.whl"], env)

    def test_vcs_url_refused_by_default_policy(self) -> None:
        """A git+https requirement is refused under the default BLOCK policy."""
        env = _linux_311().environment
        with pytest.raises(UnsupportedVcsError, match="VcsPolicy is BLOCK"):
            _parse_requirements(
                [f"pkg @ git+https://example.com/pkg.git@{_FORTY_SHA}"], env
            )

    def test_url_constraint_refused(self) -> None:
        """A direct-URL constraint is refused the same way as a requirement."""
        env = _linux_311().environment
        with pytest.raises(UnsupportedVcsError, match="VcsPolicy is BLOCK"):
            _parse_requirements(
                [f"pkg @ git+https://example.com/pkg.git@{_FORTY_SHA}"],
                env,
                kind="constraint",
            )

    def test_admitted_vcs_url_raises_not_implemented(self) -> None:
        """An admitted VCS requirement still has no universal resolve path."""
        env = _linux_311().environment
        vcs_config = VcsConfig(
            policy=VcsPolicy.ALLOW, allowed_schemes=frozenset({"git+https"})
        )
        with pytest.raises(NotImplementedError, match="not implemented"):
            _parse_requirements(
                [f"pkg @ git+https://example.com/pkg.git@{_FORTY_SHA}"],
                env,
                vcs_config=vcs_config,
            )


class TestRootExtras:
    """``_root_extras`` recovers requested extras from the proxy keys."""

    def test_recovers_and_normalizes_extras(self) -> None:
        env = _linux_311().environment
        out = _parse_requirements(["pkg[My_Extra]", "other"], env)
        assert _root_extras(out) == {("pkg", "my-extra")}

    def test_no_extras_yields_empty(self) -> None:
        assert _root_extras({"pkg": VersionRange.full()}) == set()


class TestResolveOneTuple:
    """``_resolve_one_tuple`` runs one resolve and reports stats."""

    def test_success_returns_pins(self) -> None:
        """A trivial resolve produces a TupleResult with pins set."""
        coordinator = _make_coordinator({"pkg": [_make_wheel("1.0", package="pkg")]})
        tr = _resolve_one_tuple(
            coordinator,
            _linux_311(),
            requirements={"pkg": VersionRange.full()},
            constraints=None,
            uploaded_prior_to=None,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
        )
        assert tr.success
        assert tr.pins == {"pkg": Version("1.0")}
        assert tr.tuple_.python_version == "3.11"
        assert tr.tuple_.platform_id == "linux_x86_64"

    def test_failure_returns_error(self) -> None:
        """A resolve with no candidate version reports failure."""
        coordinator = _make_coordinator({"pkg": []})
        tr = _resolve_one_tuple(
            coordinator,
            _linux_311(),
            requirements={"pkg": VersionRange.full()},
            constraints=None,
            uploaded_prior_to=None,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
        )
        assert not tr.success
        assert tr.error is not None
        assert "ResolutionError" in tr.error

    def test_missing_hash_reports_failed_tuple(self) -> None:
        """An unhashed wheel resolves but the tuple fails with MissingHashError."""
        unhashed = WheelFile(
            filename="pkg-1.0-py3-none-any.whl",
            url="https://example.com/pkg-1.0.whl",
            version="1.0",
            requires_python=None,
            has_metadata=True,
            upload_time=None,
        )
        coordinator = _make_coordinator({"pkg": [unhashed]})
        tr = _resolve_one_tuple(
            coordinator,
            _linux_311(),
            requirements={"pkg": VersionRange.full()},
            constraints=None,
            uploaded_prior_to=None,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
        )
        assert not tr.success
        assert tr.error is not None
        assert "MissingHashError" in tr.error
        assert tr.pins == {"pkg": Version("1.0")}

    def test_sdist_install_without_sdist_reports_failed_tuple(self) -> None:
        """A wheel-only version under sdist-install fails the tuple."""
        coordinator = _make_coordinator({"pkg": [_make_wheel("1.0", package="pkg")]})
        tr = _resolve_one_tuple(
            coordinator,
            _linux_311(),
            requirements={"pkg": VersionRange.full()},
            constraints=None,
            uploaded_prior_to=None,
            dist_policy=DistPolicy.SDIST_INSTALL,
            build_policy=BuildPolicy.NEVER,
        )
        assert not tr.success
        assert tr.error is not None
        assert "MissingSdistError" in tr.error
        assert tr.pins == {"pkg": Version("1.0")}


class TestVcsConfigPlumbing:
    """``vcs_config`` and ``vcs_cache_dir`` flow through to the provider."""

    def test_block_policy_rejects_vcs_source_via_one_tuple(self) -> None:
        """``_resolve_one_tuple`` surfaces the BLOCK refusal as a ValueError.

        If ``vcs_config`` were dropped on the way through the universal
        layer the default :class:`VcsConfig` is also BLOCK, so we
        additionally check the error string matches the policy
        diagnostic (not a NoneType crash or a generic ``ValueError``).
        """
        coordinator = _make_coordinator({})
        source = VcsSource(
            name="pkg",
            url=f"git+https://example.com/pkg.git@{_FORTY_SHA}",
        )
        with pytest.raises(ValueError, match="vcs_sources require VcsPolicy.ALLOW"):
            _resolve_one_tuple(
                coordinator,
                _linux_311(),
                requirements={"pkg": VersionRange.full()},
                constraints=None,
                uploaded_prior_to=None,
                dist_policy=DistPolicy.WHEEL_OR_SDIST,
                build_policy=BuildPolicy.NEVER,
                vcs_config=VcsConfig(policy=VcsPolicy.BLOCK),
                vcs_sources=[source],
            )

    def test_allow_policy_admits_vcs_source_via_one_tuple(self, tmp_path: Path) -> None:
        """An ALLOW-configured tuple registers the VCS source without crashing.

        The resolver requests no package so the materialise path is
        not exercised.  Reaching a non-error :class:`TupleResult` is
        sufficient evidence that ``vcs_config`` and ``vcs_cache_dir``
        both reached the provider (the BLOCK fast-fail in
        ``index_vcs_sources`` would have raised at construction time).
        """
        coordinator = _make_coordinator(
            {"other": [_make_wheel("1.0", package="other")]}
        )
        source = VcsSource(
            name="pkg",
            url=f"git+https://example.com/pkg.git@{_FORTY_SHA}",
        )
        cache = tmp_path / "vcs"
        cache.mkdir()
        tr = _resolve_one_tuple(
            coordinator,
            _linux_311(),
            requirements={"other": VersionRange.full()},
            constraints=None,
            uploaded_prior_to=None,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
            ),
            vcs_sources=[source],
            vcs_cache_dir=cache,
        )
        assert tr.success
        assert tr.pins == {"other": Version("1.0")}

    def test_vcs_cache_dir_required_for_materialize(self) -> None:
        """Omitting ``vcs_cache_dir`` makes vcs materialisation raise cleanly.

        When the resolver requests the VCS-backed package the
        provider raises ``UnsupportedSdistError`` mentioning
        ``vcs_cache_dir``.  Catching that diagnostic confirms the
        kwarg flows through to the provider attribute the materialise
        path reads.  Without the plumbing the resolver would attribute-
        error on ``provider.vcs_cache_dir`` instead.
        """
        coordinator = _make_coordinator({})
        source = VcsSource(
            name="pkg",
            url=f"git+https://example.com/pkg.git@{_FORTY_SHA}",
        )
        with pytest.raises(UnsupportedSdistError, match="vcs_cache_dir"):
            _resolve_one_tuple(
                coordinator,
                _linux_311(),
                requirements={"pkg": VersionRange.full()},
                constraints=None,
                uploaded_prior_to=None,
                dist_policy=DistPolicy.WHEEL_OR_SDIST,
                build_policy=BuildPolicy.NEVER,
                vcs_config=VcsConfig(
                    policy=VcsPolicy.ALLOW,
                    allowed_schemes=frozenset({"git+https"}),
                ),
                vcs_sources=[source],
                vcs_cache_dir=None,
            )

    def test_resolve_with_coordinator_threads_vcs_config(self) -> None:
        """``resolve_with_coordinator`` forwards ``vcs_config`` end-to-end.

        Failing at the indexing step is the visible signal that the
        kwarg reached :class:`UniversalProvider`.
        """
        coordinator = _make_coordinator({})
        source = VcsSource(
            name="pkg",
            url=f"git+https://example.com/pkg.git@{_FORTY_SHA}",
        )
        matrix = Matrix(python="==3.11", platforms=("linux_x86_64",))
        with pytest.raises(ValueError, match="vcs_sources require VcsPolicy.ALLOW"):
            resolve_with_coordinator(
                coordinator,
                matrix,
                ["pkg"],
                vcs_config=VcsConfig(policy=VcsPolicy.BLOCK),
                vcs_sources=[source],
                build_policy=BuildPolicy.NEVER,
            )


class TestRunPassSerial:
    """Serial mode through ``_run_pass`` covers alignment chain."""

    def test_serial_align_propagates_pins(self) -> None:
        """Each tuple's pins update the accumulated preferences."""
        wheels = [_make_wheel("1.0", package="pkg"), _make_wheel("2.0", package="pkg")]
        coordinator = _make_coordinator({"pkg": wheels})

        tuples = [_linux_311(), _windows_311()]
        results = _run_pass(
            tuples,
            requirements=["pkg"],
            constraints=None,
            coordinator=coordinator,
            uploaded_prior_to=None,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            resolution_strategy="highest",
            direct_packages=frozenset({"pkg"}),
            preferences={},
            align_serial=True,
        )
        assert len(results) == 2
        assert all(r.success for r in results)

    def test_serial_no_align_skips_pin_propagation(self) -> None:
        """``align_serial=False`` runs each tuple independently."""
        wheels = [_make_wheel("1.0", package="pkg"), _make_wheel("2.0", package="pkg")]
        coordinator = _make_coordinator({"pkg": wheels})

        tuples = [_linux_311(), _windows_311()]
        results = _run_pass(
            tuples,
            requirements=["pkg"],
            constraints=None,
            coordinator=coordinator,
            uploaded_prior_to=None,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            resolution_strategy="highest",
            direct_packages=frozenset({"pkg"}),
            preferences={},
            align_serial=False,
        )
        assert len(results) == 2
        assert all(r.success for r in results)


class TestRunPassConflict:
    """A contradictory root requirement fails each tuple cleanly."""

    def test_conflicting_requirements_fail_the_tuple(self) -> None:
        """Pinned-but-different reqs surface as a failed TupleResult.

        ``_parse_requirements`` raises before the resolver runs, so
        the failure has to be caught per tuple rather than escaping
        the whole universal pass.
        """
        coordinator = _make_coordinator({})
        results = _run_pass(
            [_linux_311()],
            requirements=["pkg==1.0", "pkg==2.0"],
            constraints=None,
            coordinator=coordinator,
            uploaded_prior_to=None,
            dist_policy=DistPolicy.WHEEL_OR_SDIST,
            build_policy=BuildPolicy.NEVER,
            resolution_strategy="highest",
            direct_packages=frozenset({"pkg"}),
            preferences={},
            align_serial=True,
        )
        assert len(results) == 1
        assert not results[0].success
        assert results[0].error is not None
        assert "ResolutionError" in results[0].error


class TestUniversalResult:
    """``UniversalResult`` aggregates and surfaces per-tuple info."""

    def test_success_property(self) -> None:
        """``success`` is True only when every tuple succeeded."""
        ok = TupleResult(tuple_=_linux_311(), success=True)
        bad = TupleResult(tuple_=_windows_311(), success=False)
        assert UniversalResult(
            matrix=Matrix(python="==3.11", platforms=("linux_x86_64",)),
            tuple_results=[ok, ok],
        ).success
        assert not UniversalResult(
            matrix=Matrix(python="==3.11", platforms=("linux_x86_64",)),
            tuple_results=[ok, bad],
        ).success

    def test_merged_lock_skips_failures(self) -> None:
        """Failed tuples contribute nothing to the merged lock."""
        ok = TupleResult(
            tuple_=_linux_311(),
            success=True,
            pins={"pkg": Version("1.0")},
        )
        bad = TupleResult(
            tuple_=_windows_311(),
            success=False,
            error="ResolutionError: ...",
        )
        result = UniversalResult(
            matrix=Matrix(
                python="==3.11",
                platforms=("linux_x86_64", "windows_amd64"),
            ),
            tuple_results=[ok, bad],
        )
        merged = result.merged_lock()
        assert "pkg" in merged
        labels = {label for _, label in merged["pkg"]}
        # Only the successful tuple's label appears.
        assert labels == {"py311-linux_x86_64"}


class TestMergeUniversalLockInputs:
    """``merge_universal_lock_inputs`` collapses per-tuple LockInputs."""

    def test_skips_tuples_without_lock_input(self) -> None:
        """A successful tuple whose lock_input is None is skipped.

        ``lock_input is None`` happens when the resolve succeeded but
        the artefact set lacks a sha256 somewhere; the tuple's pins
        are still on the result, but the merged lock cannot record
        them and so it is omitted entirely.
        """
        ok_with_lock = TupleResult(
            tuple_=_linux_311(),
            success=True,
            pins={"pkg": Version("1.0")},
            lock_input=LockInput(
                pins={
                    "pkg": IndexPin(name="pkg", version="1.0", index="pypi"),
                },
            ),
        )
        ok_without_lock = TupleResult(
            tuple_=_windows_311(),
            success=True,
            pins={"pkg": Version("1.0")},
            lock_input=None,
        )
        result = UniversalResult(
            matrix=Matrix(
                python="==3.11",
                platforms=("linux_x86_64", "windows_amd64"),
            ),
            tuple_results=[ok_with_lock, ok_without_lock],
        )
        merged = merge_universal_lock_inputs(result)
        # Only the linux tuple survives; windows had no lock_input.
        assert set(merged.per_tuple_pins) == {"py311-linux_x86_64"}
        assert set(merged.tuple_markers) == {"py311-linux_x86_64"}

    def test_distinct_platform_specs_do_not_clobber_pins(self) -> None:
        """Two specs sharing a platform_id keep separate per-tuple pins.

        Both tuples share python_version and platform_id, so before the
        label gained a spec discriminator they produced the same label
        and the second tuple's pins overwrote the first in
        ``per_tuple_pins``, silently dropping a resolved pin.
        """
        matrix = Matrix(
            python="==3.11",
            platforms=(
                PlatformSpec("linux_x86_64", manylinux_floor=(2, 17)),
                PlatformSpec("linux_x86_64", manylinux_floor=(2, 34)),
            ),
        )
        older, newer = matrix.expand()
        results = [
            TupleResult(
                tuple_=older,
                success=True,
                pins={"pkg": Version("1.0")},
                lock_input=LockInput(
                    pins={"pkg": IndexPin(name="pkg", version="1.0", index="pypi")},
                ),
            ),
            TupleResult(
                tuple_=newer,
                success=True,
                pins={"pkg": Version("2.0")},
                lock_input=LockInput(
                    pins={"pkg": IndexPin(name="pkg", version="2.0", index="pypi")},
                ),
            ),
        ]
        merged = merge_universal_lock_inputs(
            UniversalResult(matrix=matrix, tuple_results=results)
        )
        assert len(merged.per_tuple_pins) == 2
        assert len(merged.tuple_markers) == 2
        versions = {pins["pkg"].version for pins in merged.per_tuple_pins.values()}
        assert versions == {"1.0", "2.0"}


class TestResolveWithCoordinator:
    """End-to-end orchestration via the testable injected-coordinator entry."""

    def test_first_pass_returns_pins(self) -> None:
        """Single-tuple resolve produces a UniversalResult with pins."""
        coordinator = _make_coordinator({"pkg": [_make_wheel("1.0", package="pkg")]})
        matrix = Matrix(python="==3.11", platforms=("linux_x86_64",))
        result = resolve_with_coordinator(
            coordinator,
            matrix,
            ["pkg"],
        )
        assert result.success
        assert result.tuple_results[0].pins == {"pkg": Version("1.0")}

    def test_user_requested_missing_extra_raises(self) -> None:
        """A user-requested extra a package does not provide raises.

        Matches the single-environment path: with the default
        ``ExtrasMode.ERROR_USER``, a missing user extra is an error
        rather than a silent drop.
        """
        coordinator = _make_coordinator({"pkg": [_make_wheel("1.0", package="pkg")]})
        matrix = Matrix(python="==3.11", platforms=("linux_x86_64",))
        with pytest.raises(MissingExtraError):
            resolve_with_coordinator(coordinator, matrix, ["pkg[missing]"])

    def test_constraints_passed_through(self) -> None:
        """User-supplied constraints reach the resolver."""
        coordinator = _make_coordinator(
            {
                "pkg": [
                    _make_wheel("1.0", package="pkg"),
                    _make_wheel("2.0", package="pkg"),
                ]
            }
        )
        matrix = Matrix(python="==3.11", platforms=("linux_x86_64",))
        result = resolve_with_coordinator(
            coordinator,
            matrix,
            ["pkg"],
            constraints=["pkg<2.0"],
        )
        assert result.success
        assert result.tuple_results[0].pins == {"pkg": Version("1.0")}


class TestResolveUniversalWrapper:
    """``resolve_universal`` wraps ``resolve_with_coordinator`` with a transport."""

    def test_constructs_coordinator_and_delegates(self) -> None:
        """The wrapper constructs a FetchCoordinator and delegates."""
        # We don't exercise real networking; we verify the wrapper
        # contract by mocking ``FetchCoordinator`` and
        # ``HttpxAsyncTransport`` and asserting the inner function
        # was called with our coordinator.
        sentinel = MagicMock()
        sentinel.success = True
        sentinel.tuple_results = []
        with patch.object(resolve_mod, "FetchCoordinator") as fetch_cls:
            fetch_cls.return_value.__enter__.return_value = "COORD"
            fetch_cls.return_value.__exit__.return_value = False
            with (
                patch.object(
                    resolve_mod, "Urllib3AsyncTransport", return_value=MagicMock()
                ),
                patch.object(
                    resolve_mod,
                    "resolve_with_coordinator",
                    return_value=sentinel,
                ) as inner,
            ):
                result = resolve_mod.resolve_universal(
                    matrix=Matrix(python="==3.11", platforms=("linux_x86_64",)),
                    requirements=["pkg"],
                )
        assert result is sentinel
        assert inner.called
        # First positional arg should be the coordinator from FetchCoordinator.__enter__.
        assert inner.call_args.args[0] == "COORD"

    def test_explicit_indexes_passed_through(self) -> None:
        """Explicit ``indexes=`` keeps the user-supplied list."""
        from unittest.mock import MagicMock, patch

        from nab_index.multi_index import IndexConfig
        from nab_python.universal import resolve as resolve_mod

        custom = [IndexConfig("internal", "https://internal.example.com/simple/")]
        sentinel = MagicMock()
        sentinel.success = True
        sentinel.tuple_results = []
        with (
            patch.object(resolve_mod, "FetchCoordinator") as fetch_cls,
            patch.object(
                resolve_mod, "Urllib3AsyncTransport", return_value=MagicMock()
            ),
            patch.object(
                resolve_mod, "resolve_with_coordinator", return_value=sentinel
            ),
        ):
            fetch_cls.return_value.__enter__.return_value = "COORD"
            fetch_cls.return_value.__exit__.return_value = False
            resolve_mod.resolve_universal(
                matrix=Matrix(python="==3.11", platforms=("linux_x86_64",)),
                requirements=["pkg"],
                indexes=custom,
            )
        # FetchCoordinator received the same list (not the default).
        assert fetch_cls.call_args.kwargs["indexes"] is custom


class TestLocalVcsRequiresPython:
    """A local or VCS pin must satisfy each targeted Python version.

    Index candidates are filtered by Requires-Python while listing;
    local-path and VCS sources skip that filter, so the universal
    resolve checks them after resolving.
    """

    def _write(self, tmp_path: Path, body: str) -> LocalSource:
        (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
        return LocalSource("foo", str(tmp_path))

    def test_excluding_python_fails_the_tuple(self, tmp_path: Path) -> None:
        local = self._write(
            tmp_path,
            '[project]\nname = "foo"\nversion = "1.0"\nrequires-python = ">=3.12"\n',
        )
        coord = make_coordinator([], package="foo")
        result = resolve_with_coordinator(
            coord,
            Matrix(python="==3.10", platforms=("linux_x86_64",)),
            ["foo"],
            local_sources=[local],
        )
        assert not result.success
        error = result.tuple_results[0].error
        assert error is not None
        assert "foo 1.0 requires Python" in error
        assert "3.10" in error

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
        result = resolve_with_coordinator(
            coord,
            Matrix(python="==3.10", platforms=("linux_x86_64",)),
            ["foo"],
            local_sources=[local],
        )
        assert result.success
        pins = result.tuple_results[0].pins
        assert str(pins["foo"]) == "1.0"
        assert str(pins["bar"]) == "2.0"

    def test_no_requires_python_is_unconstrained(self, tmp_path: Path) -> None:
        local = self._write(tmp_path, '[project]\nname = "foo"\nversion = "1.0"\n')
        coord = make_coordinator([], package="foo")
        result = resolve_with_coordinator(
            coord,
            Matrix(python="==3.10", platforms=("linux_x86_64",)),
            ["foo"],
            local_sources=[local],
        )
        assert result.success
        assert str(result.tuple_results[0].pins["foo"]) == "1.0"
