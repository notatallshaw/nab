"""Tests for :class:`nab_provider.target.ResolveTarget`.

The host constructors take their environment and tag sources as
arguments, so every test here names the interpreter it means instead of
the one running the suite.  One smoke test uses the live sources.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from nab_provider._vendor.packaging.markers import (
    InvalidMarker,
    Marker,
    default_environment,
)
from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.tags import Tag
from nab_provider._vendor.packaging.version import InvalidVersion, Version
from nab_provider.tags import PlatformSpec
from nab_provider.target import (
    IMPLEMENTATION_MARKERS,
    PEP508_MARKER_VARIABLES,
    PLATFORM_MARKERS,
    UNBOUNDABLE_MARKER_VARIABLES,
    Matrix,
    NonIntervalMarkerError,
    ResolveTarget,
    apply_python_axis_overlay,
    check_free_threaded,
    declared_environment,
    declared_range_marker,
    environment_declaration,
    host_environment,
    marker_variables,
    micro_boundary_points,
    python_axis_environment,
    slices_from_points,
    unboundable_variables,
)

_HOST_ENV: dict[str, str] = {
    "implementation_name": "cpython",
    "implementation_version": "3.13.2",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "6.8.0",
    "platform_system": "Linux",
    "platform_version": "#1 SMP",
    "python_full_version": "3.13.2",
    "python_version": "3.13",
    "sys_platform": "linux",
}

_HOST_TAGS: tuple[Tag, ...] = (
    Tag("cp313", "cp313", "manylinux_2_39_x86_64"),
    Tag("cp313", "cp313", "linux_x86_64"),
    Tag("cp313", "abi3", "manylinux_2_39_x86_64"),
    Tag("py3", "none", "any"),
)


def _host_env() -> dict[str, str]:
    return dict(_HOST_ENV)


def _host_tags() -> tuple[Tag, ...]:
    return _HOST_TAGS


class TestHostEnvironment:
    def test_drops_non_string_values(self) -> None:
        """A source yielding a non-string value contributes nothing for it."""
        env = host_environment(lambda: {"os_name": "posix", "extras": frozenset()})
        assert env == {"os_name": "posix"}

    def test_defaults_to_the_live_interpreter(self) -> None:
        """With no source, the host's own PEP 508 environment is returned."""
        env = host_environment()
        assert env["python_version"].count(".") == 1
        assert Version(env["python_full_version"])
        assert env["sys_platform"]


class TestForHost:
    def test_marker_env_is_the_untouched_host(self) -> None:
        target = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert target.marker_env == _HOST_ENV
        assert target.host_faithful

    def test_label_names_the_host(self) -> None:
        target = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert target.label == "host"
        assert target.platform_id == "host"

    def test_tags_are_the_hosts(self) -> None:
        target = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert target.tags.ordered == _HOST_TAGS

    def test_live_sources_smoke(self) -> None:
        """The default sources answer for the interpreter running the suite."""
        target = ResolveTarget.for_host()
        assert target.host_faithful
        assert Version(target.python_full_version)
        assert target.tags.ordered


class TestPrereleaseHost:
    """``Requires-Python`` names a language, so a prerelease host is one."""

    def test_a_release_candidate_host_admits_its_release(self) -> None:
        """A 3.15 candidate satisfies ``>=3.15``, as it does under pip.

        The PEP 508 full version keeps the ``rc``, and a specifier admits no
        prerelease unless it names one, so comparing that value would drop
        every distribution requiring the release the host is a candidate for.
        """
        target = ResolveTarget.for_host(
            env_source=lambda: {
                **_HOST_ENV,
                "python_full_version": "3.15.0rc1",
                "python_version": "3.15",
            },
            tags_source=_host_tags,
        )
        assert target.python_full_version == "3.15.0rc1"
        assert target.admits_requires_python(SpecifierSet(">=3.15"))
        assert target.admits_requires_python(SpecifierSet("==3.15"))


class TestTargetPythonIsComparable:
    """Every candidate's Requires-Python is tested against this target."""

    def test_an_unparseable_full_version_names_itself(self) -> None:
        """A local-build version fails here, not once per candidate."""
        with pytest.raises(ValueError, match="python_full_version '3.11.2\\+'"):
            ResolveTarget.for_host(
                env_source=lambda: {**_HOST_ENV, "python_full_version": "3.11.2+"},
                tags_source=_host_tags,
            )

    def test_an_unparseable_minor_names_itself(self) -> None:
        """Requires-Python is compared against the minor, so it is checked too."""
        with pytest.raises(ValueError, match="python_version '3.11\\+'"):
            ResolveTarget.for_host(
                env_source=lambda: {**_HOST_ENV, "python_version": "3.11+"},
                tags_source=_host_tags,
            )


class TestForHostPython:
    def test_python_axis_moves(self) -> None:
        target = ResolveTarget.for_host_python(
            "3.10.5", env_source=_host_env, tags_source=_host_tags
        )
        assert target.python_version == "3.10"
        assert target.python_full_version == "3.10.5"
        assert not target.host_faithful

    def test_machine_stays_the_host(self) -> None:
        """Only the python axis moves; the machine's markers carry over."""
        target = ResolveTarget.for_host_python(
            "3.10", env_source=_host_env, tags_source=_host_tags
        )
        assert target.marker_env["platform_machine"] == "x86_64"
        assert target.marker_env["platform_release"] == "6.8.0"

    def test_label_names_the_python_and_the_host(self) -> None:
        target = ResolveTarget.for_host_python(
            "3.10.5", env_source=_host_env, tags_source=_host_tags
        )
        assert target.label == "py310-host"

    def test_tags_keep_the_host_platforms(self) -> None:
        """The host's platform tags carry over, in the host's own order."""
        target = ResolveTarget.for_host_python(
            "3.10", env_source=_host_env, tags_source=_host_tags
        )
        tag_strs = {str(t) for t in target.tags.ordered}
        assert "cp310-cp310-manylinux_2_39_x86_64" in tag_strs
        assert "cp310-cp310-linux_x86_64" in tag_strs
        assert not any("cp313" in t for t in tag_strs)

    def test_pypy_host_keeps_its_interpreter(self) -> None:
        """A PyPy host yields PyPy tags for the target Python."""

        def pypy_tags() -> tuple[Tag, ...]:
            return (
                Tag("pp311", "pypy311_pp73", "manylinux_2_39_x86_64"),
                Tag("py3", "none", "any"),
            )

        target = ResolveTarget.for_host_python(
            "3.10", env_source=_host_env, tags_source=pypy_tags
        )
        tag_strs = {str(t) for t in target.tags.ordered}
        assert "pp310-pypy310_pp73-manylinux_2_39_x86_64" in tag_strs

    def test_free_threaded_host_keeps_its_abi(self) -> None:
        """A free-threaded host targets the ``cpXYt`` ABI at the new Python."""

        def free_threaded_tags() -> tuple[Tag, ...]:
            return (
                Tag("cp313", "cp313t", "manylinux_2_39_x86_64"),
                Tag("py3", "none", "any"),
            )

        target = ResolveTarget.for_host_python(
            "3.14", env_source=_host_env, tags_source=free_threaded_tags
        )
        tag_strs = {str(t) for t in target.tags.ordered}
        assert "cp314-cp314t-manylinux_2_39_x86_64" in tag_strs

    def test_invalid_python_raises(self) -> None:
        with pytest.raises(InvalidVersion, match="python_version 'not-a-version'"):
            ResolveTarget.for_host_python(
                "not-a-version", env_source=_host_env, tags_source=_host_tags
            )

    def test_zero_micro_pins_whole(self) -> None:
        """A ``3.13.0`` target names one micro, resolved whole."""
        target = ResolveTarget.for_host_python(
            "3.13.0", env_source=_host_env, tags_source=_host_tags
        )
        assert not target.is_minor_interval
        assert 'python_full_version == "3.13.0"' in declared_range_marker(target)

    def test_bare_minor_is_an_interval(self) -> None:
        """A bare ``3.13`` target is a micro interval off its ``.0`` floor."""
        target = ResolveTarget.for_host_python(
            "3.13", env_source=_host_env, tags_source=_host_tags
        )
        assert target.is_minor_interval
        assert target.admits_requires_python(SpecifierSet(">=3.13.5"))

    def test_live_sources_smoke(self) -> None:
        """The default sources are the running interpreter's."""
        target = ResolveTarget.for_host_python("3.10")
        assert target.python_full_version == "3.10.0"
        assert target.tags.ordered


