"""Tests for the import-boundary checker in tasks/check_boundaries.py.

A passing run over a clean workspace says nothing about whether the rules fire,
so these point the checker at synthetic trees that break one rule each.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tasks" / "check_boundaries.py"
_spec = importlib.util.spec_from_file_location("nab_check_boundaries", _PATH)
assert _spec is not None
assert _spec.loader is not None
check_boundaries = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_boundaries)

SUPPORTED_TABLE = """Package with a table.

The supported API is the module paths below.

    pkg_a.api    Thing
"""

# A module body defining the name SUPPORTED_TABLE promises.
THING = "class Thing:\n    pass\n"


class FakeWorkspace:
    """A tmp tree the checker reads as if it were the repo and its release list."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Point the checker at an empty ``root`` workspace."""
        self.root = root
        self._monkeypatch = monkeypatch
        self._names: list[str] = []
        monkeypatch.setattr(check_boundaries, "REPO_ROOT", root)
        monkeypatch.setattr(check_boundaries.build_dists, "REPO_ROOT", root)
        monkeypatch.setattr(check_boundaries, "PUBLISHES_SUPPORTED_PATHS", ())
        self._set_packages(())

    def _set_packages(self, names: tuple[str, ...]) -> None:
        self._monkeypatch.setattr(check_boundaries.build_dists, "PACKAGES", names)

    def add(
        self,
        name: str,
        *,
        requires: tuple[str, ...] = (),
        docstring: str = "",
        **modules: str,
    ) -> None:
        """Write distribution ``name`` and add it to the release list.

        Each keyword becomes ``src/<module>/<keyword>.py`` holding its value, so a
        test spells only the import it is about.
        """
        source = self.root / name / "src" / name.replace("-", "_")
        source.mkdir(parents=True)

        dependencies = ", ".join(f'"{requirement}"' for requirement in requires)
        (self.root / name / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\ndependencies = [{dependencies}]\n',
            encoding="utf-8",
        )

        (source / "__init__.py").write_text(f'"""{docstring}"""\n', encoding="utf-8")
        for module, body in modules.items():
            (source / f"{module}.py").write_text(body, encoding="utf-8")

        self.release(name)

    def release(self, name: str) -> None:
        """Add ``name`` to the release list, whether or not it has a directory."""
        self._names.append(name)
        self._set_packages(tuple(self._names))

    def publishes_supported_paths(self, *modules: str) -> None:
        """Require a readable supported-path table from each of ``modules``."""
        self._monkeypatch.setattr(
            check_boundaries, "PUBLISHES_SUPPORTED_PATHS", modules
        )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeWorkspace:
    return FakeWorkspace(tmp_path, monkeypatch)


def test_packages_are_the_ones_the_release_builds() -> None:
    """The checked set comes from the release package list."""
    checked = {package.dist_name for package in check_boundaries.packages()}

    assert checked == set(check_boundaries.build_dists.PACKAGES)


def test_the_packages_that_publish_a_table_are_pinned() -> None:
    """Pin the constant every other test here replaces.

    Emptying it turns the ``supported`` rule off for every package.
    """

    assert check_boundaries.PUBLISHES_SUPPORTED_PATHS == (
        "nab_index",
        "nab_markersets",
        "nab_resolver",
        "nab_provider",
        "nab_project",
    )


