"""Inline metadata supplies declarations independently of the Python body."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab_project.conflicts import ConflictKind, ConflictMember, ConflictSet
from nab_project.input_source import (
    InputFormat,
    ScriptMetadataError,
    read_input_document,
)
from nab_project.inputs import ResolveInputs
from nab_project.resolve import plan_project_inputs
from nab_project.script_metadata import read_script_metadata
from nab_provider.project_requirements import InvalidProjectRequirementError


def _script(tmp_path: Path, block: str, body: str = "") -> Path:
    """Write an executable input whose metadata can be inspected in isolation."""
    path = tmp_path / "script.py"
    path.write_text(f"# /// script\n{block}# ///\n\n{body}", encoding="utf-8")
    return path


def test_script_requirements_and_tool_configuration(tmp_path: Path) -> None:
    path = _script(
        tmp_path,
        '# requires-python = ">=3.11"\n# dependencies = ["Demo[feature]>=1; sys_platform == \'linux\'"]\n# [tool.nab]\n# dist-policy = "wheel-only"\n',
        'raise RuntimeError("must not execute")\n',
    )
    original = path.read_bytes()
    metadata = read_script_metadata(path)
    assert str(metadata.dependencies[0]) == 'Demo[feature]>=1; sys_platform == "linux"'
    assert metadata.requires_python == ">=3.11"
    assert metadata.tool == {"nab": {"dist-policy": "wheel-only"}}
    document = read_input_document(path, input_format=InputFormat.SCRIPT)
    assert document == {
        "project": {
            "dependencies": ['Demo[feature]>=1; sys_platform == "linux"'],
            "requires-python": ">=3.11",
        },
        "tool": {"nab": {"dist-policy": "wheel-only"}},
    }
    assert path.read_bytes() == original


@pytest.mark.parametrize("contents", ["", "#\n", "# dependencies = []\n"])
def test_metadata_fields_are_optional(tmp_path: Path, contents: str) -> None:
    metadata = read_script_metadata(_script(tmp_path, contents))
    assert metadata.dependencies == ()
    assert metadata.requires_python is None
    assert metadata.tool == {}
    assert metadata.project_document() == {"project": {"dependencies": []}, "tool": {}}


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("", "no complete"),
        ("# /// script\n# dependencies=[]\n", "no complete"),
        ("# /// SCRIPT\n# dependencies=[]\n# ///\n", "no complete"),
        ("# /// script\n# dependencies=[]\n\n# ///\n", "no complete"),
        ("# /// script\n# dependencies=[\n# ///\n", "invalid TOML"),
        ("# /// script\n# dependencies='demo'\n# ///\n", "dependencies"),
        ("# /// script\n# dependencies=[1]\n# ///\n", "dependencies"),
        ("# /// script\n# dependencies=['not valid ???']\n# ///\n", "dependencies"),
        ("# /// script\n# requires-python=3\n# ///\n", "must be a string"),
        (
            "# /// script\n# requires-python='not a version'\n# ///\n",
            "requires-python|Invalid",
        ),
        ("# /// script\n# tool=false\n# ///\n", "must be a table"),
        ("# /// script\n# ///\n\n# /// script\n# ///\n", "multiple"),
    ],
)
def test_malformed_metadata_is_refused(
    tmp_path: Path, contents: str, message: str
) -> None:
    path = tmp_path / "script"
    path.write_text(contents)
    with pytest.raises(ScriptMetadataError, match=message):
        read_script_metadata(path)


def test_other_blocks_and_unclosed_blocks_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "script"
    path.write_text(
        "# /// other\n# not valid TOML !\n# ///\n\n# /// script\n# dependencies=[]\n"
        '\n# a prelude comment\n# /// script\n# dependencies=["demo"]\n# ///\n# trailing comment\n'
    )
    assert [
        str(requirement) for requirement in read_script_metadata(path).dependencies
    ] == ["demo"]


def test_embedded_end_markers_follow_the_specification(tmp_path: Path) -> None:
    path = _script(
        tmp_path,
        '# [tool.example]\n# text = """\n# /// <summary>\n# /// text\n# ///\n# /// </summary>\n# """\n',
    )
    assert read_script_metadata(path).tool == {
        "example": {"text": "/// <summary>\n/// text\n///\n/// </summary>\n"}
    }


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("final_newline", [True, False])
def test_python_line_endings_and_missing_final_newline(
    tmp_path: Path, newline: str, final_newline: bool
) -> None:
    path = tmp_path / "script"
    text = '#!/usr/bin/env python\n# /// script\n# dependencies=["demo"]\n# ///'
    if final_newline:
        text += "\n"
    path.write_bytes(text.replace("\n", newline).encode())
    assert len(read_script_metadata(path).dependencies) == 1


