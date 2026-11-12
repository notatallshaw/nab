"""Supported runtimes agree on absent, empty and populated URL queries."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING
from urllib import parse

import pytest

from nab_index import _urljoin

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize(
    "join", [_urljoin.join_url, _urljoin._legacy_join_url], ids=["selected", "legacy"]
)
@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("", "https://example.com/pkg.whl?view=index#old"),
        ("#download", "https://example.com/pkg.whl?view=index#download"),
        ("?", "https://example.com/pkg.whl"),
        ("?#download", "https://example.com/pkg.whl#download"),
        ("?\t\r\n#download", "https://example.com/pkg.whl#download"),
        ("?view=download", "https://example.com/pkg.whl?view=download"),
        ("other.whl?", "https://example.com/other.whl"),
        ("https://other.example/file.whl?", "https://other.example/file.whl"),
        ("mailto:person@example.com?", "mailto:person@example.com"),
    ],
)
def test_query_semantics(
    join: Callable[[str, str], str], reference: str, expected: str
) -> None:
    actual = join("https://example.com/pkg.whl?view=index#old", reference)
    assert parse.urlsplit(actual).geturl() == expected


@pytest.mark.parametrize(
    "join", [_urljoin.join_url, _urljoin._legacy_join_url], ids=["selected", "legacy"]
)
def test_malformed_reference_keeps_authority_validation(
    join: Callable[[str, str], str],
) -> None:
    with pytest.raises(ValueError, match="Invalid IPv6 URL"):
        join("https://example.com/pkg.whl?view=index", "https://[bad/file.whl?")


@pytest.mark.parametrize(
    "clears_empty_query", [False, True], ids=["affected-runtime", "corrected-runtime"]
)
def test_runtime_capability_selects_empty_query_compatibility(
    monkeypatch: pytest.MonkeyPatch, *, clears_empty_query: bool
) -> None:
    original = parse.urljoin

    def runtime_join(base: str, reference: str) -> str:
        parts = parse.urlsplit(base)
        if reference == "?" and parts.query:
            if not clears_empty_query:
                return base
            base = parse.urlunsplit(
                (parts.scheme, parts.netloc, parts.path, "", parts.fragment)
            )
        return original(base, reference)

    try:
        with monkeypatch.context() as changes:
            changes.setattr(parse, "urljoin", runtime_join)
            importlib.reload(_urljoin)
            assert (_urljoin.join_url is runtime_join) is clears_empty_query
            cleared = _urljoin.join_url("https://example.com/pkg.whl?view=index", "?")
            assert parse.urlsplit(cleared).geturl() == "https://example.com/pkg.whl"
            assert _urljoin.join_url(
                "https://example.com/pkg.whl?view=index", "#x"
            ) == ("https://example.com/pkg.whl?view=index#x")
            assert (
                _urljoin.join_url("https://example.com/pkg.whl?view=index", "other.whl")
                == "https://example.com/other.whl"
            )
            assert (
                _urljoin.join_url(
                    "https://example.com/pkg.whl?view=index", "?download=1"
                )
                == "https://example.com/pkg.whl?download=1"
            )
    finally:
        importlib.reload(_urljoin)
