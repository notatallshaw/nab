"""Unit tests for the matrix-expansion logic.

Run with ``.venv/bin/python -m pytest examples/universal/test_matrix.py``
from the ``nab/`` directory.
"""

from __future__ import annotations

import pytest

from nab_python._vendor.packaging.markers import Marker
from nab_python.universal.matrix import (
    _IMPLEMENTATION_DEFAULTS,
    _KNOWN_PYTHON_MINORS,
    _PLATFORM_DEFAULTS,
    Matrix,
    MatrixTuple,
    _pythons_in_range,
)
from nab_python.universal.wheel_selection import PlatformSpec


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
    """``Matrix.expand`` builds tuples and validates inputs."""

    def test_simple_range_expands_to_known_minors(self) -> None:
        """A python range maps to the union of known minors that satisfy it."""
        matrix = Matrix(python=">=3.11, <3.13", platforms=("linux_x86_64",))
        tuples = matrix.expand()
        assert [t.python_version for t in tuples] == ["3.11", "3.12"]
        assert all(t.platform_id == "linux_x86_64" for t in tuples)

    def test_cross_product_pythons_and_platforms(self) -> None:
        """All (python, platform) pairs are present in the cross-product."""
        matrix = Matrix(
            python=">=3.10, <3.12",
            platforms=("linux_x86_64", "macos_arm64", "windows_amd64"),
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

    def test_environment_has_all_required_pep508_keys(self) -> None:
        """Every PEP 508 marker variable must be set per tuple."""
        matrix = Matrix(python="==3.12", platforms=("macos_arm64",))
        tuples = matrix.expand()
        assert len(tuples) == 1
        env = tuples[0].environment
        required_keys = {
            "python_version",
            "python_full_version",
            "implementation_name",
            "implementation_version",
            "os_name",
            "platform_machine",
            "platform_python_implementation",
            "platform_release",
            "platform_system",
            "platform_version",
            "sys_platform",
        }
        assert required_keys.issubset(env)
        assert env["python_version"] == "3.12"
        assert env["sys_platform"] == "darwin"
        assert env["platform_machine"] == "arm64"

    def test_unknown_platform_id_raises(self) -> None:
        """An unknown platform id is a user error, raised eagerly."""
        matrix = Matrix(python=">=3.11", platforms=("freebsd_amd64",))
        with pytest.raises(ValueError, match="Unknown platform"):
            matrix.expand()

    def test_empty_python_range_raises(self) -> None:
        """A python spec satisfied by no known minor is a user error."""
        matrix = Matrix(python=">=4.0", platforms=("linux_x86_64",))
        with pytest.raises(ValueError, match="No known Python"):
            matrix.expand()

    def test_python_order_desc_reverses_iteration(self) -> None:
        """``python_order='desc'`` yields tuples in reversed Python order."""
        asc = Matrix(python=">=3.10, <3.13", platforms=("linux_x86_64",))
        desc = Matrix(
            python=">=3.10, <3.13",
            platforms=("linux_x86_64",),
            python_order="desc",
        )
        assert [t.python_version for t in asc.expand()] == ["3.10", "3.11", "3.12"]
        assert [t.python_version for t in desc.expand()] == ["3.12", "3.11", "3.10"]

    def test_invalid_python_order_raises(self) -> None:
        """Anything other than asc/desc is a user error."""
        with pytest.raises(ValueError, match="python_order"):
            Matrix(
                python=">=3.10",
                platforms=("linux_x86_64",),
                python_order="banana",
            ).expand()

    def test_python_patches_override_full_version(self) -> None:
        """``python_patches`` sets ``python_full_version`` per minor."""
        matrix = Matrix(
            python=">=3.11, <3.13",
            platforms=("linux_x86_64",),
            python_patches={"3.11": "3.11.4", "3.12": "3.12.1"},
        )
        tuples = matrix.expand()
        py311 = next(t for t in tuples if t.python_version == "3.11")
        py312 = next(t for t in tuples if t.python_version == "3.12")
        assert py311.environment["python_full_version"] == "3.11.4"
        assert py312.environment["python_full_version"] == "3.12.1"
        assert py311.environment["implementation_version"] == "3.11.4"

    def test_python_patches_default_to_zero(self) -> None:
        """When patches not declared, ``.0`` is used."""
        matrix = Matrix(python="==3.11", platforms=("linux_x86_64",))
        tuples = matrix.expand()
        assert tuples[0].environment["python_full_version"] == "3.11.0"

    def test_python_patches_partial_mapping(self) -> None:
        """A partial mapping uses overrides for declared minors only."""
        matrix = Matrix(
            python=">=3.11, <3.13",
            platforms=("linux_x86_64",),
            python_patches={"3.11": "3.11.4"},
        )
        tuples = matrix.expand()
        py311 = next(t for t in tuples if t.python_version == "3.11")
        py312 = next(t for t in tuples if t.python_version == "3.12")
        assert py311.environment["python_full_version"] == "3.11.4"
        assert py312.environment["python_full_version"] == "3.12.0"

    def test_patch_level_marker_evaluation(self) -> None:
        """Patch-bound markers evaluate against the declared full version."""
        from nab_python._vendor.packaging.markers import Marker

        matrix = Matrix(
            python="==3.11",
            platforms=("linux_x86_64",),
            python_patches={"3.11": "3.11.5"},
        )
        env = matrix.expand()[0].environment
        assert Marker('python_full_version >= "3.11.4"').evaluate(env)
        assert not Marker('python_full_version >= "3.11.6"').evaluate(env)

    def test_platform_release_default_is_empty_string(self) -> None:
        """Without a user declaration, ``platform_release`` is ``""``."""
        matrix = Matrix(python="==3.11", platforms=("linux_x86_64",))
        env = matrix.expand()[0].environment
        assert env["platform_release"] == ""
        assert env["platform_version"] == ""

    def test_platform_release_from_spec(self) -> None:
        """A spec-declared ``platform_release`` flows into the environment."""
        spec = PlatformSpec(
            "linux_x86_64", platform_release="5.10.0", platform_version="#1 SMP"
        )
        matrix = Matrix(python="==3.11", platforms=(spec,))
        env = matrix.expand()[0].environment
        assert env["platform_release"] == "5.10.0"
        assert env["platform_version"] == "#1 SMP"

    def test_kernel_marker_evaluates_against_declared_release(self) -> None:
        """Kernel-conditioned markers fire when the user declares a target."""
        spec = PlatformSpec("linux_x86_64", platform_release="5.10.0")
        matrix = Matrix(python="==3.11", platforms=(spec,))
        env = matrix.expand()[0].environment
        assert Marker('platform_release >= "5.10"').evaluate(env)
        assert not Marker('platform_release >= "6.0"').evaluate(env)

    def test_marker_evaluation_works_against_environment(self) -> None:
        """A real PEP 508 marker should evaluate against the tuple's env."""
        matrix = Matrix(
            python=">=3.11, <3.12",
            platforms=("windows_amd64", "linux_x86_64"),
        )
        tuples = matrix.expand()
        win_env = next(
            t.environment for t in tuples if t.platform_id == "windows_amd64"
        )
        linux_env = next(
            t.environment for t in tuples if t.platform_id == "linux_x86_64"
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
        matrix = Matrix(python="==3.11", platforms=("linux_x86_64",))
        tuples = matrix.expand()
        assert len(tuples) == 1
        env = tuples[0].environment
        assert tuples[0].implementation == "cpython"
        assert env["implementation_name"] == "cpython"
        assert env["platform_python_implementation"] == "CPython"

    def test_count_is_pythons_times_platforms_times_implementations(self) -> None:
        """The implementation axis multiplies the tuple count."""
        matrix = Matrix(
            python=">=3.11, <3.13",
            platforms=("linux_x86_64", "macos_arm64"),
            implementations=("cpython", "pypy"),
        )
        tuples = matrix.expand()
        assert len(tuples) == 2 * 2 * 2

    def test_pypy_tuple_has_pypy_markers(self) -> None:
        """A PyPy tuple sets the PyPy interpreter-identity markers."""
        matrix = Matrix(
            python="==3.11",
            platforms=("linux_x86_64",),
            implementations=("pypy",),
        )
        env = matrix.expand()[0].environment
        assert env["implementation_name"] == "pypy"
        assert env["platform_python_implementation"] == "PyPy"
        assert Marker('platform_python_implementation == "PyPy"').evaluate(env)
        assert not Marker('platform_python_implementation == "CPython"').evaluate(env)

    def test_unknown_implementation_raises(self) -> None:
        """An unknown implementation is a user error, raised eagerly."""
        matrix = Matrix(
            python="==3.11",
            platforms=("linux_x86_64",),
            implementations=("jython",),
        )
        with pytest.raises(ValueError, match="Unknown implementations"):
            matrix.expand()


class TestMatrixTuple:
    """``MatrixTuple`` is a lightweight value object."""

    def test_label_is_short_and_stable(self) -> None:
        """``label`` is a short id suitable for filenames and logs."""
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={},
        )
        assert t.label == "py311-linux_x86_64"

    def test_pypy_label_uses_pp_prefix(self) -> None:
        """A PyPy tuple's label uses the ``pp`` interpreter prefix."""
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={},
            implementation="pypy",
        )
        assert t.label == "pp311-linux_x86_64"

    def test_selection_appends_member_suffix_to_label(self) -> None:
        """A conflict-fork selection appends sorted ``kind-name`` members."""
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={},
            selection=(("group", "isort5"), ("group", "black22")),
        )
        assert t.label == "py311-linux_x86_64-group-black22.group-isort5"

    def test_mixed_extra_and_group_selection_label_format(self) -> None:
        """Mixed selections sort ``extra-`` before ``group-`` lexically.

        Pins the exact byte-stable label shape so renaming
        :data:`KIND_EXTRA` / :data:`KIND_GROUP` cannot silently flip the
        sort order and rewrite every label dict key.
        """
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={},
            selection=(("group", "isort5"), ("extra", "cpu")),
        )
        assert t.label == "py311-linux_x86_64-extra-cpu.group-isort5"

    def test_label_distinguishes_selections_that_split_on_hyphen(self) -> None:
        """Names containing ``-`` cannot collide two selections into one label.

        Canonical names collapse ``[-_.]`` runs to a single ``-``, so a
        ``-`` joiner is ambiguous: ``a-b`` plus ``c`` and ``a`` plus
        ``b-c`` would both read as ``a-b-c``.  The ``.`` separator keeps
        the two selections on distinct labels.
        """
        first = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={},
            selection=(("extra", "a-b"), ("extra", "c")),
        )
        second = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={},
            selection=(("extra", "a"), ("extra", "b-c")),
        )
        assert first.label != second.label

    def test_label_distinguishes_extra_from_group_of_same_name(self) -> None:
        """An extra and a group of the same name get distinct labels."""
        as_extra = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={},
            selection=(("extra", "cpu"),),
        )
        as_group = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={},
            selection=(("group", "cpu"),),
        )
        assert as_extra.label != as_group.label

    def test_extra_selection_adds_extras_marker_clause(self) -> None:
        """An extra member adds a bare ``in extras`` clause to the marker."""
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={
                "sys_platform": "linux",
                "platform_machine": "x86_64",
                "implementation_name": "cpython",
            },
            selection=(("extra", "cpu"),),
        )
        assert t.marker_string.endswith('and "cpu" in extras')

    def test_group_selection_adds_dependency_groups_clause(self) -> None:
        """A group member adds a bare ``in dependency_groups`` clause."""
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={
                "sys_platform": "linux",
                "platform_machine": "x86_64",
                "implementation_name": "cpython",
            },
            selection=(("group", "black22"),),
        )
        assert t.marker_string.endswith('and "black22" in dependency_groups')

    def test_selection_clauses_are_sorted(self) -> None:
        """Selection clauses emit in sorted order for byte-stable output."""
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={
                "sys_platform": "linux",
                "platform_machine": "x86_64",
                "implementation_name": "cpython",
            },
            selection=(("group", "isort5"), ("extra", "cpu")),
        )
        # sorted by (kind, name): ("extra", "cpu") < ("group", "isort5")
        assert t.marker_string.endswith(
            'and "cpu" in extras and "isort5" in dependency_groups'
        )

    def test_environment_marker_string_omits_selection(self) -> None:
        """The env-only marker drops the conflict membership clause."""
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={
                "sys_platform": "linux",
                "platform_machine": "x86_64",
                "implementation_name": "cpython",
            },
            selection=(("group", "black22"),),
        )
        assert "in dependency_groups" not in t.environment_marker_string
        assert t.environment_marker_string.endswith('platform_machine == "x86_64"')
        # The full per-package marker still carries the membership clause.
        assert '"black22" in dependency_groups' in t.marker_string

    def test_empty_selection_leaves_marker_and_label_unchanged(self) -> None:
        """The default empty selection is a no-op (back-compat)."""
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={
                "sys_platform": "linux",
                "platform_machine": "x86_64",
                "implementation_name": "cpython",
            },
        )
        assert t.label == "py311-linux_x86_64"
        assert "in extras" not in t.marker_string
        assert "in dependency_groups" not in t.marker_string

    def test_duplicate_platform_id_specs_get_distinct_labels(self) -> None:
        """Two specs sharing a platform_id but differing in a floor stay distinct.

        The default-floor spec keeps the bare label; a non-default floor
        gets a discriminator suffix so the tuples do not collapse.
        """
        matrix = Matrix(
            python="==3.11",
            platforms=(
                PlatformSpec("linux_x86_64", manylinux_floor=(2, 17)),
                PlatformSpec("linux_x86_64", manylinux_floor=(2, 34)),
            ),
        )
        labels = [t.label for t in matrix.expand()]
        assert len(set(labels)) == 2
        assert "py311-linux_x86_64" in labels

    def test_cpython_marker_omits_implementation(self) -> None:
        """The default CPython marker keeps its three-clause form."""
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={
                "sys_platform": "linux",
                "platform_machine": "x86_64",
                "implementation_name": "cpython",
            },
        )
        assert "implementation_name" not in t.marker_string

    def test_pypy_marker_constrains_implementation(self) -> None:
        """A PyPy marker adds an ``implementation_name`` clause."""
        t = MatrixTuple(
            python_version="3.11",
            platform_id="linux_x86_64",
            environment={
                "sys_platform": "linux",
                "platform_machine": "x86_64",
                "implementation_name": "pypy",
            },
            implementation="pypy",
        )
        assert t.marker_string.endswith('and implementation_name == "pypy"')

    def test_multi_implementation_cpython_marker_excludes_pypy(self) -> None:
        """In a multi-implementation matrix the CPython tuple constrains
        ``implementation_name`` so it no longer matches a PyPy environment."""
        matrix = Matrix(
            python="==3.11",
            platforms=("linux_x86_64",),
            implementations=("cpython", "pypy"),
        )
        tuples = matrix.expand()
        cpython = next(t for t in tuples if t.implementation == "cpython")
        pypy = next(t for t in tuples if t.implementation == "pypy")
        assert cpython.marker_string.endswith('and implementation_name == "cpython"')
        assert not Marker(cpython.marker_string).evaluate(pypy.environment)
        assert not Marker(pypy.marker_string).evaluate(cpython.environment)


class TestKnownConstants:
    """Coverage for the module-level lookup tables."""

    def test_known_python_minors_are_sorted(self) -> None:
        """Stability matters because callers rely on iteration order."""
        as_versions = [
            tuple(int(part) for part in m.split(".")) for m in _KNOWN_PYTHON_MINORS
        ]
        assert as_versions == sorted(as_versions)

    def test_each_implementation_default_has_interpreter_markers(self) -> None:
        """Every implementation default sets the two interpreter markers."""
        required = {"platform_python_implementation", "implementation_name"}
        for impl, env in _IMPLEMENTATION_DEFAULTS.items():
            assert required == env.keys(), impl

    def test_each_platform_default_has_required_keys(self) -> None:
        """Every platform default must define the OS/arch markers."""
        required = {
            "sys_platform",
            "platform_system",
            "platform_machine",
            "os_name",
        }
        for platform_id, env in _PLATFORM_DEFAULTS.items():
            missing = required - env.keys()
            assert not missing, f"{platform_id} missing {missing}"
