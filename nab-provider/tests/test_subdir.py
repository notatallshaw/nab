"""Tests for nab_provider.subdir."""

from __future__ import annotations

from nab_provider.subdir import subdirectory_escapes


class TestSubdirectoryEscapes:
    def test_empty_subdirectory_is_the_root(self) -> None:
        assert subdirectory_escapes("") is False

    def test_plain_relative_path_stays_contained(self) -> None:
        assert subdirectory_escapes("packages/core") is False

    def test_parent_component_escapes(self) -> None:
        assert subdirectory_escapes("../outside") is True

    def test_absolute_path_escapes(self) -> None:
        assert subdirectory_escapes("/etc") is True

    def test_windows_drive_letter_escapes(self) -> None:
        assert subdirectory_escapes("C:\\windows") is True

    def test_posix_backslash_parent_escapes(self) -> None:
        assert subdirectory_escapes("c\\d/../..") is True

    def test_backslash_segment_stays_contained(self) -> None:
        assert subdirectory_escapes("sub\\deeper") is False
