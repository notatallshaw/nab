"""ASCII URL recognition agrees with the standard parser's round trip."""

import pytest

from nab_index import client

from .url_normalization import BASE, expected_url


@pytest.mark.parametrize(
    "raw",
    [
        "https://files.example/pkg.whl",
        "http://user:pass@host:90/path?q=v#frag",
        "https://host/\x00.whl",
        "https://host/??value",
        "https://host/path#two#parts",
    ],
)
def test_normalized_ascii_http_urls_skip_parsing(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_parse(_url: str) -> None:
        raise AssertionError("already-normalized ASCII URL was parsed")

    monkeypatch.setattr(client, "urlsplit", unexpected_parse)

    assert client._resolve_file_url(raw, BASE) is raw


@pytest.mark.parametrize(
    "raw",
    [
        "https://host/path?",
        "https://host/path#",
        "https://host/path?#part",
        "https://host/pa\tth",
        "https://host/pa\rth",
        "https://host/pa\nth",
        "https://[::1]/pkg.whl",
        "https://[broken/pkg.whl",
        "https://host]/pkg.whl",
        "https:////host/pkg.whl",
        "https:///pkg.whl",
        "https://?query",
        "http://",
        "HTTPS://HOST/pkg.whl",
        "../pkg.whl",
        "https://host/é.whl",
        "https://host/\ud800.whl",
    ],
)
def test_other_urls_keep_the_full_normalization(raw: str) -> None:
    assert client._resolve_file_url(raw, BASE) == expected_url(raw)


def test_string_subclasses_keep_the_parser_path() -> None:
    class URL(str):
        __slots__ = ()

    raw = URL("https://files.example/pkg.whl")

    assert not client._normalized_ascii_http_url(raw)
    assert client._resolve_file_url(raw, BASE) == expected_url(raw)
