"""Tests for the [tool.nab] config reader."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomli

from nab.config.ladder import (
    SourceRoots,
    build_cli_layer,
    discover_layers,
    read_env_layer,
    render_explain,
    render_get,
    resolve_config,
)
from nab.config.model import (
    EnvironmentConfig,
    plan_targets,
    read_pyproject_config,
)
from nab.config.subflags import CliTable, build_cli_tables
from nab_markersets._compat import get_int_max_str_digits
from nab_project.conflicts import (
    ConflictKind,
    ConflictMember,
    ConflictPolicy,
    ConflictSet,
)
from nab_provider.errors import ConfigError
from nab_provider.policy import ResolveMode
from nab_provider.provider import (
    BuildPolicy,
    DistPolicy,
    LocalSource,
    VcsConfig,
    VcsPolicy,
)
from nab_provider.serialization import SimpleSerialization
from nab_provider.tags import PlatformSpec
from nab_provider.target import (
    host_environment,
)
from nab_provider.value import field_names, replace
from nab_provider.vcs_admission import admit_vcs_url

requires_int_limit = pytest.mark.skipif(
    get_int_max_str_digits() == 0, reason="requires an enabled integer conversion limit"
)


# Every sub-flag parameter the assembler reads, none of them written.
_TABLE_PARAMS: dict[str, object] = {
    "project_matrix_python": None,
    "project_matrix_platforms": (),
    "project_matrix_implementations": (),
    "project_matrix_free_threaded": (),
    "project_matrix_python_order": None,
    "project_matrix_python_patches": (),
    "project_environment_python": None,
    "project_environment_platform": (),
    "project_environment_implementation": None,
}


def _cli_table(parent: str, prefix: str, written: dict[str, object]) -> CliTable:
    """Assemble the table a ``--project-<parent>-*`` line writes.

    Built through the assembler rather than by hand so a case carries the
    flags and tokens the fold and every message read off it.
    """
    params = {**_TABLE_PARAMS}
    params.update({f"{prefix}{key}": value for key, value in written.items()})
    return build_cli_tables(params)[parent]


def _cli_matrix(**written: object) -> CliTable:
    """The ``matrix`` table the named ``--project-matrix-*`` flags write."""
    return _cli_table("matrix", "project_matrix_", written)


def _cli_environment(**written: object) -> CliTable:
    """The ``environment`` table the named environment flags write."""
    return _cli_table("environment", "project_environment_", written)


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "pyproject.toml"
    # TOML is UTF-8 by spec and nab reads the file in binary, so the fixture
    # must not pick up the platform default (cp1252 on Windows).
    p.write_text(body, encoding="utf-8")
    return p


DOCS_REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "reference"

DOCS_CONFLICTS = (
    Path(__file__).resolve().parents[1] / "docs" / "explanation" / "conflicts.md"
)


def conflicts_doc_examples() -> list[str]:
    """Return every ``[tool.nab]`` fenced TOML block in the conflicts page."""
    text = DOCS_CONFLICTS.read_text(encoding="utf-8")
    blocks = [
        block
        for block in re.findall(r"```toml\n(.*?)```", text, re.DOTALL)
        if block.lstrip().startswith("[tool.nab]")
    ]

    if not blocks:
        raise AssertionError("no [tool.nab] example block in conflicts.md")
    return blocks


def first_tool_nab_example() -> str:
    """Return the first ``[tool.nab]`` fenced TOML block in the config reference."""
    text = (DOCS_REFERENCE / "resolution-settings.md").read_text(encoding="utf-8")
    for block in re.findall(r"```toml\n(.*?)```", text, re.DOTALL):
        if block.lstrip().startswith("[tool.nab]"):
            return block
    raise AssertionError("no [tool.nab] example block in resolution-settings.md")


def section_doc_example(page: str) -> str:
    """Return the first runnable TOML example on a reference page."""
    text = (DOCS_REFERENCE / page).read_text(encoding="utf-8")
    blocks = re.findall(r"```toml\n(.*?)```", text, re.DOTALL)
    if not blocks:
        msg = f"no TOML example in {page}"
        raise AssertionError(msg)
    return blocks[0]


class TestCliOverridesFold:
    """``--project-*`` overrides fold into the resolved config."""

    def test_file_only_settings_refuse_scalar_cli_overrides(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="takes no command line"):
            read_pyproject_config(write(tmp_path, ""), cli_overrides={"matrix": {}})

    def test_cli_override_beats_file_scalar(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\ndist-policy = "wheel-or-sdist"\n')
        config = read_pyproject_config(
            path, discover_workspace=False, cli_overrides={"dist-policy": "sdist-only"}
        )
        assert config.inputs.dist_policy is DistPolicy.SDIST_ONLY

    def test_cli_overrides_none_matches_no_overrides(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\ndist-policy = "sdist-only"\n')
        plain = read_pyproject_config(path, discover_workspace=False)
        explicit_none = read_pyproject_config(
            path, discover_workspace=False, cli_overrides=None
        )
        assert plain == explicit_none

    def test_cli_list_replaces_the_file_list(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[tool.nab]\nconstraints = ["a<1"]\n')
        config = read_pyproject_config(
            path, discover_workspace=False, cli_overrides={"constraints": ["b<2"]}
        )
        assert config.inputs.constraints == ("b<2",)

    def test_cli_mode_universal_requires_matrix(self, tmp_path: Path) -> None:
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')
        with pytest.raises(ConfigError) as excinfo:
            read_pyproject_config(
                path, discover_workspace=False, cli_overrides={"mode": "universal"}
            )
        message = str(excinfo.value)
        assert "pass --project-matrix-python and --project-matrix-platforms" in message
        assert "[tool.nab.matrix] to the project's pyproject.toml" in message
        assert "top-level [matrix] table to the project's nab.toml" in message
        assert "there is no --project-matrix" not in message

    def test_cli_mode_universal_satisfied_by_nab_toml_matrix(
        self, tmp_path: Path
    ) -> None:
        """A top-level [matrix] in nab.toml satisfies --project-mode universal."""
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')
        (tmp_path / "nab.toml").write_text(
            '[matrix]\npython = ">=3.11,<3.13"\nplatforms = ["linux_x86_64"]\n',
            encoding="utf-8",
        )
        config = read_pyproject_config(
            path, discover_workspace=False, cli_overrides={"mode": "universal"}
        )
        assert config.mode is ResolveMode.UNIVERSAL
        assert config.matrix is not None
        assert config.matrix.platforms == (PlatformSpec("linux_x86_64"),)

    def test_a_cli_matrix_narrows_the_file_matrix(self, tmp_path: Path) -> None:
        """A key no flag names keeps the value the project file declared."""
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n'
            '[tool.nab.matrix]\npython = ">=3.11,<3.13"\n'
            'platforms = ["windows_amd64"]\n'
            'implementations = ["cpython", "pypy"]\n',
        )

        config = read_pyproject_config(
            path,
            discover_workspace=False,
            cli_overrides={
                "matrix": _cli_matrix(python="==3.11", platforms=("linux_x86_64",))
            },
        )

        assert config.matrix is not None
        assert config.matrix.implementations == ("cpython", "pypy")
        assert config.matrix.platforms == (PlatformSpec("linux_x86_64"),)
        assert config.matrix.python == "==3.11"

    def test_cli_mode_specific_shadows_declared_matrix(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n'
            '[tool.nab.matrix]\npython = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n',
        )
        config = read_pyproject_config(
            path, discover_workspace=False, cli_overrides={"mode": "specific"}
        )
        assert config.mode is ResolveMode.SPECIFIC
        assert config.matrix is None

    def test_a_complete_cli_matrix_takes_the_documented_defaults(
        self, tmp_path: Path
    ) -> None:
        """Flags with no file table get the documented defaults for the axes they omit."""
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')

        config = read_pyproject_config(
            path,
            discover_workspace=False,
            cli_overrides={
                "mode": "universal",
                "matrix": _cli_matrix(
                    python=">=3.11,<3.13", platforms=("linux_x86_64",)
                ),
            },
        )

        assert config.matrix is not None
        assert config.matrix.python_order == "asc"
        assert config.matrix.implementations == ("cpython",)
        assert not config.matrix.python_patches

    def test_a_python_flag_folds_onto_a_declared_platform(self, tmp_path: Path) -> None:
        """The python axis moves and the declared machine stays."""
        path = write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n'
            '[tool.nab.environment]\nplatform = "macos_arm64"\n',
        )

        config = read_pyproject_config(
            path,
            discover_workspace=False,
            cli_overrides={"environment": _cli_environment(python="3.12")},
        )

        assert config.environment is not None
        assert config.environment.python == "3.12"
        assert config.environment.platform == PlatformSpec("macos_arm64")

    def test_an_environment_python_flag_names_the_key_not_the_flag(
        self, tmp_path: Path
    ) -> None:
        """The merged table is checked by the same hook a file table is."""
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')

        with pytest.raises(
            ConfigError, match="environment.python must be a version like"
        ):
            read_pyproject_config(
                path,
                discover_workspace=False,
                cli_overrides={"environment": _cli_environment(python="3.12.x")},
            )


class TestMode:
    def test_a_cli_matrix_under_specific_mode_names_the_flags(
        self, tmp_path: Path
    ) -> None:
        """An explicit specific mode refuses a multi-target CLI matrix."""
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')
        matrix = _cli_matrix(python=">=3.11,<3.13", platforms=("linux_x86_64",))

        with pytest.raises(ConfigError) as excinfo:
            read_pyproject_config(
                path,
                discover_workspace=False,
                cli_overrides={"matrix": matrix, "mode": "specific"},
            )

        assert str(excinfo.value) == (
            "--project-matrix-python and --project-matrix-platforms declare"
            " a matrix with multiple targets but mode is 'specific'\n    try: pass --project-mode universal"
            " as well, or drop the matrix flags."
        )

    def test_a_cli_matrix_over_a_file_environment_names_the_flags_written(
        self, tmp_path: Path
    ) -> None:
        """The refusal names the flags on the line, not a fixed family pair."""
        path = write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n'
            '[tool.nab.environment]\npython = "3.12"\n'
            'platform = "linux_x86_64"\n',
        )
        tail = (
            " declares one environment; the matrix declares one environment"
            " per tuple, so the two contradict\n    try: drop one."
        )

        for written, flags in (
            (
                {"python": ">=3.12,<3.13", "platforms": ("macos_arm64",)},
                "--project-matrix-python and --project-matrix-platforms",
            ),
            (
                {
                    "python": ">=3.12,<3.13",
                    "platforms": ("macos_arm64",),
                    "implementations": ("cpython",),
                },
                (
                    "--project-matrix-python and --project-matrix-platforms"
                    " and --project-matrix-implementations"
                ),
            ),
        ):
            overrides = {
                "mode": "universal",
                "matrix": _cli_matrix(**written),
            }

            with pytest.raises(ConfigError) as excinfo:
                read_pyproject_config(
                    path, discover_workspace=False, cli_overrides=overrides
                )

            assert str(excinfo.value) == (
                f"{flags} declare a matrix and [tool.nab.environment]{tail}"
            )

    def test_a_cli_environment_key_under_a_file_matrix_names_the_flag(
        self, tmp_path: Path
    ) -> None:
        """The droppable half is the one flag, so the refusal names it."""
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n'
            '[tool.nab.matrix]\npython = ">=3.12,<3.13"\n'
            'platforms = ["linux_x86_64"]\n',
        )

        with pytest.raises(ConfigError) as excinfo:
            read_pyproject_config(
                path,
                discover_workspace=False,
                cli_overrides={
                    "environment": _cli_environment(platform=("macos_arm64",))
                },
            )

        assert str(excinfo.value) == (
            "[tool.nab.matrix] declares a matrix and"
            " --project-environment-platform declares one environment; the"
            " matrix declares one environment per tuple, so the two"
            " contradict\n    try: drop one."
        )

    def test_both_tables_from_the_cli_still_contradict(self, tmp_path: Path) -> None:
        """Neither half is a file, so each half names its flags and agrees its verb."""
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')

        for environment, named in (
            (
                _cli_environment(platform=("macos_arm64",)),
                "--project-environment-platform declares one",
            ),
            (
                _cli_environment(python="3.12", platform=("macos_arm64",)),
                (
                    "--project-environment-python and"
                    " --project-environment-platform declare one"
                ),
            ),
        ):
            with pytest.raises(ConfigError) as excinfo:
                read_pyproject_config(
                    path,
                    discover_workspace=False,
                    cli_overrides={
                        "mode": "universal",
                        "matrix": _cli_matrix(
                            python=">=3.12,<3.13", platforms=("linux_x86_64",)
                        ),
                        "environment": environment,
                    },
                )

            assert str(excinfo.value) == (
                "--project-matrix-python and --project-matrix-platforms declare"
                f" a matrix and {named} environment; the matrix declares one"
                " environment per tuple, so the two contradict\n    try: drop one."
            )

    def test_the_specific_mode_message_names_the_flags_written(
        self, tmp_path: Path
    ) -> None:
        """One narrowing flag is what the reader can drop, so it is what is named."""
        path = write(
            tmp_path,
            '[tool.nab]\nmode = "universal"\n'
            '[tool.nab.matrix]\npython = ">=3.12,<3.14"\n'
            'platforms = ["linux_x86_64", "macos_arm64"]\n',
        )

        with pytest.raises(ConfigError) as excinfo:
            read_pyproject_config(
                path,
                discover_workspace=False,
                cli_overrides={
                    "mode": "specific",
                    "matrix": _cli_matrix(platforms=("macos_arm64",)),
                },
            )

        assert str(excinfo.value) == (
            "--project-matrix-platforms declares a matrix with multiple targets but mode is"
            " 'specific'\n    try: pass --project-mode universal as well, or drop the"
            " matrix flags."
        )

    def test_a_declared_matrix_under_a_cli_mode_still_names_the_tables(
        self, tmp_path: Path
    ) -> None:
        """The matrix's own origin picks the wording, not the mode's.

        Only ``--project-mode`` comes from the command line here, so a
        refusal naming the flags would send the reader to a matrix they
        never wrote.
        """
        path = write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n'
            '[tool.nab.matrix]\npython = ">=3.12,<3.13"\n'
            'platforms = ["macos_arm64"]\n'
            '[tool.nab.environment]\npython = "3.12"\n'
            'platform = "linux_x86_64"\n',
        )

        with pytest.raises(ConfigError) as excinfo:
            read_pyproject_config(
                path, discover_workspace=False, cli_overrides={"mode": "universal"}
            )

        assert str(excinfo.value) == (
            "[tool.nab.matrix] and [tool.nab.environment] cannot both be set:"
            " the matrix declares one environment per tuple, so a single"
            " declared environment would contradict it\n    try: drop one."
        )


class TestConflictsDocExamples:
    """The conflicts page teaches the syntax, so nab must accept its examples."""

    def _parse_examples(
        self, tmp_path: Path
    ) -> list[tuple[str, tuple[ConflictSet, ...]]]:
        parsed: list[tuple[str, tuple[ConflictSet, ...]]] = []
        for i, block in enumerate(conflicts_doc_examples()):
            project = tmp_path / f"example{i}"
            project.mkdir()
            path = write(project, block)
            config = read_pyproject_config(path, discover_workspace=False)
            parsed.append((block, config.inputs.conflicts))
        return parsed

    def test_every_example_declares_a_conflict(self, tmp_path: Path) -> None:
        for block, conflicts in self._parse_examples(tmp_path):
            assert conflicts, block

    def test_examples_show_every_policy(self, tmp_path: Path) -> None:
        policies = {
            conflict_set.policy
            for _, conflicts in self._parse_examples(tmp_path)
            for conflict_set in conflicts
        }
        assert policies == set(ConflictPolicy)


def _extras_set(policy: ConflictPolicy, *names: str) -> ConflictSet:
    """Build an extras-only ConflictSet for tests."""
    return ConflictSet(
        members=tuple(ConflictMember(ConflictKind.EXTRA, n) for n in names),
        policy=policy,
    )


class TestMainGroup:
    """``base-group`` names the project's own dependencies in a lock."""

    def test_a_cli_build_group_without_a_base_group_names_the_flag(
        self, tmp_path: Path
    ) -> None:
        """The project file may not hold the value the run is refusing."""
        path = write(tmp_path, "")
        with pytest.raises(ConfigError, match=r"^--project-build-group is 'build',"):
            read_pyproject_config(
                path,
                discover_workspace=False,
                cli_overrides={"build-group": "build"},
            )

    def test_a_cli_pair_naming_one_group_names_both_flags(self, tmp_path: Path) -> None:
        """Each half of the message follows the surface that set it."""
        path = write(tmp_path, "")
        with pytest.raises(
            ConfigError,
            match=r"^--project-build-group and --project-base-group are both 'shared'",
        ):
            read_pyproject_config(
                path,
                discover_workspace=False,
                cli_overrides={"base-group": "shared", "build-group": "shared"},
            )


