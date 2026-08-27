"""Tests for the lightweight METADATA parser."""

from __future__ import annotations

import email.parser
import sys

import pytest

from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.version import Version
from nab_provider.metadata import (
    _READ_FIELDS,
    _read_header_fields,
    metadata_deps_are_static,
    metadata_header_block,
    parse_metadata,
    validate_specifier_versions,
)


def test_parses_minimal_metadata() -> None:
    """A minimal valid METADATA blob round-trips to ``WheelMetadata``."""
    text = "Metadata-Version: 2.1\nName: foo\nVersion: 1.2.3\n"
    md = parse_metadata(text)
    assert md.name == "foo"
    assert md.version == Version("1.2.3")
    assert md.metadata_version == "2.1"
    assert md.requires_python is None
    assert md.requires_dist == []
    assert md.provides_extra == []


def test_accepts_bytes_input() -> None:
    """``data`` may be ``bytes``; UTF-8 decode is automatic."""
    text = b"Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
    md = parse_metadata(text)
    assert md.name == "foo"
    assert md.version == Version("1.0")


def test_name_surrounding_whitespace_stripped() -> None:
    """RFC 822 whitespace around ``Name`` is insignificant."""
    md = parse_metadata("Metadata-Version: 2.1\nName:  foo \nVersion: 1.0\n")
    assert md.name == "foo"


def test_missing_name_raises() -> None:
    """Absent ``Name`` is a parser error, not a silent default."""
    text = "Metadata-Version: 2.1\nVersion: 1.0\n"
    with pytest.raises(ValueError, match="Name"):
        parse_metadata(text)


def test_missing_version_raises() -> None:
    """Absent ``Version`` is a parser error, not a silent default."""
    text = "Metadata-Version: 2.1\nName: foo\n"
    with pytest.raises(ValueError, match="Version"):
        parse_metadata(text)


def test_valid_requires_python_parsed() -> None:
    """A well-formed Requires-Python becomes a ``SpecifierSet``."""
    text = "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Python: >=3.8\n"
    md = parse_metadata(text)
    assert md.requires_python == SpecifierSet(">=3.8")


def test_malformed_requires_python_raises() -> None:
    """A malformed Requires-Python is invalid metadata, so parsing raises.

    ``!=3.3*`` is not a valid PEP 440 specifier (the wildcard needs ``.*``).
    The resolve boundary turns this into a ``MetadataError`` so the candidate
    is dropped rather than pinned with an unread Python constraint.
    """
    text = (
        "Metadata-Version: 2.1\nName: azure-iot-hub\nVersion: 2.4.0\n"
        "Requires-Python: >=2.7, !=3.0.*, !=3.3*, <4\n"
        "Requires-Dist: msrest\n"
    )
    with pytest.raises(ValueError, match="invalid Requires-Python"):
        parse_metadata(text)


def test_dynamic_field_lowercased() -> None:
    """PEP 643 ``Dynamic`` field names are normalised to lowercase."""
    text = (
        "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n"
        "Dynamic: Requires-Dist\nDynamic: PROVIDES-EXTRA\n"
    )
    md = parse_metadata(text)
    assert md.dynamic == frozenset({"requires-dist", "provides-extra"})


def test_dynamic_field_whitespace_stripped() -> None:
    """Surrounding whitespace on a ``Dynamic`` value is insignificant."""
    text = (
        "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n"
        "Dynamic: Requires-Dist \nDynamic: Provides-Extra\t\n"
    )
    md = parse_metadata(text)
    assert md.dynamic == frozenset({"requires-dist", "provides-extra"})


def test_requires_dist_parsed() -> None:
    """Each ``Requires-Dist`` entry yields a parsed ``Requirement``."""
    text = (
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
        "Requires-Dist: bar>=1.0\nRequires-Dist: baz<2\n"
    )
    md = parse_metadata(text)
    assert [str(r) for r in md.requires_dist] == ["bar>=1.0", "baz<2"]


def test_long_description_is_not_read() -> None:
    """Header fields survive a description that repeats them as text."""
    text = (
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=1.0\n"
        "\nName: not-a-header\nRequires-Dist: nope\n"
    )
    md = parse_metadata(text)
    assert md.name == "foo"
    assert [str(r) for r in md.requires_dist] == ["bar>=1.0"]


def test_crlf_document_with_long_description() -> None:
    """A CRLF document ends its header block at the blank CRLF line."""
    text = (
        "Metadata-Version: 2.1\r\nName: foo\r\nVersion: 1.0\r\n"
        "Requires-Dist: bar>=1.0\r\n\r\nName: not-a-header\r\n"
    )
    md = parse_metadata(text)
    assert md.name == "foo"
    assert md.version == Version("1.0")
    assert [str(r) for r in md.requires_dist] == ["bar>=1.0"]


