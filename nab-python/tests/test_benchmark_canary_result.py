"""Tests for canary result and selected-pin persistence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

import pytest

_CONTRACT = Path(__file__).resolve().parents[1] / "benchmarks" / "canary_result.py"


@lru_cache(maxsize=1)
def _harness() -> ModuleType:
    """Load the persistence contract once for this test module."""
    spec = importlib.util.spec_from_file_location("_benchmark_canary_result", _CONTRACT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_CONTRACT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_CONTRACT.parent))
    return module


def _run(*, success: bool = True, version: str = "1.0") -> dict:
    pins = {"demo": version} if success else {}
    return {
        "settings": {"resolution": "highest"},
        "success": success,
        "error": None if success else "resolution failed",
        "pins": pins,
        "decisions": 1,
        "conflicts": 0,
        "backjumps": 0,
        "restarts": 0,
        "incompatibilities_learned": 0,
        "metadata_fetched": 0,
        "distributions_seen": 0,
        "look_ahead_rejections": 0,
        "packages": len(pins),
        "wall_time_seconds": 0.0,
    }


def _record(*, success: bool = True) -> dict:
    """Return one complete internal record for persistence tests."""
    run = _run(success=success)
    return {
        "contract_version": 2,
        "scenario": "quick:example",
        "source": {"commit": "a" * 40, "dirty": False, "diff_hash": None},
        "input_hash": "b" * 64,
        "execution_hash": "c" * 64,
        "input": {"requirements": ["demo"]},
        "effective_settings": run["settings"],
        "runs": [run],
        "summary": {},
    }


def test_pin_sidecar_preserves_the_v2_result_bytes() -> None:
    module = _harness()
    record = _record()
    artifacts = module.build_canary_artifacts(
        {"example": record},
        "canary_123.json",
    )

    result_data = json.loads(artifacts.result)
    pins_data = json.loads(artifacts.pins)
    expected_run = {
        name: value for name, value in record["runs"][0].items() if name != "pins"
    }
    expected_text = (
        json.dumps(
            {"example": {**record, "runs": [expected_run]}},
            indent=2,
        )
        + "\n"
    )

    assert artifacts.result == expected_text
    assert result_data["example"]["contract_version"] == 2
    assert set(result_data["example"]["runs"][0]) == module.CANARY_RUN_FIELDS
    assert pins_data["scenarios"]["example"]["runs"] == [
        {
            "run": 0,
            "success": True,
            "packages": 1,
            "pins": {"demo": "1.0"},
        }
    ]
    assert pins_data["canary_result"] == {
        "filename": "canary_123.json",
        "sha256": hashlib.sha256(artifacts.result.encode()).hexdigest(),
    }
    assert Path("canary_123.json").match("canary_*.json")
    assert not Path("canary-pins_123.json").match("canary_*.json")


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("canary_result", "sha256"), "0" * 64, "identify"),
        (("scenarios", "example", "input_hash"), "0" * 64, "identity"),
        (("scenarios", "example", "execution_hash"), "0" * 64, "identity"),
        (("scenarios", "example", "runs"), [], "runs do not match"),
    ],
)
def test_pin_sidecar_rejects_pairing_changes(
    path: tuple[str, ...],
    value: object,
    match: str,
) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    pins_data = json.loads(artifacts.pins)
    target = pins_data
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value

    with pytest.raises(ValueError, match=match):
        module.validate_canary_pins_artifact(
            artifacts.result,
            pins_data,
            result_filename="canary_123.json",
        )


@pytest.mark.parametrize(
    "result_filename",
    ["../canary_123.json", "canary-pins_123.json", "canary_123.json/other"],
)
def test_pin_sidecar_rejects_result_path_tricks(result_filename: str) -> None:
    module = _harness()

    with pytest.raises(ValueError, match="comparison filename"):
        module.build_canary_artifacts(
            {"example": _record()},
            result_filename,
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("run", 1, "run order"),
        ("success", False, "does not match"),
        ("packages", True, "invalid canary pin run"),
        ("packages", 2, "does not match"),
        ("pins", {"demo": "01.0"}, "invalid canary pin run"),
    ],
)
def test_pin_sidecar_rejects_run_changes(
    field: str,
    value: object,
    match: str,
) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    pins_data = json.loads(artifacts.pins)
    pins_data["scenarios"]["example"]["runs"][0][field] = value

    with pytest.raises(ValueError, match=match):
        module.validate_canary_pins_artifact(
            artifacts.result,
            pins_data,
            result_filename="canary_123.json",
        )


def test_pin_sidecar_rejects_reordered_runs() -> None:
    module = _harness()
    record = _record()
    record["runs"].append(_run(version="2.0"))
    artifacts = module.build_canary_artifacts(
        {"example": record},
        "canary_123.json",
    )
    pins_data = json.loads(artifacts.pins)
    pins_data["scenarios"]["example"]["runs"].reverse()

    with pytest.raises(ValueError, match="run order"):
        module.validate_canary_pins_artifact(
            artifacts.result,
            pins_data,
            result_filename="canary_123.json",
        )


@pytest.mark.parametrize("packages", [True, 1.0])
def test_pin_sidecar_rejects_noninteger_result_package_count(
    packages: object,
) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    result_data = json.loads(artifacts.result)
    result_data["example"]["runs"][0]["packages"] = packages
    result_text = json.dumps(result_data, indent=2) + "\n"
    pins_data = json.loads(artifacts.pins)
    pins_data["canary_result"]["sha256"] = hashlib.sha256(
        result_text.encode()
    ).hexdigest()

    with pytest.raises(ValueError, match="invalid canary comparison run"):
        module.validate_canary_pins_artifact(
            result_text,
            pins_data,
            result_filename="canary_123.json",
        )


def test_pin_sidecar_rejects_a_non_v2_comparison_record() -> None:
    module = _harness()
    record = _record()
    record["contract_version"] = 3

    with pytest.raises(ValueError, match="comparison record"):
        module.build_canary_artifacts(
            {"example": record},
            "canary_123.json",
        )


def test_artifacts_publish_the_sidecar_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    result_path = tmp_path / "canary_123.json"
    pins_path = tmp_path / "canary-pins_123.json"
    published: list[str] = []
    real_link = module.os.link

    def link(source: Path, destination: Path) -> None:
        published.append(Path(destination).name)
        real_link(source, destination)

    monkeypatch.setattr(module.os, "link", link)
    module.write_canary_artifacts(result_path, pins_path, artifacts)

    assert published == [pins_path.name, result_path.name]
    assert result_path.read_bytes() == artifacts.result.encode()
    assert pins_path.read_bytes() == artifacts.pins.encode()
    assert not list(tmp_path.glob(".canary-artifacts-*"))


def test_artifacts_keep_the_default_new_file_mode(tmp_path: Path) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    reference_path = tmp_path / "reference.json"
    result_path = tmp_path / "canary_123.json"
    pins_path = tmp_path / "canary-pins_123.json"
    reference_path.write_text("{}\n")

    module.write_canary_artifacts(result_path, pins_path, artifacts)

    expected_mode = stat.S_IMODE(reference_path.stat().st_mode)
    assert stat.S_IMODE(result_path.stat().st_mode) == expected_mode
    assert stat.S_IMODE(pins_path.stat().st_mode) == expected_mode


def test_artifacts_sync_files_and_published_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    result_path = tmp_path / "canary_123.json"
    pins_path = tmp_path / "canary-pins_123.json"
    calls: list[str] = []
    real_fsync = module.os.fsync
    real_link = module.os.link

    def fsync(descriptor: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        calls.append(f"sync:{kind}")
        real_fsync(descriptor)

    def link(source: Path, destination: Path) -> None:
        calls.append(f"link:{Path(destination).name}")
        real_link(source, destination)

    monkeypatch.setattr(module.os, "fsync", fsync)
    monkeypatch.setattr(module.os, "link", link)
    module.write_canary_artifacts(result_path, pins_path, artifacts)

    expected_calls = [
        "sync:file",
        "sync:file",
        f"link:{pins_path.name}",
    ]
    if module._DIRECTORY_FSYNC_SUPPORTED:
        expected_calls.append("sync:directory")
    expected_calls.append(f"link:{result_path.name}")
    if module._DIRECTORY_FSYNC_SUPPORTED:
        expected_calls.extend(["sync:directory", "sync:directory"])
    assert calls == expected_calls


def test_directory_sync_has_a_platform_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()

    def unexpected_open(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("directory open should be skipped")

    monkeypatch.setattr(module, "_DIRECTORY_FSYNC_SUPPORTED", False)
    monkeypatch.setattr(module.os, "open", unexpected_open)

    module._sync_directory(tmp_path)


@pytest.mark.parametrize(
    ("damaged", "match"),
    [("result", "serialized"), ("pins", "artifact shape")],
)
def test_artifact_writer_validates_both_payloads_before_publication(
    tmp_path: Path,
    damaged: str,
    match: str,
) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    if damaged == "result":
        artifacts = module.CanaryArtifacts(
            result=artifacts.result + "not json\n",
            pins=artifacts.pins,
        )
    else:
        artifacts = module.CanaryArtifacts(
            result=artifacts.result,
            pins="{}\n",
        )

    with pytest.raises(ValueError, match=match):
        module.write_canary_artifacts(
            tmp_path / "canary_123.json",
            tmp_path / "canary-pins_123.json",
            artifacts,
        )

    assert not list(tmp_path.iterdir())


def test_second_stage_failure_cleans_private_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    real_stage = module._stage_artifact

    def stage(path: Path, text: str) -> None:
        if path.name == "canary-pins_123.json":
            raise OSError("sidecar staging failed")
        real_stage(path, text)

    monkeypatch.setattr(module, "_stage_artifact", stage)

    with pytest.raises(OSError, match="sidecar staging failed"):
        module.write_canary_artifacts(
            tmp_path / "canary_123.json",
            tmp_path / "canary-pins_123.json",
            artifacts,
        )

    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("existing", ["result", "pins"])
def test_artifacts_reject_existing_targets(tmp_path: Path, existing: str) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    result_path = tmp_path / "canary_123.json"
    pins_path = tmp_path / "canary-pins_123.json"
    existing_path = result_path if existing == "result" else pins_path
    other_path = pins_path if existing == "result" else result_path
    existing_path.write_text("existing\n")

    with pytest.raises(FileExistsError, match="already exists"):
        module.write_canary_artifacts(result_path, pins_path, artifacts)

    assert existing_path.read_text() == "existing\n"
    assert not other_path.exists()
    assert not list(tmp_path.glob(".canary-artifacts-*"))


def test_sidecar_publish_failure_leaves_no_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    result_path = tmp_path / "canary_123.json"
    pins_path = tmp_path / "canary-pins_123.json"
    real_link = module.os.link

    def link(source: Path, destination: Path) -> None:
        if Path(destination) == pins_path:
            raise OSError("sidecar publish failed")
        real_link(source, destination)

    monkeypatch.setattr(module.os, "link", link)

    with pytest.raises(OSError, match="sidecar publish failed"):
        module.write_canary_artifacts(result_path, pins_path, artifacts)

    assert not result_path.exists()
    assert not pins_path.exists()
    assert not list(tmp_path.glob(".canary-artifacts-*"))


def test_publish_rejects_a_target_created_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    result_path = tmp_path / "canary_123.json"
    pins_path = tmp_path / "canary-pins_123.json"
    real_link = module.os.link

    def link(source: Path, destination: Path) -> None:
        if Path(destination) == pins_path:
            pins_path.write_text("raced\n")
        real_link(source, destination)

    monkeypatch.setattr(module.os, "link", link)

    with pytest.raises(FileExistsError):
        module.write_canary_artifacts(result_path, pins_path, artifacts)

    assert not result_path.exists()
    assert pins_path.read_text() == "raced\n"
    assert not list(tmp_path.glob(".canary-artifacts-*"))


def test_primary_publish_failure_leaves_bound_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )
    result_path = tmp_path / "canary_123.json"
    pins_path = tmp_path / "canary-pins_123.json"
    real_link = module.os.link

    def link(source: Path, destination: Path) -> None:
        if Path(destination) == result_path:
            raise OSError("primary publish failed")
        real_link(source, destination)

    monkeypatch.setattr(module.os, "link", link)

    with pytest.raises(OSError, match="primary publish failed"):
        module.write_canary_artifacts(result_path, pins_path, artifacts)

    assert not result_path.exists()
    assert pins_path.read_bytes() == artifacts.pins.encode()
    assert not list(tmp_path.glob(".canary-artifacts-*"))


def test_artifacts_reject_mismatched_paths(tmp_path: Path) -> None:
    module = _harness()
    artifacts = module.build_canary_artifacts(
        {"example": _record()},
        "canary_123.json",
    )

    with pytest.raises(ValueError, match="artifact paths"):
        module.write_canary_artifacts(
            tmp_path / "canary_123.json",
            tmp_path / "other" / "canary-pins_123.json",
            artifacts,
        )


@pytest.mark.parametrize(
    "pins",
    [
        {"Demo": "1.0"},
        {"demo/": "1.0"},
        {"demo": "01.0"},
        {"demo[extra]": "1.0"},
        {"z-package": "1.0", "a-package": "1.0"},
    ],
)
def test_pin_sidecar_rejects_noncanonical_pins(pins: dict[str, str]) -> None:
    module = _harness()
    record = _record()
    record["runs"][0]["pins"] = pins

    with pytest.raises(ValueError, match="invalid selected pins"):
        module.build_canary_artifacts(
            {"example": record},
            "canary_123.json",
        )


@pytest.mark.parametrize("packages", [True, 2])
def test_pin_sidecar_rejects_invalid_package_count(packages: object) -> None:
    module = _harness()
    record = _record()
    record["runs"][0]["packages"] = packages

    with pytest.raises(ValueError, match="invalid selected pins"):
        module.build_canary_artifacts(
            {"example": record},
            "canary_123.json",
        )


def test_pin_sidecar_rejects_pins_from_a_failed_run() -> None:
    module = _harness()
    record = _record(success=False)
    record["runs"][0]["packages"] = 1
    record["runs"][0]["pins"] = {"demo": "1.0"}

    with pytest.raises(ValueError, match="failed canary run"):
        module.build_canary_artifacts(
            {"example": record},
            "canary_123.json",
        )
