"""Check the published CLI pages against what the CLI does.

The CLI reference lists each subcommand's flags, env vars and statuses; the
config reference groups the ``nab lock`` flags by what they decide; the
conflicts page quotes refusal lines verbatim.

These tests read ``docs/``, which the umbrella sdist does not ship, so the
module is on that sdist's exclude list in pyproject.toml.
"""

from __future__ import annotations

import inspect
import io
import logging
import re
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from nab._download import download
from nab._lock import lock
from nab.cli import app
from nab.output import ColorChoice, Verbosity, parse_output_options
from nab_project.config_sources import OPTIONS

_DOCS = Path(__file__).resolve().parents[1] / "docs"
_CLI_REFERENCE = _DOCS / "reference" / "cli.md"
_CONFIG_REFERENCE = _DOCS / "reference" / "configuration.md"
_CONFLICTS_DOC = _DOCS / "explanation" / "conflicts.md"

_SUBCOMMANDS = ("lock", "download", "config", "cache")


def _reference_text() -> str:
    return _CLI_REFERENCE.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of ``text`` under ``heading``, up to the next ``##``."""
    return text.partition(f"\n{heading}\n")[2].partition("\n## ")[0]


def _reference_section(heading: str) -> str:
    """The CLI reference body under ``heading``."""
    return _section(_reference_text(), heading)


def _config_reference_section(heading: str) -> str:
    """The config reference body under ``heading``."""
    return _section(_CONFIG_REFERENCE.read_text(encoding="utf-8"), heading)


def _config_reference_flag_block() -> str:
    """The fenced ``nab lock`` block that opens the config reference's CLI flags."""
    block = re.search(
        r"```\n(.*?)```", _config_reference_section("## CLI flags"), re.DOTALL
    )
    if block is None:
        msg = "no fenced flag block under the config reference's CLI flags heading"
        raise AssertionError(msg)

    return block.group(1)


def _config_reference_flag_prose() -> str:
    """The config reference's CLI-flags prose, everything after the fenced block."""
    return _config_reference_section("## CLI flags").rpartition("```")[2]


def _resolve_group_lines() -> str:
    """The ``# what gets resolved`` lines of the config reference's flag block."""
    block = _config_reference_flag_block()
    header = "  # what gets resolved\n"
    if header not in block:
        msg = "no 'what gets resolved' group in the config reference's flag block"
        raise AssertionError(msg)

    return block.partition(header)[2].partition("\n\n")[0]


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _project(directory: Path) -> Path:
    """Write the minimal pyproject the config command reads."""
    return _write(
        directory / "pyproject.toml",
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n',
    )


def _run_config(args: list[str]) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        app.cli(args=["config", *args], prog="nab")
    return buf.getvalue()


def _flag_forms(flag: str) -> list[str]:
    """The spellings a page may use for ``flag``: itself, ``--no-``, a wildcard."""
    forms = [flag, f"--no-{flag.removeprefix('--')}"]
    if flag.startswith("--project-"):
        forms += ["--project-*", "--project-<key>"]
    return forms


def _spells_flag(text: str, flag: str, *, left: str) -> bool:
    """Whether ``text`` names ``flag`` in any of its forms, preceded by ``left``.

    ``left`` is what the page puts to a flag's left: a backtick in prose, a
    non-word boundary inside a fenced block.
    """
    return any(
        re.search(rf"{left}{re.escape(form)}(?![\w-])", text)
        for form in _flag_forms(flag)
    )


def _keyword_flags(command: Callable[..., None]) -> list[str]:
    """The flags ``command`` accepts, one per keyword-only parameter."""
    return [
        "--" + name.replace("_", "-")
        for name, param in inspect.signature(command).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    ]


def _block_flags(block: str) -> list[str]:
    """The flags a fenced usage ``block`` declares, one per line."""
    return re.findall(r"(?m)^\s+(--[\w<>-]+)", block)


def _doc_paragraph(text: str, needle: str) -> str:
    """The blank-line-delimited paragraph of ``text`` that contains ``needle``."""
    for paragraph in text.split("\n\n"):
        if needle in paragraph:
            return paragraph

    msg = f"no paragraph containing {needle!r}"
    raise AssertionError(msg)


def _emitted_labels(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    body: str,
    *,
    extras: tuple[str, ...] = (),
    groups: tuple[str, ...] = (),
) -> list[str]:
    """The ``# label`` headers a ``nab lock`` over ``body`` prints for the selection.

    The requirements format labels one block per emitted target and omits the
    header when there is only one, so here the headers count the forks.
    """
    pyproject = _write(tmp_path / "pyproject.toml", body)
    lock(
        pyproject,
        cache_dir=tmp_path / "cache",
        offline=True,
        extras=extras,
        groups=groups,
        format="requirements-without-hashes",
        output=Path("-"),
    )

    printed = capsys.readouterr().out
    return [line for line in printed.splitlines() if line.startswith("# ")]


