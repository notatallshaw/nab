"""Source permissions through project configuration and the supported CLI."""

import hashlib
import zipfile
from pathlib import Path

import pytest
import tomli

from nab.cli import main
from nab.config import hooks, values
from nab.config.model import read_pyproject_config
from nab_provider.archive_admission import ArchiveConfig, ArchivePolicy
from nab_provider.errors import ConfigError


@pytest.mark.parametrize(
    "body",
    [
        "3",
        '"allow"',
        "{unknown=true}",
        '{policy="unknown"}',
        '{allowed-urls="https://example.org/"}',
        "{allowed-urls=[3]}",
        '{allowed-urls=["relative"]}',
        "{require-hashes=1}",
    ],
)
def test_invalid_archive_policy_is_rejected(tmp_path: Path, body: str) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname="app"\nversion="1"\n[tool.nab]\narchive=' + body + "\n"
    )
    with pytest.raises(ConfigError, match="archive"):
        read_pyproject_config(path)


def test_archive_policy_defaults_and_rendering() -> None:
    assert values.parse_archive({}, "archive") == ArchiveConfig()
    assert hooks.render_archive(ArchiveConfig()) == "policy=declared"
    config = values.parse_archive(
        {
            "policy": "allow",
            "allowed-urls": ["https://example.org/releases/"],
            "require-hashes": False,
        },
        "archive",
    )
    assert config == ArchiveConfig(
        policy=ArchivePolicy.ALLOW,
        allowed_urls=("https://example.org/releases/",),
        require_hashes=False,
    )
    assert (
        hooks.render_archive(config)
        == "policy=allow, allowed-urls=['https://example.org/releases/'], require-hashes=false"
    )


@pytest.mark.parametrize("action", ["get", "explain"])
def test_archive_permission_is_inspectable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname="app"\nversion="1"\n[tool.nab.archive]\npolicy="allow"\nallowed-urls=["https://example.org/releases/"]\nrequire-hashes=false\n'
    )
    main(["config", action, "archive", "--path", str(path)])
    output = capsys.readouterr().out
    assert "policy=allow" in output
    assert "https://example.org/releases/" in output
    assert "require-hashes=false" in output
    if action == "explain":
        assert str(path) in output


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("ignore_table", [False, True])
def test_cli_archive_refusal_has_actionable_context(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], direct: bool, ignore_table: bool
) -> None:
    path = tmp_path / "pyproject.toml"
    dependency = (
        "core @ https://example.org/core-1-py3-none-any.whl" if direct else "core"
    )
    path.write_text(
        f'[project]\nname="app"\nversion="1"\ndependencies=["{dependency}"]\n[develop.sources]\ncore={{url="https://example.org/core-1-py3-none-any.whl"}}\n'
    )
    args = [
        "lock",
        str(path),
        "--offline",
        "--no-cache",
        "--output",
        str(tmp_path / "pylock.toml"),
    ]
    if ignore_table:
        args.append("--no-sources")
    with pytest.raises(SystemExit, match="1"):
        main(args)
    error = capsys.readouterr().err
    if direct or not ignore_table:
        assert "archive source 'core'" in error
        assert "[tool.nab.archive]" in error
        assert "requires an expected digest" in error
        assert "hashes=" in error
    assert "Traceback" not in error
    assert not (tmp_path / "pylock.toml").exists()


@pytest.mark.parametrize("policy", [None, "declared", "allow", "block"])
def test_cli_source_declaration_and_stricter_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], policy: str | None
) -> None:
    artifact = tmp_path / "core.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("pyproject.toml", '[project]\nname="core"\nversion="1"\n')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    path = tmp_path / "pyproject.toml"
    body = f'[project]\nname="app"\nversion="1"\ndependencies=["core"]\n[develop.sources]\ncore={{url="{artifact.as_uri()}",hashes={{sha256="{digest}"}}}}\n'
    if policy is not None:
        body += f'[tool.nab.archive]\npolicy="{policy}"\n'
    path.write_text(body)
    output = tmp_path / "pylock.toml"
    command = ["lock", str(path), "--offline", "--no-cache", "--output", str(output)]
    if policy == "block":
        with pytest.raises(SystemExit, match="1"):
            main(command)
        assert 'archive.policy="block"' in capsys.readouterr().err
        assert not output.exists()
    else:
        main(command)
        (pin,) = tomli.loads(output.read_text())["packages"]
        assert pin["name"] == "core"
        assert pin["archive"]["hashes"] == {"sha256": digest}
