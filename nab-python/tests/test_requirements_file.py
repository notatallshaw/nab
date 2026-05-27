"""Tests for reading dependencies from pyproject.toml files."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python.requirements_file import (
    InvalidProjectRequirementError,
    expand_self_extras,
    raise_for_unsatisfiable,
    read_pyproject_dependencies,
    read_pyproject_groups,
    read_pyproject_name,
    read_pyproject_optional_dependencies,
    resolve_groups_to_requirements,
    select_optional_dependencies,
)
from nab_resolver.errors import ResolutionError


class TestReadPyprojectDependencies:
    def test_reads_dependencies(self, tmp_path: object) -> None:
        """Parse [project].dependencies from a valid pyproject.toml."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text(
            '[project]\ndependencies = ["requests>=2.0", "click"]\n',
        )
        deps = read_pyproject_dependencies(p)
        assert len(deps) == 2
        assert deps[0].name == "requests"
        assert deps[1].name == "click"

    def test_missing_file_raises(self, tmp_path: object) -> None:
        """Raise FileNotFoundError for missing pyproject.toml."""
        p = Path(str(tmp_path)) / "missing.toml"
        with pytest.raises(FileNotFoundError):
            read_pyproject_dependencies(p)

    def test_missing_project_section_raises(self, tmp_path: object) -> None:
        """Raise KeyError when [project] section is missing."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text("[build-system]\n")
        with pytest.raises(KeyError):
            read_pyproject_dependencies(p)

    def test_missing_dependencies_key_raises(self, tmp_path: object) -> None:
        """Raise KeyError when dependencies key is missing."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[project]\nname = "foo"\n')
        with pytest.raises(KeyError):
            read_pyproject_dependencies(p)

    def test_empty_dependencies(self, tmp_path: object) -> None:
        """Return empty list when dependencies list is empty."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text("[project]\ndependencies = []\n")
        assert read_pyproject_dependencies(p) == []

    def test_malformed_dependency_string_raises(self, tmp_path: object) -> None:
        """A malformed PEP 508 string raises InvalidProjectRequirementError."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[project]\ndependencies = ["requests >= 2.0 extra junk"]\n')
        with pytest.raises(
            InvalidProjectRequirementError, match=r"\[project\].dependencies"
        ):
            read_pyproject_dependencies(p)


