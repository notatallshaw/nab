"""Check the published CLI pages against commands and emitters.

The CLI reference owns subcommands, flags, environment variables, and statuses.
Other pages own selection, formats, failures, config overrides, and refusal
text.

These tests read ``docs/``, which the umbrella sdist does not ship, so the
module is on that sdist's exclude list in pyproject.toml.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from nab._cli import spec as cli_spec
from nab._cli.parse import parse
from nab._lock import lock
from nab.cli import run
from nab.config.ladder import OPTIONS
from nab.optiontable import ALL
from nab.output import ColorChoice, Verbosity
from nab_project.lockfile import (
    ArchivePin,
    IndexPin,
    LocalPin,
    LockInput,
    PinShape,
    TargetLock,
    VcsPin,
    WheelArtifact,
    write_requirements_with_hashes,
    write_requirements_without_hashes,
)
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget

_DOCS = Path(__file__).resolve().parents[1] / "docs"
_CLI_REFERENCE = _DOCS / "reference" / "cli.md"
_SELECTION = _DOCS / "reference" / "selection.md"
_FORMATS = _DOCS / "reference" / "formats.md"
_DIAGNOSTICS = _DOCS / "reference" / "diagnostics.md"
_CONFIG_REFERENCE = _DOCS / "reference" / "configuration.md"
_LOCKFILE_REFERENCE = _DOCS / "reference" / "lockfile.md"
_CONFLICTS_DOC = _DOCS / "explanation" / "conflicts.md"
_README = Path(__file__).resolve().parents[1] / "README.md"

# The published documentation, which is what ``nab config explain`` sends a
# reader to and what the reference page tells them to expect.
_DOCS_SITE = "https://nab.readthedocs.io/"

_SUBCOMMANDS = ("lock", "download", "config", "cache")

# The shortest line each subcommand accepts, so a case that only wants to
# prove a global flag parses does not have to invent one per command.
_SUBCOMMAND_LINES = {
    "lock": (),
    "download": (),
    "config": ("list",),
    "cache": ("dir",),
}

# The flag counts lock and download carry.  The other two commands have no
# documented flag list, so nothing here derives theirs.
_FLAG_COUNTS = {"lock": 36, "download": 32}

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


def _run_config(args: list[str], *, status: int = 0) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert run(("config", *args)) == status
    return buf.getvalue()


def _flag_forms(flag: str, *, wildcard: str) -> list[str]:
    """Return the forms a page may use for ``flag``.

    A ``--project-`` flag is also covered by ``wildcard``, the placeholder a
    page writes when it stands for the whole family rather than one member.
    """
    forms = [flag, f"--no-{flag.removeprefix('--')}"]
    if flag.startswith("--project-"):
        forms.append(wildcard)
    return forms


def _names_flag(text: str, flag: str) -> bool:
    """Whether ``text`` names ``flag``, its ``--no-`` form, or a covering wildcard."""
    return any(
        re.search(rf"`{re.escape(form)}(?![\w-])", text)
        for form in _flag_forms(flag, wildcard="--project-*")
    )


def _names_value(text: str, value: str) -> bool:
    """Whether ``text`` writes ``value`` as a literal: a code span or a quoted token."""
    return re.search(rf"""[`"']{re.escape(value)}(?![\w-])""", text) is not None


def _unwrapped(text: str) -> str:
    """``text`` with its line wrapping removed, so a claim matches on one line."""
    return " ".join(text.split())


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


def _command_flags(command: str) -> list[str]:
    """The flags ``command`` accepts, off the one option declaration.

    The size is asserted here because three of the four readers assert
    inside a loop over this, and an empty list would pass them all.
    """
    flags = [
        row.cli_flag
        for row in ALL
        if command in row.commands and row.cli_flag is not None
    ]
    assert len(flags) == _FLAG_COUNTS[command], flags
    return flags


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


class TestEveryRowNamesAPage:
    """A row's ``docs=`` is where ``nab config explain`` sends a reader.

    Nothing else checks the page is a file: the umbrella sdist ships
    ``src/nab`` and ``tests`` alone, so a row cannot look for it as it is
    built without raising on every installed nab.
    """

    def test_no_row_names_a_page_that_is_not_there(self) -> None:
        missing = sorted({row.docs for row in ALL if not (_DOCS / row.docs).is_file()})

        assert missing == []

    def test_a_key_links_to_a_page_that_writes_out_its_values(self) -> None:
        """The page a key names writes out every value that key takes.

        A value counts only where the page writes it as a literal, so the
        word in ordinary prose does not stand in for the documented value.
        The key count is asserted because an empty list would pass the loop.
        """
        enumerated = [row for row in OPTIONS if row.choices]
        assert len(enumerated) == 6, [row.name for row in enumerated]

        unlisted: dict[str, list[str]] = {}
        for row in enumerated:
            page = _page(_DOCS / row.docs)
            missing = [value for value in row.choices if not _names_value(page, value)]
            if missing:
                unlisted[row.name] = missing

        assert unlisted == {}


class TestCliReferenceFlagCoverage:
    """Every flag a run subcommand accepts is named on one of its pages."""

    @pytest.mark.parametrize(
        ("heading", "command", "pages"),
        [
            ("## `nab lock`", "lock", (_SELECTION, _FORMATS)),
            ("## `nab download`", "download", ()),
        ],
    )
    def test_section_names_every_flag(
        self, heading: str, command: str, pages: tuple[Path, ...]
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
        for flag in _command_flags("lock"):
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

        ``-q`` is a root row, which every command's line may also carry, so
        the enumeration must include ``cache``.
        """
        for sub, verbs in _SUBCOMMAND_LINES.items():
            line = ("-q", sub, *verbs)
            parsed = parse(line, cli_spec.ROOT, cli_spec.COMMANDS, "nab")

            assert parsed.command == sub
            assert parsed.options["quiet"] == 1

        scope = next(
            para
            for para in _reference_section(_CLI_REFERENCE, "## Output control").split(
                "\n\n"
            )
            if "They work with" in para
        )
        for sub in _SUBCOMMANDS:
            assert f"`{sub}`" in scope, (
                f"Output control scope omits the {sub!r} subcommand"
            )


class TestConfigExplainReferenceDocs:
    """The CLI reference describes what ``explain`` prints."""

    def test_reference_names_every_status(
        self, hermetic_roots: Path, tmp_path: Path
    ) -> None:
        # Exercise a rejected user file, shadowed pyproject binding, and
        # winning CLI source.
        _write(
            hermetic_roots / "pyproject.toml",
            '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
            '[tool.nab]\nresolution = "lowest"\nmode = "universal"\n'
            '[tool.nab.matrix]\npython = ">=3.11,<3.14"\n'
            'platforms = ["linux_x86_64", "macos_arm64"]\n',
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
        ) + _run_config(
            [
                "explain",
                "matrix",
                "--project-matrix-platforms",
                "macos_arm64",
                "--include-rejected",
                "--path",
                str(hermetic_roots / "pyproject.toml"),
            ]
        )

        section = _reference_section(_CLI_REFERENCE, "## `nab config`")
        for status in ("winner", "shadowed", "rejected", "merged"):
            assert status in printed, status
            assert f"`{status}`" in section, status

    def test_reference_names_the_documentation_line(self, hermetic_roots: Path) -> None:
        _write(
            hermetic_roots / "pyproject.toml",
            '[project]\nname = "x"\nversion = "0"\ndependencies = []\n',
        )

        printed = _run_config(
            ["explain", "resolution", "--path", str(hermetic_roots / "pyproject.toml")]
        )

        docs_line = printed.splitlines()[2]
        section = _reference_section(_CLI_REFERENCE, "## `nab config`")

        assert docs_line.startswith(f"  see {_DOCS_SITE}")
        assert _DOCS_SITE in section


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
        with redirect_stdout(out), redirect_stderr(err):
            status = run(("config", "list", "--path", path))

        assert status == 1
        assert out.getvalue() == ""
        assert "config error" in err.getvalue()
        assert "resolutionn" in _run_config(["list", _FLAG, "--path", path])
        assert f"exits {status}" in _reference_section(
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
        return _unwrapped(_doc_paragraph(doc, needle))

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


_WITHOUT_HASHES = "requirements-without-hashes"
_ARCHIVE_URL = "https://example.com/my-archive-1.0.tar.gz"
_ARCHIVE_DIGEST = "c" * 64
_COMMIT = "d" * 40
_WHEEL_DIGEST = "a" * 64
_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)
_NAME_EQUALS_VERSION = re.compile(r"[^\s@]+==\S+")


def _pins_by_shape(directory: Path) -> dict[str, PinShape]:
    """One pin per source shape, keyed by the name the docs give the shape."""
    return {
        "index": IndexPin(
            name="fastapi",
            version="0.109.1",
            index="pypi",
            wheels=(
                WheelArtifact(
                    filename="fastapi-0.109.1-py3-none-any.whl",
                    url="https://example.com/fastapi-0.109.1-py3-none-any.whl",
                    hashes=(("sha256", _WHEEL_DIGEST),),
                ),
            ),
        ),
        "archive": ArchivePin(
            name="my-archive",
            version="1.0",
            url=_ARCHIVE_URL,
            hashes=(("sha256", _ARCHIVE_DIGEST),),
        ),
        "local": LocalPin(name="my-fork", version="2.0", path=str(directory)),
        "vcs": VcsPin(
            name="some-pkg",
            version="0.0.0+vcs",
            repo_url=f"git+https://github.com/me/x.git@{_COMMIT}",
            bare_repo_url="https://github.com/me/x.git",
            commit_id=_COMMIT,
        ),
    }


def _requirements_lines(pins: Iterable[PinShape], *, with_hashes: bool) -> list[str]:
    """The requirements lines ``pins`` produce in one of the two formats."""
    lock_input = LockInput(
        targets={
            _TARGET.label: TargetLock(
                target=_TARGET, pins={pin.name: pin for pin in pins}
            )
        }
    )
    write = (
        write_requirements_with_hashes
        if with_hashes
        else write_requirements_without_hashes
    )
    return write(lock_input).splitlines()


def _format_bullets(text: str) -> str:
    """The ``--format`` bullets of a stretch of page text, joined into one string."""
    return " ".join(
        chunk for chunk in _prose_chunks(text) if chunk.startswith("* `--format")
    )


