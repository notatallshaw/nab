"""Tests for the distribution build helpers in tasks/build_dists.py."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "tasks" / "build_dists.py"
_spec = importlib.util.spec_from_file_location("nab_build_dists", _PATH)
assert _spec is not None
assert _spec.loader is not None
build_dists = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_dists)


def test_source_dir_maps_umbrella_to_root() -> None:
    assert build_dists.source_dir("nab") == build_dists.REPO_ROOT
    assert (
        build_dists.source_dir("nab-resolver") == build_dists.REPO_ROOT / "nab-resolver"
    )


def test_artifact_hashes_picks_only_distributions(tmp_path: Path) -> None:
    pkg = tmp_path / "nab"
    pkg.mkdir()
    (pkg / "nab-0.0.3-py3-none-any.whl").write_bytes(b"wheel")
    (pkg / "nab-0.0.3.tar.gz").write_bytes(b"sdist")
    (pkg / "notes.txt").write_bytes(b"ignore me")

    hashes = build_dists._artifact_hashes(tmp_path)

    assert set(hashes) == {"nab-0.0.3-py3-none-any.whl", "nab-0.0.3.tar.gz"}
    assert hashes["nab-0.0.3.tar.gz"] == hashlib.sha256(b"sdist").hexdigest()
