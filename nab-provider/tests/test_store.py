"""Fetched listings, errors and source metadata kept by the provider."""

from __future__ import annotations

import pytest

from nab_provider.errors import SdistHashMismatchError
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


class TestListingsAndRequests:
    def test_listing_roundtrip(self) -> None:
        idx = InMemoryIndex()
        assert idx.get_listing("foo") is None
        wheels = [
            make_wheel(v, package="foo", has_metadata=False)
            for v in ("1.0", "2.0", "3.0")
        ]
        idx.store_listing("foo", wheels)
        assert idx.get_listing("foo") == wheels

    def test_offline_listing_miss_roundtrip(self) -> None:
        idx = InMemoryIndex()
        idx.store_listing("foo", [], offline_miss=True)
        assert idx.get_listing("foo") == []
        assert idx.is_offline_listing_miss("foo")

    def test_served_empty_listing_is_not_an_offline_miss(self) -> None:
        idx = InMemoryIndex()
        idx.store_listing("foo", [])
        assert not idx.is_offline_listing_miss("foo")

    def test_offline_listing_miss_fires_listing_pending(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("listing:foo")
        idx.store_listing("foo", [], offline_miss=True)
        assert event.is_set()

    def test_yank_status_is_visible_with_the_listing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Readers can identify an all-yanked page as soon as the files are visible."""
        idx = InMemoryIndex()
        readback: list[str] = []

        class _ReadOnPublish(dict[str, object]):
            """Classify the page the instant its listing slot becomes readable."""

            def __setitem__(self, key: str, value: object) -> None:
                super().__setitem__(key, value)
                assert idx.get_listing(key) == []
                readback.append(
                    "yanked" if idx.is_all_yanked_listing(key) else "absent"
                )

        monkeypatch.setattr(idx, "_listings", _ReadOnPublish())
        idx.store_listing("foo", [], all_yanked=True)

        assert readback == ["yanked"]
        assert idx.is_all_yanked_listing("foo")

    def test_pending_event_set_on_listing(self) -> None:
        idx = InMemoryIndex()
        event, existed = idx.get_or_create_pending("listing:foo")
        assert not existed
        assert not event.is_set()
        wheels = [make_wheel(package="foo", has_metadata=False)]
        idx.store_listing("foo", wheels)
        assert event.is_set()
        assert idx.get_listing("foo") == wheels

    def test_get_or_create_existing(self) -> None:
        idx = InMemoryIndex()
        first, existed1 = idx.get_or_create_pending("key")
        second, existed2 = idx.get_or_create_pending("key")
        assert not existed1
        assert existed2
        assert first is second

    def test_repeated_request_gets_a_set_event_after_publication(self) -> None:
        """A published key still reports as existing, and its event is set."""
        idx = InMemoryIndex()
        idx.get_or_create_pending("listing:foo")
        idx.store_listing("foo", [])

        event, existed = idx.get_or_create_pending("listing:foo")

        assert existed
        assert event.is_set()

    def test_listing_index_roundtrip(self) -> None:
        """Only an explicit package pin marks the recorded index as pinned."""
        idx = InMemoryIndex()
        assert idx.get_listing_index("foo") is None
        assert idx.pinned_listing_index("foo") is None

        idx.store_listing_index("foo", "primary", pinned=True)
        assert idx.get_listing_index("foo") == "primary"
        assert idx.pinned_listing_index("foo") == "primary"

        idx.store_listing_index("foo", "primary")
        assert idx.get_listing_index("foo") == "primary"
        assert idx.pinned_listing_index("foo") is None


class TestSourceArchives:
    def test_sdist_pyproject_slot_keeps_only_the_project_table(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_pyproject(
            "foo",
            "1.0",
            {
                "project": {"name": "foo"},
                "build-system": {"requires": ["setuptools"]},
                "tool": {"mypy": {"strict": True}},
            },
        )
        assert idx.get_sdist_pyproject("foo", "1.0") == {"project": {"name": "foo"}}

    def test_sdist_pyproject_with_no_project_table_reads_as_fetched(self) -> None:
        """An empty table keeps a fetched pyproject distinct from an absent one."""
        idx = InMemoryIndex()
        idx.store_sdist_pyproject("foo", "1.0", {"build-system": {"requires": []}})

        assert idx.get_sdist_pyproject("foo", "1.0") == {}
        assert idx.get_sdist_pyproject("bar", "1.0") is None

    @pytest.mark.parametrize("replacement", [b"new archive", b"", None])
    def test_latest_fetch_replaces_the_previous_archive_or_error(
        self, replacement: bytes | None
    ) -> None:
        index = InMemoryIndex()
        index.store_sdist_archive("pkg", "1", b"old archive")

        failure = OSError("download interrupted")
        index.store_sdist_archive_error("pkg", "1", failure)
        assert index.get_sdist_archive_error("pkg", "1") is failure
        assert index.get_sdist_archive("pkg", "1") is None

        index.store_sdist_archive("pkg", "1", replacement)
        assert index.get_sdist_archive_error("pkg", "1") is None
        assert index.get_sdist_archive("pkg", "1") == replacement

    def test_sdist_archive_pending_event_fires(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("sdist-archive:foo:1.0")
        assert not event.is_set()
        idx.store_sdist_archive("foo", "1.0", b"bytes")
        assert event.is_set()
        assert idx.get_sdist_archive("foo", "1.0") == b"bytes"

    def test_sdist_archive_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_archive("foo", "1.0", None)
        assert idx.get_sdist_archive("foo", "1.0") is None

    def test_store_sdist_archive_error_fires_pending(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("sdist-archive:foo:1.0")
        assert not event.is_set()
        err = SdistHashMismatchError("boom")
        idx.store_sdist_archive_error("foo", "1.0", err)
        assert event.is_set()
        assert idx.get_sdist_archive_error("foo", "1.0") is err
        assert idx.get_sdist_archive("foo", "1.0") is None

    def test_store_sdist_archive_error_without_pending(self) -> None:
        idx = InMemoryIndex()
        err = SdistHashMismatchError("boom")
        idx.store_sdist_archive_error("foo", "1.0", err)
        assert idx.get_sdist_archive_error("foo", "1.0") is err
