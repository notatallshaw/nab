"""Tests for nab_index.vcs URL parsing + clone helpers.

The clone path shells out to ``git``, so its tests stub
:func:`subprocess.run`.
"""

from __future__ import annotations

import errno
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from nab_index.client import SdistFile, WheelFile  # noqa: F401 - re-exported by helpers
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.vcs import (
    _COMPLETE_MARKER,
    _REPO_SELECTION_VARS,
    VcsCloneError,
    VcsRequest,
    _clone_complete,
    _resolve_sha,
    prepare_clone,
)
from nab_project._testing.coordinator_fake import FakeFetchPort, make_coordinator
from nab_project.fetch import FetchCoordinator
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import VcsPin, build_target_lock
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.version import Version
from nab_provider.metadata import WheelMetadata
from nab_provider.provider import (
    BuildPolicy,
    LocalSource,
    Provider,
    SourceNameMismatchError,
    UnsupportedSdistError,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)
from nab_provider.target import ResolveTarget
from nab_provider.vcs_request import _split_repo_ref


def _mark_complete(clone_dir: Path) -> None:
    """Fabricate a finished cached clone the way ``prepare_clone`` leaves one."""
    (clone_dir / ".git").mkdir(parents=True, exist_ok=True)
    (clone_dir / ".git" / _COMPLETE_MARKER).touch()


# One per transport git can speak, including the ``git://`` daemon protocol
# that no client-side git config reaches.
_STALLED_REPO_URLS = [
    "https://example.invalid/x/y.git",
    "ssh://git@example.invalid/x/y.git",
    "git://example.invalid/x/y.git",
]


def _refuse_git(*_args: object, **_kwargs: object) -> object:
    """Stand in for git on a path that must not shell out at all."""
    msg = "offline mode must not invoke git"
    raise AssertionError(msg)


def _stalled_remote(cmd: list[str], **kwargs: object) -> object:
    """Stand in for git talking to a remote that goes quiet after the handshake."""
    if cmd[1] in {"ls-remote", "fetch"}:
        timeout = kwargs.get("timeout")
        if not isinstance(timeout, (int, float)):
            msg = f"unbounded git {cmd[1]} hangs on a stalled remote"
            raise AssertionError(msg)
        raise subprocess.TimeoutExpired(cmd, float(timeout))

    if cmd[1] == "init":
        (Path(str(kwargs["cwd"])) / ".git").mkdir(exist_ok=True)
    return type("P", (), {"returncode": 0})()


class TestVcsRequestParse:
    def test_https_with_sha(self) -> None:
        req = VcsRequest.parse("git+https://github.com/x/y.git@" + "a" * 40)
        assert req.scheme == "git"
        assert req.repo_url == "https://github.com/x/y.git"
        assert req.ref == "a" * 40
        assert req.subdirectory == ""

    def test_branch_ref(self) -> None:
        req = VcsRequest.parse("git+https://github.com/x/y.git@main")
        assert req.ref == "main"

    def test_no_ref(self) -> None:
        req = VcsRequest.parse("git+https://github.com/x/y.git")
        assert req.ref == ""

    def test_subdirectory_fragment(self) -> None:
        req = VcsRequest.parse("git+https://github.com/x/y.git@v1#subdirectory=pkg/sub")
        assert req.subdirectory == "pkg/sub"
        assert req.ref == "v1"

    def test_egg_fragment_ignored(self) -> None:
        req = VcsRequest.parse(
            "git+https://github.com/x/y.git@v1#egg=pkg&subdirectory=sub"
        )
        assert req.subdirectory == "sub"

    def test_ssh_url(self) -> None:
        req = VcsRequest.parse("git+ssh://git@github.com/x/y.git@" + "b" * 40)
        assert req.repo_url == "ssh://git@github.com/x/y.git"
        assert req.ref == "b" * 40

    def test_user_at_host_no_ref(self) -> None:
        # user@host without ``://`` is the SSH shortcut form
        req = VcsRequest.parse("git+git@github.com:x/y.git")
        assert req.ref == ""

    def test_unrecognised_url_raises(self) -> None:
        with pytest.raises(VcsCloneError, match="not a recognised"):
            VcsRequest.parse("https://example.com/not-vcs")

    def test_hg_scheme_refused(self) -> None:
        with pytest.raises(VcsCloneError, match="not a recognised"):
            VcsRequest.parse("hg+https://hg.example.com/repo@v1.0")

    def test_svn_scheme_refused(self) -> None:
        with pytest.raises(VcsCloneError, match="not a recognised"):
            VcsRequest.parse("svn+https://svn.example.com/repo")

    def test_bzr_scheme_refused(self) -> None:
        with pytest.raises(VcsCloneError, match="not a recognised"):
            VcsRequest.parse("bzr+https://bzr.example.com/repo")

    def test_vcs_prefix_refused(self) -> None:
        """``vcs+`` is not a scheme: ``git+`` is the only prefix stripped."""
        with pytest.raises(VcsCloneError, match="not a recognised"):
            VcsRequest.parse("vcs+https://example.com/repo.git")

    def test_ssh_shortcut_with_ref(self) -> None:
        req = VcsRequest.parse("git+git@github.com:x/y.git@" + "c" * 40)
        assert req.repo_url == "git@github.com:x/y.git"
        assert req.ref == "c" * 40

    def test_parent_subdirectory_rejected(self) -> None:
        with pytest.raises(VcsCloneError, match="unsafe VCS subdirectory"):
            VcsRequest.parse(
                "git+https://ex.com/r.git@" + "a" * 40 + "#subdirectory=../../../../etc"
            )

    def test_absolute_subdirectory_rejected(self) -> None:
        with pytest.raises(VcsCloneError, match="unsafe VCS subdirectory"):
            VcsRequest.parse(
                "git+https://ex.com/r.git@" + "a" * 40 + "#subdirectory=/etc/secrets"
            )

    def test_posix_backslash_parent_escape_rejected(self) -> None:
        with pytest.raises(VcsCloneError, match="unsafe VCS subdirectory"):
            VcsRequest.parse(
                "git+https://ex.com/r.git@" + "a" * 40 + "#subdirectory=c\\d/../.."
            )

    def test_contained_internal_dotdot_allowed(self) -> None:
        req = VcsRequest.parse(
            "git+https://ex.com/r.git@" + "a" * 40 + "#subdirectory=a/../b"
        )
        assert req.subdirectory == "a/../b"


