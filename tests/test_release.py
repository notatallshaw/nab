"""Tests for the release tooling in tasks/release.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.version import InvalidVersion

pytest.importorskip("tomlkit")

_ROOT = Path(__file__).resolve().parents[1]
_PATH = _ROOT / "tasks" / "release.py"
_spec = importlib.util.spec_from_file_location("nab_release_tasks", _PATH)
assert _spec is not None
assert _spec.loader is not None
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

# Spelled out rather than read off the parser, so a reworded summary fails here.
_MAKE_SUMMARY = "Branch, bump, tag, and push a release, then open the PR yourself."
_CHECK_SUMMARY = (
    "Verify the working tree matches a release tag (run by the publish workflow)."
)

_MakeCall = tuple[str, str | None, bool, bool]


@pytest.fixture
def wide_help(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the terminal so argparse stops wrapping help text mid-sentence."""
    monkeypatch.setenv("COLUMNS", "200")


@pytest.fixture
def make_calls(monkeypatch: pytest.MonkeyPatch) -> list[_MakeCall]:
    """Replace ``_make`` with a recorder and return the list it appends to."""
    calls: list[_MakeCall] = []

    def recorder(
        version: str, next_dev: str | None, *, assume_yes: bool, push: bool
    ) -> None:
        calls.append((version, next_dev, assume_yes, push))

    monkeypatch.setattr(release, "_make", recorder)
    return calls


def _make_tree(tmp_path: Path, version: str, pin: str) -> tuple[Path, Path]:
    root = tmp_path / "pyproject.toml"
    member = tmp_path / "nab-project" / "pyproject.toml"
    member.parent.mkdir()
    root.write_text(
        f'[project]\nname = "nab"\nversion = "{version}"\n'
        f'dependencies = ["nab-project=={pin}"]\n',
        encoding="utf-8",
    )
    member.write_text(
        f'[project]\nname = "nab-project"\nversion = "{version}"\n'
        'dependencies = ["typing_extensions>=4.6"]\n',
        encoding="utf-8",
    )
    return root, member


def _use_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    pin: str,
    literal: str | None = None,
) -> None:
    """Build a workspace tree and point release.py's path constants at it.

    ``literal`` is the version the tree's version module holds, defaulting to
    the version its manifests hold.
    """
    root, member = _make_tree(tmp_path, version, pin)
    module = tmp_path / "_version.py"
    module.write_text(release._version_module(literal or version), encoding="utf-8")
    monkeypatch.setattr(release, "PYPROJECT_PATHS", (root, member))
    monkeypatch.setattr(release, "VERSION_LITERAL_PATHS", (module,))


def _version_modules_on_disk() -> set[Path]:
    """Return every ``_version.py`` under the workspace's shipped source trees."""
    roots = [release.REPO_ROOT / "src"]
    roots += [release.REPO_ROOT / name / "src" for name in release.WORKSPACE_PACKAGES]
    return {path for root in roots for path in root.rglob("_version.py")}


def test_cross_pin() -> None:
    assert release.cross_pin("0.0.3") == "==0.0.3"


def test_is_dev_version() -> None:
    assert release.is_dev_version("0.0.4.dev0")
    assert not release.is_dev_version("0.0.3")
    assert not release.is_dev_version("0.0.3a0")
    assert not release.is_dev_version("not-a-version")


def test_is_release_version() -> None:
    assert release.is_release_version("0.0.3")
    assert release.is_release_version("0.0.3rc1")
    assert not release.is_release_version("0.0.3.dev0")
    assert not release.is_release_version("0.0.3+local")
    assert not release.is_release_version("not-a-version")


def test_next_dev_version() -> None:
    assert release.next_dev_version("0.0.3") == "0.0.4.dev0"
    assert release.next_dev_version("1.2.0") == "1.2.1.dev0"


def test_next_dev_version_rejects_invalid() -> None:
    with pytest.raises(InvalidVersion):
        release.next_dev_version("not-a-version")


