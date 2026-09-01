"""The partition of ``ResolveInputs`` behind the build-requires resolve.

A slot reaches the build environment only by being forwarded, pinned or
dropped, and the value it carries is checked against the resolve that
environment runs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

from nab_project._build.env import _inner_resolve_inputs, _without_build_permission
from nab_project.conflicts import ConflictKind, ConflictMember, ConflictSet
from nab_project.inputs import ResolveInputs
from nab_provider.overrides import IndexOverride
from nab_provider.policy import (
    ArchiveSource,
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    LocalSource,
    ResolutionStrategy,
    VcsSource,
)
from nab_provider.records import IndexConfig
from nab_provider.testing import pkg_override
from nab_provider.vcs_admission import VcsConfig, VcsPolicy

if TYPE_CHECKING:
    from pathlib import Path


# Every slot, grouped by what the inner build-requires config does with it:
# forward the outer value, pin a fixed one, or leave the default.
_INNER_FORWARDS = frozenset(
    {
        "decision_order",
        "index_overrides",
        "indexes",
        "package_overrides",
        "uploaded_prior_to",
    }
)
_INNER_PINNED: dict[str, object] = {
    "build_policy": BuildPolicy.NEVER,
    "dist_policy": DistPolicy.WHEEL_OR_SDIST,
}
_INNER_PINS = frozenset(_INNER_PINNED)
_INNER_DROPS = frozenset(
    {
        "archive_sources",
        "base_group",
        "build_group",
        "build_requires_depth",
        "conflicts",
        "constraints",
        "default_groups",
        "local_sources",
        "requires_python",
        "resolution",
        "trust_unverified_sdist_deps",
        "vcs",
        "vcs_sources",
    }
)


_CUTOFF = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _inputs_off_every_default(tmp_path: Path) -> ResolveInputs:
    """Settings with every slot set away from its default.

    A slot left at its default would make the checks below vacuous: a value
    that failed to cross would read back as the one it was meant to carry.
    """
    return ResolveInputs(
        archive_sources=(
            ArchiveSource("blob", "https://example.invalid/b-1.0.tar.gz"),
        ),
        base_group="project",
        build_group="build",
        build_policy=BuildPolicy.BUILD_REMOTE,
        build_requires_depth=1,
        conflicts=(ConflictSet(members=(ConflictMember(ConflictKind.EXTRA, "cpu"),)),),
        constraints=("setuptools<70",),
        decision_order=DecisionOrder.STABLE,
        default_groups=("dev",),
        dist_policy=DistPolicy.SDIST_ONLY,
        index_overrides={
            "internal": IndexOverride(
                uploaded_prior_to=_CUTOFF, build_policy=BuildPolicy.BUILD_REMOTE
            )
        },
        indexes=(IndexConfig("internal", "https://example.invalid/simple/"),),
        local_sources=(LocalSource("plugin", str(tmp_path / "plugin")),),
        package_overrides=(
            pkg_override(
                "hatchling",
                uploaded_prior_to=_CUTOFF,
                build_policy=BuildPolicy.BUILD_REMOTE,
            ),
        ),
        requires_python=">=3.11",
        resolution=ResolutionStrategy.LOWEST,
        trust_unverified_sdist_deps=True,
        uploaded_prior_to=_CUTOFF,
        vcs=VcsConfig(policy=VcsPolicy.ALLOW),
        vcs_sources=(VcsSource("tool", "git+https://example.invalid/tool.git@v1"),),
    )


class TestSeamPartition:
    """What the build environment may see of the settings around it."""

    def test_every_slot_is_forwarded_pinned_or_dropped(self) -> None:
        """A setting reaches the build env only by being named.

        A slot in none of the three sets takes its default inside the build env.
        """
        slots = set(ResolveInputs.__slots__)

        assert slots == _INNER_FORWARDS | _INNER_PINS | _INNER_DROPS

    def test_the_inner_resolve_carries_every_forwarded_setting(
        self, tmp_path: Path
    ) -> None:
        """A forwarded setting arrives, less any build permission it granted."""
        outer = _inputs_off_every_default(tmp_path)

        inner = _inner_resolve_inputs(outer)

        expected = {name: getattr(outer, name) for name in _INNER_FORWARDS}
        expected["package_overrides"] = tuple(
            _without_build_permission(override) for override in outer.package_overrides
        )
        expected["index_overrides"] = {
            name: _without_build_permission(override)
            for name, override in outer.index_overrides.items()
        }

        assert {name: getattr(inner, name) for name in _INNER_FORWARDS} == expected

    def test_the_inner_resolve_pins_the_policies_it_fixes(self, tmp_path: Path) -> None:
        """A pinned setting holds its fixed value whatever the outer one was."""
        outer = _inputs_off_every_default(tmp_path)

        inner = _inner_resolve_inputs(outer)

        assert {name: getattr(inner, name) for name in _INNER_PINS} == _INNER_PINNED

    def test_the_inner_resolve_leaks_no_dropped_setting(self, tmp_path: Path) -> None:
        """A dropped setting reads back at its default inside the build env."""
        outer = _inputs_off_every_default(tmp_path)
        bare = ResolveInputs()
        at_default = sorted(
            name
            for name in ResolveInputs.__slots__
            if getattr(outer, name) == getattr(bare, name)
        )
        assert not at_default

        inner = _inner_resolve_inputs(outer)

        assert {name: getattr(inner, name) for name in _INNER_DROPS} == {
            name: getattr(bare, name) for name in _INNER_DROPS
        }


class TestResolveInputs:
    """The value type itself."""

    def test_replace_changes_the_named_slots_and_keeps_the_rest(
        self, tmp_path: Path
    ) -> None:
        """``replace`` is ``dataclasses.replace`` for a slotted value type."""
        outer = _inputs_off_every_default(tmp_path)

        narrowed = outer.replace(
            build_policy=BuildPolicy.NEVER, constraints=("pip<26",)
        )

        assert narrowed.build_policy is BuildPolicy.NEVER
        assert narrowed.constraints == ("pip<26",)
        assert narrowed.indexes == outer.indexes
        assert outer.build_policy is BuildPolicy.BUILD_REMOTE

    def test_replace_refuses_a_name_the_type_does_not_declare(self) -> None:
        """A misspelled setting raises rather than being dropped."""
        with pytest.raises(TypeError, match="workspace"):
            ResolveInputs().replace(workspace=None)
