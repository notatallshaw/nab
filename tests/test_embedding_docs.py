"""Run the embedding guide's example and hold it to the output it prints.

The nab-resolver wheel ships no tests, so this page is the only worked provider
an installed consumer can read.
"""

from __future__ import annotations

import io
import re
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import nab_resolver

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "docs" / "how-to" / "embed-the-resolver.md"

_FENCE = re.compile(
    r"^```(?P<language>\w*)\n(?P<body>.*?)^```", re.DOTALL | re.MULTILINE
)


def _fences() -> list[tuple[str, str]]:
    """Every fenced block on the page as ``(language, body)``, in page order."""
    text = GUIDE.read_text(encoding="utf-8")
    return [(match["language"], match["body"]) for match in _FENCE.finditer(text)]


def _output_after(fences: list[tuple[str, str]], index: int) -> str | None:
    """The output a Python block documents, or None when the page shows none.

    A fence right after a Python block is that block's output unless it opens
    the next one.  Any other language fails here rather than being skipped: a
    fence this cannot read is a block that goes unchecked.
    """
    following = fences[index + 1 : index + 2]
    if not following or following[0][0] == "python":
        return None

    language, body = following[0]
    assert language == "text", (
        f"{GUIDE.name} follows a python block with a ```{language} fence; want ```text"
    )
    return body


def _example() -> list[tuple[str, str | None]]:
    """Each Python block paired with the output it documents, in page order."""
    fences = _fences()
    return [
        (body, _output_after(fences, index))
        for index, (language, body) in enumerate(fences)
        if language == "python"
    ]


def _promised_api_table() -> str:
    """The API table lifted out of ``nab_resolver.__doc__``, left-aligned.

    The rows and their wrapped continuations are the docstring's only indented
    block, so the slice runs from the first indented line to the last.
    """
    lines = (nab_resolver.__doc__ or "").splitlines()
    indented = [index for index, line in enumerate(lines) if line.startswith(" ")]
    return textwrap.dedent("\n".join(lines[indented[0] : indented[-1] + 1]))


def test_the_guide_prints_what_it_documents() -> None:
    """Every block runs, and each documented output is the one it produced.

    The blocks are one program split across the page, so they run in order
    against a single namespace.
    """
    example = _example()
    assert example, f"{GUIDE.name} carries no python block"
    assert any(expected is not None for _, expected in example), (
        f"{GUIDE.name} documents no output to compare against"
    )

    namespace: dict[str, object] = {"__name__": "embedding_guide"}
    for position, (body, expected) in enumerate(example, start=1):
        printed = io.StringIO()
        with redirect_stdout(printed):
            exec(compile(body, str(GUIDE), "exec"), namespace)  # noqa: S102
        if expected is not None:
            assert printed.getvalue() == expected, (
                f"{GUIDE.name} python block {position} no longer prints this output"
            )


def test_the_guide_copies_the_promised_api_table() -> None:
    """The page's supported-API table is the package docstring's, verbatim."""
    tables = [
        body
        for language, body in _fences()
        if language == "text" and body.startswith("nab_resolver.")
    ]

    assert len(tables) == 1, f"{GUIDE.name} carries {len(tables)} API tables, want 1"
    assert tables[0].rstrip("\n") == _promised_api_table(), (
        f"{GUIDE.name}'s API table is not the one nab_resolver.__doc__ promises"
    )
