"""Apply extra markers and expand selected optional dependencies."""

from __future__ import annotations

import pytest

from nab_provider._provider.metadata_resolver import extend_with_extras
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.utils import InvalidName
from nab_provider.marker_holds import IntractableMarkerError, UnevaluableMarkerError
from nab_provider.project_requirements import add_extra_marker
from nab_provider.requirements_file import (
    InvalidProjectRequirementError,
    expand_extra_requirements,
    parse_project_requirement,
)

from .integer_limits import AT_DIGIT_LIMIT, OVER_DIGIT_LIMIT, requires_int_limit


class TestAddExtraMarker:
    def test_no_existing_marker(self) -> None:
        """A bare requirement gets a fresh ``extra ==`` marker."""
        out = add_extra_marker("numpy>=1.0", "foo")
        assert out == 'numpy>=1.0 ; extra == "foo"'

    def test_with_existing_marker(self) -> None:
        """An existing marker is wrapped and combined with ``and``."""
        out = add_extra_marker("numpy>=1.0 ; python_version >= '3.10'", "foo")
        assert out == 'numpy>=1.0 ; (python_version >= "3.10") and extra == "foo"'

    def test_semicolon_in_direct_url_kept(self) -> None:
        """A ``;`` in a direct-URL is part of the URL, not the marker."""
        out = add_extra_marker("foo @ https://h/a;b/p.tar.gz", "bar")
        req = Requirement(out)
        assert req.url == "https://h/a;b/p.tar.gz"
        assert str(req.marker) == 'extra == "bar"'

    def test_semicolon_in_direct_url_with_marker_kept(self) -> None:
        """The URL ``;`` survives and the existing marker is kept with extra."""
        out = add_extra_marker(
            "foo @ https://h/a;b/p.tar.gz ; python_version >= '3.10'", "bar"
        )
        req = Requirement(out)
        assert req.url == "https://h/a;b/p.tar.gz"
        assert str(req.marker) == 'python_version >= "3.10" and extra == "bar"'

    def test_nested_double_paren_or_group_precedence_kept(self) -> None:
        """Folding must keep the parens around a nested double-paren or group.

        The dep needs ``python_version < "3.10"``, so on 3.12 it stays inactive
        only if the or group cannot leak past the and condition.
        """
        out = add_extra_marker(
            'pkg ; python_version < "3.10" '
            'and ((sys_platform == "linux" or sys_platform == "darwin"))',
            "cli",
        )
        marker = Requirement(out).marker
        assert marker is not None
        env = {"python_version": "3.12", "sys_platform": "darwin", "extra": "cli"}
        assert marker.evaluate(env) is False

    def test_non_canonical_extra_name_normalized(self) -> None:
        """A marker condition normalises a non-canonical extra name per PEP 685."""
        out = add_extra_marker("numpy", "My.Extra")
        assert out == 'numpy ; extra == "my-extra"'

    def test_extra_name_with_marker_syntax_rejected(self) -> None:
        """A name that is not a valid PEP 685 name is rejected.

        A ``[project.optional-dependencies]`` key like
        ``a" or os_name != "x`` would otherwise close the quote and leave
        the dependency conditioned on a marker that is always true.
        """
        with pytest.raises(InvalidName):
            add_extra_marker("pkg", 'a" or os_name != "x')

    def test_invalid_extra_name_rejected_as_project_requirement(self) -> None:
        """An invalid extra name is rejected by the synthesis path, not
        folded into a dependency with a marker that is always true."""
        with pytest.raises(InvalidProjectRequirementError):
            parse_project_requirement(
                "pkg",
                "[project.optional-dependencies] extra 'x'",
                extra='a" or os_name != "x',
            )


def test_invalid_sdist_extra_without_dependencies_is_rejected() -> None:
    with pytest.raises(InvalidProjectRequirementError, match="bad name"):
        extend_with_extras([], {"bad name": []})


class TestSelfReferenceUnevaluableMarker:
    def test_self_reference_gate_no_comparison_decides(self) -> None:
        """A self-ref condition is reduced against the walked extra, the
        second place a marker with no meaning is read."""
        opt = {
            "all": ["mypkg[fast]; python_full_version ~= '3'"],
            "fast": ["some-dep"],
        }
        with pytest.raises(UnevaluableMarkerError, match='python_full_version ~= "3"'):
            expand_extra_requirements(opt, "mypkg", ["all"])


