"""Tests for direct-URL archive sources (``[[tool.nab.archive-sources]]``).

Most tests pre-seed the in-memory index with the archive bytes rather than
hitting the network, and exercise the extract-then-materialise path on real
``.tar.gz`` bytes.  The fetch tests drive a real coordinator, so the download
itself is under test.
"""

from __future__ import annotations

import ast
import errno
import gzip
import hashlib
import inspect
import io
import os
import sys
import tarfile
import textwrap
import traceback
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import httpx
import pytest
import respx

from nab_index.client import SdistHashMismatchError, extract_sdist_archive
from nab_index.httpx_async_transport import HttpxAsyncTransport
from nab_index.multi_index import IndexConfig
from nab_index.transport import HttpError
from nab_project import _sources as sources
from nab_project._sources import _fetch_archive_bytes
from nab_project._testing.coordinator_fake import make_coordinator
from nab_project.download import (
    DownloadEntry,
    DownloadError,
    _coalesce_download_targets,
    download_lock,
    iter_artifacts,
)
from nab_project.fetch import FetchCoordinator
from nab_project.lockfile import ArchivePin, LockInput, TargetLock
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import Version
from nab_provider.archive import ArchiveRequest, ArchiveRequestError
from nab_provider.metadata import WheelMetadata
from nab_provider.provider import (
    ArchiveSource,
    BuildPolicy,
    Provider,
    SourceNameMismatchError,
    UnsupportedSdistError,
)
from nab_provider.target import ResolveTarget

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager

    from nab_project.lockfile import PinShape


