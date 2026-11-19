"""Resolve dependency groups and follow include-group declarations."""

from __future__ import annotations

import sys

import pytest

from nab_provider.requirements_file import (
    InvalidProjectRequirementError,
    expand_group_includes,
    iter_group_requirements,
    resolve_groups_to_requirements,
)

from .integer_limits import AT_DIGIT_LIMIT, OVER_DIGIT_LIMIT, requires_int_limit


def _include_chain(depth: int, leaf: str) -> dict[str, list[str | dict[str, str]]]:
    """Return an include chain ending with ``leaf`` at ``g{depth}``."""
    groups: dict[str, list[str | dict[str, str]]] = {
        f"g{step}": [{"include-group": f"g{step + 1}"}] for step in range(depth)
    }
    groups[f"g{depth}"] = [leaf]
    return groups


def _boundary_marker_message() -> str:
    """Probe the first marker-depth failure from the current caller's stack."""
    depth = 0
    while True:
        depth += 1
        nested = "(" * depth + "os_name == 'posix'" + ")" * depth
        try:
            resolve_groups_to_requirements({"probe": [f"foo ; {nested}"]}, ("probe",))
        except InvalidProjectRequirementError as caught:
            return str(caught)


def _boundary_include_message() -> str:
    """Locate the first failing include depth for the current caller."""
    lower, upper = 0, sys.getrecursionlimit()
    message: str | None = None
    while upper - lower > 1:
        depth = (lower + upper) // 2
        try:
            resolve_groups_to_requirements(_include_chain(depth, "pytest"), ("g0",))
        except InvalidProjectRequirementError as caught:
            upper = depth
            message = str(caught)
        else:
            lower = depth

    assert message is not None, "the include chain did not reach the recursion limit"
    return message


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

    def test_included_requirements_belong_to_each_selected_group(self) -> None:
        groups = {
            "unit": ["pytest"],
            "all": [{"include-group": "unit"}, "ruff"],
        }

        selected = iter_group_requirements(groups, ("all", "Unit", "all"))
        assert [(name, [req.name for req in reqs]) for name, reqs in selected] == [
            ("all", ["pytest", "ruff"]),
            ("Unit", ["pytest"]),
            ("all", ["pytest", "ruff"]),
        ]

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

    def test_over_nested_marker_raises(self) -> None:
        """Report excessive marker nesting as an invalid group requirement."""
        depth = sys.getrecursionlimit()
        over_nested_marker = "(" * depth + "os_name == 'posix'" + ")" * depth
        with pytest.raises(InvalidProjectRequirementError) as caught:
            resolve_groups_to_requirements(
                {"dev": [f"foo ; {over_nested_marker}"]}, ("dev",)
            )

        assert str(caught.value) == (
            "invalid requirement in [dependency-groups]: "
            "marker is nested too deeply to parse"
        )

    def test_marker_at_the_stack_boundary_names_the_marker(self) -> None:
        """The first marker-depth failure retains the marker diagnostic."""
        assert _boundary_marker_message() == (
            "invalid requirement in [dependency-groups]: "
            "marker is nested too deeply to parse"
        )

    def test_over_deep_include_chain_names_the_chain(self) -> None:
        """An include chain beyond the recursion limit reports the include depth."""
        chain = _include_chain(sys.getrecursionlimit(), leaf="pytest")
        with pytest.raises(InvalidProjectRequirementError) as caught:
            resolve_groups_to_requirements(chain, ("g0",))

        assert str(caught.value) == (
            "invalid [dependency-groups]: "
            "include-group chain is nested too deeply to resolve"
        )

    def test_include_chain_at_the_stack_boundary_reports_the_chain(self) -> None:
        """Parsing a plain requirement at the depth limit is still an include error."""
        assert _boundary_include_message() == (
            "invalid [dependency-groups]: "
            "include-group chain is nested too deeply to resolve"
        )

    def test_unreached_malformed_requirement_names_the_chain(self) -> None:
        """The chain is reported even when a group holds a malformed string."""
        chain = _include_chain(sys.getrecursionlimit(), leaf="pytest >= bad junk")
        with pytest.raises(InvalidProjectRequirementError) as caught:
            resolve_groups_to_requirements(chain, ("g0",))

        message = str(caught.value)
        assert "include-group chain" in message
        assert "marker" not in message

    @requires_int_limit
    @pytest.mark.parametrize("operator", ["==", "==="])
    def test_oversized_specifier_version_raises(self, operator: str) -> None:
        """An oversized version converts only on comparison; parsing forces it."""
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"\[dependency-groups\]: Exceeds the limit",
        ):
            resolve_groups_to_requirements(
                {"dev": [f"foo{operator}{OVER_DIGIT_LIMIT}"]}, ("dev",)
            )

    def test_at_limit_specifier_version_resolves(self) -> None:
        """A run of exactly the limit converts, so it stays a legal requirement."""
        reqs = resolve_groups_to_requirements(
            {"dev": [f"foo=={AT_DIGIT_LIMIT}"]}, ("dev",)
        )
        assert [r.name for r in reqs] == ["foo"]


class TestExpandGroupIncludes:
    def test_empty_selection(self) -> None:
        groups = {"dev": [{"include-group": "test"}], "test": ["pytest"]}
        assert expand_group_includes(groups, []) == []

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

    def test_non_sequence_group_value_tolerated(self) -> None:
        """A group whose value is not a list is skipped, not crashed on."""
        groups = {"a": [{"include-group": "b"}], "docs": 5}
        assert expand_group_includes(groups, ["a", "docs"]) == ["a", "docs", "b"]

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