class TestCliReferenceFlagCoverage:
    """Each run subcommand's reference section names every flag it accepts."""

    @pytest.mark.parametrize(
        ("heading", "command"),
        [("## `nab lock`", lock), ("## `nab download`", download)],
    )
    def test_section_names_every_flag(
        self, heading: str, command: Callable[..., None]
    ) -> None:
        # Flags shared by both commands are documented once, in their own section.
        scope = _reference_section(heading) + _reference_section("## Runtime flags")

        for flag in _keyword_flags(command):
            assert _spells_flag(scope, flag, left="`"), f"{heading} omits {flag}"


class TestConfigReferenceCliFlags:
    """The config reference's CLI flags section matches the ``nab lock`` surface.

    The section lists the flags in groups, then says what each flag of the
    first group does to ``[tool.nab]``.  These check both halves against the
    ``lock`` signature.
    """

    def test_block_lists_every_lock_flag(self) -> None:
        block = _config_reference_flag_block()

        for flag in _keyword_flags(lock):
            assert _spells_flag(block, flag, left=r"(?<![\w-])"), (
                f"the CLI flags block omits {flag}"
            )

    def test_block_lists_only_flags_lock_accepts(self) -> None:
        accepted = {form for flag in _keyword_flags(lock) for form in _flag_forms(flag)}

        for flag in _block_flags(_config_reference_flag_block()):
            assert flag in accepted, f"the CLI flags block still lists {flag}"

    def test_prose_places_every_resolve_flag(self) -> None:
        """The prose says what each flag of the first group does to ``[tool.nab]``."""
        prose = _config_reference_flag_prose()

        for flag in _block_flags(_resolve_group_lines()):
            assert _spells_flag(prose, flag, left="`"), (
                f"the section never says what {flag} does to [tool.nab]"
            )


class TestCliReferenceSelectionShape:
    """The reference's selection paragraph matches the number of resolves run."""

    _EXTRAS = (
        '[project]\nname = "proj"\nversion = "0.1.0"\ndependencies = []\n'
        "[project.optional-dependencies]\n"
        "cpu = []\n"
        "gpu = []\n"
    )

    _CONFLICT = '[tool.nab]\nconflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'

    def _selection_paragraph(self) -> str:
        """The ``nab lock`` paragraph that states what a selection resolves to."""
        return _doc_paragraph(_reference_section("## `nab lock`"), "union resolve")

    def test_selection_alone_is_one_union_resolve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two extras with no conflict declared resolve once, as the page says."""
        labels = _emitted_labels(tmp_path, capsys, self._EXTRAS, extras=("cpu", "gpu"))
        assert labels == []

        assert "single union resolve" in self._selection_paragraph()

    def test_co_selected_conflict_members_fork_the_resolve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Declaring the same two extras exclusive resolves each separately."""
        labels = _emitted_labels(
            tmp_path, capsys, self._EXTRAS + self._CONFLICT, extras=("cpu", "gpu")
        )
        assert labels == ["# host-extra-cpu", "# host-extra-gpu"]

        paragraph = self._selection_paragraph()
        assert "`[tool.nab].conflicts`" in paragraph
        assert "../explanation/conflicts.md" in paragraph


class TestCliReferenceDocumentsOutputPolicy:
    """The CLI reference lists every output-policy flag and env var the CLI accepts."""

    def test_verbosity_flags_documented(self) -> None:
        text = _reference_text()
        for flag in ("-v", "-vv", "-q", "-qq", "--verbose", "--quiet"):
            assert f"`{flag}`" in text, f"CLI reference omits verbosity flag {flag}"

    def test_color_flags_documented(self) -> None:
        text = _reference_text()
        assert "`--color`" in text
        assert "`--no-color`" in text
        for choice in ColorChoice:
            assert f"`{choice.value}`" in text, (
                f"CLI reference omits --color value {choice.value}"
            )

    def test_progress_documented(self) -> None:
        text = _reference_text()
        assert "`--no-progress`" in text
        assert "Resolving" in text

    def test_output_env_vars_documented(self) -> None:
        text = _reference_text()
        for var in ("NAB_VERBOSITY", "NAB_NO_PROGRESS", "NO_COLOR", "FORCE_COLOR"):
            assert var in text, f"CLI reference omits env var {var}"

    def test_nab_verbosity_values_documented(self) -> None:
        text = _reference_text()
        for level in Verbosity:
            name = level.name.lower()
            assert f"`{name}`" in text, (
                f"CLI reference omits NAB_VERBOSITY value {name!r}"
            )

    def test_output_control_scope_covers_every_subcommand(self) -> None:
        """The scope paragraph must name every subcommand the flags reach.

        ``main`` extracts a global ``-q`` before dispatch, so the enumeration
        must include ``cache``.
        """
        for sub in _SUBCOMMANDS:
            _opts, rest = parse_output_options(["-q", sub], {})
            assert rest == [sub], f"global -q not extracted before {sub!r}"

        scope = next(
            para
            for para in _reference_section("## Output control").split("\n\n")
            if "before the subcommand" in para
        )
        for sub in _SUBCOMMANDS:
            assert f"`{sub}`" in scope, (
                f"Output control scope omits the {sub!r} subcommand"
            )


