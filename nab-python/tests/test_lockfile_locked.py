"""Tests for the consolidated ``--locked`` check and the lock-shaping helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.version import Version
from nab_python.lockfile import (
    IndexPin,
    InvalidLockfileError,
    LockfileSyntaxError,
    LockInput,
    RootRequirement,
    TargetLock,
    WheelArtifact,
    check_locked,
    drop_workspace_pins,
    render_lock,
    summarize_lock,
)
from nab_python.tags import PlatformSpec
from nab_python.target import ResolveTarget

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

LINUX_ENV = {
    "python_version": "3.11",
    "sys_platform": "linux",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_system": "Linux",
    "implementation_name": "cpython",
}

TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)


def _pin(name: str, version: str) -> IndexPin:
    """An index pin carrying the one wheel PEP 751 needs it to record."""
    wheel = WheelArtifact(
        filename=f"{name}-{version}-py3-none-any.whl",
        url=f"https://example.com/{name}-{version}-py3-none-any.whl",
        hashes=(("sha256", "0" * 64),),
        size=4,
    )
    return IndexPin(
        name=name,
        version=version,
        index="https://example.com/simple",
        wheels=(wheel,),
    )


def _lock_input(
    pins: Mapping[str, IndexPin],
    *,
    dependencies: Mapping[str, tuple[str, ...]] | None = None,
    base_dependencies: Mapping[str, tuple[str, ...]] | None = None,
) -> LockInput:
    return LockInput(
        targets={
            TARGET.label: TargetLock(
                target=TARGET,
                pins=dict(pins),
                dependencies=dict(dependencies or {}),
                base_dependencies=dict(base_dependencies or {}),
            )
        }
    )


def _write_lock(path: Path, pins: Mapping[str, IndexPin]) -> Path:
    path.write_text(render_lock(_lock_input(pins)), encoding="utf-8")
    return path


class TestReadErrors:
    """A committed lock that cannot be parsed raises a typed error."""

    def test_not_toml_raises_syntax_error(self, tmp_path: Path) -> None:
        target = tmp_path / "pylock.toml"
        target.write_text("not = = toml", encoding="utf-8")
        with pytest.raises(LockfileSyntaxError):
            check_locked(
                target,
                requires_python=None,
                extras=(),
                dependency_groups=(),
                default_groups=(),
            )

    def test_toml_but_not_pep751_raises_invalid(self, tmp_path: Path) -> None:
        target = tmp_path / "pylock.toml"
        target.write_text("title = 'not a lock'", encoding="utf-8")
        with pytest.raises(InvalidLockfileError):
            check_locked(
                target,
                requires_python=None,
                extras=(),
                dependency_groups=(),
                default_groups=(),
            )

    def test_unreadable_raises_oserror(self, tmp_path: Path) -> None:
        target = tmp_path / "missing" / "pylock.toml"
        with pytest.raises(FileNotFoundError):
            check_locked(
                target,
                requires_python=None,
                extras=(),
                dependency_groups=(),
                default_groups=(),
            )


class TestCheckLocked:
    """The envelope runs first, then the direct requirements and constraints."""

    def test_matching_lock_falls_through(self, tmp_path: Path) -> None:
        target = _write_lock(tmp_path / "pylock.toml", {"foo": _pin("foo", "1.0")})
        assert (
            check_locked(
                target,
                requires_python=None,
                extras=(),
                dependency_groups=(),
                default_groups=(),
                roots=[RootRequirement(Requirement("foo"), "[project].dependencies")],
                marker_env=LINUX_ENV,
            )
            is None
        )

    def test_envelope_difference_fires_first(self, tmp_path: Path) -> None:
        target = _write_lock(tmp_path / "pylock.toml", {"foo": _pin("foo", "1.0")})
        result = check_locked(
            target,
            requires_python=">=3.12",
            extras=(),
            dependency_groups=(),
            default_groups=(),
        )
        assert result is not None
        assert "requires-python" in result.reason

    def test_roots_none_runs_envelope_only(self, tmp_path: Path) -> None:
        target = _write_lock(tmp_path / "pylock.toml", {"foo": _pin("foo", "1.0")})
        assert (
            check_locked(
                target,
                requires_python=None,
                extras=(),
                dependency_groups=(),
                default_groups=(),
                roots=None,
                marker_env=LINUX_ENV,
            )
            is None
        )

    def test_marker_env_none_runs_envelope_only(self, tmp_path: Path) -> None:
        target = _write_lock(tmp_path / "pylock.toml", {"foo": _pin("foo", "1.0")})
        assert (
            check_locked(
                target,
                requires_python=None,
                extras=(),
                dependency_groups=(),
                default_groups=(),
                roots=[RootRequirement(Requirement("bar"), "[project].dependencies")],
                marker_env=None,
            )
            is None
        )

    def test_unpinned_direct_requirement_fires(self, tmp_path: Path) -> None:
        target = _write_lock(tmp_path / "pylock.toml", {"foo": _pin("foo", "1.0")})
        result = check_locked(
            target,
            requires_python=None,
            extras=(),
            dependency_groups=(),
            default_groups=(),
            roots=[RootRequirement(Requirement("bar"), "[project].dependencies")],
            marker_env=LINUX_ENV,
        )
        assert result is not None
        assert "no bar pin" in result.reason

    def test_excluded_root_is_skipped(self, tmp_path: Path) -> None:
        target = _write_lock(tmp_path / "pylock.toml", {"foo": _pin("foo", "1.0")})
        assert (
            check_locked(
                target,
                requires_python=None,
                extras=(),
                dependency_groups=(),
                default_groups=(),
                roots=[RootRequirement(Requirement("Bar"), "[project].dependencies")],
                marker_env=LINUX_ENV,
                exclude=frozenset({"bar"}),
            )
            is None
        )

    def test_violated_constraint_fires(self, tmp_path: Path) -> None:
        target = _write_lock(tmp_path / "pylock.toml", {"foo": _pin("foo", "1.0")})
        result = check_locked(
            target,
            requires_python=None,
            extras=(),
            dependency_groups=(),
            default_groups=(),
            roots=[],
            constraints=["foo>=2"],
            marker_env=LINUX_ENV,
        )
        assert result is not None
        assert "constraint foo>=2" in result.reason


class TestDropWorkspacePins:
    """``--no-emit-workspace`` drops member pins and the edges naming them."""

    def test_empty_exclude_returns_input(self) -> None:
        lock_input = _lock_input({"foo": _pin("foo", "1.0")})
        assert drop_workspace_pins(lock_input, frozenset()) is lock_input

    def test_drop_filters_pins_and_dependency_graph(self) -> None:
        lock_input = _lock_input(
            {"foo": _pin("foo", "1.0"), "alpha": _pin("alpha", "2.0")},
            dependencies={"foo": ("alpha", "bar"), "alpha": ("bar",)},
        )
        dropped = drop_workspace_pins(lock_input, frozenset({"alpha"}))
        target = dropped.targets[TARGET.label]
        assert set(target.pins) == {"foo"}
        assert target.dependencies == {"foo": ("bar",)}

    def test_drop_carries_base_dependency_edges(self) -> None:
        edges = {"foo": ("alpha", "bar"), "alpha": ("bar",)}
        lock_input = _lock_input(
            {"foo": _pin("foo", "1.0"), "alpha": _pin("alpha", "2.0")},
            dependencies=edges,
            base_dependencies=edges,
        )

        dropped = drop_workspace_pins(lock_input, frozenset({"alpha"}))
        assert dropped.targets[TARGET.label].base_dependencies == edges


class TestSummarizeLock:
    """The written-lock summary diffs against the prior pins."""

    def test_multiple_targets_report_tuple_count(self) -> None:
        other = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_x86_64")
        )
        lock_input = LockInput(
            targets={
                TARGET.label: TargetLock(
                    target=TARGET, pins={"foo": _pin("foo", "1.0")}
                ),
                other.label: TargetLock(target=other, pins={"foo": _pin("foo", "1.0")}),
            }
        )
        assert summarize_lock(lock_input, None) == "2 tuples"

    def test_no_prior_reports_count_only(self) -> None:
        lock_input = _lock_input({"foo": _pin("foo", "1.0")})
        assert summarize_lock(lock_input, None) == "1 packages"

    def test_unchanged_pins_report_count_only(self) -> None:
        lock_input = _lock_input({"foo": _pin("foo", "1.0")})
        assert summarize_lock(lock_input, {"foo": Version("1.0")}) == "1 packages"

    def test_added_upgraded_and_removed(self) -> None:
        lock_input = _lock_input({"foo": _pin("foo", "2.0"), "baz": _pin("baz", "1.0")})
        prior = {"foo": Version("1.0"), "bar": Version("1.0")}
        assert summarize_lock(lock_input, prior) == (
            "2 packages: 1 added, 1 upgraded, 1 removed"
        )

    def test_downgrade_is_reported(self) -> None:
        lock_input = _lock_input({"foo": _pin("foo", "1.0")})
        assert summarize_lock(lock_input, {"foo": Version("2.0")}) == (
            "1 packages: 1 downgraded"
        )
