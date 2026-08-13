"""Unit tests for the matrix-expansion logic."""

from __future__ import annotations

import pytest

from nab_provider._vendor.packaging.markers import Marker, default_environment
from nab_provider.tags import PlatformSpec
from nab_provider.target import (
    KNOWN_PYTHON_MINORS,
    Matrix,
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
