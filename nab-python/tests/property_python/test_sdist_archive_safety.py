"""Property tests for :mod:`nab_index` sdist archive handling and file URLs.

``_extract_sdist_files`` never raises on arbitrary bytes; for well-formed
sdists it returns PKG-INFO and pyproject.toml from the single top-level
directory that carries a PKG-INFO, and only for files one level below it.
``extract_sdist_archive`` must never write outside the
target directory, whatever the member names are (``..``, absolute,
backslash, ``./.`` prefixes, symlink members); it either raises or
extracts safely.  ``parse_file_url`` round-trips ``Path.as_uri()`` for
spacey and unicode paths.
"""

from __future__ import annotations

import contextlib
import io
import tarfile
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nab_index.client import _extract_sdist_files, extract_sdist_archive
from nab_index.local_index import parse_file_url

from .strategies import PROPERTY_SETTINGS

pytestmark = pytest.mark.property


def make_targz(members: list[tuple[str, bytes | None]]) -> bytes:
    """Build a .tar.gz; value ``None`` makes a directory, bytes a regular file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


texts = st.text(min_size=0, max_size=30)
depths = st.integers(min_value=0, max_value=3)
roots = st.sampled_from(["pkg-1.0", "other-2.0"])
kinds = st.sampled_from(["PKG-INFO", "pyproject.toml"])


def _member_path(root: str, depth: int, kind: str) -> str:
    """Path for ``kind`` sitting ``depth`` levels below the archive root.

    Depth 0 is the bare archive root (no directory); depth 1 is directly
    under ``root``; deeper values nest extra ``sub`` levels.
    """
    if depth == 0:
        return kind
    return "/".join([root] + ["sub"] * (depth - 1) + [kind])


@st.composite
def sdist_archives(draw: st.DrawFn) -> tuple[bytes, str | None, str | None]:
    """Archive plus the expected ``(pkg_info, pyproject)`` extraction result.

    Models the single-root contract of ``_select_sdist_root``: only files
    one level below a top-level directory count, the root is the lone
    directory carrying a PKG-INFO, pyproject counts only inside that same
    root, and zero or several PKG-INFO roots yield ``(None, None)``.
    """
    members: list[tuple[str, bytes | None]] = []
    seen: set[str] = set()
    depth1: dict[tuple[str, str], str] = {}

    for _ in range(draw(st.integers(min_value=0, max_value=5))):
        root = draw(roots)
        kind = draw(kinds)
        depth = draw(depths)
        content = draw(texts)
        path = _member_path(root, depth, kind)
        if path in seen:
            continue
        seen.add(path)
        members.append((path, content.encode()))
        if depth == 1:
            depth1[(root, kind)] = content

    pkg_roots = [r for (r, k) in depth1 if k == "PKG-INFO"]
    if len(pkg_roots) == 1:
        (root,) = pkg_roots
        expected_pkginfo: str | None = depth1[(root, "PKG-INFO")]
        expected_pyproject: str | None = depth1.get((root, "pyproject.toml"))
    else:
        expected_pkginfo = None
        expected_pyproject = None

    return make_targz(members), expected_pkginfo, expected_pyproject


@given(data=sdist_archives())
@PROPERTY_SETTINGS
def test_extract_sdist_files_single_root_rule(
    data: tuple[bytes, str | None, str | None],
) -> None:
    """PKG-INFO and pyproject.toml come from one top-level root, depth 1."""
    archive, expected_pkginfo, expected_pyproject = data
    pkg_info, pyproject = _extract_sdist_files(archive)
    assert pkg_info == expected_pkginfo
    assert pyproject == expected_pyproject


@given(blob=st.binary(max_size=200))
@PROPERTY_SETTINGS
def test_extract_sdist_files_never_raises_on_garbage(blob: bytes) -> None:
    """Arbitrary bytes yield ``(None, None)``, never an exception."""
    assert _extract_sdist_files(blob) == (None, None)


_PKG_INFO = "Metadata-Version: 2.2\nName: pkg\nVersion: 1.0\n"


@st.composite
def truncated_archives(draw: st.DrawFn) -> bytes:
    """A well-formed sdist cut short at a random byte."""
    archive = make_targz([("pkg-1.0/PKG-INFO", _PKG_INFO.encode())])
    cut = draw(st.integers(min_value=1, max_value=len(archive) - 1))
    return archive[:cut]


@given(blob=truncated_archives())
@PROPERTY_SETTINGS
def test_extract_sdist_files_never_raises_on_truncated_archive(blob: bytes) -> None:
    """A truncated archive yields whole metadata or none, never an exception."""
    pkg_info, pyproject = _extract_sdist_files(blob)
    assert pkg_info in (None, _PKG_INFO)
    assert pyproject is None


hostile_names = st.sampled_from(
    [
        "../evil.txt",
        "/abs.txt",
        "./.hidden",
        "a\\b.txt",
        "pkg-1.0/../../evil.txt",
        "..",
        "pkg-1.0/./../up.txt",
        "./../up2.txt",
        "ok/inner/../../../esc.txt",
    ]
)
benign_names = st.sampled_from(
    [
        "pkg-1.0/setup.py",
        "pkg-1.0/PKG-INFO",
        "README",
        "pkg-1.0/sub/x.txt",
        "./pkg-1.0/y",
    ]
)


@st.composite
def mixed_archives(draw: st.DrawFn) -> bytes:
    """Archives mixing benign members with traversal attempts."""
    n = draw(st.integers(min_value=1, max_value=5))
    members: list[tuple[str, bytes | None]] = []
    for _ in range(n):
        hostile = draw(st.booleans())
        name = draw(hostile_names) if hostile else draw(benign_names)
        members.append((name, b"x"))
    return make_targz(members)


@given(archive=mixed_archives())
@PROPERTY_SETTINGS
def test_extract_sdist_archive_never_escapes_target(archive: bytes) -> None:
    """No member, hostile or not, may create a path outside the target."""
    with tempfile.TemporaryDirectory() as td:
        parent = Path(td)
        target = parent / "t"
        target.mkdir()
        before = set(parent.rglob("*"))
        with contextlib.suppress(ValueError, OSError, tarfile.TarError):
            extract_sdist_archive(archive, target)
        new = set(parent.rglob("*")) - before
        for p in new:
            assert target in p.parents, f"escaped target: {p}"


@given(symlink_target=st.sampled_from(["/etc/passwd", "../../outside", "ok.txt"]))
@PROPERTY_SETTINGS
def test_extract_sdist_archive_symlinks_cannot_escape(symlink_target: str) -> None:
    """Writing through a symlink member cannot land outside the target."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="pkg-1.0/link")
        info.type = tarfile.SYMTYPE
        info.linkname = symlink_target
        tar.addfile(info)
        info2 = tarfile.TarInfo(name="pkg-1.0/link/inner.txt")
        info2.size = 1
        tar.addfile(info2, io.BytesIO(b"x"))
    archive = buf.getvalue()
    with tempfile.TemporaryDirectory() as td:
        parent = Path(td)
        target = parent / "t"
        target.mkdir()
        with contextlib.suppress(ValueError, OSError, tarfile.TarError):
            extract_sdist_archive(archive, target)
        outside = [
            p for p in parent.rglob("*") if p != target and target not in p.parents
        ]
        assert outside == []


path_segments = st.from_regex(r"[A-Za-z0-9 _.é京-]{1,12}", fullmatch=True).filter(
    lambda s: s not in (".", "..") and not s.endswith(" ") and not s.startswith(" ")
)


@given(segments=st.lists(path_segments, min_size=1, max_size=4))
@PROPERTY_SETTINGS
def test_parse_file_url_roundtrips_as_uri(segments: list[str]) -> None:
    """``parse_file_url`` inverts ``Path.as_uri`` for odd but legal paths."""
    path = Path("/", *segments)
    assert parse_file_url(path.as_uri()) == path
