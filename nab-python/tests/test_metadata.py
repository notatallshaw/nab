"""Tests for the lightweight METADATA parser."""

from __future__ import annotations

import pytest

from nab_python._vendor.packaging.version import Version
from nab_python.metadata import parse_metadata


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


def test_dynamic_field_lowercased() -> None:
    """PEP 643 ``Dynamic`` field names are normalised to lowercase."""
    text = (
        "Metadata-Version: 2.2\nName: foo\nVersion: 1.0\n"
        "Dynamic: Requires-Dist\nDynamic: PROVIDES-EXTRA\n"
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


def test_provides_extra_kept_as_strings() -> None:
    """``Provides-Extra`` values are retained as raw strings."""
    text = (
        "Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n"
        "Provides-Extra: dev\nProvides-Extra: docs\n"
    )
    md = parse_metadata(text)
    assert md.provides_extra == ["dev", "docs"]
