"""Named selections use the normal CLI, solver and saved-lock checker."""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
from contextlib import nullcontext
from pathlib import Path

import pytest
from packaging.pylock import Pylock

from nab.cli import run
from nab_project import toml_io, toml_writer
from nab_provider.testing import FakeFetchPort

from .lock_registry import Registry


@pytest.fixture
def named(registry: Registry) -> Registry:
    """Declare a test plan and a build plan in the temporary project."""
    registry.project.write_text(
        '[project]\nname="app"\nversion="1"\nrequires-python=">=3.10"\n'
        '[dependency-groups]\ntest=["a"]\nrelease=["b"]\n'
        '[build-system]\nrequires=["b"]\n'
        '[tool.nab]\nbuild-policy="never"\n'
        '[tool.nab.plans.dev]\ngroups=["test"]\n'
        '[tool.nab.plans.build]\ngroups=["release"]\nbuild-requirements=true\n'
    )
    return registry


def command(value: Registry, *args: str) -> int:
    """Exercise parsing and execution without replacing the lock implementation."""
    return run(("--no-progress", "lock", str(value.project), *args))


@pytest.mark.parametrize(
    ("args", "source"),
    [
        ((), "plan 'dev': settings.requires-python"),
        (("--project-requires-python", "<3"), "--project-requires-python"),
        (
            ("--setting", 'project.requires-python="<3"'),
            "--setting project.requires-python",
        ),
    ],
)
def test_excluded_python_names_the_winning_plan_or_flag(
    named: Registry,
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
    source: str,
) -> None:
    with named.project.open("a") as project:
        project.write('[tool.nab.plans.dev.settings]\nrequires-python="<3"\n')

    assert command(named, "--plan", "dev", *args) == 1
    assert f"{source} = '<3' excludes the resolve target" in capsys.readouterr().err


def test_plan_defaults_to_its_project_directory_and_checks_without_fetching(
    named: Registry,
) -> None:
    assert command(named, "--plan", "dev") == 0
    output = named.project.with_name("pylock.dev.toml")
    saved = output.read_bytes()
    doc = toml_io.load_path(output)
    assert {p["name"]: p["version"] for p in doc["packages"]} == {"a": "1", "b": "1"}
    assert doc["tool"]["nab"]["input-path"] == str(named.project)
    assert "--plan" in doc["tool"]["nab"]["command-line"]
    assert not list(named.project.parent.glob(".nab-lock-*.toml"))

    coordinator_count = len(named.coordinators)
    assert command(named, "--plan", "dev", "--locked") == 0
    assert len(named.coordinators) == coordinator_count
    assert output.read_bytes() == saved


def test_plan_can_lock_with_a_deep_table_from_another_tool(named: Registry) -> None:
    # TOML accepts this dotted key, but recursively copying its tables exhausts
    # the Python stack while preparing a plan.
    key = ".".join(["child"] * 600)
    with named.project.open("a", encoding="utf-8") as stream:
        stream.write(f"[tool.other]\n{key}=1\n")

    assert command(named, "--plan", "dev") == 0
    output = named.project.with_name("pylock.dev.toml")
    saved = output.read_bytes()
    assert command(named, "--plan", "dev", "--locked") == 0
    assert output.read_bytes() == saved


def test_all_plans_keep_build_inputs_separate_and_reuse_relative_cutoff(
    named: Registry,
) -> None:
    named.upload_time = "2020-01-01T00:00:00Z"
    with named.project.open("a") as file:
        file.write('[tool.nab.plans.build.settings]\nuploaded-prior-to="P7D"\n')
    assert command(named, "--all-plans") == 0
    build = toml_io.load_path(named.project.with_name("pylock.build.toml"))
    assert [p["name"] for p in build["packages"]] == ["b"]
    assert "--plan" in build["tool"]["nab"]["command-line"]
    assert "--all-plans" not in build["tool"]["nab"]["command-line"]
    assert command(named, "--all-plans", "--locked") == 0
    assert command(named, "--plan", "dev", "--reproduce") == 0