def test_equal_markers_are_interned() -> None:
    """The same marker text across different dep strings shares one object."""
    text = (
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
        'Requires-Dist: pytest; extra == "test"\n'
        'Requires-Dist: coverage; extra == "test"\n'
        'Requires-Dist: ruff; extra == "lint"\n'
    )
    md = parse_metadata(text)
    pytest_marker, coverage_marker, ruff_marker = (r.marker for r in md.requires_dist)
    assert pytest_marker is coverage_marker
    assert ruff_marker is not pytest_marker


def test_markerless_requirement_keeps_none_marker() -> None:
    """A dep without a marker is unaffected by interning."""
    md = parse_metadata(
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\nRequires-Dist: bar>=1.0\n"
    )
    assert md.requires_dist[0].marker is None


def test_provides_extra_kept_as_strings() -> None:
    """``Provides-Extra`` values are retained as raw strings."""
    text = (
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
        "Provides-Extra: dev\nProvides-Extra: docs\n"
    )
    md = parse_metadata(text)
    assert md.provides_extra == ["dev", "docs"]


def test_provides_extra_whitespace_stripped() -> None:
    """Surrounding whitespace on a ``Provides-Extra`` value is insignificant."""
    text = (
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
        "Provides-Extra: dev \nProvides-Extra: docs\t\n"
    )
    md = parse_metadata(text)
    assert md.provides_extra == ["dev", "docs"]


_HEADER_BLOCK_DOCUMENTS = [
    pytest.param(
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.2.3\n"
        "Requires-Python: >=3.9\nRequires-Dist: bar>=2\n"
        "Provides-Extra: docs\n\nName: not-a-header\nA description.\n",
        id="lf",
    ),
    pytest.param(
        "Metadata-Version: 2.1\r\nName: foo\r\nVersion: 1.2.3\r\n"
        "Requires-Dist: bar>=2\r\n\r\nA description.\r\n",
        id="crlf",
    ),
    pytest.param(
        "Metadata-Version: 2.1\r\nName: foo\r\nVersion: 1.2.3\r\n"
        "Requires-Dist: bar>=2\r\n\r\nFirst para.\n\nSecond para.\n",
        id="crlf-headers-lf-description",
    ),
    pytest.param(
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.2.3\n"
        "Summary: line one\nline two unfolded\nRequires-Dist: bar>=2\n"
        "\nA description.\n",
        id="unfolded-field-ends-the-headers-early",
    ),
    pytest.param("Metadata-Version: 2.1\nName: foo\nVersion: 1.2.3\n", id="no-body"),
]


class TestMetadataHeaderBlock:
    """Cutting a document at its header boundary."""

    def test_description_after_the_blank_line_is_dropped(self) -> None:
        text = "Name: foo\nVersion: 1.0\n\nA long description.\n"
        assert metadata_header_block(text) == "Name: foo\nVersion: 1.0\n\n"

    def test_crlf_blank_line_ends_the_headers(self) -> None:
        text = "Name: foo\r\nVersion: 1.0\r\n\r\nA long description.\r\n"
        assert metadata_header_block(text) == "Name: foo\r\nVersion: 1.0\r\n\r\n"

    def test_crlf_headers_cut_before_an_lf_description(self) -> None:
        text = "Name: foo\r\nVersion: 1.0\r\n\r\nFirst para.\n\nSecond para.\n"
        assert metadata_header_block(text) == "Name: foo\r\nVersion: 1.0\r\n\r\n"

    def test_document_without_a_blank_line_comes_back_whole(self) -> None:
        text = "Name: foo\nVersion: 1.0\n"
        assert metadata_header_block(text) is text

    def test_cuts_at_the_first_blank_line(self) -> None:
        text = "Name: foo\nVersion: 1.0\n\nFirst para.\n\nSecond para.\n"
        assert metadata_header_block(text) == "Name: foo\nVersion: 1.0\n\n"

    @pytest.mark.parametrize("text", _HEADER_BLOCK_DOCUMENTS)
    def test_the_cut_keeps_the_whole_header_block(self, text: str) -> None:
        """The prefix ends at or after where ``email.parser`` ends the headers."""
        cut = metadata_header_block(text)
        assert text.startswith(cut)

        parser = email.parser.Parser()
        assert parser.parsestr(cut).items() == parser.parsestr(text).items()
        assert parse_metadata(cut) == parse_metadata(text)