class TestRequiresPython:
    """``requires-python`` declares the supported range; it is not a target.

    The resolve target is the host (or ``[tool.nab.environment]``), so a
    specifier finer than the host's Python is paired with the environment
    that satisfies it; one that admits no target at all is an error.
    """

    def test_a_python_override_rescues_a_declaration_the_host_fails(
        self, tmp_path: Path
    ) -> None:
        """``--python`` is the knob the error names, so it has to work.

        The check runs against the target the resolve actually uses, and
        ``--python`` moves that target after the config is read.  Without it a
        library capped below the interpreter locking it could not be locked at
        all.  nab itself needs 3.10, so a project declaring only 3.9 is never
        the host, whatever runs the suite.
        """
        path = write(tmp_path, '[tool.nab]\nrequires-python = "==3.9.*"\n')
        config = read_pyproject_config(path)
        with pytest.raises(ConfigError, match="excludes the resolve target"):
            plan_targets(config)

        retargeted = read_pyproject_config(
            path, cli_overrides={"environment": _cli_environment(python="3.9")}
        )
        (target,) = plan_targets(retargeted)
        assert target.python_version == "3.9"

    @pytest.mark.parametrize(
        ("shared_python", "override", "key"),
        [
            (
                "==3.12",
                'python = "==3.11"\n',
                "matrix.overrides.implementations.pypy.python",
            ),
            ("==3.11", 'platforms = ["windows_amd64"]\n', "matrix.python"),
            ("==3.11", "", "matrix.python"),
        ],
    )
    def test_matrix_target_error_names_the_setting_that_can_rescue_it(
        self, tmp_path: Path, shared_python: str, override: str, key: str
    ) -> None:
        """The suggested Python setting actually moves the excluded target."""
        body = (
            '[tool.nab]\nmode = "universal"\nrequires-python = ">=3.12"\n'
            f'[tool.nab.matrix]\npython = "{shared_python}"\n'
            'platforms = ["linux_x86_64"]\nimplementations = ["pypy"]\n'
            "[tool.nab.matrix.overrides.implementations.pypy]\n"
        )
        path = write(tmp_path, body + override)
        with pytest.raises(ConfigError) as exc:
            plan_targets(read_pyproject_config(path))
        assert f"change {key}" in str(exc.value)

        if key == "matrix.python":
            rescued = read_pyproject_config(
                path, cli_overrides={"matrix": _cli_matrix(python="==3.12")}
            )
        else:
            rescued = read_pyproject_config(
                write(tmp_path, body + 'python = "==3.12"\n')
            )
        assert [target.python_version for target in plan_targets(rescued)] == ["3.12"]

    def test_top_level_doc_example_is_a_valid_specifier(self, tmp_path: Path) -> None:
        """The config reference's top-level example must parse as a valid specifier."""
        block = first_tool_nab_example()
        match = re.search(r'^requires-python = "([^"]*)"', block, re.MULTILINE)
        assert match is not None, "top-level example dropped requires-python"
        value = match.group(1)

        # Select a target admitted by the executable example.
        path = write(
            tmp_path,
            f'[tool.nab]\nrequires-python = "{value}"\n'
            '[tool.nab.environment]\npython = "3.13"\n',
        )
        assert read_pyproject_config(path).inputs.requires_python == value