def test_project_policy_and_cli_table_subflags_keep_their_precedence(
    named: Registry,
) -> None:
    with named.project.open("a") as file:
        file.write(
            '[tool.nab.matrix]\npython="==3.12"\nplatforms=["linux_x86_64","windows_amd64"]\n'
            '[tool.nab.plans.dev.settings]\nmode="specific"\n'
            '[tool.nab.plans.dev.settings.environment]\npython="3.12"\nplatform="linux_x86_64"\n'
        )
    assert command(named, "--plan", "dev", "--project-environment-python", "3.13") == 0
    doc = toml_io.load_path(named.project.with_name("pylock.dev.toml"))
    assert any('python_version == "3.13"' in env for env in doc["environments"])
    assert any('sys_platform == "linux"' in env for env in doc["environments"])


@pytest.mark.parametrize(
    ("setting", "location"),
    [
        ('matrix = {python="==3.11.4", platforms=["linux_x86_64"]}', "settings.matrix"),
        ("packages = []", "settings.package-rules"),
        ("package-rules = {}", "settings.packages"),
    ],
)
def test_plan_settings_errors_name_the_setting_in_the_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], setting: str, location: str
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname="app"\nversion="1"\n')
    (tmp_path / "nab.toml").write_text(f"[plans.My_Plan.settings]\n{setting}\n")

    assert run(("lock", str(project), "--plan", "my-plan", "--offline")) == 1
    error = capsys.readouterr().err
    assert "plan 'my-plan'" in error
    assert f"this plan's {location}" in error


def test_local_source_groups_are_prepared_without_rebinding_relative_sources(
    named: Registry,
) -> None:
    source = named.project.parent / "upstream"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname="upstream"\ndependencies=["a"]\n'
        '[project.optional-dependencies]\ntesting=["b"]\n'
        '[dependency-groups]\ntest=["upstream[testing]"]\n'
        "[build-system]\nrequires=[]\n"
    )
    with named.project.open("a") as file:
        file.write(
            '[tool.nab.input-sources.test]\npath="upstream"\n[tool.nab.plans.dev.inputs.test]\nprojects=[{groups=["test"],dependencies=true}]\n'
        )
    assert command(named, "--plan", "dev") == 0
    assert command(named, "--plan", "dev", "--locked") == 0
    (source / "pyproject.toml").write_text(
        '[project]\nname="upstream"\ndependencies=["missing"]\n[build-system]\nrequires=[]\n[dependency-groups]\ntest=[]\n'
    )
    assert command(named, "--plan", "dev", "--locked") == 1


def test_named_lock_accepts_nan_in_another_tools_settings(named: Registry) -> None:
    with named.project.open("a") as file:
        file.write("[tool.example]\nthreshold = nan\n")

    assert command(named, "--plan", "dev") == 0
    assert command(named, "--plan", "dev", "--locked") == 0


@pytest.mark.parametrize("format_name", ["pylock", "requirements-without-hashes"])
@pytest.mark.parametrize(
    ("old", "new"),
    [('test=["a"]', 'test=["b"]'), ("dependencies=true", "dependencies=1")],
)
def test_source_change_during_resolution_preserves_the_previous_lock(
    named: Registry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    format_name: str,
    old: str,
    new: str,
) -> None:
    named.project.write_text(
        named.project.read_text().replace(
            "[tool.nab.plans.dev]", "[tool.nab.plans.dev]\ndependencies=true"
        )
    )
    args = ("--plan", "dev", "--format", format_name)
    assert command(named, *args) == 0
    output = named.project.with_name(
        "pylock.dev.toml" if format_name == "pylock" else "dev.requirements.txt"
    )
    previous = output.read_bytes()
    original = named.coordinator

    def changed(transport: object, **kwargs: object) -> nullcontext[FakeFetchPort]:
        result = original(transport, **kwargs)
        named.project.write_text(named.project.read_text().replace(old, new))
        return result

    monkeypatch.setattr("nab_project.resolve.FetchCoordinator", changed)
    assert command(named, *args) == 1
    assert output.read_bytes() == previous
    assert "inputs changed" in capsys.readouterr().err


