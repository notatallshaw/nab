"""Credential-free URL forms retain their source identity."""

import pytest

from nab_index.urls import strip_userinfo


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://Example.com:443/simple?a=1#part",
            "https://Example.com:443/simple?a=1#part",
        ),
        (
            "https://user:password@Example.com:443/simple?a=1#part",
            "https://Example.com:443/simple?a=1#part",
        ),
        ("https://token@example.com/simple/", "https://example.com/simple/"),
        ("ssh://git@example.com/repository", "ssh://git@example.com/repository"),
        (
            "git+ssh://git:password@example.com/repository",
            "git+ssh://git@example.com/repository",
        ),
        ("https://user:p%40ss@[::1]:8080/simple/", "https://[::1]:8080/simple/"),
    ],
)
def test_url_identity_without_credentials(url: str, expected: str) -> None:
    assert strip_userinfo(url) == expected