_UNIVERSAL_MATRIX = (
    '[tool.nab.matrix]\npython = ">=3.11,<3.12"\nplatforms = ["linux_x86_64"]\n'
)

_UNIVERSAL_RULE = '[[tool.nab.package-rules]]\nmatch = ["foo", "bar"]\n'


_FREE_THREADED_NO_PYTHON = (
    "[tool.nab.environment]\n"
    'platform = { id = "linux_x86_64", free-threaded = true }\n'
    '[tool.nab]\nbuild-policy = "never"\n'
)


def _host_python(monkeypatch: pytest.MonkeyPatch, full_version: str) -> None:
    """Report ``full_version`` as the running interpreter to the planner."""
    env = {**host_environment(), "python_full_version": full_version}
    monkeypatch.setattr("nab_project.config.host_environment", lambda: env)


class TestEnvironment:
    """``[tool.nab.environment]``: the one environment to resolve for."""

    def test_python_override_keeps_a_release_candidate_tag(
        self, tmp_path: Path
    ) -> None:
        """``--python 3.14rc1`` pins the candidate on a project with a platform."""
        path = write(
            tmp_path,
            '[tool.nab.environment]\npython = "3.13"\n'
            'platform = "linux_x86_64"\n'
            '[tool.nab]\nbuild-policy = "never"\n',
        )
        config = read_pyproject_config(
            path, cli_overrides={"environment": _cli_environment(python="3.14rc1")}
        )
        (target,) = plan_targets(config)

        assert target.python_full_version == "3.14.0rc1"
        assert not target.is_minor_interval

    def test_platform_table_declares_the_tag_knobs(self, tmp_path: Path) -> None:
        """The table form of matrix.platforms works on the one environment too."""
        path = write(
            tmp_path,
            "[tool.nab.environment]\n"
            'python = "3.12"\n'
            'platform = { id = "macos_arm64", runs-on-macos = "14.0" }\n'
            '[tool.nab]\nbuild-policy = "never"\n',
        )
        config = read_pyproject_config(path)
        assert config.environment == EnvironmentConfig(
            python="3.12", platform=PlatformSpec("macos_arm64", runs_on_macos=(14, 0))
        )
        (target,) = plan_targets(config)
        wheel = "somepkg-1.0-cp312-cp312-macosx_14_0_arm64.whl"
        assert target.tags.accepts(wheel)
        assert "platform=macos_arm64[runs-on-macos=14.0]" in _inspect(
            path, "environment"
        )

    def test_the_inspector_renders_a_knobless_table(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            '[tool.nab.environment]\nplatform = { id = "linux_x86_64" }\n'
            '[tool.nab]\nbuild-policy = "never"\n',
        )
        assert "platform=linux_x86_64" in _inspect(path, "environment")

    def test_free_threaded_without_python_accepts_a_newer_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor is checked against the python ``--python`` picked."""
        _host_python(monkeypatch, "3.12.11")
        path = write(tmp_path, _FREE_THREADED_NO_PYTHON)
        config = read_pyproject_config(
            path, cli_overrides={"environment": _cli_environment(python="3.14")}
        )
        (target,) = plan_targets(config)

        assert target.python_version == "3.14"
        assert target.tags.accepts("somepkg-1.0-cp314-cp314t-manylinux_2_28_x86_64.whl")

    def test_free_threaded_rejects_an_old_python_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A python set for one run is held to the same floor a declared one is."""
        _host_python(monkeypatch, "3.14.0")
        path = write(tmp_path, _FREE_THREADED_NO_PYTHON)
        with pytest.raises(ConfigError, match="needs CPython 3.13 or newer"):
            read_pyproject_config(
                path,
                cli_overrides={"environment": _cli_environment(python="3.12")},
            )

    def test_an_implementation_flag_alone_still_needs_a_platform(
        self, tmp_path: Path
    ) -> None:
        """The file rule applies to the merged table however it was written."""
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')

        with pytest.raises(
            ConfigError,
            match=r"\[tool\.nab\.environment\]\.implementation needs a platform",
        ):
            read_pyproject_config(
                path,
                discover_workspace=False,
                cli_overrides={"environment": _cli_environment(implementation="pypy")},
            )

    def test_implementation_without_platform_named_for_nab_toml_under_a_flag(
        self, tmp_path: Path
    ) -> None:
        """A flag on another axis does not move the repair out of nab.toml."""
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')
        (tmp_path / "nab.toml").write_text(
            '[environment]\nimplementation = "pypy"\n', encoding="utf-8"
        )

        with pytest.raises(ConfigError) as excinfo:
            read_pyproject_config(
                path,
                discover_workspace=False,
                cli_overrides={"environment": _cli_environment(python="3.12")},
            )

        message = str(excinfo.value)
        assert "[environment].implementation needs a platform" in message
        assert "tool.nab" not in message

    @pytest.mark.parametrize("python", [None, "3.12"])
    @pytest.mark.parametrize("project_filename", ["pyproject.toml", "custom.toml"])
    def test_missing_platform_repair_updates_both_environment_tables(
        self, tmp_path: Path, python: str | None, project_filename: str
    ) -> None:
        """Apply the platform edit only to tables and files named by the diagnostic."""
        path = tmp_path / project_filename
        path.write_text(
            '[tool.nab.environment]\nimplementation = "pypy"\n', encoding="utf-8"
        )
        (tmp_path / "nab.toml").write_text(
            '[environment]\nimplementation = "pypy"\n', encoding="utf-8"
        )
        overrides = (
            {} if python is None else {"environment": _cli_environment(python=python)}
        )

        with pytest.raises(ConfigError) as excinfo:
            read_pyproject_config(
                path, discover_workspace=False, cli_overrides=overrides
            )

        repairs = re.findall(
            r"\[(tool\.nab\.environment|environment)\] in (.*?\.toml)(?= and |\.$)",
            str(excinfo.value),
        )
        for table, filename in repairs:
            target = tmp_path / filename
            target.write_text(
                target.read_text(encoding="utf-8").replace(
                    f"[{table}]\n", f'[{table}]\nplatform = "linux_x86_64"\n'
                ),
                encoding="utf-8",
            )

        config = read_pyproject_config(
            path, discover_workspace=False, cli_overrides=overrides
        )
        assert config.environment == EnvironmentConfig(
            python=python, platform=PlatformSpec("linux_x86_64"), implementation="pypy"
        )


