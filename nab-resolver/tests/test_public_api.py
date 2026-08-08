"""Keep the package's exports, its imports, and its README in step.

Embedders are told to import from ``nab_resolver`` rather than from its
submodules, which only works if every advertised name is really bound there.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType

import nab_resolver

README = Path(__file__).resolve().parents[1] / "README.md"

_CLAIM_OPENING = "The public API is"
_BACKTICKED = re.compile(r"`([^`\n]+)`")


def _readme_api_names() -> set[str]:
    """Names the README's public-API paragraph claims the package exports."""
    paragraphs = README.read_text(encoding="utf-8").split("\n\n")
    claims = [p for p in paragraphs if p.startswith(_CLAIM_OPENING)]
    assert len(claims) == 1, f"expected one paragraph opening {_CLAIM_OPENING!r}"

    # The paragraph also names the package it is describing.
    return set(_BACKTICKED.findall(claims[0])) - {nab_resolver.__name__}


def test_every_exported_name_resolves() -> None:
    missing = [name for name in nab_resolver.__all__ if not hasattr(nab_resolver, name)]
    assert missing == []


def test_no_public_name_escapes_all() -> None:
    """Anything bound on the package root without an underscore is declared."""
    bound = {
        name
        for name, value in vars(nab_resolver).items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    }
    assert bound == set(nab_resolver.__all__)


def test_readme_lists_the_exported_names() -> None:
    assert _readme_api_names() == set(nab_resolver.__all__)