def test_archive_inputs_follow_offline_cache_and_backend_controls(
    named: Registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    buffer = io.BytesIO()
    metadata = (
        b'[project]\nname="upstream"\ndependencies=["a"]\n[build-system]\nrequires=[]\n'
    )
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("release/pyproject.toml")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
    data = buffer.getvalue()
    with named.project.open("a") as file:
        file.write(
            '[tool.nab.input-sources.test]\nurl="https://source.example/release.tar.gz"\n'
            f'sha256="{hashlib.sha256(data).hexdigest()}"\n'
            "[tool.nab.plans.dev.inputs.test]\n"
        )
    calls = []

    def fetch(url: str, backend: str) -> bytes:
        calls.append((url, backend))
        return data

    monkeypatch.setattr("nab._lock._download_input", fetch)
    cache = named.project.parent / "input-cache"
    args = ("--plan", "dev", "--cache-dir", str(cache), "--http-backend", "httpx")
    assert command(named, *args, "--offline") == 1
    assert not calls
    assert "not cached" in capsys.readouterr().err

    assert command(named, *args) == 0
    assert calls == [("https://source.example/release.tar.gz", "httpx")]
    assert command(named, *args, "--locked", "--offline") == 0
    assert len(calls) == 1
    assert command(named, *args, "--offline", "--no-cache") == 1
    assert len(calls) == 1


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--plan", "missing"), id="unknown-lock"),
        pytest.param(("--plan", "dev", "--all-plans"), id="mixed-selections"),
        pytest.param(
            ("--all-plans", "--setting", 'user.plan="dev"'), id="plan-setting-and-all"
        ),
        pytest.param(
            ("--all-plans", "--output", "pylock.other.toml"), id="batch-output"
        ),
    ],
)
def test_invalid_selections_report_errors_without_a_traceback(
    named: Registry, capsys: pytest.CaptureFixture[str], args: tuple[str, ...]
) -> None:
    assert command(named, *args) == 1
    assert "Traceback" not in capsys.readouterr().err
    assert not named.coordinators
    assert not list(named.project.parent.glob("pylock.*.toml"))


def test_all_plans_overrides_an_environment_default(
    named: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NAB_PLAN", "dev")
    assert command(named, "--all-plans") == 0
    assert named.project.with_name("pylock.dev.toml").is_file()
    assert named.project.with_name("pylock.build.toml").is_file()


def test_invalid_plan_name_identifies_the_setting(
    named: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command(named, "--plan", "bad name") == 1
    assert "--plan 'bad name' is not a valid lock plan name" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("declaration", "problem"),
    [
        ('[tool.nab.plans.build.settings]\nstrategy="lowest"', "strategy"),
        ("[tool.nab.plans.build.settings]\nresolution=42", "resolution"),
        ('[tool.nab.plans.build.inputs.missing]\ngroups=["test"]', "missing"),
    ],
)
def test_invalid_later_plan_preserves_existing_locks(
    named: Registry, capsys: pytest.CaptureFixture[str], declaration: str, problem: str
) -> None:
    assert command(named, "--plan", "dev") == 0
    lock = named.project.with_name("pylock.dev.toml")
    previous = lock.read_bytes()
    coordinator_count = len(named.coordinators)
    named.add_release()
    with named.project.open("a") as file:
        file.write(declaration + "\n")

    assert command(named, "--all-plans") == 1
    assert lock.read_bytes() == previous
    assert len(named.coordinators) == coordinator_count
    message = capsys.readouterr().err
    assert "'build'" in message
    assert problem in message

    assert command(named, "--plan", "dev") == 0
    assert {p["version"] for p in toml_io.load_path(lock)["packages"]} == {"2"}


def test_batch_output_collisions_fail_before_writing(named: Registry) -> None:
    named.project.write_text(
        named.project.read_text()
        .replace('groups=["test"]', 'groups=["test"]\noutput="pylock.shared.toml"')
        .replace(
            'groups=["release"]', 'groups=["release"]\noutput="pylock.shared.toml"'
        )
    )
    assert command(named, "--all-plans") == 1
    assert not named.coordinators
    assert not named.project.with_name("pylock.shared.toml").exists()


def test_plain_group_selection_ignores_foreign_inputs(
    named: Registry,
) -> None:
    source = named.project.parent / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname="source"\ndependencies=["b"]\n[build-system]\nrequires=[]\n'
    )
    with named.project.open("a") as file:
        file.write('[tool.nab.input-sources.test]\npath="absent"\n')
    assert command(named, "--groups", "test", "--output", str(named.output)) == 0
    assert (
        command(named, "--groups", "test", "--locked", "--output", str(named.output))
        == 0
    )


