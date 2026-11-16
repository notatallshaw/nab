"""Hash-pinned imports reuse verified declarations and refuse unusable offline state."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from nab_project.dependency_sources import DependencySourceReader
from nab_provider.errors import SdistHashMismatchError


def _archive(files: dict[str, bytes]) -> bytes:
    """Build one source archive for download and integrity checks."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo("demo/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_online_import_and_offline_reuse_check_content_and_archive_hashes(
    tmp_path: Path,
) -> None:
    data = b'[project]\nname="demo"\n'
    payload = _archive({"pyproject.toml": data})
    declaration = {
        "url": "https://example.org/source.tar.gz",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return payload

    online = DependencySourceReader(tmp_path, tmp_path / "cache", fetch)
    assert online.bind(declaration)("pyproject.toml") == data.decode()
    assert calls == [declaration["url"]]
    calls.clear()
    offline = DependencySourceReader(tmp_path, tmp_path / "cache", fetch, offline=True)
    assert offline.bind(declaration)("pyproject.toml") == data.decode()
    assert calls == []
    (cache,) = (tmp_path / "cache").rglob("*.json")
    record = json.loads(cache.read_text())
    record["content"] = "modified"
    cache.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="not cached"):
        offline.bind(declaration)("pyproject.toml")
    assert calls == []


def test_hash_mismatch_does_not_publish_cached_declarations(tmp_path: Path) -> None:
    payload = _archive({"pyproject.toml": b'[project]\nname="wrong"\n'})
    reader = DependencySourceReader(tmp_path, tmp_path / "cache", lambda url: payload)
    with pytest.raises(SdistHashMismatchError, match="mismatch"):
        reader.bind({"url": "https://example.org/source.tar.gz", "sha256": "0" * 64})(
            "pyproject.toml"
        )
    assert not (tmp_path / "cache").exists()


def test_local_import_refuses_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (tmp_path / "secret").write_text("outside")
    (root / "pyproject.toml").symlink_to(tmp_path / "secret")
    reader = DependencySourceReader(tmp_path, tmp_path / "cache", lambda url: b"")
    with pytest.raises(ValueError, match="escapes"):
        reader.bind({"path": "source"})("pyproject.toml")


