"""Unit tests for the matrix-expansion logic."""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.markers import Marker, default_environment
from nab_provider.tags import PlatformSpec
from nab_provider.target import (
    KNOWN_PYTHON_MINORS,
    ImplementationOverride,
    Matrix,
    MatrixInclude,
    _pythons_in_range,
    declared_range_marker,
)


class TestPythonsInRange:
    """``_pythons_in_range`` matches PEP 440 specifiers to known minors."""

    def test_lower_bound_inclusive(self) -> None:
        """``>=3.11`` admits 3.11, 3.12, 3.13, ..."""
        assert "3.11" in list(_pythons_in_range(">=3.11"))

    def test_upper_bound_exclusive(self) -> None:
        """``<3.13`` excludes 3.13 itself."""
        out = list(_pythons_in_range(">=3.10, <3.13"))
        assert "3.13" not in out
        assert "3.12" in out

    def test_exact_version_admits_one_minor(self) -> None:
        """``==3.12`` matches exactly one minor."""
        assert list(_pythons_in_range("==3.12")) == ["3.12"]

    def test_unknown_minor_yields_nothing(self) -> None:
        """A spec satisfied by no known minor returns an empty list."""
        assert list(_pythons_in_range(">=4.0")) == []


class TestMatrixExpand:
    """``Matrix.expand`` builds targets and validates inputs."""

    def test_simple_range_expands_to_known_minors(self) -> None:
        """A python range maps to the union of known minors that satisfy it."""
        matrix = Matrix(
            python=">=3.11, <3.13", platforms=(PlatformSpec("linux_x86_64"),)
        )
        tuples = matrix.expand()
        assert [t.python_version for t in tuples] == ["3.11", "3.12"]
        assert all(t.platform_id == "linux_x86_64" for t in tuples)

    def test_range_expands_to_python_315(self) -> None:
        """A range spanning 3.15 includes it, matching the ``--python`` path."""
        matrix = Matrix(
            python=">=3.14, <3.16", platforms=(PlatformSpec("linux_x86_64"),)
        )
        tuples = matrix.expand()
        assert [t.python_version for t in tuples] == ["3.14", "3.15"]

    def test_cross_product_pythons_and_platforms(self) -> None:
        """All (python, platform) pairs are present in the cross-product."""
        matrix = Matrix(
            python=">=3.10, <3.12",
            platforms=(
                PlatformSpec("linux_x86_64"),
                PlatformSpec("macos_arm64"),
                PlatformSpec("windows_amd64"),
            ),
        )
        tuples = matrix.expand()
        expected_count = 6
        assert len(tuples) == expected_count
        pairs = {(t.python_version, t.platform_id) for t in tuples}
        assert pairs == {
            ("3.10", "linux_x86_64"),
            ("3.10", "macos_arm64"),
            ("3.10", "windows_amd64"),
            ("3.11", "linux_x86_64"),
            ("3.11", "macos_arm64"),
            ("3.11", "windows_amd64"),
        }

    def test_environment_models_every_scalar_marker_variable(self) -> None:
        """Every scalar packaging marker variable is set per target.

        A variable the matrix leaves out does not read as missing. The
        provider evaluates through ``Marker.evaluate``, which seeds the host
        ``default_environment()`` beneath the environment it is handed, so the
        host's value answers the marker on every target; the root-requirement
        path evaluates through ``MarkerSet``, which raises instead. Deriving
        the required set from packaging rather than a hardcoded list makes a
        newly added variable fail here.
        """
        matrix = Matrix(python="==3.12", platforms=(PlatformSpec("macos_arm64"),))
        (only,) = matrix.expand()
        missing = set(default_environment()) - set(only.marker_env)
        assert not missing, (
            f"matrix target does not model {sorted(missing)!r}; add each to "
            "declared_environment or the resolve answers it from the host"
        )

    def test_environment_has_expected_axis_values(self) -> None:
        """The python and platform axes drive their marker values."""
        matrix = Matrix(python="==3.12", platforms=(PlatformSpec("macos_arm64"),))
        (only,) = matrix.expand()
        assert only.marker_env["python_version"] == "3.12"
        assert only.marker_env["sys_platform"] == "darwin"
        assert only.marker_env["platform_machine"] == "arm64"

    def test_unknown_platform_id_raises(self) -> None:
        """An unknown platform id is a user error, raised eagerly."""
        matrix = Matrix(python=">=3.11", platforms=(PlatformSpec("freebsd_amd64"),))
        with pytest.raises(ValueError, match="Unknown platform"):
            matrix.expand()

    def test_unknown_platform_id_wins_over_the_free_threaded_rule(self) -> None:
        """A bad id is the error to report; the rest of the target is moot."""
        matrix = Matrix(
            python=">=3.13",
            platforms=(PlatformSpec("freebsd_amd64", free_threaded=True),),
            implementations=("pypy",),
        )
        with pytest.raises(ValueError, match="Unknown platform"):
            matrix.expand()

    def test_empty_python_range_raises(self) -> None:
        """A python spec satisfied by no known minor is a user error."""
        matrix = Matrix(python=">=4.0", platforms=(PlatformSpec("linux_x86_64"),))
        with pytest.raises(ValueError, match="No known Python"):
            matrix.expand()

    def test_python_order_desc_reverses_iteration(self) -> None:
        """``python_order='desc'`` yields tuples in reversed Python order."""
        asc = Matrix(python=">=3.10, <3.13", platforms=(PlatformSpec("linux_x86_64"),))
        desc = Matrix(
            python=">=3.10, <3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
            python_order="desc",
        )
        assert [t.python_version for t in asc.expand()] == ["3.10", "3.11", "3.12"]
        assert [t.python_version for t in desc.expand()] == ["3.12", "3.11", "3.10"]

    def test_invalid_python_order_raises(self) -> None:
        """Anything other than asc/desc is a user error."""
        with pytest.raises(ValueError, match="python_order"):
            Matrix(
                python=">=3.10",
                platforms=(PlatformSpec("linux_x86_64"),),
                python_order="banana",
            ).expand()

    def test_python_patches_override_full_version(self) -> None:
        """``python_patches`` sets ``python_full_version`` per minor."""
        matrix = Matrix(
            python=">=3.11, <3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
            python_patches={"3.11": "3.11.4", "3.12": "3.12.1"},
        )
        tuples = matrix.expand()
        py311 = next(t for t in tuples if t.python_version == "3.11")
        py312 = next(t for t in tuples if t.python_version == "3.12")
        assert py311.marker_env["python_full_version"] == "3.11.4"
        assert py312.marker_env["python_full_version"] == "3.12.1"
        assert py311.marker_env["implementation_version"] == "3.11.4"

    def test_python_patches_default_to_zero(self) -> None:
        """When patches not declared, ``.0`` is used."""
        matrix = Matrix(python="==3.11", platforms=(PlatformSpec("linux_x86_64"),))
        tuples = matrix.expand()
        assert tuples[0].marker_env["python_full_version"] == "3.11.0"

    def test_python_patches_zero_micro_pins_whole(self) -> None:
        """A ``.0`` pin is a concrete micro resolved whole, not a bare minor.

        The two read the same by string, so only the pin's markers tell them
        apart: a whole target declares the micro it resolved at.
        """
        matrix = Matrix(
            python="==3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
            python_patches={"3.13": "3.13.0"},
        )
        target = matrix.expand()[0]
        assert not target.is_minor_interval
        assert 'python_full_version == "3.13.0"' in declared_range_marker(target)

    def test_python_patches_partial_mapping(self) -> None:
        """A partial mapping uses overrides for declared minors only."""
        matrix = Matrix(
            python=">=3.11, <3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
            python_patches={"3.11": "3.11.4"},
        )
        tuples = matrix.expand()
        py311 = next(t for t in tuples if t.python_version == "3.11")
        py312 = next(t for t in tuples if t.python_version == "3.12")
        assert py311.marker_env["python_full_version"] == "3.11.4"
        assert py312.marker_env["python_full_version"] == "3.12.0"

    def test_python_patches_unknown_minor_key_raises(self) -> None:
        """A patches key that is not a known ``major.minor`` is a user error."""
        matrix = Matrix(
            python="==3.11",
            platforms=(PlatformSpec("linux_x86_64"),),
            python_patches={"3.11.0": "3.11.9"},
        )
        with pytest.raises(ValueError, match="python_patches"):
            matrix.expand()

    def test_patch_level_marker_evaluation(self) -> None:
        """Patch-bound markers evaluate against the declared full version."""
        from nab_provider._vendor.packaging.markers import Marker

        matrix = Matrix(
            python="==3.11",
            platforms=(PlatformSpec("linux_x86_64"),),
            python_patches={"3.11": "3.11.5"},
        )
        env = matrix.expand()[0].marker_env
        assert Marker('python_full_version >= "3.11.4"').evaluate(env)
        assert not Marker('python_full_version >= "3.11.6"').evaluate(env)

    def test_platform_release_default_is_empty_string(self) -> None:
        """Without a user declaration, ``platform_release`` is ``""``."""
        matrix = Matrix(python="==3.11", platforms=(PlatformSpec("linux_x86_64"),))
        env = matrix.expand()[0].marker_env
        assert env["platform_release"] == ""
        assert env["platform_version"] == ""

    def test_platform_release_from_spec(self) -> None:
        """A spec-declared ``platform_release`` flows into the environment."""
        spec = PlatformSpec(
            "linux_x86_64", platform_release="5.10.0", platform_version="#1 SMP"
        )
        matrix = Matrix(python="==3.11", platforms=(spec,))
        env = matrix.expand()[0].marker_env
        assert env["platform_release"] == "5.10.0"
        assert env["platform_version"] == "#1 SMP"

    def test_kernel_marker_evaluates_against_declared_release(self) -> None:
        """Kernel-conditioned markers fire when the user declares a target."""
        spec = PlatformSpec("linux_x86_64", platform_release="5.10.0")
        matrix = Matrix(python="==3.11", platforms=(spec,))
        env = matrix.expand()[0].marker_env
        assert Marker('platform_release >= "5.10"').evaluate(env)
        assert not Marker('platform_release >= "6.0"').evaluate(env)

    def test_marker_evaluation_works_against_environment(self) -> None:
        """A real PEP 508 marker should evaluate against the tuple's env."""
        matrix = Matrix(
            python=">=3.11, <3.12",
            platforms=(PlatformSpec("windows_amd64"), PlatformSpec("linux_x86_64")),
        )
        tuples = matrix.expand()
        win_env = next(t.marker_env for t in tuples if t.platform_id == "windows_amd64")
        linux_env = next(
            t.marker_env for t in tuples if t.platform_id == "linux_x86_64"
        )

        win_marker = Marker("sys_platform == 'win32'")
        assert win_marker.evaluate(win_env) is True
        assert win_marker.evaluate(linux_env) is False

        py_marker = Marker('python_version >= "3.10"')
        assert py_marker.evaluate(win_env) is True
        assert py_marker.evaluate(linux_env) is True


