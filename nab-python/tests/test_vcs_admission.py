"""Tests for VCS direct-URL requirement admission (Layer 1).

Admit-or-refuse VCS URLs up front so silently-dropped requirements
turn into loud, actionable errors.  The actual clone path is Layer 2.
"""

from __future__ import annotations

import pytest

from nab_python._vcs_admission import (
    UnsupportedVcsError,
    VcsConfig,
    VcsPolicy,
    admit_vcs_url,
    has_full_commit_sha,
    split_vcs_scheme,
)

_FORTY = "0123456789abcdef0123456789abcdef01234567"


def _allow_https() -> VcsConfig:
    # An empty allowed-repos denies all, so list a permissive prefix
    # for the scheme/pin tests that are not about repo filtering.
    return VcsConfig(
        policy=VcsPolicy.ALLOW,
        allowed_schemes=frozenset({"git+https"}),
        allowed_repos=("https://",),
    )


class TestSplitVcsScheme:
    def test_strips_git_https(self) -> None:
        scheme, inner = split_vcs_scheme(
            f"git+https://github.com/foo/bar.git@{_FORTY}",
        )
        assert scheme == "git+https"
        assert inner == f"https://github.com/foo/bar.git@{_FORTY}"

    def test_strips_git_ssh(self) -> None:
        scheme, inner = split_vcs_scheme(
            "git+ssh://git@github.com/foo/bar.git",
        )
        assert scheme == "git+ssh"
        assert inner == "ssh://git@github.com/foo/bar.git"

    def test_bare_svn_refused(self) -> None:
        scheme, inner = split_vcs_scheme("svn://example.com/r")
        assert scheme is None
        assert inner == "svn://example.com/r"

    def test_bare_git_refused(self) -> None:
        scheme, inner = split_vcs_scheme("git://example.com/r.git")
        assert scheme is None
        assert inner == "git://example.com/r.git"

    def test_hg_https_refused(self) -> None:
        url = "hg+https://hg.example.com/r"
        scheme, inner = split_vcs_scheme(url)
        assert scheme is None
        assert inner == url

    def test_https_archive_returns_none(self) -> None:
        url = "https://example.com/pkg.whl"
        scheme, inner = split_vcs_scheme(url)
        assert scheme is None
        assert inner == url

    def test_file_path_returns_none(self) -> None:
        url = "file:///tmp/pkg.tar.gz"
        scheme, inner = split_vcs_scheme(url)
        assert scheme is None
        assert inner == url


class TestHasFullCommitSha:
    def test_full_sha_after_at(self) -> None:
        url = f"git+https://github.com/foo/bar.git@{_FORTY}"
        assert has_full_commit_sha(url)

    def test_short_sha_rejected(self) -> None:
        url = "git+https://github.com/foo/bar.git@abc123"
        assert not has_full_commit_sha(url)

    def test_tag_rejected(self) -> None:
        url = "git+https://github.com/foo/bar.git@v1.0"
        assert not has_full_commit_sha(url)

    def test_no_at_rejected(self) -> None:
        url = "git+https://github.com/foo/bar.git"
        assert not has_full_commit_sha(url)

    def test_user_at_host_with_sha(self) -> None:
        url = f"git+ssh://git@github.com/foo/bar.git@{_FORTY}"
        assert has_full_commit_sha(url)

    def test_user_at_host_without_sha(self) -> None:
        url = "git+ssh://git@github.com/foo/bar.git"
        assert not has_full_commit_sha(url)

    def test_creds_in_url_with_sha(self) -> None:
        url = f"git+https://user:pass@github.com/foo/bar.git@{_FORTY}"
        assert has_full_commit_sha(url)

    def test_subdirectory_fragment_ignored(self) -> None:
        url = f"git+https://github.com/foo/bar.git@{_FORTY}#subdirectory=sub"
        assert has_full_commit_sha(url)

    def test_uppercase_sha_accepted(self) -> None:
        # A 40-char hex SHA is case-insensitive; uppercase satisfies require-pin.
        upper = _FORTY.upper()
        url = f"git+https://github.com/foo/bar.git@{upper}"
        assert has_full_commit_sha(url)

    def test_sha_in_authority_without_path_rejected(self) -> None:
        """An ``@<sha>`` in the authority is userinfo, not a ref."""
        url = f"git+https://github.com@{_FORTY}"
        assert not has_full_commit_sha(url)

    def test_sha_as_userinfo_with_unpinned_path_rejected(self) -> None:
        url = f"git+https://{_FORTY}@github.com/foo/bar.git"
        assert not has_full_commit_sha(url)

    def test_thirty_nine_char_ref_rejected(self) -> None:
        # One char short of the 40-hex requirement.
        url = f"git+https://github.com/foo/bar.git@{'a' * 39}"
        assert not has_full_commit_sha(url)

    def test_forty_one_char_ref_rejected(self) -> None:
        # One char over the 40-hex requirement.
        url = f"git+https://github.com/foo/bar.git@{'a' * 41}"
        assert not has_full_commit_sha(url)

    def test_forty_char_non_hex_ref_rejected(self) -> None:
        # Exactly 40 chars but not all hex ('g' is out of range).
        url = f"git+https://github.com/foo/bar.git@{'g' * 40}"
        assert not has_full_commit_sha(url)


