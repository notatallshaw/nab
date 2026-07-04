"""Set-correct ``extra`` marker evaluation (:mod:`nab_python._marker_extras`).

The dependency-specifiers spec resolves ``extra`` comparisons against the
whole set of extras requested for a package: ``==`` is membership, ``!=``
is non-membership, and every other operator is undefined (False).  These
tests pin that matrix, the delegation of non-``extra`` leaves to
packaging, and PEP 685 name normalization.
"""

from __future__ import annotations

import pytest

from nab_python._conflict_kind import EMPTY_MEMBERSHIP_SETS
from nab_python._marker_extras import (
    evaluate_marker_with_extras,
    references_extra,
)
from nab_python._vendor.packaging.markers import Marker, default_environment

# A concrete linux / CPython 3.12 environment, seeded with the empty
# lockfile-only set variables exactly as the provider seeds it.
ENV: dict[str, str | frozenset[str]] = {
    **{k: v for k, v in default_environment().items() if isinstance(v, str)},
    "python_version": "3.12",
    "python_full_version": "3.12.0",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
    **EMPTY_MEMBERSHIP_SETS,
}


def _eval(marker_text: str, extras: set[str]) -> bool:
    return evaluate_marker_with_extras(Marker(marker_text), frozenset(extras), ENV)


class TestPositiveOperator:
    """``extra == "x"`` is membership in the requested set."""

    @pytest.mark.parametrize(
        ("extras", "expected"),
        [
            (set(), False),
            ({"gpu"}, True),
            ({"cpu"}, False),
            ({"cpu", "gpu"}, True),
        ],
    )
    def test_eq(self, extras: set[str], expected: bool) -> None:
        assert _eval('extra == "gpu"', extras) is expected

    def test_operand_order_is_symmetric(self) -> None:
        """The extra literal may sit on either side of the operator."""
        assert _eval('"gpu" == extra', {"gpu"}) is True
        assert _eval('"gpu" == extra', {"cpu"}) is False


class TestNegativeOperator:
    """``extra != "x"`` is non-membership; True for the base install."""

    @pytest.mark.parametrize(
        ("extras", "expected"),
        [
            (set(), True),
            ({"gpu"}, False),
            ({"cpu"}, True),
            ({"cpu", "gpu"}, False),
        ],
    )
    def test_ne(self, extras: set[str], expected: bool) -> None:
        assert _eval('extra != "gpu"', extras) is expected


class TestUndefinedOperators:
    """Every operator on ``extra`` other than ``==``/``!=`` evaluates False.

    The vendored packaging evaluator treats ``<=``/``>=`` on a string key
    as ``==``, so without this rule ``extra <= "gpu"`` would wrongly gate
    on the ``gpu`` extra.
    """

    @pytest.mark.parametrize("op", ["<=", ">=", "<", ">", "==="])
    @pytest.mark.parametrize("extras", [set(), {"gpu"}, {"cpu"}])
    def test_undefined_operator_never_matches(
        self, op: str, extras: set[str]
    ) -> None:
        assert _eval(f'extra {op} "gpu"', extras) is False


class TestBooleanCombinations:
    """``and``/``or`` combine leaf results as packaging groups them."""

    @pytest.mark.parametrize(
        ("marker", "extras", "expected"),
        [
            ('extra == "a" and extra == "b"', {"a"}, False),
            ('extra == "a" and extra == "b"', {"a", "b"}, True),
            ('extra == "a" or extra == "b"', set(), False),
            ('extra == "a" or extra == "b"', {"b"}, True),
            ('extra != "a" and extra != "b"', set(), True),
            ('extra != "a" and extra != "b"', {"a"}, False),
            ('extra != "a" and extra != "b"', {"c"}, True),
            ('extra != "a" and extra != "b"', {"a", "b"}, False),
            ('extra == "a" and extra != "b"', {"a"}, True),
            ('extra == "a" and extra != "b"', {"a", "b"}, False),
        ],
    )
    def test_combination(
        self, marker: str, extras: set[str], expected: bool
    ) -> None:
        assert _eval(marker, extras) is expected

    def test_nested_parentheses(self) -> None:
        marker = '(extra == "a" or extra == "b") and sys_platform == "linux"'
        assert _eval(marker, {"a"}) is True
        assert _eval(marker, set()) is False


