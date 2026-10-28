"""Artifact identity across equivalent release versions."""

import pytest

from nab_provider.store import InMemoryIndex, sdist_artifact_key


@pytest.mark.parametrize(
    ("written_version", "read_version"),
    [("1", "1.0"), ("1.0.0", "1"), ("1.0rc1", "1rc1"), ("1.0+abc", "1+abc")],
)
def test_sdist_slots_reuse_equivalent_versions(
    written_version: str, read_version: str
) -> None:
    index = InMemoryIndex()
    url = "https://example.org/pkg.tar.gz"
    written_key = sdist_artifact_key(written_version, url)
    metadata = "Metadata-Version: 2.2\nName: pkg\nVersion: 1\n"
    project = {"project": {"dependencies": ["dep"]}}

    index.store_sdist_metadata("pkg", written_key, metadata)
    index.store_sdist_pyproject("pkg", written_key, project)
    index.store_sdist_archive("pkg", written_key, b"archive")

    read_key = sdist_artifact_key(read_version, url)
    assert index.get_metadata("pkg", read_key) == metadata
    assert index.get_sdist_pyproject("pkg", read_key) == project
    assert index.get_sdist_archive("pkg", read_key) == b"archive"


@pytest.mark.parametrize(
    ("version", "url"),
    [
        ("2", "https://example.org/pkg.tar.gz"),
        ("1+abc", "https://example.org/pkg.tar.gz"),
        ("1", "https://example.org/other/pkg.tar.gz"),
    ],
)
def test_sdist_slots_keep_distinct_artifacts(version: str, url: str) -> None:
    index = InMemoryIndex()
    index.store_sdist_archive(
        "pkg", sdist_artifact_key("1", "https://example.org/pkg.tar.gz"), b"archive"
    )

    assert index.get_sdist_archive("pkg", sdist_artifact_key(version, url)) is None
