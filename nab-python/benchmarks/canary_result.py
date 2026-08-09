"""Persist canary comparison results and their selected-pin sidecars."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import NamedTuple

from nab_python._vendor.packaging.utils import is_normalized_name
from nab_python._vendor.packaging.version import Version
from nab_python.provider import split_extra

CANARY_CONTRACT_VERSION = 2
CANARY_PINS_CONTRACT_VERSION = 1
CANARY_RUN_FIELDS = frozenset(
    {
        "settings",
        "success",
        "error",
        "decisions",
        "conflicts",
        "backjumps",
        "restarts",
        "incompatibilities_learned",
        "metadata_fetched",
        "distributions_seen",
        "look_ahead_rejections",
        "packages",
        "wall_time_seconds",
    }
)
_CANARY_RECORD_FIELDS = frozenset(
    {
        "contract_version",
        "scenario",
        "source",
        "input_hash",
        "execution_hash",
        "input",
        "effective_settings",
        "runs",
        "summary",
    }
)
_PIN_RUN_FIELDS = frozenset({"run", "success", "packages", "pins"})
_RESULT_FILENAME = re.compile(r"canary_([0-9]+)\.json")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIRECTORY_FSYNC_SUPPORTED = os.name != "nt"


class CanaryArtifacts(NamedTuple):
    """Serialized comparison and selected-pin artifacts for one invocation."""

    result: str
    pins: str


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2) + "\n"


def _pins_are_valid(value: object) -> bool:
    """Return whether a value is a sorted map of normalized package pins."""
    if not isinstance(value, dict) or list(value) != sorted(value):
        return False
    for name, version in value.items():
        if (
            not isinstance(name, str)
            or not name
            or not is_normalized_name(name)
            or split_extra(name)[1] is not None
            or not isinstance(version, str)
            or not version
        ):
            return False
        try:
            normalized_version = str(Version(version))
        except ValueError:
            return False
        if normalized_version != version:
            return False
    return True


def _split_run(index: int, run: object) -> tuple[dict, dict]:
    """Split one internal run into comparison metrics and selected pins."""
    if not isinstance(run, dict):
        msg = "canary run must be an object"
        raise TypeError(msg)
    if set(run) != CANARY_RUN_FIELDS | {"pins"}:
        msg = "canary run does not match the internal result shape"
        raise ValueError(msg)

    success = run["success"]
    packages = run["packages"]
    pins = run["pins"]
    if type(success) is not bool or type(packages) is not int or packages < 0:
        msg = "canary run has invalid selected pins"
        raise ValueError(msg)
    if not _pins_are_valid(pins) or packages != len(pins):
        msg = "canary run has invalid selected pins"
        raise ValueError(msg)
    if not success and pins:
        msg = "failed canary run has selected pins"
        raise ValueError(msg)

    result_run = {name: value for name, value in run.items() if name != "pins"}
    pin_run = {
        "run": index,
        "success": success,
        "packages": packages,
        "pins": pins,
    }
    return result_run, pin_run


def _validate_result_identity(
    identity: object,
    *,
    result_filename: str,
    result_text: str,
) -> None:
    """Check that a sidecar names and hashes its comparison result."""
    expected = {
        "filename": result_filename,
        "sha256": hashlib.sha256(result_text.encode()).hexdigest(),
    }
    if not isinstance(identity, dict) or identity != expected:
        msg = "canary pin artifact does not identify its comparison result"
        raise ValueError(msg)


def _validate_pin_run(
    name: str,
    index: int,
    result_run: object,
    pin_run: object,
) -> None:
    """Check one pin row against the same-position comparison row."""
    if not isinstance(result_run, dict) or set(result_run) != CANARY_RUN_FIELDS:
        msg = f"invalid canary comparison run for {name!r}"
        raise ValueError(msg)
    if not isinstance(pin_run, dict) or set(pin_run) != _PIN_RUN_FIELDS:
        msg = f"invalid canary pin run for {name!r}"
        raise ValueError(msg)

    result_success = result_run["success"]
    result_packages = result_run["packages"]
    success = pin_run["success"]
    packages = pin_run["packages"]
    pins = pin_run["pins"]
    if (
        type(result_success) is not bool
        or type(result_packages) is not int
        or result_packages < 0
    ):
        msg = f"invalid canary comparison run for {name!r}"
        raise ValueError(msg)
    if pin_run["run"] != index or type(pin_run["run"]) is not int:
        msg = f"canary pin run order does not match {name!r}"
        raise ValueError(msg)
    if type(success) is not bool or type(packages) is not int or packages < 0:
        msg = f"invalid canary pin run for {name!r}"
        raise ValueError(msg)
    if success is not result_success or packages != result_packages:
        msg = f"canary pin run does not match {name!r}"
        raise ValueError(msg)
    if not _pins_are_valid(pins) or packages != len(pins) or (not success and pins):
        msg = f"invalid canary pin run for {name!r}"
        raise ValueError(msg)


def _validate_scenario(
    name: str,
    result_record: dict,
    pin_record: object,
) -> None:
    """Check one scenario sidecar against its comparison record."""
    if set(result_record) != _CANARY_RECORD_FIELDS or (
        type(result_record["contract_version"]) is not int
        or result_record["contract_version"] != CANARY_CONTRACT_VERSION
    ):
        msg = f"invalid canary comparison record for {name!r}"
        raise ValueError(msg)
    if not isinstance(pin_record, dict) or set(pin_record) != {
        "input_hash",
        "execution_hash",
        "runs",
    }:
        msg = f"invalid canary pin record for {name!r}"
        raise ValueError(msg)
    input_hash = result_record["input_hash"]
    execution_hash = result_record["execution_hash"]
    if (
        not isinstance(input_hash, str)
        or _SHA256.fullmatch(input_hash) is None
        or not isinstance(execution_hash, str)
        or _SHA256.fullmatch(execution_hash) is None
        or pin_record["input_hash"] != input_hash
        or pin_record["execution_hash"] != execution_hash
    ):
        msg = f"canary pin identity does not match {name!r}"
        raise ValueError(msg)

    result_runs = result_record.get("runs")
    pin_runs = pin_record["runs"]
    if not isinstance(result_runs, list) or not isinstance(pin_runs, list):
        msg = f"canary pin runs for {name!r} must be arrays"
        raise TypeError(msg)
    if len(pin_runs) != len(result_runs):
        msg = f"canary pin runs do not match {name!r}"
        raise ValueError(msg)
    for index, (result_run, pin_run) in enumerate(
        zip(result_runs, pin_runs, strict=True)
    ):
        _validate_pin_run(name, index, result_run, pin_run)


def validate_canary_pins_artifact(
    result_text: str,
    pins_data: object,
    *,
    result_filename: str,
) -> None:
    """Validate a selected-pin sidecar against its comparison result."""
    if _RESULT_FILENAME.fullmatch(result_filename) is None:
        msg = "invalid canary comparison filename"
        raise ValueError(msg)
    if not isinstance(result_text, str):
        msg = "serialized canary comparison result must be text"
        raise TypeError(msg)
    try:
        result_data = json.loads(result_text)
    except ValueError as exc:
        msg = "invalid serialized canary comparison result"
        raise ValueError(msg) from exc
    if not isinstance(result_data, dict) or any(
        not isinstance(name, str) or not name or not isinstance(record, dict)
        for name, record in result_data.items()
    ):
        msg = "canary comparison artifact must contain scenario records"
        raise TypeError(msg)
    if not isinstance(pins_data, dict):
        msg = "canary pin artifact must be an object"
        raise TypeError(msg)
    if set(pins_data) != {
        "pins_contract_version",
        "canary_result",
        "scenarios",
    }:
        msg = "invalid canary pin artifact shape"
        raise ValueError(msg)
    if type(pins_data["pins_contract_version"]) is not int or (
        pins_data["pins_contract_version"] != CANARY_PINS_CONTRACT_VERSION
    ):
        msg = "invalid canary pin contract version"
        raise ValueError(msg)

    _validate_result_identity(
        pins_data["canary_result"],
        result_filename=result_filename,
        result_text=result_text,
    )

    scenarios = pins_data["scenarios"]
    if not isinstance(scenarios, dict):
        msg = "canary pin scenarios must be an object"
        raise TypeError(msg)
    if list(scenarios) != list(result_data):
        msg = "canary pin artifact scenario set does not match its comparison result"
        raise ValueError(msg)
    for name, result_record in result_data.items():
        _validate_scenario(name, result_record, scenarios[name])


def build_canary_artifacts(
    records: dict[str, dict],
    result_filename: str,
) -> CanaryArtifacts:
    """Build paired contract-v2 and selected-pin artifacts."""
    result_data: dict[str, dict] = {}
    pin_scenarios: dict[str, dict] = {}
    for name, record in records.items():
        runs = record.get("runs")
        if not isinstance(runs, list):
            msg = f"canary record runs for {name!r} must be an array"
            raise TypeError(msg)

        split_runs = [_split_run(index, run) for index, run in enumerate(runs)]
        result_data[name] = {**record, "runs": [run[0] for run in split_runs]}
        pin_scenarios[name] = {
            "input_hash": record.get("input_hash"),
            "execution_hash": record.get("execution_hash"),
            "runs": [run[1] for run in split_runs],
        }

    result_text = _json_text(result_data)
    pins_data = {
        "pins_contract_version": CANARY_PINS_CONTRACT_VERSION,
        "canary_result": {
            "filename": result_filename,
            "sha256": hashlib.sha256(result_text.encode()).hexdigest(),
        },
        "scenarios": pin_scenarios,
    }
    pins_text = _json_text(pins_data)
    validate_canary_pins_artifact(
        result_text,
        json.loads(pins_text),
        result_filename=result_filename,
    )
    return CanaryArtifacts(result=result_text, pins=pins_text)


def _validate_artifact_paths(result_path: Path, pins_path: Path) -> None:
    """Check paired final paths before creating staged files."""
    match = _RESULT_FILENAME.fullmatch(result_path.name)
    expected_pins_name = f"canary-pins_{match.group(1)}.json" if match else None
    if (
        match is None
        or pins_path.name != expected_pins_name
        or result_path.parent != pins_path.parent
        or result_path.parent.is_symlink()
        or not result_path.parent.is_dir()
    ):
        msg = "invalid canary artifact paths"
        raise ValueError(msg)
    if os.path.lexists(result_path) or os.path.lexists(pins_path):
        msg = "canary artifact path already exists"
        raise FileExistsError(msg)


def _validate_artifact_texts(
    result_path: Path,
    artifacts: CanaryArtifacts,
) -> None:
    """Parse and validate both serialized payloads before publication."""
    try:
        pins_data = json.loads(artifacts.pins)
    except (TypeError, ValueError) as exc:
        msg = "invalid serialized canary artifacts"
        raise ValueError(msg) from exc
    validate_canary_pins_artifact(
        artifacts.result,
        pins_data,
        result_filename=result_path.name,
    )


def _stage_artifact(path: Path, text: str) -> None:
    """Write and sync one artifact inside the private staging directory."""
    with path.open("xb") as staged:
        staged.write(text.encode())
        staged.flush()
        os.fsync(staged.fileno())


def _sync_directory(path: Path) -> None:
    """Sync directory changes on platforms that expose directory handles."""
    if not _DIRECTORY_FSYNC_SUPPORTED:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_exclusive(staged: Path, destination: Path) -> None:
    """Publish a staged file atomically without replacing a destination."""
    os.link(staged, destination)
    _sync_directory(destination.parent)


def write_canary_artifacts(
    result_path: Path,
    pins_path: Path,
    artifacts: CanaryArtifacts,
) -> None:
    """Publish a sidecar before making its bound comparison result visible."""
    _validate_artifact_paths(result_path, pins_path)
    _validate_artifact_texts(result_path, artifacts)
    try:
        with tempfile.TemporaryDirectory(
            dir=result_path.parent,
            prefix=".canary-artifacts-",
        ) as staging_dir:
            staging_path = Path(staging_dir)
            result_staged = staging_path / result_path.name
            pins_staged = staging_path / pins_path.name
            _stage_artifact(result_staged, artifacts.result)
            _stage_artifact(pins_staged, artifacts.pins)

            _publish_exclusive(pins_staged, pins_path)
            _publish_exclusive(result_staged, result_path)
    finally:
        _sync_directory(result_path.parent)
