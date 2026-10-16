"""Direct archives require permission for their origin and expected contents."""

import pytest

from nab_provider.archive_admission import (
    ArchiveConfig,
    ArchivePolicy,
    UnsupportedArchiveError,
    admit_archive_url,
    validate_archive_prefix,
)

HASH = "0" * 64


def permitted(*urls: str, require_hashes: bool = True) -> ArchiveConfig:
    """An explicit acquisition opt-in, still requiring hashes by default."""
    return ArchiveConfig(
        policy=ArchivePolicy.ALLOW, allowed_urls=urls, require_hashes=require_hashes
    )


def test_default_refuses_before_parsing() -> None:
    with pytest.raises(UnsupportedArchiveError, match='archive.policy="declared"'):
        admit_archive_url("not a URL", ArchiveConfig())


@pytest.mark.parametrize(
    ("url", "prefix"),
    [
        ("https://example.org/releases/core.whl", "https://example.org/releases/"),
        (
            "https://example.org/releases/core.whl?download=1",
            "https://example.org/releases/core.whl",
        ),
        (
            "https://user:secret@EXAMPLE.org:443/releases/core.whl",
            "https://example.org/releases",
        ),
        ("http://example.org:80/core.tar.gz", "http://example.org/"),
        ("https://example.org", "https://example.org"),
        ("file:///repo/dist/core.zip", "file:///repo/dist/"),
        ("https://example.org/a%20b/core.whl", "https://example.org/a%20b/"),
    ],
)
def test_allowed_location_with_expected_hash(url: str, prefix: str) -> None:
    request = admit_archive_url(url + "#sha256=" + HASH, permitted(prefix))
    assert request.url == url
    assert request.hashes == (("sha256", HASH),)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/releases/core.whl",
        "https://other.org/releases/core.whl",
        "https://example.org:444/releases/core.whl",
        "https://example.org.other/releases/core.whl",
        "https://example.org/releases-other/core.whl",
        "https://example.org/elsewhere/core.whl",
    ],
)
def test_permissions_do_not_match_other_locations(url: str) -> None:
    with pytest.raises(UnsupportedArchiveError, match="allowed-urls does not permit"):
        admit_archive_url(
            url + "#sha256=" + HASH, permitted("https://example.org/releases")
        )


def test_allow_without_locations_still_refuses() -> None:
    with pytest.raises(UnsupportedArchiveError, match="allowed-urls does not permit"):
        admit_archive_url("https://example.org/core.whl#sha256=" + HASH, permitted())


def test_permission_does_not_disable_hash_requirements() -> None:
    with pytest.raises(UnsupportedArchiveError, match="expected digest"):
        admit_archive_url(
            "https://example.org/core.whl", permitted("https://example.org/")
        )
    request = admit_archive_url(
        "https://example.org/core.whl",
        permitted("https://example.org/", require_hashes=False),
    )
    assert request.hashes == ()


def test_empty_hash_is_never_an_opt_out() -> None:
    with pytest.raises(UnsupportedArchiveError, match="cannot be empty"):
        admit_archive_url(
            "https://example.org/core.whl#sha256=",
            permitted("https://example.org/", require_hashes=False),
        )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/core.zip",
        "relative.zip",
        "file:relative.zip",
        "https:///core.zip",
        "https://[broken/core.zip",
        "https://example.org:bad/core.zip",
        "https://example.org/a/../core.zip",
        "https://example.org/a/%2e%2e/core.zip",
        "https://example.org/a/./core.zip",
        "https://example.org/a%5cb/core.zip",
        "https://example.org/a\n/core.zip",
        "https://example.org/a%00b/core.zip",
    ],
)
def test_unstable_or_invalid_locations_are_refused(url: str) -> None:
    with pytest.raises(UnsupportedArchiveError, match="invalid archive"):
        admit_archive_url(url + "#sha256=" + HASH, permitted("https://example.org/"))


@pytest.mark.parametrize(
    "prefix",
    [
        "https://user:secret@example.org/",
        "https://example.org/?key=secret",
        "https://example.org/#hash",
    ],
)
def test_allowlists_do_not_carry_credentials_or_queries(prefix: str) -> None:
    with pytest.raises(ValueError, match="omit credentials"):
        validate_archive_prefix(prefix)


def test_policy_errors_do_not_echo_credentials() -> None:
    url = "https://name:secret@example.org/core.whl#sha256=invalid"
    with pytest.raises(UnsupportedArchiveError, match="invalid hashes") as exc:
        admit_archive_url(url, permitted("https://example.org/"))
    assert "secret" not in str(exc.value)


def test_selected_archive_does_not_need_a_second_url_permission() -> None:
    request = admit_archive_url(
        "https://example.org/core.zip#sha256=" + HASH, ArchiveConfig(), declared=True
    )
    assert request.hashes == (("sha256", HASH),)


def test_explicit_archive_block_overrides_a_declaration() -> None:
    with pytest.raises(UnsupportedArchiveError, match='archive.policy="block"'):
        admit_archive_url(
            "https://example.org/core.zip#sha256=" + HASH,
            ArchiveConfig(policy=ArchivePolicy.BLOCK),
            declared=True,
        )


def test_declaration_does_not_disable_hash_policy() -> None:
    with pytest.raises(UnsupportedArchiveError, match="requires an expected digest"):
        admit_archive_url(
            "https://example.org/core.zip", ArchiveConfig(), declared=True
        )