class TestForDeclared:
    def test_environment_has_all_eleven_pep508_variables(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("macos_arm64")
        )
        assert set(target.marker_env) == {
            "implementation_name",
            "implementation_version",
            "os_name",
            "platform_machine",
            "platform_python_implementation",
            "platform_release",
            "platform_system",
            "platform_version",
            "python_full_version",
            "python_version",
            "sys_platform",
        }
        assert target.marker_env["sys_platform"] == "darwin"
        assert target.python_full_version == "3.12.0"

    def test_patch_release_overrides_the_default(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64"),
            python_full_version="3.11.4",
        )
        assert target.python_full_version == "3.11.4"
        assert target.marker_env["implementation_version"] == "3.11.4"

    def test_is_not_host_faithful(self) -> None:
        """A declared target impersonates a machine, so a build here lies."""
        target = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        )
        assert not target.host_faithful

    def test_platform_id_and_spec_are_kept(self) -> None:
        spec = PlatformSpec("linux_x86_64", libc="musl")
        target = ResolveTarget.for_declared(python_version="3.11", spec=spec)
        assert target.platform_spec == spec
        assert target.platform_id == "linux_x86_64"

    def test_tags_come_from_the_spec(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64", libc="musl")
        )
        tag_strs = {str(t) for t in target.tags.ordered}
        assert "cp311-cp311-musllinux_1_2_x86_64" in tag_strs
        assert not any("manylinux" in t for t in tag_strs)


def _a_bare_minor_target() -> ResolveTarget:
    """A declared 3.13 target, standing for every micro of the minor."""
    return ResolveTarget.for_declared(
        python_version="3.13", spec=PlatformSpec("linux_x86_64")
    )


def _a_patch_pinned_target() -> ResolveTarget:
    """A matrix ``python-patches`` target pinned to one micro of 3.13."""
    return ResolveTarget.for_declared(
        python_version="3.13",
        spec=PlatformSpec("linux_x86_64"),
        python_full_version="3.13.4",
    )


def _a_host_target() -> ResolveTarget:
    """The 3.13.2 host of ``_HOST_ENV``, reporting a real interpreter."""
    return ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)


_EVERY_TARGET_KIND = pytest.mark.parametrize(
    "make_target",
    [
        pytest.param(_a_bare_minor_target, id="bare-minor"),
        pytest.param(_a_patch_pinned_target, id="patch-pin"),
        pytest.param(_a_host_target, id="host"),
    ],
)

# Requires-Python declarations and whether they admit the 3.13 language.
_ADMITS_PYTHON_3_13 = [
    ("", True),
    ("==3.13", True),
    ("==3.13.*", True),
    ("==3.13.4.*", True),
    ("==3.13.4", True),
    ("==3.13.0", True),
    ("==3.*", True),
    ("==3", False),
    ("==3.14", False),
    ("!=3.13", False),
    ("!=3.13.*", False),
    ("!=3.13.0.*", True),
    ("!=3.*", False),
    ("!=3.13.7", True),
    ("!=3.13.0", True),
    ("!=3.13rc1", True),
    ("!=3.13.post1", True),
    ("!=3.13.dev1", True),
    ("!=3.13+local", True),
    (">=3.13", True),
    (">=3.13.1", True),
    (">=3.13.8", True),
    (">=3.10a1", True),
    (">=3.14", False),
    (">=1!3.13", False),
    (">3.13", True),
    (">3.13.9", True),
    ("<=3.13", True),
    ("<=3.13.2", True),
    ("<=3.13.0rc1", False),
    ("<3.13.5", True),
    ("<3.13", False),
    ("<3.14", True),
    ("<3.11", False),
    ("~=3.13", True),
    ("~=3.13.4", True),
    ("~=3.14.0", False),
    ("===3.13", True),
    ("===3.13.7", True),
    ("===not-a-version", False),
    (">=3.7,!=3.9.7", True),
]


class TestAdmitsRequiresPython:
    """Requires-Python names a language, so every target answers at its minor.

    A micro segment says which patch releases a distribution was built and
    tested against, not which Python it runs on, so it neither admits a minor
    nor excludes one.  The three kinds of target differ in how precisely they
    name their interpreter and agree on every declaration.
    """

    @_EVERY_TARGET_KIND
    @pytest.mark.parametrize(("spec", "admits"), _ADMITS_PYTHON_3_13)
    def test_the_language_level_verdict(
        self, make_target: Callable[[], ResolveTarget], spec: str, admits: bool
    ) -> None:
        assert make_target().admits_requires_python(SpecifierSet(spec)) is admits

    def test_a_declaration_that_names_only_micros_admits_everything(self) -> None:
        """Every clause dropping leaves an empty set, which excludes nothing."""
        target = _a_bare_minor_target()
        assert target.admits_requires_python(SpecifierSet("!=3.13.7,!=3.9.2"))

    @_EVERY_TARGET_KIND
    def test_an_uncomparable_version_raises(
        self, make_target: Callable[[], ResolveTarget]
    ) -> None:
        """A version too large to compare raises instead of getting a verdict."""
        oversized = SpecifierSet(">=3." + "9" * 5000)
        with pytest.raises(ValueError, match="Exceeds the limit"):
            make_target().admits_requires_python(oversized)

    def test_every_slice_of_a_split_minor_agrees(self) -> None:
        """A slice off a bare minor is an interval on every side of the split.

        A split moves the upper slice onto a real micro representative, but the
        row still stands for every interpreter above the boundary, so the whole
        minor is the admission granularity.
        """
        floor, upper = slices_from_points(_a_bare_minor_target(), [Version("3.13.4")])
        assert floor.is_minor_interval
        assert upper.is_minor_interval
        spec = SpecifierSet(">=3.13.6")
        assert floor.admits_requires_python(spec)
        assert upper.admits_requires_python(spec)


class TestLabels:
    def test_label_is_short_and_stable(self) -> None:
        """``label`` is a short id suitable for filenames and logs."""
        target = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        )
        assert target.label == "py311-linux_x86_64"

    def test_pypy_label_uses_pp_prefix(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64"),
            implementation="pypy",
        )
        assert target.label == "pp311-linux_x86_64"

    def test_spec_knobs_add_a_discriminator(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64", libc="musl"),
        )
        assert target.label == "py311-linux_x86_64-musl"

    def test_windows_arm64_label(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("windows_arm64")
        )
        assert target.label == "py312-windows_arm64"

    def test_linux_i686_label(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_i686")
        )
        assert target.label == "py312-linux_i686"

    def test_linux_armv7l_label(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_armv7l")
        )
        assert target.label == "py312-linux_armv7l"

    def test_free_threaded_windows_arm64_label(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.13",
            spec=PlatformSpec("windows_arm64", free_threaded=True),
        )
        assert target.label == "py313-windows_arm64-ft"
        assert "cp313t" in {t.abi for t in target.tags.ordered}

    def test_selection_appends_sorted_member_suffix(self) -> None:
        """A conflict-fork selection appends sorted ``kind-name`` members."""
        target = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        ).with_selection((("group", "isort5"), ("group", "black22")))
        assert target.label == "py311-linux_x86_64-group-black22.group-isort5"

    def test_mixed_extra_and_group_selection_label_format(self) -> None:
        """Mixed selections sort ``extra-`` before ``group-`` lexically.

        Pins the exact byte-stable label shape so renaming
        :data:`KIND_EXTRA` / :data:`KIND_GROUP` cannot silently flip the
        sort order and rewrite every label dict key.
        """
        target = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        ).with_selection((("group", "isort5"), ("extra", "cpu")))
        assert target.label == "py311-linux_x86_64-extra-cpu.group-isort5"

    def test_label_distinguishes_selections_that_split_on_hyphen(self) -> None:
        """Names containing ``-`` cannot collide two selections into one label.

        Canonical names collapse ``[-_.]`` runs to a single ``-``, so a
        ``-`` joiner is ambiguous: ``a-b`` plus ``c`` and ``a`` plus
        ``b-c`` would both read as ``a-b-c``.  The ``.`` separator keeps
        the two selections on distinct labels.
        """
        base = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        )
        first = base.with_selection((("extra", "a-b"), ("extra", "c")))
        second = base.with_selection((("extra", "a"), ("extra", "b-c")))
        assert first.label != second.label

    def test_label_distinguishes_extra_from_group_of_same_name(self) -> None:
        base = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        )
        as_extra = base.with_selection((("extra", "cpu"),))
        as_group = base.with_selection((("group", "cpu"),))
        assert as_extra.label != as_group.label

    def test_reselecting_replaces_the_previous_suffix(self) -> None:
        """A target already under a fork takes the new fork's label."""
        forked = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        ).with_selection((("extra", "cpu"),))
        assert forked.with_selection((("extra", "gpu"),)).label == (
            "py311-linux_x86_64-extra-gpu"
        )
        assert forked.with_selection(()).label == "py311-linux_x86_64"

    def test_selection_slug_names_the_fork(self) -> None:
        """The slug is the label suffix without its leading separator."""
        base = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        )
        assert base.selection_slug == ""
        forked = base.with_selection((("group", "isort5"), ("extra", "cpu")))
        assert forked.selection_slug == "extra-cpu.group-isort5"
        assert forked.label.endswith(f"-{forked.selection_slug}")