def test_named_lock_rejects_an_override_before_replacing_the_file(
    named: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert command(named, "--plan", "dev") == 0
    assert command(named, "--plan", "dev", "--groups", "release") == 1
    document = toml_io.load_path(named.project.with_name("pylock.dev.toml"))
    assert [p["name"] for p in document["packages"]] == ["a", "b"]


def test_plan_with_an_explicit_output_and_requirements_format(
    named: Registry,
) -> None:
    output = named.project.parent / "dev.requirements.txt"
    assert (
        command(
            named,
            "--plan",
            "dev",
            "--format",
            "requirements-without-hashes",
            "--output",
            str(output),
        )
        == 0
    )
    assert output.read_text().splitlines() == ["a==1", "b==1"]
    assert (
        command(named, "--plan", "dev", "--format", "requirements-without-hashes") == 0
    )
    assert named.project.with_name("dev.requirements.txt").exists()


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param("offline=true", id="user-policy-in-project"),
        pytest.param("unknown=true", id="unknown-setting"),
        pytest.param('default-groups=["test"]', id="selection-in-settings"),
    ],
)
def test_named_project_policy_refusals_are_reported(
    named: Registry, settings: str
) -> None:
    with named.project.open("a") as file:
        file.write("[tool.nab.plans.dev.settings]\n" + settings + "\n")
    assert command(named, "--plan", "dev") == 1
    assert not named.coordinators


def test_a_script_cannot_select_a_plan(named: Registry) -> None:
    script = named.project.parent / "tool.py"
    script.write_text("# /// script\n# dependencies=[]\n# ///\n")
    assert run(("lock", "--script", str(script), "--plan", "dev")) == 1
    assert command(named, "--plan", "dev", "--build-requirements") == 1


def test_configured_http_download_uses_the_selected_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from nab._lock import _download_input
    from nab_index.response import HttpxResponse

    calls = []

    class Transport:
        async def get(self, url: str, **kwargs: object) -> HttpxResponse:
            calls.append(url)
            return HttpxResponse(
                httpx.Response(200, request=httpx.Request("GET", url)), b"archive"
            )

        async def aclose(self) -> None:
            calls.append("closed")

    def transport(backend: str) -> Transport:
        calls.append(backend)
        return Transport()

    monkeypatch.setattr("nab._lock._make_resolve_transport", transport)

    async def fetch() -> None:
        assert (
            _download_input("https://source.example/a.tar.gz", "urllib3") == b"archive"
        )

    asyncio.run(fetch())
    assert calls == ["urllib3", "https://source.example/a.tar.gz", "closed"]


def test_batch_provenance_replays_each_individual_plan(
    named: Registry,
) -> None:
    assert command(named, "--all-plans") == 0
    build = toml_io.load_path(named.project.with_name("pylock.build.toml"))
    arguments = build["tool"]["nab"]["command-line"]
    assert arguments[arguments.index("--plan") + 1] == "build"


def test_plan_records_and_replays_the_selected_upgrade(named: Registry) -> None:
    assert command(named, "--plan", "dev") == 0
    named.add_release()
    named.declarations["a"]["2"] = ("b>=1",)

    assert command(named, "--plan", "dev", "--upgrade-package", "a") == 0

    output = named.project.with_name("pylock.dev.toml")
    document = toml_io.load_path(output)
    assert {p["name"]: p["version"] for p in document["packages"]} == {
        "a": "2",
        "b": "1",
    }
    named.declarations["a"]["3"] = ("b>=1",)
    assert run(tuple(document["tool"]["nab"]["command-line"][1:])) == 0
    document = toml_io.load_path(output)
    assert {p["name"]: p["version"] for p in document["packages"]} == {
        "a": "3",
        "b": "1",
    }


@pytest.mark.parametrize("output", ['"-"', "3"])
def test_batch_refuses_outputs_that_are_not_files(named: Registry, output: str) -> None:
    named.project.write_text(
        named.project.read_text().replace(
            'groups=["test"]',
            f'groups=["test"]\noutput={output}',
        )
    )
    assert command(named, "--all-plans") == 1
    assert not named.coordinators


@pytest.fixture
def features(named: Registry) -> Registry:
    """Publish independent optional features through the normal resolver boundary."""
    named.declarations = {"runtime": {"1": ()}, "httpx": {"1": ()}, "rich": {"1": ()}}
    named.project.write_text(
        '[project]\nname="app"\nversion="1"\nrequires-python=">=3.10"\n'
        'dependencies=["runtime"]\n'
        '[project.optional-dependencies]\nhttp=["httpx"]\ncli=["rich"]\n'
        '[tool.nab]\nbuild-policy="never"\n'
        '[tool.nab.plans.http]\nextras=["http"]\n'
        '[tool.nab.plans.cli]\nextras=["cli"]\n'
    )
    return named