def test_two_declarations_share_one_download(tmp_path: Path) -> None:
    payload = _archive(
        {"pyproject.toml": b"root", "backend/pyproject.toml": b"backend"}
    )
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return payload

    reader = DependencySourceReader(tmp_path, tmp_path / "cache", fetch)
    read = reader.bind(
        {
            "url": "https://example.org/source.tar.gz",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    assert read("pyproject.toml") == "root"
    assert read("backend/pyproject.toml") == "backend"
    assert calls == ["https://example.org/source.tar.gz"]


def test_local_declarations_need_neither_network_nor_a_cache(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("local")
    reader = DependencySourceReader(
        tmp_path, tmp_path / "cache", lambda url: b"", offline=True
    )
    assert reader.bind({"path": "source"})("pyproject.toml") == "local"
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        pytest.param({"path": 1}, "path must be a string", id="non-string-path"),
        pytest.param(
            {"path": ".", "url": "https://example.org/source.tar.gz"},
            "cannot be combined",
            id="ambiguous-source",
        ),
        pytest.param({}, "need a URL", id="missing-source"),
        pytest.param(
            {"url": "https://example.org/source.tar.gz"},
            "need a URL",
            id="missing-hash",
        ),
        pytest.param({"url": 1, "sha256": "0" * 64}, "need a URL", id="non-string-url"),
        pytest.param(
            {"url": "https://example.org/source.tar.gz", "sha256": "not-a-hash"},
            "hexadecimal",
            id="invalid-hash",
        ),
    ],
)
def test_invalid_source_declarations_do_not_fetch(
    tmp_path: Path, declaration: dict[str, object], message: str
) -> None:
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b""

    reader = DependencySourceReader(tmp_path, tmp_path / "cache", fetch)
    with pytest.raises((ValueError, TypeError), match=message):
        reader.bind(declaration)
    assert not calls


def test_url_fragments_cannot_silently_change_the_source_selection(
    tmp_path: Path,
) -> None:
    reader = DependencySourceReader(tmp_path, tmp_path / "cache", lambda url: b"")
    with pytest.raises(ValueError, match="URL fragments"):
        reader.bind(
            {
                "url": "https://example.org/source.tar.gz#subdirectory=backend",
                "sha256": "0" * 64,
            }
        )


def test_hash_case_does_not_change_source_identity(tmp_path: Path) -> None:
    payload = _archive({"pyproject.toml": b"source"})
    reader = DependencySourceReader(tmp_path, tmp_path / "cache", lambda url: payload)
    digest = hashlib.sha256(payload).hexdigest()
    assert (
        reader.bind(
            {"url": "https://example.org/source.tar.gz", "sha256": digest.upper()}
        )("pyproject.toml")
        == "source"
    )


def test_offline_errors_redact_source_credentials(tmp_path: Path) -> None:
    reader = DependencySourceReader(
        tmp_path, tmp_path / "cache", lambda url: b"", offline=True
    )
    read = reader.bind(
        {"url": "https://user:secret@example.org/source.tar.gz", "sha256": "0" * 64}
    )
    with pytest.raises(ValueError, match="not cached") as error:
        read("pyproject.toml")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"not-json", id="invalid-json"),
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(b"[]", id="non-table-record"),
        pytest.param(b'{"content": 1}', id="non-string-content"),
        pytest.param(b'{"content": ' + b"9" * 10_000 + b"}", id="oversized-integer"),
    ],
)
def test_unusable_cached_records_remain_offline(tmp_path: Path, data: bytes) -> None:
    cache = tmp_path / "cache"
    relative = "pyproject.toml"
    record = (
        cache
        / "dependency-inputs-v1"
        / ("0" * 64)
        / (hashlib.sha256(relative.encode()).hexdigest() + ".json")
    )
    record.parent.mkdir(parents=True)
    record.write_bytes(data)
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b""

    reader = DependencySourceReader(tmp_path, cache, fetch, offline=True)
    read = reader.bind({"url": "https://example.org/source.tar.gz", "sha256": "0" * 64})
    with pytest.raises(ValueError, match="not cached"):
        read(relative)
    assert not calls


def test_uncached_archive_reads_stay_in_memory(tmp_path: Path) -> None:
    payload = _archive(
        {"pyproject.toml": b"root", "backend/pyproject.toml": b"backend"}
    )
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return payload

    reader = DependencySourceReader(tmp_path, None, fetch)
    read = reader.bind(
        {
            "url": "https://example.org/source.tar.gz",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    assert read("pyproject.toml") == "root"
    assert read("backend/pyproject.toml") == "backend"
    assert len(calls) == 1
    assert not list(tmp_path.iterdir())


def test_read_two_group_selections_from_one_source(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\ntest=["pytest"]\ndocs=["sphinx"]\n'
    )
    reader = DependencySourceReader(tmp_path, None, lambda url: b"", offline=True)
    source = {"path": "."}
    for group, expected in [("test", "pytest"), ("docs", "sphinx")]:
        assert reader.requirements(source, {"groups": [group]}) == [expected]


@pytest.mark.parametrize(
    ("source", "selection"),
    [({"path": ".", "groups": []}, {}), ({"path": "."}, {"unknown": []})],
)
def test_refuse_unrecognized_input_fields(
    tmp_path: Path, source: dict[str, object], selection: dict[str, object]
) -> None:
    reader = DependencySourceReader(tmp_path, None, lambda url: b"")
    with pytest.raises(ValueError, match="unknown.*keys"):
        reader.requirements(source, selection)


def test_multi_project_inputs_cannot_also_select_the_root(tmp_path: Path) -> None:
    reader = DependencySourceReader(tmp_path, None, lambda url: b"")
    with pytest.raises(ValueError, match="root project"):
        reader.requirements({"path": "."}, {"groups": ["dev"], "projects": []})
