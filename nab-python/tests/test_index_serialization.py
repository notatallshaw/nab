"""Tests for nab_index.serialization."""

from __future__ import annotations

from nab_index.serialization import SimpleSerialization, simple_accept_header


class TestAcceptHeader:
    def test_negotiate_advertises_every_supported_type(self) -> None:
        assert simple_accept_header(SimpleSerialization.NEGOTIATE) == (
            "application/vnd.pypi.simple.v1+json, "
            "application/vnd.pypi.simple.v1+html;q=0.2, "
            "text/html;q=0.01"
        )

    def test_json_asks_for_one_type_without_a_quality_value(self) -> None:
        assert (
            simple_accept_header(SimpleSerialization.JSON)
            == "application/vnd.pypi.simple.v1+json"
        )

    def test_html_also_advertises_the_pep503_spelling(self) -> None:
        assert simple_accept_header(SimpleSerialization.HTML) == (
            "application/vnd.pypi.simple.v1+html, text/html;q=0.01"
        )


class TestVocabulary:
    def test_values_are_the_config_vocabulary(self) -> None:
        assert [m.value for m in SimpleSerialization] == ["negotiate", "json", "html"]
