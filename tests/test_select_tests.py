"""Exercise CI selection against the real nox install closures and Git diffs."""

from __future__ import annotations

import ast
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "nab_select_tests", REPO_ROOT / "tasks" / "select_tests.py"
)
assert _spec is not None
assert _spec.loader is not None
select_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(select_tests)
WORKSPACES = select_tests.workspace_inputs(REPO_ROOT)
GIT = shutil.which("git")
assert GIT is not None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("nab-resolver/src/nab_resolver/ranges.py", list(WORKSPACES)),
        ("nab-resolver/tests/property/test_pubgrub_resolver.py", list(WORKSPACES)),
        (
            "nab-markersets/src/nab_markersets/_packaging.py",
            ["provider", "project", "umbrella"],
        ),
        (
            "nab-provider/src/nab_provider/_vendor/packaging/ranges.py",
            ["provider", "project", "umbrella"],
        ),
        ("nab-index/src/nab_index/client.py", ["project", "umbrella"]),
        (
            "nab-project/tests/property_python/test_alignment.py",
            ["project", "umbrella"],
        ),
        ("src/nab/cli.py", ["project", "umbrella"]),
        ("tests/test_output.py", ["umbrella"]),
        ("docs/reference/cli.md", ["project", "umbrella"]),
        ("README.md", ["umbrella"]),
        ("conftest.py", list(WORKSPACES)),
        ("pyproject.toml", list(WORKSPACES)),
        ("noxfile.py", list(WORKSPACES)),
        (".github/requirements/pylock.tests.toml", list(WORKSPACES)),
        (".github/requirements/pylock.crosshair.toml", list(WORKSPACES)),
        (".github/workflows/test.yml", list(WORKSPACES)),
        ("tasks/select_tests.py", list(WORKSPACES)),
        ("new-package/src/new_package/__init__.py", list(WORKSPACES)),
        ("nab-resolver-other/file.py", list(WORKSPACES)),
    ],
)
def test_changed_path_selects_its_consumers(path: str, expected: list[str]) -> None:
    assert select_tests.select_workspaces([path], WORKSPACES) == expected


def test_combined_changes_and_empty_comparison() -> None:
    assert select_tests.select_workspaces(
        ["src/nab/cli.py", "nab-index/tests/test_client.py"], WORKSPACES
    ) == ["project", "umbrella"]
    assert select_tests.select_workspaces([], WORKSPACES) == list(WORKSPACES)
    assert select_tests.select_workspaces(
        ["src/nab/cli.py", "conftest.py"], WORKSPACES
    ) == list(WORKSPACES)


def test_install_closure_changes_affect_selection() -> None:
    workspaces = {
        **WORKSPACES,
        "host": (["nab-resolver", "nab-index"], ["host/tests"], []),
    }
    assert select_tests.select_workspaces(
        ["nab-index/src/nab_index/client.py"], workspaces
    ) == ["project", "umbrella", "host"]


def test_selected_pytest_paths_keep_downstream_properties() -> None:
    outputs = select_tests.selection_outputs(["project", "umbrella"], WORKSPACES)
    assert outputs == {
        "workspaces": "project umbrella",
        "test-paths": "nab-provider/tests nab-project/tests nab-index/tests tests",
        "resolver": "false",
    }
    outputs = select_tests.selection_outputs(list(WORKSPACES), WORKSPACES)
    assert outputs["resolver"] == "true"
    assert outputs["test-paths"].split().count("nab-provider/tests") == 1
    assert "nab-resolver/tests" in outputs["test-paths"].split()


def test_crosshair_is_owned_by_the_resolver() -> None:
    """Keep the conditional CrossHair job aligned with the marked tests it runs."""
    marked = []
    test_paths = {path for _, paths, _ in WORKSPACES.values() for path in paths}
    for path in sorted(
        path
        for directory in test_paths
        for path in (REPO_ROOT / directory).rglob("test_*.py")
    ):
        source = path.read_text(encoding="utf-8")
        if "crosshair" not in source:
            continue
        if any(
            isinstance(node, ast.Attribute)
            and node.attr == "crosshair"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
            for node in ast.walk(ast.parse(source))
        ):
            marked.append(path.relative_to(REPO_ROOT))
    assert marked
    assert all(path.is_relative_to("nab-resolver/tests") for path in marked)