class TestMetadataDepsAreStatic:
    def test_2_2_without_dynamic_is_static(self) -> None:
        md = parse_metadata("Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n")
        assert metadata_deps_are_static(md) is True

    def test_2_3_is_static(self) -> None:
        md = parse_metadata("Metadata-Version: 2.3\nName: foo\nVersion: 1.0\n")
        assert metadata_deps_are_static(md) is True

    def test_2_2_with_dynamic_requires_dist_is_not_static(self) -> None:
        md = parse_metadata(
            "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\nDynamic: Requires-Dist\n"
        )
        assert metadata_deps_are_static(md) is False

    def test_2_2_with_whitespace_dynamic_requires_dist_is_not_static(self) -> None:
        md = parse_metadata(
            "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\nDynamic: Requires-Dist \n"
        )
        assert metadata_deps_are_static(md) is False

    def test_2_2_with_dynamic_provides_extra_is_not_static(self) -> None:
        """Provides-Extra is the other DEPENDENCY_FIELDS member: a dynamic
        extras set leaves the deps non-final even with a static Requires-Dist.
        """
        md = parse_metadata(
            "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n"
            "Requires-Dist: bar\nDynamic: Provides-Extra\n"
        )
        assert metadata_deps_are_static(md) is False

    def test_micro_metadata_version_qualifies(self) -> None:
        md = parse_metadata("Metadata-Version: 2.2.1\nName: foo\nVersion: 1.0\n")
        assert metadata_deps_are_static(md) is True

    def test_pre_2_2_is_not_static(self) -> None:
        md = parse_metadata("Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n")
        assert metadata_deps_are_static(md) is False

    def test_missing_metadata_version_is_not_static(self) -> None:
        md = parse_metadata("Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n")
        md.metadata_version = None
        assert metadata_deps_are_static(md) is False

    def test_unparseable_metadata_version_is_not_static(self) -> None:
        md = parse_metadata("Metadata-Version: 2.x\nName: foo\nVersion: 1.0\n")
        assert metadata_deps_are_static(md) is False


_AT_LIMIT = "1" * sys.get_int_max_str_digits()
_OVERSIZED = _AT_LIMIT + "1"


class TestValidateSpecifierVersions:
    """A clause ``SpecifierSet`` accepts can still fail to convert later."""

    @pytest.mark.parametrize(
        "spec",
        [
            ">=3.8",
            "==1.0.*",
            "!=2.*",
            "~=1.4.2",
            "===lolwat",
            "===1.0-bananas",
            "===1.0",
            "==1.0+abc",
            "<4,>=3.9",
            ">1!2.0",
            "==1.0.0.dev1",
            f"=={_AT_LIMIT}",
        ],
    )
    def test_accepts_every_legal_clause(self, spec: str) -> None:
        """Including a run of exactly the limit, which int() still converts."""
        assert validate_specifier_versions(SpecifierSet(spec)) is None

    @pytest.mark.parametrize(
        "spec",
        [
            pytest.param(f">={_OVERSIZED}", id="release"),
            pytest.param(f">={_OVERSIZED}!1.0", id="epoch"),
            pytest.param(f">=1.0rc{_OVERSIZED}", id="pre"),
            pytest.param(f">=1.0.post{_OVERSIZED}", id="post"),
            pytest.param(f">=1.0.dev{_OVERSIZED}", id="dev"),
            pytest.param(f"==1.0+{_OVERSIZED}", id="local"),
            pytest.param(f"=={_OVERSIZED}.*", id="wildcard"),
            pytest.param(f"==={_OVERSIZED}", id="arbitrary"),
        ],
    )
    def test_rejects_oversized_digit_run(self, spec: str) -> None:
        """``===`` is included: ``to_range`` converts its literal too."""
        with pytest.raises(ValueError, match="Exceeds the limit"):
            validate_specifier_versions(SpecifierSet(spec))


class TestOversizedClauseVersions:
    """A clause version past the int-from-string limit fails at parse time.

    ``SpecifierSet`` and ``Requirement`` both accept one, converting it only
    when something compares against it, so ``parse_metadata`` forces the
    conversion and the field fails where it is read.
    """

    def test_oversized_requires_python_raises(self) -> None:
        """Requires-Python fails under its own guard, which names the field."""
        text = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            f"Requires-Python: >=3.{_OVERSIZED}\n"
        )
        with pytest.raises(ValueError, match="invalid Requires-Python"):
            parse_metadata(text)

    def test_oversized_requires_dist_raises(self) -> None:
        """A dep clause fails with the raw ``int()`` conversion error."""
        text = (
            "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
            f"Requires-Dist: click>=8.{_OVERSIZED}\n"
        )
        with pytest.raises(ValueError, match="Exceeds the limit"):
            parse_metadata(text)


