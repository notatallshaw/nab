"""Tests for nab_index.vcs URL parsing + clone helpers.

The clone path itself shells out to ``git`` and is not unit-tested
here; integration tests live alongside the runner.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nab_index.client import SdistFile, WheelFile  # noqa: F401 - re-exported by helpers
from nab_index.vcs import (
    VcsCloneError,
    VcsRequest,
    _resolve_sha,
    _split_repo_ref,
    prepare_clone,
)
from nab_python.fetch import InMemoryIndex
from nab_python.provider import (
    BuildPolicy,
    LocalSource,
    Provider,
    UnsupportedSdistError,
    VcsConfig,
    VcsPolicy,
    VcsSource,
)


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

    def test_hg_scheme(self) -> None:
        req = VcsRequest.parse("hg+https://hg.example.com/repo@v1.0")
        assert req.scheme == "hg"
        assert req.ref == "v1.0"


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
        with pytest.raises(VcsCloneError, match="vcs_require_pin"):
            _resolve_sha(req, require_pin=True)

    def test_floating_no_pin_calls_ls_remote(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "1" * 40
        recorded: list[list[str]] = []

        class FakeProc:
            stdout = f"{sha}\trefs/heads/main\n"

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
            stdout = ""

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
            stdout = "garbage\trefs/heads/main\n"

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


class TestPrepareClone:
    def test_idempotent_when_dest_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "c" * 40
        dest = tmp_path / "vcs" / "1234567890abcdef" / sha
        (dest / ".git").mkdir(parents=True)

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
            if "fetch" in cmd:
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

    def test_subdirectory_propagates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sha = "f" * 40
        dest = tmp_path / "vcs" / "k" / sha
        (dest / ".git").mkdir(parents=True)
        monkeypatch.setattr("nab_index.vcs._repo_key", lambda _url: "k")
        req = VcsRequest("git", "https://example/r.git", sha, "subpkg")
        clone = prepare_clone(tmp_path, req, require_pin=True)
        assert clone.subdirectory == "subpkg"


class TestProviderVcsIntegration:
    def coordinator(self) -> MagicMock:
        coordinator = MagicMock()
        coordinator.index = InMemoryIndex()
        return coordinator

    def test_pinned_clone_resolves(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: provider materialises a VCS source via the cache."""
        sha = "a" * 40
        clone_dir = tmp_path / "cache" / "vcs" / "k" / sha
        (clone_dir / ".git").mkdir(parents=True)
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
        (clone_dir / ".git").mkdir(parents=True)
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
        with pytest.raises(UnsupportedSdistError, match="BUILD_REMOTE"):
            provider.fetch_versions("foo")

    def test_duplicate_source_across_local_vcs_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="duplicate source"):
            Provider(
                self.coordinator(),
                local_sources=[LocalSource("foo", str(tmp_path))],
                vcs_config=VcsConfig(
                    policy=VcsPolicy.ALLOW,
                    allowed_schemes=frozenset({"git+https"}),
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