class TestImplementationAxis:
    """The implementation axis adds PyPy alongside the default CPython."""

    def test_default_is_cpython_only(self) -> None:
        """Without declaring implementations, every tuple is CPython."""
        matrix = Matrix(python="==3.11", platforms=(PlatformSpec("linux_x86_64"),))
        tuples = matrix.expand()
        assert len(tuples) == 1
        env = tuples[0].marker_env
        assert tuples[0].implementation == "cpython"
        assert env["implementation_name"] == "cpython"
        assert env["platform_python_implementation"] == "CPython"

    def test_count_is_pythons_times_platforms_times_implementations(self) -> None:
        """The implementation axis multiplies the tuple count."""
        matrix = Matrix(
            python=">=3.11, <3.13",
            platforms=(PlatformSpec("linux_x86_64"), PlatformSpec("macos_arm64")),
            implementations=("cpython", "pypy"),
        )
        tuples = matrix.expand()
        assert len(tuples) == 2 * 2 * 2

    def test_pypy_tuple_has_pypy_markers(self) -> None:
        """A PyPy tuple sets the PyPy interpreter-identity markers."""
        matrix = Matrix(
            python="==3.11",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementations=("pypy",),
        )
        env = matrix.expand()[0].marker_env
        assert env["implementation_name"] == "pypy"
        assert env["platform_python_implementation"] == "PyPy"
        assert Marker('platform_python_implementation == "PyPy"').evaluate(env)
        assert not Marker('platform_python_implementation == "CPython"').evaluate(env)

    def test_unknown_implementation_raises(self) -> None:
        """An unknown implementation is a user error, raised eagerly."""
        matrix = Matrix(
            python="==3.11",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementations=("jython",),
        )
        with pytest.raises(ValueError, match="Unknown implementations"):
            matrix.expand()