def _make_rooted_sdist(root: str, pyproject: str) -> bytes:
    """Return ``.tar.gz`` bytes for a one-file sdist under the ``root`` directory.

    ``root`` goes into the member name unchanged, so a test can give it any
    name tarfile writes.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = pyproject.encode("utf-8")
        info = tarfile.TarInfo(f"{root}/pyproject.toml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_sdist(name: str, version: str, pyproject: str) -> bytes:
    """Return ``.tar.gz`` bytes for a one-file sdist rooted at name-version."""
    return _make_rooted_sdist(f"{name}-{version}", pyproject)


def _make_flat_sdist(pyproject: str, *extra: tuple[str, bytes]) -> bytes:
    """Return ``.tar.gz`` bytes with pyproject.toml at the top level (no root dir).

    Each ``extra`` is one more top-level file as ``(name, body)``.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        members = (("pyproject.toml", pyproject.encode("utf-8")), *extra)
        for member_name, body in members:
            info = tarfile.TarInfo(member_name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


_PYPROJECT = '[project]\nname = "foo"\nversion = "1.0.0"\ndependencies = ["bar>=1"]\n'


def _half(data: bytes) -> bytes:
    """Return the leading half of ``data``, as a cut-short download leaves it."""
    return data[: len(data) // 2]


# A digit run just past CPython's int-from-string limit.
_OVERSIZED_DIGITS = "1" * sys.get_int_max_str_digits() + "1"


def _pax_sparse_map_sdist(sparse_map: str) -> bytes:
    """Return ``.tar.gz`` bytes carrying ``sparse_map`` as a PAX ``GNU.sparse.map``.

    ``tarfile`` runs ``int()`` over the record while opening the archive.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("foo-1.0.0/sparse")
        info.pax_headers = {"GNU.sparse.map": sparse_map}
        tar.addfile(info, io.BytesIO(b""))
    return buf.getvalue()


_CLEAN_PREFIX = 1 << 20
_INVALID_DEFLATE_BLOCK = b"\x07" * 8


def _corrupt_deflate_sdist() -> bytes:
    """Return ``.tar.gz`` bytes whose member data is behind a corrupt deflate block.

    The clean prefix is longer than the reader's decompression buffer, so the
    archive opens and its tar headers read; only reading a member's data hits the
    reserved block type zlib rejects.
    """
    body = (_PYPROJECT + "# filler\n" * (2 * _CLEAN_PREFIX // 9)).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("foo-1.0.0/pyproject.toml")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))

    deflate = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    clean = deflate.compress(buf.getvalue()[:_CLEAN_PREFIX])
    return clean + deflate.flush(zlib.Z_SYNC_FLUSH) + _INVALID_DEFLATE_BLOCK


def _provider(
    archive_sources: list[ArchiveSource],
    cache_dir: Path | None,
    *,
    build_policy: BuildPolicy = BuildPolicy.NEVER,
    offline: bool = False,
) -> Provider:
    return Provider(
        make_coordinator(offline=offline),
        archive_sources=archive_sources,
        archive_cache_dir=cache_dir,
        build_policy=build_policy,
    )


def _fetching_provider(
    coordinator: FetchCoordinator, source: ArchiveSource, cache_dir: Path
) -> Provider:
    """Provider wired to a real coordinator, so the archive is really fetched."""
    return Provider(
        coordinator,
        archive_sources=[source],
        archive_cache_dir=cache_dir,
        build_policy=BuildPolicy.NEVER,
    )


def _fetch_bytes(provider: Provider, source: ArchiveSource) -> bytes:
    """Fetch and verify ``source``'s archive the way materialisation does."""
    return _fetch_archive_bytes(
        canonicalize_name(source.name),
        source,
        ArchiveRequest.parse(source.url),
        port=provider.coordinator,
    )


def _archive_provider(data: bytes, cache: Path) -> Provider:
    """Return a provider that serves ``data`` as archive source "foo"."""
    digest = hashlib.sha256(data).hexdigest()
    source = ArchiveSource(
        name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
    )
    provider = _provider([source], cache)
    provider.coordinator.index.store_sdist_archive("foo", digest, data)
    return provider


def _warm_extracted_tree(cache: Path, digest: str) -> None:
    """Write the extracted tree a prior resolve of ``digest`` leaves behind.

    The hash record names the one sha256 that resolve verified.
    """
    target = cache / digest
    tree = target / "tree"
    tree.mkdir(parents=True)
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (target / ".nab-hashes").write_text(f"sha256={digest}", encoding="utf-8")
    (target / ".nab-complete").touch()


def _warm_dynamic_archive_tree(cache: Path, digest: str) -> Path:
    """Return a cached archive tree whose dependency metadata needs a backend."""
    _warm_extracted_tree(cache, digest)
    tree = cache / digest / "tree"
    (tree / "pyproject.toml").write_text(
        '[project]\nname = "foo"\nversion = "1.0.0"\ndynamic = ["dependencies"]\n',
        encoding="utf-8",
    )
    return tree


def _deny_access(monkeypatch: pytest.MonkeyPatch, method: str, target: Path) -> None:
    """Make one ``Path`` call on ``target`` fail with EACCES.

    A real chmod would not do: root ignores the mode bits and Windows has none.
    """
    original = getattr(Path, method)

    def failing(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, failing)


def _deny_disk_space(monkeypatch: pytest.MonkeyPatch, method: str, name: str) -> None:
    """Make every ``Path`` call on a path named ``name`` fail with ENOSPC.

    ``mkdtemp`` picks the entry's directory name, so paths beneath it cannot
    be named up front and the denial matches on the basename instead.
    """
    original = getattr(Path, method)

    def failing(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == name:
            raise OSError(errno.ENOSPC, "No space left on device", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, failing)


def _lock_input(pins: Mapping[str, PinShape]) -> LockInput:
    """Wrap ``pins`` as the one-target lock input the downloader reads."""
    target = ResolveTarget.for_host()
    return LockInput(targets={target.label: TargetLock(target=target, pins=pins)})


def _archive_download_entries(
    *,
    first_filename: str = "v1.0.0.tar.gz",
    second_filename: str = "v1.0.0.tar.gz",
    first_digest: str = "a" * 64,
    second_digest: str = "b" * 64,
    second_algorithm: str = "sha256",
) -> list[DownloadEntry]:
    """Return two archive entries from different URLs."""
    pins = {
        "alpha": ArchivePin(
            name="alpha",
            version="1.0",
            url=f"https://a.example.com/dist/{first_filename}",
            hashes=(("sha256", first_digest),),
        ),
        "beta": ArchivePin(
            name="beta",
            version="2.0",
            url=f"https://b.example.com/dist/{second_filename}",
            hashes=((second_algorithm, second_digest),),
        ),
    }
    return list(iter_artifacts(_lock_input(pins)))


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
        from nab_provider.provider import LocalSource

        digest = "a" * 64
        with pytest.raises(ValueError, match="duplicate source"):
            Provider(
                make_coordinator(),
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

    @requires_data_filter
    def test_project_name_mismatch_is_hard_error(self, tmp_path: Path) -> None:
        # The archive unpacks to a project whose [project].name (bar) is not the
        # declared source name (foo), so pinning it would carry a different
        # distribution's version and dependencies under the requested name.
        pyproject = (
            '[project]\nname = "bar"\nversion = "9.9.9"\n'
            'dependencies = ["unrelated-dep==6.6.6"]\n'
        )
        data = _make_sdist("bar", "9.9.9", pyproject)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/bar-9.9.9.tar.gz#sha256={digest}"
        )
        provider = _provider([source], tmp_path / "arch")
        provider.coordinator.index.store_sdist_archive("foo", digest, data)
        with pytest.raises(SourceNameMismatchError, match="bar") as excinfo:
            provider.fetch_versions("foo")
        assert "foo" in str(excinfo.value)

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
        # No bytes stored: the fetch produced nothing.
        with pytest.raises(UnsupportedSdistError, match="download.*failed"):
            provider.fetch_versions("foo")

    def test_tampered_archive_is_hard_error(self, tmp_path: Path) -> None:
        # Tampered bytes fail the resolve loudly, not as an UnsupportedSdistError
        # the look-ahead would treat as a skippable version.
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
        assert list((tmp_path / "arch").iterdir()) == []

    def test_truncated_archive_raises(self, tmp_path: Path) -> None:
        # The hash covers the truncated bytes, so this fails in extraction
        # rather than in the hash check.
        data = _half(_make_sdist("foo", "1.0.0", _PYPROJECT))
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        provider = _provider([source], tmp_path / "arch")
        provider.coordinator.index.store_sdist_archive("foo", digest, data)
        with pytest.raises(UnsupportedSdistError, match="could not be extracted"):
            provider.fetch_versions("foo")
        assert list((tmp_path / "arch").iterdir()) == []

    def test_unwritable_cache_dir_reports_the_os_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A cache directory this process cannot write fails the resolve the
        # way an unextractable archive does.
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        cache = tmp_path / "arch"
        _deny_access(monkeypatch, "mkdir", cache)
        provider = _archive_provider(data, cache)

        with pytest.raises(
            UnsupportedSdistError, match="could not be written"
        ) as excinfo:
            provider.fetch_versions("foo")

        assert "Permission denied" in str(excinfo.value)

    def test_full_cache_filesystem_reports_the_os_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "unpacked" is the first write inside the entry's temporary directory,
        # so this failure lands after ``mkdtemp`` succeeded.
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        cache = tmp_path / "arch"
        _deny_disk_space(monkeypatch, "mkdir", "unpacked")
        provider = _archive_provider(data, cache)

        with pytest.raises(
            UnsupportedSdistError, match="could not be written"
        ) as excinfo:
            provider.fetch_versions("foo")

        assert "No space left on device" in str(excinfo.value)
        assert list(cache.iterdir()) == []

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

    def test_dynamic_backend_mutation_does_not_poison_cached_tree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        digest = "a" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        tree = _warm_dynamic_archive_tree(cache, digest)
        dependency = tree / "dependency.txt"
        dependency.write_text("dep-one==1", encoding="utf-8")
        dependency_alias = tree / "dependency-alias.txt"
        os.link(dependency, dependency_alias)

        def mutating_backend(path: Path, **_kwargs: object) -> WheelMetadata:
            assert path.name == tree.name
            backend_input = path / "dependency.txt"
            backend_alias = path / "dependency-alias.txt"
            assert backend_input.samefile(backend_alias)
            backend_input.write_text("dep-two==2", encoding="utf-8")
            requires_dist = [Requirement(backend_alias.read_text(encoding="utf-8"))]
            return WheelMetadata(
                name="foo",
                version=Version("1.0.0"),
                requires_python=None,
                requires_dist=requires_dist,
                provides_extra=[],
            )

        monkeypatch.setattr(
            "nab_project.build_backend.extract_metadata", mutating_backend
        )

        first = _provider([source], cache, build_policy=BuildPolicy.BUILD_REMOTE)
        first.fetch_versions("foo")
        second = _provider([source], cache, build_policy=BuildPolicy.BUILD_REMOTE)
        second.fetch_versions("foo")

        metadata_key = ("foo", Version("1.0.0"))
        assert [
            str(first.metadata_cache[metadata_key].requires_dist[0]),
            str(second.metadata_cache[metadata_key].requires_dist[0]),
        ] == ["dep-two==2", "dep-two==2"]
        assert dependency.read_text(encoding="utf-8") == "dep-one==1"
        assert dependency_alias.read_text(encoding="utf-8") == "dep-one==1"
        assert dependency.samefile(dependency_alias)
        assert first.coordinator.calls_to("request_direct_archive") == []
        assert second.coordinator.calls_to("request_direct_archive") == []

    def test_warm_tree_build_runs_offline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A warm tree needs no download, so ``--offline`` only reaches its build."""
        digest = "a" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        _warm_dynamic_archive_tree(cache, digest)

        captured: dict[str, object] = {}

        def capturing_backend(_path: Path, **kwargs: object) -> WheelMetadata:
            captured.update(kwargs)
            return WheelMetadata(
                name="foo",
                version=Version("1.0.0"),
                requires_python=None,
                requires_dist=[],
                provides_extra=[],
            )

        monkeypatch.setattr(
            "nab_project.build_backend.extract_metadata", capturing_backend
        )

        provider = _provider(
            [source], cache, build_policy=BuildPolicy.BUILD_REMOTE, offline=True
        )
        provider.fetch_versions("foo")

        assert captured == {"config": None, "offline": True}

    @pytest.mark.parametrize(
        ("failure_target", "expected"),
        [
            (
                "nab_project._sources.tempfile.TemporaryDirectory",
                "could not create a temporary build tree",
            ),
            (
                "nab_project._sources.shutil.copytree",
                "could not copy cached source tree",
            ),
            (
                "nab_project._sources.os.link",
                "could not copy cached source tree",
            ),
        ],
    )
    def test_cached_tree_copy_failure_names_archive_source(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure_target: str,
        expected: str,
    ) -> None:
        digest = "a" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        tree = _warm_dynamic_archive_tree(cache, digest)
        dependency = tree / "dependency.txt"
        dependency.write_text("dep-one==1", encoding="utf-8")
        os.link(dependency, tree / "dependency-alias.txt")

        def fail_copy(*args: object, **kwargs: object) -> NoReturn:
            raise OSError("simulated copy failure")

        monkeypatch.setattr(failure_target, fail_copy)

        provider = _provider([source], cache, build_policy=BuildPolicy.BUILD_REMOTE)
        with pytest.raises(UnsupportedSdistError) as excinfo:
            provider.fetch_versions("foo")

        message = str(excinfo.value)
        assert "archive source 'foo'" in message
        assert expected in message
        assert "simulated copy failure" in message
        assert provider.coordinator.calls_to("request_direct_archive") == []

    @requires_data_filter
    def test_every_verified_hash_is_recorded_for_reuse(self, tmp_path: Path) -> None:
        # A warm resolve tests its declaration against the record, so the
        # record has to name every hash the download was checked against.
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        sha512 = hashlib.sha512(data).hexdigest()
        source = ArchiveSource(
            name="foo",
            url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}&sha512={sha512}",
        )
        cache = tmp_path / "arch"
        first = _provider([source], cache)
        first.coordinator.index.store_sdist_archive("foo", digest, data)
        first.fetch_versions("foo")

        record = (cache / digest / ".nab-hashes").read_text(encoding="utf-8")
        assert record.splitlines() == [f"sha256={digest}", f"sha512={sha512}"]

        second = _provider([source], cache)
        versions = second.fetch_versions("foo")

        assert str(versions[0][0]) == "1.0.0"
        assert not second.coordinator.calls_to("request_direct_archive")

    @requires_data_filter
    def test_partial_tree_never_visible_at_cache_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The marker is the only completeness signal, so a concurrent run would
        # read a half-written tree at the cache path as its own to wipe or to
        # mark complete.  Nothing may appear there until extraction is done.
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        target = cache / digest
        seen_mid_extract: list[bool] = []

        def watching_extract(payload: bytes, target_dir: Path) -> Path:
            seen_mid_extract.append(target.exists())
            return extract_sdist_archive(payload, target_dir)

        monkeypatch.setattr(sources, "extract_sdist_archive", watching_extract)
        provider = _provider([source], cache)
        provider.coordinator.index.store_sdist_archive("foo", digest, data)

        versions = provider.fetch_versions("foo")

        assert seen_mid_extract == [False]
        assert str(versions[0][0]) == "1.0.0"
        assert (target / "tree" / "pyproject.toml").is_file()
        assert (target / ".nab-complete").is_file()

    @requires_data_filter
    def test_lost_publish_race_uses_the_finished_tree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When a concurrent run publishes its own finished tree first, that tree
        # is used as-is and the temporary one is dropped.
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        target = cache / digest
        seen_mid_extract: list[bool] = []

        def racing_extract(payload: bytes, target_dir: Path) -> Path:
            seen_mid_extract.append(target.exists())
            root = extract_sdist_archive(payload, target_dir)
            _warm_extracted_tree(cache, digest)
            (target / "tree" / "WINNER").write_text("", encoding="utf-8")
            return root

        monkeypatch.setattr(sources, "extract_sdist_archive", racing_extract)
        provider = _provider([source], cache)
        provider.coordinator.index.store_sdist_archive("foo", digest, data)

        versions = provider.fetch_versions("foo")

        assert seen_mid_extract == [False]
        assert str(versions[0][0]) == "1.0.0"
        assert (target / "tree" / "WINNER").is_file()
        assert list(cache.iterdir()) == [target]

    @requires_data_filter
    def test_interrupted_extraction_leaves_no_temp_tree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An interrupt lands after the bytes are on disk but before the rename.
        # Nothing sweeps the cache later, so the temp tree has to go now.
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        cache = tmp_path / "arch"

        def interrupted_extract(payload: bytes, target_dir: Path) -> Path:
            extract_sdist_archive(payload, target_dir)
            raise KeyboardInterrupt

        monkeypatch.setattr(sources, "extract_sdist_archive", interrupted_extract)

        with pytest.raises(KeyboardInterrupt):
            sources._extract_archive(cache, digest, data, (("sha256", digest),))

        assert list(cache.iterdir()) == []

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
        # The cache dir is reached through a symlink, so the extractor's root
        # comes back resolved while the temp dir it moves into does not.  For a
        # flat archive the whole extraction dir is what moves.
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


class TestArchiveChosenNames:
    """The archive chooses every name inside its own tree.

    Only the bytes are pinned, by the URL's hash, so the root directory can
    carry any name, including the cache's own marker names.
    """

    @requires_data_filter
    @pytest.mark.parametrize(
        "marker", [sources._COMPLETE_MARKER, sources._HASHES_MARKER]
    )
    def test_root_named_like_a_cache_marker_resolves(
        self, marker: str, tmp_path: Path
    ) -> None:
        data = _make_rooted_sdist(marker, _PYPROJECT)
        cache = tmp_path / "arch"
        target = cache / hashlib.sha256(data).hexdigest()

        versions = _archive_provider(data, cache).fetch_versions("foo")

        assert str(versions[0][0]) == "1.0.0"
        assert sorted(entry.name for entry in target.iterdir()) == [
            ".nab-complete",
            ".nab-hashes",
            "tree",
        ]

        # Both markers are still files the warm path can read, so a second
        # resolve is served from the cache.
        second = _archive_provider(data, cache)
        assert str(second.fetch_versions("foo")[0][0]) == "1.0.0"
        assert not second.coordinator.calls_to("request_direct_archive")

    @requires_data_filter
    def test_flat_archive_keeps_its_own_marker_named_file(self, tmp_path: Path) -> None:
        # A flat archive ships its files where the cache keeps its bookkeeping,
        # so a file of the same name has to survive with the archive's bytes.
        data = _make_flat_sdist(_PYPROJECT, (sources._HASHES_MARKER, b"shipped"))
        digest = hashlib.sha256(data).hexdigest()
        cache = tmp_path / "arch"
        target = cache / digest

        versions = _archive_provider(data, cache).fetch_versions("foo")

        assert str(versions[0][0]) == "1.0.0"

        assert (target / "tree" / sources._HASHES_MARKER).read_bytes() == b"shipped"
        record = (target / sources._HASHES_MARKER).read_text(encoding="utf-8")
        assert record == f"sha256={digest}"


class TestWarmArchiveCache:
    """A digest already extracted is served from the cache, with no download."""

    def test_warm_tree_is_served_without_a_fetch(self, tmp_path: Path) -> None:
        digest = "b" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        _warm_extracted_tree(cache, digest)
        provider = _provider([source], cache)

        versions = provider.fetch_versions("foo")

        assert str(versions[0][0]) == "1.0.0"
        assert not provider.coordinator.calls_to("request_direct_archive")

    def test_partial_tree_without_marker_is_not_served(self, tmp_path: Path) -> None:
        # Without the completion marker the tree is a crashed run's leftover,
        # so the archive is fetched rather than read out of a half-written tree.
        digest = "d" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        partial = cache / digest / "foo-1.0.0"
        partial.mkdir(parents=True)
        (partial / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
        provider = _provider([source], cache)

        with pytest.raises(UnsupportedSdistError, match="download.*failed"):
            provider.fetch_versions("foo")

    def test_added_hash_is_checked_against_the_bytes(self, tmp_path: Path) -> None:
        # The tree records only the sha256 it is keyed on, so adding a hash
        # refetches and checks the bytes against it.
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(
            name="foo",
            url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}&sha512={'f' * 128}",
        )
        cache = tmp_path / "arch"
        _warm_extracted_tree(cache, digest)
        provider = _provider([source], cache)
        provider.coordinator.index.store_sdist_archive("foo", digest, data)

        with pytest.raises(SdistHashMismatchError):
            provider.fetch_versions("foo")

    def test_tree_without_hash_record_is_not_served(self, tmp_path: Path) -> None:
        # A tree extracted before the record was written says nothing about
        # which hashes were checked, so it is refetched rather than trusted.
        digest = "e" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        _warm_extracted_tree(cache, digest)
        (cache / digest / ".nab-hashes").unlink()
        provider = _provider([source], cache)

        with pytest.raises(UnsupportedSdistError, match="download.*failed"):
            provider.fetch_versions("foo")

    def test_tree_with_undecodable_hash_record_is_not_served(
        self, tmp_path: Path
    ) -> None:
        # A record that will not decode covers no hashes, so the tree is
        # refetched rather than trusted.
        digest = "c" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        _warm_extracted_tree(cache, digest)
        (cache / digest / ".nab-hashes").write_bytes(b"sha256=\xff\xfe\n")
        provider = _provider([source], cache)

        with pytest.raises(UnsupportedSdistError, match="download.*failed"):
            provider.fetch_versions("foo")

    def test_tree_with_unreadable_hash_record_is_not_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A record that stats as a file but will not open says as little as
        # no record at all.
        digest = "f" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        _warm_extracted_tree(cache, digest)
        _deny_access(monkeypatch, "read_text", cache / digest / ".nab-hashes")
        provider = _provider([source], cache)

        with pytest.raises(UnsupportedSdistError, match="download.*failed"):
            provider.fetch_versions("foo")

    def test_entry_with_unreadable_marker_is_not_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mkdtemp leaves the entry 0700, so another user's entry is not
        # traversable and its marker cannot be stat'ed at all.
        digest = "1" * 64
        source = ArchiveSource(
            name="foo", url=f"https://ex.com/foo-1.0.0.tar.gz#sha256={digest}"
        )
        cache = tmp_path / "arch"
        _warm_extracted_tree(cache, digest)
        _deny_access(monkeypatch, "stat", cache / digest / ".nab-complete")
        provider = _provider([source], cache)

        with pytest.raises(UnsupportedSdistError, match="download.*failed"):
            provider.fetch_versions("foo")

    @respx.mock
    def test_offline_resolve_reads_the_warm_tree(self, tmp_path: Path) -> None:
        # --offline never reaches the network, so the tree an earlier resolve
        # extracted is all there is to resolve from.
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        url = "https://ex.invalid/foo-1.0.0.tar.gz"
        route = respx.get(url).mock(return_value=httpx.Response(200, content=data))
        source = ArchiveSource(name="foo", url=f"{url}#sha256={digest}")
        cache = tmp_path / "arch"
        _warm_extracted_tree(cache, digest)

        with FetchCoordinator(
            transport=HttpxAsyncTransport(), offline=True
        ) as coordinator:
            provider = _fetching_provider(coordinator, source, cache)
            versions = provider.fetch_versions("foo")

        assert str(versions[0][0]) == "1.0.0"
        assert not route.called


class TestArchiveFetch:
    """An archive URL is read by its own scheme, whatever the index speaks."""

    def test_file_archive_read_from_disk_under_https_index(
        self, tmp_path: Path
    ) -> None:
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        archive = tmp_path / "foo-1.0.0.tar.gz"
        archive.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        source = ArchiveSource(name="foo", url=f"{archive.as_uri()}#sha256={digest}")

        with FetchCoordinator(transport=HttpxAsyncTransport()) as coordinator:
            provider = _fetching_provider(coordinator, source, tmp_path / "arch")
            fetched = _fetch_bytes(provider, source)

        assert fetched == data

    @respx.mock
    def test_https_archive_fetched_over_http_under_file_index(
        self, tmp_path: Path
    ) -> None:
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        url = "https://ex.invalid/foo-1.0.0.tar.gz"
        respx.get(url).mock(return_value=httpx.Response(200, content=data))
        source = ArchiveSource(name="foo", url=f"{url}#sha256={digest}")

        with FetchCoordinator(
            transport=HttpxAsyncTransport(),
            indexes=[IndexConfig("local", wheelhouse.as_uri())],
        ) as coordinator:
            provider = _fetching_provider(coordinator, source, tmp_path / "arch")
            fetched = _fetch_bytes(provider, source)

        assert fetched == data

    @respx.mock
    def test_offline_cold_archive_reports_offline_not_download_failure(
        self, tmp_path: Path
    ) -> None:
        # Nothing was downloaded, so "download failed" would name the wrong
        # cause: the run was told not to reach the network.
        data = _make_sdist("foo", "1.0.0", _PYPROJECT)
        digest = hashlib.sha256(data).hexdigest()
        url = "https://ex.invalid/foo-1.0.0.tar.gz"
        route = respx.get(url).mock(return_value=httpx.Response(200, content=data))
        source = ArchiveSource(name="foo", url=f"{url}#sha256={digest}")

        with FetchCoordinator(
            transport=HttpxAsyncTransport(), offline=True
        ) as coordinator:
            provider = _fetching_provider(coordinator, source, tmp_path / "arch")
            with pytest.raises(UnsupportedSdistError, match="offline mode") as excinfo:
                _fetch_bytes(provider, source)

        assert url in str(excinfo.value)
        assert not route.called

    @respx.mock
    def test_http_error_is_reported_with_its_status(self, tmp_path: Path) -> None:
        url = "https://ex.invalid/foo-1.0.0.tar.gz"
        respx.get(url).mock(return_value=httpx.Response(404))
        source = ArchiveSource(name="foo", url=f"{url}#sha256={'a' * 64}")

        with FetchCoordinator(transport=HttpxAsyncTransport()) as coordinator:
            provider = _fetching_provider(coordinator, source, tmp_path / "arch")
            with pytest.raises(UnsupportedSdistError, match="404") as excinfo:
                _fetch_bytes(provider, source)

        assert isinstance(excinfo.value.__cause__, HttpError)

    def test_missing_file_archive_fails_the_resolve(self, tmp_path: Path) -> None:
        absent = tmp_path / "absent-1.0.0.tar.gz"
        source = ArchiveSource(name="foo", url=f"{absent.as_uri()}#sha256={'a' * 64}")

        with FetchCoordinator(transport=HttpxAsyncTransport()) as coordinator:
            provider = _fetching_provider(coordinator, source, tmp_path / "arch")
            with pytest.raises(UnsupportedSdistError, match="absent-1.0.0"):
                _fetch_bytes(provider, source)


class TestArchiveDownload:
    def test_archive_pin_yields_download_entry(self) -> None:
        pin = ArchivePin(
            name="foo",
            version="1.0",
            url="https://ex.com/foo-1.0.tar.gz",
            hashes=(("sha256", "e" * 64),),
        )
        (entry,) = list(iter_artifacts(_lock_input({"foo": pin})))
        assert entry.url == "https://ex.com/foo-1.0.tar.gz"
        assert entry.filename == "foo-1.0.tar.gz"
        assert entry.hash_algo == "sha256"
        assert entry.digest == "e" * 64
        assert entry.local_path is None

    def test_file_url_archive_pin_carries_local_path(self, tmp_path: Path) -> None:
        archive = tmp_path / "foo-1.0.0.tar.gz"
        archive.write_bytes(b"ARCHIVE")
        pin = ArchivePin(
            name="foo",
            version="1.0.0",
            url=archive.as_uri(),
            hashes=(("sha256", hashlib.sha256(b"ARCHIVE").hexdigest()),),
        )
        (entry,) = list(iter_artifacts(_lock_input({"foo": pin})))
        assert entry.filename == "foo-1.0.0.tar.gz"
        assert entry.local_path == archive

    def test_primary_digest_no_acceptable_hash_raises(self) -> None:
        pin = ArchivePin(name="foo", version="1.0", url="u", hashes=(("md5", "x"),))
        with pytest.raises(ValueError, match="no acceptable hash"):
            _ = pin.primary_digest

    def test_different_hash_same_filename_rejected(self) -> None:
        with pytest.raises(DownloadError, match="different hash identities"):
            _coalesce_download_targets(_archive_download_entries())

    def test_different_hash_casefold_equivalent_filenames_rejected(self) -> None:
        with pytest.raises(DownloadError, match="different hash identities") as excinfo:
            _coalesce_download_targets(
                _archive_download_entries(
                    first_filename="Artifact.tar.gz",
                    second_filename="artifact.tar.gz",
                )
            )

        assert "'Artifact.tar.gz' and 'artifact.tar.gz'" in str(excinfo.value)

    def test_same_hash_identity_same_filename_coalesced(self) -> None:
        entries = _archive_download_entries(second_digest="a" * 64)

        assert entries[0].url != entries[1].url
        assert _coalesce_download_targets(entries) == [entries[0]]

    def test_same_hash_identity_casefold_equivalent_filenames_coalesced(self) -> None:
        entries = _archive_download_entries(
            first_filename="Artifact.tar.gz",
            second_filename="artifact.tar.gz",
            second_digest="a" * 64,
        )
        assert _coalesce_download_targets(entries) == [entries[0]]

    def test_unicode_casefold_equivalent_filenames_coalesced(self) -> None:
        entries = _archive_download_entries(
            first_filename="Straße.tar.gz",
            second_filename="STRASSE.tar.gz",
            second_digest="a" * 64,
        )
        assert _coalesce_download_targets(entries) == [entries[0]]

    @pytest.mark.parametrize(
        ("first_filename", "second_filename"),
        [
            pytest.param("artifact.tar.gz", "artifact.tar.gz", id="exact"),
            pytest.param("Artifact.tar.gz", "artifact.tar.gz", id="casefold"),
        ],
    )
    def test_same_hash_identity_casefold_equivalent_filename_written_once(
        self,
        tmp_path: Path,
        first_filename: str,
        second_filename: str,
    ) -> None:
        payload = b"ARCHIVE"
        digest = hashlib.sha256(payload).hexdigest()
        first = tmp_path / "first" / first_filename
        second = tmp_path / "second" / second_filename
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(payload)
        second.write_bytes(payload)
        pins = {
            "alpha": ArchivePin(
                name="alpha",
                version="1.0",
                url=first.as_uri(),
                hashes=(("sha256", digest),),
            ),
            "beta": ArchivePin(
                name="beta",
                version="1.0",
                url=second.as_uri(),
                hashes=(("sha256", digest),),
            ),
        }

        output = tmp_path / "output"
        result = download_lock(
            _lock_input(pins),
            HttpxAsyncTransport(),
            output,
        )

        expected = output / first_filename
        assert result.written == (expected,)
        assert result.skipped == ()
        assert expected.read_bytes() == payload
        assert list(output.iterdir()) == [expected]

    def test_same_digest_text_under_different_algorithms_rejected(self) -> None:
        with pytest.raises(DownloadError, match="different hash identities"):
            _coalesce_download_targets(
                _archive_download_entries(
                    first_filename="Artifact.tar.gz",
                    second_filename="artifact.tar.gz",
                    second_digest="a" * 64,
                    second_algorithm="sha384",
                )
            )


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

    @requires_data_filter
    def test_chained_link_members_raise_value_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chain of link members is an unreadable archive, not a crash.

        tarfile recurses once per link either way: it copies a link's target
        when it cannot create the link, and it resolves the destination through
        the links it did create.  A long enough chain runs out of stack.
        Refusing os.symlink, as a Windows host without the privilege does, pins
        this to the copying path.
        """

        def refuse_symlink(*_args: object, **_kwargs: object) -> NoReturn:
            raise OSError(errno.EPERM, "symbolic link privilege not held")

        monkeypatch.setattr(os, "symlink", refuse_symlink)

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            directory = tarfile.TarInfo("foo-1.0.0")
            directory.type = tarfile.DIRTYPE
            tar.addfile(directory)

            for index in range(200):
                link = tarfile.TarInfo(f"foo-1.0.0/link{index}")
                link.type = tarfile.SYMTYPE
                link.linkname = f"link{index - 1}" if index else "absent"
                tar.addfile(link)

        out = tmp_path / "out"
        out.mkdir()

        original = sys.getrecursionlimit()
        try:
            # Just above the current depth, so the chain overruns the limit
            # whatever this interpreter starts with.
            sys.setrecursionlimit(len(traceback.extract_stack()) + 100)
            with pytest.raises(ValueError, match="unreadable sdist archive"):
                extract_sdist_archive(buf.getvalue(), out)
        finally:
            sys.setrecursionlimit(original)

    @requires_data_filter
    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"not a gzip stream",
            gzip.compress(b"not a tar"),
            _half(_make_sdist("foo", "1.0.0", _PYPROJECT)),
            _corrupt_deflate_sdist(),
            _pax_sparse_map_sdist(_OVERSIZED_DIGITS + ",1"),
            _pax_sparse_map_sdist("nope,1"),
        ],
        ids=[
            "empty",
            "not-gzip",
            "gzip-not-tar",
            "truncated",
            "corrupt-deflate",
            "sparse-map-past-int-limit",
            "sparse-map-non-numeric",
        ],
    )
    def test_unreadable_archive_raises_value_error(
        self, data: bytes, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(ValueError, match="unreadable sdist archive"):
            extract_sdist_archive(data, out)

    @requires_data_filter
    def test_flat_archive_with_one_package_dir_keeps_root(self, tmp_path: Path) -> None:
        # Top-level project files beside a lone package dir: the package dir is
        # not a wrapping root, so the source root stays the extraction root.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, text in (
                ("pyproject.toml", _PYPROJECT),
                ("setup.py", "from setuptools import setup\n\nsetup()\n"),
                ("mypkg/__init__.py", ""),
            ):
                body = text.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        out = tmp_path / "out"
        out.mkdir()
        root = extract_sdist_archive(buf.getvalue(), out)
        assert root == out.resolve()
        assert (root / "pyproject.toml").is_file()

    @requires_data_filter
    def test_wrapping_directory_is_the_source_root(self, tmp_path: Path) -> None:
        # A conformant sdist holds every member under one name-version directory,
        # and that directory is the source root the build backend runs in.
        out = tmp_path / "out"
        out.mkdir()
        root = extract_sdist_archive(_make_sdist("foo", "1.0.0", _PYPROJECT), out)
        assert root == out.resolve() / "foo-1.0.0"
        assert (root / "pyproject.toml").is_file()

    @requires_data_filter
    def test_top_level_file_beside_a_directory_keeps_root(self, tmp_path: Path) -> None:
        # A stray top-level file means no directory wraps every member, so the
        # root stays at the extraction dir instead of guessing that the lone
        # directory is the source tree.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, text in (
                ("foo-1.0.0/pyproject.toml", _PYPROJECT),
                ("README", "readme\n"),
            ):
                body = text.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        out = tmp_path / "out"
        out.mkdir()
        root = extract_sdist_archive(buf.getvalue(), out)
        assert root == out.resolve()
        assert (root / "foo-1.0.0" / "pyproject.toml").is_file()