def _format_summaries() -> dict[str, str]:
    """The user-facing ``nab lock --format`` summaries, keyed by where each lives.

    Each reference page carries a bullet per format, and the README is the
    distribution's PyPI description.
    """
    preamble = _page(_LOCKFILE_REFERENCE).partition("\n## ")[0]
    readme = _page(_README)

    summaries = {
        "formats.md": _format_bullets(_reference_section(_FORMATS, "## `--format`")),
        "lockfile.md": _format_bullets(preamble),
        "README.md": _unwrapped(_doc_paragraph(readme, _WITHOUT_HASHES)),
    }

    for source, summary in summaries.items():
        assert _WITHOUT_HASHES in summary, f"{source} no longer names the format"

    return summaries


class TestLockFormatSummaries:
    """The ``nab lock --format`` summaries describe what the emitters print.

    Only an index pin renders as ``name==version``; a local, VCS or archive
    pin is a URL line, and an archive pin's URL carries its digest in both
    formats.
    """

    def test_only_an_index_pin_renders_name_equals_version(
        self, tmp_path: Path
    ) -> None:
        """One pin of each shape gives one pinned line and three URL lines."""
        shapes = _pins_by_shape(tmp_path)
        assert _requirements_lines(shapes.values(), with_hashes=False) == [
            "fastapi==0.109.1",
            f"my-archive @ {_ARCHIVE_URL}#sha256={_ARCHIVE_DIGEST}",
            f"my-fork @ {tmp_path.resolve().as_uri()}",
            f"some-pkg @ git+https://github.com/me/x.git@{_COMMIT}",
        ]

    def test_dropping_the_hash_lines_leaves_the_url_lines_alone(
        self, tmp_path: Path
    ) -> None:
        """Only the index pin's line differs between the two formats."""
        shapes = _pins_by_shape(tmp_path)
        hashed = _requirements_lines(shapes.values(), with_hashes=True)
        plain = _requirements_lines(shapes.values(), with_hashes=False)

        assert hashed[:2] == [
            "fastapi==0.109.1 \\",
            f"    --hash=sha256:{_WHEEL_DIGEST}",
        ]
        assert hashed[2:] == plain[1:]

    def test_summaries_scope_hash_removal_to_index_hash_lines(
        self, tmp_path: Path
    ) -> None:
        """Only index hash lines disappear; an archive digest remains in its URL."""
        plain = _requirements_lines(
            _pins_by_shape(tmp_path).values(), with_hashes=False
        )
        assert any(f"sha256={_ARCHIVE_DIGEST}" in line for line in plain)

        for source, summary in _format_summaries().items():
            normalized = summary.lower().replace("`", "")
            assert "index pin" in normalized, source
            assert "hash line" in normalized, source

        reference_sections = {
            "formats.md": _reference_section(_FORMATS, "## `--format`"),
            "lockfile.md": _page(_LOCKFILE_REFERENCE).partition("\n## ")[0],
        }
        for source, text in reference_sections.items():
            bullet = next(
                chunk
                for chunk in _prose_chunks(text)
                if chunk.startswith("* `--format requirements`")
            )
            normalized = bullet.lower().replace("`", "")
            assert "index pin" in normalized, source

        for source in ("formats.md", "lockfile.md"):
            assert "archive" in _format_summaries()[source].lower(), source
            assert "digest" in _format_summaries()[source].lower(), source

    def test_a_summary_naming_name_equals_version_says_which_pins(self) -> None:
        """The phrase covers index pins only, so a summary using it says so."""
        for source, summary in _format_summaries().items():
            if "name==version" in summary.replace("`", ""):
                assert "index pin" in summary.lower(), source

    def test_reference_pages_name_every_shape_that_renders_as_a_url(
        self, tmp_path: Path
    ) -> None:
        """Every shape whose line is a URL is named on both reference pages."""
        url_shapes = {
            shape
            for shape, pin in _pins_by_shape(tmp_path).items()
            if not _NAME_EQUALS_VERSION.fullmatch(
                _requirements_lines([pin], with_hashes=False)[0]
            )
        }
        assert url_shapes == {"archive", "local", "vcs"}

        summaries = _format_summaries()
        for source in ("formats.md", "lockfile.md"):
            for shape in sorted(url_shapes):
                assert shape in summaries[source].lower(), f"{source} omits {shape}"


