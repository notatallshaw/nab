"""Compare index file URLs with the standard parser over generated input."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nab_index import client

from ..url_normalization import BASE, expected_url

pytestmark = pytest.mark.property


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