def _attribute_docstrings(cls: type) -> dict[str, str]:
    """Return each class attribute's docstring, keyed by attribute name.

    A bare string literal after an assignment in a class body is an attribute
    docstring.  It is not reachable at runtime (``member.__doc__`` returns the
    class docstring), so read it from source the way Sphinx and IDEs do.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    classdef = tree.body[0]
    assert isinstance(classdef, ast.ClassDef)

    docs: dict[str, str] = {}
    pending: str | None = None
    for node in classdef.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            pending = node.targets[0].id
            continue
        if (
            pending is not None
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            docs[pending] = node.value.value
        pending = None
    return docs


class TestArchiveBuildPolicyLevels:
    """Archive sources across the three build-policy levels.

    Pins the behaviour the BuildPolicy attribute docstrings enumerate: a
    static ``[project]`` archive is read at every level, and a dynamic
    archive raises until build-remote, where the backend is invoked.
    """

    def _extract(self, policy: BuildPolicy, path: Path) -> WheelMetadata:
        return sources.extract_source_metadata(
            path,
            descriptor="archive source 'pkg'",
            policy=policy,
            kind="archive",
            offline=False,
            build_config=None,
        )

    @pytest.mark.parametrize("policy", [BuildPolicy.NEVER, BuildPolicy.BUILD_LOCAL])
    def test_static_archive_read_below_build_remote(
        self, policy: BuildPolicy, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "1.0.0"\n'
            'dependencies = ["requests>=2"]\n',
            encoding="utf-8",
        )
        metadata = self._extract(policy, tmp_path)
        assert metadata.name == "pkg"
        assert [str(r) for r in metadata.requires_dist] == ["requests>=2"]

    @pytest.mark.parametrize("policy", [BuildPolicy.NEVER, BuildPolicy.BUILD_LOCAL])
    def test_unreadable_pyproject_reports_the_errno(
        self,
        policy: BuildPolicy,
        tmp_path: Path,
        deny_access: Callable[[Path], AbstractContextManager[None]],
    ) -> None:
        """An unreadable pyproject is reported as a read failure.

        Under a policy that bars the build, the fall-through would call the
        source dynamic, which is a claim about a file nothing has read.
        """
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "1.0.0"\n', encoding="utf-8"
        )
        with (
            deny_access(tmp_path / "pyproject.toml"),
            pytest.raises(UnsupportedSdistError, match="Permission denied") as caught,
        ):
            self._extract(policy, tmp_path)
        assert "dynamic metadata" not in str(caught.value)

    @pytest.mark.parametrize(
        "policy",
        [BuildPolicy.NEVER, BuildPolicy.BUILD_LOCAL, BuildPolicy.BUILD_REMOTE],
    )
    def test_unparseable_pyproject_reports_the_parse_error(
        self, policy: BuildPolicy, tmp_path: Path
    ) -> None:
        """A pyproject that is not TOML is a read failure at every level.

        No build policy makes the file parse, so none of them is the remedy.
        """
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion =\n', encoding="utf-8"
        )
        with pytest.raises(UnsupportedSdistError) as caught:
            self._extract(policy, tmp_path)

        msg = str(caught.value)
        assert msg.startswith(
            f"archive source 'pkg': could not read pyproject.toml at {tmp_path}: "
        )
        assert "line 3" in msg
        assert "dynamic metadata" not in msg

    @pytest.mark.parametrize(
        "policy",
        [BuildPolicy.NEVER, BuildPolicy.BUILD_LOCAL, BuildPolicy.BUILD_REMOTE],
    )
    def test_non_utf8_pyproject_reports_the_decode_error(
        self, policy: BuildPolicy, tmp_path: Path
    ) -> None:
        """Bytes that will not decode as UTF-8 are unread, not dynamic."""
        (tmp_path / "pyproject.toml").write_bytes(
            b'[project]\nname = "pkg"\nversion = "1.0"\ndescription = "\xe9"\n'
        )
        with pytest.raises(UnsupportedSdistError) as caught:
            self._extract(policy, tmp_path)

        msg = str(caught.value)
        assert msg.startswith(
            f"archive source 'pkg': could not read pyproject.toml at {tmp_path}: "
        )
        assert "utf-8" in msg
        assert "dynamic metadata" not in msg

    @pytest.mark.parametrize(
        "policy",
        [BuildPolicy.NEVER, BuildPolicy.BUILD_LOCAL, BuildPolicy.BUILD_REMOTE],
    )
    def test_non_regular_pyproject_reports_the_file_type(
        self, policy: BuildPolicy, tmp_path: Path
    ) -> None:
        """A directory in place of pyproject.toml is unread, not dynamic."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.mkdir()
        with pytest.raises(UnsupportedSdistError) as caught:
            self._extract(policy, tmp_path)
        assert str(caught.value) == (
            f"archive source 'pkg': {pyproject} exists but is not a regular file"
        )

    @pytest.mark.parametrize("policy", [BuildPolicy.NEVER, BuildPolicy.BUILD_LOCAL])
    def test_dynamic_archive_raises_below_build_remote(
        self, policy: BuildPolicy, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "1.0.0"\ndynamic = ["dependencies"]\n',
            encoding="utf-8",
        )
        with pytest.raises(UnsupportedSdistError) as excinfo:
            self._extract(policy, tmp_path)
        msg = str(excinfo.value)
        assert "building requires build-policy 'build-remote'" in msg
        assert f"effective policy is '{policy.value}'" in msg
        assert "BuildPolicy." not in msg

    def test_dynamic_archive_builds_at_build_remote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "1.0.0"\ndynamic = ["dependencies"]\n',
            encoding="utf-8",
        )
        built = WheelMetadata(
            name="pkg",
            version=Version("1.0.0"),
            requires_python=None,
            requires_dist=[],
            provides_extra=[],
        )
        monkeypatch.setattr(
            "nab_project.build_backend.extract_metadata",
            lambda _path, **_kwargs: built,
        )
        assert self._extract(BuildPolicy.BUILD_REMOTE, tmp_path) is built

    @pytest.mark.parametrize("member", ["NEVER", "BUILD_LOCAL", "BUILD_REMOTE"])
    def test_level_docstring_names_archive_sources(self, member: str) -> None:
        docstring = _attribute_docstrings(BuildPolicy)[member]
        assert "archive" in docstring.lower()


