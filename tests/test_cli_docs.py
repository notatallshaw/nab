"""Check the published CLI pages against what the CLI does.

The reference page lists each subcommand's invocation, its own flags, the env
vars and the statuses; selection, output formats and resolution failures have
a page each; the conflicts page quotes refusal lines verbatim.

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
_SELECTION = _DOCS / "reference" / "selection.md"
_FORMATS = _DOCS / "reference" / "formats.md"
_DIAGNOSTICS = _DOCS / "reference" / "diagnostics.md"
_CONFLICTS_DOC = _DOCS / "explanation" / "conflicts.md"

_SUBCOMMANDS = ("lock", "download", "config", "cache")

# A ``--flag`` opening a code span, so prose naming one is matched and a
# ``--hash=`` inside a fenced example is not.
_CODE_FLAG = re.compile(r"`(--[a-z][\w-]*)")


def _page(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _reference_section(page: Path, heading: str) -> str:
    """The body of ``page`` under ``heading``, up to the next ``##``.

    A ``###`` subheading does not end a section, so a subcommand's own
    subsections come back with it.
    """
    return _page(page).partition(f"\n{heading}\n")[2].partition("\n## ")[0]


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


def _names_flag(text: str, flag: str) -> bool:
    """Whether ``text`` names ``flag``, its ``--no-`` form, or a covering wildcard."""
    forms = [flag, f"--no-{flag.removeprefix('--')}"]
    if flag.startswith("--project-"):
        forms.append("--project-*")
    return any(re.search(rf"`{re.escape(form)}(?![\w-])", text) for form in forms)


def _prose_chunks(text: str) -> list[str]:
    """Split a page or section into one string per paragraph and bullet."""
    chunks: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if current and (not line or raw.startswith("* ")):
            chunks.append(" ".join(current))
            current = []
        if line:
            current.append(line)

    if current:
        chunks.append(" ".join(current))
    return chunks


def _command_flags(command: Callable[..., None]) -> list[str]:
    """The ``--flag`` spelling of every keyword-only parameter of ``command``."""
    return [
        "--" + name.replace("_", "-")
        for name, param in inspect.signature(command).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    ]


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
    """Every flag a run subcommand accepts is named on one of its pages."""

    @pytest.mark.parametrize(
        ("heading", "command", "pages"),
        [
            ("## `nab lock`", lock, (_SELECTION, _FORMATS)),
            ("## `nab download`", download, ()),
        ],
    )
    def test_section_names_every_flag(
        self, heading: str, command: Callable[..., None], pages: tuple[Path, ...]
    ) -> None:
        # Flags shared by both commands are documented once, in Runtime
        # flags; what `nab lock` selects and what it writes have their own
        # pages.
        scope = "\n".join(
            [
                _reference_section(_CLI_REFERENCE, heading),
                _reference_section(_CLI_REFERENCE, "## Runtime flags"),
                *(_page(page) for page in pages),
            ]
        )

        for flag in _command_flags(command):
            assert _names_flag(scope, flag), f"{heading} omits {flag}"


class TestCliReferenceSplitPages:
    """Every page the CLI reference splits into is one hop from it.

    The hop has to hold in both directions: a reader who searches
    ``cli.md`` for a flag has to find it named there, and a flag named
    there has to be documented on the page the link leads to.
    """

    @pytest.mark.parametrize("page", [_SELECTION, _FORMATS, _DIAGNOSTICS])
    def test_cli_reference_links_the_page(self, page: Path) -> None:
        assert f"]({page.name})" in _page(_CLI_REFERENCE), page.name

    @pytest.mark.parametrize("page", [_SELECTION, _FORMATS])
    def test_lock_section_names_the_flags_the_page_documents(self, page: Path) -> None:
        """A browser search of the `nab lock` section finds a flag that moved."""
        section = _reference_section(_CLI_REFERENCE, "## `nab lock`")
        moved = _page(page)
        for flag in _command_flags(lock):
            if _names_flag(moved, flag):
                assert _names_flag(section, flag), f"`nab lock` omits {flag}"

    @pytest.mark.parametrize("page", [_SELECTION, _FORMATS])
    def test_the_page_documents_the_flags_its_link_advertises(self, page: Path) -> None:
        """A flag named beside a link is a signpost until the page carries it.

        Without this, ``test_section_names_every_flag`` would be satisfied
        by the signpost alone and the page it points at never read.
        """
        moved = _page(page)
        for chunk in _prose_chunks(_page(_CLI_REFERENCE)):
            if f"]({page.name})" not in chunk:
                continue
            for flag in _CODE_FLAG.findall(chunk):
                assert _names_flag(moved, flag), f"{page.name} omits {flag}"


class TestCliReferenceSelectionShape:
    """The selection page's paragraph matches the number of resolves run."""

    _EXTRAS = (
        '[project]\nname = "proj"\nversion = "0.1.0"\ndependencies = []\n'
        "[project.optional-dependencies]\n"
        "cpu = []\n"
        "gpu = []\n"
    )

    _CONFLICT = '[tool.nab]\nconflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'

    def _selection_paragraph(self) -> str:
        """The selection page's paragraph stating what a selection resolves to."""
        return _doc_paragraph(_page(_SELECTION), "union resolve")

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
        text = _page(_CLI_REFERENCE)
        for flag in ("-v", "-vv", "-q", "-qq", "--verbose", "--quiet"):
            assert f"`{flag}`" in text, f"CLI reference omits verbosity flag {flag}"

    def test_color_flags_documented(self) -> None:
        text = _page(_CLI_REFERENCE)
        assert "`--color`" in text
        assert "`--no-color`" in text
        for choice in ColorChoice:
            assert f"`{choice.value}`" in text, (
                f"CLI reference omits --color value {choice.value}"
            )

    def test_progress_documented(self) -> None:
        text = _page(_CLI_REFERENCE)
        assert "`--no-progress`" in text
        assert "Resolving" in text

    def test_output_env_vars_documented(self) -> None:
        text = _page(_CLI_REFERENCE)
        for var in ("NAB_VERBOSITY", "NAB_NO_PROGRESS", "NO_COLOR", "FORCE_COLOR"):
            assert var in text, f"CLI reference omits env var {var}"

    def test_nab_verbosity_values_documented(self) -> None:
        text = _page(_CLI_REFERENCE)
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
            for para in _reference_section(_CLI_REFERENCE, "## Output control").split(
                "\n\n"
            )
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

        section = _reference_section(_CLI_REFERENCE, "## `nab config`")
        for status in ("winner", "shadowed", "rejected"):
            assert status in printed, status
            assert f"`{status}`" in section, status


class TestLockReferenceDocumentsProjectOverrides:
    """The CLI reference lists every ``--project-*`` flag and how it combines."""

    def test_every_project_flag_is_documented_as_replacing(self) -> None:
        prefix = "--project-"
        prose = "\n\n".join(
            para
            for para in _reference_section(_CLI_REFERENCE, "## `nab lock`").split(
                "\n\n"
            )
            if prefix in para
        )
        for spec in OPTIONS:
            if spec.cli_flag is not None and spec.cli_flag.startswith(prefix):
                assert f"`{spec.cli_flag}`" in prose, spec.cli_flag

        assert "replaces the file value" in prose
        assert "append" not in prose


_FLAG = "--include-rejected"


def _include_rejected_chunks() -> list[str]:
    return [
        c
        for c in _prose_chunks(_reference_section(_CLI_REFERENCE, "## `nab config`"))
        if _FLAG in c
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
        assert f"`{label}`" in _reference_section(_CLI_REFERENCE, "## `nab config`")

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
        assert f"exits {exc.value.code}" in _reference_section(
            _CLI_REFERENCE, "## `nab config`"
        )

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
