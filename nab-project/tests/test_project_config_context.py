"""Configuration grammar uses explicit paths and stable time references."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nab_project import config_schema as schema
from nab_project.config_values import SourceConfigError


@pytest.mark.parametrize(("value", "expected"), [("TRUE", True), ("0", False)])
def test_boolean_grammar(value: str, expected: bool, tmp_path: Path) -> None:
    assert (
        schema.ALLOW_ZIP_SDISTS.parse(
            value, "zip setting", context=schema.ProjectValueContext(tmp_path)
        )
        is expected
    )


@pytest.mark.parametrize("value", ["invalid", 1])
def test_boolean_grammar_refuses_invalid_values(value: object, tmp_path: Path) -> None:
    with pytest.raises(SourceConfigError, match="zip setting must be"):
        schema.ALLOW_ZIP_SDISTS.parse(
            value, "zip setting", context=schema.ProjectValueContext(tmp_path)
        )


def test_default_parsing_context_uses_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (index,) = schema.INDEXES.parse([{"name": "local", "url": "file:wheels"}], "index")
    assert index.url == (tmp_path / "wheels").as_uri()


@pytest.mark.parametrize("url", ["file:bad%00path", "file://localhost/bad%00path"])
def test_invalid_file_indexes_are_refused(url: str, tmp_path: Path) -> None:
    with pytest.raises(SourceConfigError, match="invalid file-index URL"):
        schema.INDEXES.parse(
            [{"name": "local", "url": url}],
            "index",
            context=schema.ProjectValueContext(tmp_path),
        )


@pytest.mark.parametrize(
    "url", ["https://[bad", "https://example.invalid/simple", "file:wheels"]
)
def test_index_url_grammar_preserves_non_file_urls(url: str, tmp_path: Path) -> None:
    (index,) = schema.INDEXES.parse(
        [{"name": "local", "url": url}],
        "index",
        context=schema.ProjectValueContext(tmp_path),
    )
    assert index.url == (
        (tmp_path / "wheels").as_uri() if url == "file:wheels" else url
    )


def test_relative_cutoffs_remain_relative_during_inspection(tmp_path: Path) -> None:
    context = schema.ProjectValueContext(tmp_path)
    assert schema.UPLOADED_PRIOR_TO.parse("P4D", "cutoff", context=context) == "P4D"


def test_override_comparison_anchor_is_explicit(tmp_path: Path) -> None:
    anchor = datetime(2026, 5, 10, tzinfo=timezone.utc)
    (rule,) = schema.PACKAGES.parse(
        {"demo": {"uploaded-prior-to": "P4D"}},
        "packages",
        context=schema.ProjectValueContext(tmp_path, comparison_anchor=anchor),
    )
    assert rule.uploaded_prior_to == anchor - timedelta(days=4)


def test_matrix_error_uses_the_declaring_prefix(tmp_path: Path) -> None:
    with pytest.raises(SourceConfigError, match="selection.python-patches"):
        schema.MATRIX.parse(
            {"python": "==3.11.4", "platforms": ["linux_x86_64"]},
            "selection.*",
            context=schema.ProjectValueContext(tmp_path, prefix=""),
        )


def test_absolute_file_index_keeps_its_location(tmp_path: Path) -> None:
    url = (tmp_path / "wheels").as_uri()
    (index,) = schema.INDEXES.parse(
        [{"name": "local", "url": url}],
        "index",
        context=schema.ProjectValueContext(tmp_path),
    )
    assert index.url == url
