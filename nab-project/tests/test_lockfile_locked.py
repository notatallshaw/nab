"""Tests for the consolidated ``--locked`` check and the lock-shaping helpers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from nab_project.lockfile import (
    BASE_MEMBER,
    IndexPin,
    InvalidLockfileError,
    LockfileSyntaxError,
    LockInput,
    RootRequirement,
    TargetLock,
    WheelArtifact,
    build_pylock,
    check_locked,
    drop_workspace_pins,
    read_lockfile_packages,
    render_lock,
    summarize_lock,
)
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

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
    package_gates: Mapping[str, tuple[tuple[str, str], ...]] | None = None,
    base_group: str | None = None,
) -> LockInput:
    return LockInput(
        targets={
            TARGET.label: TargetLock(
                target=TARGET,
                pins=dict(pins),
                dependencies=dict(dependencies or {}),
                base_dependencies=dict(base_dependencies or {}),
                package_gates=dict(package_gates or {}),
            )
        },
        base_group=base_group,
    )


def _write_lock(path: Path, pins: Mapping[str, IndexPin]) -> Path:
    path.write_text(render_lock(_lock_input(pins)), encoding="utf-8")
    return path


def _write_foreign_lock(path: Path, packages: str) -> Path:
    """Write a lock from raw TOML, as another tool would have produced it."""
    path.write_text(
        "lock-version = '1.0'\n"
        "created-by = 'other-tool'\n"
        "requires-python = '>=3.10'\n" + packages,
        encoding="utf-8",
    )
    return path


_AT_LIMIT = "9" * sys.get_int_max_str_digits()
_OVERSIZED = _AT_LIMIT + "9"


def _write_requires_python_lock(path: Path, requires_python: str) -> Path:
    path.write_text(
        "lock-version = '1.0'\n"
        "created-by = 'other-tool'\n"
        f"requires-python = '{requires_python}'\n"
        "packages = []\n",
        encoding="utf-8",
    )
    return path


class TestReadErrors:
    """A committed lock that cannot be parsed or used raises a typed error."""

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

    def test_oversized_requires_python_raises_invalid(self, tmp_path: Path) -> None:
        """A digit run past int()'s limit parses as a specifier but never converts."""
        target = _write_requires_python_lock(
            tmp_path / "pylock.toml", f">={_OVERSIZED}"
        )
        with pytest.raises(InvalidLockfileError, match="requires-python"):
            check_locked(
                target,
                requires_python=">=3.10",
                extras=(),
                dependency_groups=(),
                default_groups=(),
            )

    def test_at_limit_requires_python_is_compared(self, tmp_path: Path) -> None:
        target = _write_requires_python_lock(tmp_path / "pylock.toml", f">={_AT_LIMIT}")
        result = check_locked(
            target,
            requires_python=">=3.10",
            extras=(),
            dependency_groups=(),
            default_groups=(),
        )
        assert result is not None
        assert "requires-python" in result.reason


