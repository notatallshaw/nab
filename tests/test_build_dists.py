"""Tests for the distribution build helpers in tasks/build_dists.py."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import urllib.error
import zipfile
from email.message import Message
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tasks" / "build_dists.py"
_spec = importlib.util.spec_from_file_location("nab_build_dists", _PATH)
assert _spec is not None
assert _spec.loader is not None
build_dists = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_dists)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the published-file verifier with its real release helpers."""
    monkeypatch.syspath_prepend(str(_PATH.parent))
    monkeypatch.setitem(sys.modules, "build_dists", build_dists)
    path = _PATH.with_name("verify_published.py")
    spec = importlib.util.spec_from_file_location("nab_verify_published", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wheel_tree(tmp_path: Path) -> tuple[Path, list[Path]]:
    """Write one metadata-bearing wheel for every release package."""
    wheels: list[Path] = []
    for package in build_dists.PACKAGES:
        stem = package.replace("-", "_")
        directory = tmp_path / package
        directory.mkdir()
        wheel = directory / f"{stem}-0.0.3-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                f"{stem}-0.0.3.dist-info/METADATA",
                "Name: " + package + "\nVersion: 0.0.3\n",
            )
        wheels.append(wheel)
    return tmp_path, wheels


def test_source_dir_maps_umbrella_to_root() -> None:
    assert build_dists.source_dir("nab") == build_dists.REPO_ROOT
    assert (
        build_dists.source_dir("nab-resolver") == build_dists.REPO_ROOT / "nab-resolver"
    )


def test_artifact_hashes_keep_package_directories_and_skip_other_files(
    tmp_path: Path,
) -> None:
    pkg = tmp_path / "nab"
    pkg.mkdir()
    (pkg / "nab-0.0.3-py3-none-any.whl").write_bytes(b"wheel")
    (pkg / "nab-0.0.3.tar.gz").write_bytes(b"sdist")
    (pkg / "notes.txt").write_bytes(b"ignore me")

    other = tmp_path / "nab-index"
    other.mkdir()
    (other / "nab-0.0.3.tar.gz").write_bytes(b"sdist")

    wheel = hashlib.sha256(b"wheel").hexdigest()
    sdist = hashlib.sha256(b"sdist").hexdigest()
    assert build_dists._artifact_hashes(tmp_path) == {
        Path("nab/nab-0.0.3-py3-none-any.whl"): wheel,
        Path("nab/nab-0.0.3.tar.gz"): sdist,
        Path("nab-index/nab-0.0.3.tar.gz"): sdist,
    }