def test_diagnostic_imports_use_their_published_owner(
    workspace: FakeWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.add(
        "nab-resolver",
        docstring="Shared diagnostics.\n\nSupported diagnostic imports:\n\n    nab_resolver.diagnostics  DiagnosticMessage\n",
        diagnostics="class DiagnosticMessage:\n    pass\n",
        errors="from .diagnostics import DiagnosticMessage\n",
    )
    workspace.add(
        "nab-project",
        requires=("nab-resolver",),
        wrong="from nab_resolver.errors import DiagnosticMessage\n",
    )
    workspace.publishes_supported_paths("nab_resolver")

    assert check_boundaries.main() == 1
    assert (
        "supports DiagnosticMessage at nab_resolver.diagnostics, "
        "not through nab_resolver.errors"
    ) in capsys.readouterr().err


def test_the_published_table_resolves_against_its_own_package() -> None:
    """nab-resolver's real table, read out of its docstring and checked row by row."""
    resolver = next(
        package
        for package in check_boundaries.packages()
        if package.module == "nab_resolver"
    )

    assert resolver.supported["Resolver"] == "nab_resolver.resolver"
    assert check_boundaries.unresolved_rows(resolver) == []


@pytest.mark.parametrize(
    ("module", "name", "status"),
    [
        ("values", "SlottedValue", 0),
        ("predicate_ranges", "PredicateRange", 0),
        ("ranges", "PredicateRange", 1),
        ("result", "walk_selected", 0),
        ("version_spans", "cached_dependency_span", 0),
        ("ranges", "SlottedValue", 1),
        ("report", "walk_selected", 1),
        ("ranges", "cached_dependency_span", 1),
    ],
)
def test_host_components_use_their_supported_owners(
    workspace: FakeWorkspace,
    capsys: pytest.CaptureFixture[str],
    module: str,
    name: str,
    status: int,
) -> None:
    workspace.add(
        "nab-resolver",
        docstring="Generic host components.\n\nSupported imports:\n\n    nab_resolver.values SlottedValue\n    nab_resolver.result walk_selected\n    nab_resolver.version_spans cached_dependency_span\n    nab_resolver.predicate_ranges PredicateRange\n",
        predicate_ranges="class PredicateRange:\n    pass\n",
        values="class SlottedValue:\n    pass\n",
        result="def walk_selected():\n    pass\n",
        version_spans="def cached_dependency_span():\n    pass\n",
        ranges="from .values import SlottedValue\nfrom .version_spans import cached_dependency_span\n",
        report="from .result import walk_selected\n",
    )
    workspace.add(
        "host",
        requires=("nab-resolver",),
        use=f"from nab_resolver.{module} import {name}\n",
    )
    workspace.publishes_supported_paths("nab_resolver")

    assert check_boundaries.main() == status
    if status:
        assert f"supports {name} at nab_resolver." in capsys.readouterr().err


@pytest.mark.parametrize(("module", "status"), [("toml_writer", 0), ("lockfile", 1)])
def test_generated_writer_uses_its_supported_module(
    workspace: FakeWorkspace,
    capsys: pytest.CaptureFixture[str],
    module: str,
    status: int,
) -> None:
    workspace.add(
        "nab-project",
        docstring="Generated TOML.\n\nSupported imports:\n\n    nab_project.toml_writer  dumps\n",
        toml_writer="def dumps(value):\n    return str(value)\n",
        lockfile="from .toml_writer import dumps\n",
    )
    workspace.add(
        "writer-consumer",
        requires=("nab-project",),
        use=f"from nab_project.{module} import dumps\n",
    )
    workspace.publishes_supported_paths("nab_project")

    assert check_boundaries.main() == status
    if status:
        assert "supports dumps at nab_project.toml_writer" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("module", "status"), [("lock_preferences", 0), ("resolve", 1)]
)
def test_previous_lock_preferences_use_their_project_module(
    workspace: FakeWorkspace,
    capsys: pytest.CaptureFixture[str],
    module: str,
    status: int,
) -> None:
    workspace.add(
        "nab-project",
        docstring="Previous lock policy.\n\nSupported imports:\n\n    nab_project.lock_preferences  LockPreferences\n",
        lock_preferences="class LockPreferences:\n    pass\n",
        resolve="from .lock_preferences import LockPreferences\n",
    )
    workspace.add(
        "lock-consumer",
        requires=("nab-project",),
        use=f"from nab_project.{module} import LockPreferences\n",
    )
    workspace.publishes_supported_paths("nab_project")

    assert check_boundaries.main() == status

    if status:
        assert (
            "supports LockPreferences at nab_project.lock_preferences"
            in capsys.readouterr().err
        )


