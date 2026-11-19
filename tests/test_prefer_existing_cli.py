"""Previous-lock CLI policy uses current provider facts and preserves defaults."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from shlex import split
from typing import TYPE_CHECKING

import pytest

from nab import _lock
from nab.cli import run
from nab_project import toml_io, toml_writer
from nab_project.lock_policy import LockPolicy

from .lock_registry import Registry

if TYPE_CHECKING:
    from nab_provider.target import ResolveTarget


@pytest.mark.parametrize(
    ("edit", "field"),
    [
        ('dynamic = "dependencies"', "[project].dynamic"),
        ("dependencies = {foo = 1}", "[project].dependencies"),
        ('optional-dependencies = ["cli"]', "[project.optional-dependencies]"),
        (
            '[project.optional-dependencies]\ncli = "bar"',
            "[project.optional-dependencies].cli",
        ),
    ],
)
def test_locked_reports_invalid_local_project_fields(
    hermetic_roots: Path, capsys: pytest.CaptureFixture[str], edit: str, field: str
) -> None:
    source = hermetic_roots / "local"
    source.mkdir()
    metadata = source / "pyproject.toml"
    valid = '[project]\nname="foo"\nversion="2"\n'
    metadata.write_text(valid, encoding="utf-8")
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname="app"\nversion="1"\ndependencies=["foo>=2"]\n'
        '[[tool.nab.local-sources]]\nname="foo"\npath="local"\n',
        encoding="utf-8",
    )
    lock = hermetic_roots / "pylock.toml"
    args = ("lock", str(project), "--offline", "--no-cache", "--output", str(lock))
    assert run(args) == 0
    saved = lock.read_bytes()

    metadata.write_text(valid + edit, encoding="utf-8")
    assert run((*args, "--locked")) == 1
    assert f"{metadata}: {field} must be" in capsys.readouterr().err
    assert lock.read_bytes() == saved


def test_opt_in_keeps_valid_prior_choices_and_requests_current_metadata(
    registry: Registry,
) -> None:
    assert registry.command() == 0

    registry.add_release()

    assert registry.command("--lock-policy", "prefer-existing") == 0

    assert registry.versions() == {"a": "1", "b": "1"}
    assert registry.coordinators[-1].calls_to("request_metadata")


def test_default_still_selects_new_highest_releases(registry: Registry) -> None:
    assert registry.command() == 0

    registry.add_release()

    assert registry.command() == 0

    assert registry.versions() == {"a": "2", "b": "2"}


@pytest.mark.parametrize("source", ["system", "user", "project", "environment"])
@pytest.mark.parametrize("policy", ["prefer-existing", "reuse"])
def test_file_and_environment_lock_policy_use_saved_versions(
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    policy: str,
) -> None:
    assert registry.command() == 0

    registry.add_release()
    if source == "environment":
        monkeypatch.setenv("NAB_LOCK_POLICY", policy)
    else:
        directory = (
            registry.project.parent
            if source == "project"
            else registry.project.parent.parent
            / ("sys" if source == "system" else "usr")
        )
        directory.mkdir(exist_ok=True)
        (directory / "nab.toml").write_text(f'lock-policy = "{policy}"\n')

    coordinators_before = len(registry.coordinators)

    assert registry.command() == 0
    assert registry.versions() == {"a": "1", "b": "1"}

    if policy == "reuse":
        assert len(registry.coordinators) == coordinators_before
    else:
        assert registry.coordinators[-1].calls_to("request_metadata")


def test_explicit_resolve_overrides_environment_and_file_reuse(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert registry.command() == 0

    registry.add_release()
    registry.project.with_name("nab.toml").write_text('lock-policy = "reuse"\n')
    monkeypatch.setenv("NAB_LOCK_POLICY", "prefer-existing")

    assert registry.command("--lock-policy", "resolve") == 0
    assert registry.versions() == {"a": "2", "b": "2"}


@pytest.mark.parametrize("value", ['"unknown"', "true", "17"])
def test_invalid_file_lock_policy_is_rejected_before_transport(
    registry: Registry, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.project.with_name("nab.toml").write_text(f"lock-policy = {value}\n")

    assert registry.command() == 1
    assert not registry.coordinators
    assert "lock-policy" in capsys.readouterr().err


@pytest.mark.parametrize("argument", ["--locked", "--reproduce"])
def test_configured_reuse_preserves_mode_restrictions(
    registry: Registry, argument: str
) -> None:
    assert registry.command() == 0

    registry.project.with_name("nab.toml").write_text('lock-policy = "reuse"\n')
    coordinators_before = len(registry.coordinators)

    assert registry.command(argument) == 1
    assert len(registry.coordinators) == coordinators_before


def test_config_inspection_reports_file_and_cli_lock_policy(
    registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.project.with_name("nab.toml").write_text('lock-policy = "reuse"\n')

    assert run(("config", "get", "lock-policy", "--path", str(registry.project))) == 0
    assert capsys.readouterr().out.strip() == "reuse"
    assert (
        run(
            (
                "config",
                "get",
                "lock-policy",
                "--path",
                str(registry.project),
                "--lock-policy",
                "resolve",
            )
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "resolve"


def test_missing_prior_uses_a_fresh_resolution(registry: Registry) -> None:
    registry.add_release()

    assert registry.command("--lock-policy", "prefer-existing") == 0

    assert registry.versions() == {"a": "2", "b": "2"}


@pytest.mark.parametrize("contents", ["not valid TOML [", 'lock-version="999"'])
def test_invalid_prior_uses_a_fresh_resolution(
    registry: Registry, contents: str
) -> None:
    registry.output.write_text(contents)
    registry.add_release()

    assert registry.command("--lock-policy", "prefer-existing") == 0

    assert registry.versions() == {"a": "2", "b": "2"}


def test_current_artifact_hashes_replace_previous_records(registry: Registry) -> None:
    assert registry.command() == 0

    registry.digest = "d" * 64

    assert registry.command("--lock-policy", "prefer-existing") == 0

    assert registry.versions() == {"a": "1", "b": "1"}

    for package in toml_io.load_path(registry.output)["packages"]:
        assert all(wheel["hashes"]["sha256"] == "d" * 64 for wheel in package["wheels"])


@pytest.mark.parametrize(
    "change", ["metadata", "python", "removed", "yanked", "source", "new-package"]
)
def test_current_changes_repair_the_model(registry: Registry, change: str) -> None:
    assert registry.command() == 0

    registry.add_release()
    expected = {"a": "1", "b": "2"}
    if change in {"metadata", "source"}:
        registry.declarations["a"]["1"] = ("b==2",)
        if change == "source":
            registry.domain = "https://changed.example"
            registry.digest = "c" * 64
    elif change == "python":
        registry.requires_python[("a", "1")] = ">=3.13"
        expected["a"] = "2"
    elif change == "removed":
        del registry.declarations["a"]["1"]
        expected["a"] = "2"
    elif change == "yanked":
        registry.withdrawn.add(("a", "1"))
        expected["a"] = "2"
    else:
        registry.declarations["a"]["1"] = ("new-package",)
        registry.declarations["new-package"] = {"1": ()}
        expected = {"a": "1", "new-package": "1"}

    assert registry.command("--lock-policy", "prefer-existing") == 0

    assert registry.versions() == expected

    if change == "source":
        for package in toml_io.load_path(registry.output)["packages"]:
            assert all(
                wheel["url"].startswith(registry.domain) for wheel in package["wheels"]
            )


def test_validated_prior_is_shared_with_the_write_summary(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert registry.command() == 0

    def unexpected_read(_path: Path) -> None:
        pytest.fail("validated prior was reparsed for the write summary")

    monkeypatch.setattr(_lock, "read_lockfile_packages", unexpected_read)

    assert registry.command("--lock-policy", "prefer-existing") == 0


def test_refresh_command_keeps_the_previous_lock_policy(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(registry.project.parent)

    assert registry.command() == 0

    registry.add_release()
    invocation = _lock._LockRun(
        path=Path("pyproject.toml"),
        output=Path("pylock.toml"),
        groups=(),
        all_groups=False,
        extras=(),
        all_extras=False,
        offline=None,
        build_requirements=False,
        workspace_discovery=True,
        no_emit_workspace=False,
        cli_overrides={"lock-policy": "prefer-existing"},
        upgrade=False,
        lock_policy=LockPolicy.PREFER_EXISTING,
    )

    assert run(split(invocation.refresh_command())[1:]) == 0

    assert registry.versions() == {"a": "1", "b": "1"}


@pytest.mark.parametrize(
    "arguments",
    [
        ("--format", "requirements"),
        ("--format", "requirements", "--output", "pins-{platform_id}.txt"),
        ("--output", "-"),
        ("--locked",),
        ("--project-resolution", "lowest"),
    ],
)
def test_inapplicable_modes_are_refused(
    registry: Registry, arguments: tuple[str, ...]
) -> None:
    assert registry.command("--lock-policy", "prefer-existing", *arguments) == 1
    assert not registry.output.exists()


@pytest.mark.parametrize("change", ["release", "metadata", "yank", "deletion", "hash"])
def test_reuse_keeps_a_valid_file_without_remote_discovery(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    assert registry.command() == 0

    before = registry.output.read_bytes()
    identity = registry.output.stat()
    coordinator_count = len(registry.coordinators)
    if change == "release":
        registry.add_release()
    elif change == "metadata":
        registry.declarations["a"]["1"] = ("b==2",)
    elif change == "yank":
        registry.withdrawn.add(("a", "1"))
    elif change == "deletion":
        del registry.declarations["a"]["1"]
    else:
        registry.digest = "c" * 64

    def unexpected_transport(_backend: str) -> None:
        pytest.fail("saved-lock reuse created an index transport")

    monkeypatch.setattr(_lock, "_make_resolve_transport", unexpected_transport)
    registry.output.chmod(0o444)
    try:
        assert registry.command("--lock-policy", "reuse") == 0
    finally:
        registry.output.chmod(identity.st_mode)

    after = registry.output.stat()

    assert registry.output.read_bytes() == before
    assert (after.st_ino, after.st_mtime_ns) == (identity.st_ino, identity.st_mtime_ns)
    assert len(registry.coordinators) == coordinator_count


def test_reuse_accepts_compatible_bounds_without_rewriting(registry: Registry) -> None:
    assert registry.command() == 0

    before = registry.output.read_bytes()
    registry.project.write_text(
        registry.project.read_text().replace(
            'dependencies=["a"]', 'dependencies=["a>=1"]'
        )
    )
    coordinator_count = len(registry.coordinators)

    assert registry.command("--lock-policy", "reuse") == 0

    assert registry.output.read_bytes() == before
    assert len(registry.coordinators) == coordinator_count


def test_reuse_miss_uses_current_preferred_resolution(registry: Registry) -> None:
    assert registry.command() == 0

    registry.add_release()
    registry.declarations["a"]["1"] = ("b==2",)
    registry.declarations["new"] = {"1": ()}
    registry.project.write_text(
        registry.project.read_text().replace(
            'dependencies=["a"]', 'dependencies=["a", "new"]'
        )
    )
    coordinator_count = len(registry.coordinators)

    assert registry.command("--lock-policy", "reuse") == 0

    assert len(registry.coordinators) == coordinator_count + 1
    assert registry.versions() == {"a": "1", "b": "2", "new": "1"}
    assert registry.coordinators[-1].calls_to("request_metadata")


@pytest.mark.parametrize("contents", [None, "not valid TOML [", 'lock-version="999"'])
def test_reuse_without_a_supported_lock_resolves(
    registry: Registry, contents: str | None
) -> None:
    if contents is not None:
        registry.output.write_text(contents)

    registry.add_release()

    assert registry.command("--lock-policy", "reuse") == 0

    assert registry.versions() == {"a": "2", "b": "2"}


@pytest.mark.parametrize("policy", ["prefer-existing", "reuse"])
@pytest.mark.parametrize(
    "arguments",
    [
        ("--project-resolution", "lowest"),
        ("--locked",),
        ("--output", "-"),
        ("--format", "requirements"),
    ],
)
def test_nondefault_policy_restrictions_precede_valid_hits(
    registry: Registry, policy: str, arguments: tuple[str, ...]
) -> None:
    assert registry.command() == 0

    before = registry.output.read_bytes()
    coordinator_count = len(registry.coordinators)

    assert registry.command("--lock-policy", policy, *arguments) == 1

    assert registry.output.read_bytes() == before
    assert len(registry.coordinators) == coordinator_count


def test_reuse_refuses_reproduction(registry: Registry) -> None:
    assert registry.command() == 0
    assert registry.command("--lock-policy", "reuse", "--reproduce") == 1


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX arbitrary-byte file source identities"
)
@pytest.mark.parametrize("second_bytes", [b"index-\xfe", b"index-\xef\xbf\xbd"])
def test_reuse_declines_the_existing_file_index_fingerprint_collision(
    registry: Registry,
    second_bytes: bytes,
) -> None:
    first = registry.project.parent / os.fsdecode(b"index-\xff")
    second = registry.project.parent / os.fsdecode(second_bytes)
    text = (
        registry.project.read_text()
        + f'\n[[tool.nab.indexes]]\nname="local"\nurl="{first.as_uri()}"\n'
    )
    registry.project.write_text(text)

    assert registry.command() == 0

    coordinator_count = len(registry.coordinators)
    registry.project.write_text(text.replace(first.as_uri(), second.as_uri()))
    registry.digest = "c" * 64

    assert registry.command("--lock-policy", "reuse") == 0

    assert len(registry.coordinators) == coordinator_count + 1

    for package in toml_io.load_path(registry.output)["packages"]:
        assert all(wheel["hashes"]["sha256"] == "c" * 64 for wheel in package["wheels"])


@pytest.mark.parametrize("policy", ["resolve", "prefer-existing", "reuse"])
@pytest.mark.parametrize(
    "declared", ["", 'dependencies=["a"]\n'], ids=["omitted", "partial"]
)
def test_dynamic_root_dependencies_never_use_a_hidden_backend(
    registry: Registry, policy: str, declared: str
) -> None:
    assert registry.command() == 0

    marker = registry.project.parent / "backend-ran"
    backend = registry.project.parent / "backend.py"
    backend.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('backend imported')\n"
        "def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):\n"
        "    info = Path(metadata_directory) / 'example-1.dist-info'\n"
        "    info.mkdir()\n"
        "    (info / 'METADATA').write_text('Metadata-Version: 2.4\\nName: example\\nVersion: 1\\nRequires-Dist: a\\n')\n"
        "    return info.name\n"
    )
    text = registry.project.read_text().replace(
        'dependencies=["a"]', declared + 'dynamic=["dependencies"]'
    )
    text = text.replace(
        'build-policy="never"',
        'build-policy="build-remote"\nassume-universal-deps=true',
    )
    registry.project.write_text(
        text
        + '\n[build-system]\nrequires=[]\nbuild-backend="backend"\nbackend-path=["."]\n'
    )
    before = registry.output.read_bytes()

    assert registry.command("--lock-policy", policy) == 1

    assert not marker.exists()
    assert registry.output.read_bytes() == before


@pytest.mark.parametrize("cutoff", [None, "2025-01-01T00:00:00Z", "P7D"])
def test_upgrade_uses_live_resolution_and_reanchors_even_with_reuse(
    registry: Registry, cutoff: str | None
) -> None:
    registry.upload_time = "2000-01-01T00:00:00Z"
    if cutoff is not None:
        registry.project.write_text(
            registry.project.read_text().replace(
                'decision-order="stable"',
                f'decision-order="stable"\nuploaded-prior-to="{cutoff}"',
            )
        )

    assert registry.command() == 0

    document = toml_io.load_path(registry.output)
    old_anchor = datetime(2000, 1, 1, tzinfo=timezone.utc)
    document["tool"]["nab"]["created-at"] = old_anchor
    registry.output.write_text(toml_writer.dumps(document))
    coordinator_count = len(registry.coordinators)

    assert registry.command("--lock-policy", "reuse", "--upgrade") == 0

    assert len(registry.coordinators) == coordinator_count + 1
    assert toml_io.load_path(registry.output)["tool"]["nab"]["created-at"] != old_anchor


@pytest.mark.parametrize("change", ["metadata-assumption", "http-index"])
def test_reuse_checks_current_declared_policy_and_source(
    registry: Registry, change: str
) -> None:
    assert registry.command() == 0

    coordinator_count = len(registry.coordinators)
    arguments: tuple[str, ...] = ()
    if change == "metadata-assumption":
        arguments = ("--project-assume-consistent-metadata",)
    else:
        registry.domain = "https://changed.example"
        registry.digest = "c" * 64
        registry.project.write_text(
            registry.project.read_text()
            + '\n[[tool.nab.indexes]]\nname="changed"\nurl="https://changed.example/simple/"\n'
        )

    assert registry.command("--lock-policy", "reuse", *arguments) == 0

    assert len(registry.coordinators) == coordinator_count + 1
    assert registry.coordinators[-1].calls_to("request_metadata")

    if change == "http-index":
        for package in toml_io.load_path(registry.output)["packages"]:
            assert all(
                wheel["url"].startswith(registry.domain) for wheel in package["wheels"]
            )


def test_reuse_shares_the_snapshot_target_plan(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert registry.command() == 0

    original = _lock.plan_targets
    calls = 0

    def counted(config: _lock.NabProjectConfig) -> tuple[ResolveTarget, ...]:
        nonlocal calls
        calls += 1
        return original(config)

    monkeypatch.setattr(_lock, "plan_targets", counted)

    assert registry.command("--lock-policy", "reuse") == 0

    assert calls == 1
