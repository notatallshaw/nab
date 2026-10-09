"""CLI behavior and executable documentation examples."""

from __future__ import annotations

import builtins
import io
import itertools
import logging
import re
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from nab._cli import spec as cli_spec
from nab._cli.definition.options import ALL
from nab._cli.parse import parse
from nab._lock import lock
from nab._resolve import _make_transport
from nab.cli import run
from nab.output import ColorChoice, Printer, ProgressReporter, Verbosity
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
_CONFLICTS_DOC = _DOCS / "explanation" / "conflicts.md"

_DOCS_SITE = "https://nab.readthedocs.io/"


# The shortest line each subcommand accepts, so a case that only wants to
# prove a global flag parses does not have to invent one per command.
_SUBCOMMAND_LINES = {
    "lock": (),
    "download": (),
    "config": ("list",),
    "cache": ("dir",),
}

# Carriage return plus "erase to end of line": the prefix of every repaint.
_CLEAR_LINE = "\r\033[K"

_MAX_SPINNER_FRAMES = 64


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


class _Tty(io.StringIO):
    """Capture output as a terminal stream."""

    def isatty(self) -> bool:
        return True


def _documented_progress_line() -> str:
    """The progress line the CLI reference shows in its fenced example."""
    section = _reference_section(_CLI_REFERENCE, "## Output control")
    block = re.search(r"```\n(.*?)\n```", section, re.DOTALL)
    if block is None:
        msg = "no fenced progress example under ## Output control"
        raise AssertionError(msg)
    return block.group(1)


def _painted_progress_lines(fetched: int, pinned: int) -> set[str]:
    """Return one spinner cycle at the requested counts."""
    err = _Tty()
    printer = Printer(
        stderr=err, verbosity=Verbosity.NORMAL, color=ColorChoice.NEVER, env={}
    )

    # Advance past the repaint throttle on each clock read.
    ticks = itertools.count(0.0, 1.0)
    reporter = ProgressReporter(printer, clock=lambda: next(ticks))
    for _ in range(fetched):
        reporter.on_fetch()

    painted: set[str] = set()
    for _ in range(_MAX_SPINNER_FRAMES):
        reporter.on_pin(pinned)
        line = err.getvalue().rpartition(_CLEAR_LINE)[2]
        if line in painted:
            return painted
        painted.add(line)

    msg = f"the spinner does not cycle within {_MAX_SPINNER_FRAMES} repaints"
    raise AssertionError(msg)


class TestOptionDocumentationLinks:
    """Option explanation links must target pages in the documentation build."""

    def test_no_row_names_a_page_that_is_not_there(self) -> None:
        missing = sorted({row.docs for row in ALL if not (_DOCS / row.docs).is_file()})
        assert missing == []


class TestCliSelection:
    """Selections combine unless a conflict requires separate resolves."""

    _EXTRAS = (
        '[project]\nname = "proj"\nversion = "0.1.0"\ndependencies = []\n'
        "[project.optional-dependencies]\n"
        "cpu = []\n"
        "gpu = []\n"
    )

    _CONFLICT = '[tool.nab]\nconflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]\n'

    def test_selection_alone_is_one_union_resolve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two extras with no conflict declared resolve together."""
        labels = _emitted_labels(tmp_path, capsys, self._EXTRAS, extras=("cpu", "gpu"))
        assert labels == []

    def test_co_selected_conflict_members_fork_the_resolve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Declaring the same two extras exclusive resolves each separately."""
        labels = _emitted_labels(
            tmp_path, capsys, self._EXTRAS + self._CONFLICT, extras=("cpu", "gpu")
        )
        assert labels == ["# host-extra-cpu", "# host-extra-gpu"]


class TestOutputPolicy:
    """Output flags and rendered progress examples."""

    def test_documented_progress_line_is_one_the_reporter_paints(self) -> None:
        """The fenced example is a real repaint, spinner frame included."""
        documented = _documented_progress_line()
        counts = re.search(r"(\d+) fetched, (\d+) pinned", documented)
        assert counts is not None, f"no counts in progress line {documented!r}"

        fetched, pinned = (int(group) for group in counts.groups())
        assert documented in _painted_progress_lines(fetched, pinned)

    def test_quiet_flag_parses_for_every_subcommand(self) -> None:
        """Every command accepts the global quiet flag."""
        for sub, verbs in _SUBCOMMAND_LINES.items():
            line = ("-q", sub, *verbs)
            parsed = parse(line, cli_spec.ROOT, cli_spec.COMMANDS, "nab")

            assert parsed.command == sub
            assert parsed.options["quiet"] == 1