def test_feature_plans_record_and_validate_separate_extra_selections(
    features: Registry,
) -> None:
    assert command(features, "--all-plans") == 0
    for name, dependency in [("http", "httpx"), ("cli", "rich")]:
        document = toml_io.load_path(features.project.with_name(f"pylock.{name}.toml"))
        assert not document.get("extras")
        assert {package["name"] for package in document["packages"]} == {
            "runtime",
            dependency,
        }
    assert command(features, "--all-plans", "--locked") == 0
    assert command(features, "--plan", "http", "--reproduce") == 0


def test_user_selection_defaults_do_not_change_a_named_recipe(
    features: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAB_EXTRAS", '["cli"]')
    monkeypatch.setenv("NAB_ALL_EXTRAS", "true")
    monkeypatch.setenv("NAB_GROUPS", '["missing"]')
    monkeypatch.setenv("NAB_ALL_GROUPS", "true")
    assert command(features, "--plan", "http") == 0
    document = toml_io.load_path(features.project.with_name("pylock.http.toml"))
    assert not document.get("extras")
    assert {package["name"] for package in document["packages"]} == {"runtime", "httpx"}


@pytest.mark.parametrize(
    "arguments",
    [("--groups", "test", "--all-groups"), ("--extras", "http", "--all-extras")],
)
def test_named_plans_refuse_conflicting_explicit_selection_controls(
    named: Registry,
    arguments: tuple[str, ...],
) -> None:
    assert command(named, "--plan", "dev", *arguments) == 1
    assert not named.coordinators


def test_a_shared_docs_group_works_without_runtime_or_backend_metadata(
    named: Registry,
) -> None:
    named.declarations["sphinx"] = {"1": ()}
    source = named.project.parent / "shared-tools"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname="tools"\ndynamic=["dependencies"]\n'
        '[build-system]\nrequires="unselected backend metadata"\n'
        '[dependency-groups]\ndocs=["sphinx"]\n'
    )
    with named.project.open("a") as file:
        file.write(
            '[tool.nab.input-sources.docs]\npath="shared-tools"\n'
            '[tool.nab.plans.docs]\ninputs.docs={groups=["docs"]}\n'
        )
    assert command(named, "--plan", "docs") == 0
    document = toml_io.load_path(named.project.with_name("pylock.docs.toml"))
    assert [package["name"] for package in document["packages"]] == ["sphinx"]
    assert command(named, "--plan", "docs", "--locked") == 0


def test_plain_requirements_can_supply_a_group_without_a_source_project(
    named: Registry,
) -> None:
    named.declarations["rich"] = {"1": ()}
    source = named.project.parent / "shared-config"
    source.mkdir()
    (source / "requirements.txt").write_text("rich\n")
    with named.project.open("a") as file:
        file.write(
            '[tool.nab.input-sources.tools]\npath="shared-config"\n'
            '[tool.nab.plans.tools]\ninputs.tools={requirements-files=["requirements.txt"]}\n'
        )
    assert command(named, "--plan", "tools") == 0
    document = toml_io.load_path(named.project.with_name("pylock.tools.toml"))
    assert [package["name"] for package in document["packages"]] == ["rich"]
    assert command(named, "--plan", "tools", "--locked") == 0


def test_plan_names_in_nab_toml_select_the_same_cli_plan(features: Registry) -> None:
    document = toml_io.load_path(features.project)
    plans = document["tool"]["nab"].pop("plans")
    features.project.write_text(toml_writer.dumps(document))
    config = features.project.with_name("nab.toml")
    config.write_text(toml_writer.dumps({"plans": plans}))

    assert command(features, "--plan", "http") == 0
    output = toml_io.load_path(features.project.with_name("pylock.http.toml"))
    assert not output.get("extras")
    assert {package["name"] for package in output["packages"]} == {"runtime", "httpx"}
    assert command(features, "--plan", "http", "--locked") == 0


