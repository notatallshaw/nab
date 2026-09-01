"""Tests for the guarded PEP 508 requirement parser."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nab_provider._vendor.packaging.requirements import InvalidRequirement
from nab_provider.pep508 import parse_requirement

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SHIPPED_PACKAGES = (
    "src/nab",
    "nab-index/src/nab_index",
    "nab-markersets/src/nab_markersets",
    "nab-project/src/nab_project",
    "nab-provider/src/nab_provider",
    "nab-resolver/src/nab_resolver",
)

_GUARDED_PARSER = _REPO_ROOT / "nab-provider/src/nab_provider/pep508.py"


def _direct_requirement_calls(source: str) -> list[int]:
    """Return the lines of ``source`` that call packaging's ``Requirement``.

    Import aliases are collected first, so ``R('x')`` after an ``as R`` import
    counts, as does a module-qualified ``requirements.Requirement('x')``.
    """
    tree = ast.parse(source)
    aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith("packaging.requirements")
        for alias in node.names
        if alias.name == "Requirement"
    }

    def calls_requirement(func: ast.expr) -> bool:
        if isinstance(func, ast.Name):
            return func.id in aliases
        return isinstance(func, ast.Attribute) and func.attr == "Requirement"

    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and calls_requirement(node.func)
    ]


def _shipped_modules() -> list[Path]:
    """Return every shipped module the census scans."""
    out: list[Path] = []
    for package in _SHIPPED_PACKAGES:
        root = _REPO_ROOT / package
        assert root.is_dir(), f"{root} is not a package tree"
        out.extend(
            path
            for path in sorted(root.rglob("*.py"))
            if "_vendor" not in path.parts and path != _GUARDED_PARSER
        )
    return out


class TestParseRequirement:
    def test_parses_a_requirement(self) -> None:
        req = parse_requirement("click>=8 ; os_name == 'posix'")
        assert req.name == "click"
        assert str(req.specifier) == ">=8"
        assert req.marker is not None

    def test_malformed_string_raises_invalid_requirement(self) -> None:
        with pytest.raises(InvalidRequirement):
            parse_requirement("requests >= 2.0 extra junk")

    def test_moderate_nesting_still_parses(self) -> None:
        """Ordinary parenthesised grouping is untouched by the guard."""
        marker = "(" * 100 + "os_name == 'posix'" + ")" * 100
        assert parse_requirement(f"foo ; {marker}").marker is not None

    def test_over_nested_marker_raises_invalid_requirement(
        self, over_nested_marker: str
    ) -> None:
        """Exhausting the stack is a rejected requirement, not a RecursionError."""
        with pytest.raises(InvalidRequirement, match="nested too deeply") as caught:
            parse_requirement(f"foo ; {over_nested_marker}")

        assert isinstance(caught.value.__cause__, RecursionError)


class TestRequirementParsingIsRouted:
    """No shipped module calls packaging's ``Requirement`` directly.

    The vendored group loader parses its own entries, out of the census's
    reach; :func:`~nab_provider.requirements_file.resolve_groups_to_requirements`
    guards that call.
    """

    def test_no_shipped_module_constructs_a_requirement(self) -> None:
        modules = _shipped_modules()
        assert modules

        offenders = [
            f"{path.relative_to(_REPO_ROOT)}:{line}"
            for path in modules
            for line in _direct_requirement_calls(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    @pytest.mark.parametrize(
        "source",
        [
            "from packaging.requirements import Requirement\nRequirement('x')\n",
            "from ._vendor.packaging.requirements import Requirement as R\nR('x')\n",
            "from a.packaging import requirements\nrequirements.Requirement('x')\n",
        ],
    )
    def test_census_catches_every_spelling(self, source: str) -> None:
        assert _direct_requirement_calls(source) == [2]

    def test_census_ignores_a_name_that_only_looks_like_one(self) -> None:
        source = "from nab_resolver.types import RootRequirement\nRootRequirement()\n"
        assert _direct_requirement_calls(source) == []