class TestArtifactUrlFilenames:
    """With ``name`` omitted, the file name comes from the percent-decoded URL."""

    def test_encoded_wheel_url_is_accepted(self, tmp_path: Path) -> None:
        target = _write_foreign_lock(
            tmp_path / "pylock.toml",
            "[[packages]]\n"
            "name = 'torch'\n"
            "version = '2.0.0+cu118'\n"
            "[[packages.wheels]]\n"
            "url = 'https://example.com/cu118/"
            "torch-2.0.0%2Bcu118-cp310-cp310-linux_x86_64.whl'\n"
            "[packages.wheels.hashes]\n"
            f"sha256 = '{'0' * 64}'\n",
        )

        assert (
            check_locked(
                target,
                requires_python=">=3.10",
                extras=(),
                dependency_groups=(),
                default_groups=(),
                roots=[RootRequirement(Requirement("torch"), "[project].dependencies")],
                resolve_target=TARGET,
            )
            is None
        )

    def test_encoded_sdist_url_is_accepted(self, tmp_path: Path) -> None:
        target = _write_foreign_lock(
            tmp_path / "pylock.toml",
            "[[packages]]\n"
            "name = 'spam'\n"
            "version = '1.0+cpu'\n"
            "[packages.sdist]\n"
            "url = 'https://example.com/files/spam-1.0%2Bcpu.tar.gz'\n"
            "[packages.sdist.hashes]\n"
            f"sha256 = '{'0' * 64}'\n",
        )

        assert (
            check_locked(
                target,
                requires_python=">=3.10",
                extras=(),
                dependency_groups=(),
                default_groups=(),
                roots=[RootRequirement(Requirement("spam"), "[project].dependencies")],
                resolve_target=TARGET,
            )
            is None
        )

    def test_decoded_wheel_version_is_still_checked(self, tmp_path: Path) -> None:
        target = _write_foreign_lock(
            tmp_path / "pylock.toml",
            "[[packages]]\n"
            "name = 'torch'\n"
            "version = '2.0.0+cu118'\n"
            "[[packages.wheels]]\n"
            "url = 'https://example.com/cu117/"
            "torch-2.0.0%2Bcu117-cp310-cp310-linux_x86_64.whl'\n"
            "[packages.wheels.hashes]\n"
            f"sha256 = '{'0' * 64}'\n",
        )

        with pytest.raises(InvalidLockfileError) as caught:
            check_locked(
                target,
                requires_python=None,
                extras=(),
                dependency_groups=(),
                default_groups=(),
            )

        assert "torch-2.0.0+cu117-cp310-cp310-linux_x86_64.whl" in str(caught.value)
        assert "not consistent" in str(caught.value)

    def test_literal_plus_in_the_url_path_is_left_alone(self, tmp_path: Path) -> None:
        """A ``+`` in a URL path is a plus, not a space."""
        target = _write_foreign_lock(
            tmp_path / "pylock.toml",
            "[[packages]]\n"
            "name = 'torch'\n"
            "version = '2.0.0+cu118'\n"
            "[[packages.wheels]]\n"
            "url = 'https://example.com/cu118/"
            "torch-2.0.0+cu118-cp310-cp310-linux_x86_64.whl'\n"
            "[packages.wheels.hashes]\n"
            f"sha256 = '{'0' * 64}'\n",
        )

        assert (
            check_locked(
                target,
                requires_python=">=3.10",
                extras=(),
                dependency_groups=(),
                default_groups=(),
                roots=[RootRequirement(Requirement("torch"), "[project].dependencies")],
                resolve_target=TARGET,
            )
            is None
        )

    def test_prior_pins_survive_an_encoded_url(self, tmp_path: Path) -> None:
        target = _write_foreign_lock(
            tmp_path / "pylock.toml",
            "[[packages]]\n"
            "name = 'torch'\n"
            "version = '2.0.0+cu118'\n"
            "[[packages.wheels]]\n"
            "url = 'https://example.com/cu118/"
            "torch-2.0.0%2Bcu118-cp310-cp310-linux_x86_64.whl'\n"
            "[packages.wheels.hashes]\n"
            f"sha256 = '{'0' * 64}'\n",
        )

        assert read_lockfile_packages(target) == {"torch": Version("2.0.0+cu118")}


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
                resolve_target=TARGET,
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
                resolve_target=TARGET,
            )
            is None
        )

    def test_resolve_target_none_runs_envelope_only(self, tmp_path: Path) -> None:
        target = _write_lock(tmp_path / "pylock.toml", {"foo": _pin("foo", "1.0")})
        assert (
            check_locked(
                target,
                requires_python=None,
                extras=(),
                dependency_groups=(),
                default_groups=(),
                roots=[RootRequirement(Requirement("bar"), "[project].dependencies")],
                resolve_target=None,
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
            resolve_target=TARGET,
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
                resolve_target=TARGET,
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
            resolve_target=TARGET,
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

    def test_a_surviving_package_keeps_its_whole_gate(self) -> None:
        """Dropping a member cannot narrow what still installs.

        A gate names install contexts, never packages, so nothing a
        dropped member reached can leave a gate behind that no longer
        resolves.
        """
        lock_input = _lock_input(
            {"foo": _pin("foo", "1.0"), "alpha": _pin("alpha", "2.0")},
            dependencies={"alpha": ("foo",)},
            package_gates={
                "foo": (BASE_MEMBER, ("group", "dev")),
                "alpha": (BASE_MEMBER,),
            },
            base_group="default",
        )

        dropped = drop_workspace_pins(lock_input, frozenset({"alpha"}))

        assert dropped.targets[TARGET.label].package_gates == {
            "foo": (BASE_MEMBER, ("group", "dev"))
        }
        marker = next(p.marker for p in build_pylock(dropped).packages)
        assert marker is not None
        assert str(marker) == (
            '"default" in dependency_groups or "dev" in dependency_groups'
        )


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

    def test_each_change_kind_reports_its_own_count(self) -> None:
        """Each kind gets a distinct count, so a swapped label changes the summary."""
        lock_input = _lock_input(
            {
                "added1": _pin("added1", "1.0"),
                "added2": _pin("added2", "1.0"),
                "added3": _pin("added3", "1.0"),
                "upgraded1": _pin("upgraded1", "2.0"),
                "upgraded2": _pin("upgraded2", "2.0"),
                "downgraded1": _pin("downgraded1", "1.0"),
                "unchanged": _pin("unchanged", "1.0"),
            }
        )
        prior = {
            "upgraded1": Version("1.0"),
            "upgraded2": Version("1.0"),
            "downgraded1": Version("2.0"),
            "unchanged": Version("1.0"),
            "removed1": Version("1.0"),
            "removed2": Version("1.0"),
            "removed3": Version("1.0"),
            "removed4": Version("1.0"),
        }

        assert summarize_lock(lock_input, prior) == (
            "7 packages: 3 added, 2 upgraded, 1 downgraded, 4 removed"
        )

    def test_downgrade_is_reported(self) -> None:
        lock_input = _lock_input({"foo": _pin("foo", "1.0")})
        assert summarize_lock(lock_input, {"foo": Version("2.0")}) == (
            "1 packages: 1 downgraded"
        )
