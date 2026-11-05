"""Removed combined policies fail with migrations for their original scope."""

from __future__ import annotations

from pathlib import Path

import pytest

from nab.cli import run
from nab.config.model import read_pyproject_config
from nab_provider.errors import ConfigError
from nab_provider.policy import MetadataPolicy


@pytest.mark.parametrize(
    ("legacy", "replacement"),
    [("prefer-wheel", "wheel-or-sdist"), ("sdist-install", "sdist-only")],
)
@pytest.mark.parametrize(
    "table",
    [
        "[tool.nab]",
        "[tool.nab.packages.demo]",
        '[[tool.nab.package-rules]]\nmatch = ["demo"]',
        "[tool.nab.index.pypi]",
    ],
)
def test_removed_policy_explains_both_replacement_settings(
    tmp_path: Path, legacy: str, replacement: str, table: str
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'{table}\ndist-policy = "{legacy}"\n')
    with pytest.raises(ConfigError) as error:
        read_pyproject_config(path, discover_workspace=False)
    message = str(error.value)
    assert f"dist-policy = '{legacy}' was removed" in message
    assert f"dist-policy = '{replacement}'" in message
    assert "metadata-policy = 'prefer-wheel' at the same scope" in message

    path.write_text(
        f'{table}\ndist-policy = "{replacement}"\nmetadata-policy = "prefer-wheel"\n'
    )
    config = read_pyproject_config(path, discover_workspace=False)
    if table == "[tool.nab]":
        assert config.metadata_policy is MetadataPolicy.PREFER_WHEEL
    elif "index" in table:
        assert (
            config.index_overrides["pypi"].metadata_policy
            is MetadataPolicy.PREFER_WHEEL
        )
    else:
        assert (
            config.package_overrides[0].metadata_policy is MetadataPolicy.PREFER_WHEEL
        )


@pytest.mark.parametrize(
    ("legacy", "replacement"),
    [("prefer-wheel", "wheel-or-sdist"), ("sdist-install", "sdist-only")],
)
def test_removed_cli_choice_has_an_executable_replacement(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, legacy: str, replacement: str
) -> None:
    assert run(("lock", "--project-dist-policy", legacy)) != 0
    error = capsys.readouterr().err
    assert f"dist-policy '{legacy}' was removed" in error
    assert (
        f"--project-dist-policy {replacement} --project-metadata-policy prefer-wheel"
        in error
    )
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "demo"\nversion = "1.0"\n')
    assert (
        run(
            (
                "config",
                "get",
                "metadata-policy",
                "--path",
                str(path),
                "--project-dist-policy",
                replacement,
                "--project-metadata-policy",
                "prefer-wheel",
            )
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "prefer-wheel"


@pytest.mark.parametrize("legacy", ["prefer-wheel", "sdist-install"])
def test_table_migration_preserves_the_trust_setting(
    tmp_path: Path, legacy: str
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[tool.nab]\ndist-policy = { policy = "'
        + legacy
        + '", trust-unverified-deps = true }\n'
    )
    with pytest.raises(ConfigError, match="in the existing dist-policy table"):
        read_pyproject_config(path, discover_workspace=False)
    replacement = "sdist-only" if legacy == "sdist-install" else "wheel-or-sdist"
    path.write_text(
        path.read_text().replace(f'policy = "{legacy}"', f'policy = "{replacement}"')
        + 'metadata-policy = "prefer-wheel"\n'
    )
    config = read_pyproject_config(path, discover_workspace=False)
    assert config.trust_unverified_sdist_deps
    assert config.metadata_policy is MetadataPolicy.PREFER_WHEEL


def test_independent_scoped_policies_can_overlap(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[tool.nab.packages."demo<2"]\ndist-policy = "sdist-only"\n'
        '[tool.nab.packages."demo>=1"]\nmetadata-policy = "prefer-wheel"\n'
    )
    config = read_pyproject_config(path, discover_workspace=False)
    assert len(config.package_overrides) == 2

    path.write_text(
        path.read_text().replace(
            'dist-policy = "sdist-only"', 'metadata-policy = "match-artifact"'
        )
    )
    with pytest.raises(ConfigError, match="metadata-policy"):
        read_pyproject_config(path, discover_workspace=False)
