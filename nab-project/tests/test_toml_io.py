"""Tests for the TOML reader every nab parse goes through."""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest
import tomli

from nab_project import toml_io

WORKSPACE = Path(__file__).resolve().parents[2]

SHIPPED_PACKAGES = (
    WORKSPACE / "src" / "nab",
    *sorted(WORKSPACE.glob("nab-*/src/nab_*")),
)
"""The source trees the census walks, globbed rather than listed.

A tree the glob missed would leave the census with nothing to find, so the
test asserts this set before walking it.
"""

OVER_NESTED = 1100
"""Deeper than tomli will follow into an inline array or table."""


def _reaches_a_parser(node: ast.AST) -> bool:
    """Whether ``node`` reaches a TOML parser other than ``toml_io``'s.

    An alias or a ``from`` import binds the parser under a name no walk over
    attributes would catch, and ``tomllib`` is a second parser, so the imports
    count as much as the calls.  Plain ``import tomli`` stays allowed, since
    modules import it to name ``TOMLDecodeError``.
    """
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id == "tomli"
            and node.attr in {"load", "loads"}
        )
    if isinstance(node, ast.Import):
        return any(
            alias.name == "tomllib"
            or (alias.name == "tomli" and alias.asname is not None)
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        if node.module == "tomllib":
            return True
        return node.module == "tomli" and any(
            alias.name in {"load", "loads"} for alias in node.names
        )
    return False


def parser_references(source: str) -> list[int]:
    """Return the lines of ``source`` that reach a parser other than ``toml_io``."""
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.stmt, ast.expr)) and _reaches_a_parser(node)
    )


class TestParseFailures:
    def test_syntax_error_stays_a_decode_error(self) -> None:
        with pytest.raises(tomli.TOMLDecodeError, match="Invalid value"):
            toml_io.loads("count = [")

    def test_oversized_integer_becomes_a_decode_error(
        self, oversized_integer: str
    ) -> None:
        with pytest.raises(tomli.TOMLDecodeError, match="Exceeds the limit"):
            toml_io.loads(f"count = {oversized_integer}")

    def test_oversized_integer_in_a_binary_file(self, oversized_integer: str) -> None:
        handle = io.BytesIO(f"count = {oversized_integer}\n".encode())
        with pytest.raises(tomli.TOMLDecodeError, match="Exceeds the limit"):
            toml_io.load(handle)

    # tomli's compiled build trips its own nesting guard and the pure-Python
    # build CPython's recursion limit, so the wording differs but the cause
    # does not.
    def test_over_nested_arrays_become_a_decode_error(self) -> None:
        document = "value = " + "[" * OVER_NESTED + "]" * OVER_NESTED
        with pytest.raises(tomli.TOMLDecodeError) as excinfo:
            toml_io.loads(document)
        assert isinstance(excinfo.value.__cause__, RecursionError)

    def test_over_nested_tables_become_a_decode_error(self) -> None:
        document = "value = " + "{ a = " * OVER_NESTED + "1" + " }" * OVER_NESTED
        with pytest.raises(tomli.TOMLDecodeError) as excinfo:
            toml_io.loads(document)
        assert isinstance(excinfo.value.__cause__, RecursionError)

    def test_undecodable_bytes_stay_a_unicode_error(self) -> None:
        # The decode runs before the parse, so it is not folded.
        with pytest.raises(UnicodeDecodeError):
            toml_io.load(io.BytesIO(b'name = "\xe9"'))

    def test_substituted_error_carries_the_document(
        self, oversized_integer: str
    ) -> None:
        text = f"count = {oversized_integer}\n"
        with pytest.raises(tomli.TOMLDecodeError) as excinfo:
            toml_io.loads(text)
        assert "Exceeds the limit" in excinfo.value.msg
        assert excinfo.value.doc == text
        assert excinfo.value.pos == 0


class TestParseSuccess:
    def test_loads_returns_the_table(self) -> None:
        assert toml_io.loads('[project]\nname = "demo"\n') == {
            "project": {"name": "demo"}
        }

    def test_load_returns_the_table(self) -> None:
        assert toml_io.load(io.BytesIO(b"count = 1\n")) == {"count": 1}

    def test_load_path_returns_the_table(self, tmp_path: Path) -> None:
        document = tmp_path / "pyproject.toml"
        document.write_bytes(b'[project]\nname = "demo"\n')
        assert toml_io.load_path(document) == {"project": {"name": "demo"}}


class TestLoadPathFailures:
    """A failed read reaches the caller unwrapped."""

    def test_a_missing_file_stays_an_os_error(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            toml_io.load_path(tmp_path / "absent.toml")

    def test_undecodable_bytes_stay_a_unicode_error(self, tmp_path: Path) -> None:
        document = tmp_path / "pyproject.toml"
        document.write_bytes(b'name = "\xe9"')
        with pytest.raises(UnicodeDecodeError):
            toml_io.load_path(document)

    def test_a_syntax_error_stays_a_decode_error(self, tmp_path: Path) -> None:
        document = tmp_path / "pyproject.toml"
        document.write_bytes(b"count = [")
        with pytest.raises(tomli.TOMLDecodeError, match="Invalid value"):
            toml_io.load_path(document)


class TestCensus:
    """Every shipped module parses through :mod:`nab_project.toml_io`."""

    def test_census_sees_the_forms_it_bans(self) -> None:
        source = (
            "import tomli\n"
            "import tomli as t\n"
            "import tomllib\n"
            "from tomli import loads\n"
            "from tomli import TOMLDecodeError\n"
            "from tomllib import load\n"
            "tomli.loads(text)\n"
            "tomli.TOMLDecodeError\n"
        )
        assert parser_references(source) == [2, 3, 4, 6, 7]

    def test_census_reads_the_wrapper_itself(self) -> None:
        # toml_io holds the last such call, so finding it is what says the
        # walk can see one at all.
        source = WORKSPACE / "nab-project" / "src" / "nab_project" / "toml_io.py"
        assert len(parser_references(source.read_text(encoding="utf-8"))) == 1

    def test_every_parse_goes_through_toml_io(self) -> None:
        # A glob that missed a package would leave nothing to find.
        assert [package.name for package in SHIPPED_PACKAGES] == [
            "nab",
            "nab_index",
            "nab_markersets",
            "nab_project",
            "nab_provider",
            "nab_resolver",
        ]

        found: list[str] = []
        for package in SHIPPED_PACKAGES:
            for path in sorted(package.rglob("*.py")):
                if "_vendor" in path.parts or path.name == "toml_io.py":
                    continue
                lines = parser_references(path.read_text(encoding="utf-8"))
                found.extend(f"{path.relative_to(WORKSPACE)}:{line}" for line in lines)

        assert found == []