class TestMarkerStrings:
    @staticmethod
    def _linux_311() -> ResolveTarget:
        return ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        )

    def test_extra_selection_adds_extras_marker_clause(self) -> None:
        target = self._linux_311().with_selection((("extra", "cpu"),))
        assert target.marker_string.endswith('and "cpu" in extras')

    def test_group_selection_adds_dependency_groups_clause(self) -> None:
        target = self._linux_311().with_selection((("group", "black22"),))
        assert target.marker_string.endswith('and "black22" in dependency_groups')

    def test_selection_clauses_are_sorted(self) -> None:
        target = self._linux_311().with_selection(
            (("group", "isort5"), ("extra", "cpu"))
        )
        # sorted by (kind, name): ("extra", "cpu") < ("group", "isort5")
        assert target.marker_string.endswith(
            'and "cpu" in extras and "isort5" in dependency_groups'
        )

    def test_environment_marker_string_omits_selection(self) -> None:
        target = self._linux_311().with_selection((("group", "black22"),))
        assert "in dependency_groups" not in target.environment_marker_string
        assert target.environment_marker_string.endswith('platform_machine == "x86_64"')
        assert '"black22" in dependency_groups' in target.marker_string

    def test_empty_selection_leaves_marker_and_label_unchanged(self) -> None:
        target = self._linux_311()
        assert target.label == "py311-linux_x86_64"
        assert "in extras" not in target.marker_string
        assert "in dependency_groups" not in target.marker_string

    def test_cpython_marker_omits_implementation(self) -> None:
        assert "implementation_name" not in self._linux_311().marker_string

    def test_pypy_marker_constrains_implementation(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64"),
            implementation="pypy",
        )
        assert target.marker_string.endswith('and implementation_name == "pypy"')

    def test_multi_implementation_cpython_marker_pins_implementation(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64"),
            multi_implementation=True,
        )
        assert target.marker_string.endswith('and implementation_name == "cpython"')


class TestEnvWithMembership:
    def test_membership_sets_are_seeded_empty(self) -> None:
        """A marker testing ``extras`` evaluates False instead of raising."""
        target = ResolveTarget.for_declared(
            python_version="3.11", spec=PlatformSpec("linux_x86_64")
        )
        env = target.env_with_membership()
        assert env["extras"] == frozenset()
        assert env["dependency_groups"] == frozenset()
        assert env["sys_platform"] == "linux"


class TestWithMarkerOverrides:
    def test_empty_overrides_keep_the_target(self) -> None:
        target = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert target.with_marker_overrides({}) is target

    def test_override_replaces_the_host_value(self) -> None:
        target = ResolveTarget.for_host(
            env_source=_host_env, tags_source=_host_tags
        ).with_marker_overrides({"sys_platform": "win32"})
        assert target.marker_env["sys_platform"] == "win32"
        assert target.marker_env["os_name"] == "posix"
        assert not target.host_faithful

    def test_python_axis_override_syncs_the_full_version(self) -> None:
        target = ResolveTarget.for_host(
            env_source=_host_env, tags_source=_host_tags
        ).with_marker_overrides({"python_version": "3.8"})
        assert target.python_full_version == "3.8.0"

    def test_tags_do_not_move_with_the_overlay(self) -> None:
        """An overlay names no libc floor, so it cannot rebuild the tag axis."""
        base = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        overlaid = base.with_marker_overrides({"sys_platform": "win32"})
        assert overlaid.tags == base.tags

    def test_a_moved_machine_marker_disowns_the_tags(self) -> None:
        """The tags now describe a machine the markers no longer name."""
        base = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert base.tags_faithful
        assert not base.with_marker_overrides({"sys_platform": "win32"}).tags_faithful

    def test_a_moved_python_marker_disowns_the_tags(self) -> None:
        """The tag set encodes the interpreter, which the overlay cannot rebuild."""
        base = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert not base.with_marker_overrides({"python_version": "3.8"}).tags_faithful

    def test_an_overlay_that_moves_nothing_keeps_the_tags(self) -> None:
        """Restating a value the target already has changes no axis."""
        base = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        overlaid = base.with_marker_overrides({"platform_system": "Linux"})
        assert overlaid.tags_faithful

    def test_an_off_axis_overlay_keeps_the_tags(self) -> None:
        """No wheel tag encodes the kernel version, so moving it is harmless."""
        base = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        overlaid = base.with_marker_overrides({"platform_release": "9.9.9"})
        assert overlaid.tags_faithful

    def test_an_unfaithful_target_stays_unfaithful(self) -> None:
        """A second overlay cannot restore tags the first one disowned."""
        base = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        overlaid = base.with_marker_overrides({"sys_platform": "win32"})
        assert not overlaid.with_marker_overrides({"os_name": "nt"}).tags_faithful


class TestDeclaredEnvironment:
    def test_kernel_markers_evaluate_against_the_declared_release(self) -> None:
        env = declared_environment(
            "3.11",
            PlatformSpec("linux_x86_64", platform_release="5.10.0"),
            "cpython",
        )
        assert Marker('platform_release >= "5.10"').evaluate(env)


class TestMarkerTables:
    def test_each_implementation_sets_the_interpreter_markers(self) -> None:
        required = {"platform_python_implementation", "implementation_name"}
        for impl, env in IMPLEMENTATION_MARKERS.items():
            assert required == env.keys(), impl

    def test_each_platform_sets_the_os_and_arch_markers(self) -> None:
        required = {"sys_platform", "platform_system", "platform_machine", "os_name"}
        for platform_id, env in PLATFORM_MARKERS.items():
            missing = required - env.keys()
            assert not missing, f"{platform_id} missing {missing}"

    def test_linux_armv7l_marker_machine(self) -> None:
        env = declared_environment("3.12", PlatformSpec("linux_armv7l"), "cpython")
        assert env["platform_machine"] == "armv7l"
        assert env["sys_platform"] == "linux"


class TestNewPlatformIdsExpand:
    """The added ids expand from a matrix like any other platform."""

    def test_windows_arm64_and_linux_i686_expand_to_targets(self) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(
                PlatformSpec("windows_arm64"),
                PlatformSpec("linux_i686"),
            ),
        )
        labels = {t.label for t in matrix.expand()}
        assert labels == {"py312-windows_arm64", "py312-linux_i686"}

    def test_linux_armv7l_expands_to_a_target(self) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(PlatformSpec("linux_armv7l"),),
        )
        labels = {t.label for t in matrix.expand()}
        assert labels == {"py312-linux_armv7l"}


class TestPythonAxisEnvironment:
    def test_single_component_version_pads_python_version(self) -> None:
        """``python_version`` is padded to major.minor like full to three."""
        env = python_axis_environment("3")
        assert env == {"python_version": "3.0", "python_full_version": "3.0.0"}

    def test_unparseable_version_raises_named_error(self) -> None:
        """The error names the bad ``python_version`` input."""
        with pytest.raises(InvalidVersion, match="python_version 'not-a-version'"):
            python_axis_environment("not-a-version")

    def test_two_component_prerelease_keeps_tag(self) -> None:
        """A 2-release-component prerelease keeps its tag in full version."""
        env = python_axis_environment("3.14a1")
        assert env["python_version"] == "3.14"
        assert Version(env["python_full_version"]) == Version("3.14a1")

    def test_two_component_prerelease_not_treated_as_final(self) -> None:
        """A prerelease target must not satisfy a final-release marker."""
        env = python_axis_environment("3.14rc1")
        assert Marker('python_full_version >= "3.14.0"').evaluate(env) is False
        assert Marker('python_full_version == "3.14.0"').evaluate(env) is False

    def test_epoch_is_kept_when_padding(self) -> None:
        """The epoch survives the pad to three release components."""
        env = python_axis_environment("1!3.9")
        assert env["python_full_version"] == "1!3.9.0"