class TestAdmitVcsUrlBlock:
    def test_block_default_refuses_vcs(self) -> None:
        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            admit_vcs_url(
                f"git+https://github.com/foo/bar.git@{_FORTY}",
                VcsConfig(),
            )

    def test_block_with_allowlists_still_refuses(self) -> None:
        """Filling allowlists without flipping policy must still refuse."""
        config = VcsConfig(
            policy=VcsPolicy.BLOCK,
            allowed_schemes=frozenset({"git+https"}),
        )
        with pytest.raises(UnsupportedVcsError, match='vcs.policy is "block"'):
            admit_vcs_url(
                f"git+https://github.com/foo/bar.git@{_FORTY}",
                config,
            )


class TestAdmitVcsUrlScheme:
    def test_allow_git_https_passes(self) -> None:
        scheme = admit_vcs_url(
            f"git+https://github.com/foo/bar.git@{_FORTY}",
            _allow_https(),
        )
        assert scheme == "git+https"

    def test_disallowed_scheme_refused(self) -> None:
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-schemes"):
            admit_vcs_url(
                f"git+ssh://git@github.com/foo/bar.git@{_FORTY}",
                _allow_https(),
            )

    def test_insecure_scheme_message_warns(self) -> None:
        with pytest.raises(UnsupportedVcsError, match="unauthenticated"):
            admit_vcs_url(
                f"git+http://example.com/r.git@{_FORTY}",
                _allow_https(),
            )

    def test_empty_allowlist_refuses(self) -> None:
        with pytest.raises(UnsupportedVcsError, match=r"\{<empty>\}"):
            admit_vcs_url(
                f"git+https://github.com/foo/bar.git@{_FORTY}",
                VcsConfig(policy=VcsPolicy.ALLOW),
            )


