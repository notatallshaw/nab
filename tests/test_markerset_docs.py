"""Run the marker-set README and guide, and hold both to what they print.

The README is `nab-markersets`'s PyPI description and the guide is the only
place the package's traps are written out for a reader who has not yet found the
method that carries them.
"""

from __future__ import annotations

import doctest
import re
from pathlib import Path

import nab_markersets
from nab_markersets import errors, markersets

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "nab-markersets" / "README.md"
GUIDE = REPO_ROOT / "docs" / "how-to" / "reason-about-markers.md"

_FENCE = re.compile(r"^```pycon\n(?P<body>.*?)^```", re.DOTALL | re.MULTILINE)

# Pinned so deleting a block fails here rather than passing on nothing.
BLOCKS = {README: 2, GUIDE: 15}


def _run_pycon_blocks(path: Path) -> tuple[int, doctest.TestResults]:
    """Run every ```pycon block in one file, sharing a namespace across them.

    Returns the block count beside the result. ``doctest.testfile`` would need a
    blank line before every closing fence and could not answer that.
    """
    blocks = [match["body"] for match in _FENCE.finditer(path.read_text("utf-8"))]
    parser = doctest.DocTestParser()
    runner = doctest.DocTestRunner()
    globs: dict[str, object] = {}
    for index, body in enumerate(blocks):
        test = parser.get_doctest(body, globs, f"{path.name}[{index}]", str(path), 0)
        runner.run(test, clear_globs=False)
        globs = test.globs
    return len(blocks), runner.summarize(verbose=False)


def test_the_docs_print_what_they_document() -> None:
    for path, expected in BLOCKS.items():
        blocks, results = _run_pycon_blocks(path)

        assert blocks == expected, path.name
        assert results.failed == 0, path.name
        assert results.attempted > 0, path.name


def test_both_pages_copy_the_promised_api_table() -> None:
    """The README's table, the guide's and the package docstring's are one list."""
    promised = _api_table(nab_markersets.__doc__ or "")

    assert promised
    for path in BLOCKS:
        assert promised in path.read_text("utf-8"), path.name


def test_the_promised_table_is_what_the_modules_export() -> None:
    """Every exported name is promised, and every promised name is exported.

    Without this, dropping a name from the table and the README together
    satisfies both copies and quietly unpublishes it.
    """
    promised = {
        (module, name)
        for module, names in _api_rows(nab_markersets.__doc__ or "")
        for name in names
    }
    exported = {
        (module.__name__, name)
        for module in (errors, markersets)
        for name in module.__all__
    }

    assert promised == exported


def _api_rows(text: str) -> list[tuple[str, list[str]]]:
    """Each ``nab_markersets.<module>  <names>`` row as its module and its names."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("    nab_"):
            continue
        module, _, names = line.strip().partition(" ")
        rows.append((module, [n.strip() for n in names.split(",") if n.strip()]))
    return rows


def _api_table(text: str) -> str:
    """The indented ``nab_markersets.<module>  <names>`` rows, left-aligned."""
    rows = [line[4:] for line in text.splitlines() if line.startswith("    nab_")]
    return "\n".join(rows)
