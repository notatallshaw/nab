"""Exercise Mercurial acquisition through the subprocess and cache boundaries."""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request, build_opener

import pytest

from nab_index import _hg_no_redirect, _mercurial
from nab_index.vcs import VcsCloneError, VcsRequest, prepare_clone

_NODE = "a" * 40
_COMMANDS = {"version", "init", "pull", "nab-pull", "log", "manifest", "update"}


class Client:
    """Represent the result of each external client operation."""

    def __init__(self, executable: Path) -> None:
        self.executable = str(executable)
        self.calls: list[tuple[str, ...]] = []
        self.version = b"7.2.4"
        self.node = _NODE.encode()
        self.checked_node = self.node
        self.manifest = b"\0pyproject.toml\0"
        self.failure: Exception | None = None

    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        **options: object,
    ) -> subprocess.CompletedProcess[bytes]:
        """Provide a result after checking the invocation's isolation settings."""
        self.calls.append(args)
        assert args[0] == self.executable
        assert "--noninteractive" in args
        assert "ui.clonebundles=false" in args
        assert "subrepos.allowed=false" in args
        assert env["HGRCPATH"] == ""
        assert env["HGRCSKIPREPO"] == "1"
        assert env["HGPLAIN"] == "+strictflags"
        assert "PYTHONPATH" not in env
        assert all(Path(entry).is_absolute() for entry in env["PATH"].split(os.pathsep))
        assert options["stdin"] == subprocess.DEVNULL
        assert options["check"]
        assert options["stderr"] == subprocess.DEVNULL
        assert options["timeout"]

        command = next(arg for arg in args if arg in _COMMANDS)
        data = b""
        reads_output = command in {"version", "log", "manifest"}
        assert options["stdout"] == (
            subprocess.PIPE if reads_output else subprocess.DEVNULL
        )
        if command == "init":
            repository = Path(args[-1])
        elif command != "version":
            repository = Path(args[args.index("--repository") + 1])

        if command != "version":
            assert repository.is_absolute()
            assert cwd == repository.parent

        if command == "version":
            data = self.version
            assert cwd == Path(Path(self.executable).anchor)
        elif command == "init":
            (repository / ".hg").mkdir()
        elif command in {"pull", "nab-pull"} and self.failure is not None:
            raise self.failure
        elif command == "manifest":
            data = self.manifest
        elif command == "log":
            data = (
                self.checked_node if args[args.index("--rev") + 1] == "." else self.node
            )
        elif command == "update":
            (repository / "pyproject.toml").write_text(
                '[project]\nname="demo"\nversion="1"\n', encoding="utf-8"
            )
        return subprocess.CompletedProcess(
            args, 0, data if reads_output else None, None
        )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Client:
    directory = tmp_path / "bin"
    executable = directory / ("hg.exe" if os.name == "nt" else "hg")
    result = Client(executable)
    monkeypatch.setenv(
        "PATH", os.pathsep.join(("relative", str(tmp_path / "missing"), str(directory)))
    )
    monkeypatch.setenv("HGRCPATH", "ambient.rc")
    monkeypatch.setenv("PYTHONPATH", "ambient")
    monkeypatch.setattr(
        _mercurial.shutil,
        "which",
        lambda path: path if path == str(executable) else None,
    )
    monkeypatch.setattr(_mercurial.subprocess, "run", result.run)
    return result


@pytest.mark.parametrize("scheme", ["https", "file"])
def test_hg_checks_out_the_node_and_reuses_the_completed_tree(
    tmp_path: Path, client: Client, scheme: str
) -> None:
    repo = "https://example.com/demo" if scheme == "https" else "file:///tmp/demo"
    request = VcsRequest("hg", repo, _NODE.upper(), "")
    # A tracked symlink may be materialized as an ordinary file by the host.
    client.manifest = b"@\0pyproject.toml\0"

    result = prepare_clone(
        tmp_path, request, require_pin=False, offline=scheme == "file"
    )

    assert result.commit_sha == _NODE
    assert (result.path / "pyproject.toml").is_file()
    pull = client.calls[2]
    assert pull[-4:] == ("--rev", _NODE, "--", repo)
    if scheme == "https":
        assert "nab-pull" in pull
        assert any(arg.startswith("extensions.nab_no_redirect=") for arg in pull)
    else:
        assert "pull" in pull

    client.calls.clear()
    assert prepare_clone(tmp_path, request, require_pin=True, offline=True) == result
    assert client.calls == []


def test_hg_refuses_an_uncached_remote_source_offline(
    tmp_path: Path, client: Client
) -> None:
    request = VcsRequest("hg", "https://example.com/demo", _NODE, "")

    with pytest.raises(VcsCloneError, match="offline mode"):
        prepare_clone(tmp_path, request, require_pin=True, offline=True)

    assert client.calls == []


def test_hg_refuses_floating_refs_before_using_the_client(
    tmp_path: Path, client: Client
) -> None:
    request = VcsRequest("hg", "https://example.com/demo", "tip", "")

    with pytest.raises(VcsCloneError, match="full 40-character changeset"):
        prepare_clone(tmp_path, request, require_pin=False)

    assert client.calls == []


def test_unrecognized_backend_does_not_run_a_client(
    tmp_path: Path, client: Client
) -> None:
    request = VcsRequest("svn", "https://example.com/demo", _NODE, "")

    with pytest.raises(VcsCloneError, match="unknown VCS backend"):
        prepare_clone(tmp_path, request, require_pin=True)

    assert client.calls == []


