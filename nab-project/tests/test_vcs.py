"""Resolve, lock and build projects with Git sources."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.vcs import (
    _COMPLETE_MARKER,
    VcsCloneError,
)
from nab_project.fetch import FetchCoordinator
from nab_project.inputs import ResolveInputs
from nab_project.lockfile import VcsPin, build_target_lock
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
from nab_provider.testing import FakeFetchPort
from nab_test_support.coordinator import make_coordinator


def _mark_complete(clone_dir: Path) -> None:
    """Fabricate a finished cached clone the way ``prepare_clone`` leaves one."""
    (clone_dir / ".git").mkdir(parents=True, exist_ok=True)
    (clone_dir / ".git" / _COMPLETE_MARKER).touch()


def _refuse_git(*_args: object, **_kwargs: object) -> object:
    """Stand in for git on a path that must not shell out at all."""
    msg = "offline mode must not invoke git"
    raise AssertionError(msg)


class TestProviderVcsIntegration:
    def coordinator(self) -> FakeFetchPort:
        return make_coordinator()

    def test_declared_source_is_not_cloned_offline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Offline blocks a declared VCS source and remote archives.

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