class TestConfigExplainReferenceDocs:
    """The CLI reference names every status ``explain`` prints."""

    def test_reference_names_every_status(
        self, hermetic_roots: Path, tmp_path: Path
    ) -> None:
        # One source per status: the user file rejected (project-scope key),
        # the pyproject binding shadowed, the CLI winning.
        _write(
            hermetic_roots / "pyproject.toml",
            '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
            '[tool.nab]\nresolution = "lowest"\n',
        )
        _write(tmp_path / "usr" / "nab.toml", 'resolution = "highest"\n')

        printed = _run_config(
            [
                "explain",
                "resolution",
                "--project-resolution",
                "highest",
                "--include-rejected",
                "--path",
                str(hermetic_roots / "pyproject.toml"),
            ]
        )

        section = _reference_section("## `nab config`")
        for status in ("winner", "shadowed", "rejected"):
            assert status in printed, status
            assert f"`{status}`" in section, status


class TestLockReferenceDocumentsProjectOverrides:
    """The CLI reference lists every ``--project-*`` flag and how it combines."""

    def test_every_project_flag_is_documented_as_replacing(self) -> None:
        prefix = "--project-"
        prose = "\n\n".join(
            para
            for para in _reference_section("## `nab lock`").split("\n\n")
            if prefix in para
        )
        for spec in OPTIONS:
            if spec.cli_flag is not None and spec.cli_flag.startswith(prefix):
                assert f"`{spec.cli_flag}`" in prose, spec.cli_flag

        assert "replaces the file value" in prose
        assert "append" not in prose


_FLAG = "--include-rejected"


def _prose_chunks(section: str) -> list[str]:
    """Split a reference section into one string per paragraph and bullet."""
    chunks: list[str] = []
    current: list[str] = []
    for raw in section.splitlines():
        line = raw.strip()
        if current and (not line or raw.startswith("* ")):
            chunks.append(" ".join(current))
            current = []
        if line:
            current.append(line)

    if current:
        chunks.append(" ".join(current))
    return chunks


def _include_rejected_chunks() -> list[str]:
    return [
        c for c in _prose_chunks(_reference_section("## `nab config`")) if _FLAG in c
    ]


def _action_bullet(action: str) -> str:
    """The ``--include-rejected`` bullet for one ``nab config`` action."""
    prefix = f"* `nab config {action}"
    return next(c for c in _include_rejected_chunks() if c.startswith(prefix))


