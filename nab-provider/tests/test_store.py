"""Fetched listings, errors and source metadata kept by the provider."""

import pytest

from nab_provider.store import InMemoryIndex, sdist_artifact_key
from nab_provider.testing import make_wheel


def test_replacing_a_listing_replaces_its_diagnostics() -> None:
    index = InMemoryIndex()
    index.store_listing("pkg", [], offline_miss=True)
    index.store_listing("pkg", [], unreadable_only=True, no_usable_file=True)
    assert not index.is_offline_listing_miss("pkg")
    assert index.is_unreadable_only_listing("pkg")
    assert index.is_no_usable_file_listing("pkg")

    index.store_listing("pkg", [], unreachable_only=True, all_yanked=True)
    assert not index.is_unreadable_only_listing("pkg")
    assert not index.is_no_usable_file_listing("pkg")
    assert index.is_unreachable_only_listing("pkg")
    assert index.is_all_yanked_listing("pkg")

    wheel = make_wheel()
    index.store_listing("pkg", [wheel])
    assert index.get_listing("pkg") == [wheel]
    assert not index.is_unreachable_only_listing("pkg")
    assert not index.is_all_yanked_listing("pkg")


def test_replacing_a_listing_with_an_error_and_recovering() -> None:
    index = InMemoryIndex()
    wheel = make_wheel()
    index.store_listing("pkg", [wheel], zip_sdists=frozenset({"2"}))
    files = index.get_listing("pkg")
    assert files is not None
    context = index.consistent_metadata_context("pkg", files)
    assert context is not None

    failure = OSError("index unavailable")
    index.store_listing_error("pkg", failure)
    assert index.get_listing_error("pkg") is failure
    assert index.get_listing("pkg") is None
    assert not index.zip_sdist_versions("pkg")
    assert index.consistent_metadata_context("pkg", files) is None

    index.store_listing("pkg", [wheel])
    assert index.get_listing_error("pkg") is None
    assert index.get_listing("pkg") == [wheel]


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