class TestMarkerEnvironmentDeprecation:
    """``[tool.nab.marker-environment]`` translates to the environment table.

    The overlay set marker variables one at a time, so a partial platform
    left the rest of the machine on the host.  Every key must now name an
    environment axis; one that cannot is an error, not a wrong resolve.
    """

    def test_both_surfaces_rejected_under_a_cli_key(self, tmp_path: Path) -> None:
        """A file table beside the overlay still refuses, flag or no flag.

        The flag wins the key, so the table is named for the file under it.
        """
        path = write(
            tmp_path,
            '[tool.nab.environment]\npython = "3.12"\n'
            "[tool.nab.marker-environment]\n"
            'python_version = "3.11"\n',
        )
        with pytest.raises(
            ConfigError,
            match=r"\[tool.nab.environment\] and the deprecated"
            r" \[tool.nab.marker-environment\] are both set",
        ):
            read_pyproject_config(
                path,
                discover_workspace=False,
                cli_overrides={"environment": _cli_environment(python="3.12")},
            )

    def test_a_cli_python_key_folds_onto_the_overlay(self, tmp_path: Path) -> None:
        """The flag sets its axis over the translation, as it does over a table."""
        path = write(
            tmp_path,
            "[tool.nab.marker-environment]\n"
            'sys_platform = "darwin"\n'
            'platform_machine = "arm64"\n',
        )

        config = read_pyproject_config(
            path,
            discover_workspace=False,
            cli_overrides={"environment": _cli_environment(python="3.12")},
        )

        assert config.environment == EnvironmentConfig(
            python="3.12", platform=PlatformSpec("macos_arm64")
        )

    def test_a_cli_implementation_key_folds_onto_the_overlay(
        self, tmp_path: Path
    ) -> None:
        """The translated platform is the machine the implementation is modelled on."""
        path = write(
            tmp_path,
            '[tool.nab]\nbuild-policy = "never"\n'
            "[tool.nab.marker-environment]\n"
            'sys_platform = "darwin"\n'
            'platform_machine = "arm64"\n',
        )

        config = read_pyproject_config(
            path,
            discover_workspace=False,
            cli_overrides={"environment": _cli_environment(implementation="pypy")},
        )

        assert config.environment == EnvironmentConfig(
            platform=PlatformSpec("macos_arm64"), implementation="pypy"
        )

    def test_both_surfaces_in_nab_toml_named_at_top_level(self, tmp_path: Path) -> None:
        """Both surfaces come from nab.toml, so neither is named under tool.nab."""
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')
        (tmp_path / "nab.toml").write_text(
            '[environment]\npython = "3.12"\n'
            '[marker-environment]\npython_version = "3.11"\n',
            encoding="utf-8",
        )

        with pytest.raises(ConfigError) as excinfo:
            read_pyproject_config(path, discover_workspace=False)

        message = str(excinfo.value)
        assert "[environment] and the deprecated [marker-environment]" in message
        assert "tool.nab" not in message

    def test_both_surfaces_in_nab_toml_named_under_a_cli_key(
        self, tmp_path: Path
    ) -> None:
        """A flag wins the key, so the table is named for the file below it."""
        path = write(tmp_path, '[project]\nname = "x"\nversion = "0"\n')
        (tmp_path / "nab.toml").write_text(
            '[environment]\npython = "3.12"\n'
            '[marker-environment]\npython_version = "3.11"\n',
            encoding="utf-8",
        )

        with pytest.raises(ConfigError) as excinfo:
            read_pyproject_config(
                path,
                discover_workspace=False,
                cli_overrides={"environment": _cli_environment(python="3.13")},
            )

        message = str(excinfo.value)
        assert "[environment] and the deprecated [marker-environment]" in message
        assert "tool.nab" not in message