@pytest.mark.parametrize("dynamic", [False, True])
def test_tool_plan_can_exclude_application_runtime(
    named: Registry, *, dynamic: bool
) -> None:
    document = toml_io.load_path(named.project)
    if dynamic:
        document["project"]["dynamic"] = ["dependencies"]
    else:
        document["project"]["dependencies"] = ["unavailable-runtime"]
    document["tool"]["nab"]["plans"]["dev"]["dependencies"] = False
    named.project.write_text(toml_writer.dumps(document))
    assert command(named, "--plan", "dev") == 0
    lock = toml_io.load_path(named.project.with_name("pylock.dev.toml"))
    assert {p["name"] for p in lock["packages"]} == {"a", "b"}
    assert command(named, "--plan", "dev", "--locked") == 0


@pytest.mark.parametrize("dependencies", [True, False])
def test_build_plan_can_explicitly_select_runtime(
    named: Registry, dependencies: bool
) -> None:
    document = toml_io.load_path(named.project)
    named.declarations["runtime"] = {"1": ()}
    document["project"]["dependencies"] = ["runtime"]
    document["tool"]["nab"]["plans"]["build"]["dependencies"] = dependencies
    named.project.write_text(toml_writer.dumps(document))
    assert command(named, "--plan", "build") == 0
    lock = toml_io.load_path(named.project.with_name("pylock.build.toml"))
    assert {p["name"] for p in lock["packages"]} == (
        {"b", "runtime"} if dependencies else {"b"}
    )
    assert command(named, "--plan", "build", "--locked") == 0


def test_invalid_runtime_selection_is_rejected(named: Registry) -> None:
    document = toml_io.load_path(named.project)
    document["tool"]["nab"]["plans"]["dev"]["dependencies"] = "false"
    named.project.write_text(toml_writer.dumps(document))
    assert command(named, "--plan", "dev") == 1
    assert not named.coordinators


def test_separate_plans_lock_incompatible_tool_environments(named: Registry) -> None:
    document = toml_io.load_path(named.project)
    named.declarations = {"a": {"1": ("b==1",)}, "b": {"1": (), "2": ()}}
    document["dependency-groups"]["release"] = ["b==2"]
    document["tool"]["nab"]["plans"] = {
        "test": {"groups": ["test"], "dependencies": False},
        "release": {"groups": ["release"], "dependencies": False},
    }
    named.project.write_text(toml_writer.dumps(document))
    assert command(named, "--all-plans") == 0
    for plan, expected in [("test", {"a": "1", "b": "1"}), ("release", {"b": "2"})]:
        lock = Pylock.from_dict(
            toml_io.load_path(named.project.with_name(f"pylock.{plan}.toml"))
        )
        assert {p.name: str(p.version) for p, _ in lock.select()} == expected
    assert command(named, "--all-plans", "--locked") == 0


def test_tool_plan_ignores_default_groups_and_detects_runtime_selection_changes(
    named: Registry,
) -> None:
    document = toml_io.load_path(named.project)
    named.declarations = {
        "runtime": {"1": ()},
        "tool": {"1": ()},
        "bootstrap": {"1": ()},
    }
    document["project"]["dependencies"] = ["runtime"]
    document["dependency-groups"] = {"lint": ["tool"], "bootstrap": ["bootstrap"]}
    document["tool"]["nab"]["default-groups"] = ["bootstrap"]
    document["tool"]["nab"]["plans"]["dev"] = {
        "groups": ["lint"],
        "dependencies": False,
    }
    named.project.write_text(toml_writer.dumps(document))

    assert command(named, "--plan", "dev") == 0
    output = named.project.with_name("pylock.dev.toml")
    saved = output.read_bytes()
    assert {p["name"] for p in toml_io.load_path(output)["packages"]} == {
        "tool",
    }
    assert command(named, "--plan", "dev", "--locked") == 0

    document["tool"]["nab"]["plans"]["dev"]["dependencies"] = True
    named.project.write_text(toml_writer.dumps(document))
    assert command(named, "--plan", "dev", "--locked") == 1
    assert output.read_bytes() == saved
    assert command(named, "--plan", "dev") == 0
    assert {p["name"] for p in toml_io.load_path(output)["packages"]} == {
        "tool",
        "runtime",
    }


def test_complete_plans_install_their_selection_without_installer_flags(
    named: Registry,
) -> None:
    assert command(named, "--plan", "dev") == 0
    lock = Pylock.from_dict(
        toml_io.load_path(named.project.with_name("pylock.dev.toml"))
    )
    assert {p.name for p, _ in lock.select()} == {"a", "b"}
    assert not lock.dependency_groups