class TestImplementationOverrides:
    """Implementation bodies replace shared axes without changing global order."""

    @pytest.mark.parametrize(
        ("python_order", "expected"),
        [
            (
                "asc",
                [
                    "py310-macos_arm64",
                    "py310-linux_x86_64",
                    "pp311-macos_arm64",
                    "py311-macos_arm64",
                    "pp311-linux_x86_64",
                    "py311-linux_x86_64",
                    "py312-macos_arm64",
                    "py312-linux_x86_64",
                ],
            ),
            (
                "desc",
                [
                    "py312-macos_arm64",
                    "py312-linux_x86_64",
                    "pp311-macos_arm64",
                    "py311-macos_arm64",
                    "pp311-linux_x86_64",
                    "py311-linux_x86_64",
                    "py310-macos_arm64",
                    "py310-linux_x86_64",
                ],
            ),
        ],
    )
    def test_restricted_matrix_preserves_target_order(
        self, python_order: str, expected: list[str]
    ) -> None:
        matrix = Matrix(
            python=">=3.10,<3.13",
            platforms=(PlatformSpec("macos_arm64"), PlatformSpec("linux_x86_64")),
            python_order=python_order,
            implementations=("pypy", "cpython"),
            implementation_overrides={"pypy": ImplementationOverride(python="==3.11")},
        )
        targets = matrix.expand()
        assert [target.label for target in targets] == expected

        pypy_312 = {
            **targets[0].marker_env,
            "python_version": "3.12",
            "python_full_version": "3.12.0",
            "implementation_version": "3.12.0",
            "implementation_name": "pypy",
            "platform_python_implementation": "PyPy",
        }
        assert not any(
            Marker(target.environment_marker_string).evaluate(pypy_312)
            for target in targets
        )

    def test_python_override_replaces_shared_range(self) -> None:
        matrix = Matrix(
            python=">=3.10,<3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementations=("pypy",),
            implementation_overrides={
                "pypy": ImplementationOverride(python=">=3.9,<3.12")
            },
        )
        assert [target.python_version for target in matrix.expand()] == [
            "3.9",
            "3.10",
            "3.11",
        ]

    def test_explicit_empty_mapping_preserves_default_targets(self) -> None:
        default = Matrix(
            python=">=3.11,<3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
        ).expand()
        empty = Matrix(
            python=">=3.11,<3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementation_overrides={},
        ).expand()
        assert empty == default
        assert all(not target.declares_implementation for target in empty)

    def test_unselected_implementation_range_is_inactive(self) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementation_overrides={"pypy": ImplementationOverride(python="==4.0")},
        )
        (target,) = matrix.expand()
        assert target.python_version == "3.12"
        assert target.implementation == "cpython"
        assert not target.declares_implementation

    def test_unknown_implementation_range_key_is_rejected(self) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementation_overrides={
                "jython": ImplementationOverride(python="==3.12")
            },
        )
        with pytest.raises(ValueError, match="Unknown implementation_overrides"):
            matrix.expand()

    @pytest.mark.parametrize("implementation", ["cpython", "pypy"])
    def test_invalid_range_is_rejected_even_when_unselected(
        self, implementation: str
    ) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementation_overrides={
                implementation: ImplementationOverride(python="banana")
            },
        )
        with pytest.raises(ValueError, match="Invalid specifier"):
            matrix.expand()

    def test_empty_selected_range_is_rejected(self) -> None:
        matrix = Matrix(
            python=">=3.11,<3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementations=("cpython", "pypy"),
            implementation_overrides={"pypy": ImplementationOverride(python="==4.0")},
        )
        with pytest.raises(
            ValueError, match="No known Python versions match implementation 'pypy'"
        ):
            matrix.expand()

    def test_patch_pin_applies_to_each_selected_implementation(self) -> None:
        matrix = Matrix(
            python=">=3.11,<3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementations=("cpython", "pypy"),
            implementation_overrides={"pypy": ImplementationOverride(python="==3.11")},
            python_patches={"3.11": "3.11.4"},
        )
        targets = matrix.expand()
        pinned = [target for target in targets if target.python_version == "3.11"]
        assert [target.implementation for target in pinned] == ["cpython", "pypy"]
        assert all(target.python_full_version == "3.11.4" for target in pinned)
        assert all(not target.is_minor_interval for target in pinned)

        (unpinned,) = [target for target in targets if target.python_version == "3.12"]
        assert unpinned.implementation == "cpython"
        assert unpinned.is_minor_interval

    def test_free_threaded_range_checks_only_selected_minors(self) -> None:
        matrix = Matrix(
            python=">=3.12,<3.15",
            platforms=(PlatformSpec("linux_x86_64", free_threaded=True),),
            implementation_overrides={
                "cpython": ImplementationOverride(python=">=3.13,<3.15")
            },
        )
        assert [target.python_version for target in matrix.expand()] == ["3.13", "3.14"]

    def test_free_threaded_range_still_rejects_old_selected_minor(self) -> None:
        matrix = Matrix(
            python=">=3.12,<3.15",
            platforms=(PlatformSpec("linux_x86_64", free_threaded=True),),
            implementation_overrides={
                "cpython": ImplementationOverride(python="==3.12")
            },
        )
        with pytest.raises(
            ValueError, match="free-threaded platform needs CPython 3.13"
        ):
            matrix.expand()

    def test_free_threaded_range_still_rejects_pypy(self) -> None:
        matrix = Matrix(
            python=">=3.13,<3.15",
            platforms=(PlatformSpec("linux_x86_64", free_threaded=True),),
            implementations=("cpython", "pypy"),
            implementation_overrides={"pypy": ImplementationOverride(python="==3.13")},
        )
        with pytest.raises(
            ValueError, match="free-threaded platform needs CPython, not"
        ):
            matrix.expand()

    @pytest.mark.parametrize(
        ("python_order", "expected"),
        [
            (
                "asc",
                [
                    "pp311-macos_arm64",
                    "pp311-windows_amd64",
                    "py312-linux_x86_64",
                    "py312-macos_arm64",
                ],
            ),
            (
                "desc",
                [
                    "py312-linux_x86_64",
                    "py312-macos_arm64",
                    "pp311-macos_arm64",
                    "pp311-windows_amd64",
                ],
            ),
        ],
    )
    def test_union_versions_and_new_platforms_use_global_order(
        self, python_order: str, expected: list[str]
    ) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(PlatformSpec("linux_x86_64"), PlatformSpec("macos_arm64")),
            python_order=python_order,
            implementations=("pypy", "cpython"),
            implementation_overrides={
                "pypy": ImplementationOverride(
                    python="==3.11",
                    platforms=(
                        PlatformSpec("windows_amd64"),
                        PlatformSpec("macos_arm64"),
                    ),
                ),
            },
        )
        assert [target.label for target in matrix.expand()] == expected

    def test_new_platform_order_follows_selected_implementations(self) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementations=("pypy", "cpython"),
            implementation_overrides={
                "cpython": ImplementationOverride(
                    platforms=(PlatformSpec("linux_aarch64"),)
                ),
                "pypy": ImplementationOverride(
                    platforms=(PlatformSpec("windows_arm64"),)
                ),
            },
        )
        assert [target.label for target in matrix.expand()] == [
            "pp312-windows_arm64",
            "py312-linux_aarch64",
        ]

    def test_platform_override_replaces_shared_platforms_and_knobs(self) -> None:
        shared = PlatformSpec("linux_x86_64", runs_on_libc=(2, 34))
        replacement = PlatformSpec("linux_x86_64", libc="musl", runs_on_libc=(1, 2))
        matrix = Matrix(
            python="==3.11",
            platforms=(shared, PlatformSpec("macos_arm64")),
            implementations=("cpython", "pypy"),
            implementation_overrides={
                "pypy": ImplementationOverride(platforms=(replacement,))
            },
        )
        targets = matrix.expand()
        assert [(target.platform_id, target.implementation) for target in targets] == [
            ("linux_x86_64", "cpython"),
            ("linux_x86_64", "pypy"),
            ("macos_arm64", "cpython"),
        ]
        assert targets[0].platform_spec is shared
        assert targets[1].platform_spec is replacement
        cpython_platforms = {tag.platform for tag in targets[0].tags.ordered}
        pypy_platforms = {tag.platform for tag in targets[1].tags.ordered}
        assert "manylinux_2_34_x86_64" in cpython_platforms
        assert "musllinux_1_2_x86_64" in pypy_platforms
        assert not any(platform.startswith("manylinux") for platform in pypy_platforms)

    def test_empty_body_inherits_both_defaults(self) -> None:
        matrix = Matrix(
            python=">=3.11,<3.13",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementation_overrides={"cpython": ImplementationOverride()},
        )
        assert [target.label for target in matrix.expand()] == [
            "py311-linux_x86_64",
            "py312-linux_x86_64",
        ]

    @pytest.mark.parametrize("implementation", ["cpython", "pypy"])
    def test_unknown_platform_rejected_even_when_unselected(
        self, implementation: str
    ) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementation_overrides={
                implementation: ImplementationOverride(
                    platforms=(PlatformSpec("unknown"),)
                )
            },
        )
        with pytest.raises(ValueError, match="Unknown platform id 'unknown'"):
            matrix.expand()

    @pytest.mark.parametrize("implementation", ["cpython", "pypy"])
    def test_duplicate_override_platform_ids_rejected(
        self, implementation: str
    ) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementation_overrides={
                implementation: ImplementationOverride(
                    platforms=(
                        PlatformSpec("linux_x86_64"),
                        PlatformSpec("linux_x86_64", libc="musl"),
                    )
                )
            },
        )
        with pytest.raises(ValueError, match="Duplicate platform id 'linux_x86_64'"):
            matrix.expand()

    @pytest.mark.parametrize("implementation", ["cpython", "pypy"])
    def test_empty_platform_override_is_invalid_even_when_inactive(
        self, implementation: str
    ) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementation_overrides={
                implementation: ImplementationOverride(platforms=())
            },
        )
        with pytest.raises(ValueError, match="must list at least one platform"):
            matrix.expand()

    def test_empty_inherited_platforms_cannot_supply_an_override(
        self,
    ) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(),
            implementation_overrides={"cpython": ImplementationOverride()},
        )
        with pytest.raises(ValueError, match="must list at least one platform"):
            matrix.expand()

    def test_shared_duplicate_platform_ids_preserve_full_spec_order(self) -> None:
        matrix = Matrix(
            python="==3.11",
            platforms=(
                PlatformSpec("linux_x86_64"),
                PlatformSpec("linux_x86_64", libc="musl"),
            ),
            implementations=("pypy", "cpython"),
        )
        assert [target.label for target in matrix.expand()] == [
            "pp311-linux_x86_64",
            "py311-linux_x86_64",
            "pp311-linux_x86_64-musl",
            "py311-linux_x86_64-musl",
        ]

    def test_shared_free_threaded_error_precedes_empty_python_range(self) -> None:
        matrix = Matrix(
            python="==4.0",
            platforms=(PlatformSpec("linux_x86_64", free_threaded=True),),
            implementations=("pypy",),
        )
        with pytest.raises(
            ValueError, match="free-threaded platform needs CPython, not"
        ):
            matrix.expand()

    def test_unused_platform_and_python_replacements_are_inactive(self) -> None:
        matrix = Matrix(
            python="==3.12",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementation_overrides={
                "pypy": ImplementationOverride(
                    python="==4.0", platforms=(PlatformSpec("windows_amd64"),)
                )
            },
        )
        assert [target.label for target in matrix.expand()] == ["py312-linux_x86_64"]

    def test_overridden_defaults_need_not_select_known_versions(self) -> None:
        matrix = Matrix(
            python="==4.0",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementation_overrides={
                "cpython": ImplementationOverride(python="==3.12")
            },
        )
        assert [target.label for target in matrix.expand()] == ["py312-linux_x86_64"]

    def test_free_threaded_and_pypy_use_independent_platform_specs(self) -> None:
        threaded = PlatformSpec("linux_x86_64", free_threaded=True)
        ordinary = PlatformSpec("linux_x86_64")
        matrix = Matrix(
            python="==3.13",
            platforms=(threaded,),
            implementations=("cpython", "pypy"),
            implementation_overrides={
                "pypy": ImplementationOverride(python="==3.11", platforms=(ordinary,))
            },
        )
        pypy, cpython = matrix.expand()
        assert pypy.python_version == "3.11"
        assert pypy.platform_spec is ordinary
        assert cpython.python_version == "3.13"
        assert cpython.platform_spec is threaded
        assert "cp313t" in {tag.abi for tag in cpython.tags.ordered}
        for abi in ("pp73", "pp80"):
            filename = f"demo-1.0-pp311-pypy311_{abi}-manylinux_2_17_x86_64.whl"
            assert pypy.tags.accepts(filename)
            assert not cpython.tags.accepts(filename)
        assert not Marker(cpython.environment_marker_string).evaluate(pypy.marker_env)
        assert not Marker(pypy.environment_marker_string).evaluate(cpython.marker_env)


