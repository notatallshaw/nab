"""Filename extraction preserves URL normalization in HTML project listings."""

import json
from html import escape

import pytest

from nab_index._pep503 import json_listing


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize(
    ("href", "filename"),
    [
        ("https://files.example/pkg.whl", "pkg.whl"),
        ("http://files.example/pkg.whl", "pkg.whl"),
        ("https://files.example/a%2Fb.whl", "a/b.whl"),
        ("https://files.example/pkg.whl?q=/other.whl", "pkg.whl"),
        ("https://files.example/\tpkg.whl", "pkg.whl"),
        ("https://files.example/\rpkg.whl", "pkg.whl"),
        ("https://files.example/\npkg.whl", "pkg.whl"),
        ("../pkg.whl", "pkg.whl"),
    ],
)
def test_filename_from_html_url(scheme: str, href: str, filename: str) -> None:
    page = f'<a href="{escape(href, quote=True)}">download</a>'
    files = json.loads(json_listing(page, f"{scheme}://index.example/simple/"))["files"]
    assert len(files) == 1
    assert files[0]["filename"] == filename


@pytest.mark.parametrize("href", ["http://a", "https://a", "https://a/"])
def test_authority_without_filename(href: str) -> None:
    page = f'<a href="{href}">download</a>'
    assert json.loads(json_listing(page, "https://index.example/"))["files"] == []


def test_empty_base_still_normalizes_url() -> None:
    page = '<a href="https://files.example/\tpkg.whl">download</a>'
    files = json.loads(json_listing(page, ""))["files"]
    assert files == [{"filename": "pkg.whl", "url": "https://files.example/\tpkg.whl"}]


def test_empty_base_checks_malformed_authority() -> None:
    page = '<a href="https://[bad/foo.whl">download</a>'
    assert json.loads(json_listing(page, ""))["files"] == []


@pytest.mark.parametrize("control", ["\t", "\r", "\n"])
def test_empty_href_normalizes_base(control: str) -> None:
    base = f"https://files.example/a{control}b.whl"
    files = json.loads(json_listing('<a href="">download</a>', base))["files"]
    assert files == [{"filename": "ab.whl", "url": base}]


def test_empty_href_checks_base_authority() -> None:
    page = '<a href="">download</a>'
    assert json.loads(json_listing(page, "https://[bad/foo.whl"))["files"] == []
