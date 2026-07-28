"""Tests for the lightweight METADATA parser."""

from __future__ import annotations

import pytest

from nab_python._vendor.packaging.specifiers import SpecifierSet
from nab_python._vendor.packaging.version import Version
from nab_python.metadata import metadata_deps_are_static, parse_metadata


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
