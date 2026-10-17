"""Configuration sources share validators and preserve table override semantics."""

from __future__ import annotations

import hashlib
import shlex
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import respx

from nab import _download, _resolve, _run
from nab._cli.definition.model import Kind, Opt, Scope, VType
from nab.cli import main
from nab.config import hooks, ladder, values
from nab.config.ladder import (
    BY_KEY,
    SourceKind,
    SourceRoots,
    build_cli_layer,
    discover_layers,
    read_env_layer,
    resolve_config,
)
from nab.config.model import read_pyproject_config
from nab.config.subflags import CliKey, CliTable
from nab.config.values import SourceConfigError
from nab_index.transport import HttpResponse
from nab_project import toml_io
from nab_provider.policy import ResolutionStrategy


class NoNetwork:
    """Refuse acquisition so CLI policy tests do not construct live HTTP clients."""

    def __init__(self) -> None:
        """Record any attempt to acquire a URL."""
        self.requests: list[str] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        self.requests.append(url)
        msg = f"unexpected request: {url}"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        """Release this transport's empty resource set."""


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> NoNetwork:
    """Replace the transport boundary while exercising the real CLI and resolver."""
    transport = NoNetwork()
    monkeypatch.setattr(_download, "_make_transport", lambda _backend: transport)
    monkeypatch.setattr(_resolve, "_make_urllib3_transport", lambda: transport)
    return transport


@pytest.mark.parametrize("kind", [SourceKind.SYSTEM_TOML, SourceKind.USER_TOML])
@pytest.mark.parametrize(
    ("key", "text"),
    [
        ("resolution", '"lowest"'),
        ("offline", "true"),
        ("constraints", '["foo<2", "bar>=1"]'),
        ("constraints", "[]"),
        ("archive", '{ policy = "block" }'),
        ("vcs", '{ policy = "block" }'),
        ("indexes", '[{ name = "internal", url = "https://example.org/simple/" }]'),
        ("packages", '{ "foo>=2" = { build-policy = "never" } }'),
        ("package-rules", '[{ match = ["foo", "bar"], build-policy = "never" }]'),
        ("matrix", '{ python = ">=3.11,<3.13", platforms = ["linux_x86_64"] }'),
    ],
)
def test_global_and_environment_values_use_the_file_validator(
    tmp_path: Path, kind: SourceKind, key: str, text: str
) -> None:
    config_file = tmp_path / "nab.toml"
    config_file.write_text(f"{key} = {text}\n")
    roots = (
        SourceRoots(system_toml=config_file)
        if kind is SourceKind.SYSTEM_TOML
        else SourceRoots(user_toml=config_file)
    )
    file_layer = discover_layers(roots)[0]
    name = BY_KEY[key].env_var
    assert name is not None
    token = text.strip('"') if key == "resolution" else text
    environment = read_env_layer({name: token})

    assert environment.values[key] == file_layer.values[key]
    effective = resolve_config([file_layer], environment, build_cli_layer({}))
    assert effective[key].origin.kind is SourceKind.ENV
    assert len(effective[key].stack) == 2


def test_environment_table_is_the_base_for_cli_subflags() -> None:
    environment = read_env_layer(
        {"NAB_ENVIRONMENT": '{ python = "3.11", platform = "linux_x86_64" }'}
    )
    effective = resolve_config(
        [],
        environment,
        build_cli_layer(
            {
                "environment": CliTable(
                    (
                        CliKey(
                            "python", "--project-environment-python", ("3.12",), "3.12"
                        ),
                    )
                )
            }
        ),
    )
    assert effective["environment"].value == {
        "python": "3.12",
        "platform": "linux_x86_64",
    }


