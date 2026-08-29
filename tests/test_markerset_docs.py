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


def test_the_readme_copies_the_promised_api_table() -> None:
    """The README's table and the package docstring's are one list, not two."""
    promised = _api_table(nab_markersets.__doc__ or "")

    assert promised
    assert promised in README.read_text("utf-8")


def _api_table(text: str) -> str:
    """The indented ``nab_markersets.<module>  <names>`` rows, left-aligned."""
    rows = [line[4:] for line in text.splitlines() if line.startswith("    nab_")]
    return "\n".join(rows)
