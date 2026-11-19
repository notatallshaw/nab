"""Release metadata, wheel sidecars and range reads in the provider store."""

from __future__ import annotations

import pytest

from nab_provider.errors import (
    HttpError,
    MalformedSimpleResponseError,
    MetadataHashMismatchError,
    SdistHashMismatchError,
)
from nab_provider.metadata import parse_metadata
from nab_provider.records import RangeOutcome
from nab_provider.store import InMemoryIndex
from nab_provider.testing import make_metadata

_RANGE_URL_A = "https://files.example.org/packages/widget-1.0-cp312-linux.whl"
_RANGE_URL_B = "https://files.example.org/packages/widget-1.0-cp312-macos.whl"


class TestStoredMetadata:
    def test_metadata_roundtrip(self) -> None:
        idx = InMemoryIndex()
        assert idx.get_metadata("foo", "1.0") is None
        assert not idx.has_metadata("foo", "1.0")
        idx.store_metadata("foo", "1.0", "Metadata-Version: 2.1")
        assert idx.get_metadata("foo", "1.0") == "Metadata-Version: 2.1"
        assert idx.has_metadata("foo", "1.0")

    def test_metadata_slot_keeps_only_the_header_block(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata(
            "foo", "1.0", "Name: foo\nVersion: 1.0\n\nA long description.\n"
        )
        assert idx.get_metadata("foo", "1.0") == "Name: foo\nVersion: 1.0\n\n"

    def test_sdist_metadata_slot_keeps_only_the_header_block(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_metadata(
            "foo", "1.0", "Name: foo\nVersion: 1.0\n\nA long description.\n"
        )
        assert idx.get_metadata("foo", "1.0") == "Name: foo\nVersion: 1.0\n\n"
        assert idx.metadata_from_sdist("foo", "1.0")

    def test_sdist_metadata_slot_drops_the_folded_description(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_metadata(
            "foo",
            "1.0",
            "Name: foo\nDescription: Foo\n        A long description.\nVersion: 1.0\n",
        )
        assert idx.get_metadata("foo", "1.0") == "Name: foo\nVersion: 1.0\n"

    def test_equal_metadata_texts_in_a_release_share_one_str(self) -> None:
        """Wheels of one version whose header blocks match hold one shared str."""
        idx = InMemoryIndex()
        linux = "Name: foo\nVersion: 1.0\nRequires-Dist: linux-only\n\n"
        macos = "Name: foo\nVersion: 1.0\nRequires-Dist: macos-only\n\n"
        windows = "Name: foo\nVersion: 1.0\nRequires-Dist: windows-only\n\n"
        wheels = [
            ("https://example.com/foo-linux.whl", linux),
            ("https://example.com/foo-manylinux.whl", linux),
            ("https://example.com/foo-macos.whl", macos),
            ("https://example.com/foo-macos-arm.whl", macos),
            ("https://example.com/foo-windows.whl", windows),
        ]
        for url, header in wheels:
            idx.store_metadata(
                "foo", "1.0", header + "A long description.\n", metadata_url=url
            )

        held = [idx.get_metadata("foo", "1.0", metadata_url=url) for url, _ in wheels]

        assert held == [linux, linux, macos, macos, windows]

        assert held[0] is held[1]
        assert held[2] is held[3]
        assert held[0] is not held[2]

    def test_store_metadata_none(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", None)
        assert idx.has_metadata("foo", "1.0")

    def test_offline_metadata_miss_roundtrip(self) -> None:
        idx = InMemoryIndex()
        url = "https://files.example.com/foo-1.0.whl"
        assert not idx.is_offline_metadata_miss("foo", "1.0", url)
        idx.record_offline_metadata_miss("foo", "1.0", url)
        assert idx.is_offline_metadata_miss("foo", "1.0", url)
        assert not idx.is_offline_metadata_miss("foo", "2.0", url)

    def test_an_offline_metadata_miss_applies_only_to_its_artifact_url(self) -> None:
        idx = InMemoryIndex()
        idx.record_offline_metadata_miss(
            "foo", "1.0", "https://files.example.com/foo-1.0.whl"
        )
        assert not idx.is_offline_metadata_miss(
            "foo", "1.0", "https://files.example.com/foo-1.0.tar.gz"
        )
        assert not idx.is_offline_metadata_miss("foo", "1.0", None)

    def test_empty_metadata_slot_is_not_an_offline_miss(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", None)
        idx.store_sdist_metadata("foo", "1.0", None)
        assert not idx.is_offline_metadata_miss("foo", "1.0", None)

    def test_only_the_first_caller_claims_the_offline_warning(self) -> None:
        idx = InMemoryIndex()
        assert idx.claim_offline_metadata_warning("foo")
        assert not idx.claim_offline_metadata_warning("foo")
        assert idx.claim_offline_metadata_warning("bar")

    def test_sidecar_retry_replaces_its_own_result(self) -> None:
        index = InMemoryIndex()
        linux = "https://index.example/demo-linux.whl.metadata"
        windows = "https://index.example/demo-windows.whl.metadata"
        index.store_sdist_metadata("demo", "1", "source metadata")
        index.store_metadata("demo", "1", "Linux", metadata_url=linux)
        index.store_metadata("demo", "1", "Windows", metadata_url=windows)

        failure = OSError("read interrupted")
        index.store_metadata_error("demo", "1", failure, linux)
        assert index.has_metadata("demo", "1", linux)
        assert index.get_metadata_error("demo", "1", linux) is failure
        assert index.get_metadata("demo", "1") == "source metadata"
        assert index.get_metadata("demo", "1", linux) is None
        assert index.get_metadata("demo", "1", windows) == "Windows"

        index.store_metadata("demo", "1", "new Linux", metadata_url=linux)
        assert index.get_metadata_error("demo", "1", linux) is None
        assert index.get_metadata("demo", "1", linux) == "new Linux"

    def test_sdist_failure_retires_derived_metadata(self) -> None:
        index = InMemoryIndex()
        text = make_metadata("demo", "1")
        index.store_sdist_metadata("demo", "1", text)
        index.store_resolved_sdist_metadata("demo", "1", parse_metadata(text))

        failure = OSError("read interrupted")
        index.store_sdist_metadata_error("demo", "1", failure)
        assert index.get_metadata_with_origin("demo", "1") == (None, False)
        assert index.get_resolved_sdist_metadata("demo", "1") is None

        index.store_sdist_metadata("demo", "1", text)
        assert index.get_metadata_error("demo", "1") is None
        assert index.metadata_from_sdist("demo", "1")

    def test_metadata_error_roundtrip(self) -> None:
        idx = InMemoryIndex()
        assert idx.get_metadata_error("foo", "1.0") is None
        error = MetadataHashMismatchError("metadata sha256 mismatch")
        idx.store_metadata_error("foo", "1.0", error)
        assert idx.get_metadata_error("foo", "1.0") is error
        assert idx.get_metadata("foo", "1.0") is None

    def test_store_metadata_error_fires_metadata_pending(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("metadata:foo:1.0:https://f/a.metadata")
        idx.store_metadata_error(
            "foo", "1.0", MetadataHashMismatchError("bad"), "https://f/a.metadata"
        )
        assert event.is_set()

    def test_store_metadata_error_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata_error("foo", "1.0", MetadataHashMismatchError("bad"))
        assert idx.get_metadata_error("foo", "1.0") is not None
        assert idx.get_metadata("foo", "1.0") is None

    def test_pending_event_set_on_metadata(self) -> None:
        idx = InMemoryIndex()
        event, existed = idx.get_or_create_pending("metadata:foo:1.0:https://f/a.md")
        assert not existed
        idx.store_metadata("foo", "1.0", "text", metadata_url="https://f/a.md")
        assert event.is_set()
        assert idx.get_metadata_with_origin("foo", "1.0", "https://f/a.md") == (
            "text",
            False,
        )

    def test_sidecar_slot_answers_only_for_its_own_artifact(self) -> None:
        linux_url = "https://f/linux.whl.metadata"
        win_url = "https://f/win.whl.metadata"
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", "linux", metadata_url=linux_url)
        assert idx.has_metadata("foo", "1.0", linux_url)
        assert not idx.has_metadata("foo", "1.0", win_url)

    def test_a_positional_metadata_url_is_refused(self) -> None:
        """A keyword-only URL prevents accidentally storing it as metadata text."""
        url = "https://f/linux.whl.metadata"
        idx = InMemoryIndex()
        with pytest.raises(TypeError):
            idx.store_metadata("foo", "1.0", url, "linux")  # type: ignore[call-arg]
        assert not idx.has_metadata("foo", "1.0", url)

    def test_sibling_sidecars_keep_their_own_text(self) -> None:
        """Two wheels of one version can declare different dependencies."""
        linux_url = "https://f/linux.whl.metadata"
        win_url = "https://f/win.whl.metadata"
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", "linux", metadata_url=linux_url)
        idx.store_metadata("foo", "1.0", "win", metadata_url=win_url)
        assert idx.get_metadata("foo", "1.0", linux_url) == "linux"
        assert idx.get_metadata("foo", "1.0", win_url) == "win"
        assert idx.get_metadata("foo", "1.0") is None

    def test_an_injected_override_answers_for_any_artifact(self) -> None:
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", "override\n")
        assert idx.has_metadata("foo", "1.0", "https://f/win.whl.metadata")
        assert idx.get_metadata_with_origin(
            "foo", "1.0", "https://f/win.whl.metadata"
        ) == ("override\n", False)

    def test_sdist_pkg_info_does_not_answer_for_an_unfetched_sidecar(self) -> None:
        """A wheel that declares its own dependencies must fetch them.

        The sdist is a fallback for an artifact with no text of its own, so
        lending its PKG-INFO to a sidecar nobody has fetched yet would give
        the wheel the sdist's dependencies instead of the wheel's.
        """
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        assert not idx.has_metadata("foo", "1.0", "https://f/win.whl.metadata")
        assert idx.get_metadata_with_origin(
            "foo", "1.0", "https://f/win.whl.metadata"
        ) == (None, False)
        assert idx.get_metadata_with_origin("foo", "1.0") == ("PKG-INFO\n", True)

    def test_a_sidecar_that_was_not_served_falls_back_to_the_sdist(self) -> None:
        """An empty sidecar slot does not shadow the version-level text."""
        url = "https://f/a.whl.metadata"
        idx = InMemoryIndex()
        idx.store_metadata("foo", "1.0", None, metadata_url=url)
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        assert idx.has_metadata("foo", "1.0", url)
        assert idx.get_metadata_with_origin("foo", "1.0", url) == ("PKG-INFO\n", True)

    def test_has_metadata_is_false_on_an_empty_slot(self) -> None:
        idx = InMemoryIndex()
        assert not idx.has_metadata("foo", "1.0", "https://f/a.whl.metadata")

    def test_a_sidecars_integrity_error_answers_only_for_that_sidecar(self) -> None:
        linux_url = "https://f/linux.whl.metadata"
        win_url = "https://f/win.whl.metadata"
        idx = InMemoryIndex()
        error = MetadataHashMismatchError("metadata sha256 mismatch")
        idx.store_metadata_error("foo", "1.0", error, linux_url)
        assert idx.get_metadata_error("foo", "1.0", linux_url) is error
        assert idx.get_metadata_error("foo", "1.0", win_url) is None
        assert idx.get_metadata_error("foo", "1.0") is None

    def test_a_version_level_integrity_error_answers_for_any_artifact(self) -> None:
        idx = InMemoryIndex()
        error = SdistHashMismatchError("boom")
        idx.store_sdist_metadata_error("foo", "1.0", error)
        assert idx.get_metadata_error("foo", "1.0", "https://f/a.whl.metadata") is error

    def test_store_sdist_metadata_fires_sdist_pending(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("sdist:foo:1.0")
        assert not event.is_set()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        assert event.is_set()
        assert idx.get_metadata("foo", "1.0") == "PKG-INFO\n"

    def test_store_sdist_metadata_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", None)
        assert idx.has_metadata("foo", "1.0")
        assert idx.get_metadata("foo", "1.0") is None

    def test_store_sdist_metadata_error_fires_sdist_pending(self) -> None:
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("sdist:foo:1.0")
        assert not event.is_set()
        err = SdistHashMismatchError("boom")
        idx.store_sdist_metadata_error("foo", "1.0", err)
        assert event.is_set()
        assert idx.get_metadata_error("foo", "1.0") is err
        assert idx.get_metadata("foo", "1.0") is None

    def test_store_sdist_metadata_error_without_pending(self) -> None:
        idx = InMemoryIndex()
        err = SdistHashMismatchError("boom")
        idx.store_sdist_metadata_error("foo", "1.0", err)
        assert idx.get_metadata_error("foo", "1.0") is err

    def test_metadata_from_sdist_tracks_last_write(self) -> None:
        idx = InMemoryIndex()
        assert not idx.metadata_from_sdist("foo", "1.0")
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        assert idx.metadata_from_sdist("foo", "1.0")
        idx.store_metadata("foo", "1.0", "METADATA\n")
        assert not idx.metadata_from_sdist("foo", "1.0")

    def test_metadata_none_keeps_stored_sdist_pkg_info(self) -> None:
        """A failed sidecar fetch must not erase stored sdist PKG-INFO."""
        idx = InMemoryIndex()
        event, _ = idx.get_or_create_pending("metadata:foo:1.0:https://f/a.md")
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO\n")
        idx.store_metadata("foo", "1.0", None, metadata_url="https://f/a.md")
        assert event.is_set()
        assert idx.get_metadata("foo", "1.0") == "PKG-INFO\n"
        assert idx.metadata_from_sdist("foo", "1.0")
        # The sidecar that resolved to nothing reads the kept text.
        assert idx.get_metadata_with_origin("foo", "1.0", "https://f/a.md") == (
            "PKG-INFO\n",
            True,
        )

    def test_metadata_none_after_sdist_none_stays_none(self) -> None:
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", None)
        idx.store_metadata("foo", "1.0", None)
        assert idx.get_metadata("foo", "1.0") is None
        assert not idx.metadata_from_sdist("foo", "1.0")

    def test_parsed_metadata_roundtrip(self) -> None:
        """``store_parsed_metadata`` and ``get_parsed_metadata`` round-trip."""
        idx = InMemoryIndex()
        sentinel = object()
        assert idx.get_parsed_metadata("foo", "1.0", "METADATA") is None
        idx.store_parsed_metadata("foo", "1.0", sentinel, "METADATA")
        assert idx.get_parsed_metadata("foo", "1.0", "METADATA") is sentinel

    def test_parsed_metadata_per_version(self) -> None:
        """Each ``(package, version)`` pair has its own slot."""
        idx = InMemoryIndex()
        idx.store_parsed_metadata("foo", "1.0", "v1", "TEXT-1")
        idx.store_parsed_metadata("foo", "2.0", "v2", "TEXT-2")
        assert idx.get_parsed_metadata("foo", "1.0", "TEXT-1") == "v1"
        assert idx.get_parsed_metadata("foo", "2.0", "TEXT-2") == "v2"

    def test_parsed_metadata_keeps_every_text_of_a_version(self) -> None:
        """Keep a separate parse for each metadata text at a version."""
        idx = InMemoryIndex()
        idx.store_parsed_metadata("foo", "1.0", "linux-parse", "LINUX-METADATA")
        idx.store_parsed_metadata("foo", "1.0", "win-parse", "WIN-METADATA")
        assert idx.get_parsed_metadata("foo", "1.0", "LINUX-METADATA") == "linux-parse"
        assert idx.get_parsed_metadata("foo", "1.0", "WIN-METADATA") == "win-parse"

    def test_parsed_metadata_answers_only_for_its_own_text(self) -> None:
        """Do not return a parse for different metadata text."""
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO")
        idx.store_parsed_metadata("foo", "1.0", "sdist-parse", "PKG-INFO")
        idx.store_metadata("foo", "1.0", "METADATA")
        assert idx.get_parsed_metadata("foo", "1.0", "METADATA") is None

    def test_get_metadata_with_origin_reports_the_last_write(self) -> None:
        """Text and origin come back together, from whichever kind wrote last."""
        idx = InMemoryIndex()
        assert idx.get_metadata_with_origin("foo", "1.0") == (None, False)
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO")
        assert idx.get_metadata_with_origin("foo", "1.0") == ("PKG-INFO", True)
        idx.store_metadata("foo", "1.0", "METADATA")
        assert idx.get_metadata_with_origin("foo", "1.0") == ("METADATA", False)

    def test_resolved_sdist_metadata_roundtrip(self) -> None:
        """``store_resolved_sdist_metadata`` round-trips through ``get``."""
        idx = InMemoryIndex()
        metadata = parse_metadata(make_metadata("foo", "1.0"))
        assert idx.get_resolved_sdist_metadata("foo", "1.0") is None
        idx.store_resolved_sdist_metadata("foo", "1.0", metadata)
        assert idx.get_resolved_sdist_metadata("foo", "1.0") is metadata

    def test_metadata_rewrite_drops_the_reconciled_sdist_view(self) -> None:
        """Replacing the raw text drops the post-reconciliation entry too.

        The reconciled record is the PKG-INFO in the slot plus the bundled
        pyproject.toml or a PEP 517 build, so it cannot outlive that text.
        """
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO")
        metadata = parse_metadata(make_metadata("foo", "1.0"))
        idx.store_resolved_sdist_metadata("foo", "1.0", metadata)
        idx.store_metadata("foo", "1.0", "METADATA")
        assert idx.get_resolved_sdist_metadata("foo", "1.0") is None

    def test_restoring_the_same_text_keeps_the_reconciled_sdist_view(self) -> None:
        """Re-storing identical text is not a rewrite, so the entry survives."""
        idx = InMemoryIndex()
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO")
        metadata = parse_metadata(make_metadata("foo", "1.0"))
        idx.store_resolved_sdist_metadata("foo", "1.0", metadata)
        idx.store_sdist_metadata("foo", "1.0", "PKG-INFO")
        assert idx.get_resolved_sdist_metadata("foo", "1.0") is metadata


class TestRangeMetadata:
    def test_later_range_read_replaces_an_error_or_text(self) -> None:
        index = InMemoryIndex()
        index.store_range_error("widget", "1", _RANGE_URL_A, OSError("try again"))
        index.store_range_metadata("widget", "1", _RANGE_URL_A, "fetched")
        assert index.get_metadata_error("widget", "1", _RANGE_URL_A) is None
        assert index.get_metadata("widget", "1", _RANGE_URL_A) == "fetched"

        index.store_range_absent("widget", "1", _RANGE_URL_A)
        assert not index.has_metadata("widget", "1", _RANGE_URL_A)
        assert index.get_metadata("widget", "1", _RANGE_URL_A) is None

    def test_store_range_metadata_fires_pending(self) -> None:
        idx = InMemoryIndex()
        key = f"range:widget:1.0:{_RANGE_URL_A}"
        event, _ = idx.get_or_create_pending(key)
        idx.store_range_metadata("widget", "1.0", _RANGE_URL_A, "META")
        assert event.is_set()
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) == "META"
        assert not idx.metadata_from_sdist("widget", "1.0")

    def test_store_range_metadata_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_range_metadata("widget", "1.0", _RANGE_URL_A, "META")
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) == "META"

    def test_sibling_wheels_keep_independent_range_slots(self) -> None:
        idx = InMemoryIndex()
        idx.store_range_metadata("widget", "1.0", _RANGE_URL_A, "META_A")
        idx.store_range_metadata("widget", "1.0", _RANGE_URL_B, "META_B")
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) == "META_A"
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_B) == "META_B"

    def test_store_range_absent_fires_pending_without_slot(self) -> None:
        idx = InMemoryIndex()
        key = f"range:widget:1.0:{_RANGE_URL_A}"
        event, _ = idx.get_or_create_pending(key)
        idx.store_range_absent("widget", "1.0", _RANGE_URL_A)
        assert event.is_set()
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) is None

    def test_store_range_absent_without_pending(self) -> None:
        idx = InMemoryIndex()
        idx.store_range_absent("widget", "1.0", _RANGE_URL_A)
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) is None

    def test_store_range_error_fires_pending_and_records(self) -> None:
        idx = InMemoryIndex()
        key = f"range:widget:1.0:{_RANGE_URL_A}"
        event, _ = idx.get_or_create_pending(key)
        error = MalformedSimpleResponseError("bad")
        idx.store_range_error("widget", "1.0", _RANGE_URL_A, error)
        assert event.is_set()
        assert idx.get_metadata_error("widget", "1.0", _RANGE_URL_A) is error
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_A) is None

    def test_store_range_error_without_pending(self) -> None:
        idx = InMemoryIndex()
        error = HttpError("boom")
        idx.store_range_error("widget", "1.0", _RANGE_URL_A, error)
        assert idx.get_metadata_error("widget", "1.0", _RANGE_URL_A) is error

    def test_sibling_range_error_spares_the_other_wheel(self) -> None:
        idx = InMemoryIndex()
        error = HttpError("boom")
        idx.store_range_error("widget", "1.0", _RANGE_URL_A, error)
        idx.store_range_metadata("widget", "1.0", _RANGE_URL_B, "META_B")
        assert idx.get_metadata_error("widget", "1.0", _RANGE_URL_A) is error
        assert idx.get_metadata_error("widget", "1.0", _RANGE_URL_B) is None
        assert idx.get_metadata("widget", "1.0", _RANGE_URL_B) == "META_B"

    def test_range_outcome_roundtrip(self) -> None:
        idx = InMemoryIndex()
        assert idx.get_range_outcome("widget", "1.0", _RANGE_URL_A) is None
        idx.store_range_outcome("widget", "1.0", _RANGE_URL_A, RangeOutcome.PARTIAL)
        assert (
            idx.get_range_outcome("widget", "1.0", _RANGE_URL_A) is RangeOutcome.PARTIAL
        )
        # Keyed like the read itself: a sibling wheel's slot stays empty.
        assert idx.get_range_outcome("widget", "1.0", _RANGE_URL_B) is None