class TestSourceWithoutProjectFile:
    """A declared source whose tree holds no pyproject.toml and no setup.py.

    The static reader returns ``None`` for it just as it does for a dynamic
    tree, but no policy level gives the backend anything to invoke, so a
    build-policy refusal would name a remedy that cannot work.
    """

    def _extract(self, path: Path, *, kind: str, policy: BuildPolicy) -> WheelMetadata:
        return sources.extract_source_metadata(
            path,
            descriptor=f"{kind} source 'pkg'",
            policy=policy,
            kind=kind,
            offline=False,
            build_config=None,
        )

    @pytest.mark.parametrize(
        ("kind", "policy"),
        [
            ("archive", BuildPolicy.NEVER),
            ("archive", BuildPolicy.BUILD_LOCAL),
            ("archive", BuildPolicy.BUILD_REMOTE),
            ("local", BuildPolicy.NEVER),
            ("local", BuildPolicy.BUILD_LOCAL),
            ("vcs", BuildPolicy.BUILD_REMOTE),
        ],
    )
    def test_tree_without_a_project_file_names_the_missing_file(
        self, kind: str, policy: BuildPolicy, tmp_path: Path
    ) -> None:
        with pytest.raises(UnsupportedSdistError) as excinfo:
            self._extract(tmp_path, kind=kind, policy=policy)
        assert str(excinfo.value) == (
            f"{kind} source 'pkg': no pyproject.toml or setup.py at {tmp_path}"
        )

    def test_missing_subdirectory_names_the_missing_file(self, tmp_path: Path) -> None:
        """A mistyped ``subdirectory`` lands on a path that is not there at all."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "pkg"\nversion = "1.0.0"\n', encoding="utf-8"
        )
        subdirectory = tmp_path / "nope"
        with pytest.raises(UnsupportedSdistError) as excinfo:
            self._extract(subdirectory, kind="archive", policy=BuildPolicy.BUILD_LOCAL)
        assert str(excinfo.value) == (
            f"archive source 'pkg': no pyproject.toml or setup.py at {subdirectory}"
        )

    def test_setup_py_tree_is_still_a_policy_refusal(self, tmp_path: Path) -> None:
        """A legacy setup.py tree has a project file, so the policy refuses it."""
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup\n\nsetup()\n", encoding="utf-8"
        )
        with pytest.raises(UnsupportedSdistError) as excinfo:
            self._extract(tmp_path, kind="local", policy=BuildPolicy.NEVER)
        assert "has dynamic metadata" in str(excinfo.value)

    def test_pyproject_without_a_project_table_is_still_a_policy_refusal(
        self, tmp_path: Path
    ) -> None:
        """A pyproject with no ``[project]`` is a project file, so policy decides."""
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\n', encoding="utf-8"
        )
        with pytest.raises(UnsupportedSdistError) as excinfo:
            self._extract(tmp_path, kind="local", policy=BuildPolicy.NEVER)
        assert "has dynamic metadata" in str(excinfo.value)