def test_smoke_installs_every_built_wheel_and_checks_both_entries(
    wheel_tree: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    dist, wheels = wheel_tree
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> str:
        calls.append(command)
        return "nab 0.0.3\n" if "--version" in command else ""

    monkeypatch.setattr(build_dists, "_run", run)
    monkeypatch.setattr(
        build_dists.shutil, "which", lambda name, *, path: str(Path(path) / name)
    )
    build_dists.smoke_test(dist)

    installs = [
        command
        for command in calls
        if command[1:4] == ["-m", "pip", "install"] and "--upgrade" not in command
    ]
    assert len(installs) == 1
    assert installs[0][4:] == [str(wheel) for wheel in wheels]

    assert any(
        command[1:]
        == ["-I", str(_PATH.with_name("check_installed.py")), *build_dists.PACKAGES]
        for command in calls
    )

    versions = [command for command in calls if command[-1] == "--version"]
    assert len(versions) == 2
    assert versions[0][1:] == ["-m", "nab", "--version"]
    assert Path(versions[1][0]).name == "nab"
    assert any(command[-1] == "--help" for command in calls)


def test_installed_check_requires_the_package_typing_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "typed_fixture"
    package.mkdir()
    (package / "__init__.py").touch()
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    def check() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - The script and fixture name are fixed.
            [
                sys.executable,
                str(_PATH.with_name("check_installed.py")),
                "typed-fixture",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    missing = check()
    assert missing.returncode == 1
    assert "typed_fixture/py.typed" in missing.stderr

    (package / "py.typed").touch()
    check().check_returncode()


@pytest.mark.parametrize("entry", ["module", "console"])
@pytest.mark.parametrize("reported", ["0.0.4", "0.0.30"])
def test_smoke_rejects_a_wrong_installed_version(
    wheel_tree: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    reported: str,
) -> None:
    dist, _ = wheel_tree

    def run(command: list[str], **kwargs: object) -> str:
        selected = "module" if command[1:3] == ["-m", "nab"] else "console"
        return f"nab {reported if selected == entry else '0.0.3'}"

    monkeypatch.setattr(build_dists, "_run", run)
    monkeypatch.setattr(
        build_dists.shutil, "which", lambda name, *, path: str(Path(path) / name)
    )
    with pytest.raises(SystemExit, match="expected.*0.0.3"):
        build_dists.smoke_test(dist)


@pytest.mark.parametrize("copies", [0, 2])
def test_smoke_requires_one_wheel_for_every_package(
    wheel_tree: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    dist, wheels = wheel_tree
    wheel = wheels[0]
    if copies == 0:
        wheel.unlink()
    else:
        wheel.with_name("other.whl").write_bytes(wheel.read_bytes())
    calls: list[list[str]] = []
    monkeypatch.setattr(
        build_dists, "_run", lambda command, **kwargs: calls.append(command) or ""
    )
    with pytest.raises(SystemExit, match="exactly one wheel"):
        build_dists.smoke_test(dist)
    assert calls == []


@pytest.mark.parametrize("copies", [0, 2])
def test_published_verification_requires_one_local_artifact(
    tmp_path: Path, published: ModuleType, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    filename = "nab-0.0.3.tar.gz"
    for index in range(copies):
        directory = tmp_path / str(index)
        directory.mkdir()
        (directory / filename).write_bytes(b"built")

    calls: list[str] = []
    monkeypatch.setattr(
        published, "_verify_attestation", lambda *args: calls.append("attestation")
    )
    monkeypatch.setattr(
        published,
        "_published_sha256",
        lambda *args: hashlib.sha256(b"built").hexdigest(),
    )
    with pytest.raises(published.VerificationError, match="exactly one"):
        published._check_once("nab", "0.0.3", filename, "repository", tmp_path)
    assert calls == []


def test_release_checks_all_local_artifacts_before_remote_work(
    tmp_path: Path, published: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = published.expected_files("0.0.3")
    for _, filename in files[:-1]:
        (tmp_path / filename).write_bytes(b"built")
    calls: list[str] = []
    monkeypatch.setattr(
        published, "_verify_attestation", lambda *args: calls.append("attestation")
    )
    monkeypatch.setattr(
        published,
        "_published_sha256",
        lambda *args: hashlib.sha256(b"built").hexdigest(),
    )
    with pytest.raises(SystemExit, match="exactly one"):
        published.verify_release("0.0.3", tmp_path)
    assert calls == []


def test_published_verification_preserves_attestation_only_mode(
    published: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        published, "_verify_attestation", lambda *args: calls.append(args)
    )
    result = published._check_once(
        "nab", "0.0.3", "nab-0.0.3.tar.gz", "repository", None
    )
    assert result == "attestation OK"
    assert calls == [("nab-0.0.3.tar.gz", "repository")]


@pytest.mark.parametrize("matches", [False, True])
def test_published_verification_compares_the_built_bytes(
    tmp_path: Path,
    published: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    matches: bool,
) -> None:
    filename = "nab-0.0.3.tar.gz"
    (tmp_path / filename).write_bytes(b"built")
    monkeypatch.setattr(published, "_verify_attestation", lambda *args: None)
    payload = b"built" if matches else b"different"
    monkeypatch.setattr(
        published,
        "_published_sha256",
        lambda *args: hashlib.sha256(payload).hexdigest(),
    )
    if matches:
        assert (
            published._check_once("nab", "0.0.3", filename, "repository", tmp_path)
            == "attestation OK; published bytes match built bytes"
        )
    else:
        with pytest.raises(published.VerificationError, match="published bytes differ"):
            published._check_once("nab", "0.0.3", filename, "repository", tmp_path)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_archive_sizes_count_member_bytes(tmp_path: Path, kind: str) -> None:
    payload = b"archive member\n"
    if kind == "wheel":
        path = tmp_path / "demo-1-py3-none-any.whl"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("demo.py", payload)
    else:
        path = tmp_path / "demo-1.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo("demo-1/demo.py")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    measured = build_dists.archive_size(path)
    assert measured.filename == path.name
    assert measured.compressed_bytes == path.stat().st_size
    assert measured.unpacked_bytes == len(payload)


def test_size_summary_reports_numeric_rows_and_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel = tmp_path / "demo-1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("demo.py", b"abc")
    directory = tmp_path / "nested"
    directory.mkdir()
    other = directory / "other-1-py3-none-any.whl"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("other.py", b"12345")
    summary = tmp_path / "summary.md"
    summary.write_bytes(b"existing\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    build_dists.report_sizes(tmp_path)

    expected = [
        (wheel.stat().st_size, 3),
        (other.stat().st_size, 5),
        (wheel.stat().st_size + other.stat().st_size, 8),
    ]
    for rendered in (capsys.readouterr().out, summary.read_text()):
        rows = [
            line.split("|") for line in rendered.splitlines() if line.startswith("|")
        ]
        numeric = [
            (int(row[2]), int(row[3])) for row in rows if row[2].strip().isdigit()
        ]
        assert numeric == expected
    assert summary.read_bytes().startswith(b"existing\n")


def _json_response(entries: list[tuple[str, str | None]]) -> io.BytesIO:
    """Return a real response stream with PyPI's file-digest shape."""
    return io.BytesIO(
        json.dumps(
            {
                "urls": [
                    {"filename": filename, "digests": {"sha256": digest}}
                    for filename, digest in entries
                ]
            }
        ).encode()
    )


def test_release_reuses_package_json_but_not_across_runs(
    tmp_path: Path, published: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = published.expected_files("0.0.3rc1")
    digest = hashlib.sha256(b"built").hexdigest()
    for _, filename in files:
        (tmp_path / filename).write_bytes(b"built")
    requests: list[str] = []
    attestations: list[str] = []

    def urlopen(url: str, *, timeout: int) -> io.BytesIO:
        requests.append(url)
        name = url.split("/")[-3]
        return _json_response(
            [(filename, digest) for package, filename in files if package == name]
        )

    monkeypatch.setattr(published.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        published,
        "_verify_attestation",
        lambda filename, repository: attestations.append(filename),
    )
    monkeypatch.setattr(
        sys, "argv", ["verify_published", "v0.0.3-rc1", "--dist-dir", str(tmp_path)]
    )
    published.main()
    assert len(requests) == len(build_dists.PACKAGES)
    assert attestations == [filename for _, filename in files]

    published.verify_release("0.0.3-rc1", tmp_path)
    assert len(requests) == 2 * len(build_dists.PACKAGES)
    assert len(attestations) == 2 * len(files)


def test_missing_file_refreshes_json_on_every_retry(
    tmp_path: Path, published: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdist, wheel = "nab-0.0.3.tar.gz", "nab-0.0.3-py3-none-any.whl"
    digest = hashlib.sha256(b"built").hexdigest()
    (tmp_path / wheel).write_bytes(b"built")
    snapshots: list[list[tuple[str, str | None]]] = [
        [(sdist, digest)],
        [(sdist, digest)],
        [(sdist, digest), (wheel, digest)],
    ]
    requests: list[str] = []
    waits: list[float] = []

    def urlopen(url: str, *, timeout: int) -> io.BytesIO:
        requests.append(url)
        return _json_response(snapshots[len(requests) - 1])

    monkeypatch.setattr(published.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(published, "_verify_attestation", lambda *args: None)
    clock = iter([0.0, 20.0])
    monkeypatch.setattr(published.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(published.time, "sleep", waits.append)
    cache = {}
    assert published._published_sha256("nab", "0.0.3", sdist, cache) == digest
    published._check_file(
        "nab", "0.0.3", wheel, "repository", tmp_path, 20.0, digests=cache
    )
    assert len(requests) == 3
    assert waits == [15.0]


@pytest.mark.parametrize("failure", ["404", "network"])
def test_transient_json_failures_are_not_cached(
    published: ModuleType, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    calls = []

    def urlopen(url: str, *, timeout: int) -> io.BytesIO:
        calls.append(url)
        if len(calls) == 1:
            if failure == "404":
                raise urllib.error.HTTPError(
                    url, 404, "pending", Message(), io.BytesIO()
                )
            raise urllib.error.URLError("offline")
        return _json_response([("file.whl", "digest")])

    monkeypatch.setattr(published.urllib.request, "urlopen", urlopen)
    cache = {}
    with pytest.raises(published.NotYetAvailableError):
        published._published_sha256("nab", "0.0.3", "file.whl", cache)
    assert cache == {}
    assert published._published_sha256("nab", "0.0.3", "file.whl", cache) == "digest"
    assert len(calls) == 2


@pytest.mark.parametrize("digest", [None, "wrong"])
def test_invalid_published_digest_never_waits(
    tmp_path: Path,
    published: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    digest: str | None,
) -> None:
    filename = "nab-0.0.3.tar.gz"
    (tmp_path / filename).write_bytes(b"built")
    monkeypatch.setattr(
        published.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _json_response([(filename, digest)]),
    )
    monkeypatch.setattr(published, "_verify_attestation", lambda *args: None)
    waits = []
    monkeypatch.setattr(published.time, "sleep", waits.append)
    with pytest.raises(published.VerificationError):
        published._check_file(
            "nab", "0.0.3", filename, "repository", tmp_path, float("inf"), digests={}
        )
    assert waits == []


def test_pending_json_still_obeys_the_shared_deadline(
    tmp_path: Path, published: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    filename = "nab-0.0.3.tar.gz"
    (tmp_path / filename).write_bytes(b"built")
    monkeypatch.setattr(
        published.urllib.request, "urlopen", lambda *args, **kwargs: _json_response([])
    )
    monkeypatch.setattr(published, "_verify_attestation", lambda *args: None)
    times = iter([0.0, 15.0, 20.0])
    monkeypatch.setattr(published.time, "monotonic", lambda: next(times))
    waits = []
    monkeypatch.setattr(published.time, "sleep", waits.append)
    with pytest.raises(published.VerificationError, match="retry budget"):
        published._check_file(
            "nab", "0.0.3", filename, "repository", tmp_path, 20.0, digests={}
        )
    assert waits == [15.0, 5.0]
