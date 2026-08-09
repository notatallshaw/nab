"""Shared contract for persisted universal benchmark results."""

from __future__ import annotations

import math

from benchmark_datetime import is_valid_datetime

RESULT_SCHEMA_VERSION = 3
MANIFEST_FILENAME = "_manifest.json"

_PORTABLE_COMPONENT_START = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
)
_PORTABLE_COMPONENT_CHARS = _PORTABLE_COMPONENT_START | frozenset(".-")
_MAX_SCENARIO_NAME_LENGTH = 128
_MAX_STORAGE_COMPONENT_LENGTH = 255
_WINDOWS_RESERVED_COMPONENTS = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)

_TOP_LEVEL_FIELDS = frozenset(
    {"input", "reason", "result", "merged_pins", "stats", "per_tuple"}
)
_REQUIRED_INPUT_FIELDS = frozenset(
    {
        "align_across_tuples",
        "benchmark_schema",
        "build_policy",
        "commit",
        "dist_policy",
        "platforms",
        "python",
        "python_order",
        "reason",
        "requirements",
        "resolution_strategy",
        "scenario",
        "skip_on_fail",
        "source",
        "trust_unverified_sdist_deps",
    }
)
_OPTIONAL_INPUT_FIELDS = frozenset({"constraints", "datetime"})
_SOURCE_FIELDS = frozenset({"commit", "diff_hash", "dirty"})
_RESOLUTION_STRATEGIES = frozenset({"highest", "lowest", "lowest-direct"})
_DIST_POLICIES = frozenset(
    {"prefer-wheel", "sdist-install", "sdist-only", "wheel-only", "wheel-or-sdist"}
)
_BUILD_POLICIES = frozenset({"build-local", "build-remote", "never"})
_RESULT_FIELDS = frozenset(
    {
        "error",
        "expected_failure",
        "lock_consistent",
        "lock_inconsistencies",
        "resolution_success",
        "skip_on_fail",
        "success",
        "timed_out",
    }
)
_STATS_FIELDS = frozenset(
    {
        "backjumps_total",
        "conflicts_total",
        "decisions_total",
        "distributions_seen_total",
        "diverging_packages",
        "merged_packages",
        "metadata_fetched_total",
        "rounds_total",
        "tuples_fail",
        "tuples_ok",
        "tuples_recorded",
        "tuples_total",
    }
)
_TUPLE_COUNTER_FIELDS = frozenset(
    {
        "backjumps",
        "conflicts",
        "decisions",
        "distributions_seen",
        "metadata_fetched",
        "package_count",
        "rounds",
    }
)
_TUPLE_FIELDS = _TUPLE_COUNTER_FIELDS | {
    "error",
    "label",
    "pins",
    "platform_id",
    "python_version",
    "success",
    "wall_time_seconds",
}
_TUPLE_TOTALS = {
    "backjumps_total": "backjumps",
    "conflicts_total": "conflicts",
    "decisions_total": "decisions",
    "distributions_seen_total": "distributions_seen",
    "metadata_fetched_total": "metadata_fetched",
    "rounds_total": "rounds",
}


def _is_portable_path_component(value: str, *, max_length: int) -> bool:
    """Return whether a value is one portable ASCII filename component."""
    if (
        not value
        or len(value) > max_length
        or value[0] not in _PORTABLE_COMPONENT_START
        or value.endswith(".")
        or any(character not in _PORTABLE_COMPONENT_CHARS for character in value)
    ):
        return False
    windows_basename = value.partition(".")[0].casefold()
    return windows_basename not in _WINDOWS_RESERVED_COMPONENTS


def is_portable_scenario_name(value: object) -> bool:
    """Return whether a scenario name can identify one universal result file."""
    if not isinstance(value, str):
        return False
    result_filename = f"{value}.json"
    return (
        _is_portable_path_component(value, max_length=_MAX_SCENARIO_NAME_LENGTH)
        and _is_portable_path_component(
            result_filename,
            max_length=_MAX_STORAGE_COMPONENT_LENGTH,
        )
        and result_filename.casefold() != MANIFEST_FILENAME.casefold()
    )


def _is_optional_bool(value: object) -> bool:
    return value is None or type(value) is bool


def _is_nonnegative_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _is_hex_digest(value: object, lengths: set[int]) -> bool:
    return (
        isinstance(value, str)
        and len(value) in lengths
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_source(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
        return False
    commit = value["commit"]
    diff_hash = value["diff_hash"]
    dirty = value["dirty"]
    if type(dirty) is not bool:
        return False
    if commit is not None and not _is_hex_digest(commit, {40, 64}):
        return False
    if diff_hash is not None and not _is_hex_digest(diff_hash, {64}):
        return False
    return dirty is True or (commit is not None and diff_hash is None)


def _valid_string_list(value: object, *, unique: bool = False) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and bool(item) for item in value)
        and (not unique or len(set(value)) == len(value))
    )