class TestConfigExplainOutput:
    """Config explanation reports statuses and a documentation URL."""

    def test_explain_prints_every_status(
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

        for status in ("winner", "shadowed", "rejected", "merged"):
            assert status in printed, status

    def test_explain_prints_documentation_url(self, hermetic_roots: Path) -> None:
        _write(
            hermetic_roots / "pyproject.toml",
            '[project]\nname = "x"\nversion = "0"\ndependencies = []\n',
        )

        printed = _run_config(
            ["explain", "resolution", "--path", str(hermetic_roots / "pyproject.toml")]
        )

        docs_line = printed.splitlines()[2]

        assert docs_line.startswith(f"  see {_DOCS_SITE}")


_FLAG = "--include-rejected"


class TestIncludeRejected:
    """Rejected config sources under ``--include-rejected``.

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

    def test_list_labels_rejected_sources(
        self, hermetic_roots: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _project(hermetic_roots)
        monkeypatch.setenv("NAB_OFLINE", "1")
        out = _run_config(
            ["list", _FLAG, "--path", str(hermetic_roots / "pyproject.toml")]
        )

        label = "rejected:"
        assert any(line.strip() == label for line in out.splitlines())

    def test_rejected_file_exits_without_the_flag(self, hermetic_roots: Path) -> None:
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


class TestConflictForkingPolicies:
    """Exclusive conflicts fork co-selected groups."""

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

    def test_exactly_one_forks_co_selected_members(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An exclusive set resolves each co-selected member on its own."""
        labels = _emitted_labels(
            tmp_path, capsys, self._body("exactly-one"), groups=("a", "b")
        )
        assert labels == ["# host-group-a", "# host-group-b"]

    def test_at_least_one_co_selection_stays_one_resolve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An at-least-one set permits co-selection, so the run does not fork."""
        labels = _emitted_labels(
            tmp_path, capsys, self._body("at-least-one"), groups=("a", "b")
        )
        assert labels == []


_ARCHIVE_URL = "https://example.com/my-archive-1.0.tar.gz"
_ARCHIVE_DIGEST = "c" * 64
_COMMIT = "d" * 40
_WHEEL_DIGEST = "a" * 64
_TARGET = ResolveTarget.for_declared(
    python_version="3.11", spec=PlatformSpec("linux_x86_64")
)


def _pins_by_shape(directory: Path) -> dict[str, PinShape]:
    """Return one pin for each supported source shape."""
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


class TestRequirementsFormats:
    """Requirements formats preserve URLs and archive digests.

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


class TestCliRefusals:
    """Optional dependency failures and invalid cache actions."""

    @pytest.mark.parametrize("backend", ["httpx", "httpx2"])
    @pytest.mark.parametrize("missing", ["backend", "h2"])
    def test_missing_backend_dependency_reports_install_command(
        self,
        backend: str,
        missing: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        original_import = builtins.__import__
        blocked = (
            f"nab_index.{backend}_async_transport" if missing == "backend" else "h2"
        )

        def import_without_dependency(
            name: str, *args: object, **kwargs: object
        ) -> object:
            if name == blocked:
                raise ImportError(name)
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", import_without_dependency)
        with pytest.raises(SystemExit) as caught:
            _make_transport(backend)

        assert caught.value.code == 1
        expected = (
            f"{backend} is not installed"
            if missing == "backend"
            else f"{backend} is installed without HTTP/2 support"
        )
        err = capsys.readouterr().err
        assert expected in err
        assert f"pip install nab[{backend}]" in err
        assert "Traceback" not in err

    def test_unknown_cache_action_exits_two(self, tmp_path: Path) -> None:
        assert run(("cache", "bogus", "--cache-dir", str(tmp_path))) == 2
