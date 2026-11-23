"""Direct archive URL parsing in the provider."""

from __future__ import annotations

import pytest

from nab_provider.archive import ArchiveRequest, ArchiveRequestError


class TestArchiveRequestParse:
    def test_url_hash_and_subdirectory(self) -> None:
        req = ArchiveRequest.parse(
            "https://ex.com/foo-1.0.tar.gz#sha256=abc&subdirectory=pkg"
        )
        assert req.url == "https://ex.com/foo-1.0.tar.gz"
        assert req.hashes == (("sha256", "abc"),)
        assert req.subdirectory == "pkg"

    def test_unknown_fragment_key_raises(self) -> None:
        with pytest.raises(ArchiveRequestError, match="unknown archive URL fragment"):
            ArchiveRequest.parse("https://ex.com/foo.tar.gz#egg=foo")

    def test_missing_equals_raises(self) -> None:
        with pytest.raises(ArchiveRequestError, match="malformed archive URL fragment"):
            ArchiveRequest.parse("https://ex.com/foo.tar.gz#sha256")

    def test_empty_fragment_part_ignored(self) -> None:
        req = ArchiveRequest.parse("https://ex.com/foo.tar.gz#sha256=abc&")
        assert req.hashes == (("sha256", "abc"),)

    def test_unescaped_space_rejected(self) -> None:
        url = "https://ex.com/foo 1.0.tar.gz"
        with pytest.raises(ArchiveRequestError) as excinfo:
            ArchiveRequest.parse(f"{url}#sha256=abc")
        assert str(excinfo.value) == (
            f"archive URL {url!r} contains an unescaped space;"
            " percent-encode spaces as %20"
        )

    def test_percent_encoded_space_allowed(self) -> None:
        req = ArchiveRequest.parse("https://ex.com/foo%201.0.tar.gz#sha256=abc")
        assert req.url == "https://ex.com/foo%201.0.tar.gz"

    def test_non_hex_digest_raises(self) -> None:
        with pytest.raises(ArchiveRequestError, match="is not hexadecimal"):
            ArchiveRequest.parse("https://ex.com/foo.tar.gz#sha256=not-a-digest")

    def test_path_shaped_digest_raises(self) -> None:
        """The digest names the extraction cache directory, so it cannot escape it."""
        with pytest.raises(ArchiveRequestError, match="is not hexadecimal"):
            ArchiveRequest.parse("https://ex.com/foo.tar.gz#sha256=../../x")

    def test_empty_digest_kept_as_unusable(self) -> None:
        req = ArchiveRequest.parse("https://ex.com/foo.tar.gz#sha256=")
        assert req.hashes == (("sha256", ""),)
        assert not req.has_usable_hash

    def test_parent_subdirectory_rejected(self) -> None:
        with pytest.raises(ArchiveRequestError, match="unsafe archive subdirectory"):
            ArchiveRequest.parse(
                "https://ex.com/foo.tar.gz#sha256=abc&subdirectory=../../etc"
            )

    @pytest.mark.parametrize(
        "subdirectory", ["../source-root/demo", r"..\source-root\demo"]
    )
    def test_source_root_named_parent_escape_rejected(self, subdirectory: str) -> None:
        with pytest.raises(ArchiveRequestError, match="unsafe archive subdirectory"):
            ArchiveRequest.parse(
                f"https://ex.com/foo.tar.gz#sha256=abc&subdirectory={subdirectory}"
            )

    def test_absolute_subdirectory_rejected(self) -> None:
        with pytest.raises(ArchiveRequestError, match="unsafe archive subdirectory"):
            ArchiveRequest.parse(
                "https://ex.com/foo.tar.gz#sha256=abc&subdirectory=/etc"
            )

    def test_escaping_internal_dotdot_rejected(self) -> None:
        with pytest.raises(ArchiveRequestError, match="unsafe archive subdirectory"):
            ArchiveRequest.parse(
                "https://ex.com/foo.tar.gz#sha256=abc&subdirectory=a/../../etc"
            )

    def test_contained_internal_dotdot_allowed(self) -> None:
        # a/../b normalises to b, which stays inside the tree, so it is kept.
        req = ArchiveRequest.parse(
            "https://ex.com/foo.tar.gz#sha256=abc&subdirectory=a/../b"
        )
        assert req.subdirectory == "a/../b"

    def test_backslash_escape_rejected(self) -> None:
        # The join is native, so a backslash escapes on Windows; the ntpath
        # containment check rejects it on every platform.
        with pytest.raises(ArchiveRequestError, match="unsafe archive subdirectory"):
            ArchiveRequest.parse(
                "https://ex.com/foo.tar.gz#sha256=abc&subdirectory=..\\..\\etc"
            )

    def test_drive_subdirectory_rejected(self) -> None:
        with pytest.raises(ArchiveRequestError, match="unsafe archive subdirectory"):
            ArchiveRequest.parse(
                "https://ex.com/foo.tar.gz#sha256=abc&subdirectory=C:\\other"
            )

    def test_contained_backslash_subdirectory_allowed(self) -> None:
        # A backslash-separated path that stays inside the tree is kept.
        req = ArchiveRequest.parse(
            "https://ex.com/foo.tar.gz#sha256=abc&subdirectory=sub\\deeper"
        )
        assert req.subdirectory == "sub\\deeper"

    def test_posix_backslash_parent_escape_rejected(self) -> None:
        # On POSIX ``c\d`` is a single segment, so ``c\d/../..`` climbs above
        # the source root even though ntpath alone reports it contained.
        with pytest.raises(ArchiveRequestError, match="unsafe archive subdirectory"):
            ArchiveRequest.parse(
                "https://ex.com/foo.tar.gz#sha256=abc&subdirectory=c\\d/../.."
            )
