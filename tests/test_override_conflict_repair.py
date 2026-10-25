"""Apply package/index override conflict repairs through the CLI."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from nab.cli import run
from nab.config.model import read_pyproject_config
from nab_provider.records import IndexConfig
from nab_provider.testing import make_coordinator, make_metadata, make_wheel


@pytest.mark.parametrize("package_surface", ["packages", "package-rules"])
@pytest.mark.parametrize("repair_surface", ["package", "index"])
def test_override_conflict_repairs_resolve(
    hermetic_roots: Path,
    capsys: pytest.CaptureFixture[str],
    package_surface: str,
    repair_surface: str,
) -> None:
    """Check entry removal and field removal with surviving policies."""
    path = hermetic_roots / "pyproject.toml"
    header = (
        "[project]\n"
        'name = "app"\n'
        'version = "1.0"\n'
        'dependencies = ["demo==1.0"]\n'
        "[tool.nab]\n"
        'build-policy = "never"\n'
        "[[tool.nab.indexes]]\n"
        'name = "local"\n'
        'url = "https://example.org/simple/"\n'
    )
    package_selector = (
        "[tool.nab.packages.demo]\n"
        if package_surface == "packages"
        else '[[tool.nab.package-rules]]\nmatch = ["demo"]\n'
    )
    policy = 'dist-policy = "wheel-only"\n'
    args = [
        "lock",
        str(path),
        "--offline",
        "--no-cache",
        "--no-workspace-discovery",
        "--no-progress",
        "--format",
        "requirements-without-hashes",
        "--output",
        "-",
    ]
    coordinator = make_coordinator(
        [make_wheel("1.0", package="demo")],
        package="demo",
        metadata_text=make_metadata("demo", "1.0"),
    )
    coordinator.indexes = [IndexConfig("local", "https://example.org/simple/")]
    coordinator.index.store_listing_index("demo", "local")

    with patch(
        "nab_project.resolve.FetchCoordinator",
        return_value=nullcontext(coordinator),
    ):
        for has_other_policy in (False, True):
            package_entry = package_selector + policy
            index_entry = "[tool.nab.index.local]\n" + policy
            if has_other_policy:
                package_entry += "allow-zip-sdists = true\n"
                index_entry += "assume-fresh-seconds = 60\n"

            path.write_text(header + package_entry + index_entry, encoding="utf-8")

            assert run(args) == 1
            message = capsys.readouterr().err
            assert "override conflict for demo==1.0" in message
            if has_other_policy:
                assert (
                    "remove 'dist-policy' from the override for 'demo' or index 'local'"
                    in message
                )
            else:
                assert (
                    "remove the whole entry if it would otherwise set no policy"
                    in message
                )

            entry = package_entry if repair_surface == "package" else index_entry
            replacement = entry.replace(policy, "") if has_other_policy else ""
            path.write_text(
                (header + package_entry + index_entry).replace(entry, replacement),
                encoding="utf-8",
            )

            assert run(args) == 0, capsys.readouterr().err
            assert capsys.readouterr().out.strip() == "demo==1.0"

            if has_other_policy:
                config = read_pyproject_config(path, discover_workspace=False)
                assert config.package_overrides[0].allow_zip_sdists is True
                assert config.index_overrides["local"].assume_fresh_seconds == 60