class TestApplyPythonAxisOverlay:
    def test_cpython_moves_implementation_version_with_axis(self) -> None:
        """On CPython the axis move drags implementation_version along."""
        env = {
            "implementation_name": "cpython",
            "python_version": "3.11",
            "python_full_version": "3.11.5",
            "implementation_version": "3.11.5",
        }
        apply_python_axis_overlay(env, {"python_version": "3.8"})
        assert env["python_full_version"] == "3.8.0"
        assert env["implementation_version"] == "3.8.0"

    def test_non_cpython_keeps_its_implementation_version(self) -> None:
        """A PyPy axis move keeps the interpreter's own release version."""
        env = {
            "implementation_name": "pypy",
            "python_version": "3.9",
            "python_full_version": "3.9.18",
            "implementation_version": "7.3.13",
        }
        apply_python_axis_overlay(env, {"python_version": "3.10"})
        assert env["python_full_version"] == "3.10.0"
        assert env["implementation_version"] == "7.3.13"

    def test_patch_precision_python_version_normalizes_to_minor(self) -> None:
        """A patch-precision python_version overlay normalizes to major.minor."""
        env = {
            "implementation_name": "cpython",
            "python_version": "3.11",
            "python_full_version": "3.11.5",
            "implementation_version": "3.11.5",
        }
        apply_python_axis_overlay(env, {"python_version": "3.10.5"})
        assert env["python_version"] == "3.10"
        assert env["python_full_version"] == "3.10.5"
        assert Marker('python_version == "3.10"').evaluate(env) is True

    def test_overlay_without_the_axis_is_a_plain_merge(self) -> None:
        env = {"python_version": "3.11", "python_full_version": "3.11.5"}
        apply_python_axis_overlay(env, {"sys_platform": "win32"})
        assert env["sys_platform"] == "win32"
        assert env["python_full_version"] == "3.11.5"

    def test_explicit_implementation_version_override_wins(self) -> None:
        """An explicit implementation_version override is kept verbatim."""
        env = {
            "implementation_name": "cpython",
            "python_version": "3.9",
            "python_full_version": "3.9.0",
            "implementation_version": "3.9.0",
        }
        apply_python_axis_overlay(
            env, {"python_version": "3.9", "implementation_version": "3.9.7"}
        )
        assert env["implementation_version"] == "3.9.7"


class TestMarkerVariables:
    """The lock's ``environments`` declaration is built from the PEP 508
    variables the resolve's markers named, so the scan must find every
    one the spec defines and nothing else.
    """

    def test_finds_every_variable_a_marker_names(self) -> None:
        found = marker_variables(
            'python_full_version < "3.11.4" and sys_platform == "win32"'
        )
        assert found == {"python_full_version", "sys_platform"}

    def test_python_version_does_not_match_inside_python_full_version(self) -> None:
        assert marker_variables('python_full_version >= "3.9.1"') == {
            "python_full_version"
        }

    def test_a_marker_naming_nothing_yields_nothing(self) -> None:
        """``extra`` is not a lock-environment axis, so it is filtered out."""
        assert marker_variables('extra == "docs"') == frozenset()

    def test_set_variables_are_not_environment_axes(self) -> None:
        """The set variables never enter the ``environments`` declaration:
        a lock cannot pin ``extras`` / ``dependency_groups``."""
        assert marker_variables('"docs" in extras') == frozenset()
        assert marker_variables('"g" in dependency_groups') == frozenset()

    def test_a_name_in_a_string_literal_is_not_a_referenced_variable(self) -> None:
        """A quoted value on the right of a variable comparison is not a variable
        the marker reads, so parsing reports only the bare variable.
        """
        assert marker_variables('os_name == "platform_machine"') == {"os_name"}

    def test_unparseable_marker_raises(self) -> None:
        """A string that is not a valid marker raises InvalidMarker."""
        with pytest.raises(InvalidMarker):
            marker_variables("this is not a marker")

    def test_arbitrary_equality_still_yields_the_variable_name(self) -> None:
        """A ``===`` marker names a variable even though the algebra rejects it
        at construction: extracting names does not build atoms.
        """
        assert marker_variables('python_full_version === "3.13.5"') == {
            "python_full_version"
        }
        assert marker_variables('implementation_version === "3.13.5"') == {
            "implementation_version"
        }
        assert marker_variables('platform_release === "6.8.0"') == {"platform_release"}

    def test_every_declared_variable_is_a_marker_environment_key(self) -> None:
        target = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert target.marker_env.keys() >= PEP508_MARKER_VARIABLES

    def test_variables_are_exactly_the_ones_packaging_defines(self) -> None:
        """The filter set is packaging's environment, not a subset of it.

        ``marker_variables`` intersects with it, so a variable missing here
        drops out of the lock's ``environments`` declaration and the lock
        claims to cover an installer that answers the marker the other way.
        """
        assert set(default_environment()) == PEP508_MARKER_VARIABLES

    def test_a_repeated_marker_text_is_parsed_once(self) -> None:
        """The second call reads the memo instead of parsing the text again."""
        text = 'sys_platform == "linux" and python_version >= "3.10"'
        assert marker_variables(text) is marker_variables(text)


class TestEnvironmentDeclaration:
    """A single-environment lock declares the environment it was resolved
    for: an installer that answers a consulted marker differently needs a
    different package set, so the declaration has to refuse it.
    """

    def _target(self, full_version: str = "3.13.2") -> ResolveTarget:
        def env_source() -> dict[str, str]:
            return {**_HOST_ENV, "python_full_version": full_version}

        return ResolveTarget.for_host(env_source=env_source, tags_source=_host_tags)

    def test_the_three_axes_are_declared_unconsulted(self) -> None:
        declaration = environment_declaration(self._target(), ())
        assert declaration == (
            'python_version == "3.13" and sys_platform == "linux"'
            ' and platform_machine == "x86_64"'
        )

    def test_a_consulted_variable_is_declared(self) -> None:
        declaration = environment_declaration(
            self._target(), [Marker('platform_system == "Windows"')]
        )
        assert declaration.endswith('and platform_system == "Linux"')
        assert Marker(declaration).evaluate(_HOST_ENV)

    def test_an_arbitrary_equality_marker_adds_no_full_version_clause(self) -> None:
        """A consulted ``===`` on the version axis names the axis (the scrape
        collects it) but the algebra rejects the ``===`` atom, so it yields no
        boundary: a whole target adds no ``python_full_version`` clause and the
        declaration still holds on the target.
        """
        declaration = environment_declaration(
            self._target(), [Marker('python_full_version === "3.13.5"')]
        )
        assert "python_full_version" not in declaration
        assert Marker(declaration).evaluate(self._target().marker_env)

    def test_consulted_variables_are_declared_in_sorted_order(self) -> None:
        declaration = environment_declaration(
            self._target(),
            [Marker('platform_system == "Windows"'), Marker('os_name == "nt"')],
        )
        assert declaration.endswith(
            'and os_name == "posix" and platform_system == "Linux"'
        )

    def test_unboundable_variables_are_never_declared(self) -> None:
        declaration = environment_declaration(
            self._target(),
            [Marker('platform_release >= "5.10" and platform_version == "#1 SMP"')],
        )
        assert "platform_release" not in declaration
        assert "platform_version" not in declaration

    def test_the_declaration_refuses_a_foreign_environment(self) -> None:
        declaration = environment_declaration(
            self._target(), [Marker('platform_system == "Windows"')]
        )
        windows = {**_HOST_ENV, "sys_platform": "win32", "platform_system": "Windows"}
        assert not Marker(declaration).evaluate(windows)

    def test_a_lone_cpython_target_leaves_the_interpreter_open(self) -> None:
        """CPython alone is the default, so the axis is nobody's question."""
        assert "implementation_name" not in environment_declaration(self._target(), ())

    def test_a_pypy_target_declares_its_interpreter(self) -> None:
        """The pins were chosen for PyPy's wheels, so a CPython must not take them."""
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64"),
            implementation="pypy",
        )
        declaration = environment_declaration(target, ())
        assert 'implementation_name == "pypy"' in declaration
        assert not Marker(declaration).evaluate(
            {**_HOST_ENV, "python_version": "3.11", "implementation_name": "cpython"}
        )

    def test_a_multi_implementation_matrix_declares_both_sides(self) -> None:
        """The two entries for one (python, platform) point must stay disjoint."""
        matrix = Matrix(
            python="==3.11",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementations=("cpython", "pypy"),
        )
        cpython, pypy = (environment_declaration(t, ()) for t in matrix.expand())
        assert 'implementation_name == "cpython"' in cpython
        assert 'implementation_name == "pypy"' in pypy

        on_pypy = declared_environment("3.11", PlatformSpec("linux_x86_64"), "pypy")
        assert Marker(pypy).evaluate(on_pypy)
        assert not Marker(cpython).evaluate(on_pypy)

    def test_a_pypy_target_drops_the_implementation_version_clause(self) -> None:
        """PyPy 3.11 reports 7.3.x, not 3.11.0, so a bound on the synthetic
        3.11.0 would refuse the very interpreter the lock targets.
        """
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64"),
            implementation="pypy",
        )
        declaration = environment_declaration(
            target, [Marker('implementation_version < "7.3"')]
        )
        assert "implementation_version" not in declaration

        real_pypy = {
            **declared_environment("3.11", PlatformSpec("linux_x86_64"), "pypy"),
            "implementation_version": "7.3.17",
            "python_full_version": "3.11.9",
        }
        assert Marker(declaration).evaluate(real_pypy)


