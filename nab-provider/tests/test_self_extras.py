"""Follow a project's references to its own extras and their markers."""

from __future__ import annotations

import pytest
from packaging.requirements import Requirement

from nab_provider.requirements_file import (
    expand_self_extras,
    self_extra_markers,
)


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

        monkeypatch.setattr(
            "nab_provider.requirements_file.parse_requirement", reversed_extras
        )
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
        """Leave unknown extras for ``expand_extra_requirements``.

        Continue walking the reachable extras.
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
        """Selecting all also selects fast when the marker matches all."""
        opt = {
            "all": ['mypkg[fast]; extra == "all"'],
            "fast": ["some-dep"],
        }
        env = {"python_version": "3.11", "python_full_version": "3.11.0"}
        assert expand_self_extras(opt, "mypkg", ["all"], env) == ["all", "fast"]

    def test_self_reference_extra_equals_other_extra_skipped(self) -> None:
        """Selecting all skips the dependency whose marker requires another extra."""
        opt = {
            "all": ['mypkg[fast]; extra == "other"'],
            "fast": ["some-dep"],
        }
        env = {"python_version": "3.11", "python_full_version": "3.11.0"}
        assert expand_self_extras(opt, "mypkg", ["all"], env) == ["all"]

    def test_self_reference_negated_extra_skips_own_extra(self) -> None:
        """The cpu extra skips fast when its marker excludes cpu."""
        opt = {
            "cpu": ['mypkg[fast]; extra != "cpu"'],
            "fast": ["some-dep"],
        }
        env = {"python_version": "3.11", "python_full_version": "3.11.0"}
        assert expand_self_extras(opt, "mypkg", ["cpu"], env) == ["cpu"]

    def test_self_reference_negated_extra_walks_other_extra(self) -> None:
        """A self-ref conditioned by ``extra != "cpu"`` activates from
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
        """The walk follows the closure and collects nested conditions."""
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
        """A nested condition counts when its outer condition reads false:
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
