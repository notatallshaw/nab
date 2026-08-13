"""Tests for hash-digest lowercasing in nab_index's Simple API JSON parser."""

from __future__ import annotations

from nab_index.client import _parse_files


def _file(hashes: dict[str, str]) -> dict[str, object]:
    return {
        "filename": "foo-1.0-py3-none-any.whl",
        "url": "https://example.com/foo/foo-1.0-py3-none-any.whl",
        "hashes": hashes,
    }


def test_single_hash_digest_lowercased() -> None:
    data = {"files": [_file({"sha256": "A" * 64})]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert files[0].hashes == (("sha256", "a" * 64),)


def test_multi_hash_digests_lowercased() -> None:
    data = {"files": [_file({"sha256": "A" * 64, "sha512": "B" * 128})]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert dict(files[0].hashes) == {"sha256": "a" * 64, "sha512": "b" * 128}


def test_single_hash_algorithm_lowercased() -> None:
    data = {"files": [_file({"SHA256": "a" * 64})]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert files[0].hashes == (("sha256", "a" * 64),)


def test_multi_hash_algorithms_lowercased() -> None:
    data = {"files": [_file({"SHA256": "a" * 64, "Sha512": "b" * 128})]}
    files = _parse_files(data, "https://example.com/", "foo")
    assert dict(files[0].hashes) == {"sha256": "a" * 64, "sha512": "b" * 128}