class TestCliReferenceMatchesTheseFourBehaviours:
    """Check four CLI-page claims against the implementation."""

    @staticmethod
    def _rows(heading: str) -> dict[str, str]:
        """Each markdown table row under ``heading``, keyed by its first cell."""
        rows: dict[str, str] = {}
        for line in _reference_section(_CLI_REFERENCE, heading).splitlines():
            if line.startswith("|"):
                cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
                rows[cells[0]] = " ".join(cells[1:])
        return rows

    def test_the_backend_paragraph_quotes_the_refusals_it_prints(self) -> None:
        page = _page(_CLI_REFERENCE)
        source = Path(run.__module__.replace(".", "/")).parent / "_resolve.py"
        body = (Path(__file__).resolve().parents[1] / "src" / source).read_text()

        assert "ImportError" not in page
        for fragment in ("httpx is not installed", "without HTTP/2 support"):
            assert fragment in page, fragment
            assert fragment in body, fragment

    def test_the_exit_two_row_names_an_unknown_action(self, tmp_path: Path) -> None:
        assert run(("cache", "bogus", "--cache-dir", str(tmp_path))) == 2

        rows = self._rows("## Exit codes")
        assert "action" in rows["2"]
        assert "action" not in rows["1"]

    def test_the_environment_table_names_the_config_root(self) -> None:
        assert "XDG_CONFIG_HOME" in self._rows("## Environment variables")

    def test_list_reports_the_winning_source_and_explain_the_option(self) -> None:
        text = _reference_section(_CLI_REFERENCE, "## `nab config`")

        assert "the scope of" in _doc_paragraph(text, "config list")
        assert "explain" in _doc_paragraph(text, "config list")
