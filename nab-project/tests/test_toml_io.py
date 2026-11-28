"""Tests for the TOML reader every nab parse goes through."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
import tomli

from nab_project import toml_io

OVER_NESTED = 1100
"""Deeper than tomli will follow into an inline array or table."""


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


def test_shared_containers_are_copied_once_and_remain_independent() -> None:
    item = {"tags": ["build"]}
    source = {"first": item, "second": item}
    result = toml_io.copy_table(source)

    assert result["first"] is result["second"]
    assert result["first"] is not item
    assert toml_io.values_match(source, result)

    item["tags"].append("test")
    assert not toml_io.values_match(source, result)


def test_cached_reader_rereads_changed_bytes(tmp_path: Path) -> None:
    path = tmp_path / "pylock.toml"
    path.write_bytes(b'version = "1"')
    cache = toml_io.ReadCache()
    first = toml_io.load_path(path, cache=cache)

    assert toml_io.load_path(path, cache=cache) is first
    original = path.stat()
    path.write_bytes(b'version = "2"')
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert toml_io.load_path(path, cache=cache) == {"version": "2"}

    path.write_bytes(b"invalid")
    with pytest.raises(tomli.TOMLDecodeError):
        toml_io.load_path(path, cache=cache)

    path.unlink()
    with pytest.raises(FileNotFoundError):
        toml_io.load_path(path, cache=cache)
