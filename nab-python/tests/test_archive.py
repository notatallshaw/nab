"""Tests for direct-URL archive sources (``[[tool.nab.archive-sources]]``).

The download itself goes through the fetch coordinator, so these tests
pre-seed the in-memory index with the archive bytes (or an error) rather
than hitting the network, and exercise the extract-then-materialise path
on real ``.tar.gz`` bytes.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from nab_index._subdir import subdirectory_escapes
from nab_index.archive import ArchiveRequest, ArchiveRequestError
from nab_index.client import SdistHashMismatchError, extract_sdist_archive
from nab_python.download import (
    DownloadError,
    _reject_colliding_targets,
    iter_artifacts,
)
from nab_python.fetch import InMemoryIndex
from nab_python.lockfile import ArchivePin, LockInput
from nab_python.provider import (
    ArchiveSource,
    BuildPolicy,
    Provider,
    UnsupportedSdistError,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_sdist(name: str, version: str, pyproject: str) -> bytes:
    """Return ``.tar.gz`` bytes for a one-file sdist rooted at name-version."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = pyproject.encode("utf-8")
        info = tarfile.TarInfo(f"{name}-{version}/pyproject.toml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_flat_sdist(pyproject: str) -> bytes:
    """Return ``.tar.gz`` bytes with pyproject.toml at the top level (no root dir)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = pyproject.encode("utf-8")
        info = tarfile.TarInfo("pyproject.toml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


_PYPROJECT = '[project]\nname = "foo"\nversion = "1.0.0"\ndependencies = ["bar>=1"]\n'


def _provider(archive_sources: list[ArchiveSource], cache_dir: Path | None) -> Provider:
    coordinator = MagicMock()
    coordinator.index = InMemoryIndex()
    return Provider(
        coordinator,
        archive_sources=archive_sources,
        archive_cache_dir=cache_dir,
        build_policy=BuildPolicy.NEVER,
    )


# Extraction requires the tar data filter (PEP 706), so skip the paths that
# actually extract on a Python that lacks it (before 3.10.12 / 3.11.4 / 3.12).
requires_data_filter = pytest.mark.skipif(
    not hasattr(tarfile, "data_filter"),
    reason="sdist extraction requires the tar data filter (PEP 706)",
)


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

    def test_parent_subdirectory_rejected(self) -> None:
        with pytest.raises(ArchiveRequestError, match="unsafe archive subdirectory"):
            ArchiveRequest.parse(
                "https://ex.com/foo.tar.gz#sha256=abc&subdirectory=../../etc"
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


class TestSubdirectoryEscapes:
    def test_posix_backslash_parent_escapes(self) -> None:
        assert subdirectory_escapes("c\\d/../..") is True

    def test_backslash_segment_stays_contained(self) -> None:
        assert subdirectory_escapes("sub\\deeper") is False


class TestArchiveIndexing:
    def test_duplicate_archive_source_rejected(self) -> None:
        digest = "a" * 64
        url = f"https://ex.com/foo-1.0.tar.gz#sha256={digest}"
        with pytest.raises(ValueError, match="duplicate source"):
            _provider(
                [
                    ArchiveSource(name="Foo-Bar", url=url),
                    ArchiveSource(name="foo_bar", url=url),
                ],
                None,
            )

    def test_valid_source_is_registered(self) -> None:
        source = ArchiveSource(
            name="Foo-Bar", url=f"https://ex.com/foo-1.0.tar.gz#sha256={'a' * 64}"
        )
        provider = _provider([source], None)
        assert provider.archive_source_for("foo_bar") is source

    def test_hashless_url_rejected_at_index(self) -> None:
        # config parse also rejects this, but a directly-built Provider must
        # not slip through to an IndexError at materialisation.
        with pytest.raises(ValueError, match="no hash"):
            _provider(
                [ArchiveSource(name="foo", url="https://ex.com/foo-1.0.tar.gz")],
                None,
            )

    def test_archive_collides_with_local_source(self, tmp_path: Path) -> None:
        from nab_python.provider import LocalSource

        digest = "a" * 64
        coordinator = MagicMock()
        coordinator.index = InMemoryIndex()
        with pytest.raises(ValueError, match="duplicate source"):
            Provider(
                coordinator,
                local_sources=[LocalSource(name="foo", path=str(tmp_path))],
                archive_sources=[
                    ArchiveSource(
                        name="foo",
                        url=f"https://ex.com/foo-1.0.tar.gz#sha256={digest}",
                    ),
                ],
                build_policy=BuildPolicy.NEVER,
            )


class TestArchiveMaterialize:
    # The download-and-verify guards need no tar filter and run on every runner;
    # only the tests that actually extract carry @requires_data_filter.

    @requires_data_filter
    def test_resolves_and_seeds_single_version(self, tmp_path: Path) -> None:
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        provider = _provider([source], tmp_path / "arch")
        provider.coordinator.index.store_sdist_archive("foo", digest, data)

        versions = provider.fetch_versions("foo")

        assert len(versions) == 1
        assert str(versions[0][0]) == "1.0.0"

    def test_hash_mismatch_is_hard_error(self, tmp_path: Path) -> None:
        digest = "a" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        provider = _provider([source], tmp_path / "arch")
        provider.coordinator.index.store_sdist_archive_error(
            "foo", digest, SdistHashMismatchError("sha256 mismatch")
        )
        # A tampered archive fails the resolve loudly; it is not swallowed
        # as an UnsupportedSdistError the look-ahead would treat as a skip.
        with pytest.raises(SdistHashMismatchError):
            provider.fetch_versions("foo")

    def test_missing_cache_dir_raises(self) -> None:
        digest = "a" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        provider = _provider([source], None)
        with pytest.raises(UnsupportedSdistError, match="no.*archive_cache_dir"):
            provider.fetch_versions("foo")

    def test_download_failure_raises(self, tmp_path: Path) -> None:
        digest = "a" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        provider = _provider([source], tmp_path / "arch")
        # No bytes stored and no error recorded: the fetch produced nothing.
        with pytest.raises(UnsupportedSdistError, match="download.*failed"):
            provider.fetch_versions("foo")

    def test_local_source_hash_verified(self, tmp_path: Path) -> None:
        # A file:// index reads bytes without verifying, so materialisation
        # re-checks the hash: tampered bytes present (and no recorded error)
        # still fail the resolve loudly.
        digest = "a" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        provider = _provider([source], tmp_path / "arch")
        provider.coordinator.index.store_sdist_archive("foo", digest, b"tampered")
        with pytest.raises(SdistHashMismatchError):
            provider.fetch_versions("foo")

    def test_unextractable_archive_raises(self, tmp_path: Path) -> None:
        # Bytes whose hash matches (so verification passes) but which are not a
        # valid tarball: the failure is extraction, not the hash check.
        data = b"not a tarball"
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        provider = _provider([source], tmp_path / "arch")
        provider.coordinator.index.store_sdist_archive("foo", digest, data)
        with pytest.raises(UnsupportedSdistError, match="could not be extracted"):
            provider.fetch_versions("foo")

    @requires_data_filter
    def test_reextraction_replaces_stale_partial_tree(self, tmp_path: Path) -> None:
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        # A partial dir from a crashed run (no completion marker) is discarded.
        stale = tmp_path / "arch" / digest
        stale.mkdir(parents=True)
        (stale / "stale.txt").write_text("old", encoding="utf-8")

        provider = _provider([source], tmp_path / "arch")
        provider.coordinator.index.store_sdist_archive("foo", digest, data)

        versions = provider.fetch_versions("foo")
        assert str(versions[0][0]) == "1.0.0"
        assert not (stale / "stale.txt").exists()

    @requires_data_filter
    def test_second_resolve_reuses_extracted_tree(self, tmp_path: Path) -> None:
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        first = _provider([source], cache)
        first.coordinator.index.store_sdist_archive("foo", digest, data)
        first.fetch_versions("foo")

        # A sentinel dropped into the extracted tree survives a later resolve,
        # proving the tree is reused rather than wiped and re-extracted.
        sentinel = cache / digest / "SENTINEL"
        sentinel.write_text("keep", encoding="utf-8")

        second = _provider([source], cache)
        second.coordinator.index.store_sdist_archive("foo", digest, data)
        versions = second.fetch_versions("foo")

        assert str(versions[0][0]) == "1.0.0"
        assert sentinel.exists()

    @requires_data_filter
    def test_flat_archive_without_root_dir(self, tmp_path: Path) -> None:
        # A flat archive (pyproject.toml at the top level) extracts to the
        # target dir itself, with no single root subdirectory.
        data = _make_flat_sdist(_PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        provider = _provider([source], tmp_path / "arch")
        provider.coordinator.index.store_sdist_archive("foo", digest, data)

        versions = provider.fetch_versions("foo")
        assert str(versions[0][0]) == "1.0.0"

    @requires_data_filter
    def test_flat_archive_reuse_through_symlinked_cache(self, tmp_path: Path) -> None:
        # When the cache dir resolves to a different path (a symlink component),
        # a flat archive's reuse marker must still record "no root subdir", or the
        # second resolve looks under a nonexistent <digest>/<digest> directory.
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        cache = link / "arch"
        data = _make_flat_sdist(_PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )

        first = _provider([source], cache)
        first.coordinator.index.store_sdist_archive("foo", digest, data)
        first.fetch_versions("foo")

        second = _provider([source], cache)
        second.coordinator.index.store_sdist_archive("foo", digest, data)
        versions = second.fetch_versions("foo")
        assert str(versions[0][0]) == "1.0.0"


class TestArchiveDownload:
    def test_archive_pin_yields_download_entry(self) -> None:
        pin = ArchivePin(
            name="foo",
            version="1.0",
            url="https://ex.com/foo-1.0.tar.gz",
            hashes=(("sha256", "e" * 64),),
        )
        (entry,) = list(iter_artifacts(LockInput(pins={"foo": pin})))
        assert entry.url == "https://ex.com/foo-1.0.tar.gz"
        assert entry.filename == "foo-1.0.tar.gz"
        assert entry.hash_algo == "sha256"
        assert entry.digest == "e" * 64
        assert entry.local_path is None

    def test_primary_digest_no_acceptable_hash_raises(self) -> None:
        pin = ArchivePin(name="foo", version="1.0", url="u", hashes=(("md5", "x"),))
        with pytest.raises(ValueError, match="no acceptable hash"):
            _ = pin.primary_digest

    def test_colliding_basenames_rejected(self) -> None:
        # Two archives from different repos sharing a URL basename would clobber
        # each other in the flat output dir, so the collision is refused.
        pins = {
            "foo": ArchivePin(
                name="foo",
                version="1.0",
                url="https://a.example.com/dist/v1.0.0.tar.gz",
                hashes=(("sha256", "a" * 64),),
            ),
            "bar": ArchivePin(
                name="bar",
                version="2.0",
                url="https://b.example.com/dist/v1.0.0.tar.gz",
                hashes=(("sha256", "b" * 64),),
            ),
        }
        entries = list(iter_artifacts(LockInput(pins=pins)))
        with pytest.raises(DownloadError, match="collide on output filename"):
            _reject_colliding_targets(entries)

    def test_same_basename_same_digest_allowed(self) -> None:
        # The same archive pinned under two names writes identical bytes, so it
        # is not a collision.
        url = "https://a.example.com/dist/v1.0.0.tar.gz"
        pins = {
            "foo": ArchivePin(
                name="foo", version="1.0", url=url, hashes=(("sha256", "a" * 64),)
            ),
            "bar": ArchivePin(
                name="bar", version="1.0", url=url, hashes=(("sha256", "a" * 64),)
            ),
        }
        entries = list(iter_artifacts(LockInput(pins=pins)))
        _reject_colliding_targets(entries)


class TestExtractArchive:
    """extract_sdist_archive requires and defers safety to the tar data filter."""

    def test_requires_data_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nab_index.client._SUPPORTS_DATA_FILTER", False)
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(ValueError, match="requires the tar data filter"):
            extract_sdist_archive(_make_sdist("foo", "1.0.0", _PYPROJECT), out)

    @requires_data_filter
    def test_rejects_escaping_symlink_member(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("foo-1.0.0/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(ValueError, match="unsafe sdist member"):
            extract_sdist_archive(buf.getvalue(), out)

    @requires_data_filter
    def test_broken_hard_link_member_raises_value_error(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("foo-1.0.0/PKG-INFO")
            info.type = tarfile.LNKTYPE
            info.linkname = "foo-1.0.0/absent"
            tar.addfile(info)
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(ValueError, match="broken link in sdist member"):
            extract_sdist_archive(buf.getvalue(), out)