def _valid_input(value: object, reason: object) -> bool:
    if not isinstance(value, dict):
        return False
    fields = set(value)
    if not fields >= _REQUIRED_INPUT_FIELDS or not fields <= (
        _REQUIRED_INPUT_FIELDS | _OPTIONAL_INPUT_FIELDS
    ):
        return False
    return (
        value["benchmark_schema"] == RESULT_SCHEMA_VERSION
        and is_portable_scenario_name(value["scenario"])
        and isinstance(value["commit"], str)
        and bool(value["commit"])
        and _valid_source(value["source"])
        and isinstance(value["python"], str)
        and bool(value["python"])
        and _valid_string_list(value["platforms"], unique=True)
        and isinstance(value["python_order"], str)
        and value["python_order"] in {"asc", "desc"}
        and _valid_string_list(value["requirements"])
        and type(value["align_across_tuples"]) is bool
        and isinstance(value["resolution_strategy"], str)
        and value["resolution_strategy"] in _RESOLUTION_STRATEGIES
        and isinstance(value["dist_policy"], str)
        and value["dist_policy"] in _DIST_POLICIES
        and isinstance(value["build_policy"], str)
        and value["build_policy"] in _BUILD_POLICIES
        and type(value["trust_unverified_sdist_deps"]) is bool
        and type(value["skip_on_fail"]) is bool
        and isinstance(value["reason"], str)
        and reason == value["reason"]
        and ("constraints" not in value or _valid_string_list(value["constraints"]))
        and ("datetime" not in value or is_valid_datetime(value["datetime"]))
    )


def _valid_result(value: object, input_data: dict) -> bool:
    if not isinstance(value, dict) or set(value) != _RESULT_FIELDS:
        return False
    inconsistencies = value["lock_inconsistencies"]
    error = value["error"]
    return (
        type(value["success"]) is bool
        and _is_optional_bool(value["resolution_success"])
        and type(value["expected_failure"]) is bool
        and type(value["skip_on_fail"]) is bool
        and value["skip_on_fail"] is input_data["skip_on_fail"]
        and type(value["timed_out"]) is bool
        and _is_optional_bool(value["lock_consistent"])
        and (error is None or (isinstance(error, str) and bool(error)))
        and isinstance(inconsistencies, list)
        and all(isinstance(item, str) and bool(item) for item in inconsistencies)
    )


