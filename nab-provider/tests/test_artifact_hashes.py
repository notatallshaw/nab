"""Hash parsing and selection for package files."""

from __future__ import annotations

from nab_provider.records import (
    parse_hash_table,
    select_artifact_hash,
    sidecar_hash,
)


class TestParseHashes:
    def test_single_entry(self) -> None:
        assert parse_hash_table({"sha256": "a" * 64}) == (("sha256", "a" * 64),)

    def test_single_entry_malformed(self) -> None:
        assert parse_hash_table({"sha256": 123}) == ()

    def test_multiple_entries(self) -> None:
        result = parse_hash_table({"sha256": "a" * 64, "md5": "b" * 32})
        assert result == (("sha256", "a" * 64), ("md5", "b" * 32))

    def test_multiple_entries_skips_malformed(self) -> None:
        result = parse_hash_table({"sha256": "a" * 64, "md5": 123})
        assert result == (("sha256", "a" * 64),)

    def test_non_dict(self) -> None:
        assert parse_hash_table("sha256:abc") == ()

    def test_empty_dict(self) -> None:
        assert parse_hash_table({}) == ()

    def test_single_empty_digest_dropped(self) -> None:
        assert parse_hash_table({"sha256": ""}) == ()

    def test_empty_digest_falls_through_to_valid(self) -> None:
        assert parse_hash_table({"sha256": "", "sha512": "f" * 128}) == (
            ("sha512", "f" * 128),
        )

    def test_empty_digest_yields_no_sdist_check(self) -> None:
        assert select_artifact_hash(parse_hash_table({"sha256": ""})) is None

    def test_single_non_hex_digest_dropped(self) -> None:
        assert parse_hash_table({"sha256": "not-a-digest"}) == ()

    def test_hex_digest_split_by_whitespace_dropped(self) -> None:
        assert parse_hash_table({"sha256": "0123abcd\nbeef"}) == ()

    def test_non_hex_digest_falls_through_to_valid(self) -> None:
        assert parse_hash_table({"sha256": "deadbeef ", "sha512": "f" * 128}) == (
            ("sha512", "f" * 128),
        )

    def test_uppercase_digest_kept_lowercased(self) -> None:
        assert parse_hash_table({"sha256": "A" * 64}) == (("sha256", "a" * 64),)

    def test_bare_true_publishes_no_sidecar_hash(self) -> None:
        assert sidecar_hash(True) is None  # noqa: FBT003


class TestSelectArtifactHash:
    def test_prefers_sha256(self) -> None:
        hashes = (("sha512", "f" * 128), ("sha256", "a" * 64))
        assert select_artifact_hash(hashes) == ("sha256", "a" * 64)

    def test_falls_through_to_sha384(self) -> None:
        hashes = (("sha512", "f" * 128), ("sha384", "b" * 96))
        assert select_artifact_hash(hashes) == ("sha384", "b" * 96)

    def test_falls_through_to_sha512(self) -> None:
        assert select_artifact_hash((("sha512", "f" * 128),)) == (
            "sha512",
            "f" * 128,
        )

    def test_empty_returns_none(self) -> None:
        assert select_artifact_hash(()) is None

    def test_only_unacceptable_returns_none(self) -> None:
        assert select_artifact_hash((("md5", "d" * 32),)) is None

    def test_empty_digest_returns_none(self) -> None:
        assert select_artifact_hash((("sha256", ""),)) is None

    def test_empty_digest_falls_through_to_valid_algo(self) -> None:
        hashes = (("sha256", ""), ("sha512", "f" * 128))
        assert select_artifact_hash(hashes) == ("sha512", "f" * 128)