class TestSplitRepoRef:
    def test_url_with_ref(self) -> None:
        repo, ref = _split_repo_ref("https://example/repo.git@v1")
        assert repo == "https://example/repo.git"
        assert ref == "v1"

    def test_url_without_ref(self) -> None:
        repo, ref = _split_repo_ref("https://example/repo.git")
        assert repo == "https://example/repo.git"
        assert ref == ""

    def test_url_with_user_in_netloc(self) -> None:
        repo, ref = _split_repo_ref("https://user@example.com/x/y.git")
        assert repo == "https://user@example.com/x/y.git"
        assert ref == ""

    def test_url_with_user_in_netloc_and_ref(self) -> None:
        repo, ref = _split_repo_ref("https://user@example.com/x/y.git@v1")
        assert repo == "https://user@example.com/x/y.git"
        assert ref == "v1"

    def test_url_with_branch_containing_slash(self) -> None:
        repo, ref = _split_repo_ref("https://example/repo.git@release/1.0")
        assert repo == "https://example/repo.git"
        assert ref == "release/1.0"

    def test_ssh_shortcut(self) -> None:
        repo, ref = _split_repo_ref("git@github.com:x/y.git")
        assert repo == "git@github.com:x/y.git"
        assert ref == ""

    def test_ssh_shortcut_with_ref(self) -> None:
        repo, ref = _split_repo_ref("git@github.com:x/y.git@v1.0")
        assert repo == "git@github.com:x/y.git"
        assert ref == "v1.0"

    def test_url_no_path_no_ref(self) -> None:
        repo, ref = _split_repo_ref("https://example.com")
        assert repo == "https://example.com"
        assert ref == ""