class TestVcsDocExample:
    """The config reference's ``[tool.nab.vcs]`` example is a working allowlist."""

    def test_example_shows_every_key(self) -> None:
        """The example names each key ``[tool.nab.vcs]`` accepts."""
        example = tomli.loads(section_doc_example("dependency-sources.md"))
        shown = set(example["tool"]["nab"]["vcs"])

        assert shown == {name.replace("_", "-") for name in field_names(VcsConfig)}

    def test_allowlists_admit_the_repo_they_name(self, tmp_path: Path) -> None:
        """Opened to ``allow``, the example admits a pinned URL to its own repo."""
        path = write(tmp_path, section_doc_example("dependency-sources.md"))
        opened = replace(read_pyproject_config(path).inputs.vcs, policy=VcsPolicy.ALLOW)
        pinned = f"git+https://github.com/me/x@{'a' * 40}"

        assert admit_vcs_url(pinned, opened) == "git+https"


class TestMatrix:
    def _matrix_body(self, **extra: str) -> str:
        body = (
            "[tool.nab]\n"
            'mode = "universal"\n'
            "[tool.nab.matrix]\n"
            'python = ">=3.11,<3.14"\n'
            'platforms = ["linux_x86_64", "macos_arm64"]\n'
        )
        for k, v in extra.items():
            body += f"{k} = {v}\n"
        return body

    def test_cli_can_narrow_implementations_with_file_overrides(
        self, tmp_path: Path
    ) -> None:
        body = self._matrix_body(
            implementations='["cpython", "pypy"]',
            overrides='{ implementations = { pypy = { python = "==3.11" } } }',
        )
        config = read_pyproject_config(
            write(tmp_path, body),
            cli_overrides={"matrix": _cli_matrix(implementations=("cpython",))},
        )
        assert {
            target.marker_env["implementation_name"] for target in plan_targets(config)
        } == {"cpython"}

    def test_cli_changes_defaults_but_preserves_explicit_include_axes(
        self, tmp_path: Path
    ) -> None:
        body = self._matrix_body(
            include='[{python = "==3.10", implementations = ["pypy"]}]'
        )
        config = read_pyproject_config(
            write(tmp_path, body),
            cli_overrides={"matrix": _cli_matrix(implementations=("cpython",))},
        )
        assert {target.implementation for target in plan_targets(config)} == {
            "cpython",
            "pypy",
        }