class TestVersionEmission:
    """``python_full_version`` is never declared by value.  A minor target is a
    micro interval and a consulted marker splits it at its boundary, so each
    slice carries its own ``python_full_version`` bounds; a whole target and an
    unsplit minor emit no ``python_full_version`` clause at all.
    """

    def _minor(self) -> ResolveTarget:
        return ResolveTarget.for_declared(
            python_version="3.12", spec=PlatformSpec("linux_x86_64")
        )

    def _host(self, full_version: str = "3.13.2") -> ResolveTarget:
        def env_source() -> dict[str, str]:
            return {**_HOST_ENV, "python_full_version": full_version}

        return ResolveTarget.for_host(env_source=env_source, tags_source=_host_tags)

    def test_an_unsplit_minor_emits_a_plain_row(self) -> None:
        """A minor no marker split reverts to a plain ``python_version`` row."""
        declaration = environment_declaration(self._minor(), ())
        assert declaration == (
            'python_version == "3.12" and sys_platform == "linux"'
            ' and platform_machine == "x86_64"'
        )

    def test_a_uniform_marker_leaves_the_minor_plain(self) -> None:
        """A consulted marker whose boundary falls outside the minor is uniform,
        so it splits nothing and the row carries no python_full_version."""
        declaration = environment_declaration(
            self._minor(), [Marker('python_full_version <= "3.11.0a6"')]
        )
        assert "python_full_version" not in declaration

    def test_a_whole_target_emits_no_full_version_clause(self) -> None:
        """A host names a real micro and is whole, so a consulted marker on the
        micro is not lifted onto the row: the row is its plain minor."""
        declaration = environment_declaration(
            self._host(), [Marker('python_full_version >= "3.13.4"')]
        )
        assert "python_full_version" not in declaration
        assert declaration == (
            'python_version == "3.13" and sys_platform == "linux"'
            ' and platform_machine == "x86_64"'
        )

    def test_a_split_slice_emits_dev0_lower_and_release_upper(self) -> None:
        """The above slice's lower bound is snapped to ``.dev0``; the below
        slice's upper bound stays release form."""
        below, above = slices_from_points(self._minor(), [Version("3.12.4")])
        consulted = [Marker('python_full_version >= "3.12.4"')]
        assert 'python_full_version < "3.12.4"' in environment_declaration(
            below, consulted
        )
        assert 'python_full_version >= "3.12.4.dev0"' in environment_declaration(
            above, consulted
        )

    def test_a_consulted_marker_without_the_axis_adds_no_clause(self) -> None:
        """A consulted marker that never reads ``python_full_version`` does not
        disturb the slice bounds; only the markers that read the axis split it,
        so an off-axis ``os_name`` clause leaves the split untouched."""
        below, above = slices_from_points(self._minor(), [Version("3.12.4")])
        consulted = [
            Marker('python_full_version >= "3.12.4"'),
            Marker('os_name == "posix"'),
        ]
        assert 'python_full_version < "3.12.4"' in environment_declaration(
            below, consulted
        )
        assert 'python_full_version >= "3.12.4.dev0"' in environment_declaration(
            above, consulted
        )

    def test_implementation_version_mirrors_the_slice_bounds(self) -> None:
        """On CPython ``implementation_version`` tracks ``python_full_version``,
        so a marker that consulted it gets the slice's bounds mirrored onto its
        own name alongside the ``python_full_version`` ones."""
        _below, above = slices_from_points(self._minor(), [Version("3.12.4")])
        declaration = environment_declaration(
            above, [Marker('implementation_version >= "3.12.4"')]
        )
        assert 'python_full_version >= "3.12.4.dev0"' in declaration
        assert 'implementation_version >= "3.12.4.dev0"' in declaration

    def test_implementation_version_is_not_mirrored_when_not_consulted(self) -> None:
        """No marker consulted ``implementation_version``, so only the
        ``python_full_version`` bound is emitted."""
        _below, above = slices_from_points(self._minor(), [Version("3.12.4")])
        declaration = environment_declaration(
            above, [Marker('python_full_version >= "3.12.4"')]
        )
        assert "implementation_version" not in declaration


class TestUnboundableVariables:
    """CPython carries ``implementation_version`` as its own micro release;
    a non-CPython target's value there is synthetic, so the lock cannot
    bound it.
    """

    def test_cpython_names_only_the_kernel_axes(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64"),
            implementation="cpython",
        )
        assert unboundable_variables(target) == UNBOUNDABLE_MARKER_VARIABLES

    def test_pypy_adds_implementation_version(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64"),
            implementation="pypy",
        )
        assert unboundable_variables(target) == (
            UNBOUNDABLE_MARKER_VARIABLES | {"implementation_version"}
        )


class TestEnvironmentDeclarationDocumented:
    """The lockfile reference names every variable an ``environments`` row
    singles out: one it never pins by value, and one it carries as a slice
    bound.
    """

    def _documented_names(self) -> set[str]:
        doc = Path(__file__).resolve().parents[2] / "docs" / "reference" / "lockfile.md"
        text = doc.read_text(encoding="utf-8")

        start = text.index("### The environments the lock is for")
        end = text.index("\n### ", start + 1)
        section = text[start:end]

        # Backticks wrap whole clauses, so split each span into bare names.
        return {
            token
            for span in re.findall(r"`([^`]+)`", section)
            for token in re.findall(r"[a-z_]+", span)
        }

    def _target(self, implementation: str) -> ResolveTarget:
        return ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64"),
            implementation=implementation,
        )

    def _never_pinned(self, target: ResolveTarget) -> set[str]:
        """Return the variables a consulted marker does not pin by value."""
        never: set[str] = set()
        for name in PEP508_MARKER_VARIABLES:
            value = target.marker_env[name]
            row = environment_declaration(target, [Marker(f'{name} == "{value}"')])
            if f'{name} == "{value}"' not in row:
                never.add(name)

        return never

    @pytest.mark.parametrize("implementation", ["cpython", "pypy"])
    def test_a_variable_the_row_never_pins_is_documented(
        self, implementation: str
    ) -> None:
        missing = self._never_pinned(self._target(implementation))
        missing -= self._documented_names()

        assert not missing, f"undocumented on {implementation}: {sorted(missing)}"

    def test_a_slice_bound_the_row_carries_is_documented(self) -> None:
        target = self._target("cpython")
        consulted = [Marker('implementation_version >= "3.11.4"')]

        carried: set[str] = set()
        for slice_ in slices_from_points(
            target, micro_boundary_points(target, consulted)
        ):
            carried |= marker_variables(environment_declaration(slice_, consulted))

        missing = carried - self._documented_names()
        assert not missing, f"undocumented clause variables: {sorted(missing)}"


class TestDeclaredRangeMarker:
    """The environment a target stands for on its whole declared range."""

    def test_a_minor_interval_leaves_the_micro_open(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.13", spec=PlatformSpec("linux_x86_64")
        )
        assert target.is_minor_interval
        assert declared_range_marker(target) == (
            'implementation_name == "cpython" and os_name == "posix"'
            ' and platform_machine == "x86_64"'
            ' and platform_python_implementation == "CPython"'
            ' and platform_system == "Linux" and python_version == "3.13"'
            ' and sys_platform == "linux"'
        )

    def test_a_host_target_pins_the_full_version(self) -> None:
        target = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert not target.is_minor_interval
        assert declared_range_marker(target) == (
            'implementation_name == "cpython" and os_name == "posix"'
            ' and platform_machine == "x86_64"'
            ' and platform_python_implementation == "CPython"'
            ' and platform_system == "Linux" and python_version == "3.13"'
            ' and sys_platform == "linux"'
            ' and python_full_version == "3.13.2"'
        )

    def test_a_python_patches_micro_pins_the_full_version(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.13",
            spec=PlatformSpec("linux_x86_64"),
            python_full_version="3.13.4",
        )
        assert not target.is_minor_interval
        assert declared_range_marker(target).endswith(
            'and python_full_version == "3.13.4"'
        )

    def test_the_kernel_and_by_constraint_axes_are_never_pinned(self) -> None:
        target = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        marker = declared_range_marker(target)
        assert "platform_release" not in marker
        assert "platform_version" not in marker
        assert "implementation_version" not in marker

    def test_a_non_cpython_target_pins_its_implementation(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.11",
            spec=PlatformSpec("linux_x86_64"),
            implementation="pypy",
        )
        assert target.is_minor_interval
        assert declared_range_marker(target) == (
            'implementation_name == "pypy" and os_name == "posix"'
            ' and platform_machine == "x86_64"'
            ' and platform_python_implementation == "PyPy"'
            ' and platform_system == "Linux" and python_version == "3.11"'
            ' and sys_platform == "linux"'
        )

    def test_the_marker_evaluates_true_on_the_target_environment(self) -> None:
        target = ResolveTarget.for_declared(
            python_version="3.13", spec=PlatformSpec("linux_x86_64")
        )
        assert Marker(declared_range_marker(target)).evaluate(target.marker_env)