class TestNonExtraLeavesDelegate:
    """Leaves that do not reference ``extra`` use the resolve environment."""

    def test_pure_environment_marker(self) -> None:
        assert _eval('sys_platform == "linux"', set()) is True
        assert _eval('sys_platform == "win32"', {"gpu"}) is False

    def test_extra_gated_by_environment(self) -> None:
        marker = 'python_version >= "3.10" and extra == "a"'
        assert _eval(marker, {"a"}) is True
        assert _eval(marker, set()) is False

    def test_env_marker_gate_can_veto_extra(self) -> None:
        marker = 'extra == "a" and sys_platform == "win32"'
        assert _eval(marker, {"a"}) is False

    def test_plural_extras_variable_binds_to_the_set(self) -> None:
        """A literal ``"x" in extras`` leaf tests the bound ``extras`` set.

        The provider never routes such a marker through this primitive (see
        ``references_extra``): a plural-set marker in metadata is a base-path
        environment marker evaluated against the empty set and dropped.
        """
        assert _eval('"x" in extras', {"x"}) is True
        assert _eval('"x" in extras', set()) is False


class TestNameNormalization:
    """Extra names compare under PEP 685 canonicalization."""

    def test_literal_and_requested_name_normalize(self) -> None:
        assert _eval('extra == "Foo.Bar"', {"foo-bar"}) is True
        assert evaluate_marker_with_extras(
            Marker('extra == "foo-bar"'), frozenset({"Foo_Bar"}), ENV
        ) is True


class TestQuoteAwareRewrite:
    """A value that merely contains ``extra``/operator text is never a comparison.

    packaging can serialize a value like ``"junk extra =="``; a rewrite that is
    not quote-aware would match the ``extra ==`` inside it and corrupt the
    marker across the value boundary.
    """

    def test_extra_text_inside_a_value_is_not_a_comparison(self) -> None:
        marker = Marker('os_name == "junk extra ==" and sys_platform == "linux"')
        assert references_extra(marker) is False

    def test_value_with_extra_text_is_preserved_alongside_a_real_comparison(
        self,
    ) -> None:
        from nab_python._marker_extras import rewrite_extra_markers

        marker = Marker('os_name == "junk extra ==" and extra == "gpu"')
        assert references_extra(marker) is True
        rewritten = str(rewrite_extra_markers(marker))
        assert rewritten == 'os_name == "junk extra ==" and "gpu" in extras'

    def test_value_containing_extra_text_evaluates_as_environment(self) -> None:
        # os_name is "posix" here, so the value comparison is simply False; the
        # point is that it is not corrupted into an extras membership test.
        assert _eval('os_name == "an extra == thing"', {"gpu"}) is False

    def test_reversed_operand_canonicalizes(self) -> None:
        assert _eval('"GPU_Foo" == extra', {"gpu-foo"}) is True
        assert _eval('"GPU_Foo" != extra', {"gpu-foo"}) is False


class TestReferencesExtra:
    """``marker_references_extra`` detects an ``extra`` comparison anywhere."""

    @pytest.mark.parametrize(
        ("marker", "expected"),
        [
            ('extra == "a"', True),
            ('extra != "a"', True),
            ('"a" == extra', True),
            ('(extra == "a") or sys_platform == "linux"', True),
            ('python_version >= "3.10" and extra == "a"', True),
            ('python_version >= "3.10"', False),
            ('sys_platform == "linux" and os_name == "posix"', False),
            # A parenthesised group that does not reference extra still has
            # to be walked past to reach the rest of the marker.
            ('(sys_platform == "linux") and os_name == "posix"', False),
            ('"x" in extras', False),
        ],
    )
    def test_references_extra(self, marker: str, expected: bool) -> None:
        assert references_extra(Marker(marker)) is expected