class TestMatrixIncludes:
    def test_include_inherits_the_selected_implementation_override(self) -> None:
        targets = Matrix(
            "==3.15",
            (PlatformSpec("linux_x86_64"),),
            implementation_overrides={
                "pypy": ImplementationOverride(
                    python="==3.11", platforms=(PlatformSpec("windows_amd64"),)
                )
            },
            include=(MatrixInclude(implementations=("pypy",)),),
        ).expand()
        assert [target.label for target in targets] == [
            "pp311-windows_amd64",
            "py315-linux_x86_64",
        ]

    def test_include_explicit_axes_replace_implementation_defaults(self) -> None:
        targets = Matrix(
            "==3.15",
            (PlatformSpec("linux_x86_64"),),
            implementations=("cpython", "pypy"),
            implementation_overrides={
                "pypy": ImplementationOverride(
                    python="==3.11", platforms=(PlatformSpec("windows_amd64"),)
                )
            },
            include=(
                MatrixInclude(
                    python="==3.12",
                    platforms=(PlatformSpec("macos_arm64"),),
                    implementations=("pypy",),
                    free_threaded=False,
                ),
            ),
        ).expand()
        assert [target.label for target in targets] == [
            "pp311-windows_amd64",
            "pp312-macos_arm64",
            "py315-linux_x86_64",
        ]

    def test_include_false_overrides_an_implementation_feature(self) -> None:
        targets = Matrix(
            "==3.15",
            (PlatformSpec("linux_x86_64"),),
            implementation_overrides={
                "cpython": ImplementationOverride(free_threaded=True)
            },
            include=(MatrixInclude(free_threaded=False),),
        ).expand()
        assert [target.label for target in targets] == [
            "py315-linux_x86_64-ft",
            "py315-linux_x86_64",
        ]

    @pytest.mark.parametrize("python_order", ["asc", "desc"])
    def test_sparse_groups_keep_builds_and_implementations_independent(
        self, python_order: str
    ) -> None:
        matrix = Matrix(
            ">=3.10,<3.16",
            (PlatformSpec("linux_x86_64"),),
            python_order=python_order,
            python_patches={"3.15": "3.15.1"},
            include=(
                MatrixInclude(python=">=3.15,<3.16", free_threaded=True),
                MatrixInclude(python="==3.11", implementations=("pypy",)),
            ),
        )
        targets = matrix.expand()
        expected = [
            "py310-linux_x86_64",
            "py311-linux_x86_64",
            "pp311-linux_x86_64",
            "py312-linux_x86_64",
            "py313-linux_x86_64",
            "py314-linux_x86_64",
            "py315-linux_x86_64",
            "py315-linux_x86_64-ft",
        ]
        if python_order == "desc":
            expected = [
                *expected[6:],
                expected[5],
                expected[4],
                expected[3],
                *expected[1:3],
                expected[0],
            ]
        assert [target.label for target in targets] == expected

        ordinary = next(
            target for target in targets if target.label == "py315-linux_x86_64"
        )
        threaded = next(
            target for target in targets if target.label == "py315-linux_x86_64-ft"
        )
        assert ordinary.marker_env == threaded.marker_env
        assert ordinary.python_full_version == threaded.python_full_version == "3.15.1"
        for target, native_abi in [(ordinary, "cp315"), (threaded, "cp315t")]:
            for abi in ("cp315", "cp315t"):
                assert target.tags.accepts(
                    f"example-1-cp315-{abi}-manylinux_2_17_x86_64.whl"
                ) is (abi == native_abi)
            assert target.tags.accepts("example-1-py3-none-any.whl")
        assert not threaded.tags.accepts(
            "example-1-cp310-abi3-manylinux_2_17_x86_64.whl"
        )

        pypy = next(target for target in targets if target.implementation == "pypy")
        assert pypy.python_version == "3.11"
        assert not Marker(pypy.environment_marker_string).evaluate(ordinary.marker_env)
        assert not Marker(ordinary.environment_marker_string).evaluate(pypy.marker_env)

    def test_explicit_axes_replace_inherited_defaults(self) -> None:
        replacement = PlatformSpec("linux_x86_64", libc="musl", runs_on_libc=(1, 2))
        targets = Matrix(
            "==3.12",
            (PlatformSpec("windows_amd64"),),
            include=(
                MatrixInclude(
                    python="==3.11", platforms=(replacement,), implementations=("pypy",)
                ),
            ),
        ).expand()
        assert [target.label for target in targets] == [
            "pp311-linux_x86_64-musl1.2",
            "py312-windows_amd64",
        ]
        assert targets[0].platform_spec is replacement
        assert targets[0].tags.accepts(
            "example-1-pp311-pypy311_pp73-musllinux_1_2_x86_64.whl"
        )
        assert not targets[0].tags.accepts(
            "example-1-pp311-pypy311_pp73-manylinux_2_17_x86_64.whl"
        )

    def test_overlaps_merge_exact_coordinates_only(self) -> None:
        original = PlatformSpec("linux_x86_64")
        newer = PlatformSpec("linux_x86_64", runs_on_libc=(2, 34))
        targets = Matrix(
            ">=3.11,<3.13",
            (original,),
            include=(
                MatrixInclude(),
                MatrixInclude(python="==3.11"),
                MatrixInclude(python="==3.11", platforms=(newer,)),
            ),
        ).expand()
        assert [target.label for target in targets] == [
            "py311-linux_x86_64",
            "py311-linux_x86_64-glibc2.34",
            "py312-linux_x86_64",
        ]
        assert targets[0].platform_spec is original
        assert targets[1].platform_spec is newer

    def test_false_feature_flag_replaces_an_inherited_true(self) -> None:
        targets = Matrix(
            "==3.15",
            (PlatformSpec("windows_amd64"),),
            free_threaded=True,
            include=(MatrixInclude(), MatrixInclude(free_threaded=False)),
        ).expand()
        assert [target.label for target in targets] == [
            "py315-windows_amd64-ft",
            "py315-windows_amd64",
        ]
        assert targets[0].tags.accepts("example-1-cp315-cp315t-win_amd64.whl")
        assert targets[1].tags.accepts("example-1-cp315-cp315-win_amd64.whl")

    def test_inherited_platform_settings_remain_per_entry(self) -> None:
        spec = PlatformSpec("linux_x86_64", libc="musl", runs_on_libc=(1, 2))
        targets = Matrix(
            "==3.15", (spec,), include=(MatrixInclude(free_threaded=True),)
        ).expand()
        assert targets[0].platform_spec is spec
        assert targets[1].tags.accepts(
            "example-1-cp315-cp315t-musllinux_1_2_x86_64.whl"
        )
        assert not targets[1].tags.accepts(
            "example-1-cp315-cp315t-manylinux_2_17_x86_64.whl"
        )

    def test_explicit_feature_overrides_the_platform_build_setting(self) -> None:
        spec = PlatformSpec("linux_x86_64", free_threaded=True)
        targets = Matrix("==3.15", (spec,), free_threaded=False).expand()
        assert [target.label for target in targets] == ["py315-linux_x86_64"]

    @pytest.mark.parametrize(
        ("entry", "message"),
        [
            (MatrixInclude(python="==4.0"), "No known Python versions"),
            (MatrixInclude(platforms=()), "at least one platform"),
            (
                MatrixInclude(platforms=(PlatformSpec("unknown"),)),
                "Unknown platform ids",
            ),
            (MatrixInclude(implementations=()), "at least one implementation"),
            (MatrixInclude(implementations=("unknown",)), "Unknown implementations"),
            (
                MatrixInclude(python="==3.12", free_threaded=True),
                "free-threaded platform needs CPython",
            ),
            (
                MatrixInclude(implementations=("pypy",), free_threaded=True),
                "free-threaded platform needs CPython",
            ),
        ],
    )
    def test_each_entry_must_select_supported_targets(
        self, entry: MatrixInclude, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message) as exc:
            Matrix(
                "==3.15", (PlatformSpec("windows_amd64"),), include=(entry,)
            ).expand()
        assert "matrix.include[0]" in str(exc.value)

    def test_empty_base_implementation_axis_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one implementation"):
            Matrix(
                "==3.15", (PlatformSpec("windows_amd64"),), implementations=()
            ).expand()

    def test_unsupported_build_error_precedes_an_empty_python_range(self) -> None:
        with pytest.raises(
            ValueError, match="free-threaded platform needs CPython, not"
        ):
            Matrix(
                "==4.0",
                (PlatformSpec("linux_x86_64", free_threaded=True),),
                implementations=("pypy",),
            ).expand()

    def test_default_full_platform_spec_order_is_preserved(self) -> None:
        targets = Matrix(
            "==3.11",
            (PlatformSpec("linux_x86_64"), PlatformSpec("linux_x86_64", libc="musl")),
            implementations=("pypy", "cpython"),
        ).expand()
        assert [target.label for target in targets] == [
            "pp311-linux_x86_64",
            "py311-linux_x86_64",
            "pp311-linux_x86_64-musl",
            "py311-linux_x86_64-musl",
        ]