class TestMicroBoundarySplitting:
    """A declared target names a minor and synthesizes its ``.0`` micro.  A
    consulted marker with an in-minor python_full_version boundary cuts the
    micro line, and the minor is resolved once per slice.
    """

    def _target(
        self, python_version: str = "3.12", full_version: str | None = None
    ) -> ResolveTarget:
        return ResolveTarget.for_declared(
            python_version=python_version,
            spec=PlatformSpec("linux_x86_64"),
            python_full_version=full_version,
        )

    def _probe(self, full_version: str) -> dict[str, str]:
        return {
            "python_version": "3.12",
            "python_full_version": full_version,
            "sys_platform": "linux",
            "platform_machine": "x86_64",
            "implementation_name": "cpython",
        }

    @pytest.mark.parametrize(
        ("marker", "points"),
        [
            ('python_full_version < "3.12.4"', ["3.12.4"]),
            ('python_full_version >= "3.12.4"', ["3.12.4"]),
            ('python_full_version <= "3.12.4"', ["3.12.5"]),
            ('python_full_version > "3.12.4"', ["3.12.5"]),
            ('python_full_version == "3.12.4"', ["3.12.4", "3.12.5"]),
            ('python_full_version != "3.12.4"', ["3.12.4", "3.12.5"]),
            ('python_full_version ~= "3.12.4"', ["3.12.4"]),
            ('python_full_version == "3.12.*"', []),
            ('python_full_version == "3.12.4.*"', ["3.12.4", "3.12.5"]),
        ],
    )
    def test_each_operator_maps_to_its_boundaries(
        self, marker: str, points: list[str]
    ) -> None:
        """``<``/``>=`` cut at the literal, ``<=``/``>`` at the release after
        it, and ``==``/``!=``/``~=``/``== V.*`` at each edge of the region they
        name.  A boundary outside the minor or at its floor cuts nothing, so
        ``~= "3.12.4"`` cuts only its lower edge and ``== "3.12.*"`` cuts
        nothing.  Every boundary comes from an edge of the range's
        ``release_intervals``.
        """
        found = micro_boundary_points(self._target(), [Marker(marker)])
        assert [str(point) for point in found] == points

    @pytest.mark.parametrize(
        ("marker", "points"),
        [
            ('"3.12.4" > python_full_version', ["3.12.4"]),
            ('"3.12.4" <= python_full_version', ["3.12.4"]),
            ('"3.12.4" >= python_full_version', ["3.12.5"]),
            ('"3.12.4" < python_full_version', ["3.12.5"]),
            ('"3.12.4" == python_full_version', ["3.12.4", "3.12.5"]),
            ('"3.12.4" != python_full_version', ["3.12.4", "3.12.5"]),
        ],
    )
    def test_a_literal_on_the_left_mirrors_the_operator(
        self, marker: str, points: list[str]
    ) -> None:
        """PEP 508 allows the literal first, and packaging keeps that order.
        An ordered or symmetric operator is mirrored back to variable-on-left
        form, so each literal-first clause cuts at the same release as its
        variable-first equivalent: ``"3.12.4" > python_full_version`` reads
        like ``< "3.12.4"``.
        """
        found = micro_boundary_points(self._target(), [Marker(marker)])
        assert [str(point) for point in found] == points

    @pytest.mark.parametrize(
        ("marker", "points"),
        [
            ('python_full_version < "3.12.0"', []),
            ('python_full_version >= "3.12.0"', []),
            ('python_full_version <= "3.12.0"', ["3.12.1"]),
            ('python_full_version > "3.12.0"', ["3.12.1"]),
            ('python_full_version < "3.12"', []),
            ('python_full_version >= "3.12"', []),
            ('python_full_version <= "3.12"', ["3.12.1"]),
            ('python_full_version > "3.12"', ["3.12.1"]),
        ],
    )
    def test_a_boundary_at_the_floor_splits_only_after_the_literal(
        self, marker: str, points: list[str]
    ) -> None:
        """``.0`` is the minor's floor. ``<``/``>=`` flip there and cut
        nothing, but ``<=``/``>`` flip at ``.1``, so they still split. The
        two-component literal ``3.12`` equals ``3.12.0`` and behaves the same.
        """
        found = micro_boundary_points(self._target(), [Marker(marker)])
        assert [str(point) for point in found] == points

    def test_an_after_literal_prerelease_below_the_floor_does_not_split(
        self,
    ) -> None:
        """``<= "3.11.0a6"`` flips at ``3.11.0``, the minor's floor, so the
        whole real minor reads it False. Bumping to ``3.11.1`` would split it
        spuriously; the flip is the boundary's own final release, not the next
        micro."""
        target = self._target(python_version="3.11")
        markers = [
            Marker('python_full_version <= "3.11.0a6"'),
            Marker('python_full_version > "3.11.0a6"'),
        ]
        assert micro_boundary_points(target, markers) == []

    def test_an_after_literal_prerelease_above_the_floor_splits_at_its_final(
        self,
    ) -> None:
        """``<= "3.12.2a1"`` flips at ``3.12.2``, its own final release, so a
        real 3.12.1 and 3.12.2 land on opposite slices."""
        found = micro_boundary_points(
            self._target(), [Marker('python_full_version <= "3.12.2a1"')]
        )
        assert [str(point) for point in found] == ["3.12.2"]

    def test_an_after_literal_post_release_splits_at_the_next_micro(self) -> None:
        """A post release sorts above its final, so ``<= "3.11.0.post1"`` still
        flips at the next micro, ``3.11.1``."""
        found = micro_boundary_points(
            self._target(python_version="3.11"),
            [Marker('python_full_version <= "3.11.0.post1"')],
        )
        assert [str(point) for point in found] == ["3.11.1"]

    def test_a_boundary_outside_the_minor_does_not_split(self) -> None:
        """An earlier or later minor is the whole-minor declaration's business."""
        markers = [
            Marker('python_full_version < "3.11.5"'),
            Marker('python_full_version >= "3.13.0"'),
        ]
        assert micro_boundary_points(self._target(), markers) == []

    @pytest.mark.parametrize(
        "marker",
        [
            'python_full_version < "1!3.12.4"',
            'python_full_version >= "1!3.12.4"',
            'python_full_version <= "1!3.12.4"',
            'python_full_version > "1!3.12.4"',
            'python_full_version == "1!3.12.4"',
            'python_full_version != "1!3.12.4"',
            'python_full_version ~= "1!3.12.4"',
            'python_full_version == "1!3.12.*"',
            'python_full_version < "1!3.12.4rc1"',
            'python_full_version >= "1!3.12.4.post1"',
            '"1!3.12.4" > python_full_version',
            'implementation_version >= "1!3.12.4"',
        ],
    )
    def test_an_epoch_tagged_literal_does_not_split(self, marker: str) -> None:
        """An epoch-tagged literal is outside ``[3.12.0, 3.13.0)`` whatever
        its release says, and no interpreter reports an epoch, so the clause
        is uniform across the real minor and splits nothing.
        """
        assert micro_boundary_points(self._target(), [Marker(marker)]) == []

    def test_an_epoch_tagged_literal_reads_the_same_on_every_micro(self) -> None:
        """The clause a split would have cut at reads the same on the slice
        representative as on every real micro of the minor.
        """
        marker = Marker('python_full_version >= "1!3.12.4"')
        assert not marker.evaluate(self._probe("3.12.0"))
        assert not marker.evaluate(self._probe("3.12.4"))
        assert not marker.evaluate(self._probe("3.12.19"))

    def test_a_prerelease_literal_below_the_floor_does_not_split(self) -> None:
        """``>= "3.12.0a1"`` names a prerelease of the floor, so it is uniform
        across the real minor under the rides-with-X convention and crashes
        nothing: its release form ``3.12.0`` is not above the floor."""
        assert (
            micro_boundary_points(
                self._target(), [Marker('python_full_version >= "3.12.0a1"')]
            )
            == []
        )

    def test_a_prerelease_literal_outside_the_minor_does_not_split(self) -> None:
        """A prerelease of another minor is uniform here, so no crash."""
        assert (
            micro_boundary_points(
                self._target(), [Marker('python_full_version < "3.13.4rc1"')]
            )
            == []
        )

    def test_a_post_release_literal_of_the_floor_crashes(self) -> None:
        """A post release sorts above its release, so ``>= "3.12.0.post1"``
        splits 3.12.0 (False) off 3.12.1 (True) at a boundary no micro sits on.
        Unlike the prerelease of the floor ``>= "3.12.0a1"``, which is uniform,
        the post-release of the floor is a loud crash."""
        with pytest.raises(NonIntervalMarkerError):
            micro_boundary_points(
                self._target(), [Marker('python_full_version >= "3.12.0.post1"')]
            )

    def test_a_post_release_literal_outside_the_minor_does_not_split(self) -> None:
        """A post release of another minor is uniform across this one, so it
        crashes nothing: ``>= "3.13.4.post1"`` is False for every 3.12 micro."""
        assert (
            micro_boundary_points(
                self._target(), [Marker('python_full_version >= "3.13.4.post1"')]
            )
            == []
        )

    def test_a_before_literal_post_release_splits_at_the_next_micro(self) -> None:
        """A post release sorts above its release, so the exclusive-upper
        ``< "3.12.4.post1"`` pushes its boundary to the next real micro and
        tiles cleanly, cutting at 3.12.5 like ``<= "3.12.4"``."""
        found = micro_boundary_points(
            self._target(), [Marker('python_full_version < "3.12.4.post1"')]
        )
        assert [str(point) for point in found] == ["3.12.5"]

    @pytest.mark.parametrize(
        ("marker", "points"),
        [
            ('python_full_version > "3.12.4rc1"', ["3.12.4"]),
            ('python_full_version > "3.12.4.post1"', ["3.12.5"]),
        ],
    )
    def test_an_after_literal_pre_or_post_release_splits_above_the_literal(
        self, marker: str, points: list[str]
    ) -> None:
        """``>`` puts its boundary past the literal, so it lands on a real
        micro either way: a prerelease cuts at its own release, 3.12.4, and a
        post release at the one after it, 3.12.5."""
        found = micro_boundary_points(self._target(), [Marker(marker)])
        assert [str(point) for point in found] == points

    @pytest.mark.parametrize(
        "marker",
        [
            "python_full_version < python_version",
            "python_full_version >= implementation_version",
            'python_full_version < "3.12.x"',
            'python_full_version == "wat"',
            'python_full_version === "3.12.4"',
            'python_full_version in "3.12.4"',
            'python_full_version not in "3.12.4"',
            'python_full_version < "3.12.4rc1"',
            'python_full_version >= "3.12.4rc1"',
            'python_full_version == "3.12.4rc1"',
            'python_full_version != "3.12.4rc1"',
            'python_full_version ~= "3.12.4b1"',
            '"3.12.4" ~= python_full_version',
            '"3.12.4rc1" == python_full_version',
            'python_full_version >= "3.12.4.post1"',
            'python_full_version ~= "3.12.4.post1"',
            'python_full_version == "3.12.4.post1"',
            'python_full_version != "3.12.4.post1"',
            '"3.12.4.post1" == python_full_version',
        ],
    )
    def test_an_untileable_marker_crashes(self, marker: str) -> None:
        """A membership, verbatim ===, non-version, variable, or interior
        pre- or post-release comparison on a minor interval is a loud crash:
        the whole-minor pin that once absorbed it is gone. ``>= "3.12.4.post1"``
        flips between the 3.12.4 and 3.12.5 micros just as ``>= "3.12.4rc1"``
        flips between 3.12.3 and 3.12.4, so neither lands on a release the lock
        can render."""
        with pytest.raises(NonIntervalMarkerError):
            micro_boundary_points(self._target(), [Marker(marker)])

    @pytest.mark.parametrize(
        "marker",
        [
            'python_full_version < "3.12.*"',
            'python_full_version <= "3.12.*"',
            'python_full_version > "3.12.*"',
            'python_full_version >= "3.12.*"',
            'python_full_version < "3.13.*"',
            'python_full_version < "3.11.0rc1.*"',
            'python_full_version ~= "3"',
            'python_full_version ~= "3.12.*"',
            'implementation_version >= "3.12.*"',
        ],
    )
    def test_a_literal_the_operator_rejects_crashes(self, marker: str) -> None:
        """A ``.*`` suffix is a valid marker literal, but a valid specifier
        only under ``==``/``!=``, and ``~=`` needs two release components.
        The clause parses and the literal is a version either way, so the
        mismatch shows up only when the specifier is built.

        The literal names no interval under its operator whatever minor reads
        it, so a literal outside the target's own minor is refused too.
        """
        with pytest.raises(NonIntervalMarkerError):
            micro_boundary_points(self._target(), [Marker(marker)])

    def test_the_crash_names_the_same_clause_in_either_order(self) -> None:
        """Two untileable markers name the same clause in either order.

        The caller passes an unordered set, so the scan order varies per run.
        """
        markers = [
            Marker('python_full_version in "3.12.1"'),
            Marker('python_full_version === "3.12.4"'),
        ]

        messages: set[str] = set()
        for ordering in (markers, list(reversed(markers))):
            with pytest.raises(NonIntervalMarkerError) as caught:
                micro_boundary_points(self._target(), ordering)
            messages.add(str(caught.value))

        assert len(messages) == 1
        assert 'clause python_full_version === "3.12.4" cannot tile' in messages.pop()

    def test_a_non_version_clause_is_ignored(self) -> None:
        """Only the python_full_version clause of a marker is a boundary."""
        found = micro_boundary_points(
            self._target(),
            [Marker('python_full_version < "3.12.4" and os_name == "posix"')],
        )
        assert [str(point) for point in found] == ["3.12.4"]

    def test_many_boundaries_in_one_minor_all_split(self) -> None:
        found = micro_boundary_points(
            self._target(),
            [
                Marker('python_full_version < "3.12.2"'),
                Marker('python_full_version >= "3.12.6"'),
                Marker('python_full_version <= "3.12.8"'),
            ],
        )
        assert [str(point) for point in found] == ["3.12.2", "3.12.6", "3.12.9"]

    def test_a_host_target_is_never_split(self) -> None:
        """A host target names a real micro, so nothing is synthesized to cut."""
        host = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert (
            micro_boundary_points(host, [Marker('python_full_version < "3.13.4"')])
            == []
        )

    def test_a_user_pinned_micro_is_never_split(self) -> None:
        """A declared target moved past ``.0`` names a real deployment micro."""
        assert (
            micro_boundary_points(
                self._target(full_version="3.12.4"),
                [Marker('python_full_version < "3.12.6"')],
            )
            == []
        )

    def test_a_bare_minor_python_target_is_split(self) -> None:
        """``--python 3.11`` (and a platform-less ``[tool.nab.environment]``)
        synthesizes ``3.11.0`` like a matrix tuple, so an in-minor boundary
        cuts it even though it carries no ``platform_spec``."""
        target = ResolveTarget.for_host_python(
            "3.11", env_source=_host_env, tags_source=_host_tags
        )
        assert target.platform_spec is None
        found = micro_boundary_points(
            target, [Marker('python_full_version < "3.11.4"')]
        )
        assert [str(point) for point in found] == ["3.11.4"]

    def test_a_host_target_at_a_zero_micro_is_never_split(self) -> None:
        """A host reporting ``3.13.0`` names a real interpreter, so it stays
        whole even though its micro equals the minor's floor."""
        host = ResolveTarget.for_host(
            env_source=lambda: {**_host_env(), "python_full_version": "3.13.0"},
            tags_source=_host_tags,
        )
        assert (
            micro_boundary_points(host, [Marker('python_full_version < "3.13.4"')])
            == []
        )

    def test_no_points_leaves_the_target_whole(self) -> None:
        target = self._target()
        assert slices_from_points(target, []) == [target]

    def test_one_point_makes_two_slices(self) -> None:
        below, above = slices_from_points(self._target(), [Version("3.12.4")])
        assert below.label == "py312-linux_x86_64-pf3120"
        assert below.python_full_version == "3.12.0"
        assert below.micro_clauses == ('python_full_version < "3.12.4"',)
        assert above.label == "py312-linux_x86_64-pf3124"
        assert above.python_full_version == "3.12.4"
        assert above.micro_clauses == ('python_full_version >= "3.12.4.dev0"',)

    def test_a_middle_slice_carries_both_bounds(self) -> None:
        points = [Version("3.12.4"), Version("3.12.8")]
        _low, mid, _high = slices_from_points(self._target(), points)
        assert mid.python_full_version == "3.12.4"
        assert mid.micro_clauses == (
            'python_full_version >= "3.12.4.dev0"',
            'python_full_version < "3.12.8"',
        )

    def test_slice_rows_are_disjoint_and_cover_the_minor(self) -> None:
        below, above = slices_from_points(self._target(), [Version("3.12.4")])
        consulted = [Marker('python_full_version >= "3.12.4"')]
        below_row = environment_declaration(below, consulted)
        above_row = environment_declaration(above, consulted)
        assert 'python_full_version < "3.12.4"' in below_row
        assert 'python_full_version >= "3.12.4.dev0"' in above_row
        assert Marker(below_row).evaluate(self._probe("3.12.1"))
        assert not Marker(below_row).evaluate(self._probe("3.12.5"))
        assert Marker(above_row).evaluate(self._probe("3.12.5"))
        assert not Marker(above_row).evaluate(self._probe("3.12.1"))

    def test_a_slice_emits_its_own_bounds_not_the_consulted_clause(self) -> None:
        """The row carries the slice's own ``python_full_version`` bounds, not
        the consulted clause: a ``<=`` boundary flips one release past the
        literal, so the slice's upper bound ``< "3.12.5"`` is what fences it,
        and the raw ``<= "3.12.4"`` clause is not lifted onto the row."""
        below, _above = slices_from_points(self._target(), [Version("3.12.5")])
        row = environment_declaration(
            below, [Marker('python_full_version <= "3.12.4"')]
        )
        assert 'python_full_version < "3.12.5"' in row
        assert 'python_full_version <= "3.12.4"' not in row

    def test_a_slice_marker_string_carries_its_micro_bounds(self) -> None:
        below, above = slices_from_points(self._target(), [Version("3.12.4")])
        assert below.environment_marker_string.endswith(
            'and python_full_version < "3.12.4"'
        )
        assert 'python_full_version >= "3.12.4.dev0"' in above.environment_marker_string


