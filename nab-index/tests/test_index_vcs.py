"""Tests for nab_index.vcs._split_repo_ref."""

from __future__ import annotations

from nab_index.vcs import _split_repo_ref


def test_split_repo_ref_bare_local_name_has_no_ref() -> None:
    # No scheme and no colon: the whole string is the repo, ref is empty.
    assert _split_repo_ref("myrepo") == ("myrepo", "")