def test_rewrite_requirement_updates_workspace_pins() -> None:
    assert (
        release._rewrite_requirement("nab-resolver==0.0.2", "0.0.3")
        == "nab-resolver==0.0.3"
    )
    assert (
        release._rewrite_requirement("nab-index[httpx]==0.0.2", "0.0.3")
        == "nab-index[httpx]==0.0.3"
    )


def test_rewrite_requirement_leaves_third_party_alone() -> None:
    assert release._rewrite_requirement("tyro>=1.0", "0.0.3") == "tyro>=1.0"
    assert release._rewrite_requirement("packaging>=24.0", "0.0.3") == "packaging>=24.0"


def test_apply_version_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, member = _make_tree(tmp_path, "0.0.2", "0.0.2")
    literals = (tmp_path / "one_version.py", tmp_path / "two_version.py")
    monkeypatch.setattr(release, "PYPROJECT_PATHS", (root, member))
    monkeypatch.setattr(release, "VERSION_LITERAL_PATHS", literals)

    release.apply_version("0.0.3")

    assert release.read_current_version() == "0.0.3"
    assert '"nab-project==0.0.3"' in root.read_text(encoding="utf-8")
    assert "typing_extensions>=4.6" in member.read_text(encoding="utf-8")

    written = release._version_module("0.0.3")
    assert [path.read_text(encoding="utf-8") for path in literals] == [written] * 2


def test_version_module_normalizes_the_version() -> None:
    """A non-normalized release is written the way the metadata will spell it."""
    assert release._version_module("1.0.0-rc1").endswith('__version__ = "1.0.0rc1"\n')


def test_version_literal_paths_names_every_version_module() -> None:
    """The constant names every version module the source trees hold."""
    assert set(release.VERSION_LITERAL_PATHS) == _version_modules_on_disk()


@pytest.mark.parametrize(
    "path", release.VERSION_LITERAL_PATHS, ids=lambda p: p.parent.name
)
def test_version_modules_hold_what_release_would_write(path: Path) -> None:
    """Each checked-in version module is byte for byte what a release writes."""
    expected = release._version_module(release.read_current_version())
    assert path.read_text(encoding="utf-8") == expected


def test_read_current_version_rejects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text('[project]\nname = "a"\nversion = "0.0.2"\n', encoding="utf-8")
    b.write_text('[project]\nname = "b"\nversion = "0.0.3"\n', encoding="utf-8")
    monkeypatch.setattr(release, "PYPROJECT_PATHS", (a, b))
    with pytest.raises(ValueError, match="not in lockstep"):
        release.read_current_version()


def test_check_release_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_tree(tmp_path, monkeypatch, "0.0.3", "0.0.3")
    release.check_release("v0.0.3")


