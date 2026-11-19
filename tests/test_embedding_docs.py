"""Execute the resolver guide and project README examples."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from nab_index.multi_index import IndexConfig
from nab_project.inputs import ResolveInputs
from nab_resolver.errors import ResolutionError

from ._local_artifacts import make_wheel_archive, publish_artifacts
from ._markdown import fenced_blocks

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "docs" / "how-to" / "embed-the-resolver.md"


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
    fences = list(fenced_blocks(GUIDE.read_text(encoding="utf-8")))
    return [
        (body, _output_after(fences, index))
        for index, (language, body) in enumerate(fences)
        if language == "python"
    ]


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


def test_project_readme_resolves_for_the_host(tmp_path: Path) -> None:
    readme = REPO_ROOT / "nab-project" / "README.md"
    [example] = [
        code
        for language, code in fenced_blocks(readme.read_text(encoding="utf-8"))
        if language == "python"
    ]
    namespace: dict[str, object] = {}
    exec(compile(example, str(readme), "exec"), namespace)  # noqa: S102
    project_versions = namespace["project_versions"]
    assert callable(project_versions)

    index = tmp_path / "simple"
    wheel = make_wheel_archive(
        "leaf",
        "1.0",
        "Metadata-Version: 2.1\nName: leaf\nVersion: 1.0\n",
        "py3-none-any",
    )
    publish_artifacts(index, "leaf", {"leaf-1.0-py3-none-any.whl": wheel})
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "app"\nversion = "1"\ndependencies = ["leaf"]\n',
        encoding="utf-8",
    )

    inputs = ResolveInputs(indexes=(IndexConfig("local", index.as_uri()),))
    assert project_versions(project, inputs) == {"leaf": "1.0"}
    with pytest.raises(ResolutionError, match="leaf"):
        project_versions(project, inputs.replace(constraints=("leaf>=2",)))
