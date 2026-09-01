"""Tests for the environment nab reads: the ``NAB_*`` layer and the census."""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

from nab import env
from nab.cli import main
from nab.config.ladder import OPTIONS, RejectedLayer, read_env_layer

_PROJECT = '[project]\nname = "probe"\nversion = "0.1"\ndependencies = []\n'

_TYPO_WARNING = "NAB_OFLINE is not a recognized nab setting"

_ROOT = Path(__file__).resolve().parents[1]

_SOURCE_TREES = {
    "nab": _ROOT / "src" / "nab",
    "nab_index": _ROOT / "nab-index" / "src" / "nab_index",
    "nab_markersets": _ROOT / "nab-markersets" / "src" / "nab_markersets",
    "nab_project": _ROOT / "nab-project" / "src" / "nab_project",
    "nab_provider": _ROOT / "nab-provider" / "src" / "nab_provider",
    "nab_resolver": _ROOT / "nab-resolver" / "src" / "nab_resolver",
}

# nab decides what it does from one module; the help renderer reads COLUMNS,
# a width rather than a decision, and must not pull nab/env.py onto the
# --help path.  The other two build a subprocess's environment, which the
# package spawning it owns.
_ENVIRONMENT_READERS = {
    "nab": {"nab/env.py", "nab/_cli/render.py"},
    "nab_index": {"nab_index/vcs.py"},
    "nab_markersets": set[str](),
    "nab_project": {"nab_project/_build/env.py"},
    "nab_provider": set[str](),
    "nab_resolver": set[str](),
}


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(
            (
                "lock",
                "{project}/pyproject.toml",
                "--output",
                "{project}/pylock.toml",
                "--cache-dir",
                "{project}/cache",
            ),
            id="lock",
        ),
        pytest.param(
            (
                "download",
                "{project}/pyproject.toml",
                "--output",
                "{project}/wheels",
                "--cache-dir",
                "{project}/cache",
            ),
            id="download",
        ),
        pytest.param(
            ("config", "list", "--path", "{project}/pyproject.toml"),
            id="config-list",
        ),
        pytest.param(("cache", "dir"), id="cache-dir"),
    ],
)
def test_unknown_env_warning_fires_once(
    argv: tuple[str, ...],
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every command reads the env layer once, so a typo warns once.

    The count is the assertion: zero would mean the guard stopped
    running, and two that the command built the layer twice.
    """
    (hermetic_roots / "pyproject.toml").write_text(_PROJECT)
    monkeypatch.setenv("NAB_OFLINE", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["nab", *(part.format(project=hermetic_roots) for part in argv)],
    )

    main()

    assert capsys.readouterr().err.count(_TYPO_WARNING) == 1


@pytest.mark.parametrize(
    ("flags", "verbosity", "warnings"),
    [
        pytest.param((), None, 1, id="default"),
        pytest.param(("-q",), None, 1, id="quiet"),
        pytest.param(("-qq",), None, 0, id="quiet-twice"),
        pytest.param((), "silent", 0, id="env-silent"),
    ],
)
def test_the_unknown_name_warning_sits_on_the_ordinary_warning_level(
    flags: tuple[str, ...],
    verbosity: str | None,
    warnings: int,
    hermetic_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-qq`` and ``NAB_VERBOSITY=silent`` turn the typo warning off too.

    It reports a mistake nab has already refused to act on and names the
    fix in its own text, so it is a warning like the rest rather than a
    line the run cannot switch off.
    """
    (hermetic_roots / "pyproject.toml").write_text(_PROJECT)
    monkeypatch.setenv("NAB_OFLINE", "1")
    if verbosity is not None:
        monkeypatch.setenv("NAB_VERBOSITY", verbosity)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nab",
            *flags,
            "config",
            "list",
            "--path",
            str(hermetic_roots / "pyproject.toml"),
        ],
    )

    main()

    assert capsys.readouterr().err.count(_TYPO_WARNING) == warnings


_ENVIRON_READS = frozenset({"environ", "getenv"})


def _reads_the_environment(module: ast.Module) -> bool:
    """Whether ``module`` reaches ``os.environ`` or ``os.getenv``.

    Both spellings count, and under whatever name they were bound:
    ``import os as _o`` and ``from os import environ`` are the two ways
    past a check that only knows the literal ``os.environ``.
    """
    through_module: set[str] = set()
    by_name: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            through_module |= {a.asname or a.name for a in node.names if a.name == "os"}
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            by_name |= {
                a.asname or a.name for a in node.names if a.name in _ENVIRON_READS
            }

    for node in ast.walk(module):
        if isinstance(node, ast.Attribute) and node.attr in _ENVIRON_READS:
            if isinstance(node.value, ast.Name) and node.value.id in through_module:
                return True
        elif isinstance(node, ast.Name) and node.id in by_name:
            return True
    return False


