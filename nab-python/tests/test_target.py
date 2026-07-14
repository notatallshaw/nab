"""Tests for :class:`nab_python.target.ResolveTarget`.

The host constructors take their environment and tag sources as
arguments, so every test here names the interpreter it means instead of
the one running the suite.  One smoke test uses the live sources.
"""

from __future__ import annotations

import pytest

from nab_python._vendor.packaging.markers import Marker
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.tags import Tag
from nab_python._vendor.packaging.version import InvalidVersion, Version
from nab_python.tags import PlatformSpec
from nab_python.target import (
    IMPLEMENTATION_MARKERS,
    PEP508_MARKER_VARIABLES,
    PLATFORM_MARKERS,
    Matrix,
    ResolveTarget,
    apply_python_axis_overlay,
    declared_environment,
    environment_declaration,
    host_environment,
    marker_variables,
    python_axis_environment,
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


class TestPythonRelease:
    """``Requires-Python`` names a release, so a prerelease host is one."""

    def test_a_release_candidate_host_is_its_release(self) -> None:
        """A 3.15 candidate satisfies ``>=3.15``, as it does under pip.

        The PEP 508 marker value keeps the ``rc``, but a specifier admits no
        prerelease unless it names one, so comparing that value would drop
        every distribution requiring the release the host is a candidate for.
        """
        target = ResolveTarget.for_host(
            env_source=lambda: {**_HOST_ENV, "python_full_version": "3.15.0rc1"},
            tags_source=_host_tags,
        )
        assert target.python_full_version == "3.15.0rc1"
        assert target.python_release == Version("3.15.0")
        assert target.python_release in SpecifierSet(">=3.15")

    def test_a_final_release_is_itself(self) -> None:
        target = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert target.python_release == Version(_HOST_ENV["python_full_version"])


class TestTargetPythonIsComparable:
    """Every candidate's Requires-Python is tested against this target."""

    def test_an_unparseable_python_names_itself(self) -> None:
        """A local-build version fails here, not once per candidate."""
        with pytest.raises(ValueError, match="not a PEP 440 version"):
            ResolveTarget.for_host(
                env_source=lambda: {**_HOST_ENV, "python_full_version": "3.11.2+"},
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
        assert marker_variables('extra == "docs"') == frozenset()

    def test_a_name_in_a_string_literal_counts(self) -> None:
        """Over-approximating narrows the declaration, which is the safe way."""
        assert marker_variables('os_name == "platform_machine"') == {
            "os_name",
            "platform_machine",
        }

    def test_every_declared_variable_is_a_marker_environment_key(self) -> None:
        target = ResolveTarget.for_host(env_source=_host_env, tags_source=_host_tags)
        assert target.marker_env.keys() >= PEP508_MARKER_VARIABLES


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


class TestFullVersionDeclaration:
    """``python_full_version`` is declared by constraint, not by value: the
    resolve depends on how its clauses read the micro release, not on the
    micro release itself, so a lock built on 3.13.2 must install on 3.13.9.
    """

    def _target(self, full_version: str) -> ResolveTarget:
        def env_source() -> dict[str, str]:
            return {**_HOST_ENV, "python_full_version": full_version}

        return ResolveTarget.for_host(env_source=env_source, tags_source=_host_tags)

    def test_a_clause_that_read_false_is_declared_complemented(self) -> None:
        """coverage's ``tomli ; python_full_version <= "3.11.0a6"`` is the case."""
        declaration = environment_declaration(
            self._target("3.13.2"), [Marker('python_full_version <= "3.11.0a6"')]
        )
        assert 'python_full_version > "3.11.0a6"' in declaration
        assert "python_full_version ==" not in declaration
        assert Marker(declaration).evaluate(
            {**_HOST_ENV, "python_full_version": "3.13.9"}
        )

    def test_a_clause_that_read_true_is_declared_as_it_stands(self) -> None:
        declaration = environment_declaration(
            self._target("3.13.5"), [Marker('python_full_version >= "3.13.4"')]
        )
        assert declaration.endswith('and python_full_version >= "3.13.4"')

    def test_a_clause_that_splits_the_micros_still_splits_them(self) -> None:
        """The dep this marker gates is in the pins; a 3.13.2 install is not."""
        declaration = environment_declaration(
            self._target("3.13.5"), [Marker('python_full_version >= "3.13.4"')]
        )
        assert Marker(declaration).evaluate(
            {**_HOST_ENV, "python_full_version": "3.13.5"}
        )
        assert not Marker(declaration).evaluate(
            {**_HOST_ENV, "python_full_version": "3.13.2"}
        )

    def test_the_complement_of_a_split_refuses_the_other_side(self) -> None:
        declaration = environment_declaration(
            self._target("3.13.2"), [Marker('python_full_version >= "3.13.4"')]
        )
        assert 'python_full_version < "3.13.4"' in declaration
        assert not Marker(declaration).evaluate(
            {**_HOST_ENV, "python_full_version": "3.13.5"}
        )

    def test_every_full_version_clause_is_declared(self) -> None:
        """nab's own docs graph reads the axis twice, and both readings hold."""
        declaration = environment_declaration(
            self._target("3.13.2"),
            [
                Marker('python_full_version <= "3.11.0a6"'),
                Marker('python_full_version < "3.10.2" and os_name == "posix"'),
            ],
        )
        assert declaration == (
            'python_version == "3.13" and sys_platform == "linux"'
            ' and platform_machine == "x86_64" and os_name == "posix"'
            ' and python_full_version > "3.11.0a6"'
            ' and python_full_version >= "3.10.2"'
        )

    def test_implementation_version_is_declared_by_constraint(self) -> None:
        """It is the micro release under another name, so pinning it is the same bug.

        On CPython ``implementation_version`` reports
        ``sys.implementation.version``, which is the same release
        ``python_full_version`` reports, so declaring its value would refuse
        every interpreter but the one micro the target names.
        """
        declaration = environment_declaration(
            self._target("3.13.2"),
            [Marker('implementation_version >= "3.0"')],
        )
        assert 'implementation_version >= "3.0"' in declaration
        assert 'implementation_version == "3.13.2"' not in declaration
        assert Marker(declaration).evaluate(
            {
                **_HOST_ENV,
                "python_full_version": "3.13.9",
                "implementation_version": "3.13.9",
            }
        )

    def test_a_clause_that_held_is_declared_whatever_its_operator(self) -> None:
        """A clause that held needs no complement, so ``~=`` declares itself.

        Pinning the exact value instead would refuse every other micro of the
        line the resolve was valid for.
        """
        declaration = environment_declaration(
            self._target("3.13.2"), [Marker('python_full_version ~= "3.13.0"')]
        )
        assert declaration.endswith('and python_full_version ~= "3.13.0"')
        assert Marker(declaration).evaluate(
            {**_HOST_ENV, "python_version": "3.13", "python_full_version": "3.13.9"}
        )

    def test_an_operator_with_no_complement_pins_the_value(self) -> None:
        """``~=`` read False has no single-clause negation; the value is sound."""
        declaration = environment_declaration(
            self._target("3.13.2"), [Marker('python_full_version ~= "3.14.0"')]
        )
        assert declaration.endswith('and python_full_version == "3.13.2"')

    def test_a_literal_on_the_left_is_declared_in_place(self) -> None:
        """PEP 508 allows either operand order; the complement keeps it."""
        declaration = environment_declaration(
            self._target("3.13.2"), [Marker('"3.10.2" > python_full_version')]
        )
        assert declaration.endswith('and "3.10.2" <= python_full_version')
        assert Marker(declaration).evaluate(
            {**_HOST_ENV, "python_full_version": "3.13.9"}
        )

    def test_a_name_spelled_only_inside_a_literal_pins_the_value(self) -> None:
        """The variable scan over-approximates; there is no clause to declare."""
        declaration = environment_declaration(
            self._target("3.13.2"), [Marker('os_name == "python_full_version"')]
        )
        assert declaration.endswith('and python_full_version == "3.13.2"')

    def test_a_comparison_against_another_variable_pins_the_value(self) -> None:
        declaration = environment_declaration(
            self._target("3.13.2"), [Marker("python_full_version == python_version")]
        )
        assert declaration.endswith('and python_full_version == "3.13.2"')

    def test_a_prerelease_boundary_pins_the_value(self) -> None:
        """PEP 440 keeps 3.13.0rc1 out of both ``< 3.13.0`` and ``>= 3.13.0``.

        Flipping the operator is not the complement there, so the target
        would not satisfy its own declaration; pin the value instead.
        """
        declaration = environment_declaration(
            self._target("3.13.0rc1"), [Marker('python_full_version < "3.13.0"')]
        )
        assert declaration.endswith('and python_full_version == "3.13.0rc1"')