class TestReleaseIntervals:
    """The public ``VersionRange.release_intervals`` contract nab drives.

    ``micro_boundary_points`` reads the edges of these intervals and filters
    them to the target's minor; here they are exercised on the range directly,
    so the per-operator lattice mapping is pinned without the minor filter.
    """

    @staticmethod
    def _intervals(spec: str, parts: int = 3) -> list[tuple[str | None, str | None]]:
        return [
            (
                None if lower is None else str(lower),
                None if upper is None else str(upper),
            )
            for lower, upper in SpecifierSet(spec).to_range().release_intervals(parts)
        ]

    @pytest.mark.parametrize(
        ("spec", "intervals"),
        [
            ("<3.10.2", [(None, "3.10.2")]),
            ("<=3.10.2", [(None, "3.10.3")]),
            (">3.10.2", [("3.10.3", None)]),
            (">=3.10.2", [("3.10.2", None)]),
            ("==3.10.2", [("3.10.2", "3.10.3")]),
            ("!=3.10.2", [(None, "3.10.2"), ("3.10.3", None)]),
            ("~=3.10.2", [("3.10.2", "3.11.0")]),
            ("==3.10.*", [("3.10.0", "3.11.0")]),
            ("!=3.10.*", [(None, "3.10.0"), ("3.11.0", None)]),
            ("==3.10.4.*", [("3.10.4", "3.10.5")]),
            (">3.10.2,<3.10.4", [("3.10.3", "3.10.4")]),
        ],
    )
    def test_each_operator_maps_to_lattice_intervals(
        self, spec: str, intervals: list[tuple[str | None, str | None]]
    ) -> None:
        """``<``/``>=`` open at the literal, ``<=``/``>`` at the release just
        above it, and ``==``/``!=``/``~=``/``== V.*`` bracket the region they
        name.  Each interval is a half-open ``[lower, upper)`` release pair, so
        the excluded upper edge is the release just past the region; ``None`` on
        a side is unbounded there."""
        assert self._intervals(spec) == intervals

    def test_a_dev0_snap_edge_reports_its_final_release(self) -> None:
        """``< X`` is carried as ``X.dev0``; the upper edge is the final release
        ``X``, not the dev snap, and ``X`` sits outside the half-open interval."""
        assert self._intervals("<3.11.4") == [(None, "3.11.4")]

    def test_an_upper_over_a_prerelease_reports_its_final_release(self) -> None:
        """``<= "3.11.4rc1"`` sits just above the prerelease, whose smallest
        lattice release above is its own final ``3.11.4``, the interval's
        excluded upper edge."""
        assert self._intervals("<=3.11.4rc1") == [(None, "3.11.4")]

    def test_an_unbounded_side_is_none(self) -> None:
        """An ``-inf`` / ``+inf`` edge names no release, so ``>=`` reports a
        single interval open on the right and ``<`` one open on the left."""
        assert self._intervals(">=3.11.4") == [("3.11.4", None)]
        assert self._intervals("<3.11.4") == [(None, "3.11.4")]

    def test_a_dev0_snapped_lower_bound_reports_its_release(self) -> None:
        """``>= X.dev0`` carries the prerelease as its lower edge; the interval
        starts at the final release ``X``, which the range includes."""
        assert self._intervals(">=3.11.4.dev0") == [("3.11.4", None)]

    def test_an_exclusion_yields_two_intervals(self) -> None:
        """``!=`` cuts the line in two, so the range covers two disjoint
        half-open intervals, one on each side of the excluded release."""
        assert self._intervals("!=3.11.4") == [(None, "3.11.4"), ("3.11.5", None)]

    def test_a_sub_lattice_literal_starts_at_the_release_above_it(self) -> None:
        """A literal finer than the lattice (``2.3.0.1`` on the three-part
        lattice) is not a lattice point, so the interval opens at the smallest
        lattice release above it, which the ``>=`` range includes."""
        assert self._intervals(">=2.3.0.1") == [("2.3.1", None)]

    def test_adjacent_intervals_merge_at_a_shared_edge(self) -> None:
        """Two ranges whose projected intervals meet at one release fold into a
        single interval spanning both."""
        lower = SpecifierSet(">=3.10.1,<3.10.3").to_range()
        upper = SpecifierSet("==3.10.3").to_range()
        merged = (lower | upper).release_intervals(3)
        assert [(str(lo), str(hi)) for lo, hi in merged] == [("3.10.1", "3.10.4")]

    def test_a_sub_lattice_interval_collapses_at_a_coarse_parts(self) -> None:
        """Coarsening the lattice folds both edges of a narrow interval onto one
        release, leaving an empty half-open span that is dropped."""
        assert SpecifierSet("~=3.10.2").to_range().release_intervals(2) == ()
        assert self._intervals(">=3.11.4,<3.11.9", 2) == []

    def test_an_empty_range_covers_no_interval(self) -> None:
        """The empty range has no finite edge and covers nothing."""
        assert VersionRange.empty().release_intervals(3) == ()

    def test_a_full_range_is_one_unbounded_interval(self) -> None:
        """The full range covers one interval unbounded on both sides."""
        assert VersionRange.full().release_intervals(3) == ((None, None),)

    @pytest.mark.parametrize("parts", [0, -1])
    def test_parts_below_one_is_rejected(self, parts: int) -> None:
        with pytest.raises(ValueError, match="parts must be at least 1"):
            SpecifierSet(">=3.11.4").to_range().release_intervals(parts)


class TestCheckFreeThreaded:
    """A free-threaded platform needs a CPython build that has one."""

    def test_a_foreign_implementation_raises(self) -> None:
        with pytest.raises(ValueError, match="needs CPython, not \\['pypy'\\]"):
            check_free_threaded(
                platforms=[PlatformSpec("linux_x86_64", free_threaded=True)],
                implementations=["cpython", "pypy"],
                python_versions=["3.13"],
            )

    def test_a_python_below_the_abi_floor_raises(self) -> None:
        """The ``cpXYt`` ABI ships from 3.13, so no older build satisfies it."""
        with pytest.raises(ValueError, match="CPython 3.13 or newer"):
            check_free_threaded(
                platforms=[PlatformSpec("linux_x86_64", free_threaded=True)],
                implementations=["cpython"],
                python_versions=["3.12", "3.13"],
            )

    def test_cpython_at_the_floor_passes(self) -> None:
        """CPython 3.13 and newer have the build, so the check returns."""
        check_free_threaded(
            platforms=[PlatformSpec("linux_x86_64", free_threaded=True)],
            implementations=["cpython"],
            python_versions=["3.13", "3.14"],
        )
