"""Tests for reading dependencies from pyproject.toml files."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python.requirements_file import (
    InvalidProjectRequirementError,
    expand_extra_requirements,
    expand_group_includes,
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

    def test_string_dependencies_value_raises(self, tmp_path: object) -> None:
        """A bare string is rejected, not iterated character by character."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[project]\ndependencies = "requests"\n')
        with pytest.raises(
            InvalidProjectRequirementError, match=r"\[project\].dependencies"
        ):
            read_pyproject_dependencies(p)

    def test_non_string_dependency_element_raises(self, tmp_path: object) -> None:
        """A non-string array element raises rather than crashing on parse."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text("[project]\ndependencies = [123]\n")
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
        with pytest.raises(LookupError, match="nope"):
            resolve_groups_to_requirements({"dev": ["pytest"]}, ("nope",))

    def test_multiple_missing_includes_raise_lookuperror(self) -> None:
        groups = {"x": [{"include-group": "miss1"}, {"include-group": "miss2"}]}
        with pytest.raises(LookupError, match="miss1.*miss2"):
            resolve_groups_to_requirements(groups, ("x",))

    def test_cyclic_include_raises_clean(self) -> None:
        groups = {
            "a": [{"include-group": "b"}],
            "b": [{"include-group": "a"}],
        }
        with pytest.raises(InvalidProjectRequirementError, match="Cyclic"):
            resolve_groups_to_requirements(groups, ("a",))

    def test_duplicate_group_names_raise_clean(self) -> None:
        groups = {"my-dev": ["pytest"], "my_dev": ["ruff"]}
        with pytest.raises(InvalidProjectRequirementError, match="Duplicate"):
            resolve_groups_to_requirements(groups, ("my-dev",))

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

    def test_string_extra_value_raises(self) -> None:
        """An extra whose value is a bare string is rejected, not char-iterated."""
        with pytest.raises(InvalidProjectRequirementError, match="gpu"):
            select_optional_dependencies({"gpu": "torch"}, ("gpu",))

    def test_selected_extra_name_canonicalized(self) -> None:
        """PEP 685: a request differing only by case/separator still matches."""
        opt = {"my-extra": ["requests"]}
        names = [r.name for r in select_optional_dependencies(opt, ("My_Extra",))]
        assert names == ["requests"]

    def test_declared_extra_key_canonicalized(self) -> None:
        opt = {"My_Extra": ["requests"]}
        names = [r.name for r in select_optional_dependencies(opt, ("my-extra",))]
        assert names == ["requests"]


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

    def test_self_reference_extra_name_canonicalized(self) -> None:
        """A self-ref naming an extra non-canonically still walks it (PEP 685)."""
        opt = {
            "all": ["mypkg[Sub-Extra]"],
            "sub-extra": ["mypkg[Deep]"],
            "deep": ["depX"],
        }
        assert expand_self_extras(opt, "mypkg", ["all"]) == ["all", "sub-extra", "deep"]

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

    def test_self_reference_marker_false_skipped(self) -> None:
        """A self-ref whose marker is false does not activate its extra."""
        opt = {
            "all": ["mypkg[fast]; python_version < '3.10'"],
            "fast": ["some-dep"],
        }
        env = {"python_version": "3.12", "python_full_version": "3.12.0"}
        assert expand_self_extras(opt, "mypkg", ["all"], env) == ["all"]

    def test_self_reference_marker_true_walked(self) -> None:
        """A self-ref whose marker is true activates its extra."""
        opt = {
            "all": ["mypkg[fast]; python_version < '3.10'"],
            "fast": ["some-dep"],
        }
        env = {"python_version": "3.9", "python_full_version": "3.9.0"}
        assert expand_self_extras(opt, "mypkg", ["all"], env) == ["all", "fast"]

    def test_self_reference_without_marker_walked_with_environment(self) -> None:
        """An unconditional self-ref is walked even when an environment is given."""
        opt = {"all": ["mypkg[a]"], "a": ["depA"]}
        env = {"python_version": "3.12", "python_full_version": "3.12.0"}
        assert expand_self_extras(opt, "mypkg", ["all"], env) == ["all", "a"]

    def test_self_reference_membership_set_marker_skipped(self) -> None:
        """A self-ref marker testing a lockfile-only set is False at resolve time."""
        opt = {
            "all": ['mypkg[fast]; "x" in extras'],
            "fast": ["some-dep"],
        }
        env = {"python_version": "3.12", "python_full_version": "3.12.0"}
        assert expand_self_extras(opt, "mypkg", ["all"], env) == ["all"]


class TestExpandExtraRequirements:
    def test_empty_selection_returns_empty(self) -> None:
        assert expand_extra_requirements({"a": ["depA"]}, "mypkg", []) == []

    def test_no_project_name_flattens_without_self_ref(self) -> None:
        """Without a project name a self-ref cannot be walked, so only the
        selected extra's own requirements come back."""
        opt = {"all": ["mypkg[fast]", "plain"], "fast": ["some-dep"]}
        out = expand_extra_requirements(opt, None, ["all"])
        assert sorted(r.name for r in out) == ["mypkg", "plain"]

    def test_plain_extra_requirements_keep_their_markers(self) -> None:
        opt = {"cpu": ["torch", "numpy; python_version < '3.10'"]}
        by_name = {r.name: r for r in expand_extra_requirements(opt, "mypkg", ["cpu"])}
        assert by_name["torch"].marker is None
        assert by_name["numpy"].marker is not None
        assert by_name["numpy"].marker.evaluate({"python_version": "3.9"})

    def test_self_ref_marker_propagates_to_flattened_dep(self) -> None:
        """The reported bug: a marker-gated self-ref's dep carries the marker."""
        opt = {
            "fast": ["some-dep"],
            "all": ["mypkg[fast]; python_version < '3.10'"],
        }
        out = expand_extra_requirements(opt, "mypkg", ["all"])
        dep = next(r for r in out if r.name == "some-dep")
        assert dep.marker is not None
        assert dep.marker.evaluate({"python_version": "3.9"})
        assert not dep.marker.evaluate({"python_version": "3.11"})

    def test_self_ref_without_marker_leaves_dep_unmarked(self) -> None:
        opt = {"all": ["mypkg[fast]"], "fast": ["some-dep"]}
        out = expand_extra_requirements(opt, "mypkg", ["all"])
        dep = next(r for r in out if r.name == "some-dep")
        assert dep.marker is None

    def test_self_reference_not_emitted_as_requirement(self) -> None:
        """The self-reference activates its extra but never lands as a
        requirement of its own; the project is the root, not a dependency."""
        opt = {"all": ["mypkg[fast]"], "fast": ["some-dep"]}
        names = {r.name for r in expand_extra_requirements(opt, "mypkg", ["all"])}
        assert "mypkg" not in names
        assert "some-dep" in names

    def test_dep_own_marker_anded_with_activation(self) -> None:
        opt = {
            "fast": ["some-dep; sys_platform == 'linux'"],
            "all": ["mypkg[fast]; python_version < '3.10'"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker.evaluate({"sys_platform": "linux", "python_version": "3.9"})
        assert not dep.marker.evaluate(
            {"sys_platform": "linux", "python_version": "3.11"}
        )
        assert not dep.marker.evaluate(
            {"sys_platform": "win32", "python_version": "3.9"}
        )

    def test_chain_of_self_refs_combines_markers(self) -> None:
        opt = {
            "all": ["mypkg[mid]; python_version < '3.12'"],
            "mid": ["mypkg[leaf]; sys_platform == 'linux'"],
            "leaf": ["dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "dep"
        )
        assert dep.marker.evaluate({"python_version": "3.11", "sys_platform": "linux"})
        assert not dep.marker.evaluate(
            {"python_version": "3.12", "sys_platform": "linux"}
        )
        assert not dep.marker.evaluate(
            {"python_version": "3.11", "sys_platform": "win32"}
        )

    def test_multi_path_emits_dep_under_each_activation(self) -> None:
        """A dep reachable through two markers is required under their OR:
        each activation path is emitted separately."""
        opt = {
            "all": [
                "mypkg[fast]; python_version < '3.10'",
                "mypkg[fast]; sys_platform == 'win32'",
            ],
            "fast": ["some-dep"],
        }
        deps = [
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        ]
        assert len(deps) == 2
        py_only = {"python_version": "3.9", "sys_platform": "linux"}
        win_only = {"python_version": "3.11", "sys_platform": "win32"}
        neither = {"python_version": "3.11", "sys_platform": "linux"}
        assert any(d.marker.evaluate(py_only) for d in deps)
        assert any(d.marker.evaluate(win_only) for d in deps)
        assert not any(d.marker.evaluate(neither) for d in deps)

    def test_unknown_extra_raises(self) -> None:
        with pytest.raises(LookupError, match="not declared"):
            expand_extra_requirements({"a": ["depA"]}, "mypkg", ["missing"])

    def test_self_ref_cycle_terminates(self) -> None:
        opt = {"a": ["mypkg[b]", "depA"], "b": ["mypkg[a]", "depB"]}
        names = {r.name for r in expand_extra_requirements(opt, "mypkg", ["a"])}
        assert {"depA", "depB"} <= names


class TestExpandGroupIncludes:
    def test_no_include_returns_input(self) -> None:
        groups = {"a": ["depA"], "b": ["depB"]}
        assert expand_group_includes(groups, ["a"]) == ["a"]

    def test_include_group_expanded(self) -> None:
        groups = {
            "all-tools": [{"include-group": "b22"}, {"include-group": "b23"}],
            "b22": ["black==22.0"],
            "b23": ["black==23.0"],
        }
        assert expand_group_includes(groups, ["all-tools"]) == [
            "all-tools",
            "b22",
            "b23",
        ]

    def test_chain_of_includes(self) -> None:
        groups = {
            "all": [{"include-group": "mid"}],
            "mid": [{"include-group": "leaf"}],
            "leaf": ["depA"],
        }
        assert expand_group_includes(groups, ["all"]) == ["all", "mid", "leaf"]

    def test_diamond_include_visited_once(self) -> None:
        """A group reached through two paths is emitted a single time."""
        groups = {
            "all": [{"include-group": "left"}, {"include-group": "right"}],
            "left": [{"include-group": "shared"}],
            "right": [{"include-group": "shared"}],
            "shared": ["depShared"],
        }
        assert expand_group_includes(groups, ["all"]) == [
            "all",
            "left",
            "right",
            "shared",
        ]

    def test_include_name_canonicalized(self) -> None:
        """An include naming a group non-canonically still walks it."""
        groups = {"all": [{"include-group": "Sub_Group"}], "sub-group": ["depA"]}
        assert expand_group_includes(groups, ["all"]) == ["all", "sub-group"]

    def test_table_entry_without_include_group_ignored(self) -> None:
        """A table entry that is not an include record is skipped."""
        groups = {"a": [{"not-an-include": "x"}, "depA"]}
        assert expand_group_includes(groups, ["a"]) == ["a"]

    def test_non_string_include_tolerated(self) -> None:
        """A malformed (non-string) include is skipped, not crashed on."""
        groups = {"a": [{"include-group": 123}]}
        assert expand_group_includes(groups, ["a"]) == ["a"]

    def test_cycle_terminates(self) -> None:
        groups = {
            "a": [{"include-group": "b"}],
            "b": [{"include-group": "a"}],
        }
        assert sorted(expand_group_includes(groups, ["a"])) == ["a", "b"]

    def test_unknown_include_tolerated(self) -> None:
        """An include naming a missing group does not raise here."""
        groups = {"a": [{"include-group": "missing"}]}
        assert expand_group_includes(groups, ["a"]) == ["a", "missing"]

    def test_duplicate_input_groups_collapsed(self) -> None:
        groups = {"a": ["depA"]}
        assert expand_group_includes(groups, ["a", "a"]) == ["a"]


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