@pytest.mark.parametrize(
    "payload",
    [
        b"\xef\xbb\xbf# /// script\n# dependencies=[]\n# ///\n",
        '# coding: latin-1\n# /// script\n# dependencies=[]\n# ///\nprint("é")\n'.encode(
            "latin-1"
        ),
    ],
)
def test_python_encoding_declarations(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "script"
    path.write_bytes(payload)
    assert read_script_metadata(path).dependencies == ()


@pytest.mark.parametrize(
    "payload",
    [b"# coding: unknown-encoding\n", b"\xff\n", b"\xef\xbb\xbf# coding: latin-1\n"],
)
def test_invalid_python_encodings_are_refused(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "script.py"
    path.write_bytes(payload)
    with pytest.raises(ScriptMetadataError, match="cannot decode"):
        read_script_metadata(path)


def test_missing_script_preserves_the_io_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_script_metadata(tmp_path / "missing.py")


def test_project_input_remains_toml(tmp_path: Path) -> None:
    path = tmp_path / "custom.input"
    path.write_text('[project]\ndependencies=["demo"]\n')
    assert read_input_document(path) == {"project": {"dependencies": ["demo"]}}


def test_unicode_line_separators_are_metadata_characters(tmp_path: Path) -> None:
    path = _script(tmp_path, '# [tool.example]\n# text="one\u2028two"\n')
    assert read_script_metadata(path).tool == {"example": {"text": "one\u2028two"}}


def test_python_requirement_without_dependencies(tmp_path: Path) -> None:
    metadata = read_script_metadata(_script(tmp_path, '# requires-python=">=3.11"\n'))
    assert metadata.dependencies == ()
    assert metadata.project_document() == {
        "project": {"dependencies": [], "requires-python": ">=3.11"},
        "tool": {},
    }


def test_oversized_metadata_integer_uses_the_shared_toml_error(
    tmp_path: Path, oversized_integer: str
) -> None:
    path = _script(tmp_path, f"# [tool.example]\n# value={oversized_integer}\n")
    with pytest.raises(ScriptMetadataError, match="invalid TOML"):
        read_script_metadata(path)


@pytest.mark.parametrize(
    "inputs",
    [
        pytest.param(ResolveInputs(default_groups=("dev",)), id="default-group"),
        pytest.param(ResolveInputs(base_group="runtime"), id="base-group"),
        pytest.param(ResolveInputs(build_group="build"), id="build-group"),
        pytest.param(
            ResolveInputs(
                conflicts=(
                    ConflictSet(
                        (
                            ConflictMember(ConflictKind.GROUP, "a"),
                            ConflictMember(ConflictKind.GROUP, "b"),
                        )
                    ),
                )
            ),
            id="conflicts",
        ),
    ],
)
def test_library_refuses_project_settings_for_scripts(inputs: ResolveInputs) -> None:
    with pytest.raises(InvalidProjectRequirementError, match="script inputs"):
        plan_project_inputs(
            Path("unused.py"), inputs, (), input_format=InputFormat.SCRIPT
        )


@pytest.mark.parametrize(
    ("groups", "extras", "build_requirements"),
    [
        pytest.param(("dev",), (), False, id="groups"),
        pytest.param((), ("extra",), False, id="extras"),
        pytest.param((), (), True, id="build-requirements"),
    ],
)
def test_library_refuses_script_project_selection(
    groups: tuple[str, ...], extras: tuple[str, ...], *, build_requirements: bool
) -> None:
    with pytest.raises(InvalidProjectRequirementError, match="script inputs"):
        plan_project_inputs(
            Path("unused.py"),
            ResolveInputs(),
            (),
            groups=groups,
            extras=extras,
            build_requirements=build_requirements,
            input_format=InputFormat.SCRIPT,
        )