class TestTargetLabels:
    """Expansion gives each target a distinct label."""

    def test_duplicate_platform_id_specs_get_distinct_labels(self) -> None:
        """Two specs sharing a platform_id but differing in a knob stay distinct.

        The default spec keeps the bare label; a declared libc family or
        version gets a discriminator suffix so the tuples do not collapse.
        """
        matrix = Matrix(
            python="==3.11",
            platforms=(
                PlatformSpec("linux_x86_64"),
                PlatformSpec("linux_x86_64", runs_on_libc=(2, 34)),
                PlatformSpec("linux_x86_64", libc="musl"),
            ),
        )
        labels = [t.label for t in matrix.expand()]
        assert labels == [
            "py311-linux_x86_64",
            "py311-linux_x86_64-glibc2.34",
            "py311-linux_x86_64-musl",
        ]

    def test_multi_implementation_cpython_marker_excludes_pypy(self) -> None:
        """In a multi-implementation matrix the CPython tuple constrains
        ``implementation_name`` so it no longer matches a PyPy environment."""
        matrix = Matrix(
            python="==3.11",
            platforms=(PlatformSpec("linux_x86_64"),),
            implementations=("cpython", "pypy"),
        )
        tuples = matrix.expand()
        cpython = next(t for t in tuples if t.implementation == "cpython")
        pypy = next(t for t in tuples if t.implementation == "pypy")
        assert cpython.marker_string.endswith('and implementation_name == "cpython"')
        assert not Marker(cpython.marker_string).evaluate(pypy.marker_env)
        assert not Marker(pypy.marker_string).evaluate(cpython.marker_env)


class TestKnownConstants:
    """Coverage for the module-level lookup tables."""

    def test_known_python_minors_are_sorted(self) -> None:
        """Stability matters because callers rely on iteration order."""
        as_versions = [
            tuple(int(part) for part in m.split(".")) for m in KNOWN_PYTHON_MINORS
        ]
        assert as_versions == sorted(as_versions)
