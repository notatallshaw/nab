"""Property tests for :mod:`nab_index` sdist archive handling and file URLs.

``_extract_sdist_files`` never raises on arbitrary bytes; for well-formed
sdists it returns PKG-INFO and pyproject.toml contents exactly when they
sit at depth <= 1.  ``extract_sdist_archive`` must never write outside the
target directory, whatever the member names are (``..``, absolute,
backslash, ``./.`` prefixes, symlink members); it either raises or
extracts safely.  ``_parse_file_url`` round-trips ``Path.as_uri()`` for
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
from nab_index.local_index import _parse_file_url

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


@st.composite
def benign_archives(draw: st.DrawFn) -> tuple[bytes, str | None, str | None]:
    """Archive plus the expected (pkg_info, pyproject) extraction result."""
    members: list[tuple[str, bytes | None]] = [("pkg-1.0", None)]
    expected_pkginfo: str | None = None
    expected_pyproject: str | None = None

    pkginfo_depth = draw(depths)
    if draw(st.booleans()):
        content = draw(texts)
        path = (
            "/".join(["pkg-1.0"] * pkginfo_depth + ["PKG-INFO"])
            if pkginfo_depth
            else "PKG-INFO"
        )
        members.append((path, content.encode()))
        if pkginfo_depth <= 1:
            expected_pkginfo = content

    pyproject_depth = draw(depths)
    if draw(st.booleans()):
        content = draw(texts)
        path = (
            "/".join(["pkg-1.0"] * pyproject_depth + ["pyproject.toml"])
            if pyproject_depth
            else "pyproject.toml"
        )
        members.append((path, content.encode()))
        if pyproject_depth <= 1:
            expected_pyproject = content

    # Decoys deeper in the tree must be ignored.
    if draw(st.booleans()):
        members.append(("pkg-1.0/sub/dir/PKG-INFO", b"DECOY"))
        members.append(("pkg-1.0/sub/dir/pyproject.toml", b"DECOY"))

    return make_targz(members), expected_pkginfo, expected_pyproject


@given(data=benign_archives())
@PROPERTY_SETTINGS
def test_extract_sdist_files_depth_rule(
    data: tuple[bytes, str | None, str | None],
) -> None:
    """PKG-INFO and pyproject.toml are picked up at depth <= 1 only."""
    archive, expected_pkginfo, expected_pyproject = data
    pkg_info, pyproject = _extract_sdist_files(archive)
    assert pkg_info == expected_pkginfo
    assert pyproject == expected_pyproject


@given(blob=st.binary(max_size=200))
@PROPERTY_SETTINGS
def test_extract_sdist_files_never_raises_on_garbage(blob: bytes) -> None:
    """Arbitrary bytes yield ``(None, None)``, never an exception."""
    assert _extract_sdist_files(blob) == (None, None)


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
    """``_parse_file_url`` inverts ``Path.as_uri`` for odd but legal paths."""
    path = Path("/", *segments)
    assert _parse_file_url(path.as_uri()) == path