def _valid_stats(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _STATS_FIELDS | {
        "wall_time_seconds"
    }:
        return False
    return (
        all(type(value[field]) is int and value[field] >= 0 for field in _STATS_FIELDS)
        and value["tuples_total"] > 0
        and _is_nonnegative_number(value["wall_time_seconds"])
    )


def _valid_tuple(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _TUPLE_FIELDS:
        return False
    if not all(
        isinstance(value[field], str) and bool(value[field])
        for field in ("label", "platform_id", "python_version")
    ):
        return False
    pins = value["pins"]
    error = value["error"]
    return (
        type(value["success"]) is bool
        and all(
            type(value[field]) is int and value[field] >= 0
            for field in _TUPLE_COUNTER_FIELDS
        )
        and (
            (value["success"] is True and error is None)
            or (value["success"] is False and isinstance(error, str) and bool(error))
        )
        and _is_nonnegative_number(value["wall_time_seconds"])
        and isinstance(pins, dict)
        and all(
            isinstance(name, str)
            and bool(name)
            and isinstance(version, str)
            and bool(version)
            for name, version in pins.items()
        )
        and value["package_count"] == len(pins)
        and (value["success"] is True or not pins)
    )


def _valid_per_tuple(value: object, stats: dict, input_data: dict) -> bool:
    if not isinstance(value, list):
        return False
    if not all(_valid_tuple(item) for item in value):
        return False
    labels = [item["label"] for item in value]
    cells = {(item["python_version"], item["platform_id"]) for item in value}
    platforms = set(input_data["platforms"])
    python_versions = {item["python_version"] for item in value}
    return (
        len(value) == stats["tuples_recorded"]
        and stats["tuples_ok"] + stats["tuples_fail"] == len(value)
        and sum(item["success"] is True for item in value) == stats["tuples_ok"]
        and len(set(labels)) == len(labels)
        and all(item["platform_id"] in platforms for item in value)
        and (
            not value
            or (
                cells
                == {
                    (python, platform)
                    for python in python_versions
                    for platform in platforms
                }
                and len(cells) == stats["tuples_total"]
            )
        )
        and all(
            stats[total] == sum(item[field] for item in value)
            for total, field in _TUPLE_TOTALS.items()
        )
    )


def _valid_merged_pins(value: object, stats: dict, per_tuple: list[dict]) -> bool:
    if not isinstance(value, dict) or len(value) != stats["merged_packages"]:
        return False
    expected: dict[str, set[tuple[str, str]]] = {}
    for item in per_tuple:
        if item["success"]:
            for name, version in item["pins"].items():
                expected.setdefault(name, set()).add((version, item["label"]))
    labels = {item["label"] for item in per_tuple}
    actual: dict[str, set[tuple[str, str]]] = {}
    for name, pins in value.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(pins, list)
            or not pins
            or not all(
                isinstance(pin, dict)
                and set(pin) == {"version", "target"}
                and isinstance(pin["version"], str)
                and bool(pin["version"])
                and isinstance(pin["target"], str)
                and pin["target"] in labels
                for pin in pins
            )
        ):
            return False
        pairs = {(pin["version"], pin["target"]) for pin in pins}
        if len(pairs) != len(pins):
            return False
        actual[name] = pairs
    diverging = sum(
        1 for pins in value.values() if len({pin["version"] for pin in pins}) > 1
    )
    return actual == expected and stats["diverging_packages"] == diverging


def _valid_outcome(
    result: dict, stats: dict, per_tuple: list, merged_pins: dict
) -> bool:
    resolution_success = result["resolution_success"]
    if resolution_success is None:
        zero_fields = _STATS_FIELDS - {"tuples_total"}
        return (
            result["success"] is False
            and result["expected_failure"] is False
            and result["lock_consistent"] is None
            and not result["lock_inconsistencies"]
            and isinstance(result["error"], str)
            and all(stats[field] == 0 for field in zero_fields)
            and not per_tuple
            and not merged_pins
        )
    if (
        stats["tuples_recorded"] < stats["tuples_total"]
        or result["timed_out"] is True
        or result["success"]
        is not (resolution_success is True and result["lock_consistent"] is True)
        or result["expected_failure"]
        is not (result["skip_on_fail"] is True and resolution_success is False)
    ):
        return False
    if resolution_success is False:
        return (
            stats["tuples_fail"] > 0
            and result["lock_consistent"] is None
            and not result["lock_inconsistencies"]
            and result["error"] is None
        )
    return _valid_successful_resolution_outcome(result, stats)


def _valid_successful_resolution_outcome(result: dict, stats: dict) -> bool:
    if (
        stats["tuples_fail"] != 0
        or stats["tuples_ok"] != stats["tuples_recorded"]
        or result["expected_failure"] is True
    ):
        return False
    if result["error"] is not None:
        return (
            result["success"] is False
            and result["lock_consistent"] is None
            and not result["lock_inconsistencies"]
        )
    if result["lock_consistent"] is True:
        return result["success"] is True and not result["lock_inconsistencies"]
    return (
        result["lock_consistent"] is False
        and result["success"] is False
        and bool(result["lock_inconsistencies"])
    )


def _result_outcome_is_accepted(result: dict) -> bool:
    successful_lock = (
        result["success"] is True
        and result["resolution_success"] is True
        and result["expected_failure"] is False
        and result["lock_consistent"] is True
        and result["timed_out"] is False
        and result["error"] is None
    )
    expected_resolution_failure = (
        result["success"] is False
        and result["resolution_success"] is False
        and result["expected_failure"] is True
        and result["skip_on_fail"] is True
        and result["lock_consistent"] is None
        and result["timed_out"] is False
        and result["error"] is None
    )
    return successful_lock or expected_resolution_failure


def result_is_well_formed(data: object) -> bool:
    """Whether a persisted result could have been emitted by the runner."""
    if not isinstance(data, dict) or set(data) != _TOP_LEVEL_FIELDS:
        return False
    input_data = data["input"]
    result = data["result"]
    stats = data["stats"]
    per_tuple = data["per_tuple"]
    merged_pins = data["merged_pins"]
    return (
        _valid_input(input_data, data["reason"])
        and _valid_result(result, input_data)
        and _valid_stats(stats)
        and _valid_per_tuple(per_tuple, stats, input_data)
        and _valid_merged_pins(merged_pins, stats, per_tuple)
        and _valid_outcome(result, stats, per_tuple, merged_pins)
    )


def result_is_accepted(data: object) -> bool:
    """Whether a persisted scenario result satisfies the benchmark contract."""
    return (
        result_is_well_formed(data)
        and isinstance(data, dict)
        and _result_outcome_is_accepted(data["result"])
    )