def _git(root: Path, *args: str) -> str:
    """Run Git in a disposable repository, returning its stripped output."""
    assert GIT is not None
    return subprocess.check_output(  # noqa: S603 - fixed test commands in a disposable repository
        [GIT, *args], cwd=root, text=True
    ).strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.org")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "nab-resolver").mkdir()
    (tmp_path / "nab-resolver" / "old.py").write_text("original\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Initial")
    return tmp_path


@pytest.mark.parametrize("rename", [False, True])
def test_git_diff_keeps_deleted_resolver_paths(
    repository: Path, *, rename: bool
) -> None:
    _git(repository, "checkout", "-b", "topic")
    original = repository / "nab-resolver" / "old.py"
    if rename:
        (repository / "src").mkdir()
        original.rename(repository / "src" / "new.py")
    else:
        original.unlink()
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "Change paths")
    paths = select_tests.changed_paths(repository, "main", "HEAD")
    assert "nab-resolver/old.py" in paths
    assert select_tests.select_workspaces(paths, WORKSPACES) == list(WORKSPACES)
    if rename:
        assert "src/new.py" in paths


def test_git_diff_uses_merge_base(repository: Path) -> None:
    _git(repository, "checkout", "-b", "topic")
    (repository / "src").mkdir()
    (repository / "src" / "cli.py").write_text("topic\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add CLI")
    _git(repository, "checkout", "main")
    (repository / "nab-resolver" / "old.py").write_text(
        "main changed\n", encoding="utf-8"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Change resolver")
    assert select_tests.changed_paths(repository, "main", "topic") == ["src/cli.py"]


@pytest.mark.parametrize("base", [None, "missing-ref"])
def test_main_and_failed_comparisons_run_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str | None
) -> None:
    output = tmp_path / "output"
    args = ["select_tests.py", "--output", str(output)]
    if base is not None:
        args.extend(["--base", base])
    monkeypatch.setattr("sys.argv", args)
    select_tests.main()
    assert output.read_text(encoding="utf-8").splitlines() == [
        f"{key}={value}"
        for key, value in select_tests.selection_outputs(
            list(WORKSPACES), WORKSPACES
        ).items()
    ]


def test_missing_workspace_definition_fails(tmp_path: Path) -> None:
    (tmp_path / "noxfile.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="defines no WORKSPACES"):
        select_tests.workspace_inputs(tmp_path)


def test_pr_comparison_writes_selected_arguments(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repository / "noxfile.py").write_text(
        f"WORKSPACES = {WORKSPACES!r}\n", encoding="utf-8"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add workspace configuration")
    _git(repository, "checkout", "-b", "topic")
    (repository / "nab-index").mkdir()
    (repository / "nab-index" / "client.py").write_text("changed\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Change index")
    output = repository / "output"
    monkeypatch.setattr(select_tests, "REPO_ROOT", repository)
    monkeypatch.setattr(
        "sys.argv", ["select_tests.py", "--base", "main", "--output", str(output)]
    )
    select_tests.main()
    assert output.read_text(encoding="utf-8").splitlines() == [
        "workspaces=project umbrella",
        "test-paths=nab-provider/tests nab-project/tests nab-index/tests tests",
        "resolver=false",
    ]


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        "{}",
        "{1: ([], [], [])}",
        "{'resolver': ([], [])}",
        "{'resolver': ((), [], [])}",
        "{'resolver': ([1], [], [])}",
    ],
)
def test_invalid_workspace_table_fails(tmp_path: Path, value: str) -> None:
    (tmp_path / "noxfile.py").write_text(f"WORKSPACES = {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="WORKSPACES|workspace"):
        select_tests.workspace_inputs(tmp_path)
