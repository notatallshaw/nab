"""Apply VCS policy repair hints through the CLI."""

from __future__ import annotations

import re
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest
import tomli_w

from nab.cli import run
from nab_project.build_backend import extract_static_metadata
from nab_provider.policy import SourceMaterialization
from nab_provider.testing import make_coordinator


@pytest.mark.parametrize("sources_file", ["pyproject.toml", "nab.toml"])
def test_duplicate_vcs_policy_repair_resolves(
    hermetic_roots: Path,
    capsys: pytest.CaptureFixture[str],
    sources_file: str,
) -> None:
    """Following the suggested edits must preserve cross-file agreement."""
    project_dir = hermetic_roots
    commit = "a" * 40
    url = f"git+https://example.org/foo.git@{commit}"
    policy = {
        "policy": "block",
        "allowed-schemes": ["git+https"],
        "allowed-repos": ["https://example.org/foo.git"],
    }
    pyproject_options: dict[str, object] = {"vcs": policy}
    nab_options: dict[str, object] = {"vcs": policy}
    source_options = (
        pyproject_options if sources_file == "pyproject.toml" else nab_options
    )
    source_options["vcs-sources"] = [{"name": "foo", "url": url}]

    pyproject = project_dir / "pyproject.toml"
    pyproject.write_text(
        tomli_w.dumps(
            {
                "project": {
                    "name": "app",
                    "version": "1.0",
                    "dependencies": ["foo==1.0"],
                },
                "tool": {"nab": pyproject_options},
            }
        ),
        encoding="utf-8",
    )
    nab_toml = project_dir / "nab.toml"
    nab_toml.write_text(tomli_w.dumps(nab_options), encoding="utf-8")
    args = [
        "lock",
        str(pyproject),
        "--no-cache",
        "--no-workspace-discovery",
        "--no-progress",
        "--format",
        "requirements-without-hashes",
        "--output",
        "-",
    ]

    source_dir = project_dir / "materialized-foo"
    source_dir.mkdir()
    (source_dir / "pyproject.toml").write_text(
        '[project]\nname = "foo"\nversion = "1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    metadata = extract_static_metadata(source_dir)
    assert metadata is not None
    coordinator = make_coordinator(
        materialize=lambda *_: SourceMaterialization(source_dir, metadata, commit),
    )
    with patch(
        "nab_project.resolve.FetchCoordinator",
        return_value=nullcontext(coordinator),
    ):
        assert run(args) == 1
        message = capsys.readouterr().err
        tables = re.findall(r'\[(tool\.nab\.vcs|vcs)\]\.policy = "allow"', message)
        assert tables, message

        files = {"tool.nab.vcs": pyproject, "vcs": nab_toml}
        for table in tables:
            path = files[table]
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'policy = "block"', 'policy = "allow"'
                ),
                encoding="utf-8",
            )

        assert run(args) == 0, capsys.readouterr().err

    assert capsys.readouterr().out.strip() == f"foo @ {url}"
