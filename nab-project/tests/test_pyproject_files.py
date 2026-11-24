"""Read project, build, group and optional dependency declarations from pyproject.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab_markersets._compat import get_int_max_str_digits
from nab_project.pyproject_files import (
    MissingProjectTableError,
    build_system_requires,
    read_pyproject_build_requires,
    read_pyproject_dependencies,
    read_pyproject_groups,
    read_pyproject_name,
    read_pyproject_optional_dependencies,
)
from nab_provider.requirements_file import (
    InvalidProjectRequirementError,
    InvalidProjectTableError,
)

requires_int_limit = pytest.mark.skipif(
    get_int_max_str_digits() == 0, reason="requires an enabled integer conversion limit"
)

_AT_LIMIT = "1" * (get_int_max_str_digits() or 4300)
_OVERSIZED = _AT_LIMIT + "1"


class TestReadPyprojectDependencies:
    def test_reads_dependencies(self, tmp_path: Path) -> None:
        """Parse [project].dependencies from a valid pyproject.toml."""
        p = tmp_path / "pyproject.toml"
        p.write_text(
            '[project]\ndependencies = ["requests>=2.0", "click"]\n',
        )
        deps = read_pyproject_dependencies(p)
        assert len(deps) == 2
        assert deps[0].name == "requests"
        assert deps[1].name == "click"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Raise FileNotFoundError for missing pyproject.toml."""
        p = tmp_path / "missing.toml"
        with pytest.raises(FileNotFoundError):
            read_pyproject_dependencies(p)

    def test_missing_project_section_raises(self, tmp_path: Path) -> None:
        """Keep missing-table errors distinguishable from other lookup failures."""
        p = tmp_path / "pyproject.toml"
        p.write_text("[build-system]\n")
        with pytest.raises(MissingProjectTableError) as caught:
            read_pyproject_dependencies(p)
        assert isinstance(caught.value, KeyError)
        assert caught.value.args == ("project",)

    def test_missing_dependencies_key_returns_empty(self, tmp_path: Path) -> None:
        """An absent dependencies key reads as no base deps (PEP 621 optional)."""
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\nname = "foo"\n')
        assert read_pyproject_dependencies(p) == []

    def test_missing_dependencies_key_with_extras_returns_empty(
        self, tmp_path: Path
    ) -> None:
        """Deps declared only as an extra leave the base dep set empty."""
        p = tmp_path / "pyproject.toml"
        p.write_text(
            '[project]\nname = "myproj"\nversion = "1.0"\n'
            "[project.optional-dependencies]\n"
            'dev = ["pytest"]\n'
        )
        assert read_pyproject_dependencies(p) == []

    def test_empty_dependencies(self, tmp_path: Path) -> None:
        """Return empty list when dependencies list is empty."""
        p = tmp_path / "pyproject.toml"
        p.write_text("[project]\ndependencies = []\n")
        assert read_pyproject_dependencies(p) == []

    def test_other_dynamic_field_returns_empty(self, tmp_path: Path) -> None:
        """dynamic = ['version'] without a deps key still reads as empty."""
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\nname = "foo"\ndynamic = ["version"]\n')
        assert read_pyproject_dependencies(p) == []

    @pytest.mark.parametrize(
        "declaration", ["", "dependencies = []", 'dependencies = ["requests"]']
    )
    def test_dynamic_dependencies_require_the_backend(
        self, tmp_path: Path, declaration: str
    ) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text(
            f'[project]\nname = "foo"\ndynamic = ["dependencies"]\n{declaration}\n'
        )
        with pytest.raises(InvalidProjectRequirementError, match="declared dynamic"):
            read_pyproject_dependencies(p)

    @pytest.mark.parametrize("dynamic", ['"dependencies"', '["description", 1]'])
    def test_malformed_dynamic_declaration_is_reported(
        self, tmp_path: Path, dynamic: str
    ) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text(f'[project]\ndependencies = ["requests"]\ndynamic = {dynamic}\n')
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"\[project\]\.dynamic must be an array of strings",
        ):
            read_pyproject_dependencies(p)

    def test_malformed_dependency_string_raises(self, tmp_path: Path) -> None:
        """A malformed PEP 508 string raises InvalidProjectRequirementError."""
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\ndependencies = ["requests >= 2.0 extra junk"]\n')
        with pytest.raises(
            InvalidProjectRequirementError, match=r"\[project\].dependencies"
        ):
            read_pyproject_dependencies(p)

    @requires_int_limit
    @pytest.mark.parametrize("operator", ["==", "==="])
    def test_oversized_specifier_version_raises(
        self, tmp_path: Path, operator: str
    ) -> None:
        """An oversized version converts only on comparison; parsing forces it."""
        p = tmp_path / "pyproject.toml"
        p.write_text(f'[project]\ndependencies = ["foo{operator}{_OVERSIZED}"]\n')
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"\[project\].dependencies: Exceeds the limit",
        ):
            read_pyproject_dependencies(p)

    def test_over_nested_marker_raises(
        self, tmp_path: Path, over_nested_marker: str
    ) -> None:
        """A marker the parser cannot recurse through is a rejected dependency."""
        p = tmp_path / "pyproject.toml"
        p.write_text(f'[project]\ndependencies = ["foo ; {over_nested_marker}"]\n')
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"\[project\].dependencies: .*nested",
        ):
            read_pyproject_dependencies(p)

    def test_at_limit_specifier_version_is_read(self, tmp_path: Path) -> None:
        """A run of exactly the limit converts, so it stays a legal dependency."""
        p = tmp_path / "pyproject.toml"
        p.write_text(f'[project]\ndependencies = ["foo=={_AT_LIMIT}"]\n')
        assert [d.name for d in read_pyproject_dependencies(p)] == ["foo"]

    def test_string_dependencies_value_raises(self, tmp_path: Path) -> None:
        """A bare string is rejected, not iterated character by character."""
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\ndependencies = "requests"\n')
        with pytest.raises(
            InvalidProjectRequirementError, match=r"\[project\].dependencies"
        ):
            read_pyproject_dependencies(p)

    def test_non_string_dependency_element_raises(self, tmp_path: Path) -> None:
        """The error identifies the dependency list containing the non-string entry."""
        p = tmp_path / "pyproject.toml"
        p.write_text("[project]\ndependencies = [123]\n")
        with pytest.raises(
            InvalidProjectRequirementError, match=r"\[project\].dependencies"
        ):
            read_pyproject_dependencies(p)

    def test_string_project_table_raises(self, tmp_path: Path) -> None:
        """A [project] that is a string is rejected, not subscripted."""
        p = tmp_path / "pyproject.toml"
        p.write_text('project = "hello"\n')
        with pytest.raises(TypeError, match=r"\[project\] must be a table"):
            read_pyproject_dependencies(p)

    def test_array_project_table_raises(self, tmp_path: Path) -> None:
        """A [project] that is an array is rejected, not subscripted."""
        p = tmp_path / "pyproject.toml"
        p.write_text('project = ["a", "b"]\n')
        with pytest.raises(TypeError, match=r"\[project\] must be a table"):
            read_pyproject_dependencies(p)


