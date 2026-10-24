"""ASCII URL recognition agrees with the standard parser's round trip."""

from urllib.parse import urljoin, urlsplit, urlunsplit

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nab_index import client

BASE = "https://index.example/simple/pkg/"


def expected_url(raw: str) -> str | None:
    try:
        joined = raw if raw.startswith(("https://", "http://")) else urljoin(BASE, raw)
        normalized = urlunsplit(urlsplit(joined))
        normalized.encode()
    except ValueError:
        return None
    return normalized


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


@settings(max_examples=1000, deadline=None)
@given(st.sampled_from(["http://", "https://", "", "HTTPS://"]), st.text(max_size=100))
def test_url_resolution_matches_parser_oracle(prefix: str, suffix: str) -> None:
    raw = prefix + suffix

    assert client._resolve_file_url(raw, BASE) == expected_url(raw)


@settings(max_examples=1000, deadline=None)
@given(
    st.sampled_from(["http://", "https://"]),
    st.text(alphabet=st.characters(max_codepoint=127), max_size=100),
)
def test_ascii_resolution_matches_parser_oracle(prefix: str, suffix: str) -> None:
    raw = prefix + suffix

    assert client._resolve_file_url(raw, BASE) == expected_url(raw)


def test_string_subclasses_keep_the_parser_path() -> None:
    class URL(str):
        __slots__ = ()

    raw = URL("https://files.example/pkg.whl")

    assert not client._normalized_ascii_http_url(raw)
    assert client._resolve_file_url(raw, BASE) == expected_url(raw)
