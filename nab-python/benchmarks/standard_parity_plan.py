"""Compile the standard corpus into a static Nab/uv parity plan.

The plan is translation metadata, not resolver output or comparison evidence.  This
module only reads the canonical scenario corpus.  It never invokes either resolver,
starts a subprocess, accesses the network, or populates a cache.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from datetime import timezone
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

_BENCHMARKS_DIR = Path(__file__).resolve().parent


class _StandardScenario(Protocol):
    @property
    def logical_key(self) -> str: ...

    definition: dict[str, object]


class _PlanHeader(NamedTuple):
    source_digest: str
    corpus_digest: str
    strategies: list[str]
    mapping_count: int
    scenarios: list[object]


@lru_cache(maxsize=1)
def _standard() -> ModuleType:
    """Load the canonical corpus helpers without changing ``sys.path``."""
    path = _BENCHMARKS_DIR / "scenarios.py"
    spec = importlib.util.spec_from_file_location("_standard_parity_corpus", path)
    if spec is None or spec.loader is None:
        msg = f"cannot load canonical scenario helpers from {path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLAN_SCHEMA = 1
PRODUCT_DEFAULT_TRUST_UNVERIFIED_SDIST_DEPS = False
UNSUPPORTED_REASONS = (
    "declared-unsupported",
    "partial-target-platform-system",
    "partial-target-marker-environment",
    "target-not-representable-in-uv",
    "python-micro-partition-required",
    "build-policy-no-equivalent",
    "build-package-allowlist-no-equivalent",
    "trust-unverified-sdist-deps",
    "custom-index-routing-no-equivalent",
    "project-shape-not-materialized",
    "prerelease-policy-unverified",
    "requires-python-upper-bound-policy",
    "missing-expected-outcome",
)
RUNTIME_POSTCONDITIONS = (
    "build-policy-difference-dormant",
    "artifact-domain-equivalent",
    "python-micro-policy-not-exercised",
)
ADMISSION_STATUSES = ("exact", "conditional", "unsupported")

_DEFINITION_FIELDS = frozenset(
    {
        "build_packages",
        "constraints",
        "datetime",
        "index_routes",
        "indexes",
        "marker_environment",
        "optional_dependencies",
        "platform_system",
        "project_extras",
        "project_name",
        "python_version",
        "requirements",
        "trust_unverified_sdist_deps",
        "unsupported_reason",
        "vcs_allowed_repos",
        "vcs_allowed_schemes",
        "vcs_policy",
        "vcs_require_pin",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "source_corpus_digest",
        "corpus_digest",
        "strategies",
        "scenario_count",
        "mapping_count",
        "scenarios",
    }
)
_SCENARIO_FIELDS = frozenset(
    {
        "logical_key",
        "source_definition",
        "scenario_digest",
        "nab",
        "uv",
        "admission",
        "executions",
    }
)
_NAB_FIELDS = frozenset(
    {
        "python_version",
        "requirements",
        "constraints",
        "uploaded_prior_to",
        "target",
        "indexes",
        "index_routes",
        "build_packages",
        "trust_unverified_sdist_deps",
        "project",
        "vcs",
    }
)
_ADMISSION_FIELDS = frozenset({"status", "reasons", "postconditions"})
_EXECUTION_FIELDS = frozenset(
    {"execution_key", "mapping_digest", "strategy", "nab_settings", "uv_settings"}
)
_TARGET_FIELDS = frozenset({"kind", "marker_environment"})
_INDEX_FIELDS = frozenset({"name", "url"})
_INDEX_ROUTE_FIELDS = frozenset({"name", "index"})
_PROJECT_FIELDS = frozenset({"name", "extras", "optional_dependencies"})
_VCS_FIELDS = frozenset({"policy", "allowed_schemes", "allowed_repos", "require_pin"})
_SETTINGS_FIELDS = frozenset({"resolution"})
_LOWER_HEX = frozenset("0123456789abcdef")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _expect_mapping(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        msg = f"{label} must be an object"
        raise TypeError(msg)
    return value


def _expect_exact_fields(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        msg = (
            f"{label} fields do not match schema: missing={missing}, unknown={unknown}"
        )
        raise ValueError(msg)


def _expect_str(value: object, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        expected = "a string" if allow_empty else "a non-empty string"
        msg = f"{label} must be {expected}"
        raise TypeError(msg)
    return value


def _expect_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        msg = f"{label} must be a boolean"
        raise TypeError(msg)
    return value


def _expect_int(value: object, label: str) -> int:
    if type(value) is not int:
        msg = f"{label} must be an integer"
        raise TypeError(msg)
    return value


def _expect_string_list(value: object, label: str) -> list[str]:
    if type(value) is not list:
        msg = f"{label} must be an array"
        raise TypeError(msg)
    for index, item in enumerate(value):
        _expect_str(item, f"{label}[{index}]")
    return value


def _expect_unique(values: Sequence[str], label: str) -> None:
    if len(values) != len(set(values)):
        msg = f"{label} must not contain duplicates"
        raise ValueError(msg)


def _expect_digest(value: object, label: str) -> str:
    digest = _expect_str(value, label)
    if len(digest) != 64 or any(char not in _LOWER_HEX for char in digest):
        msg = f"{label} must be a lowercase SHA-256 digest"
        raise ValueError(msg)
    return digest


def _expect_ordered_vocabulary(
    value: object, vocabulary: tuple[str, ...], label: str
) -> list[str]:
    values = _expect_string_list(value, label)
    _expect_unique(values, label)
    unknown = sorted(set(values) - set(vocabulary))
    if unknown:
        msg = f"{label} contains unknown values: {unknown}"
        raise ValueError(msg)
    expected_order = [item for item in vocabulary if item in values]
    if values != expected_order:
        msg = f"{label} must use vocabulary order"
        raise ValueError(msg)
    return values


def _validate_definition(logical_key: str, definition: object) -> dict[str, object]:
    data = _expect_mapping(definition, f"{logical_key} definition")
    unknown = sorted(set(data) - _DEFINITION_FIELDS)
    if unknown:
        msg = f"{logical_key} definition has unknown fields: {unknown}"
        raise ValueError(msg)
    for required in ("python_version", "requirements", "datetime"):
        if required not in data:
            msg = f"{logical_key} definition is missing {required}"
            raise ValueError(msg)

    _expect_str(data["python_version"], f"{logical_key}.python_version")
    _expect_string_list(data["requirements"], f"{logical_key}.requirements")
    _expect_str(data["datetime"], f"{logical_key}.datetime")
    if "constraints" in data:
        _expect_string_list(data["constraints"], f"{logical_key}.constraints")
    if "platform_system" in data:
        _expect_str(data["platform_system"], f"{logical_key}.platform_system")
    if "marker_environment" in data:
        marker_environment = _expect_mapping(
            data["marker_environment"], f"{logical_key}.marker_environment"
        )
        for name, value in marker_environment.items():
            _expect_str(name, f"{logical_key}.marker_environment key")
            _expect_str(
                value, f"{logical_key}.marker_environment.{name}", allow_empty=True
            )
        if (
            "platform_system" in data
            and "platform_system" in marker_environment
            and data["platform_system"] != marker_environment["platform_system"]
        ):
            msg = f"{logical_key} has conflicting platform_system overlays"
            raise ValueError(msg)
    if "build_packages" in data:
        build_packages = _expect_string_list(
            data["build_packages"], f"{logical_key}.build_packages"
        )
        _expect_unique(build_packages, f"{logical_key}.build_packages")
    if "trust_unverified_sdist_deps" in data:
        _expect_bool(
            data["trust_unverified_sdist_deps"],
            f"{logical_key}.trust_unverified_sdist_deps",
        )
    if "unsupported_reason" in data:
        _expect_str(data["unsupported_reason"], f"{logical_key}.unsupported_reason")
    _validate_indexes(logical_key, data)
    _validate_project(logical_key, data)
    _validate_vcs(logical_key, data)
    return data


def _validate_indexes(logical_key: str, data: Mapping[str, object]) -> None:
    names: list[str] = []
    if "indexes" in data:
        indexes = data["indexes"]
        if type(indexes) is not list:
            msg = f"{logical_key}.indexes must be an array"
            raise TypeError(msg)
        for index, item in enumerate(indexes):
            entry = _expect_mapping(item, f"{logical_key}.indexes[{index}]")
            _expect_exact_fields(
                entry, _INDEX_FIELDS, f"{logical_key}.indexes[{index}]"
            )
            names.append(
                _expect_str(entry["name"], f"{logical_key}.indexes[{index}].name")
            )
            _expect_str(entry["url"], f"{logical_key}.indexes[{index}].url")
        _expect_unique(names, f"{logical_key}.indexes names")
    if "index_routes" not in data:
        return
    routes = data["index_routes"]
    if type(routes) is not list:
        msg = f"{logical_key}.index_routes must be an array"
        raise TypeError(msg)
    available = (
        set(names)
        if "indexes" in data
        else {item.name for item in _standard().DEFAULT_INDEXES}
    )
    for index, item in enumerate(routes):
        entry = _expect_mapping(item, f"{logical_key}.index_routes[{index}]")
        _expect_exact_fields(
            entry, _INDEX_ROUTE_FIELDS, f"{logical_key}.index_routes[{index}]"
        )
        _expect_str(entry["name"], f"{logical_key}.index_routes[{index}].name")
        route_index = _expect_str(
            entry["index"], f"{logical_key}.index_routes[{index}].index"
        )
        if route_index not in available:
            msg = (
                f"{logical_key}.index_routes[{index}] names unknown index"
                f" {route_index!r}"
            )
            raise ValueError(msg)


def _validate_project(logical_key: str, data: Mapping[str, object]) -> None:
    project_fields = {"project_name", "project_extras", "optional_dependencies"}
    if not (set(data) & project_fields):
        return
    if "project_name" not in data:
        msg = f"{logical_key} project extras require project_name"
        raise ValueError(msg)
    _expect_str(data["project_name"], f"{logical_key}.project_name")
    extras = _expect_string_list(
        data.get("project_extras", []), f"{logical_key}.project_extras"
    )
    _expect_unique(extras, f"{logical_key}.project_extras")
    optional = data.get("optional_dependencies", {})
    optional_mapping = _expect_mapping(optional, f"{logical_key}.optional_dependencies")
    for name, requirements in optional_mapping.items():
        _expect_str(name, f"{logical_key}.optional_dependencies key")
        _expect_string_list(requirements, f"{logical_key}.optional_dependencies.{name}")
    missing = sorted(set(extras) - set(optional_mapping))
    if missing:
        msg = f"{logical_key} project extras have no dependency group: {missing}"
        raise ValueError(msg)


def _validate_vcs(logical_key: str, data: Mapping[str, object]) -> None:
    if "vcs_policy" in data:
        policy = _expect_str(data["vcs_policy"], f"{logical_key}.vcs_policy")
        if policy not in {item.value for item in _standard().VcsPolicy}:
            msg = f"{logical_key}.vcs_policy is unknown: {policy!r}"
            raise ValueError(msg)
    for field in ("vcs_allowed_schemes", "vcs_allowed_repos"):
        if field in data:
            values = _expect_string_list(data[field], f"{logical_key}.{field}")
            _expect_unique(values, f"{logical_key}.{field}")
    if "vcs_require_pin" in data:
        _expect_bool(data["vcs_require_pin"], f"{logical_key}.vcs_require_pin")


def _utc_cutoff(value: str) -> str:
    """Return an ISO 8601 cutoff in the canonical UTC spelling."""
    parse_input = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = _standard().parse_datetime(parse_input).astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def _normalized_nab(
    logical_key: str, definition: Mapping[str, object]
) -> dict[str, object]:
    marker_environment = dict(
        sorted(
            _standard().parse_marker_environment(logical_key, dict(definition)).items()
        )
    )
    indexes = [
        {"name": item.name, "url": item.url}
        for item in _standard().parse_indexes(logical_key, dict(definition))
    ]
    index_routes = [
        {"name": item.name, "index": item.index}
        for item in _standard().parse_index_routes(logical_key, dict(definition))
    ]
    optional = definition.get("optional_dependencies", {})
    project = None
    if "project_name" in definition:
        project = {
            "name": definition["project_name"],
            "extras": list(definition.get("project_extras", [])),
            "optional_dependencies": {
                name: list(requirements)
                for name, requirements in sorted(optional.items())
            },
        }
    target_kind = "host" if not marker_environment else "host-with-marker-overlay"
    return {
        "python_version": definition["python_version"],
        "requirements": list(definition["requirements"]),
        "constraints": list(definition.get("constraints", [])),
        "uploaded_prior_to": _utc_cutoff(str(definition["datetime"])),
        "target": {
            "kind": target_kind,
            "marker_environment": marker_environment,
        },
        "indexes": indexes,
        "index_routes": index_routes,
        "build_packages": sorted(definition.get("build_packages", [])),
        "trust_unverified_sdist_deps": definition.get(
            "trust_unverified_sdist_deps",
            PRODUCT_DEFAULT_TRUST_UNVERIFIED_SDIST_DEPS,
        ),
        "project": project,
        "vcs": {
            "policy": definition.get("vcs_policy", _standard().VcsPolicy.BLOCK.value),
            "allowed_schemes": sorted(definition.get("vcs_allowed_schemes", [])),
            "allowed_repos": list(definition.get("vcs_allowed_repos", [])),
            "require_pin": definition.get("vcs_require_pin", True),
        },
    }


def _unsupported_reasons(definition: Mapping[str, object]) -> list[str]:
    applies = {
        "declared-unsupported": "unsupported_reason" in definition,
        "partial-target-platform-system": "platform_system" in definition,
        "partial-target-marker-environment": "marker_environment" in definition,
        # Nab's product default permits only local builds while uv's default may
        # build remote sdists.  Schema 1 has no artifact evidence with which to
        # prove that difference dormant.
        "build-policy-no-equivalent": True,
        "build-package-allowlist-no-equivalent": bool(
            definition.get("build_packages", [])
        ),
        "trust-unverified-sdist-deps": definition.get(
            "trust_unverified_sdist_deps", False
        )
        is True,
        "custom-index-routing-no-equivalent": any(
            field in definition for field in ("indexes", "index_routes")
        ),
        "project-shape-not-materialized": any(
            field in definition
            for field in (
                "optional_dependencies",
                "project_extras",
                "project_name",
                "vcs_allowed_repos",
                "vcs_allowed_schemes",
                "vcs_policy",
                "vcs_require_pin",
            )
        ),
        # The standard source format carries neither a structured expected
        # outcome nor frozen dependency metadata.  It therefore cannot prove
        # prerelease or dependency Requires-Python policy equivalence.
        "prerelease-policy-unverified": True,
        "requires-python-upper-bound-policy": True,
        "missing-expected-outcome": True,
    }
    return [reason for reason in UNSUPPORTED_REASONS if applies.get(reason, False)]


def _admission(definition: Mapping[str, object]) -> dict[str, object]:
    reasons = _unsupported_reasons(definition)
    return {"status": "unsupported", "reasons": reasons, "postconditions": []}


def _strategy_settings(strategy: str) -> dict[str, str]:
    if strategy == "highest":
        return {}
    return {"resolution": strategy}


def _execution_key(logical_key: str, strategy: str) -> str:
    stem, separator, name = logical_key.partition(":")
    if not separator or not stem or not name:
        msg = f"invalid logical key {logical_key!r}"
        raise ValueError(msg)
    result_stem = stem if strategy == "highest" else f"{stem}-{strategy}"
    return f"{result_stem}/{name}.json"


def _compile_scenario(
    row: _StandardScenario, strategies: Sequence[str]
) -> dict[str, object]:
    definition = _validate_definition(row.logical_key, row.definition)
    source_definition = copy.deepcopy(definition)
    nab = _normalized_nab(row.logical_key, definition)
    admission = _admission(definition)
    uv = None
    scenario_payload = {
        "logical_key": row.logical_key,
        "source_definition": source_definition,
        "nab": nab,
        "uv": uv,
        "admission": admission,
    }
    scenario_digest = _digest(scenario_payload)
    executions: list[dict[str, object]] = []
    for strategy in strategies:
        nab_settings = _strategy_settings(strategy)
        uv_settings = None if uv is None else _strategy_settings(strategy)
        execution_payload = {
            "execution_key": _execution_key(row.logical_key, strategy),
            "strategy": strategy,
            "nab_settings": nab_settings,
            "uv_settings": uv_settings,
        }
        executions.append(
            {
                **execution_payload,
                "mapping_digest": _digest(
                    {"scenario_digest": scenario_digest, **execution_payload}
                ),
            }
        )
    return {
        **scenario_payload,
        "scenario_digest": scenario_digest,
        "executions": executions,
    }


def _source_corpus_digest(scenarios: Sequence[Mapping[str, object]]) -> str:
    return _digest(
        {
            scenario["logical_key"]: scenario["source_definition"]
            for scenario in scenarios
        }
    )


def compile_plan(files: list[Path] | None = None) -> dict[str, object]:
    """Compile *files*, or the canonical standard corpus, into a strict plan."""
    canonical = _standard()
    selected_files = canonical.standard_scenario_files() if files is None else files
    rows = canonical.load_standard_corpus(selected_files)
    logical_keys = [row.logical_key for row in rows]
    _expect_unique(logical_keys, "logical scenario keys")
    strategies = [strategy.value for strategy in canonical.STANDARD_STRATEGIES]
    _expect_unique(strategies, "strategies")
    scenarios = sorted(
        (_compile_scenario(row, strategies) for row in rows),
        key=lambda item: str(item["logical_key"]),
    )
    source_corpus_digest = canonical.standard_corpus_hash(rows)
    if _source_corpus_digest(scenarios) != source_corpus_digest:
        msg = "normalized plan sources do not match the canonical corpus digest"
        raise ValueError(msg)
    corpus_payload = {
        "source_corpus_digest": source_corpus_digest,
        "strategies": strategies,
        "scenarios": scenarios,
    }
    plan = {
        "schema": PLAN_SCHEMA,
        "source_corpus_digest": source_corpus_digest,
        "corpus_digest": _digest(corpus_payload),
        "strategies": strategies,
        "scenario_count": len(scenarios),
        "mapping_count": sum(len(item["executions"]) for item in scenarios),
        "scenarios": scenarios,
    }
    validate_plan(plan, expected_source_corpus_digest=plan["source_corpus_digest"])
    return plan


def _validate_target(value: object, label: str) -> None:
    target = _expect_mapping(value, label)
    _expect_exact_fields(target, _TARGET_FIELDS, label)
    if target["kind"] not in {"host", "host-with-marker-overlay"}:
        msg = f"{label}.kind is unknown: {target['kind']!r}"
        raise ValueError(msg)
    marker_environment = _expect_mapping(
        target["marker_environment"], f"{label}.marker_environment"
    )
    for name, item in marker_environment.items():
        _expect_str(name, f"{label}.marker_environment key")
        _expect_str(item, f"{label}.marker_environment.{name}", allow_empty=True)
    if (target["kind"] == "host") is not (marker_environment == {}):
        msg = f"{label}.kind does not match its marker environment"
        raise ValueError(msg)


def _validate_index_list(value: object, label: str) -> list[dict[str, object]]:
    if type(value) is not list:
        msg = f"{label} must be an array"
        raise TypeError(msg)
    names: list[str] = []
    for index, item in enumerate(value):
        entry = _expect_mapping(item, f"{label}[{index}]")
        _expect_exact_fields(entry, _INDEX_FIELDS, f"{label}[{index}]")
        names.append(_expect_str(entry["name"], f"{label}[{index}].name"))
        _expect_str(entry["url"], f"{label}[{index}].url")
    _expect_unique(names, f"{label} names")
    return value


def _validate_nab(value: object, label: str) -> dict[str, object]:
    nab = _expect_mapping(value, label)
    _expect_exact_fields(nab, _NAB_FIELDS, label)
    _expect_str(nab["python_version"], f"{label}.python_version")
    _expect_string_list(nab["requirements"], f"{label}.requirements")
    _expect_string_list(nab["constraints"], f"{label}.constraints")
    cutoff = _expect_str(nab["uploaded_prior_to"], f"{label}.uploaded_prior_to")
    if _utc_cutoff(cutoff) != cutoff:
        msg = f"{label}.uploaded_prior_to must be normalized to UTC"
        raise ValueError(msg)
    _validate_target(nab["target"], f"{label}.target")
    indexes = _validate_index_list(nab["indexes"], f"{label}.indexes")
    index_names = {str(item["name"]) for item in indexes}
    routes = nab["index_routes"]
    if type(routes) is not list:
        msg = f"{label}.index_routes must be an array"
        raise TypeError(msg)
    for index, item in enumerate(routes):
        route = _expect_mapping(item, f"{label}.index_routes[{index}]")
        _expect_exact_fields(
            route, _INDEX_ROUTE_FIELDS, f"{label}.index_routes[{index}]"
        )
        _expect_str(route["name"], f"{label}.index_routes[{index}].name")
        route_index = _expect_str(
            route["index"], f"{label}.index_routes[{index}].index"
        )
        if route_index not in index_names:
            msg = f"{label}.index_routes[{index}] names unknown index {route_index!r}"
            raise ValueError(msg)
    build_packages = _expect_string_list(
        nab["build_packages"], f"{label}.build_packages"
    )
    _expect_unique(build_packages, f"{label}.build_packages")
    if build_packages != sorted(build_packages):
        msg = f"{label}.build_packages must be sorted"
        raise ValueError(msg)
    _expect_bool(
        nab["trust_unverified_sdist_deps"], f"{label}.trust_unverified_sdist_deps"
    )
    _validate_normalized_project(nab["project"], f"{label}.project")
    _validate_normalized_vcs(nab["vcs"], f"{label}.vcs")
    return nab


def _validate_normalized_project(value: object, label: str) -> None:
    if value is None:
        return
    project = _expect_mapping(value, label)
    _expect_exact_fields(project, _PROJECT_FIELDS, label)
    _expect_str(project["name"], f"{label}.name")
    extras = _expect_string_list(project["extras"], f"{label}.extras")
    _expect_unique(extras, f"{label}.extras")
    optional = _expect_mapping(
        project["optional_dependencies"], f"{label}.optional_dependencies"
    )
    for name, requirements in optional.items():
        _expect_str(name, f"{label}.optional_dependencies key")
        _expect_string_list(requirements, f"{label}.optional_dependencies.{name}")
    missing = sorted(set(extras) - set(optional))
    if missing:
        msg = f"{label}.extras have no dependency group: {missing}"
        raise ValueError(msg)


def _validate_normalized_vcs(value: object, label: str) -> None:
    vcs = _expect_mapping(value, label)
    _expect_exact_fields(vcs, _VCS_FIELDS, label)
    policy = _expect_str(vcs["policy"], f"{label}.policy")
    if policy not in {item.value for item in _standard().VcsPolicy}:
        msg = f"{label}.policy is unknown: {policy!r}"
        raise ValueError(msg)
    schemes = _expect_string_list(vcs["allowed_schemes"], f"{label}.allowed_schemes")
    _expect_unique(schemes, f"{label}.allowed_schemes")
    if schemes != sorted(schemes):
        msg = f"{label}.allowed_schemes must be sorted"
        raise ValueError(msg)
    repos = _expect_string_list(vcs["allowed_repos"], f"{label}.allowed_repos")
    _expect_unique(repos, f"{label}.allowed_repos")
    _expect_bool(vcs["require_pin"], f"{label}.require_pin")


def _validate_admission(value: object, uv: object, label: str) -> None:
    admission = _expect_mapping(value, label)
    _expect_exact_fields(admission, _ADMISSION_FIELDS, label)
    status = _expect_str(admission["status"], f"{label}.status")
    if status not in ADMISSION_STATUSES:
        msg = f"{label}.status is unknown: {status!r}"
        raise ValueError(msg)
    reasons = _expect_ordered_vocabulary(
        admission["reasons"], UNSUPPORTED_REASONS, f"{label}.reasons"
    )
    postconditions = _expect_ordered_vocabulary(
        admission["postconditions"],
        RUNTIME_POSTCONDITIONS,
        f"{label}.postconditions",
    )
    if status != "unsupported":
        msg = f"{label} {status} admission is unavailable in plan schema 1"
        raise ValueError(msg)
    if not reasons or postconditions or uv is not None:
        msg = f"{label} unsupported rows require reasons and a null uv translation"
        raise ValueError(msg)


def _validate_settings(value: object, strategy: str, label: str) -> None:
    settings = _expect_mapping(value, label)
    unknown = sorted(set(settings) - _SETTINGS_FIELDS)
    if unknown:
        msg = f"{label} has unknown fields: {unknown}"
        raise ValueError(msg)
    expected = _strategy_settings(strategy)
    if settings != expected:
        msg = f"{label} must be {expected!r} for {strategy}"
        raise ValueError(msg)


def _validate_execution(
    value: object,
    *,
    logical_key: str,
    scenario_digest: str,
    strategy: str,
    label: str,
) -> str:
    execution = _expect_mapping(value, label)
    _expect_exact_fields(execution, _EXECUTION_FIELDS, label)
    if execution["strategy"] != strategy:
        msg = f"{label}.strategy does not match plan order"
        raise ValueError(msg)
    execution_key = _expect_str(execution["execution_key"], f"{label}.execution_key")
    if execution_key != _execution_key(logical_key, strategy):
        msg = f"{label}.execution_key is not canonical"
        raise ValueError(msg)
    _validate_settings(execution["nab_settings"], strategy, f"{label}.nab_settings")
    uv_settings = execution["uv_settings"]
    if uv_settings is not None:
        msg = f"{label}.uv_settings must be null in plan schema 1"
        raise ValueError(msg)
    mapping_digest = _expect_digest(
        execution["mapping_digest"], f"{label}.mapping_digest"
    )
    payload = {
        "execution_key": execution_key,
        "strategy": strategy,
        "nab_settings": execution["nab_settings"],
        "uv_settings": uv_settings,
    }
    expected_digest = _digest({"scenario_digest": scenario_digest, **payload})
    if mapping_digest != expected_digest:
        msg = f"{label}.mapping_digest does not match its mapping"
        raise ValueError(msg)
    return execution_key


def _validate_plan_header(value: object, trusted_source_digest: str) -> _PlanHeader:
    plan = _expect_mapping(value, "plan")
    _expect_exact_fields(plan, _TOP_LEVEL_FIELDS, "plan")
    if _expect_int(plan["schema"], "plan.schema") != PLAN_SCHEMA:
        msg = f"plan.schema must be {PLAN_SCHEMA}"
        raise ValueError(msg)
    source_digest = _expect_digest(
        plan["source_corpus_digest"], "plan.source_corpus_digest"
    )
    if source_digest != trusted_source_digest:
        msg = "plan.source_corpus_digest does not match the canonical corpus"
        raise ValueError(msg)
    corpus_digest = _expect_digest(plan["corpus_digest"], "plan.corpus_digest")
    strategies = _expect_string_list(plan["strategies"], "plan.strategies")
    expected_strategies = [item.value for item in _standard().STANDARD_STRATEGIES]
    if strategies != expected_strategies:
        msg = f"plan.strategies must be {expected_strategies!r}"
        raise ValueError(msg)
    scenario_count = _expect_int(plan["scenario_count"], "plan.scenario_count")
    mapping_count = _expect_int(plan["mapping_count"], "plan.mapping_count")
    scenarios = plan["scenarios"]
    if type(scenarios) is not list:
        msg = "plan.scenarios must be an array"
        raise TypeError(msg)
    if scenario_count != len(scenarios):
        msg = "plan.scenario_count does not match scenarios"
        raise ValueError(msg)

    return _PlanHeader(
        source_digest=source_digest,
        corpus_digest=corpus_digest,
        strategies=strategies,
        mapping_count=mapping_count,
        scenarios=scenarios,
    )


def _validate_scenario_contract(
    scenario: Mapping[str, object], label: str
) -> tuple[str, str]:
    logical_key = _expect_str(scenario["logical_key"], f"{label}.logical_key")
    _execution_key(logical_key, "highest")
    source_definition = _validate_definition(logical_key, scenario["source_definition"])
    nab = _validate_nab(scenario["nab"], f"{label}.nab")
    expected_nab = _normalized_nab(logical_key, source_definition)
    if nab != expected_nab:
        msg = f"{label}.nab does not match its source definition"
        raise ValueError(msg)

    uv_value = scenario["uv"]
    if uv_value is not None:
        msg = f"{label}.uv must be null in plan schema 1"
        raise ValueError(msg)
    _validate_admission(scenario["admission"], uv_value, f"{label}.admission")
    expected_admission = _admission(source_definition)
    if scenario["admission"] != expected_admission:
        msg = f"{label}.admission does not match its source definition"
        raise ValueError(msg)

    scenario_digest = _expect_digest(
        scenario["scenario_digest"], f"{label}.scenario_digest"
    )
    scenario_payload = {
        "logical_key": logical_key,
        "source_definition": source_definition,
        "nab": nab,
        "uv": uv_value,
        "admission": scenario["admission"],
    }
    if scenario_digest != _digest(scenario_payload):
        msg = f"{label}.scenario_digest does not match its contract"
        raise ValueError(msg)
    return logical_key, scenario_digest


def _validate_scenario_executions(
    value: object,
    *,
    logical_key: str,
    scenario_digest: str,
    strategies: Sequence[str],
    label: str,
) -> list[str]:
    if type(value) is not list or len(value) != len(strategies):
        msg = f"{label} must cover every strategy once"
        raise ValueError(msg)
    return [
        _validate_execution(
            value[index],
            logical_key=logical_key,
            scenario_digest=scenario_digest,
            strategy=strategy,
            label=f"{label}[{index}]",
        )
        for index, strategy in enumerate(strategies)
    ]


def _validate_scenario(
    value: object, index: int, strategies: Sequence[str]
) -> tuple[str, list[str]]:
    label = f"plan.scenarios[{index}]"
    scenario = _expect_mapping(value, label)
    _expect_exact_fields(scenario, _SCENARIO_FIELDS, label)
    logical_key, scenario_digest = _validate_scenario_contract(scenario, label)
    execution_keys = _validate_scenario_executions(
        scenario["executions"],
        logical_key=logical_key,
        scenario_digest=scenario_digest,
        strategies=strategies,
        label=f"{label}.executions",
    )
    return logical_key, execution_keys


def _validate_plan_scenarios(header: _PlanHeader) -> None:
    logical_keys: list[str] = []
    execution_keys: list[str] = []
    for index, value in enumerate(header.scenarios):
        logical_key, scenario_execution_keys = _validate_scenario(
            value, index, header.strategies
        )
        logical_keys.append(logical_key)
        execution_keys.extend(scenario_execution_keys)

    _expect_unique(logical_keys, "plan logical keys")
    if logical_keys != sorted(logical_keys):
        msg = "plan scenarios must be sorted by logical key"
        raise ValueError(msg)
    _expect_unique(execution_keys, "plan execution keys")
    if header.mapping_count != len(execution_keys):
        msg = "plan.mapping_count does not match executions"
        raise ValueError(msg)


def _validate_plan_digests(header: _PlanHeader) -> None:
    derived_source_digest = _source_corpus_digest(header.scenarios)
    if header.source_digest != derived_source_digest:
        msg = "plan.source_corpus_digest does not match embedded source definitions"
        raise ValueError(msg)
    expected_corpus_digest = _digest(
        {
            "source_corpus_digest": header.source_digest,
            "strategies": header.strategies,
            "scenarios": header.scenarios,
        }
    )
    if header.corpus_digest != expected_corpus_digest:
        msg = "plan.corpus_digest does not match the parity corpus"
        raise ValueError(msg)


def validate_plan(value: object, *, expected_source_corpus_digest: str) -> None:
    """Validate a plan against a separately trusted canonical source digest."""
    trusted_source_digest = _expect_digest(
        expected_source_corpus_digest, "expected_source_corpus_digest"
    )
    header = _validate_plan_header(value, trusted_source_digest)
    _validate_plan_scenarios(header)
    _validate_plan_digests(header)


def render_plan(plan: object, *, expected_source_corpus_digest: str) -> str:
    """Validate and serialize *plan* as deterministic JSON."""
    validate_plan(plan, expected_source_corpus_digest=expected_source_corpus_digest)
    return json.dumps(plan, indent=2, sort_keys=True) + "\n"


def main() -> None:
    """Write the canonical static plan to standard output."""
    plan = compile_plan()
    source_digest = _expect_str(
        plan["source_corpus_digest"], "plan.source_corpus_digest"
    )
    sys.stdout.write(render_plan(plan, expected_source_corpus_digest=source_digest))


if __name__ == "__main__":
    main()