@pytest.mark.parametrize(
    "text",
    [
        '{ policy = "unknown" }',
        '{ policy = "block"',
        "{ unknown = true }",
        '{ policy = "block" }\noffline = true',
        "[]",
    ],
)
def test_invalid_structured_environment_names_the_binding(text: str) -> None:
    with pytest.raises(SourceConfigError, match="NAB_ARCHIVE"):
        read_env_layer({"NAB_ARCHIVE": text})


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("NAB_VCS", '{ allowed-repos = ["https://[broken"] }'),
        (
            "NAB_PACKAGES",
            '{ foo = { build-policy="never" }, "foo>=1" = { build-policy="build-remote" } }',
        ),
    ],
)
def test_domain_validation_errors_retain_the_environment_binding(
    name: str, text: str
) -> None:
    with pytest.raises(SourceConfigError, match=name):
        read_env_layer({name: text})


def test_scalar_environment_paths_keep_their_literal_syntax() -> None:
    layer = read_env_layer({"NAB_CACHE_DIR": "[my cache]", "NAB_OFFLINE": "1"})
    assert layer.values["cache-dir"] == Path("[my cache]")
    assert layer.values["offline"] is True


def test_new_declaration_gets_file_environment_and_application_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    option = Opt(
        "future-setting",
        scope=Scope.PROJECT,
        rdefault=(),
        parse=values.parse_string_list,
        render=repr,
        help="test extension settings",
    )
    monkeypatch.setattr(ladder, "OPTIONS", (*ladder.OPTIONS, option))
    monkeypatch.setitem(BY_KEY, option.name, option)
    config_file = tmp_path / "nab.toml"
    config_file.write_text('future-setting = ["system"]\n')

    layers = discover_layers(SourceRoots(system_toml=config_file))
    environment = read_env_layer({"NAB_FUTURE_SETTING": '["session"]'})
    snapshot = ladder.ConfigSnapshot(layers, environment, {})
    effective = dict(snapshot)
    assert len(snapshot) == len(effective)
    assert effective[option.name].value == ("session",)
    assert effective[option.name].origin.kind is SourceKind.ENV
    assert effective[option.name].stack[0][1] == ("system",)

    config_file.write_text('[enforce]\nfuture-setting = ["required"]\n')
    enforced = resolve_config(
        discover_layers(SourceRoots(system_toml=config_file)),
        environment,
        build_cli_layer({}),
    )
    assert enforced[option.name].value == ("required",)
    assert enforced[option.name].origin.enforced


def test_project_environment_scalar_is_applied() -> None:
    effective = resolve_config(
        [], read_env_layer({"NAB_RESOLUTION": "lowest"}), build_cli_layer({})
    )
    assert effective["resolution"].value is ResolutionStrategy.LOWEST


@pytest.mark.parametrize("overrides", [{}, {"resolution": "lowest"}])
def test_snapshot_cannot_silently_discard_separate_cli_overrides(
    tmp_path: Path,
    overrides: dict[str, str],
) -> None:
    snapshot = ladder.ConfigSnapshot([], read_env_layer({}), {})
    with pytest.raises(ValueError, match="snapshot and cli_overrides"):
        read_pyproject_config(
            tmp_path / "pyproject.toml",
            snapshot=snapshot,
            cli_overrides=overrides,
        )


def test_enforced_table_remains_the_reported_winner_under_cli_subflags() -> None:
    environment = read_env_layer(
        {
            "NAB_ENFORCE": '{ environment = { python = "3.11", platform = "linux_x86_64" } }'
        }
    )
    effective = resolve_config(
        [],
        environment,
        build_cli_layer(
            {
                "environment": CliTable(
                    (
                        CliKey(
                            "python", "--project-environment-python", ("3.12",), "3.12"
                        ),
                    )
                )
            }
        ),
    )
    setting = effective["environment"]
    output = ladder.render_explain(effective, "environment")
    winner = next(line for line in output.splitlines() if line.startswith(">"))
    assert setting.value["python"] == "3.11"
    assert setting.spec.render(setting.value) in winner
    assert "enforced" in winner
    assert "merged" not in output


