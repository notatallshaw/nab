"""Tests for reading dependencies from pyproject.toml files."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab_python._vendor.packaging.requirements import Requirement
from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.utils import InvalidName
from nab_python.requirements_file import (
    InvalidProjectRequirementError,
    _add_extra_marker,
    _parse_project_requirement,
    expand_extra_requirements,
    expand_group_includes,
    expand_self_extras,
    raise_for_unsatisfiable,
    read_pyproject_dependencies,
    read_pyproject_groups,
    read_pyproject_name,
    read_pyproject_optional_dependencies,
    resolve_groups_to_requirements,
    self_extra_markers,
)
from nab_resolver.errors import ResolutionError


class TestAddExtraMarker:
    def test_no_existing_marker(self) -> None:
        """A bare requirement gets a fresh ``extra ==`` marker."""
        out = _add_extra_marker("numpy>=1.0", "foo")
        assert out == 'numpy>=1.0 ; extra == "foo"'

    def test_with_existing_marker(self) -> None:
        """An existing marker is wrapped and combined with ``and``."""
        out = _add_extra_marker("numpy>=1.0 ; python_version >= '3.10'", "foo")
        assert out == 'numpy>=1.0 ; (python_version >= "3.10") and extra == "foo"'

    def test_semicolon_in_direct_url_kept(self) -> None:
        """A ``;`` in a direct-URL is part of the URL, not the marker."""
        out = _add_extra_marker("foo @ https://h/a;b/p.tar.gz", "bar")
        req = Requirement(out)
        assert req.url == "https://h/a;b/p.tar.gz"
        assert str(req.marker) == 'extra == "bar"'

    def test_semicolon_in_direct_url_with_marker_kept(self) -> None:
        """The URL ``;`` survives and the existing marker is kept with extra."""
        out = _add_extra_marker(
            "foo @ https://h/a;b/p.tar.gz ; python_version >= '3.10'", "bar"
        )
        req = Requirement(out)
        assert req.url == "https://h/a;b/p.tar.gz"
        assert str(req.marker) == 'python_version >= "3.10" and extra == "bar"'

    def test_nested_double_paren_or_group_precedence_kept(self) -> None:
        """Folding must keep the parens around a nested double-paren or group.

        The dep needs ``python_version < "3.10"``, so on 3.12 it stays inactive
        only if the or group cannot leak past the and gate.
        """
        out = _add_extra_marker(
            'pkg ; python_version < "3.10" '
            'and ((sys_platform == "linux" or sys_platform == "darwin"))',
            "cli",
        )
        marker = Requirement(out).marker
        assert marker is not None
        env = {"python_version": "3.12", "sys_platform": "darwin", "extra": "cli"}
        assert marker.evaluate(env) is False

    def test_non_canonical_extra_name_normalized(self) -> None:
        """A non-canonical extra name is normalised (PEP 685) in the gate."""
        out = _add_extra_marker("numpy", "My.Extra")
        assert out == 'numpy ; extra == "my-extra"'

    def test_extra_name_with_marker_syntax_rejected(self) -> None:
        """A name that is not a valid PEP 685 name is rejected.

        A ``[project.optional-dependencies]`` key like
        ``a" or os_name != "x`` would otherwise close the quote and leave
        the dep gated on a marker that is always true.
        """
        with pytest.raises(InvalidName):
            _add_extra_marker("pkg", 'a" or os_name != "x')

    def test_invalid_extra_name_rejected_as_project_requirement(self) -> None:
        """An invalid extra name is rejected by the synthesis path, not
        folded into a dependency with a marker that is always true."""
        with pytest.raises(InvalidProjectRequirementError):
            _parse_project_requirement(
                "pkg",
                "[project.optional-dependencies] extra 'x'",
                extra='a" or os_name != "x',
            )


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

    def test_missing_dependencies_key_returns_empty(self, tmp_path: object) -> None:
        """An absent dependencies key reads as no base deps (PEP 621 optional)."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[project]\nname = "foo"\n')
        assert read_pyproject_dependencies(p) == []

    def test_missing_dependencies_key_with_extras_returns_empty(
        self, tmp_path: object
    ) -> None:
        """Deps declared only as an extra leave the base dep set empty."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text(
            '[project]\nname = "myproj"\nversion = "1.0"\n'
            "[project.optional-dependencies]\n"
            'dev = ["pytest"]\n'
        )
        assert read_pyproject_dependencies(p) == []

    def test_empty_dependencies(self, tmp_path: object) -> None:
        """Return empty list when dependencies list is empty."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text("[project]\ndependencies = []\n")
        assert read_pyproject_dependencies(p) == []

    def test_other_dynamic_field_returns_empty(self, tmp_path: object) -> None:
        """dynamic = ['version'] without a deps key still reads as empty."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[project]\nname = "foo"\ndynamic = ["version"]\n')
        assert read_pyproject_dependencies(p) == []

    def test_dynamic_dependencies_raises(self, tmp_path: object) -> None:
        """dynamic = ['dependencies'] is unsupported, not an empty dep set."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('[project]\nname = "foo"\ndynamic = ["dependencies"]\n')
        with pytest.raises(InvalidProjectRequirementError, match="dynamic"):
            read_pyproject_dependencies(p)

    def test_static_dependencies_win_over_dynamic_listing(
        self, tmp_path: object
    ) -> None:
        """A present static key is read even if dynamic also lists it."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text(
            '[project]\nname = "foo"\n'
            'dynamic = ["dependencies"]\n'
            'dependencies = ["requests"]\n'
        )
        deps = read_pyproject_dependencies(p)
        assert [d.name for d in deps] == ["requests"]

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

    def test_string_project_table_raises(self, tmp_path: object) -> None:
        """A [project] that is a string is rejected, not subscripted."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('project = "hello"\n')
        with pytest.raises(TypeError, match=r"\[project\] must be a table"):
            read_pyproject_dependencies(p)

    def test_array_project_table_raises(self, tmp_path: object) -> None:
        """A [project] that is an array is rejected, not subscripted."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('project = ["a", "b"]\n')
        with pytest.raises(TypeError, match=r"\[project\] must be a table"):
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

    def test_string_project_table_raises(self, tmp_path: object) -> None:
        """A [project] that is a string is rejected before the .get."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('project = "hello"\n')
        with pytest.raises(TypeError, match=r"\[project\] must be a table"):
            read_pyproject_optional_dependencies(p)


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

    def test_string_project_table_raises(self, tmp_path: object) -> None:
        """A [project] that is a string is rejected before the .get."""
        p = Path(str(tmp_path)) / "pyproject.toml"
        p.write_text('project = "hello"\n')
        with pytest.raises(TypeError, match=r"\[project\] must be a table"):
            read_pyproject_name(p)


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
        # Selected extras stay in front; self-ref siblings follow in sorted order.
        assert expand_self_extras(opt, "mypkg", ["all"]) == ["all", "a", "b"]

    def test_self_reference_siblings_walked_in_sorted_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sibling extras enter the worklist in sorted order.

        ``Requirement.extras`` is a set with PYTHONHASHSEED-dependent order, and
        the order siblings enter the worklist decides their
        ``root_package_order`` tiebreak. A reversed extras order must still give
        the sorted result.
        """
        opt = {
            "all": ["mypkg[a, b, c]"],
            "a": ["depA"],
            "b": ["depB"],
            "c": ["depC"],
        }

        def reversed_extras(req_str: str) -> Requirement:
            req = Requirement(req_str)
            monkeypatch.setattr(req, "extras", sorted(req.extras, reverse=True))
            return req

        monkeypatch.setattr("nab_python.requirements_file.Requirement", reversed_extras)
        assert expand_self_extras(opt, "mypkg", ["all"]) == ["all", "a", "b", "c"]

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
        """Unknown extras are surfaced by ``expand_extra_requirements``,
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

    def test_self_reference_extra_equals_own_extra_walked(self) -> None:
        """A self-ref gated by ``extra == "<own-extra>"`` activates its extra."""
        opt = {
            "all": ['mypkg[fast]; extra == "all"'],
            "fast": ["some-dep"],
        }
        env = {"python_version": "3.11", "python_full_version": "3.11.0"}
        assert expand_self_extras(opt, "mypkg", ["all"], env) == ["all", "fast"]

    def test_self_reference_extra_equals_other_extra_skipped(self) -> None:
        """A self-ref gated by ``extra == "<other-extra>"`` does not activate."""
        opt = {
            "all": ['mypkg[fast]; extra == "other"'],
            "fast": ["some-dep"],
        }
        env = {"python_version": "3.11", "python_full_version": "3.11.0"}
        assert expand_self_extras(opt, "mypkg", ["all"], env) == ["all"]

    def test_self_reference_negated_extra_skips_own_extra(self) -> None:
        """A self-ref gated ``extra != "<own-extra>"`` does not activate for it."""
        opt = {
            "cpu": ['mypkg[fast]; extra != "cpu"'],
            "fast": ["some-dep"],
        }
        env = {"python_version": "3.11", "python_full_version": "3.11.0"}
        assert expand_self_extras(opt, "mypkg", ["cpu"], env) == ["cpu"]

    def test_self_reference_negated_extra_walks_other_extra(self) -> None:
        """A self-ref gated ``extra != "cpu"`` activates when walked from
        another extra."""
        opt = {
            "gpu": ['mypkg[fast]; extra != "cpu"'],
            "fast": ["some-dep"],
        }
        env = {"python_version": "3.11", "python_full_version": "3.11.0"}
        assert expand_self_extras(opt, "mypkg", ["gpu"], env) == ["gpu", "fast"]