def _environment_readers(tree: Path) -> set[str]:
    """The modules under ``tree`` that read the process environment."""
    return {
        module.relative_to(tree.parent).as_posix()
        for module in tree.rglob("*.py")
        if _reads_the_environment(ast.parse(module.read_text(encoding="utf-8")))
    }


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import os\n\nos.environ.get('X')\n", id="attribute"),
        pytest.param("import os\n\nos.getenv('X')\n", id="getenv"),
        pytest.param("import os as _o\n\n_o.environ.get('X')\n", id="aliased"),
        pytest.param("from os import environ\n\nenviron.get('X')\n", id="by-name"),
        pytest.param("from os import getenv as _g\n\n_g('X')\n", id="renamed"),
    ],
)
def test_the_census_sees_every_spelling(source: str) -> None:
    assert _reads_the_environment(ast.parse(source)) is True


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import os\n\nos.getcwd()\n", id="other-os-call"),
        pytest.param("environ = {}\n\nenviron.get('X')\n", id="unrelated-name"),
    ],
)
def test_the_census_leaves_other_code_alone(source: str) -> None:
    assert _reads_the_environment(ast.parse(source)) is False


@pytest.mark.parametrize("package", sorted(_SOURCE_TREES))
def test_the_environment_is_read_where_the_census_says(package: str) -> None:
    """A new read of the process environment has to be classified on purpose.

    The list is an equality, so a module that starts reading the
    environment fails here until it is either routed through
    :mod:`nab.env` or recorded as a subprocess's environment.
    """
    assert _environment_readers(_SOURCE_TREES[package]) == _ENVIRONMENT_READERS[package]


def test_current_falls_back_to_the_process_environment() -> None:
    assert env.current() is os.environ


def test_current_passes_a_supplied_mapping_through() -> None:
    supplied = {"NAB_VERBOSITY": "debug"}
    assert env.current(supplied) is supplied


def test_verbosity_name_is_the_raw_value() -> None:
    assert env.verbosity_name({env.NAB_VERBOSITY: " Debug "}) == " Debug "
    assert env.verbosity_name({}) is None


def test_progress_suppressed_reads_nab_no_progress() -> None:
    assert env.progress_suppressed({env.NAB_NO_PROGRESS: "1"}) is True
    assert env.progress_suppressed({env.NAB_NO_PROGRESS: ""}) is False
    assert env.progress_suppressed({}) is False


def test_cache_and_config_roots_are_the_raw_values(tmp_path: Path) -> None:
    cache, config = str(tmp_path / "cache"), str(tmp_path / "config")
    environ = {env.XDG_CACHE_HOME: cache, env.XDG_CONFIG_HOME: config}

    assert env.cache_root(environ) == cache
    assert env.config_root(environ) == config
    assert env.cache_root({}) is None
    assert env.config_root({}) is None


def test_a_relative_root_is_ignored() -> None:
    relative = {env.XDG_CACHE_HOME: "cache", env.XDG_CONFIG_HOME: "../config"}

    assert env.cache_root(relative) is None
    assert env.config_root(relative) is None


def test_an_empty_root_is_ignored() -> None:
    assert env.cache_root({env.XDG_CACHE_HOME: ""}) is None
    assert env.config_root({env.XDG_CONFIG_HOME: ""}) is None


def test_output_owned_names_the_two_output_variables() -> None:
    assert sorted(env.OUTPUT_OWNED) == ["NAB_NO_PROGRESS", "NAB_VERBOSITY"]


def _rejections(environ: dict[str, str]) -> list[RejectedLayer]:
    """What the env layer refuses out of ``environ``, in place of warning."""
    collected: list[RejectedLayer] = []
    read_env_layer(environ, rejections=collected)
    return collected


def test_the_unknown_name_message_names_every_variable_nab_honours() -> None:
    """The message set is every ``NAB_*`` name that takes effect.

    The four keyed rows that declare an env var plus the two the output
    layer owns, which the layer skips but nab still reads.  The text is
    pinned whole because it is the list a user reads to find the name they
    meant.
    """
    [rejected] = _rejections({"NAB_OFLINE": "1"})

    assert rejected.reason == (
        "NAB_OFLINE is not a recognized nab setting and was ignored; the known"
        " NAB_* variables are NAB_CACHE_DIR, NAB_HTTP_BACKEND,"
        " NAB_MAX_CONCURRENCY, NAB_NO_PROGRESS, NAB_OFFLINE, NAB_VERBOSITY."
    )


def test_the_output_variables_are_skipped_in_silence() -> None:
    """The skip set is read off :mod:`nab.env`, not handed in by the caller.

    Both names are written out rather than taken from ``OUTPUT_OWNED``, so
    dropping one from that set fails here instead of shrinking the case.
    """
    assert _rejections({env.NAB_VERBOSITY: "debug", env.NAB_NO_PROGRESS: "1"}) == []


def test_the_skip_set_names_nothing_the_registry_declares() -> None:
    """The two sets are disjoint, so no variable is both skipped and offered.

    A ``NAB_VERBOSITY`` row in the registry would put it in ``nab config
    list`` and make the skip silently shadow it.
    """
    would_be = {"NAB_" + spec.name.upper().replace("-", "_") for spec in OPTIONS}

    assert env.OUTPUT_OWNED.isdisjoint(would_be)
    assert env.OUTPUT_OWNED.isdisjoint(
        {spec.env_var for spec in OPTIONS if spec.env_var is not None}
    )
