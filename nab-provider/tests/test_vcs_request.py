"""Tests for nab_provider.vcs_request._split_repo_ref."""

from __future__ import annotations

from nab_provider.vcs_request import _split_repo_ref


def test_split_repo_ref_bare_local_name_has_no_ref() -> None:
    # Without a scheme delimiter, the whole string is the repo and ref is empty.
    assert _split_repo_ref("myrepo") == ("myrepo", "")