class TestSelfExtraMarkers:
    def test_unknown_project_name_has_no_markers(self) -> None:
        """Without a project name nothing is a self-reference."""
        opt = {"all": ["mypkg[a]; python_version < '3.10'"], "a": ["depA"]}
        assert self_extra_markers(opt, None, ["all"]) == []

    def test_unmarked_self_reference_contributes_nothing(self) -> None:
        opt = {"all": ["mypkg[a]", "plain; python_version < '3.10'"], "a": ["depA"]}
        assert self_extra_markers(opt, "mypkg", ["all"]) == []

    def test_marker_collected_from_reachable_extras(self) -> None:
        """The walk follows the closure, so a nested gate is collected too."""
        opt = {
            "all": ["mypkg[mid]; python_full_version >= '3.10.4'"],
            "mid": ["mypkg[leaf]; sys_platform == 'win32'"],
            "leaf": ["depL"],
        }
        assert [str(m) for m in self_extra_markers(opt, "mypkg", ["all"])] == [
            'python_full_version >= "3.10.4"',
            'sys_platform == "win32"',
        ]

    def test_marker_collected_regardless_of_outer_gate(self) -> None:
        """A nested gate counts even where the gate above it reads false:
        the environments the caller has to check are not chosen yet."""
        opt = {
            "all": ["mypkg[mid]; python_version < '3.0'"],
            "mid": ["mypkg[leaf]; python_full_version >= '3.10.4'"],
            "leaf": ["depL"],
        }
        assert [str(m) for m in self_extra_markers(opt, "mypkg", ["all"])] == [
            'python_version < "3.0"',
            'python_full_version >= "3.10.4"',
        ]


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

    def test_self_ref_siblings_flattened_in_sorted_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Self-ref siblings feed the flattened requirements in sorted order.

        ``Requirement.extras`` is a set with PYTHONHASHSEED-dependent order, and
        on the universal path the order ``_self_ref_edges`` walks siblings
        becomes the resolver's root package order. A reversed extras order must
        still give the sorted result.
        """
        opt = {
            "all": ["mypkg[a, b, c]"],
            "a": ["depA"],
            "b": ["depB"],
            "c": ["depC"],
        }

        def reversed_extras(req_str: str) -> Requirement:
            req = Requirement(req_str)
            monkeypatch.setattr(req, "extras", sorted(req.extras, reverse=True))
            return req

        monkeypatch.setattr("nab_python.requirements_file.Requirement", reversed_extras)
        out = expand_extra_requirements(opt, "mypkg", ["all"])
        assert [r.name for r in out] == ["depA", "depB", "depC"]

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

    def test_self_ref_extra_gate_does_not_survive_as_marker(self) -> None:
        """An ``extra ==`` self-ref gate is satisfied at expansion, so the
        flattened dep is bare; carrying ``extra == "all"`` forward would
        drop it on every universal tuple, where ``extra`` is unbound."""
        opt = {
            "all": ['mypkg[fast]; extra == "all"'],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is None

    def test_self_ref_combined_gate_keeps_only_env_residual(self) -> None:
        """A gate of ``extra == "all" and python_version < "3.10"`` drops the
        tautological extra clause and keeps the environment condition, so the
        dep survives on 3.9 and not on 3.11."""
        opt = {
            "all": ['mypkg[fast]; extra == "all" and python_version < "3.10"'],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        assert dep.marker.evaluate({"python_version": "3.9"})
        assert not dep.marker.evaluate({"python_version": "3.11"})

    def test_self_ref_combined_gate_keeps_two_anded_env_residuals(self) -> None:
        """Two environment conditions joined by ``and`` alongside the extra
        clause both survive: the residual must keep the ``and`` between them so
        it stays parseable, not collapse to two adjacent comparisons."""
        opt = {
            "all": [
                'mypkg[fast]; extra == "all" and python_version < "3.10"'
                ' and sys_platform == "linux"'
            ],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        assert dep.marker.evaluate({"python_version": "3.9", "sys_platform": "linux"})
        assert not dep.marker.evaluate(
            {"python_version": "3.9", "sys_platform": "win32"}
        )
        assert not dep.marker.evaluate(
            {"python_version": "3.11", "sys_platform": "linux"}
        )

    def test_self_ref_combined_gate_keeps_or_of_two_anded_env_groups(self) -> None:
        """An OR of two multi-conjunct AND-groups keeps each group's inner
        ``and`` and wraps the groups so ``and``/``or`` precedence holds across
        both surviving OR branches."""
        opt = {
            "all": [
                'mypkg[fast]; (extra == "all" and python_version < "3.10"'
                ' and sys_platform == "linux") or (extra == "all"'
                ' and python_version >= "3.12" and sys_platform == "win32")'
            ],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        assert dep.marker.evaluate({"python_version": "3.9", "sys_platform": "linux"})
        assert dep.marker.evaluate({"python_version": "3.12", "sys_platform": "win32"})
        assert not dep.marker.evaluate(
            {"python_version": "3.9", "sys_platform": "win32"}
        )
        assert not dep.marker.evaluate(
            {"python_version": "3.12", "sys_platform": "linux"}
        )

    def test_self_ref_combined_gate_keeps_env_then_nested_disjunction(self) -> None:
        """A plain env conjunct followed by a surviving nested disjunction keeps
        the ``and`` between them, so the residual is ``env and (a or b)``."""
        opt = {
            "all": [
                'mypkg[fast]; extra == "all" and python_version < "3.10"'
                ' and (sys_platform == "linux" or sys_platform == "darwin")'
            ],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        assert dep.marker.evaluate({"python_version": "3.9", "sys_platform": "linux"})
        assert dep.marker.evaluate({"python_version": "3.9", "sys_platform": "darwin"})
        assert not dep.marker.evaluate(
            {"python_version": "3.9", "sys_platform": "win32"}
        )
        assert not dep.marker.evaluate(
            {"python_version": "3.11", "sys_platform": "linux"}
        )

    def test_self_ref_extra_gate_combined_with_dep_marker(self) -> None:
        """The dep's own marker survives when the self-ref gate is a pure
        ``extra ==``: the gate drops out and only the dep marker remains."""
        opt = {
            "all": ['mypkg[fast]; extra == "all"'],
            "fast": ["some-dep; sys_platform == 'linux'"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        assert dep.marker.evaluate({"sys_platform": "linux"})
        assert not dep.marker.evaluate({"sys_platform": "win32"})

    def test_self_ref_extra_gate_for_other_extra_does_not_activate(self) -> None:
        """A self-ref gated by ``extra == "<other>"`` than the one being walked
        never activates, so its extras are not flattened in."""
        opt = {
            "all": ['mypkg[fast]; extra == "other"'],
            "fast": ["some-dep"],
        }
        names = {r.name for r in expand_extra_requirements(opt, "mypkg", ["all"])}
        assert "some-dep" not in names

    def test_self_ref_extra_gate_or_env_is_unconditional(self) -> None:
        """``extra == "all" or python_version`` is a tautology when ``all`` is
        the walked extra, so the dep is required on every environment."""
        opt = {
            "all": ['mypkg[fast]; extra == "all" or python_version < "3.10"'],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is None

    def test_self_ref_extra_gate_nested_keeps_env_disjunction(self) -> None:
        """A nested ``extra == "all" and (env_a or env_b)`` keeps the env
        disjunction after the satisfied extra clause drops."""
        opt = {
            "all": [
                'mypkg[fast]; extra == "all" and '
                '(python_version < "3.10" or sys_platform == "win32")'
            ],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        assert dep.marker.evaluate({"python_version": "3.9", "sys_platform": "linux"})
        assert dep.marker.evaluate({"python_version": "3.11", "sys_platform": "win32"})
        assert not dep.marker.evaluate(
            {"python_version": "3.11", "sys_platform": "linux"}
        )

    def test_self_ref_extra_gate_chain_drops_each_link(self) -> None:
        """A chain of ``extra ==`` self-refs flattens to a bare dep: every
        link's gate is satisfied at expansion."""
        opt = {
            "all": ['mypkg[mid]; extra == "all"'],
            "mid": ['mypkg[leaf]; extra == "mid"'],
            "leaf": ["dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "dep"
        )
        assert dep.marker is None

    def test_self_ref_parenthesised_extra_clause_drops(self) -> None:
        """A bracketed ``(extra == "all")`` conjunct is a satisfied nested
        group, so only the environment condition survives."""
        opt = {
            "all": ['mypkg[fast]; (extra == "all") and python_version < "3.10"'],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        assert dep.marker.evaluate({"python_version": "3.9"})
        assert not dep.marker.evaluate({"python_version": "3.11"})

    def test_self_ref_parenthesised_other_extras_do_not_activate(self) -> None:
        """A bracketed disjunction of non-matching ``extra`` clauses is a
        contradiction, so the self-reference does not activate."""
        opt = {
            "all": [
                'mypkg[fast]; (extra == "other" or extra == "x") '
                'and python_version < "3.10"'
            ],
            "fast": ["some-dep"],
        }
        names = {r.name for r in expand_extra_requirements(opt, "mypkg", ["all"])}
        assert "some-dep" not in names

    def test_self_ref_extras_set_marker_carried_as_residual(self) -> None:
        """A self-ref gated on the ``extras`` set variable (distinct from the
        ``extra`` scalar) is carried onto the reached dep as a residual gate,
        not routed through ``extra`` evaluation, which raises on ``extras``."""
        opt = {
            "all": ['mypkg[fast]; "docs" in extras'],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        assert dep.marker.evaluate({"extras": frozenset({"docs"})}, context="lock_file")
        assert not dep.marker.evaluate({"extras": frozenset()}, context="lock_file")

    def test_self_ref_env_value_containing_extra_substring_kept(self) -> None:
        """A self-ref env gate whose value contains the substring ``extra``
        (``sys_platform == "extraos"``) is kept as a residual, not decided
        against the walked extra and dropped."""
        opt = {
            "all": ['mypkg[fast]; sys_platform == "extraos"'],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        assert dep.marker.evaluate({"sys_platform": "extraos"})
        assert not dep.marker.evaluate({"sys_platform": "linux"})

    def test_self_ref_extras_set_marker_group_gate_kept(self) -> None:
        """A grouped gate mixing the ``extras`` set variable with an environment
        condition is kept whole; the ``extras`` clause is not decided as an
        ``extra`` comparison."""
        opt = {
            "all": ['mypkg[fast]; ("docs" in extras and python_version < "3.10")'],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        assert dep.marker.evaluate(
            {"extras": frozenset({"docs"}), "python_version": "3.9"},
            context="lock_file",
        )
        assert not dep.marker.evaluate(
            {"extras": frozenset(), "python_version": "3.9"},
            context="lock_file",
        )
        assert not dep.marker.evaluate(
            {"extras": frozenset({"docs"}), "python_version": "3.11"},
            context="lock_file",
        )

    def test_unknown_extra_raises(self) -> None:
        with pytest.raises(LookupError, match="not declared"):
            expand_extra_requirements({"a": ["depA"]}, "mypkg", ["missing"])

    def test_self_ref_cycle_terminates(self) -> None:
        opt = {"a": ["mypkg[b]", "depA"], "b": ["mypkg[a]", "depB"]}
        names = {r.name for r in expand_extra_requirements(opt, "mypkg", ["a"])}
        assert {"depA", "depB"} <= names

    def test_self_ref_var_vs_var_extra_gate_kept_as_target_residual(self) -> None:
        """A variable-vs-variable self-ref gate naming ``extra``
        (``sys_platform == extra``) survives as a residual atom over the
        target's ``sys_platform``, not decided against the machine running nab.
        """
        opt = {
            "all": ["mypkg[fast]; sys_platform == extra"],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is not None
        # packaging reads the RHS variable name as a literal, so the residual
        # sys_platform == "extra" fires only where sys_platform is that string.
        assert dep.marker.evaluate({"sys_platform": "extra"})
        assert not dep.marker.evaluate({"sys_platform": "linux"})

    def test_self_ref_negated_extra_gate_for_walked_extra_drops(self) -> None:
        """``extra != "all"`` on the walked extra ``all`` is a contradiction,
        so the reached dep never activates through that path."""
        opt = {
            "all": ['mypkg[fast]; extra != "all"'],
            "fast": ["some-dep"],
        }
        names = {r.name for r in expand_extra_requirements(opt, "mypkg", ["all"])}
        assert "some-dep" not in names

    def test_self_ref_negated_other_extra_gate_is_unconditional(self) -> None:
        """``extra != "other"`` on the walked extra ``all`` is a tautology, so
        the reached dep is bare (activates on every target)."""
        opt = {
            "all": ['mypkg[fast]; extra != "other"'],
            "fast": ["some-dep"],
        }
        dep = next(
            r
            for r in expand_extra_requirements(opt, "mypkg", ["all"])
            if r.name == "some-dep"
        )
        assert dep.marker is None


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