class TestResolveSha:
    def test_pin_already_sha_returns_unchanged(self) -> None:
        sha = "a" * 40
        req = VcsRequest("git", "https://x", sha, "")
        assert _resolve_sha(req, require_pin=True) == sha

    def test_floating_under_pin_raises(self) -> None:
        req = VcsRequest("git", "https://x", "main", "")
        with pytest.raises(VcsCloneError, match=r"vcs\.require-pin"):
            _resolve_sha(req, require_pin=True)

    def test_floating_no_pin_calls_ls_remote(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "1" * 40
        recorded: list[list[str]] = []

        class FakeProc:
            stdout = f"{sha}\trefs/heads/main\n".encode()

        def fake_run(cmd: list[str], **_kwargs: object) -> FakeProc:
            recorded.append(cmd)
            return FakeProc()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://x", "main", "")
        assert _resolve_sha(req, require_pin=False) == sha
        assert recorded[0][:2] == ["git", "ls-remote"]

    def test_floating_no_match_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class EmptyProc:
            stdout = b""

        def fake_run(cmd: list[str], **_kwargs: object) -> EmptyProc:
            return EmptyProc()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://x", "missing", "")
        with pytest.raises(VcsCloneError, match="no ref"):
            _resolve_sha(req, require_pin=False)

    def test_floating_invalid_sha_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class WeirdProc:
            stdout = b"garbage\trefs/heads/main\n"

        def fake_run(cmd: list[str], **_kwargs: object) -> WeirdProc:
            return WeirdProc()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://x", "main", "")
        with pytest.raises(VcsCloneError, match="unexpected"):
            _resolve_sha(req, require_pin=False)

    def test_ls_remote_failure_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(cmd: list[str], **_kwargs: object) -> object:
            raise FileNotFoundError("git not on PATH")

        monkeypatch.setattr(subprocess, "run", boom)
        req = VcsRequest("git", "https://x", "main", "")
        with pytest.raises(VcsCloneError):
            _resolve_sha(req, require_pin=False)

    def test_unusable_scratch_directory_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A scratch directory nab cannot create surfaces as a clone error.

        The temporary root is a regular file, so ``mkdtemp`` fails with a
        ``NotADirectoryError`` rather than the ``FileNotFoundError`` a
        missing ``git`` binary raises.
        """
        not_a_directory = tmp_path / "tmp"
        not_a_directory.write_text("")
        monkeypatch.setattr(tempfile, "tempdir", str(not_a_directory))

        req = VcsRequest("git", "https://x", "main", "")
        with pytest.raises(VcsCloneError):
            _resolve_sha(req, require_pin=False)


class TestResolveShaAnnotatedTag:
    """An annotated tag resolves to its commit, not the tag object.

    ``git ls-remote repo <ref>`` returns only the tag-object line for an
    exact ref; the peeled ``refs/tags/<ref>^{}`` commit line appears only
    when the peeled ref is queried too, so ``_resolve_sha`` must ask for
    it to pin the commit a ``git tag -a`` release points at.
    """

    def test_annotated_tag_resolves_to_commit_not_tag_object(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tag_object = "a" * 40
        commit = "b" * 40
        queried: list[str] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            queried.extend(cmd)
            lines = [f"{tag_object}\trefs/tags/v1"]
            if any(arg.endswith("^{}") for arg in cmd):
                lines.append(f"{commit}\trefs/tags/v1^{{}}")
            return type("P", (), {"stdout": ("\n".join(lines) + "\n").encode()})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://x", "v1", "")
        assert _resolve_sha(req, require_pin=False) == commit
        assert "v1^{}" in queried


class TestResolveShaNonUtf8Ref:
    """ls-remote output that is not valid UTF-8 raises a VcsCloneError.

    ``git ls-remote repo <ref>`` advertises every ref whose tail matches
    the pattern at a slash boundary, and ref names are byte strings, so an
    ordinary request can come back with a line naming some other ref.
    """

    def test_non_utf8_ref_name_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sibling = "a" * 40
        wanted = "b" * 40

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            lines = [
                f"{sibling}\trefs/heads/".encode() + b"\xff/main",
                f"{wanted}\trefs/heads/main".encode(),
            ]
            return type("P", (), {"stdout": b"\n".join(lines) + b"\n"})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://x", "main", "")
        with pytest.raises(VcsCloneError, match="not valid UTF-8"):
            _resolve_sha(req, require_pin=False)


class TestStalledRemote:
    """A remote that accepts the connection and then goes quiet must not hang."""

    @pytest.mark.parametrize("repo_url", _STALLED_REPO_URLS)
    def test_ls_remote_gives_up(
        self,
        repo_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(subprocess, "run", _stalled_remote)
        req = VcsRequest("git", repo_url, "main", "")
        with pytest.raises(VcsCloneError, match="ls-remote"):
            _resolve_sha(req, require_pin=False)

    @pytest.mark.parametrize("repo_url", _STALLED_REPO_URLS)
    def test_fetch_gives_up_and_rolls_back(
        self,
        repo_url: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(subprocess, "run", _stalled_remote)
        req = VcsRequest("git", repo_url, "a" * 40, "")
        with pytest.raises(VcsCloneError, match="failed to clone"):
            prepare_clone(tmp_path, req, require_pin=True)

        repo_dir = next((tmp_path / "vcs").iterdir())
        assert list(repo_dir.iterdir()) == []

    def test_fetch_bound_outlasts_a_quiet_pack_build(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A big repo sends nothing but keepalives while the server packs it."""
        bounds: dict[str, float] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            timeout = kwargs.get("timeout")
            if isinstance(timeout, (int, float)):
                bounds[cmd[1]] = float(timeout)
            if cmd[1] == "init":
                (Path(str(kwargs["cwd"])) / ".git").mkdir(exist_ok=True)
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example.invalid/x/y.git", "a" * 40, "")
        prepare_clone(tmp_path, req, require_pin=True)

        assert bounds["fetch"] >= 15 * 60


class TestPrepareClone:
    def test_idempotent_when_marked_complete(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "c" * 40
        dest = tmp_path / "vcs" / "1234567890abcdef" / sha
        _mark_complete(dest)

        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "1234567890abcdef")

        def boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("subprocess.run should not be called")

        monkeypatch.setattr(subprocess, "run", boom)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        clone = prepare_clone(tmp_path, req, require_pin=True)
        assert clone.path == dest
        assert clone.commit_sha == sha
        assert clone.subdirectory == ""

    def test_partial_clone_wiped_and_retried(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "d" * 40
        repo_key = "fedcba9876543210"
        dest = tmp_path / "vcs" / repo_key / sha
        # Pre-populate dest WITHOUT a ``.git`` folder so the helper
        # detects a partial clone and recreates it.
        (dest / "garbage.txt").parent.mkdir(parents=True)
        (dest / "garbage.txt").write_text("stale")

        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: repo_key)

        run_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            run_calls.append(cmd)
            if cmd[:3] == ["git", "init", "--quiet"]:
                # Simulate a populated .git directory after init
                (cmd_cwd := _kwargs.get("cwd"))  # type: ignore[func-returns-value]
                if cmd_cwd is not None:
                    (Path(str(cmd_cwd)) / ".git").mkdir(exist_ok=True)
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        clone = prepare_clone(tmp_path, req, require_pin=True)
        assert clone.commit_sha == sha
        assert (clone.path / ".git").is_dir()
        assert not (clone.path / "garbage.txt").exists()
        # Three commands: init, fetch, checkout
        assert len(run_calls) == 3

    def test_failure_rolls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "e" * 40

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            if cmd[:2] == ["git", "fetch"]:
                raise subprocess.CalledProcessError(128, cmd)
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        with pytest.raises(VcsCloneError, match="failed to clone"):
            prepare_clone(tmp_path, req, require_pin=True)
        # Cache directory should not contain a partial clone
        cache = tmp_path / "vcs"
        if cache.exists():
            for entry in cache.rglob("*"):
                assert (
                    not entry.is_dir()
                    or entry.name in {"vcs"}
                    or not list(entry.iterdir())
                ), f"partial clone left behind at {entry}"

    def test_non_executable_git_fails_the_clone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A git that is present but not runnable fails the clone, not the process."""

        def boom(_cmd: list[str], **_kwargs: object) -> object:
            raise PermissionError(errno.EACCES, "Permission denied", "git")

        monkeypatch.setattr(subprocess, "run", boom)
        req = VcsRequest("git", "https://example/repo.git", "a" * 40, "")

        with pytest.raises(VcsCloneError, match="Permission denied"):
            prepare_clone(tmp_path, req, require_pin=True)

    def test_interrupted_fetch_leaves_no_temp_clone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "f" * 40

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir()
            if cmd[:2] == ["git", "fetch"]:
                (cwd / ".git" / "partial").touch()
                raise KeyboardInterrupt
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        with pytest.raises(KeyboardInterrupt):
            prepare_clone(tmp_path, req, require_pin=True)

        assert list((tmp_path / "vcs").rglob("*.tmp")) == []

    def test_subdirectory_propagates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "f" * 40
        dest = tmp_path / "vcs" / "k" / sha
        _mark_complete(dest)
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")
        req = VcsRequest("git", "https://example/r.git", sha, "subpkg")
        clone = prepare_clone(tmp_path, req, require_pin=True)
        assert clone.subdirectory == "subpkg"

    def test_init_only_tree_from_interrupted_fetch_is_recloned(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tree whose fetch never finished is discarded and recloned.

        ``git init`` creates ``.git`` before the network fetch runs, so a
        run killed mid-fetch leaves a tree containing only ``.git``.
        """
        sha = "d" * 40
        repo_key = "aaaa0000bbbb1111"
        dest = tmp_path / "vcs" / repo_key / sha
        (dest / ".git").mkdir(parents=True)
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: repo_key)

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            if cmd[:2] == ["git", "checkout"]:
                (cwd / "pyproject.toml").write_text("[project]\n")
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        clone = prepare_clone(tmp_path, req, require_pin=True)
        assert clone.path == dest
        assert (dest / "pyproject.toml").is_file()

    def test_incomplete_clone_never_visible_at_cache_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mid-fetch state must not appear at the shared cache path."""
        sha = "e" * 40
        repo_key = "cccc2222dddd3333"
        dest = tmp_path / "vcs" / repo_key / sha
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: repo_key)
        dest_seen_during_fetch: list[bool] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            if cmd[:2] == ["git", "fetch"]:
                dest_seen_during_fetch.append(dest.exists())
            if cmd[:2] == ["git", "checkout"]:
                (cwd / "pyproject.toml").write_text("[project]\n")
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        clone = prepare_clone(tmp_path, req, require_pin=True)
        assert dest_seen_during_fetch == [False]
        assert clone.path == dest
        assert (dest / "pyproject.toml").is_file()

    def test_lost_publish_race_returns_winner_clone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When a concurrent run finishes first, its clone is used as-is."""
        sha = "f" * 40
        repo_key = "eeee4444ffff5555"
        dest = tmp_path / "vcs" / repo_key / sha
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: repo_key)

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            if cmd[:2] == ["git", "fetch"]:
                _mark_complete(dest)
                (dest / "pyproject.toml").write_text("winner")
            if cmd[:2] == ["git", "checkout"]:
                (cwd / "pyproject.toml").write_text("loser")
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        clone = prepare_clone(tmp_path, req, require_pin=True)
        assert clone.path == dest
        assert (dest / "pyproject.toml").read_text() == "winner"
        assert [p for p in dest.parent.iterdir() if p != dest] == []

    def test_unmarked_tree_appearing_during_clone_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A foreign unmarked tree blocking the cache path fails loudly."""
        sha = "a" * 40
        repo_key = "9999888877776666"
        dest = tmp_path / "vcs" / repo_key / sha
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: repo_key)

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            if cmd[:2] == ["git", "fetch"]:
                (dest / ".git").mkdir(parents=True)
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        with pytest.raises(VcsCloneError, match="could not be moved into place"):
            prepare_clone(tmp_path, req, require_pin=True)
        assert [p for p in dest.parent.iterdir() if p != dest] == []

    def test_clone_completing_after_top_check_is_not_wiped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A clone completed by another run after the top check is reused, not wiped.

        In the window between the top completion check and the pre-clone wipe, a
        concurrent run renames its finished clone into place. The wipe re-checks
        the marker so the completed tree survives.
        """
        sha = "b" * 40
        repo_key = "0123456789abcdef"
        dest = tmp_path / "vcs" / repo_key / sha
        _mark_complete(dest)
        payload = dest / "content.txt"
        payload.write_text("kept")
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: repo_key)

        checks = {"count": 0}

        def racy_clone_complete(target: Path) -> bool:
            checks["count"] += 1
            if checks["count"] == 1:
                return False
            return _clone_complete(target)

        monkeypatch.setattr("nab_index.vcs._clone_complete", racy_clone_complete)

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        clone = prepare_clone(tmp_path, req, require_pin=True)
        assert clone.path == dest
        assert payload.read_text() == "kept"