class TestReadHeaderFields:
    """``_read_header_fields``: folding, envelope lines, and where headers stop."""

    def test_repeated_fields_keep_file_order(self) -> None:
        """Repeats stack in order, and a re-declared single field keeps the first."""
        fields = _read_header_fields(
            "Name: foo\nRequires-Dist: a\nProvides-Extra: dev\n"
            "Requires-Dist: b\nName: bar\nProvides-Extra: test\n"
        )
        assert fields["requires-dist"] == ["a", "b"]
        assert fields["provides-extra"] == ["dev", "test"]
        assert fields["name"] == ["foo", "bar"]

        assert parse_metadata("Name: foo\nVersion: 1.0\nName: bar\n").name == "foo"

    def test_continuation_line_keeps_its_line_ending(self) -> None:
        """A folded value joins its lines verbatim, minus the last line ending."""
        assert _read_header_fields("Requires-Dist: bar;\n extra == 'dev'\n") == {
            "requires-dist": ["bar;\n extra == 'dev'"]
        }
        assert _read_header_fields("Requires-Dist: bar;\r\n\textra == 'dev'\r\n") == {
            "requires-dist": ["bar;\r\n\textra == 'dev'"]
        }

    def test_value_may_start_on_a_continuation_line(self) -> None:
        """A blank first line leaves the value starting at the fold's own text."""
        assert _read_header_fields("Requires-Dist:\n  bar\n") == {
            "requires-dist": ["bar"]
        }

    def test_continuation_with_nothing_to_continue_is_dropped(self) -> None:
        """A fold before any field, or under one that is not read, adds nothing."""
        assert _read_header_fields(" orphan\nName: foo\n") == {"name": ["foo"]}
        assert _read_header_fields("Summary: s\n more of it\nName: foo\n") == {
            "name": ["foo"]
        }

    def test_field_values_are_stripped_of_leading_blanks_only(self) -> None:
        """The value starts after the colon's spaces and keeps its trailing ones."""
        assert _read_header_fields("Name:\t  foo  \n") == {"name": ["foo  "]}

    def test_envelope_from_line_is_not_a_field(self) -> None:
        """``From `` lines are mbox envelope headers and carry no field."""
        assert _read_header_fields(
            "From nobody Wed Aug 20 03:00:00 2026\nName: foo\nFrom someone else\n"
        ) == {"name": ["foo"]}

    def test_last_field_stays_open_to_the_end_of_the_text(self) -> None:
        """A document that stops without a line ending still yields its last field."""
        assert _read_header_fields("Name: foo\nVersion: 1.0") == {
            "name": ["foo"],
            "version": ["1.0"],
        }

    def test_bare_carriage_return_ends_a_line(self) -> None:
        """A lone ``\\r`` ends a line, as it does in :mod:`email`'s own reader."""
        assert _read_header_fields("Name: foo\rVersion: 1.0\r") == {
            "name": ["foo"],
            "version": ["1.0"],
        }

    @pytest.mark.parametrize(
        "text",
        [
            "Name: foo\nnot a header at all\nVersion: 1.0\n",
            "Name: foo\nBad Name: x\nVersion: 1.0\n",
            "Name: foo\nNo\x7fnprintable: x\nVersion: 1.0\n",
            "Name: foo\nnon-ascii-é: x\nVersion: 1.0\n",
        ],
    )
    def test_headers_end_at_the_first_non_field_line(self, text: str) -> None:
        """Anything but a continuation or ``name:`` ends the headers."""
        assert _read_header_fields(text) == {"name": ["foo"]}

    def test_a_non_ascii_name_that_lowercases_to_ascii_is_not_a_field(self) -> None:
        """U+212A lowercases to ASCII ``k``, which does not make it a field name."""
        assert _read_header_fields("Name: foo\n\u212a: x\nVersion: 1.0\n") == {
            "name": ["foo"]
        }

    def test_no_read_field_name_contains_k(self) -> None:
        """A ``k`` in a read field would let U+212A reach the lookup as that field."""
        assert not any("k" in name for name in _READ_FIELDS)

    def test_field_with_an_empty_name_is_skipped(self) -> None:
        """A line starting with a colon has no name, and the headers continue."""
        assert _read_header_fields(": stray\nName: foo\n") == {"name": ["foo"]}