def test_execution_uses_captured_sources_and_the_lock_anchor(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname="root"\nversion="1"\ndependencies=[]\n')
    global_file = tmp_path / "user.toml"
    global_file.write_text(
        'uploaded-prior-to = "P3D"\n[packages.foo]\nuploaded-prior-to = "P7D"\n'
    )
    snapshot = ladder.ConfigSnapshot(
        discover_layers(SourceRoots(user_toml=global_file)),
        read_env_layer({"NAB_BUILD_POLICY": "never"}),
        {},
    )
    global_file.write_text('build-policy = "build-remote"\n')
    anchor = datetime(2026, 10, 1, tzinfo=timezone.utc)
    config = read_pyproject_config(project, snapshot=snapshot, anchor=anchor)

    assert config.build_policy.value == "never"
    assert config.uploaded_prior_to == anchor - timedelta(days=3)
    assert config.package_overrides[0].uploaded_prior_to == anchor - timedelta(days=7)
    assert snapshot["uploaded-prior-to"].value == "P3D"
    assert snapshot["build-policy"].origin.kind is SourceKind.ENV


@pytest.mark.parametrize("duration", ["P7D", "P2D"])
def test_enforced_relative_cutoff_compares_against_one_inspector_anchor(
    tmp_path: Path, duration: str
) -> None:
    config_file = tmp_path / "system.toml"
    config_file.write_text(
        '[enforce]\nuploaded-prior-to = "P7D"\n'
        f'[packages.foo]\nuploaded-prior-to = "{duration}"\n'
    )
    with hooks.inspector_anchor():
        layers = discover_layers(SourceRoots(system_toml=config_file))
        if duration == "P2D":
            with pytest.raises(
                SourceConfigError, match="conflicting uploaded-prior-to"
            ):
                ladder.ConfigSnapshot(layers, read_env_layer({}), {})
        else:
            snapshot = ladder.ConfigSnapshot(layers, read_env_layer({}), {})
            anchor = datetime(2026, 10, 1, tzinfo=timezone.utc)
            effective = snapshot.at_anchor(anchor)
            assert effective["uploaded-prior-to"].value == anchor - timedelta(days=7)
            assert effective["packages"].value[
                0
            ].uploaded_prior_to == anchor - timedelta(days=7)


@pytest.mark.parametrize("source", ["system", "user", "env"])
def test_enforcement_outlasts_project_environment_and_cli(
    tmp_path: Path, source: str
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[tool.nab]\nresolution = "highest"\n')
    config_file = tmp_path / "external.toml"
    config_file.write_text('[enforce]\nresolution = "lowest"\n')
    roots = SourceRoots(
        project_dir=tmp_path,
        system_toml=config_file if source == "system" else None,
        user_toml=config_file if source == "user" else None,
    )
    environment = {"NAB_RESOLUTION": "highest"}
    if source == "env":
        environment["NAB_ENFORCE"] = '{ resolution = "lowest" }'
    effective = resolve_config(
        discover_layers(roots),
        read_env_layer(environment),
        build_cli_layer({"resolution": "highest"}),
    )

    assert effective["resolution"].value is ResolutionStrategy.LOWEST
    assert effective["resolution"].origin.enforced
    assert "enforced" in ladder.render_explain(effective, "resolution")
    assert "shadowed" in ladder.render_explain(effective, "resolution")


def test_enforcement_conflicts_are_errors_across_trusted_sources(
    tmp_path: Path,
) -> None:
    system_file = tmp_path / "system.toml"
    system_file.write_text('[enforce]\nresolution = "lowest"\n')
    layers = discover_layers(SourceRoots(system_toml=system_file))
    with pytest.raises(
        SourceConfigError, match="conflicting enforced.*system.toml.*NAB_ENFORCE"
    ):
        resolve_config(
            layers,
            read_env_layer({"NAB_ENFORCE": '{ resolution = "highest" }'}),
            build_cli_layer({}),
        )


def test_identical_enforcement_bindings_can_be_repeated(tmp_path: Path) -> None:
    config_file = tmp_path / "system.toml"
    config_file.write_text('[enforce]\nresolution="lowest"\n')
    effective = resolve_config(
        discover_layers(SourceRoots(system_toml=config_file)),
        read_env_layer({"NAB_ENFORCE": '{ resolution="lowest" }'}),
        build_cli_layer({}),
    )
    assert effective["resolution"].value is ResolutionStrategy.LOWEST
    assert sum(origin.enforced for origin, _value in effective["resolution"].stack) == 2


@pytest.mark.parametrize("setting", ["sources", "workspace-discovery"])
def test_locked_remedy_replays_external_source_selection(
    hermetic_roots: Path,
    setting: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(hermetic_roots)
    project = hermetic_roots / "pyproject.toml"
    document = '[project]\nname="root"\nversion="1"\ndependencies=[]\n'
    document += (
        "[develop.sources]\nunused={}\n"
        if setting == "sources"
        else '[tool.nab.workspace]\nmembers=["missing"]\n'
    )
    project.write_text(document)
    name = "NAB_" + setting.upper().replace("-", "_")
    monkeypatch.setenv(name, "0")
    output = hermetic_roots / "pylock.toml"
    with pytest.raises(SystemExit) as raised:
        main(["lock", "--locked", "--offline"])
    assert raised.value.code == 1
    error = capsys.readouterr().err
    remedy = error.split("run `", 1)[1].split("` first.", 1)[0]
    arguments = shlex.split(remedy)
    assert "--no-" + setting in arguments
    monkeypatch.delenv(name)
    main(arguments[1:])
    assert output.is_file()


@pytest.mark.parametrize("surface", ["pyproject", "project"])
def test_project_cannot_supply_enforcement(tmp_path: Path, surface: str) -> None:
    prefix = "tool.nab." if surface == "pyproject" else ""
    path = tmp_path / ("pyproject.toml" if surface == "pyproject" else "nab.toml")
    path.write_text(f'[{prefix}enforce]\nresolution = "highest"\n')
    with pytest.raises(SourceConfigError, match="enforce is allowed only"):
        discover_layers(SourceRoots(project_dir=tmp_path))


@pytest.mark.parametrize(
    "text", ["[]", "{ typo = true }", '{ archive = { policy = "bogus" } }', "{broken"]
)
def test_invalid_environment_enforcement_is_fatal(text: str) -> None:
    with pytest.raises(SourceConfigError, match="NAB_ENFORCE"):
        read_env_layer({"NAB_ENFORCE": text})


@pytest.mark.parametrize("table", ["packages.foo", "index.pypi"])
@pytest.mark.parametrize("enforced", [False, True])
def test_enforced_build_policy_cannot_be_relaxed_by_scoped_exceptions(
    tmp_path: Path, table: str, enforced: bool
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(f'[tool.nab.{table}]\nbuild-policy = "build-remote"\n')
    config_file = tmp_path / "system.toml"
    config_file.write_text(
        ("[enforce]\n" if enforced else "") + 'build-policy = "never"\n'
    )
    layers = discover_layers(SourceRoots(system_toml=config_file, project_dir=tmp_path))
    if enforced:
        with pytest.raises(
            SourceConfigError, match="enforces build-policy.*conflicting"
        ):
            resolve_config(layers, read_env_layer({}), build_cli_layer({}))
    else:
        effective = resolve_config(layers, read_env_layer({}), build_cli_layer({}))
        assert effective["build-policy"].value.value == "never"


@pytest.mark.parametrize("source", ["system", "user", "env"])
@pytest.mark.parametrize("command", ["lock", "download"])
@pytest.mark.parametrize("kind", ["archive", "vcs"])
def test_cli_enforced_blocks_refuse_project_selected_sources(
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
    command: str,
    kind: str,
    no_network: NoNetwork,
) -> None:
    declaration = (
        'url="https://example.org/foo-1-py3-none-any.whl",hashes={sha256="'
        + "0" * 64
        + '"}'
        if kind == "archive"
        else 'git="https://example.org/foo.git",rev="' + "0" * 40 + '"'
    )
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname="root"\nversion="1"\ndependencies=["foo"]\n'
        f"[develop.sources]\nfoo={{{declaration}}}\n"
        f'[tool.nab.{kind}]\npolicy="declared"\n'
    )
    if source == "env":
        monkeypatch.setenv("NAB_ENFORCE", f'{{ {kind} = {{ policy = "block" }} }}')
    else:
        path = (
            hermetic_roots.parent
            / ("sys" if source == "system" else "usr")
            / "nab.toml"
        )
        path.parent.mkdir()
        path.write_text(f'[enforce.{kind}]\npolicy = "block"\n')

    output = hermetic_roots / ("pylock.toml" if command == "lock" else "wheels")
    with pytest.raises(SystemExit) as raised:
        main(
            [command, str(project), "--offline", "--no-cache", "--output", str(output)]
        )
    assert raised.value.code == 1
    error = capsys.readouterr().err
    reason = 'archive.policy="block"' if kind == "archive" else 'vcs.policy is "block"'
    assert reason in error
    assert not output.exists()


@pytest.mark.parametrize("setting", ["sources", "workspace-discovery"])
@pytest.mark.parametrize("source", ["system", "user", "env"])
@pytest.mark.parametrize("command", ["lock", "download"])
@pytest.mark.parametrize("enforced", [False, True])
def test_source_selection_can_be_configured_without_forwarding_lists(
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    source: str,
    command: str,
    enforced: bool,
    no_network: NoNetwork,
) -> None:
    """External settings disable real invalid source/discovery inputs through both commands."""
    document = '[project]\nname="root"\nversion="1"\ndependencies=[]\n'
    if setting == "sources":
        document += "[develop.sources]\nunused={}\n"
    else:
        document += '[tool.nab.workspace]\nmembers=["does-not-exist"]\n'
    if enforced:
        document += f"[tool.nab]\n{setting}=true\n"
    project = hermetic_roots / "pyproject.toml"
    project.write_text(document)
    if source == "env":
        if enforced:
            monkeypatch.setenv("NAB_ENFORCE", f"{{ {setting} = false }}")
        else:
            monkeypatch.setenv("NAB_" + setting.upper().replace("-", "_"), "0")
    else:
        config_file = (
            hermetic_roots.parent
            / ("sys" if source == "system" else "usr")
            / "nab.toml"
        )
        config_file.parent.mkdir()
        config_file.write_text(
            ("[enforce]\n" if enforced else "") + f"{setting}=false\n"
        )
    output = hermetic_roots / ("pylock.toml" if command == "lock" else "wheels")
    flags = ["--" + setting] if enforced else []
    main(
        [
            command,
            str(project),
            "--offline",
            "--no-cache",
            "--output",
            str(output),
            *flags,
        ]
    )
    assert output.exists()


@pytest.mark.parametrize(
    ("setting", "argument"),
    [("sources", "use_sources"), ("workspace-discovery", "discover_workspace")],
)
def test_library_selection_cannot_relax_enforcement(
    tmp_path: Path, setting: str, argument: str
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname="root"\nversion="1"\n')
    snapshot = ladder.ConfigSnapshot(
        [], read_env_layer({"NAB_ENFORCE": f"{{ {setting}=false }}"}), {}
    )
    with pytest.raises(values.ConfigError, match="a caller cannot override"):
        read_pyproject_config(project, snapshot=snapshot, **{argument: True})


def test_new_cli_configuration_key_needs_no_command_forwarding_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    option = Opt(
        "future-choice",
        scope=Scope.PROJECT,
        kind=Kind.VALUE,
        vtype=VType.STR,
        commands=("lock",),
        default=None,
        rdefault=None,
        parse=values.parse_requires_python,
        render=hooks.render_optional_text,
        sample=">=3.11",
        help="test extension selection",
    )
    options = (*ladder.OPTIONS, option)
    monkeypatch.setattr(ladder, "OPTIONS", options)
    monkeypatch.setattr(_run, "OPTIONS", options)
    monkeypatch.setitem(BY_KEY, option.name, option)
    overrides = _run.command_overrides({"project_future_choice": ">=3.11"})
    snapshot = ladder.ConfigSnapshot([], read_env_layer({}), overrides)
    assert snapshot[option.name].value == ">=3.11"
    assert snapshot[option.name].origin.kind is SourceKind.CLI


def _enforce_source_block(
    root: Path, source: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Block a source even when its location appears in a broader allowlist."""
    fields = (
        'policy="block", allowed-urls=["https://example.org/"]'
        if kind == "archive"
        else 'policy="block", allowed-schemes=["git+https"], allowed-repos=["https://example.org/"]'
    )
    value = "{ " + fields + " }"
    if source == "env":
        monkeypatch.setenv("NAB_ENFORCE", f"{{ {kind} = {value} }}")
    else:
        path = root.parent / ("sys" if source == "system" else "usr") / "nab.toml"
        path.parent.mkdir()
        path.write_text(f"[enforce]\n{kind} = {value}\n")


def _fixture_git(repository: Path, *arguments: str) -> str:
    """Run a fixed Git command in a test-owned repository and return its output."""
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git is not installed")
    result = subprocess.run(  # noqa: S603 - fixed commands and test-owned paths only
        [executable, "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("source", ["system", "user", "env"])
@respx.mock
def test_enforced_archive_block_refuses_a_verified_warm_cache(
    hermetic_roots: Path,
    source: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = hermetic_roots / "foo-1-py3-none-any.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "foo-1.dist-info/METADATA", "Metadata-Version: 2.4\nName: foo\nVersion: 1\n"
        )
    data = artifact.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    url = "https://example.org/foo-1-py3-none-any.whl"
    route = respx.get(url).respond(200, content=data)
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname="root"\nversion="1"\ndependencies=["foo"]\n'
        f'[develop.sources]\nfoo={{url="{url}",hashes={{sha256="{digest}"}}}}\n'
    )
    cache = hermetic_roots / "cache"
    output = hermetic_roots / "pylock.toml"
    arguments = [
        "lock",
        str(project),
        "--cache-dir",
        str(cache),
        "--http-backend",
        "httpx",
        "--output",
        str(output),
    ]
    main(arguments)
    main([*arguments, "--offline"])
    assert route.call_count == 1
    (cached,) = cache.rglob(artifact.name)
    assert cached.read_bytes() == data
    locked = output.read_bytes()

    _enforce_source_block(hermetic_roots, source, "archive", monkeypatch)
    with pytest.raises(SystemExit) as raised:
        main([*arguments, "--offline"])
    assert raised.value.code == 1
    assert 'archive.policy="block"' in capsys.readouterr().err
    assert route.call_count == 1
    assert output.read_bytes() == locked
    assert cached.read_bytes() == data


@pytest.mark.parametrize("source", ["system", "user", "env"])
def test_enforced_git_block_refuses_an_offline_cached_clone(
    hermetic_roots: Path,
    source: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_network: NoNetwork,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    repository = hermetic_roots / "upstream"
    repository.mkdir()
    (repository / "pyproject.toml").write_text('[project]\nname="foo"\nversion="1"\n')
    commands = (
        ["init", "--initial-branch=main"],
        ["add", "."],
        [
            "-c",
            "user.name=Damian Shaw",
            "-c",
            "user.email=damian.peter.shaw@gmail.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Add fixture",
        ],
    )
    for command in commands:
        _fixture_git(repository, *command)
    revision = _fixture_git(repository, "rev-parse", "HEAD")
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname="root"\nversion="1"\ndependencies=["foo"]\n'
        f'[develop.sources]\nfoo={{git="{repository.as_uri()}",rev="{revision}"}}\n'
    )
    output = hermetic_roots / "pylock.toml"
    arguments = [
        "lock",
        str(project),
        "--offline",
        "--cache-dir",
        str(tmp_path_factory.mktemp("vcs")),
        "--output",
        str(output),
    ]
    main(arguments)
    repository.rename(hermetic_roots / "moved-repository")
    main(arguments)
    locked = output.read_bytes()

    _enforce_source_block(hermetic_roots, source, "vcs", monkeypatch)
    with pytest.raises(SystemExit) as raised:
        main(arguments)
    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert 'vcs.policy is "block"' in error
    assert output.read_bytes() == locked


@pytest.mark.parametrize("source", ["system", "user", "env"])
@pytest.mark.parametrize("kind", ["archive", "vcs"])
def test_global_blocks_survive_isolated_backend_dependency_resolution(
    hermetic_roots: Path,
    source: str,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_network: NoNetwork,
) -> None:
    package = hermetic_roots / "local"
    package.mkdir()
    requirement = (
        "backend @ https://example.org/backend-1-py3-none-any.whl#sha256=" + "0" * 64
        if kind == "archive"
        else "backend @ git+https://example.org/backend.git@" + "0" * 40
    )
    (package / "pyproject.toml").write_text(
        '[project]\nname="local"\nversion="1"\ndynamic=["dependencies"]\n'
        f'[build-system]\nrequires=["{requirement}"]\nbuild-backend="unreached_backend"\n'
    )
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname="root"\nversion="1"\ndependencies=["local"]\n'
        '[develop.sources]\nlocal={path="local"}\n'
    )
    clone = Mock(
        side_effect=AssertionError(
            "a blocked build requirement reached clone acquisition"
        )
    )
    monkeypatch.setattr("nab_project._sources.vcs.prepare_clone", clone)
    _enforce_source_block(hermetic_roots, source, kind, monkeypatch)
    output = hermetic_roots / "pylock.toml"
    with pytest.raises(SystemExit) as raised:
        main(["lock", str(project), "--no-cache", "--output", str(output)])
    assert raised.value.code == 1
    error = capsys.readouterr().err
    clone.assert_not_called()
    assert no_network.requests == []
    reason = 'archive.policy="block"' if kind == "archive" else 'vcs.policy is "block"'
    assert reason in error
    assert not output.exists()


@pytest.mark.parametrize("source", ["system", "user", "env"])
@pytest.mark.parametrize("enforced", [False, True])
@pytest.mark.parametrize(
    ("key", "literal", "token", "project_tail"),
    [
        ("mode", '"universal"', "universal", ""),
        (
            "matrix",
            '{ python="==3.11", platforms=["linux_x86_64"] }',
            '{ python="==3.11", platforms=["linux_x86_64"] }',
            "",
        ),
        ("environment", '{ implementation="pypy" }', '{ implementation="pypy" }', ""),
        ("base-group", '"dev"', "dev", "[dependency-groups]\ndev=[]\n"),
    ],
)
def test_external_cross_field_errors_name_the_declaring_source(
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
    enforced: bool,
    key: str,
    literal: str,
    token: str,
    project_tail: str,
) -> None:
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname="root"\nversion="1"\ndependencies=[]\n' + project_tail
    )
    if source == "env":
        identity = "NAB_ENFORCE" if enforced else "NAB_" + key.upper().replace("-", "_")
        monkeypatch.setenv(identity, f"{{ {key}={literal} }}" if enforced else token)
    else:
        path = (
            hermetic_roots.parent
            / ("sys" if source == "system" else "usr")
            / "nab.toml"
        )
        path.parent.mkdir()
        path.write_text(("[enforce]\n" if enforced else "") + f"{key}={literal}\n")
        identity = str(path)
    with pytest.raises(SystemExit) as raised:
        main(["lock", str(project), "--offline", "--no-cache"])
    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert identity in error
    assert key.upper() in error.upper()


@pytest.mark.parametrize("table", ["packages.foo", "package-rules", "index.pypi"])
@pytest.mark.parametrize(
    ("policy", "fixed", "exception"),
    [
        ("dist-policy", '"wheel-only"', 'dist-policy="sdist-only"'),
        (
            "dist-policy",
            '{ policy="wheel-or-sdist", trust-unverified-deps=false }',
            'dist-policy={ policy="wheel-or-sdist", trust-unverified-deps=true }',
        ),
        ("uploaded-prior-to", "2024-01-01T00:00:00Z", "uploaded-prior-to=false"),
    ],
)
def test_enforced_policy_exceptions_cannot_relax_other_policy_fields(
    tmp_path: Path,
    table: str,
    policy: str,
    fixed: str,
    exception: str,
) -> None:
    external = tmp_path / "policy.toml"
    external.write_text(f"[enforce]\n{policy}={fixed}\n")
    header = (
        '[[tool.nab.package-rules]]\nmatch=["foo"]'
        if table == "package-rules"
        else f"[tool.nab.{table}]"
    )
    (tmp_path / "pyproject.toml").write_text(header + "\n" + exception + "\n")
    layers = discover_layers(SourceRoots(system_toml=external, project_dir=tmp_path))
    with pytest.raises(SourceConfigError, match=f"enforces {policy}.*conflicting"):
        resolve_config(layers, read_env_layer({}), build_cli_layer({}))


def test_an_enforced_matrix_cannot_be_discarded_by_another_enforced_setting(
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = hermetic_roots / "pyproject.toml"
    project.write_text('[project]\nname="root"\nversion="1"\ndependencies=[]\n')
    system = hermetic_roots.parent / "sys" / "nab.toml"
    system.parent.mkdir()
    system.write_text(
        'mode="universal"\n[enforce.matrix]\npython="==3.11"\nplatforms=["linux_x86_64"]\n'
    )
    monkeypatch.setenv("NAB_ENFORCE", '{ mode="specific" }')
    output = hermetic_roots / "pylock.toml"
    with pytest.raises(SystemExit) as raised:
        main(["lock", str(project), "--offline", "--no-cache", "--output", str(output)])
    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert str(system) in error
    assert "mode is 'specific'" in error
    assert not output.exists()


def test_an_enforced_empty_environment_cannot_be_replaced_by_legacy_markers(
    hermetic_roots: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = hermetic_roots / "pyproject.toml"
    project.write_text(
        '[project]\nname="root"\nversion="1"\ndependencies=[]\n[tool.nab.marker-environment]\npython_version="3.11"\n'
    )
    system = hermetic_roots.parent / "sys" / "nab.toml"
    system.parent.mkdir()
    system.write_text("[enforce.environment]\n")
    output = hermetic_roots / "pylock.toml"
    with pytest.raises(SystemExit) as raised:
        main(["lock", str(project), "--offline", "--no-cache", "--output", str(output)])
    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert str(system) in error
    assert "are both set" in error
    assert not output.exists()


@pytest.mark.parametrize("source", ["system", "user", "env"])
@pytest.mark.parametrize("enforced", [False, True])
def test_external_workspace_paths_remain_relative_to_the_target_project(
    hermetic_roots: Path,
    source: str,
    enforced: bool,
    monkeypatch: pytest.MonkeyPatch,
    no_network: NoNetwork,
) -> None:
    member = hermetic_roots / "packages" / "foo"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text('[project]\nname="foo"\nversion="1"\n')
    project = hermetic_roots / "pyproject.toml"
    document = '[project]\nname="root"\nversion="1"\ndependencies=["foo"]\n'
    if enforced:
        document += '[tool.nab.workspace]\nmembers=["missing"]\n'
    project.write_text(document)
    value = '{ members=["packages/foo"] }'
    if source == "env":
        name = "NAB_ENFORCE" if enforced else "NAB_WORKSPACE"
        monkeypatch.setenv(name, f"{{ workspace={value} }}" if enforced else value)
    else:
        path = (
            hermetic_roots.parent
            / ("sys" if source == "system" else "usr")
            / "nab.toml"
        )
        path.parent.mkdir()
        path.write_text(("[enforce]\n" if enforced else "") + f"workspace={value}\n")
    invocation = hermetic_roots.parent / "invocation"
    invocation.mkdir()
    monkeypatch.chdir(invocation)
    output = hermetic_roots / "pylock.toml"
    main(["lock", str(project), "--offline", "--no-cache", "--output", str(output)])
    (package,) = toml_io.load_path(output)["packages"]
    assert package["name"] == "foo"
    assert package["directory"]["path"] == "packages/foo"
    assert no_network.requests == []