def _deny_below(monkeypatch: pytest.MonkeyPatch, ancestor: Path) -> None:
    """Refuse every stat and mkdir below ``ancestor``, as a missing search bit does.

    Both calls need denying: ``Path.is_file`` re-raises the stat's EACCES up to
    Python 3.13 but reports it as absent from 3.14, which leaves the mkdir as
    the first refusal a newer interpreter reaches.
    """
    original_stat, original_mkdir = Path.stat, Path.mkdir

    def refuse(path: Path) -> None:
        if ancestor in path.parents:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))

    def denying_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        refuse(self)
        return original_stat(self, *args, **kwargs)

    def denying_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        refuse(self)
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denying_stat)
    monkeypatch.setattr(Path, "mkdir", denying_mkdir)


class TestUnusableCacheEntry:
    """Reading or creating a clone's cache entry fails as VcsCloneError.

    The refusals are faked because chmod cannot produce them: the superuser
    ignores the mode bits, and Windows has none.
    """

    def test_unwritable_cache_root_reports_the_os_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A read-only cache root refuses the first directory nab makes under it."""
        vcs_dir = tmp_path / "vcs"
        original_mkdir = Path.mkdir

        def failing_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
            if self == vcs_dir:
                raise PermissionError(errno.EACCES, "Permission denied", str(self))
            original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)
        req = VcsRequest("git", "https://example/repo.git", "a" * 40, "")

        with pytest.raises(VcsCloneError, match="is unusable") as excinfo:
            prepare_clone(tmp_path, req, require_pin=True)

        assert "Permission denied" in str(excinfo.value)

    def test_unsearchable_cache_ancestor_reports_the_os_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cache nab cannot search into is reported rather than raised through.

        The cache directory was made by another user, or sits under a home
        directory NFS squashes.
        """
        cache_root = tmp_path / "unsearchable" / "cache"
        (cache_root / "vcs").mkdir(parents=True)
        _deny_below(monkeypatch, tmp_path / "unsearchable")
        req = VcsRequest("git", "https://example/repo.git", "a" * 40, "")

        with pytest.raises(VcsCloneError, match="is unusable") as excinfo:
            prepare_clone(cache_root, req, require_pin=True)

        assert "Permission denied" in str(excinfo.value)

    def test_full_cache_filesystem_reports_the_os_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def failing_mkdtemp(**_kwargs: object) -> str:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(tempfile, "mkdtemp", failing_mkdtemp)
        req = VcsRequest("git", "https://example/repo.git", "b" * 40, "")

        with pytest.raises(VcsCloneError, match="is unusable") as excinfo:
            prepare_clone(tmp_path, req, require_pin=True)

        assert "No space left on device" in str(excinfo.value)

    def test_unremovable_partial_clone_reports_the_os_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The pre-clone wipe's own refusal is reported, and nothing clones over it."""
        sha = "c" * 40
        repo_key = "5555666677778888"
        (tmp_path / "vcs" / repo_key / sha / "objects").mkdir(parents=True)
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: repo_key)

        def failing_rmtree(
            path: object, *, ignore_errors: bool = False, **_kwargs: object
        ) -> None:
            """Refuse the wipe, but honour ignore_errors so a silenced wipe shows."""
            if ignore_errors:
                return
            raise PermissionError(errno.EACCES, "Permission denied", str(path))

        monkeypatch.setattr(shutil, "rmtree", failing_rmtree)

        def boom(*_args: object, **_kwargs: object) -> object:
            msg = "an unwiped cache path must not be cloned over"
            raise AssertionError(msg)

        monkeypatch.setattr(subprocess, "run", boom)
        req = VcsRequest("git", "https://example/repo.git", sha, "")

        with pytest.raises(VcsCloneError, match="is unusable") as excinfo:
            prepare_clone(tmp_path, req, require_pin=True)

        assert "Permission denied" in str(excinfo.value)


