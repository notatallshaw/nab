"""Read declarations from real archive bytes without extracting their payloads."""

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from nab_index.archive_files import read_archive_files
from nab_index.client import extract_sdist_archive


def _tar(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.parametrize("prefix", ["", "project-1/"])
def test_read_wrapped_and_unwrapped_tar(prefix: str) -> None:
    data = _tar(
        {
            prefix + "pyproject.toml": "[project]",
            prefix + "tests/requirements.txt": "pytest",
        }
    )
    assert read_archive_files(data, ["pyproject.toml", "tests/requirements.txt"]) == {
        "pyproject.toml": "[project]",
        "tests/requirements.txt": "pytest",
    }


def test_a_single_enclosing_directory_becomes_the_source_root() -> None:
    assert read_archive_files(
        _tar({"tests/requirements.txt": "pytest"}), ["requirements.txt"]
    ) == {"requirements.txt": "pytest"}


def test_source_root_is_independent_of_the_requested_subproject() -> None:
    data = _tar(
        {"backend/pyproject.toml": "root", "backend/backend/pyproject.toml": "nested"}
    )
    assert read_archive_files(data, ["pyproject.toml"]) == {"pyproject.toml": "root"}
    assert read_archive_files(data, ["backend/pyproject.toml"]) == {
        "backend/pyproject.toml": "nested"
    }


def test_zip_declarations_are_read_without_extracting() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("project/", "")
        archive.writestr("project/pyproject.toml", "[project]")
    assert read_archive_files(buffer.getvalue(), ["pyproject.toml"]) == {
        "pyproject.toml": "[project]"
    }


@pytest.mark.parametrize(
    "name",
    [
        "../pyproject.toml",
        "/pyproject.toml",
        "C:/pyproject.toml",
        "project\\pyproject.toml",
    ],
)
def test_unsafe_archive_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe source path"):
        read_archive_files(_tar({name: "bad"}), ["pyproject.toml"])


def test_tar_symlink_is_not_followed() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("project/pyproject.toml")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        archive.addfile(info)
    with pytest.raises(ValueError, match="not a regular file"):
        read_archive_files(buffer.getvalue(), ["pyproject.toml"])


def test_zip_symlink_is_not_followed() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("project/pyproject.toml")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "../../outside")
    with pytest.raises(ValueError, match="symbolic link"):
        read_archive_files(buffer.getvalue(), ["pyproject.toml"])


def test_duplicate_archive_names_are_refused() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for text in [b"first", b"second"]:
            info = tarfile.TarInfo("project/pyproject.toml")
            info.size = len(text)
            archive.addfile(info, io.BytesIO(text))
    with pytest.raises(ValueError, match="duplicate archive path"):
        read_archive_files(buffer.getvalue(), ["pyproject.toml"])


def test_archive_file_must_have_a_name() -> None:
    with pytest.raises(ValueError, match="no file name"):
        read_archive_files(_tar({".": "not a file name"}), ["pyproject.toml"])


def test_empty_directories_participate_in_root_selection() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in [".", "empty"]:
            entry = tarfile.TarInfo(name)
            entry.type = tarfile.DIRTYPE
            archive.addfile(entry)
        entry = tarfile.TarInfo("project/pyproject.toml")
        entry.size = 4
        archive.addfile(entry, io.BytesIO(b"data"))
    assert read_archive_files(buffer.getvalue(), ["project/pyproject.toml"]) == {
        "project/pyproject.toml": "data"
    }


@pytest.mark.parametrize(
    ("prefix", "extra_directory"),
    [
        pytest.param("", False, id="flat"),
        pytest.param("project/", False, id="enclosing-directory"),
        pytest.param("backend/", False, id="root-and-subproject-share-a-name"),
        pytest.param("project/", True, id="empty-sibling-directory"),
    ],
)
def test_selected_files_agree_with_the_extracted_source_root(
    tmp_path: Path,
    prefix: str,
    *,
    extra_directory: bool,
) -> None:
    files = {
        prefix + "pyproject.toml": "root",
        prefix + "backend/pyproject.toml": "nested",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
        if extra_directory:
            archive.writestr("empty/", "")
    data = buffer.getvalue()
    root = extract_sdist_archive(data, tmp_path)
    for path in files:
        relative = Path(path).relative_to(root.relative_to(tmp_path)).as_posix()
        assert read_archive_files(data, [relative]) == {
            relative: (root / relative).read_text()
        }
