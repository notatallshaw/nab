"""Marker serialization preserves grouping and operator precedence."""

from __future__ import annotations

from nab_provider._vendor.packaging.markers import Marker


class TestFormatMarker:
    def test_an_item_serialises_left_to_right(self) -> None:
        assert str(Marker('python_version > "3.6"')) == 'python_version > "3.6"'

    def test_a_wrapping_group_loses_its_parentheses(self) -> None:
        marker = Marker('(python_version > "3.6" and os_name == "unix")')
        assert str(marker) == 'python_version > "3.6" and os_name == "unix"'

    def test_repeated_wrapping_groups_all_unwrap(self) -> None:
        assert str(Marker('((python_version > "3.6"))')) == 'python_version > "3.6"'

    def test_a_nested_group_keeps_its_parentheses(self) -> None:
        text = 'os_name == "nt" or (python_version > "3.6" and os_name == "posix")'
        assert str(Marker(text)) == text