_EXPECTED_SCRUBBED_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


class TestCacheLayout:
    """The clone cache key, and the git commands that fill an entry."""

    def _clone(
        self,
        cache_root: Path,
        requirement_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[Path, list[list[str]]]:
        """Clone a ``git+`` URL with git stubbed; return the tree and the argv."""
        argv: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            argv.append(cmd)
            if cmd[:2] == ["git", "init"]:
                (Path(str(kwargs["cwd"])) / ".git").mkdir(exist_ok=True)
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        clone = prepare_clone(
            cache_root,
            VcsRequest.parse(requirement_url),
            require_pin=True,
        )
        return clone.path, argv

    def test_repo_key_covers_the_repo_url_alone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ``git+`` prefix, the ``@<sha>`` ref and the fragment are not keyed."""
        sha = "a" * 40
        path, _ = self._clone(
            tmp_path,
            f"git+https://example.com/repo.git@{sha}#subdirectory=pkg",
            monkeypatch,
        )

        # 3f71ca0a9a455fa9 is sha256("https://example.com/repo.git")[:16].
        assert path == tmp_path / "vcs" / "3f71ca0a9a455fa9" / sha

    def test_spellings_of_one_repo_do_not_share_an_entry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One repo written three ways gets three entries: nothing canonicalises."""
        sha = "b" * 40
        spellings = (
            "https://example.com/repo.git",
            "https://example.com/repo",
            "https://example.com/repo.git/",
        )
        trees = [
            self._clone(tmp_path, f"git+{url}@{sha}", monkeypatch)[0]
            for url in spellings
        ]

        assert len({tree.parent for tree in trees}) == len(spellings)

    def test_entry_is_filled_by_init_fetch_checkout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "c" * 40
        url = "https://example.com/repo.git"
        _, argv = self._clone(tmp_path, f"git+{url}@{sha}", monkeypatch)

        assert argv == [
            ["git", "init", "--quiet"],
            ["git", "fetch", "--quiet", "--depth", "1", url, sha],
            ["git", "checkout", "--quiet", "FETCH_HEAD"],
        ]


class TestAmbientGitEnvironment:
    """git's repo-selection variables must not reach nab's git calls."""

    def _set_ambient_vars(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        """Seed the environment a git call would inherit from the caller."""
        for name in _REPO_SELECTION_VARS:
            monkeypatch.setenv(name, str(home / name.lower()))
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /keys/id_ed25519")

        # nab only defaults these, so an ambient value would hide the default.
        monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
        monkeypatch.delenv("GIT_CONFIG_SYSTEM", raising=False)

    def _run_ls_remote(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sha: str,
    ) -> list[dict[str, str]]:
        """Run _resolve_sha against a fake git and return each call's GIT_ vars."""
        seen: list[dict[str, str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            env = kwargs["env"]
            assert isinstance(env, dict)
            seen.append({k: v for k, v in env.items() if k.startswith("GIT_")})
            return type("P", (), {"stdout": f"{sha}\trefs/heads/main\n".encode()})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", "main", "")
        assert _resolve_sha(req, require_pin=False) == sha
        return seen

    def _assert_scrubbed(self, env: dict[str, str]) -> None:
        """Assert one env dropped the repo-selection vars and kept nab's own."""
        assert [name for name in _REPO_SELECTION_VARS if name in env] == []

        # The scrub stays narrow: git+ssh reaches credentials through this one.
        assert env["GIT_SSH_COMMAND"] == "ssh -i /keys/id_ed25519"

        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"

    def test_scrubbed_vars_are_pinned(self) -> None:
        """The other tests read the production tuple, so nothing else sees it change."""
        assert _REPO_SELECTION_VARS == _EXPECTED_SCRUBBED_VARS

    def test_clone_drops_inherited_repo_selection_vars(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._set_ambient_vars(monkeypatch, tmp_path / "elsewhere")
        sha = "a" * 40
        seen: list[dict[str, str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            env = kwargs["env"]
            assert isinstance(env, dict)
            seen.append({k: v for k, v in env.items() if k.startswith("GIT_")})
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        prepare_clone(tmp_path, req, require_pin=True)

        assert len(seen) == 3
        for env in seen:
            self._assert_scrubbed(env)

    def test_ls_remote_drops_inherited_repo_selection_vars(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._set_ambient_vars(monkeypatch, tmp_path / "elsewhere")
        seen = self._run_ls_remote(monkeypatch, "b" * 40)

        assert len(seen) == 1
        self._assert_scrubbed(seen[0])

    def test_ls_remote_keeps_ambient_git_config_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nab defaults the config paths, so a caller's own values survive."""
        self._set_ambient_vars(monkeypatch, tmp_path / "elsewhere")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/some/path")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/other/path")

        seen = self._run_ls_remote(monkeypatch, "d" * 40)

        assert seen[0]["GIT_CONFIG_GLOBAL"] == "/some/path"
        assert seen[0]["GIT_CONFIG_SYSTEM"] == "/other/path"

    def test_marker_write_failure_does_not_blame_the_remote(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A marker write that fails is local state, not an unreachable remote."""
        sha = "c" * 40

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "https://example/repo.git", sha, "")
        with pytest.raises(VcsCloneError) as caught:
            prepare_clone(tmp_path, req, require_pin=True)

        message = str(caught.value)
        assert "could not be marked complete" in message
        assert "failed to clone" not in message
        assert list((tmp_path / "vcs").rglob("*.tmp")) == []


class TestAmbientGitRepository:
    """A checkout nab is invoked from must not configure nab's git calls."""

    def _run_git(self, args: list[str], cwd: Path) -> str:
        """Run git with ``args`` in ``cwd`` and return its stripped stdout."""
        git = shutil.which("git")
        assert git is not None
        proc = subprocess.run(  # noqa: S603 - git is a runtime dep
            [git, *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()

    def _commit_repo(self, path: Path, content: str) -> str:
        """Create a one-commit repo on ``main`` at ``path``, returning its SHA."""
        path.mkdir()
        self._run_git(["init", "--quiet", "-b", "main"], path)
        (path / "f.txt").write_text(content)
        self._run_git(["add", "f.txt"], path)
        self._run_git(
            [
                "-c",
                "user.name=nab tests",
                "-c",
                "user.email=tests@example.invalid",
                "commit",
                "--quiet",
                "--no-gpg-sign",
                "-m",
                content,
            ],
            path,
        )
        return self._run_git(["rev-parse", "HEAD"], path)

    def _rewriting_checkout(self, tmp_path: Path) -> tuple[Path, str, str]:
        """Build an origin, a diverged mirror, and a checkout that swaps them.

        The checkout's local config rewrites the origin's URL to the
        mirror's, so any git command that reads that config answers from
        the wrong repository.  Returns the checkout, the origin's URL,
        and the SHA the origin's ``main`` names.
        """
        origin_sha = self._commit_repo(tmp_path / "origin", "origin")
        mirror_sha = self._commit_repo(tmp_path / "mirror", "mirror")
        assert origin_sha != mirror_sha

        origin_url = (tmp_path / "origin").as_uri()
        mirror_url = (tmp_path / "mirror").as_uri()

        work = tmp_path / "work"
        work.mkdir()
        self._run_git(["init", "--quiet"], work)
        self._run_git(
            ["config", "--local", f"url.{mirror_url}.insteadOf", origin_url],
            work,
        )
        return work, origin_url, origin_sha

    def test_ls_remote_ignores_a_rewrite_in_the_invoking_checkout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The rewrite in the current directory's checkout does not apply."""
        work, origin_url, origin_sha = self._rewriting_checkout(tmp_path)
        monkeypatch.chdir(work)

        req = VcsRequest("git", origin_url, "main", "")

        assert _resolve_sha(req, require_pin=False) == origin_sha

    def test_ls_remote_ignores_a_rewrite_around_the_temporary_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A scratch directory inside the checkout does not expose the rewrite."""
        work, origin_url, origin_sha = self._rewriting_checkout(tmp_path)
        scratch_root = work / "tmp"
        scratch_root.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch_root))
        monkeypatch.chdir(work)

        req = VcsRequest("git", origin_url, "main", "")

        assert _resolve_sha(req, require_pin=False) == origin_sha


class TestOfflineClone:
    """``offline`` withholds every git call that would reach the remote."""

    def test_cold_cache_fails_without_touching_the_remote(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(subprocess, "run", _refuse_git)
        req = VcsRequest("git", "https://example.com/repo.git", "a" * 40, "")
        with pytest.raises(VcsCloneError, match="offline"):
            prepare_clone(tmp_path, req, require_pin=True, offline=True)

    def test_warm_cache_is_served(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "b" * 40
        dest = tmp_path / "vcs" / "k" / sha
        _mark_complete(dest)
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")
        monkeypatch.setattr(subprocess, "run", _refuse_git)
        req = VcsRequest("git", "https://example.com/repo.git", sha, "")
        clone = prepare_clone(tmp_path, req, require_pin=True, offline=True)
        assert clone.path == dest
        assert clone.commit_sha == sha

    def test_floating_ref_is_not_resolved(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A warm clone cache does not allow an ls-remote for a branch."""
        sha = "c" * 40
        _mark_complete(tmp_path / "vcs" / "k" / sha)
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")
        monkeypatch.setattr(subprocess, "run", _refuse_git)
        req = VcsRequest("git", "https://example.com/repo.git", "main", "")
        with pytest.raises(VcsCloneError, match="offline"):
            prepare_clone(tmp_path, req, require_pin=False, offline=True)

    def test_file_url_repo_still_clones(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``file://`` repo is read by path, like a ``file:`` archive URL."""
        sha = "d" * 40
        commands: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            commands.append(cmd[1])
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", (tmp_path / "repo").as_uri(), sha, "")
        clone = prepare_clone(tmp_path, req, require_pin=True, offline=True)
        assert clone.commit_sha == sha
        assert commands == ["init", "fetch", "checkout"]

    def test_file_url_with_a_host_still_clones(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An authority does not make a ``file://`` repo a network fetch.

        git drops it on POSIX and reads a UNC path on Windows, both
        filesystem calls.  Offline gates the requests nab issues, not
        what the mount behind a path happens to do.
        """
        sha = "f" * 40
        commands: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            commands.append(cmd[1])
            cwd = Path(str(kwargs["cwd"]))
            if cmd[:2] == ["git", "init"]:
                (cwd / ".git").mkdir(exist_ok=True)
            return type("P", (), {"returncode": 0})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", "file://server/share/repo.git", sha, "")
        clone = prepare_clone(tmp_path, req, require_pin=True, offline=True)
        assert clone.commit_sha == sha
        assert commands == ["init", "fetch", "checkout"]

    def test_file_url_floating_ref_still_resolves(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "e" * 40
        _mark_complete(tmp_path / "vcs" / "k" / sha)
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            assert cmd[:2] == ["git", "ls-remote"]
            return type("P", (), {"stdout": f"{sha}\trefs/heads/main\n".encode()})()

        monkeypatch.setattr(subprocess, "run", fake_run)
        req = VcsRequest("git", (tmp_path / "repo").as_uri(), "main", "")
        clone = prepare_clone(tmp_path, req, require_pin=False, offline=True)
        assert clone.commit_sha == sha


class TestProviderVcsIntegration:
    def coordinator(self) -> FakeFetchPort:
        return make_coordinator()

    def test_declared_source_is_not_cloned_offline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Offline gates a declared VCS source, as it already gates archives.

        Uses a real coordinator so the run's own ``offline`` setting
        drives the check rather than a mock attribute.
        """
        monkeypatch.setattr(subprocess, "run", _refuse_git)
        sha = "a" * 40

        provider = Provider(
            FetchCoordinator(transport=HttpxAsyncTransport(), offline=True),
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
                require_pin=True,
            ),
            vcs_sources=[
                VcsSource(name="foo", url=f"git+https://example.com/foo.git@{sha}"),
            ],
            vcs_cache_dir=tmp_path / "cache",
            build_policy=BuildPolicy.NEVER,
        )
        with pytest.raises(UnsupportedSdistError, match="offline"):
            provider.fetch_versions("foo")

    def test_pinned_clone_resolves(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: provider materialises a VCS source via the cache."""
        sha = "a" * 40
        clone_dir = tmp_path / "cache" / "vcs" / "k" / sha
        _mark_complete(clone_dir)
        (clone_dir / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")

        provider = Provider(
            self.coordinator(),
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
                require_pin=True,
            ),
            vcs_sources=[
                VcsSource(name="foo", url=f"git+https://example.com/foo.git@{sha}"),
            ],
            vcs_cache_dir=tmp_path / "cache",
            build_policy=BuildPolicy.NEVER,
        )
        versions = provider.fetch_versions("foo")
        assert len(versions) == 1
        assert str(versions[0][0]) == "1.0.0"
        # Resolved commit SHA is recorded so the lockfile builder can
        # emit a SHA-pinned VcsPin rather than the raw ``@<ref>`` token.
        assert provider.vcs_pin_for("foo") == sha
        # Unknown package returns None.
        assert provider.vcs_pin_for("missing") is None

    def test_relative_cache_dir_yields_an_absolute_clone_uri(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cwd-relative cache dir still yields an absolute ``file:`` URI.

        ``cache-dir`` is cwd-relative, so the provider can be handed a
        relative clone root.
        """
        sha = "a" * 40
        monkeypatch.chdir(tmp_path)
        clone_dir = tmp_path / "cache" / "vcs" / "k" / sha
        _mark_complete(clone_dir)
        (clone_dir / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")

        provider = Provider(
            self.coordinator(),
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
                require_pin=True,
            ),
            vcs_sources=[
                VcsSource(name="foo", url=f"git+https://example.com/foo.git@{sha}"),
            ],
            vcs_cache_dir=Path("cache"),
            build_policy=BuildPolicy.NEVER,
        )
        versions = provider.fetch_versions("foo")
        assert len(versions) == 1
        assert versions[0][1].url == clone_dir.resolve().as_uri()

    def test_floating_ref_lock_records_resolved_sha(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A branch ref under require_pin=False locks the resolved SHA.

        End-to-end seam: the resolver materialises the clone (recording
        the ls-remote SHA), then the lock builder reads it back.  PEP 751
        wants commit_id to be the immutable SHA; the branch name lands in
        requested_revision, never in commit_id.  Guards the path that
        could otherwise emit a branch name as the commit id.
        """
        sha = "b" * 40
        clone_dir = tmp_path / "cache" / "vcs" / "k" / sha
        _mark_complete(clone_dir)
        (clone_dir / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            assert cmd[:2] == ["git", "ls-remote"]
            return type("P", (), {"stdout": f"{sha}\trefs/heads/main\n".encode()})()

        monkeypatch.setattr(subprocess, "run", fake_run)

        provider = Provider(
            self.coordinator(),
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
                require_pin=False,
            ),
            vcs_sources=[
                VcsSource(name="foo", url="git+https://example.com/foo.git@main"),
            ],
            vcs_cache_dir=tmp_path / "cache",
            build_policy=BuildPolicy.NEVER,
        )
        provider.fetch_versions("foo")
        lock = build_target_lock(
            provider, ResolveTarget.for_host(), {"foo": Version("1.0.0")}
        )
        pin = lock.pins["foo"]
        assert isinstance(pin, VcsPin)
        assert pin.commit_id == sha
        assert pin.requested_revision == "main"

    def test_name_mismatch_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cloned repo whose [project].name differs from the source name aborts."""
        sha = "a" * 40
        clone_dir = tmp_path / "cache" / "vcs" / "k" / sha
        _mark_complete(clone_dir)
        (clone_dir / "pyproject.toml").write_text(
            '[project]\nname = "bar"\nversion = "9.9.9"\n'
            'dependencies = ["unrelated-dep==6.6.6"]\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")

        provider = Provider(
            self.coordinator(),
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
                require_pin=True,
            ),
            vcs_sources=[
                VcsSource(name="foo", url=f"git+https://example.com/foo.git@{sha}"),
            ],
            vcs_cache_dir=tmp_path / "cache",
            build_policy=BuildPolicy.NEVER,
        )
        with pytest.raises(SourceNameMismatchError, match="bar") as excinfo:
            provider.fetch_versions("foo")
        assert "foo" in str(excinfo.value)

    def test_vcs_under_block_policy_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="VcsPolicy.ALLOW"):
            Provider(
                self.coordinator(),
                vcs_sources=[
                    VcsSource(name="foo", url="git+https://example.com/foo.git@abc"),
                ],
                vcs_cache_dir=tmp_path / "cache",
                build_policy=BuildPolicy.NEVER,
            )

    def test_vcs_without_cache_dir_raises(self, tmp_path: Path) -> None:
        provider = Provider(
            self.coordinator(),
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
                require_pin=True,
            ),
            vcs_sources=[
                VcsSource(
                    name="foo",
                    url="git+https://example.com/foo.git@" + "a" * 40,
                ),
            ],
            build_policy=BuildPolicy.NEVER,
        )
        with pytest.raises(UnsupportedSdistError, match="no.*vcs_cache_dir"):
            provider.fetch_versions("foo")

    def test_clone_error_surfaces_as_unsupported_sdist(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A VcsCloneError at clone time becomes UnsupportedSdistError."""

        def boom(*_args: object, **_kwargs: object) -> None:
            raise VcsCloneError("simulated clone failure")

        monkeypatch.setattr("nab_index.vcs.prepare_clone", boom)
        provider = Provider(
            self.coordinator(),
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
                require_pin=True,
            ),
            vcs_sources=[
                VcsSource(
                    name="foo",
                    url="git+https://example.com/foo.git@" + "a" * 40,
                ),
            ],
            vcs_cache_dir=tmp_path / "cache",
            build_policy=BuildPolicy.NEVER,
        )
        with pytest.raises(UnsupportedSdistError, match="simulated"):
            provider.fetch_versions("foo")

    def test_vcs_dynamic_deps_under_build_local_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A VCS clone with dynamic deps under BUILD_LOCAL is rejected.

        Building VCS clones requires BUILD_REMOTE; BUILD_LOCAL only
        covers user-declared local checkouts.
        """
        sha = "a" * 40
        clone_dir = tmp_path / "cache" / "vcs" / "k" / sha
        _mark_complete(clone_dir)
        (clone_dir / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.0"\ndynamic = ["dependencies"]\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")

        provider = Provider(
            self.coordinator(),
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
                require_pin=True,
            ),
            vcs_sources=[
                VcsSource(name="foo", url=f"git+https://example.com/foo.git@{sha}"),
            ],
            vcs_cache_dir=tmp_path / "cache",
            build_policy=BuildPolicy.BUILD_LOCAL,
        )
        with pytest.raises(UnsupportedSdistError, match="build-policy 'build-remote'"):
            provider.fetch_versions("foo")

    def test_warm_clone_build_refused_under_an_offline_coordinator(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A warm clone needs no fetch, so the refusal comes from its build env."""
        sha = "a" * 40
        clone_dir = tmp_path / "cache" / "vcs" / "k" / sha
        _mark_complete(clone_dir)
        (clone_dir / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.0"\ndynamic = ["dependencies"]\n'
            '\n[build-system]\nrequires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("offline must not reach the network")

        monkeypatch.setattr("nab_project.resolve.resolve_for_targets", _boom)

        provider = Provider(
            make_coordinator(build_config=ResolveInputs(), offline=True),
            vcs_config=VcsConfig(
                policy=VcsPolicy.ALLOW,
                allowed_schemes=frozenset({"git+https"}),
                allowed_repos=("https://example.com/",),
                require_pin=True,
            ),
            vcs_sources=[
                VcsSource(name="foo", url=f"git+https://example.com/foo.git@{sha}"),
            ],
            vcs_cache_dir=tmp_path / "cache",
            build_policy=BuildPolicy.BUILD_REMOTE,
        )
        with pytest.raises(
            UnsupportedSdistError,
            match="build requirements unavailable in offline mode: hatchling",
        ):
            provider.fetch_versions("foo")

    def test_dynamic_backend_mutation_does_not_poison_cached_clone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "a" * 40
        clone_dir = tmp_path / "cache" / "vcs" / "k" / sha
        _mark_complete(clone_dir)
        project = clone_dir / "pkg"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            '[project]\nname = "foo"\nversion = "1.0.0"\ndynamic = ["dependencies"]\n',
            encoding="utf-8",
        )
        dependency = project / "dependency.txt"
        dependency.write_text("dep-one==1", encoding="utf-8")
        (clone_dir / "shared.txt").write_text("parent context", encoding="utf-8")

        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")
        monkeypatch.setattr(subprocess, "run", _refuse_git)

        def mutating_backend(path: Path, **_kwargs: object) -> WheelMetadata:
            assert path.parent.name == clone_dir.name
            assert (path.parent / "shared.txt").read_text(encoding="utf-8") == (
                "parent context"
            )
            assert (path.parent / ".git" / _COMPLETE_MARKER).is_file()
            backend_input = path / "dependency.txt"
            requires_dist = [Requirement(backend_input.read_text(encoding="utf-8"))]
            backend_input.write_text("dep-two==2", encoding="utf-8")
            return WheelMetadata(
                name="foo",
                version=Version("1.0.0"),
                requires_python=None,
                requires_dist=requires_dist,
                provides_extra=[],
            )

        monkeypatch.setattr(
            "nab_project.build_backend.extract_metadata", mutating_backend
        )

        def make_provider() -> Provider:
            return Provider(
                self.coordinator(),
                vcs_config=VcsConfig(
                    policy=VcsPolicy.ALLOW,
                    allowed_schemes=frozenset({"git+https"}),
                    allowed_repos=("https://example.com/",),
                    require_pin=True,
                ),
                vcs_sources=[
                    VcsSource(
                        name="foo",
                        url=(f"git+https://example.com/foo.git@{sha}#subdirectory=pkg"),
                    )
                ],
                vcs_cache_dir=tmp_path / "cache",
                build_policy=BuildPolicy.BUILD_REMOTE,
            )

        first = make_provider()
        first.fetch_versions("foo")
        second = make_provider()
        second.fetch_versions("foo")

        metadata_key = ("foo", Version("1.0.0"))
        assert [
            str(first.metadata_cache[metadata_key].requires_dist[0]),
            str(second.metadata_cache[metadata_key].requires_dist[0]),
        ] == ["dep-one==1", "dep-one==1"]
        assert dependency.read_text(encoding="utf-8") == "dep-one==1"

    def test_duplicate_source_across_local_vcs_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="duplicate source"):
            Provider(
                self.coordinator(),
                local_sources=[LocalSource("foo", str(tmp_path))],
                vcs_config=VcsConfig(
                    policy=VcsPolicy.ALLOW,
                    allowed_schemes=frozenset({"git+https"}),
                    allowed_repos=("https://example.com/",),
                    require_pin=True,
                ),
                vcs_sources=[
                    VcsSource(
                        name="foo",
                        url="git+https://example.com/foo.git@" + "a" * 40,
                    ),
                ],
                vcs_cache_dir=tmp_path / "cache",
                build_policy=BuildPolicy.NEVER,
            )
