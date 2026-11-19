"""Declaration records are bound to one archive and one source-relative path."""

import json
from pathlib import Path

import pytest

from nab_index import dependency_input_cache
from nab_index.cache import OnDiskCache


@pytest.mark.parametrize(
    ("field", "value"),
    [("archive-sha256", "b" * 64), ("path", "other.toml")],
)
def test_record_identity_must_match_its_location(
    tmp_path: Path, field: str, value: str
) -> None:
    digest = "a" * 64
    bucket = tmp_path / dependency_input_cache.BUCKET
    dependency_input_cache.write_text(bucket, digest, "pyproject.toml", "[project]")
    (path,) = tmp_path.rglob("*.json")
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))

    assert dependency_input_cache.read_text(bucket, digest, "pyproject.toml") is None
    cache = OnDiskCache(tmp_path, "")
    assert (
        cache.read_cache_entry(path)
        == "dependency input does not match its cache location"
    )


def test_verify_accepts_a_record_at_its_archive_and_file_key(tmp_path: Path) -> None:
    bucket = tmp_path / dependency_input_cache.BUCKET
    digest = "a" * 64
    dependency_input_cache.write_text(bucket, digest, "docs/requirements.txt", "pytest")
    cache = OnDiskCache(tmp_path, "")
    (entry,) = cache.iter_cache_entries()
    assert cache.read_cache_entry(entry) is None
    assert (
        dependency_input_cache.read_text(bucket, digest, "docs/requirements.txt")
        == "pytest"
    )