class TestAdmitVcsUrlRepo:
    def test_empty_repos_denies_all(self) -> None:
        # An empty allowed-repos under policy = "allow" admits nothing;
        # the user must list at least one repo prefix.
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url(
                f"git+https://example.com/r.git@{_FORTY}",
                config,
            )

    def test_matching_prefix_passes(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/apache/",),
        )
        scheme = admit_vcs_url(
            f"git+https://github.com/apache/airflow.git@{_FORTY}",
            config,
        )
        assert scheme == "git+https"

    def test_non_matching_prefix_refused(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/apache/",),
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url(
                f"git+https://github.com/other/airflow.git@{_FORTY}",
                config,
            )

    def test_prefix_match_against_inner_url_strips_vcs(self) -> None:
        """The prefix matches against the URL with the ``git+`` stripped."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/apache/airflow.git",),
        )
        scheme = admit_vcs_url(
            f"git+https://github.com/apache/airflow.git@{_FORTY}",
            config,
        )
        assert scheme == "git+https"

    def test_https_credentials_under_prefix_pass(self) -> None:
        """A token in the authority does not move the repo out of its org."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/myorg/",),
        )
        scheme = admit_vcs_url(
            f"git+https://x-access-token:secret@github.com/myorg/fork.git@{_FORTY}",
            config,
        )
        assert scheme == "git+https"

    def test_ssh_login_under_prefix_passes(self) -> None:
        """The mandatory SSH ``git@`` login does not block a prefix match."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+ssh"}),
            allowed_repos=("ssh://github.com/myorg/",),
        )
        scheme = admit_vcs_url(
            f"git+ssh://git@github.com/myorg/fork.git@{_FORTY}",
            config,
        )
        assert scheme == "git+ssh"

    def test_credentials_outside_prefix_still_refused(self) -> None:
        """Stripping credentials does not admit a repo outside the allowlist."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/myorg/",),
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url(
                f"git+https://user:pass@github.com/other/fork.git@{_FORTY}",
                config,
            )

    def test_sibling_repo_extending_full_repo_prefix_refused(self) -> None:
        """A repo whose path extends an allowed full-repo URL is refused."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/apache/airflow.git",),
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url(
                f"git+https://github.com/apache/airflow.git.other@{_FORTY}",
                config,
            )

    def test_sibling_org_extending_org_name_refused(self) -> None:
        """An org whose name extends an allowed org (no slash) is refused."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/apache",),
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url(
                f"git+https://github.com/apache-other/airflow.git@{_FORTY}",
                config,
            )

    def test_full_repo_prefix_with_ref_passes(self) -> None:
        """An exact full-repo URL followed by an ``@<sha>`` ref is admitted."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/apache/airflow.git",),
        )
        scheme = admit_vcs_url(
            f"git+https://github.com/apache/airflow.git@{_FORTY}",
            config,
        )
        assert scheme == "git+https"

    def test_full_repo_prefix_with_subdir_under_repo_passes(self) -> None:
        """A subdirectory path under an allowed full-repo URL stays in-repo."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/apache/airflow",),
        )
        scheme = admit_vcs_url(
            f"git+https://github.com/apache/airflow/sub.git@{_FORTY}",
            config,
        )
        assert scheme == "git+https"

    def test_org_without_trailing_slash_matches_repo_under_org(self) -> None:
        """An org prefix with no trailing slash admits a repo at a ``/``."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/apache",),
        )
        scheme = admit_vcs_url(
            f"git+https://github.com/apache/airflow.git@{_FORTY}",
            config,
        )
        assert scheme == "git+https"

    def test_exact_repo_prefix_matches_dot_git_clone_url(self) -> None:
        """An exact-repo prefix admits git's canonical ``.git`` clone URL."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/myorg/myrepo",),
        )
        scheme = admit_vcs_url(
            f"git+https://github.com/myorg/myrepo.git@{_FORTY}",
            config,
        )
        assert scheme == "git+https"

    def test_dot_git_prefix_matches_url_without_git_suffix(self) -> None:
        """A ``.git`` prefix admits the same repo written without ``.git``."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/myorg/myrepo.git",),
        )
        scheme = admit_vcs_url(
            f"git+https://github.com/myorg/myrepo@{_FORTY}",
            config,
        )
        assert scheme == "git+https"

    def test_dot_git_strip_does_not_admit_sibling_repo(self) -> None:
        """Treating ``.git`` as optional must not admit a distinct sibling."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/myorg/myrepo",),
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url(
                f"git+https://github.com/myorg/myrepo.gitother@{_FORTY}",
                config,
            )

    def test_dot_segment_escape_above_prefix_refused(self) -> None:
        """A ``../`` path git resolves outside the prefix is refused."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/trusted/repo",),
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url(
                f"git+https://github.com/trusted/repo/../../other/repo@{_FORTY}",
                config,
            )

    @pytest.mark.parametrize(
        "escape",
        [
            "/%2e%2e/%2e%2e/other/repo",
            "/%2E%2E/%2E%2E/other/repo",
            "/..%2f..%2fother/repo",
            "/..\\..\\other/repo",
            "/..%5c..%5cother/repo",
            "/%2e%2e\\%2e%2e/other/repo",
        ],
    )
    def test_encoded_dot_segment_escape_refused(self, escape: str) -> None:
        r"""An encoded escape is refused like the raw ``../`` form.

        A percent-encoded ``..`` decodes at fetch time, and Windows
        resolves ``\`` as a separator.
        """
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+file"}),
            allowed_repos=("file:///srv/repos/trusted/repo",),
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url(
                f"git+file:///srv/repos/trusted/repo{escape}@{_FORTY}",
                config,
            )

    def test_backslash_escape_over_ssh_refused(self) -> None:
        r"""The ``\`` escape is refused on a remote scheme too."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+ssh"}),
            allowed_repos=("ssh://host/srv/trusted/repo",),
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url(
                f"git+ssh://host/srv/trusted/repo/..\\..\\other@{_FORTY}",
                config,
            )

    def test_query_dot_segment_escape_refused(self) -> None:
        """A ``?query`` carrying the escape is refused.

        ``VcsRequest.parse`` keeps the query in the URL it hands git, so the
        rewrite check covers the whole post-authority remainder.
        """
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+file"}),
            allowed_repos=("file:///srv/repos/trusted/",),
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url(
                f"git+file:///srv/repos/trusted/repo?/../../other/repo@{_FORTY}",
                config,
            )

    def test_percent_encoded_repo_name_still_admits(self) -> None:
        """Encoding that decodes to no dot-segment leaves the repo admitted."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+file"}),
            allowed_repos=("file:///srv/repos/trusted",),
        )
        scheme = admit_vcs_url(
            f"git+file:///srv/repos/trusted/my%20repo@{_FORTY}",
            config,
        )
        assert scheme == "git+file"

    def test_double_encoded_dot_segment_still_admits(self) -> None:
        """Decoding runs once, so ``%252e%252e`` stays the directory ``%2e%2e``."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+file"}),
            allowed_repos=("file:///srv/repos/trusted",),
        )
        scheme = admit_vcs_url(
            f"git+file:///srv/repos/trusted/repo/%252e%252e/other@{_FORTY}",
            config,
        )
        assert scheme == "git+file"

    def test_literal_backslash_in_repo_name_still_admits(self) -> None:
        r"""A ``\`` that is not a dot-segment escape leaves the repo admitted."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+file"}),
            allowed_repos=("file:///srv/repos/trusted",),
        )
        scheme = admit_vcs_url(
            f"git+file:///srv/repos/trusted/my\\repo@{_FORTY}",
            config,
        )
        assert scheme == "git+file"

    def test_plain_repo_under_same_prefix_still_admits(self) -> None:
        """A plain in-repo URL under that prefix still admits."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/trusted/repo",),
        )
        scheme = admit_vcs_url(
            f"git+https://github.com/trusted/repo@{_FORTY}",
            config,
        )
        assert scheme == "git+https"

    def test_full_repo_prefix_with_fragment_passes(self) -> None:
        """A fragment directly after an allowed full-repo URL stays in-repo."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/apache/airflow.git",),
            require_pin=False,
        )
        scheme = admit_vcs_url(
            "git+https://github.com/apache/airflow.git#subdirectory=sub",
            config,
        )
        assert scheme == "git+https"


class TestAdmitVcsUrlRequirePin:
    def test_pinned_passes(self) -> None:
        admit_vcs_url(
            f"git+https://github.com/foo/bar.git@{_FORTY}",
            _allow_https(),
        )

    def test_unpinned_refused_when_required(self) -> None:
        with pytest.raises(UnsupportedVcsError, match="vcs.require-pin"):
            admit_vcs_url(
                "git+https://github.com/foo/bar.git",
                _allow_https(),
            )

    def test_tag_refused_when_pin_required(self) -> None:
        with pytest.raises(UnsupportedVcsError, match="vcs.require-pin"):
            admit_vcs_url(
                "git+https://github.com/foo/bar.git@v1.0",
                _allow_https(),
            )

    def test_sha_in_authority_refused_when_pin_required(self) -> None:
        """The clone parser sees no ref here, so admission must refuse."""
        with pytest.raises(UnsupportedVcsError, match="names no repository"):
            admit_vcs_url(
                f"git+https://github.com@{_FORTY}",
                _allow_https(),
            )

    def test_unpinned_passes_when_not_required(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://",),
            require_pin=False,
        )
        scheme = admit_vcs_url(
            "git+https://github.com/foo/bar.git",
            config,
        )
        assert scheme == "git+https"


class TestAdmitVcsUrlNonVcsRefusal:
    def test_https_archive_refused(self) -> None:
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            admit_vcs_url(
                "https://example.com/pkg.whl",
                _allow_https(),
            )

    def test_file_path_refused(self) -> None:
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            admit_vcs_url(
                "file:///tmp/pkg.tar.gz",
                _allow_https(),
            )

    def test_non_vcs_url_refused_even_under_block(self) -> None:
        """Non-VCS direct URLs are refused before the BLOCK check."""
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            admit_vcs_url("https://example.com/pkg.whl", VcsConfig())

    def test_hg_url_refused(self) -> None:
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            admit_vcs_url(
                "hg+https://hg.example.com/r",
                _allow_https(),
            )

    def test_svn_url_refused(self) -> None:
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            admit_vcs_url(
                "svn+https://svn.example.com/r",
                _allow_https(),
            )

    def test_bzr_url_refused(self) -> None:
        with pytest.raises(UnsupportedVcsError, match="not a recognized VCS scheme"):
            admit_vcs_url(
                "bzr+https://bzr.example.com/r",
                _allow_https(),
            )


class TestAdmitVcsUrlRepoPath:
    def test_pathless_url_refused(self) -> None:
        """A URL with no path names no repository, so it is refused."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://",),
            require_pin=False,
        )
        with pytest.raises(UnsupportedVcsError, match="names no repository"):
            admit_vcs_url("git+https://github.com", config)

    def test_pathless_url_with_login_refused(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+ssh"}),
            allowed_repos=("ssh://",),
            require_pin=False,
        )
        with pytest.raises(UnsupportedVcsError, match="names no repository"):
            admit_vcs_url("git+ssh://git@github.com", config)

    def test_pathless_url_with_sha_appended_refused(self) -> None:
        """The ``@<sha>`` here is userinfo, not a ref: still no repository."""
        with pytest.raises(UnsupportedVcsError, match="names no repository"):
            admit_vcs_url(f"git+https://github.com@{_FORTY}", _allow_https())

    def test_root_path_only_refused(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://",),
            require_pin=False,
        )
        with pytest.raises(UnsupportedVcsError, match="names no repository"):
            admit_vcs_url("git+https://github.com/", config)

    def test_ref_only_path_refused(self) -> None:
        """Everything after the final ``@`` is the ref, leaving no repo path."""
        with pytest.raises(UnsupportedVcsError, match="names no repository"):
            admit_vcs_url(f"git+https://github.com/@{_FORTY}", _allow_https())

    def test_repo_path_with_ref_admitted(self) -> None:
        scheme = admit_vcs_url(
            f"git+https://github.com/foo/bar.git@{_FORTY}",
            _allow_https(),
        )
        assert scheme == "git+https"


