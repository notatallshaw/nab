"""VCS request parsing in the provider, before a repository is cloned."""

from __future__ import annotations

import pytest

from nab_provider.vcs_request import VcsCloneError, VcsRequest


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
        req = VcsRequest.parse(
            "git+https://github.com/x/y.git@v1#subdirectory=src/a%20b+c%26d%2520"
        )
        assert req.subdirectory == "src/a b+c&d%20"
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

    @pytest.mark.parametrize(
        "subdirectory", ["../source-root/demo", r"..\source-root\demo"]
    )
    def test_source_root_named_parent_escape_rejected(self, subdirectory: str) -> None:
        with pytest.raises(VcsCloneError, match="unsafe VCS subdirectory"):
            VcsRequest.parse(
                "git+https://ex.com/r.git@" + "a" * 40 + f"#subdirectory={subdirectory}"
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


@pytest.mark.parametrize(
    ("url", "repo", "ref"),
    [
        ("https://example/repo.git@v1", "https://example/repo.git", "v1"),
        ("https://example/repo.git", "https://example/repo.git", ""),
        ("https://user@example.com/x/y.git", "https://user@example.com/x/y.git", ""),
        (
            "https://user@example.com/x/y.git@v1",
            "https://user@example.com/x/y.git",
            "v1",
        ),
        (
            "https://example/repo.git@release/1.0",
            "https://example/repo.git",
            "release/1.0",
        ),
        ("git@github.com:x/y.git", "git@github.com:x/y.git", ""),
        ("git@github.com:x/y.git@v1.0", "git@github.com:x/y.git", "v1.0"),
        ("https://example.com", "https://example.com", ""),
        ("myrepo", "myrepo", ""),
    ],
)
def test_repository_and_ref(url: str, repo: str, ref: str) -> None:
    request = VcsRequest.parse("git+" + url)
    assert request.repo_url == repo
    assert request.ref == ref


@pytest.mark.parametrize("fragment", ["not%fftext", "nul%00byte"])
def test_invalid_subdirectory_name(fragment: str) -> None:
    with pytest.raises(VcsCloneError, match="subdirectory"):
        VcsRequest.parse(f"git+https://ex.com/demo.git#subdirectory={fragment}")
