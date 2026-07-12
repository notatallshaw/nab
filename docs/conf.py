"""Sphinx configuration for the nab documentation site."""

from __future__ import annotations

import os

project = "nab"
author = "Damian Shaw"
copyright = "2026, Damian Shaw"  # noqa: A001 - Sphinx convention

extensions = [
    "myst_parser",
    "sphinx.ext.intersphinx",
    "sphinx_copybutton",
    "sphinx_design",
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "linkify",
    "tasklist",
]

exclude_patterns = ["_build"]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "packaging": ("https://packaging.pypa.io/en/stable", None),
}

html_theme = "furo"
html_title = "nab"
html_static_path: list[str] = []

# Read the Docs serves several versions from one domain.  Without a canonical
# URL, search engines and link previews pick an arbitrary one.
html_baseurl = os.environ.get("READTHEDOCS_CANONICAL_URL", "")

source_suffix = {
    ".md": "markdown",
}