# Longer than CPython's default 4300-digit limit on int-from-string conversion.
_PAST_INT_LIMIT = "9" * 5000


@requires_int_limit
class TestDigitRunPastIntLimit:
    """A digit run past CPython's int limit is a config error.

    ``Version()`` raises a bare ``ValueError`` for one (not
    ``InvalidVersion``), and a specifier defers the conversion to its first
    comparison, so each version validator forces it and reports the key.
    ``int()`` rejects the same run when it is a ``P<n>D`` day count.
    """

    def test_environment_python_flag(self, tmp_path: Path) -> None:
        """The long form lands in the table, so its refusal names the key."""
        path = write(tmp_path, "[tool.nab]\n")
        with pytest.raises(
            ConfigError, match="environment.python must be a version like"
        ):
            read_pyproject_config(
                path,
                cli_overrides={
                    "environment": _cli_environment(python=f"3.{_PAST_INT_LIMIT}")
                },
            )


def _inspect(path: Path, key: str) -> str:
    """Render ``nab config get <key>`` the way the inspector would.

    Discovers the same project-dir ladder ``read_pyproject_config`` reads
    (pyproject + project-dir ``nab.toml``) and renders the effective value,
    so a test can assert the inspector reports exactly what the resolve
    consumes.
    """
    roots = SourceRoots(project_dir=path.parent.resolve(), pyproject=path.resolve())
    layers = discover_layers(roots)
    effective = resolve_config(layers, read_env_layer({}), build_cli_layer({}))
    return render_get(effective, key).strip()