class TestPinAppendMonotonicity:
    """Appending a pin must never turn an admit into a refuse."""

    URLS = (
        "git+https://github.com",
        "git+https://github.com/",
        "git+https://github.com/foo",
        "git+https://github.com/foo/bar.git",
        "git+https://github.com/foo/bar.git@main",
        "git+https://user:pass@github.com/foo/bar.git",
    )

    def _config(self, *, require_pin: bool) -> VcsConfig:
        return VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("",),
            require_pin=require_pin,
        )

    def _admits(self, url: str, config: VcsConfig) -> bool:
        try:
            admit_vcs_url(url, config)
        except UnsupportedVcsError:
            return False
        return True

    @pytest.mark.parametrize("url", URLS)
    def test_pin_append_never_turns_admit_into_refuse(self, url: str) -> None:
        if not self._admits(url, self._config(require_pin=False)):
            return
        assert self._admits(f"{url}@{_FORTY}", self._config(require_pin=True))


class TestAdmitVcsUrlUserinfoPosition:
    def test_allowed_prefix_in_userinfo_position_refused(self) -> None:
        """An allowed host sitting in the userinfo is not the host cloned from."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com",),
            require_pin=False,
        )
        with pytest.raises(UnsupportedVcsError, match="not in vcs.allowed-repos"):
            admit_vcs_url("git+https://github.com@elsewhere.example/foo", config)


class TestAdmitVcsUrlMalformed:
    def test_unparseable_authority_refused(self) -> None:
        """An unclosed IPv6 bracket is refused, not raised through."""
        with pytest.raises(UnsupportedVcsError, match="does not parse"):
            admit_vcs_url("git+https://[/org/repo", _allow_https())

    def test_unparseable_authority_refused_with_empty_allowed_repos(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=(),
        )
        with pytest.raises(UnsupportedVcsError, match="does not parse"):
            admit_vcs_url("git+https://[/org/repo", config)

    def test_authority_malformed_only_after_userinfo_strip_refused(self) -> None:
        """A netloc that parses whole but not once its userinfo is stripped.

        ``[::1]@[::2`` balances its brackets across the whole netloc, so the
        first urlsplit accepts it. Dropping ``[::1]@`` leaves ``[::2``, an
        unclosed bracket that the allowed-repos match rejects.
        """
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/ok/repo",),
            require_pin=False,
        )
        with pytest.raises(UnsupportedVcsError, match="does not parse"):
            admit_vcs_url("git+https://[::1]@[::2/ok/repo", config)


class TestAdmitVcsUrlRealWorldShapes:
    """Shapes a user actually writes still admit."""

    def test_ssh_login_unpinned(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+ssh"}),
            allowed_repos=("ssh://github.com/org",),
            require_pin=False,
        )
        assert admit_vcs_url("git+ssh://git@github.com/org/repo", config) == "git+ssh"

    def test_ssh_login_with_pin(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+ssh"}),
            allowed_repos=("ssh://git@github.com/org/repo.git",),
        )
        scheme = admit_vcs_url(
            f"git+ssh://git@github.com/org/repo.git@{_FORTY}",
            config,
        )
        assert scheme == "git+ssh"

    def test_port_and_fragment_with_pin(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com:8443/org",),
        )
        scheme = admit_vcs_url(
            f"git+https://github.com:8443/org/repo.git@{_FORTY}#subdirectory=pkg",
            config,
        )
        assert scheme == "git+https"

    def test_repo_path_containing_at_sign_with_pin(self) -> None:
        """Only the final ``@`` is the ref, so an ``@`` in the path survives."""
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://example.org/org",),
        )
        scheme = admit_vcs_url(
            f"git+https://example.org/org/re@po@{_FORTY}",
            config,
        )
        assert scheme == "git+https"

    def test_branch_ref_admitted_when_pin_not_required(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+https"}),
            allowed_repos=("https://github.com/org",),
            require_pin=False,
        )
        scheme = admit_vcs_url("git+https://github.com/org/repo@main", config)
        assert scheme == "git+https"

    def test_file_url_with_pin(self) -> None:
        config = VcsConfig(
            policy=VcsPolicy.ALLOW,
            allowed_schemes=frozenset({"git+file"}),
            allowed_repos=("file:///srv/repos",),
        )
        scheme = admit_vcs_url(f"git+file:///srv/repos/pkg@{_FORTY}", config)
        assert scheme == "git+file"