class TestCliReferenceDocumentsIncludeRejected:
    """The CLI reference describes ``--include-rejected`` as it behaves.

    The flag decides whether a refused source is fatal, and ``list`` is the
    only action that shows a refusal naming no config option.
    """

    def test_list_shows_a_rejection_explain_cannot_reach(
        self,
        hermetic_roots: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFLINE", "1")
        path = str(hermetic_roots / "pyproject.toml")

        listed = _run_config(["list", _FLAG, "--path", path])
        with caplog.at_level(logging.WARNING, logger="nab_project"):
            _run_config(["explain", "offline", "--path", path])
            warned = caplog.text
            caplog.clear()
            explained = _run_config(["explain", "offline", _FLAG, "--path", path])
            silenced = caplog.text

        assert "NAB_OFLINE" in listed
        assert "NAB_OFLINE" not in explained

        assert "NAB_OFLINE" in warned
        assert "NAB_OFLINE" not in silenced

        assert "stderr" in _action_bullet("explain")

    def test_rejected_section_is_documented(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFLINE", "1")
        out = _run_config(
            ["list", _FLAG, "--path", str(hermetic_roots / "pyproject.toml")]
        )

        label = "rejected:"
        assert any(line.strip() == label for line in out.splitlines())
        assert f"`{label}`" in _reference_section("## `nab config`")

    def test_exit_without_the_flag_is_documented(self, hermetic_roots: Path) -> None:
        _project(hermetic_roots)
        _write(hermetic_roots / "nab.toml", 'resolutionn = "lowest"\n')
        path = str(hermetic_roots / "pyproject.toml")

        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            pytest.raises(SystemExit) as exc,
        ):
            app.cli(args=["config", "list", "--path", path], prog="nab")

        assert out.getvalue() == ""
        assert "config error" in err.getvalue()
        assert "resolutionn" in _run_config(["list", _FLAG, "--path", path])
        assert f"exits {exc.value.code}" in _reference_section("## `nab config`")

    def test_get_renders_no_rejection(
        self,
        hermetic_roots: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _project(hermetic_roots)
        _write(hermetic_roots / "nab.toml", 'resolutionn = "lowest"\n')
        monkeypatch.setenv("NAB_OFLINE", "1")
        path = str(hermetic_roots / "pyproject.toml")

        with caplog.at_level(logging.WARNING, logger="nab_project"):
            out = _run_config(["get", "resolution", _FLAG, "--path", path])

        assert out == "highest\n"
        # The flag silences the NAB_* warning and prints nothing in its place.
        assert "NAB_OFLINE" not in caplog.text
        assert "stderr" in _action_bullet("get")


def _console_block(text: str, command: str) -> list[str]:
    """The output lines of the ``console`` block whose first line is ``command``."""
    for body in re.findall(r"```console\n(.*?)\n```", text, re.DOTALL):
        lines = body.splitlines()
        if lines[0] == command:
            return lines[1:]

    msg = f"no console block for {command!r}"
    raise AssertionError(msg)


class TestConflictsDocTranscript:
    """The conflicts page quotes the refusal lines the CLI prints."""

    _UMBRELLA = (
        '[project]\nname = "proj"\nversion = "0.1.0"\ndependencies = []\n'
        "[project.optional-dependencies]\n"
        "cpu = []\n"
        "gpu = []\n"
        'all = ["proj[cpu]", "proj[gpu]"]\n'
        "[tool.nab]\n"
        'conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'
    )

    _DEFAULT_GROUPS = (
        '[project]\nname = "proj"\nversion = "0.1.0"\ndependencies = []\n'
        "[dependency-groups]\n"
        'black22 = ["black==22.1.0"]\n'
        'black23 = ["black==23.12.0"]\n'
        "[tool.nab]\n"
        'default-groups = ["black22", "black23"]\n'
        'conflicts = [[{ group = "black22" }, { group = "black23" }]]\n'
    )

    def test_umbrella_refusal_matches_documented_transcript(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An umbrella extra reaching both members prints the page's line."""
        pyproject = _write(tmp_path / "pyproject.toml", self._UMBRELLA)
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, cache_dir=tmp_path / "cache", extras=("all",))
        printed = capsys.readouterr().err.strip()

        doc = _CONFLICTS_DOC.read_text(encoding="utf-8")
        assert printed in _console_block(doc, "$ nab lock --extras all")

    def test_default_groups_refusal_matches_documented_transcript(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two default groups in one exclusive set print the page's line."""
        pyproject = _write(tmp_path / "pyproject.toml", self._DEFAULT_GROUPS)
        with pytest.raises(SystemExit, match="1"):
            lock(pyproject, cache_dir=tmp_path / "cache")
        printed = capsys.readouterr().err.strip()

        doc = _CONFLICTS_DOC.read_text(encoding="utf-8")
        assert printed in _console_block(doc, "$ nab lock")


class TestConflictsDocForkingPolicies:
    """The conflicts page names which policies fork a co-selecting run."""

    _GROUPS = (
        '[project]\nname = "proj"\nversion = "0.1.0"\ndependencies = []\n'
        "[dependency-groups]\n"
        "a = []\n"
        "b = []\n"
    )

    def _body(self, policy: str) -> str:
        """The two-group project with ``a`` and ``b`` conflicting under ``policy``."""
        members = '[{ group = "a" }, { group = "b" }]'
        return (
            self._GROUPS
            + "[tool.nab]\n"
            + f'conflicts = [{{ members = {members}, policy = "{policy}" }}]\n'
        )

    def _unwrapped_claim(self, needle: str) -> str:
        """The page's paragraph holding ``needle``, unwrapped so a claim matches."""
        doc = _CONFLICTS_DOC.read_text(encoding="utf-8")
        return " ".join(_doc_paragraph(doc, needle).split())

    def test_exactly_one_forks_co_selected_members(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An exclusive set resolves each co-selected member on its own."""
        labels = _emitted_labels(
            tmp_path, capsys, self._body("exactly-one"), groups=("a", "b")
        )
        assert labels == ["# host-group-a", "# host-group-b"]

        claim = self._unwrapped_claim("When the selection activates")
        assert "an exclusive set (`at-most-one` or `exactly-one`)" in claim

    def test_at_least_one_co_selection_stays_one_resolve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An at-least-one set permits co-selection, so the run does not fork."""
        labels = _emitted_labels(
            tmp_path, capsys, self._body("at-least-one"), groups=("a", "b")
        )
        assert labels == []

        claim = self._unwrapped_claim("The require-one policies")
        assert "`at-least-one` permits it" in claim