def _explain(path: Path, key: str) -> str:
    roots = SourceRoots(project_dir=path.parent.resolve(), pyproject=path.resolve())
    layers = discover_layers(roots)
    effective = resolve_config(layers, read_env_layer({}), build_cli_layer({}))
    return render_explain(effective, key)


class TestProjectNabTomlConfiguresResolve:
    def _write(self, tmp_path: Path, pyproject: str, nab_toml: str) -> Path:
        path = tmp_path / "pyproject.toml"
        path.write_text(pyproject)
        (tmp_path / "nab.toml").write_text(nab_toml)
        return path

    """A project-dir ``nab.toml`` functionally configures every PROJECT key.

    Each test sets a value ONLY in a project-dir ``nab.toml`` (never in
    pyproject) and asserts (a) ``read_pyproject_config`` (what the resolve
    consumes) reflects it and (b) ``nab config get/explain`` (the
    inspector) agrees, one representative key per structural type.
    """

    def test_scalar_build_policy(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            'build-policy = "never"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.inputs.build_policy is BuildPolicy.NEVER
        assert _inspect(path, "build-policy") == "never"
        assert "project" in _explain(path, "build-policy")

    def test_scalar_dist_policy_folds_trust(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            '[dist-policy]\npolicy = "wheel-only"\ntrust-unverified-deps = true\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.inputs.dist_policy is DistPolicy.WHEEL_ONLY
        assert config.inputs.trust_unverified_sdist_deps is True
        assert "wheel-only" in _inspect(path, "dist-policy")

    def test_table_vcs(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            '[vcs]\npolicy = "allow"\nallowed-schemes = ["git+https"]\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.inputs.vcs.policy is VcsPolicy.ALLOW
        assert config.inputs.vcs.allowed_schemes == frozenset({"git+https"})
        assert "policy=allow" in _inspect(path, "vcs")

    def test_table_environment(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            '[environment]\npython = "3.12"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.environment == EnvironmentConfig(python="3.12")
        assert "python=3.12" in _inspect(path, "environment")

    def test_list_constraints(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            'constraints = ["foo<2"]\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.inputs.constraints == ("foo<2",)
        assert _inspect(path, "constraints") == "foo<2"

    def test_array_of_tables_indexes(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            "[[indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert [i.name for i in config.inputs.indexes] == ["internal"]
        assert "internal=https://pkgs.example.com/simple/" in _inspect(path, "indexes")

    def test_override_table_packages(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            '[packages.numpy]\nbuild-policy = "never"\n',
        )
        (override,) = read_pyproject_config(
            path, discover_workspace=False
        ).inputs.package_overrides
        assert override.name == "numpy"
        assert override.build_policy is BuildPolicy.NEVER
        assert "numpy" in _inspect(path, "packages")

    def test_override_table_index(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            "[[indexes]]\n"
            'name = "internal"\n'
            'url = "https://pkgs.example.com/simple/"\n'
            '[index.internal]\ndist-policy = "wheel-only"\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert (
            config.inputs.index_overrides["internal"].dist_policy
            is DistPolicy.WHEEL_ONLY
        )
        assert "internal" in _inspect(path, "index")

    def test_cross_field_matrix_and_mode(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            '[project]\nname = "x"\nversion = "0"\n',
            'mode = "universal"\n'
            "[matrix]\n"
            'python = ">=3.11,<3.13"\n'
            'platforms = ["linux_x86_64"]\n',
        )
        config = read_pyproject_config(path, discover_workspace=False)
        assert config.mode is ResolveMode.UNIVERSAL
        assert config.matrix is not None
        assert config.matrix.platforms == (PlatformSpec("linux_x86_64"),)
        # Universal mode forces build-policy to never even though the
        # project-nab.toml set no build-policy.
        assert config.inputs.build_policy is BuildPolicy.NEVER
        assert _inspect(path, "mode") == "universal"

    def test_table_workspace(self, tmp_path: Path) -> None:
        member_dir = tmp_path / "libs" / "foo"
        member_dir.mkdir(parents=True)
        (member_dir / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.0"\n',
        )
        path = self._write(
            tmp_path,
            '[project]\nname = "root"\nversion = "0"\n'
            "[tool.nab]\n"
            'build-policy = "never"\n',
            '[workspace]\nmembers = ["libs/foo"]\n',
        )
        config = read_pyproject_config(path)
        assert config.inputs.local_sources == (
            LocalSource(name="foo", path=str(member_dir), editable=True),
        )
        assert config.workspace_member_names == frozenset({"foo"})
        assert config.inputs.build_policy is BuildPolicy.NEVER
        assert "members=['libs/foo']" in _inspect(path, "workspace")


class TestConfigDocumentation:
    def test_top_level_doc_example_carries_an_offset(self, tmp_path: Path) -> None:
        """The reference's own example has to be a value the parser accepts."""
        block = first_tool_nab_example()
        match = re.search(r"^uploaded-prior-to = (.*)$", block, re.MULTILINE)
        assert match is not None, "top-level example dropped uploaded-prior-to"

        path = write(tmp_path, f"[tool.nab]\nuploaded-prior-to = {match.group(1)}\n")
        dt = read_pyproject_config(path).inputs.uploaded_prior_to
        assert dt is not None
        assert dt.utcoffset() is not None

    def test_reference_example_parses(self, tmp_path: Path) -> None:
        path = write(tmp_path, section_doc_example("indexes.md"))
        pins = {
            i.name: i.serialization for i in read_pyproject_config(path).inputs.indexes
        }

        assert pins["pypi"] is SimpleSerialization.NEGOTIATE
        assert pins["internal"] is SimpleSerialization.HTML