class TestReadPyprojectBuildRequires:
    def test_reads_requires(self, tmp_path: Path) -> None:
        """Parse [build-system].requires from a valid pyproject.toml."""
        p = tmp_path / "pyproject.toml"
        p.write_text('[build-system]\nrequires = ["hatchling", "hatch-vcs>=0.4"]\n')
        requires = read_pyproject_build_requires(p)
        assert [str(r) for r in requires] == ["hatchling", "hatch-vcs>=0.4"]

    def test_marker_is_kept(self, tmp_path: Path) -> None:
        """A build requirement may carry a PEP 508 marker."""
        p = tmp_path / "pyproject.toml"
        p.write_text(
            "[build-system]\n"
            'requires = ["setuptools", "tomli; python_version < \'3.11\'"]\n'
        )
        requires = read_pyproject_build_requires(p)
        assert str(requires[1].marker) == 'python_version < "3.11"'

    def test_empty_requires_is_no_build_requirements(self, tmp_path: Path) -> None:
        """An empty array declares a build system that needs nothing."""
        p = tmp_path / "pyproject.toml"
        p.write_text("[build-system]\nrequires = []\n")
        assert read_pyproject_build_requires(p) == []

    def test_missing_build_system_raises(self, tmp_path: Path) -> None:
        """No [build-system] is not the PEP 517 default, it is an error."""
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\nname = "foo"\n')
        with pytest.raises(InvalidProjectRequirementError) as excinfo:
            read_pyproject_build_requires(p)

        assert str(excinfo.value).startswith(f"{p} declares no [build-system]")

    def test_missing_requires_key_raises(self, tmp_path: Path) -> None:
        """PEP 518 makes requires mandatory once [build-system] is present."""
        p = tmp_path / "pyproject.toml"
        p.write_text('[build-system]\nbuild-backend = "flit_core.buildapi"\n')
        with pytest.raises(InvalidProjectRequirementError) as excinfo:
            read_pyproject_build_requires(p)

        assert str(excinfo.value) == (
            f"[build-system].requires is required by PEP 518 and {p}"
            " does not declare it"
        )

    def test_non_table_build_system_raises(self, tmp_path: Path) -> None:
        """A [build-system] that is not a table is malformed, not absent."""
        p = tmp_path / "pyproject.toml"
        p.write_text('build-system = "hatchling"\n')
        with pytest.raises(
            InvalidProjectTableError, match=r"\[build-system\] must be a table"
        ):
            read_pyproject_build_requires(p)

    def test_string_requires_raises(self, tmp_path: Path) -> None:
        """A bare string would iterate character by character."""
        p = tmp_path / "pyproject.toml"
        p.write_text('[build-system]\nrequires = "hatchling"\n')
        with pytest.raises(
            InvalidProjectRequirementError, match="must be an array of strings"
        ):
            read_pyproject_build_requires(p)

    def test_malformed_requirement_string_raises(self, tmp_path: Path) -> None:
        """A malformed PEP 508 string names the table it came from."""
        p = tmp_path / "pyproject.toml"
        p.write_text('[build-system]\nrequires = ["hatchling >= 1 junk"]\n')
        with pytest.raises(
            InvalidProjectRequirementError, match=r"\[build-system\].requires"
        ):
            read_pyproject_build_requires(p)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Raise FileNotFoundError for a missing pyproject.toml."""
        p = tmp_path / "missing.toml"
        with pytest.raises(FileNotFoundError):
            read_pyproject_build_requires(p)

    def test_the_path_names_the_file_the_error_blames(self, tmp_path: Path) -> None:
        """``build_system_requires`` reads the document and blames the path."""
        p = tmp_path / "pyproject.toml"
        with pytest.raises(InvalidProjectRequirementError) as excinfo:
            build_system_requires({"project": {"name": "foo"}}, p)

        assert str(excinfo.value).startswith(f"{p} declares no [build-system]")


class TestReadPyprojectGroups:
    def test_reads_groups_table(self, tmp_path: Path) -> None:
        """Parse [dependency-groups] from a valid pyproject.toml."""
        p = tmp_path / "pyproject.toml"
        p.write_text(
            '[dependency-groups]\ndev = ["pytest", "ruff"]\ndocs = ["sphinx"]\n',
        )
        groups = read_pyproject_groups(p)
        assert set(groups.keys()) == {"dev", "docs"}
        assert list(groups["dev"]) == ["pytest", "ruff"]

    def test_missing_table_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\ndependencies = ["foo"]\n')
        assert read_pyproject_groups(p) == {}

    def test_non_table_value_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text('dependency-groups = "not-a-table"\n')
        with pytest.raises(TypeError, match="must be a table"):
            read_pyproject_groups(p)


class TestReadPyprojectOptionalDependencies:
    def test_dynamic_extra_has_no_complete_dependency_list(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            '[project]\ndependencies=["base"]\ndynamic=["optional-dependencies"]\n'
            '[project.optional-dependencies]\nfast=["listed"]\n',
            encoding="utf-8",
        )
        assert [req.name for req in read_pyproject_dependencies(path)] == ["base"]
        with pytest.raises(
            InvalidProjectRequirementError, match="optional-dependencies.*dynamic"
        ):
            read_pyproject_optional_dependencies(path)

    def test_reads_optional_dependencies(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text(
            "[project]\n"
            'name = "x"\n'
            'version = "1"\n'
            'dependencies = ["foo"]\n'
            "[project.optional-dependencies]\n"
            'cpu = ["torch"]\n'
            'gpu = ["torch[cuda]"]\n',
        )
        opt = read_pyproject_optional_dependencies(p)
        assert set(opt.keys()) == {"cpu", "gpu"}

    def test_missing_table_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\ndependencies = ["foo"]\n')
        assert read_pyproject_optional_dependencies(p) == {}

    def test_non_table_value_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text(
            "[project]\n"
            'name = "x"\n'
            'version = "1"\n'
            'dependencies = ["foo"]\n'
            'optional-dependencies = "not-a-table"\n',
        )
        with pytest.raises(TypeError, match="must be a table"):
            read_pyproject_optional_dependencies(p)

    def test_string_project_table_raises(self, tmp_path: Path) -> None:
        """A [project] that is a string is rejected before the .get."""
        p = tmp_path / "pyproject.toml"
        p.write_text('project = "hello"\n')
        with pytest.raises(TypeError, match=r"\[project\] must be a table"):
            read_pyproject_optional_dependencies(p)


class TestReadPyprojectName:
    def test_reads_name(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\nname = "mypkg"\nversion = "1.0"\n')
        assert read_pyproject_name(p) == "mypkg"

    def test_returns_none_when_project_table_absent(self, tmp_path: Path) -> None:
        """A workspace-root pyproject without a [project] table.

        nab still reads ``[tool.nab]`` and friends from such files, so
        the helper has to tolerate the missing project table.
        """
        p = tmp_path / "pyproject.toml"
        p.write_text('[tool.nab]\nmode = "specific"\n')
        assert read_pyproject_name(p) is None

    def test_returns_none_when_name_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text('[project]\nversion = "1.0"\n')
        assert read_pyproject_name(p) is None

    def test_string_project_table_raises(self, tmp_path: Path) -> None:
        """A [project] that is a string is rejected before the .get."""
        p = tmp_path / "pyproject.toml"
        p.write_text('project = "hello"\n')
        with pytest.raises(TypeError, match=r"\[project\] must be a table"):
            read_pyproject_name(p)