def test_clean_tree_passes(
    workspace: FakeWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.add("pkg-a", api="VALUE = 1\n")
    workspace.add("pkg-b", requires=("pkg-a>=1",), use="from pkg_a.api import VALUE\n")

    assert check_boundaries.main() == 0
    assert "clean across pkg_a, pkg_b" in capsys.readouterr().out


def test_undeclared_sibling_import_is_reported(
    workspace: FakeWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.add("pkg-a", api="VALUE = 1\n")
    workspace.add("pkg-b", use="from pkg_a.api import VALUE\n")

    assert check_boundaries.main() == 1
    assert (
        "pkg-b imports pkg_a.api but does not depend on pkg-a"
        in capsys.readouterr().err
    )


def test_private_sibling_import_is_reported(
    workspace: FakeWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.add("pkg-a", _guts="VALUE = 1\n")
    workspace.add(
        "pkg-b", requires=("pkg-a>=1",), use="from pkg_a._guts import VALUE\n"
    )

    assert check_boundaries.main() == 1
    assert "pkg_a._guts is private (_guts)" in capsys.readouterr().err


@pytest.mark.parametrize(("module", "status"), [("value", 0), ("_value", 1)])
def test_shared_records_use_the_public_provider_module(
    workspace: FakeWorkspace,
    capsys: pytest.CaptureFixture[str],
    module: str,
    status: int,
) -> None:
    workspace.add("nab-provider", value=THING, _value=THING)
    workspace.add(
        "nab-project",
        requires=("nab-provider>=1",),
        use=f"from nab_provider.{module} import Thing\n",
    )

    assert check_boundaries.main() == status

    if status:
        assert "nab_provider._value is private" in capsys.readouterr().err


def test_vendored_tree_is_off_limits(
    workspace: FakeWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.add("pkg-a")
    workspace.add("pkg-b", requires=("pkg-a>=1",), use="import pkg_a._vendor.thing\n")

    assert check_boundaries.main() == 1
    assert "reaches into a vendored tree" in capsys.readouterr().err


def test_import_off_the_supported_path_is_reported(
    workspace: FakeWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.add("pkg-a", docstring=SUPPORTED_TABLE, api=THING, other=THING)
    workspace.publishes_supported_paths("pkg_a")
    workspace.add(
        "pkg-b", requires=("pkg-a>=1",), use="from pkg_a.other import Thing\n"
    )

    assert check_boundaries.main() == 1
    assert (
        "pkg-a supports Thing at pkg_a.api, not through pkg_a.other"
        in capsys.readouterr().err
    )


def test_supported_path_row_naming_a_missing_module_is_reported(
    workspace: FakeWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row is read against its own package without a sibling import."""
    workspace.add("pkg-a", docstring=SUPPORTED_TABLE)
    workspace.publishes_supported_paths("pkg_a")

    assert check_boundaries.main() == 1
    reported = capsys.readouterr().err
    assert "pkg-a supports Thing at pkg_a.api, but " in reported
    assert "api.py does not exist" in reported


def test_supported_path_row_naming_a_missing_symbol_is_reported(
    workspace: FakeWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    """The module survives a rename of the name it publishes; the row does not."""
    workspace.add("pkg-a", docstring=SUPPORTED_TABLE, api="class Widget:\n    pass\n")
    workspace.publishes_supported_paths("pkg_a")

    assert check_boundaries.main() == 1
    reported = capsys.readouterr().err
    assert "pkg-a supports Thing at pkg_a.api, but " in reported
    assert "api.py defines no Thing" in reported


def test_unreadable_supported_path_table_fails_the_run(
    workspace: FakeWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    """A package that stops publishing its table must not go quietly unenforced."""
    workspace.add("pkg-a", docstring="No table here.\n")
    workspace.publishes_supported_paths("pkg_a")

    assert check_boundaries.main() == 1
    assert (
        "Could not read a supported-path table from: pkg_a" in capsys.readouterr().err
    )


def test_package_with_no_directory_names_itself(workspace: FakeWorkspace) -> None:
    """A name in the release list with no tree gives a message, not a traceback."""
    workspace.add("pkg-a")
    workspace.release("pkg-ghost")

    with pytest.raises(SystemExit, match="pkg-ghost is in the release package list"):
        check_boundaries.packages()


@pytest.mark.parametrize(("module", "status"), [("lock_policy", 0), ("lockfile", 1)])
def test_lock_operation_policy_uses_its_owning_module(
    workspace: FakeWorkspace,
    capsys: pytest.CaptureFixture[str],
    module: str,
    status: int,
) -> None:
    workspace.add(
        "nab-project",
        docstring="Lock operation policy.\n\nSupported imports:\n\n    nab_project.lock_policy  LockPolicy\n",
        lock_policy="class LockPolicy:\n    pass\n",
        lockfile="from .lock_policy import LockPolicy\n",
    )
    workspace.add(
        "lock-consumer",
        requires=("nab-project",),
        use=f"from nab_project.{module} import LockPolicy\n",
    )
    workspace.publishes_supported_paths("nab_project")

    assert check_boundaries.main() == status

    if status:
        assert (
            "supports LockPolicy at nab_project.lock_policy" in capsys.readouterr().err
        )


@pytest.mark.parametrize(("module", "status"), [("urls", 0), ("file_urls", 1)])
def test_url_output_uses_the_supported_index_module(
    workspace: FakeWorkspace,
    capsys: pytest.CaptureFixture[str],
    module: str,
    status: int,
) -> None:
    workspace.add(
        "nab-index",
        docstring="URL utilities.\n\nSupported imports:\n\n    nab_index.urls  strip_userinfo\n",
        urls="def strip_userinfo(value):\n    return value\n",
        file_urls="from .urls import strip_userinfo\n",
    )
    workspace.add(
        "url-consumer",
        requires=("nab-index",),
        use=f"from nab_index.{module} import strip_userinfo\n",
    )
    workspace.publishes_supported_paths("nab_index")

    assert check_boundaries.main() == status

    if status:
        assert "supports strip_userinfo at nab_index.urls" in capsys.readouterr().err


@pytest.mark.parametrize(("module", "status"), [("priority", 0), ("resolver", 1)])
def test_conflict_priority_uses_its_owning_module(
    workspace: FakeWorkspace,
    capsys: pytest.CaptureFixture[str],
    module: str,
    status: int,
) -> None:
    workspace.add(
        "nab-resolver",
        docstring="Conflict ordering.\nSupported imports:\n\n    nab_resolver.priority  compute_tier\n",
        priority="def compute_tier():\n    pass\n",
        resolver="from .priority import compute_tier\n",
    )
    workspace.publishes_supported_paths("nab_resolver")
    workspace.add(
        "nab-provider",
        requires=("nab-resolver",),
        use=f"from nab_resolver.{module} import compute_tier\n",
    )

    assert check_boundaries.main() == status
    if status:
        assert (
            "supports compute_tier at nab_resolver.priority" in capsys.readouterr().err
        )