def test_check_release_rejects_missing_v(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tree(tmp_path, monkeypatch, "0.0.3", "0.0.3")
    with pytest.raises(SystemExit, match="must start with"):
        release.check_release("0.0.3")


def test_check_release_rejects_tag_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tree(tmp_path, monkeypatch, "0.0.3", "0.0.3")
    with pytest.raises(SystemExit, match="does not match"):
        release.check_release("v0.0.4")


def test_check_release_rejects_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tree(tmp_path, monkeypatch, "0.0.3.dev0", "0.0.3.dev0")
    with pytest.raises(SystemExit, match="dev version"):
        release.check_release("v0.0.3.dev0")


def test_check_release_rejects_stale_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tree(tmp_path, monkeypatch, "0.0.3", "0.0.2")
    with pytest.raises(SystemExit, match="pinned"):
        release.check_release("v0.0.3")


def test_check_release_rejects_stale_version_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tree(tmp_path, monkeypatch, "0.0.3", "0.0.3", literal="0.0.2")
    with pytest.raises(SystemExit, match="version literal"):
        release.check_release("v0.0.3")


def test_plan_returns_branch_and_tag() -> None:
    assert release._plan("0.0.3", "0.0.3.dev0", None) == (
        "0.0.3",
        "0.0.4.dev0",
        "release/0.0.3",
        "v0.0.3",
    )


def test_plan_respects_explicit_next_dev() -> None:
    assert release._plan("0.0.3", "0.0.3.dev0", "0.1.0.dev0") == (
        "0.0.3",
        "0.1.0.dev0",
        "release/0.0.3",
        "v0.0.3",
    )


def test_plan_rejects_non_release_version() -> None:
    with pytest.raises(SystemExit, match="not a release version"):
        release._plan("0.0.3.dev0", "0.0.2.dev0", None)


def test_plan_rejects_non_dev_current() -> None:
    with pytest.raises(SystemExit, match="expected a .dev"):
        release._plan("0.0.3", "0.0.2", None)


def test_plan_rejects_bad_next_dev() -> None:
    with pytest.raises(SystemExit, match="not a dev version"):
        release._plan("0.0.3", "0.0.3.dev0", "0.1.0")


def test_github_slug() -> None:
    assert (
        release._github_slug("https://github.com/notatallshaw/nab.git")
        == "notatallshaw/nab"
    )
    assert (
        release._github_slug("git@github.com:notatallshaw/nab.git")
        == "notatallshaw/nab"
    )
    assert release._github_slug("https://gitlab.com/x/y.git") is None


def test_main_make_defaults_to_pushing_and_asking_first(
    make_calls: list[_MakeCall],
) -> None:
    release.main(["make", "0.0.3"])

    assert make_calls == [("0.0.3", None, False, True)]


def test_main_make_reads_every_flag(make_calls: list[_MakeCall]) -> None:
    release.main(["make", "0.0.3", "--next-dev", "0.1.0.dev0", "--yes", "--no-push"])

    assert make_calls == [("0.0.3", "0.1.0.dev0", True, False)]


def test_main_check_forwards_the_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    tags: list[str] = []
    monkeypatch.setattr(release, "check_release", tags.append)

    release.main(["check", "v0.0.3"])

    assert tags == ["v0.0.3"]


def test_main_rejects_a_missing_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        release.main([])

    assert error.value.code == 2
    assert "the following arguments are required: command" in capsys.readouterr().err


def test_main_rejects_make_without_a_version(make_calls: list[_MakeCall]) -> None:
    with pytest.raises(SystemExit) as error:
        release.main(["make"])

    assert error.value.code == 2
    assert make_calls == []


def test_main_rejects_abbreviated_options(make_calls: list[_MakeCall]) -> None:
    """A prefix must not resolve, or a new option could change what a flag means."""
    for argv in (["--he"], ["make", "0.0.3", "--next-de", "0.1.0.dev0"]):
        with pytest.raises(SystemExit) as error:
            release.main(argv)
        assert error.value.code == 2

    assert make_calls == []


def test_hatch_and_the_publish_workflow_call_make_and_check() -> None:
    """Pin both callers verbatim, so a change to either has to come back here."""
    hatch = (_ROOT / "hatch.toml").read_text(encoding="utf-8")
    workflow = (_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert 'make = ["_sync", "python tasks/release.py make {args}"]' in hatch
    assert 'python tasks/release.py check "${RELEASE_TAG}"' in workflow


@pytest.mark.usefixtures("wide_help")
def test_top_level_help_summarizes_both_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        release.main(["--help"])

    assert error.value.code == 0
    printed = capsys.readouterr().out
    assert _MAKE_SUMMARY in printed
    assert _CHECK_SUMMARY in printed


@pytest.mark.usefixtures("wide_help")
def test_subcommand_help_documents_every_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in ("make", "check"):
        with pytest.raises(SystemExit) as error:
            release.main([command, "--help"])
        assert error.value.code == 0

    printed = capsys.readouterr().out
    for documented in (
        _MAKE_SUMMARY,
        "Version to release, for example 0.0.3.",
        "Development version to return main to.",
        "Skip the confirmation.",
        "Build the branch and tag locally.",
        _CHECK_SUMMARY,
        "Release tag to verify, for example v0.0.3.",
    ):
        assert documented in printed


@pytest.mark.usefixtures("wide_help")
def test_the_script_parses_the_argv_it_is_run_with() -> None:
    """Both callers run the file, so ``main`` has to read ``sys.argv`` itself."""
    result = subprocess.run(  # noqa: S603 - this interpreter, the repo's own script
        [sys.executable, str(_PATH), "make", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert _MAKE_SUMMARY in result.stdout