class TestReadPyprojectGroups:
    def test_reads_groups_table(self, tmp_path: object) -> None:
        """Parse [dependency-groups] from a valid pyproject.toml."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text(
            '[dependency-groups]\ndev = ["pytest", "ruff"]\ndocs = ["sphinx"]\n',
        )
        groups = read_pyproject_groups(p)
        assert set(groups.keys()) == {"dev", "docs"}
        assert list(groups["dev"]) == ["pytest", "ruff"]

    def test_missing_table_returns_empty(self, tmp_path: object) -> None:
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[project]\ndependencies = ["foo"]\n')
        assert read_pyproject_groups(p) == {}

    def test_non_table_value_raises(self, tmp_path: object) -> None:
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('dependency-groups = "not-a-table"\n')
        with pytest.raises(TypeError, match="must be a table"):
            read_pyproject_groups(p)


class TestResolveGroupsToRequirements:
    def test_returns_empty_when_nothing_selected(self) -> None:
        groups = {"dev": ["pytest"]}
        assert resolve_groups_to_requirements(groups, ()) == []

    def test_expands_single_group(self) -> None:
        groups = {"dev": ["pytest>=7", "ruff"]}
        reqs = resolve_groups_to_requirements(groups, ("dev",))
        names = sorted(r.name for r in reqs)
        assert names == ["pytest", "ruff"]

    def test_expands_include_group(self) -> None:
        groups = {
            "test": ["pytest"],
            "dev": [{"include-group": "test"}, "ruff"],
        }
        reqs = resolve_groups_to_requirements(groups, ("dev",))
        names = sorted(r.name for r in reqs)
        assert names == ["pytest", "ruff"]

    def test_unknown_group_raises(self) -> None:
        with pytest.raises(BaseException, match="nope"):
            resolve_groups_to_requirements({"dev": ["pytest"]}, ("nope",))

    def test_malformed_requirement_string_raises(self) -> None:
        with pytest.raises(
            InvalidProjectRequirementError, match=r"\[dependency-groups\]"
        ):
            resolve_groups_to_requirements({"dev": ["pytest >= bad junk"]}, ("dev",))


class TestReadPyprojectOptionalDependencies:
    def test_reads_optional_dependencies(self, tmp_path: object) -> None:
        p = Path(str(tmp_path)) / "pyproject.toml"
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

    def test_missing_table_returns_empty(self, tmp_path: object) -> None:
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[project]\ndependencies = ["foo"]\n')
        assert read_pyproject_optional_dependencies(p) == {}

    def test_non_table_value_raises(self, tmp_path: object) -> None:
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text(
            "[project]\n"
            'name = "x"\n'
            'version = "1"\n'
            'dependencies = ["foo"]\n'
            'optional-dependencies = "not-a-table"\n',
        )
        with pytest.raises(TypeError, match="must be a table"):
            read_pyproject_optional_dependencies(p)


class TestSelectOptionalDependencies:
    def test_returns_empty_when_nothing_selected(self) -> None:
        assert select_optional_dependencies({"cpu": ["torch"]}, ()) == []

    def test_expands_selected_extras(self) -> None:
        opt = {"cpu": ["torch"], "gpu": ["torch[cuda]", "nvidia-pyindex"]}
        reqs = select_optional_dependencies(opt, ("gpu",))
        names = sorted(r.name for r in reqs)
        assert names == ["nvidia-pyindex", "torch"]

    def test_unknown_extra_raises(self) -> None:
        with pytest.raises(LookupError, match="nope"):
            select_optional_dependencies({"cpu": ["torch"]}, ("nope",))

    def test_malformed_requirement_string_raises(self) -> None:
        with pytest.raises(InvalidProjectRequirementError, match="gpu"):
            select_optional_dependencies({"gpu": ["torch >= bad junk"]}, ("gpu",))


class TestReadPyprojectName:
    def test_reads_name(self, tmp_path: object) -> None:
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[project]\nname = "mypkg"\nversion = "1.0"\n')
        assert read_pyproject_name(p) == "mypkg"

    def test_returns_none_when_project_table_absent(self, tmp_path: object) -> None:
        """A workspace-root pyproject without a [project] table.

        nab still reads ``[tool.nab]`` and friends from such files, so
        the helper has to tolerate the missing project table.
        """
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[tool.nab]\nmode = "specific"\n')
        assert read_pyproject_name(p) is None

    def test_returns_none_when_name_missing(self, tmp_path: object) -> None:
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[project]\nversion = "1.0"\n')
        assert read_pyproject_name(p) is None


class TestExpandSelfExtras:
    def test_no_self_reference_returns_input(self) -> None:
        opt = {"cpu": ["torch"], "gpu": ["torch[cuda]"]}
        assert expand_self_extras(opt, "mypkg", ["cpu"]) == ["cpu"]

    def test_unknown_project_name_short_circuits(self) -> None:
        """When the project's name is not known, nothing can self-reference."""
        opt = {"all": ["mypkg[a]"], "a": ["depA"]}
        assert expand_self_extras(opt, None, ["all"]) == ["all"]

    def test_self_reference_expanded_transitively(self) -> None:
        opt = {
            "all": ["mypkg[a, b]"],
            "a": ["depA"],
            "b": ["depB"],
        }
        # ``Requirement.extras`` is a set: order between siblings is
        # not guaranteed.  Originally-selected extras appear before
        # transitively-discovered ones.
        result = expand_self_extras(opt, "mypkg", ["all"])
        assert result[0] == "all"
        assert sorted(result[1:]) == ["a", "b"]

    def test_canonical_match_collapses_underscore_dot_hyphen(self) -> None:
        """PEP 503 canonicalisation makes ``my_pkg``/``My.Pkg``/``mypkg`` all match."""
        opt = {"all": ["My.Pkg[a]"], "a": ["depA"]}
        assert expand_self_extras(opt, "my_pkg", ["all"]) == ["all", "a"]

    def test_chain_of_self_references(self) -> None:
        opt = {
            "all": ["mypkg[mid]"],
            "mid": ["mypkg[leaf]"],
            "leaf": ["depA"],
        }
        assert expand_self_extras(opt, "mypkg", ["all"]) == ["all", "mid", "leaf"]

    def test_cycle_terminates(self) -> None:
        opt = {
            "a": ["mypkg[b]"],
            "b": ["mypkg[a]"],
        }
        assert sorted(expand_self_extras(opt, "mypkg", ["a"])) == ["a", "b"]

    def test_unknown_extra_in_self_reference_tolerated(self) -> None:
        """Unknown extras are surfaced by ``select_optional_dependencies``,
        not here; expansion must keep walking what it can.
        """
        opt = {"all": ["mypkg[a, missing]"], "a": ["depA"]}
        assert sorted(expand_self_extras(opt, "mypkg", ["all"])) == [
            "a",
            "all",
            "missing",
        ]

    def test_external_extras_ignored(self) -> None:
        """``otherpkg[x]`` inside an extra is not a self-reference."""
        opt = {"all": ["otherpkg[x]"], "x": ["depX"]}
        assert expand_self_extras(opt, "mypkg", ["all"]) == ["all"]

    def test_invalid_requirement_string_skipped(self) -> None:
        """Malformed strings inside an extra's contents do not raise."""
        opt = {"all": ["::not::a::requirement::", "mypkg[a]"], "a": ["depA"]}
        assert expand_self_extras(opt, "mypkg", ["all"]) == ["all", "a"]

    def test_duplicate_input_extras_collapsed(self) -> None:
        """Duplicates in the user-supplied selection are deduped."""
        opt = {"a": ["depA"]}
        assert expand_self_extras(opt, "mypkg", ["a", "a", "a"]) == ["a"]