@pytest.mark.parametrize("absolute", [False, True])
def test_hg_requires_an_absolute_executable_on_path(
    tmp_path: Path,
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
    *,
    absolute: bool,
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "missing") if absolute else "relative")
    request = VcsRequest("hg", "file:///tmp/demo", _NODE, "")

    with pytest.raises(VcsCloneError, match="hg executable on PATH"):
        prepare_clone(tmp_path, request, require_pin=True)

    assert client.calls == []


@pytest.mark.parametrize("version", [b"7.2.3", b"7.3rc1", b"unknown", b"\xff"])
def test_hg_refuses_unverified_client_versions(
    tmp_path: Path, client: Client, version: bytes
) -> None:
    client.version = version
    request = VcsRequest("hg", "file:///tmp/demo", _NODE, "")

    with pytest.raises(VcsCloneError, match="requires hg 7.2.4 or newer"):
        prepare_clone(tmp_path, request, require_pin=True)

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (subprocess.TimeoutExpired("hg", 1800), "timed out"),
        (subprocess.CalledProcessError(255, "hg"), "exit status 255"),
        (OSError("cannot execute"), "could not run Mercurial pull"),
    ],
)
def test_hg_pull_failures_leave_no_cache_entry(
    tmp_path: Path, client: Client, failure: Exception, message: str
) -> None:
    client.failure = failure
    request = VcsRequest("hg", "file:///tmp/demo", _NODE, "")

    with pytest.raises(VcsCloneError, match=message):
        prepare_clone(tmp_path, request, require_pin=True)

    assert not list(tmp_path.rglob(".hg"))


@pytest.mark.parametrize(
    ("field", "data", "message"),
    [
        ("node", b"b" * 40, "fetched a different changeset"),
        ("checked_node", b"b" * 40, "checked out a different changeset"),
        ("manifest", b"\0.hgsub\0", "subrepositories"),
        ("manifest", b"\0.hgsubstate\0", "subrepositories"),
        ("manifest", b"\0file", "invalid manifest"),
        ("manifest", b"\0", "invalid manifest"),
    ],
)
def test_hg_refuses_unintended_checkout_contents(
    tmp_path: Path, client: Client, field: str, data: bytes, message: str
) -> None:
    setattr(client, field, data)
    request = VcsRequest("hg", "file:///tmp/demo", _NODE, "")

    with pytest.raises(VcsCloneError, match=message):
        prepare_clone(tmp_path, request, require_pin=True)

    if field != "checked_node":
        assert not any("update" in call for call in client.calls)
    assert not list(tmp_path.rglob(".hg"))


def test_hg_marker_write_failure_leaves_no_cache_entry(
    tmp_path: Path, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_path: Path) -> None:
        raise OSError("read only")

    monkeypatch.setattr(Path, "touch", fail)
    request = VcsRequest("hg", "file:///tmp/demo", _NODE, "")

    with pytest.raises(VcsCloneError, match="mark the Mercurial checkout complete"):
        prepare_clone(tmp_path, request, require_pin=True)

    assert any("update" in call for call in client.calls)
    assert not list(tmp_path.rglob(".hg"))


@pytest.mark.parametrize("target", ["file", "../outside", "missing", "absolute"])
def test_hg_checkout_links_stay_within_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    root = tmp_path.resolve() / "checkout"
    root.mkdir()
    (root / "file").write_text("inside", encoding="utf-8")
    (tmp_path / "outside").write_text("outside", encoding="utf-8")
    destination = root / "file" if target == "absolute" else Path(target)
    link = root / "link"
    try:
        link.symlink_to(destination)
    except OSError:
        # Some Windows runners cannot create symlinks.
        is_symlink, readlink, resolve = Path.is_symlink, Path.readlink, Path.resolve
        monkeypatch.setattr(
            Path, "is_symlink", lambda path: path == link or is_symlink(path)
        )
        monkeypatch.setattr(
            Path,
            "readlink",
            lambda path: destination if path == link else readlink(path),
        )

        def resolve_link(path: Path, *, strict: bool = False) -> Path:
            if path == link:
                return resolve(root / destination, strict=strict)
            return resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve_link)

    if target == "file":
        _mercurial._check_links(root, [b"link"])
        assert link.resolve().read_text(encoding="utf-8") == "inside"
    else:
        with pytest.raises(VcsCloneError, match="unsafe link"):
            _mercurial._check_links(root, [b"link"])


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_extension_registers_a_pull_that_refuses_redirects(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    url = ModuleType("mercurial.url")
    url.handlerfuncs = []
    commands = ModuleType("mercurial.commands")
    commands.table = {b"pull": object()}
    modules = {"mercurial.url": url, "mercurial.commands": commands}
    monkeypatch.setattr(_hg_no_redirect, "import_module", modules.__getitem__)

    with patch.dict(_hg_no_redirect.cmdtable, clear=True):
        _hg_no_redirect.extsetup(None)
        assert _hg_no_redirect.cmdtable == {b"nab-pull": commands.table[b"pull"]}

    (handler_factory,) = url.handlerfuncs
    opener = build_opener(handler_factory(None, None))
    request = Request("https://example.com/admitted")
    with pytest.raises(URLError, match="Mercurial HTTPS redirects are disabled"):
        opener.error(
            "http",
            request,
            io.BytesIO(),
            status,
            "Redirect",
            {"location": "https://example.com/elsewhere"},
        )