def test_complete_plan_refuses_declared_alternatives_before_resolving(
    named: Registry,
) -> None:
    document = toml_io.load_path(named.project)
    document["tool"]["nab"]["conflicts"] = [[{"group": "test"}, {"group": "release"}]]
    document["tool"]["nab"]["plans"]["dev"]["groups"] = ["test", "release"]
    named.project.write_text(toml_writer.dumps(document))
    assert command(named, "--plan", "dev") == 1
    assert not named.coordinators
    document["tool"]["nab"]["plans"]["dev"]["groups"] = ["test"]
    named.project.write_text(toml_writer.dumps(document))
    assert command(named, "--plan", "dev") == 0
    lock = Pylock.from_dict(
        toml_io.load_path(named.project.with_name("pylock.dev.toml"))
    )
    assert {p.name for p, _ in lock.select()} == {"a", "b"}


def test_complete_feature_plan_binds_extra_markers_for_default_install(
    features: Registry,
) -> None:
    document = toml_io.load_path(features.project)
    document["project"]["optional-dependencies"]["http"] = [
        "httpx; extra == 'http'",
        "missing; extra == 'other'",
    ]
    features.project.write_text(toml_writer.dumps(document))
    assert command(features, "--plan", "http") == 0
    lock = Pylock.from_dict(
        toml_io.load_path(features.project.with_name("pylock.http.toml"))
    )
    assert {p.name for p, _ in lock.select()} == {"runtime", "httpx"}
    assert command(features, "--plan", "http", "--locked") == 0


def test_named_plan_selects_its_own_build_roots(
    named: Registry,
) -> None:
    document = toml_io.load_path(named.project)
    named.declarations["runtime"] = {"1": ()}
    document["project"]["dependencies"] = ["runtime"]
    document["dependency-groups"]["test"] = []
    document["tool"]["nab"].update({"base-group": "runtime", "build-group": "backend"})
    named.project.write_text(toml_writer.dumps(document))
    assert command(named, "--plan", "dev") == 0
    lock = Pylock.from_dict(
        toml_io.load_path(named.project.with_name("pylock.dev.toml"))
    )
    assert {p.name for p, _ in lock.select()} == {"runtime"}
    assert command(named, "--plan", "dev", "--locked") == 0


def test_named_plan_refuses_partial_extras_but_still_locks_runtime(
    features: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command(features, "--plan", "http") == 0
    output = features.project.with_name("pylock.http.toml")
    saved = output.read_bytes()
    document = toml_io.load_path(features.project)
    document["project"]["dynamic"] = ["optional-dependencies"]
    features.project.write_text(toml_writer.dumps(document))

    assert command(features, "--plan", "http", "--locked") == 1
    assert "optional-dependencies" in capsys.readouterr().err
    assert output.read_bytes() == saved
    assert command(features, "--plan", "http") == 1
    assert output.read_bytes() == saved

    del document["tool"]["nab"]["plans"]["http"]["extras"]
    features.project.write_text(toml_writer.dumps(document))
    assert command(features, "--plan", "http") == 0
    assert {pkg["name"] for pkg in toml_io.load_path(output)["packages"]} == {"runtime"}


@pytest.mark.parametrize("build", [False, True])
def test_complete_plan_selects_extras_independently_of_runtime(
    features: Registry, *, build: bool
) -> None:
    document = toml_io.load_path(features.project)
    document["project"].pop("dependencies")
    document["project"]["dynamic"] = ["dependencies"]
    document["build-system"] = {"requires": ["rich"]}
    document["tool"]["nab"]["plans"]["http"].update(
        {"dependencies": False, "build-requirements": build}
    )
    features.project.write_text(toml_writer.dumps(document))
    assert command(features, "--plan", "http") == 0
    lock = Pylock.from_dict(
        toml_io.load_path(features.project.with_name("pylock.http.toml"))
    )
    assert {p.name for p, _ in lock.select()} == (
        {"httpx", "rich"} if build else {"httpx"}
    )
    assert command(features, "--plan", "http", "--locked") == 0


def test_unknown_input_leaves_existing_lock_in_place(named: Registry) -> None:
    assert command(named, "--plan", "dev") == 0
    path = named.project.with_name("pylock.dev.toml")
    saved = path.read_bytes()
    with named.project.open("a") as file:
        file.write('[tool.nab.plans.dev.inputs.missing]\ngroups=["test"]\n')
    assert command(named, "--plan", "dev") == 1
    assert path.read_bytes() == saved