class TestRaiseForUnsatisfiable:
    """``raise_for_unsatisfiable`` flags a folded range that went empty."""

    def test_satisfiable_ranges_are_silent(self) -> None:
        """A non-empty range produces no error."""
        ranges = {"foo": SpecifierSet(">=1.0").to_range()}
        raise_for_unsatisfiable(ranges, {"foo": ["foo>=1.0"]}, kind="requirement")

    def test_empty_range_raises_naming_every_source(self) -> None:
        """An empty range raises and names every conflicting requirement."""
        empty = SpecifierSet("==1.0").to_range() & SpecifierSet("==2.0").to_range()
        with pytest.raises(ResolutionError) as info:
            raise_for_unsatisfiable(
                {"foo": empty},
                {"foo": ["foo==1.0", "foo==2.0"]},
                kind="requirement",
            )
        message = str(info.value)
        assert "foo==1.0" in message
        assert "foo==2.0" in message
        assert "conflicting requirements" in message

    def test_kind_shapes_the_wording(self) -> None:
        """``kind`` selects the noun used in the message."""
        empty = SpecifierSet("==1.0").to_range() & SpecifierSet("==2.0").to_range()
        with pytest.raises(ResolutionError, match="conflicting constraints"):
            raise_for_unsatisfiable(
                {"foo": empty},
                {"foo": ["foo==1.0", "foo==2.0"]},
                kind="constraint",
            )

    def test_every_unsatisfiable_package_is_listed(self) -> None:
        """Each package with an empty range appears in the message."""
        empty_a = SpecifierSet("==1").to_range() & SpecifierSet("==2").to_range()
        empty_b = SpecifierSet("==3").to_range() & SpecifierSet("==4").to_range()
        with pytest.raises(ResolutionError) as info:
            raise_for_unsatisfiable(
                {"aaa": empty_a, "bbb": empty_b},
                {"aaa": ["aaa==1", "aaa==2"], "bbb": ["bbb==3", "bbb==4"]},
                kind="requirement",
            )
        message = str(info.value)
        assert "aaa: aaa==1, aaa==2" in message
        assert "bbb: bbb==3, bbb==4" in message