class TestExpandExtraRequirements:
    def test_empty_selection_returns_empty(self) -> None:
        assert expand_extra_requirements({"a": ["depA"]}, "mypkg", []) == []

    def test_no_project_name_flattens_without_self_ref(self) -> None:
        """Without a project name a self-ref cannot be walked, so only the
        selected extra's own requirements come back."""
        opt = {"all": ["mypkg[fast]", "plain"], "fast": ["some-dep"]}
        out = expand_extra_requirements(opt, None, ["all"])
        assert sorted(r.name for r in out) == ["mypkg", "plain"]

    @requires_int_limit
    @pytest.mark.parametrize("operator", ["==", "==="])
    def test_oversized_specifier_version_raises(self, operator: str) -> None:
        """An oversized version converts only on comparison; parsing forces it."""
        with pytest.raises(
            InvalidProjectRequirementError,
            match=r"\[project.optional-dependencies\] extra 'x': Exceeds the limit",
        ):
            expand_extra_requirements(
                {"x": [f"foo{operator}{OVER_DIGIT_LIMIT}"]}, "mypkg", ["x"]
            )

    def test_at_limit_specifier_version_expands(self) -> None:
        """A run of exactly the limit converts, so it stays a legal requirement."""
        out = expand_extra_requirements(
            {"x": [f"foo=={AT_DIGIT_LIMIT}"]}, "mypkg", ["x"]
        )
        assert [r.name for r in out] == ["foo"]

    def test_plain_extra_requirements_keep_their_markers(self) -> None:
        opt = {"cpu": ["torch", "numpy; python_version < '3.10'"]}
        by_name = {r.name: r for r in expand_extra_requirements(opt, "mypkg", ["cpu"])}
        assert by_name["torch"].marker is None
        assert by_name["numpy"].marker is not None
        assert by_name["numpy"].marker.evaluate({"python_version": "3.9"})

    def test_self_ref_marker_propagates_to_flattened_dep(self) -> None:
        """A dependency reached through a conditional extra keeps that condition."""
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

    @pytest.mark.parametrize(
        "literal",
        [
            pytest.param(AT_DIGIT_LIMIT, id="at-limit", marks=requires_int_limit),
            pytest.param(OVER_DIGIT_LIMIT, id="over-limit", marks=requires_int_limit),
        ],
    )
    def test_oversized_self_ref_marker_version_refuses(self, literal: str) -> None:
        """Reducing a self-ref marker decomposes it, so the algebra refuses.

        The limit itself refuses too: decomposition mints neighbouring
        versions by incrementing a component, which needs a digit of headroom.
        """
        opt = {
            "all": [f'mypkg[fast]; python_full_version < "{literal}"'],
            "fast": ["some-dep"],
        }
        with pytest.raises(IntractableMarkerError, match="parse limit"):
            expand_extra_requirements(opt, "mypkg", ["all"])

    def test_self_ref_marker_version_below_the_limit_expands(self) -> None:
        """One digit under the limit leaves the headroom, so the walk runs."""
        opt = {
            "all": [f'mypkg[fast]; python_full_version < "{AT_DIGIT_LIMIT[:-1]}"'],
            "fast": ["some-dep"],
        }
        out = expand_extra_requirements(opt, "mypkg", ["all"])
        assert [r.name for r in out] == ["some-dep"]

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

        monkeypatch.setattr(
            "nab_provider.requirements_file.parse_requirement", reversed_extras
        )
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
        """An ``extra ==`` self-ref condition is satisfied at expansion, so the
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
        """The ``extra == "all" and python_version < "3.10"`` condition drops the
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
                (
                    'mypkg[fast]; extra == "all" and python_version < "3.10"'
                    ' and sys_platform == "linux"'
                )
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
                (
                    'mypkg[fast]; (extra == "all" and python_version < "3.10"'
                    ' and sys_platform == "linux") or (extra == "all"'
                    ' and python_version >= "3.12" and sys_platform == "win32")'
                )
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
                (
                    'mypkg[fast]; extra == "all" and python_version < "3.10"'
                    ' and (sys_platform == "linux" or sys_platform == "darwin")'
                )
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
        """The dep marker survives a pure ``extra ==`` self-ref condition.

        Expansion removes the self-ref clause and retains the dep marker.
        """
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
        """A self-ref conditioned on another extra does not activate."""
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
                (
                    'mypkg[fast]; extra == "all" and '
                    '(python_version < "3.10" or sys_platform == "win32")'
                )
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
        """A chain of satisfied ``extra ==`` self-refs flattens to a bare dep."""
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
                (
                    'mypkg[fast]; (extra == "other" or extra == "x") '
                    'and python_version < "3.10"'
                )
            ],
            "fast": ["some-dep"],
        }
        names = {r.name for r in expand_extra_requirements(opt, "mypkg", ["all"])}
        assert "some-dep" not in names

    def test_self_ref_extras_set_marker_carried_as_residual(self) -> None:
        """An ``extras`` set condition becomes a residual dependency marker.

        The ``extras`` set differs from the ``extra`` scalar used during
        expansion.
        """
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
        """An env value containing ``extra`` remains a residual marker.

        ``sys_platform == "extraos"`` is independent of the walked extra.
        """
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
        """A grouped ``extras`` and environment condition remains intact."""
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
        """A variable comparison against ``extra`` remains a target marker.

        ``sys_platform == extra`` is evaluated against the target platform.
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
