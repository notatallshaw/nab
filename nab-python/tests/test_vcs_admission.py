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
    # An empty allowed-repos now denies all, so list a permissive prefix
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
                f"git+https://github.com/evil/airflow.git@{_FORTY}",
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
                f"git+https://user:pass@github.com/evil/fork.git@{_FORTY}",
                config,
            )


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
        with pytest.raises(UnsupportedVcsError, match="vcs.require-pin"):
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
