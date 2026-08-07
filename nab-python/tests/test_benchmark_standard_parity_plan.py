"""Contracts for the static standard Nab/uv parity-plan compiler."""

from __future__ import annotations

import copy
import importlib.util
import json
import socket
import subprocess
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
_LOGICAL_SCENARIO_COUNT = 558
_SOURCE_RUNNABLE_SCENARIO_COUNT = 536
_SOURCE_UNSUPPORTED_SCENARIO_COUNT = 22
_MAPPING_IDENTITY_COUNT = 1_674
_SOURCE_RUNNABLE_EXECUTION_COUNT = 1_608
_TOP_LEVEL_FIELDS = {
    "schema",
    "source_corpus_digest",
    "corpus_digest",
    "strategies",
    "scenario_count",
    "mapping_count",
    "scenarios",
}
_SCENARIO_FIELDS = {
    "logical_key",
    "source_definition",
    "scenario_digest",
    "nab",
    "uv",
    "admission",
    "executions",
}
_NAB_FIELDS = {
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
_ADMISSION_FIELDS = {"status", "reasons", "postconditions"}
_EXECUTION_FIELDS = {
    "execution_key",
    "mapping_digest",
    "strategy",
    "nab_settings",
    "uv_settings",
}


def _compiler() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_benchmark_standard_parity_plan", _BENCHMARKS / "standard_parity_plan.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _definition(**overrides: object) -> dict[str, object]:
    return {
        "python_version": "3.11",
        "datetime": "2025-01-02T03:04:05Z",
        "requirements": ["demo>=1"],
        **overrides,
    }


def _write_scenarios(tmp_path: Path, definitions: dict[str, dict[str, object]]) -> Path:
    def toml_value(value: object) -> str:
        if isinstance(value, dict):
            items = (f"{key} = {toml_value(item)}" for key, item in value.items())
            return "{ " + ", ".join(items) + " }"
        if isinstance(value, list):
            return "[" + ", ".join(toml_value(item) for item in value) + "]"
        return json.dumps(value)

    lines: list[str] = []
    for name, definition in definitions.items():
        lines.append(f"[{name}]")
        for key, value in definition.items():
            lines.append(f"{key} = {toml_value(value)}")
        lines.append("")
    path = tmp_path / "cases.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _single_plan(
    tmp_path: Path, **overrides: object
) -> tuple[ModuleType, dict[str, Any]]:
    module = _compiler()
    path = _write_scenarios(tmp_path, {"example": _definition(**overrides)})
    return module, module.compile_plan([path])


def _scenario(plan: dict[str, Any], logical_key: str) -> dict[str, Any]:
    scenarios = plan["scenarios"]
    assert isinstance(scenarios, list)
    return next(item for item in scenarios if item["logical_key"] == logical_key)


def _trusted_source_digest(plan: dict[str, Any]) -> str:
    value = plan["source_corpus_digest"]
    assert isinstance(value, str)
    return value


def _rehash(module: ModuleType, plan: dict[str, Any], *, source: bool = False) -> None:
    """Rebuild every digest affected by a forged plan edit."""
    for scenario in plan["scenarios"]:
        scenario_payload = {
            "logical_key": scenario["logical_key"],
            "source_definition": scenario["source_definition"],
            "nab": scenario["nab"],
            "uv": scenario["uv"],
            "admission": scenario["admission"],
        }
        scenario["scenario_digest"] = module._digest(scenario_payload)
        for execution in scenario["executions"]:
            execution_payload = {
                "execution_key": execution["execution_key"],
                "strategy": execution["strategy"],
                "nab_settings": execution["nab_settings"],
                "uv_settings": execution["uv_settings"],
            }
            execution["mapping_digest"] = module._digest(
                {
                    "scenario_digest": scenario["scenario_digest"],
                    **execution_payload,
                }
            )
    if source:
        plan["source_corpus_digest"] = module._source_corpus_digest(plan["scenarios"])
    plan["corpus_digest"] = module._digest(
        {
            "source_corpus_digest": plan["source_corpus_digest"],
            "strategies": plan["strategies"],
            "scenarios": plan["scenarios"],
        }
    )


def _assert_plan_shape(
    module: ModuleType,
    plan: dict[str, Any],
    scenarios: list[dict[str, Any]],
    executions: list[dict[str, Any]],
) -> None:
    """Check the plan schema, totals, and unique identities."""
    assert set(plan) == _TOP_LEVEL_FIELDS
    assert plan["schema"] == module.PLAN_SCHEMA
    assert plan["strategies"] == ["highest", "lowest", "lowest-direct"]
    assert plan["scenario_count"] == len(scenarios) == _LOGICAL_SCENARIO_COUNT
    assert plan["mapping_count"] == len(executions) == _MAPPING_IDENTITY_COUNT
    assert all(set(item) == _SCENARIO_FIELDS for item in scenarios)
    assert all(set(item["nab"]) == _NAB_FIELDS for item in scenarios)
    assert all(set(item["admission"]) == _ADMISSION_FIELDS for item in scenarios)
    assert all(set(item) == _EXECUTION_FIELDS for item in executions)
    assert len({item["logical_key"] for item in scenarios}) == _LOGICAL_SCENARIO_COUNT
    assert (
        len({item["execution_key"] for item in executions}) == _MAPPING_IDENTITY_COUNT
    )


def _assert_source_census(scenarios: list[dict[str, Any]]) -> None:
    """Check runnable counts without dropping unsupported source identities."""
    source_runnable = [
        scenario
        for scenario in scenarios
        if "unsupported_reason" not in scenario["source_definition"]
    ]
    source_unsupported = [
        scenario
        for scenario in scenarios
        if "unsupported_reason" in scenario["source_definition"]
    ]

    assert len(source_runnable) == _SOURCE_RUNNABLE_SCENARIO_COUNT
    assert len(source_unsupported) == _SOURCE_UNSUPPORTED_SCENARIO_COUNT
    assert (
        sum(len(item["executions"]) for item in source_runnable)
        == _SOURCE_RUNNABLE_EXECUTION_COUNT
    )
    assert all(
        ("declared-unsupported" in item["admission"]["reasons"])
        == ("unsupported_reason" in item["source_definition"])
        for item in scenarios
    )


def _assert_fail_closed_plan(
    scenarios: list[dict[str, Any]], executions: list[dict[str, Any]]
) -> None:
    """Check that schema 1 exposes no unverified uv mapping."""
    assert Counter(item["admission"]["status"] for item in scenarios) == {
        "unsupported": 558,
    }
    assert sum(item["uv"] is not None for item in scenarios) == 0
    assert sum(execution["uv_settings"] is not None for execution in executions) == 0
    assert all(
        item["uv"] is None and item["admission"]["reasons"]
        for item in scenarios
        if item["admission"]["status"] == "unsupported"
    )
    assert all(
        execution["uv_settings"] is None
        for item in scenarios
        if item["uv"] is None
        for execution in item["executions"]
    )
    assert all(
        {
            "build-policy-no-equivalent",
            "prerelease-policy-unverified",
            "requires-python-upper-bound-policy",
            "missing-expected-outcome",
        }
        <= set(item["admission"]["reasons"])
        for item in scenarios
    )


def test_canonical_plan_has_complete_unique_snapshot() -> None:
    module = _compiler()
    plan = module.compile_plan()
    scenarios = plan["scenarios"]
    assert isinstance(scenarios, list)
    executions = [
        execution for scenario in scenarios for execution in scenario["executions"]
    ]

    _assert_plan_shape(module, plan, scenarios, executions)
    _assert_source_census(scenarios)
    _assert_fail_closed_plan(scenarios, executions)
    module.validate_plan(
        plan, expected_source_corpus_digest=_trusted_source_digest(plan)
    )


def test_plan_and_all_nested_digests_are_stable_and_checked() -> None:
    module = _compiler()
    first = module.compile_plan()
    second = module.compile_plan()
    trusted = _trusted_source_digest(first)

    assert module.render_plan(
        first, expected_source_corpus_digest=trusted
    ) == module.render_plan(second, expected_source_corpus_digest=trusted)
    assert first["source_corpus_digest"] == second["source_corpus_digest"]
    assert first["corpus_digest"] == second["corpus_digest"]

    scenario_tamper = copy.deepcopy(first)
    scenario_tamper["scenarios"][0]["scenario_digest"] = "0" * 64
    with pytest.raises(ValueError, match="scenario_digest"):
        module.validate_plan(scenario_tamper, expected_source_corpus_digest=trusted)

    mapping_tamper = copy.deepcopy(first)
    mapping_tamper["scenarios"][0]["executions"][0]["mapping_digest"] = "0" * 64
    with pytest.raises(ValueError, match="mapping_digest"):
        module.validate_plan(mapping_tamper, expected_source_corpus_digest=trusted)

    corpus_tamper = copy.deepcopy(first)
    corpus_tamper["corpus_digest"] = "0" * 64
    with pytest.raises(ValueError, match="corpus_digest"):
        module.validate_plan(corpus_tamper, expected_source_corpus_digest=trusted)


def test_nab_strategy_translation_omits_default_and_uv_stays_unavailable() -> None:
    module = _compiler()
    scenario = _scenario(module.compile_plan(), "quick:requests")

    assert [item["strategy"] for item in scenario["executions"]] == [
        "highest",
        "lowest",
        "lowest-direct",
    ]
    assert scenario["executions"][0]["nab_settings"] == {}
    assert scenario["executions"][0]["uv_settings"] is None
    assert scenario["executions"][1]["nab_settings"] == {"resolution": "lowest"}
    assert scenario["executions"][1]["uv_settings"] is None
    assert scenario["executions"][2]["nab_settings"] == {"resolution": "lowest-direct"}
    assert scenario["executions"][2]["uv_settings"] is None


def test_utc_designator_is_preserved(tmp_path: Path) -> None:
    _module, plan = _single_plan(tmp_path, datetime="2025-01-02T03:04:05Z")
    scenario = plan["scenarios"][0]

    assert scenario["nab"]["uploaded_prior_to"] == "2025-01-02T03:04:05Z"


def test_cutoff_is_utc_and_declared_index_order_is_preserved(tmp_path: Path) -> None:
    module, plan = _single_plan(
        tmp_path,
        datetime="2025-01-02T05:34:05+02:30",
        indexes=[
            {"name": "second", "url": "https://second.invalid/simple/"},
            {"name": "first", "url": "https://first.invalid/simple/"},
        ],
    )
    scenario = plan["scenarios"][0]

    assert scenario["nab"]["uploaded_prior_to"] == "2025-01-02T03:04:05Z"
    assert [item["name"] for item in scenario["nab"]["indexes"]] == [
        "second",
        "first",
    ]
    assert scenario["admission"] == {
        "status": "unsupported",
        "reasons": [
            "build-policy-no-equivalent",
            "custom-index-routing-no-equivalent",
            "prerelease-policy-unverified",
            "requires-python-upper-bound-policy",
            "missing-expected-outcome",
        ],
        "postconditions": [],
    }
    assert scenario["uv"] is None
    module.validate_plan(
        plan, expected_source_corpus_digest=_trusted_source_digest(plan)
    )


def test_plain_host_is_normalized_without_uv_inference(tmp_path: Path) -> None:
    _module, plan = _single_plan(tmp_path)
    scenario = plan["scenarios"][0]

    assert scenario["nab"]["target"] == {
        "kind": "host",
        "marker_environment": {},
    }
    assert scenario["nab"]["trust_unverified_sdist_deps"] is False
    assert scenario["uv"] is None
    assert scenario["admission"]["status"] == "unsupported"
    assert scenario["admission"]["reasons"] == [
        "build-policy-no-equivalent",
        "prerelease-policy-unverified",
        "requires-python-upper-bound-policy",
        "missing-expected-outcome",
    ]


@pytest.mark.parametrize(
    ("overrides", "reason", "marker_environment"),
    [
        (
            {"platform_system": "Linux"},
            "partial-target-platform-system",
            {"platform_system": "Linux"},
        ),
        (
            {
                "marker_environment": {
                    "platform_system": "Windows",
                    "sys_platform": "win32",
                    "platform_machine": "AMD64",
                }
            },
            "partial-target-marker-environment",
            {
                "platform_system": "Windows",
                "sys_platform": "win32",
                "platform_machine": "AMD64",
            },
        ),
    ],
)
def test_partial_targets_are_retained_for_nab_but_refused_for_uv(
    tmp_path: Path,
    overrides: dict[str, object],
    reason: str,
    marker_environment: dict[str, str],
) -> None:
    _module, plan = _single_plan(tmp_path, **overrides)
    scenario = plan["scenarios"][0]

    assert scenario["nab"]["target"] == {
        "kind": "host-with-marker-overlay",
        "marker_environment": marker_environment,
    }
    assert scenario["admission"]["status"] == "unsupported"
    assert reason in scenario["admission"]["reasons"]
    assert scenario["admission"]["postconditions"] == []
    assert scenario["uv"] is None
    assert all(item["uv_settings"] is None for item in scenario["executions"])


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"unsupported_reason": "not available"}, "declared-unsupported"),
        (
            {"build_packages": ["wheel", "build"]},
            "build-package-allowlist-no-equivalent",
        ),
        ({"trust_unverified_sdist_deps": True}, "trust-unverified-sdist-deps"),
        (
            {
                "indexes": [
                    {"name": "pypi", "url": "https://pypi.org/simple/"},
                    {"name": "other", "url": "https://example.invalid/simple/"},
                ],
                "index_routes": [{"name": "demo", "index": "other"}],
            },
            "custom-index-routing-no-equivalent",
        ),
        (
            {
                "project_name": "demo-project",
                "project_extras": ["all"],
                "optional_dependencies": {"all": ["extra-dependency"]},
            },
            "project-shape-not-materialized",
        ),
        (
            {
                "vcs_policy": "allow",
                "vcs_allowed_schemes": ["git+https"],
                "vcs_allowed_repos": ["https://example.invalid/demo"],
                "vcs_require_pin": True,
            },
            "project-shape-not-materialized",
        ),
    ],
)
def test_declared_policy_shapes_fail_closed(
    tmp_path: Path, overrides: dict[str, object], reason: str
) -> None:
    _module, plan = _single_plan(tmp_path, **overrides)
    scenario = plan["scenarios"][0]

    assert scenario["admission"]["status"] == "unsupported"
    assert reason in scenario["admission"]["reasons"]
    assert scenario["uv"] is None


@pytest.mark.parametrize("reason", list(_compiler().UNSUPPORTED_REASONS))
def test_every_closed_unsupported_reason_is_accepted(reason: str) -> None:
    module = _compiler()
    module._validate_admission(
        {
            "status": "unsupported",
            "reasons": [reason],
            "postconditions": [],
        },
        None,
        "admission",
    )


def test_unsupported_admission_rejects_uv_translation() -> None:
    module = _compiler()

    with pytest.raises(ValueError, match="null uv translation"):
        module._validate_admission(
            {
                "status": "unsupported",
                "reasons": ["missing-expected-outcome"],
                "postconditions": [],
            },
            {"translation": "present"},
            "admission",
        )


@pytest.mark.parametrize("postcondition", list(_compiler().RUNTIME_POSTCONDITIONS))
def test_runtime_postconditions_require_a_later_schema(postcondition: str) -> None:
    module = _compiler()
    with pytest.raises(ValueError, match="conditional admission is unavailable"):
        module._validate_admission(
            {
                "status": "conditional",
                "reasons": [],
                "postconditions": [postcondition],
            },
            {"translation": "present"},
            "admission",
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda plan: plan.__setitem__("unknown", None), "fields do not match"),
        (lambda plan: plan.pop("mapping_count"), "fields do not match"),
        (lambda plan: plan.update(schema=True), "must be an integer"),
        (lambda plan: plan.update(scenario_count=False), "must be an integer"),
        (
            lambda plan: plan["scenarios"][0]["nab"].__setitem__("unknown", None),
            "fields do not match",
        ),
        (
            lambda plan: plan["scenarios"][0].__setitem__("unknown", None),
            "fields do not match",
        ),
        (
            lambda plan: plan["scenarios"][0]["nab"]["target"].__setitem__(
                "platform", None
            ),
            "fields do not match",
        ),
        (
            lambda plan: plan["scenarios"][0]["nab"]["vcs"].__setitem__(
                "unknown", None
            ),
            "fields do not match",
        ),
        (
            lambda plan: plan["scenarios"][0]["admission"].__setitem__("unknown", None),
            "fields do not match",
        ),
        (
            lambda plan: plan["scenarios"][0]["executions"][0].__setitem__(
                "unknown", None
            ),
            "fields do not match",
        ),
        (
            lambda plan: plan["scenarios"][0]["admission"]["reasons"].append(
                "unknown-reason"
            ),
            "unknown values",
        ),
        (
            lambda plan: plan["scenarios"][0]["admission"].update(status="future"),
            "status is unknown",
        ),
        (
            lambda plan: plan["scenarios"][0]["admission"]["postconditions"].append(
                "unknown-postcondition"
            ),
            "unknown values",
        ),
        (
            lambda plan: plan["scenarios"][0]["executions"][0][
                "nab_settings"
            ].__setitem__("resolution", "highest"),
            "must be {}",
        ),
    ],
)
def test_plan_schema_rejects_unknown_missing_and_coerced_values(
    tmp_path: Path, mutation: Any, error: str
) -> None:
    module, plan = _single_plan(tmp_path)
    mutation(plan)

    with pytest.raises((TypeError, ValueError), match=error):
        module.validate_plan(
            plan, expected_source_corpus_digest=_trusted_source_digest(plan)
        )


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("trust_unverified_sdist_deps", True),
        ("build_packages", ["demo"]),
        ("index_routes", [{"name": "demo", "index": "pypi"}]),
        (
            "project",
            {
                "name": "demo-project",
                "extras": ["all"],
                "optional_dependencies": {"all": ["extra-dependency"]},
            },
        ),
        (
            "vcs",
            {
                "policy": "allow",
                "allowed_schemes": ["git+https"],
                "allowed_repos": [],
                "require_pin": True,
            },
        ),
    ],
)
def test_recomputed_hashes_cannot_forge_nab_only_semantics(
    tmp_path: Path, field: str, forged_value: object
) -> None:
    module, plan = _single_plan(tmp_path)
    trusted = _trusted_source_digest(plan)
    plan["scenarios"][0]["nab"][field] = forged_value
    _rehash(module, plan)

    with pytest.raises(ValueError, match="nab does not match its source"):
        module.validate_plan(plan, expected_source_corpus_digest=trusted)


@pytest.mark.parametrize("status", ["exact", "conditional"])
def test_recomputed_hashes_cannot_forge_admission_status(
    tmp_path: Path, status: str
) -> None:
    module, plan = _single_plan(tmp_path)
    trusted = _trusted_source_digest(plan)
    plan["scenarios"][0]["admission"] = {
        "status": status,
        "reasons": [],
        "postconditions": ([] if status == "exact" else ["artifact-domain-equivalent"]),
    }
    _rehash(module, plan)

    with pytest.raises(ValueError, match=f"{status} admission is unavailable"):
        module.validate_plan(plan, expected_source_corpus_digest=trusted)


def test_recomputed_hashes_cannot_drop_source_derived_reason(tmp_path: Path) -> None:
    module, plan = _single_plan(tmp_path)
    trusted = _trusted_source_digest(plan)
    plan["scenarios"][0]["admission"]["reasons"].remove("missing-expected-outcome")
    _rehash(module, plan)

    with pytest.raises(ValueError, match="admission does not match its source"):
        module.validate_plan(plan, expected_source_corpus_digest=trusted)


def test_full_source_replacement_fails_the_trusted_digest(tmp_path: Path) -> None:
    module, plan = _single_plan(tmp_path)
    trusted = _trusted_source_digest(plan)
    scenario = plan["scenarios"][0]
    scenario["source_definition"]["requirements"].append("replacement")
    scenario["nab"]["requirements"].append("replacement")
    _rehash(module, plan, source=True)

    assert plan["source_corpus_digest"] != trusted
    with pytest.raises(ValueError, match="does not match the canonical corpus"):
        module.validate_plan(plan, expected_source_corpus_digest=trusted)


def test_source_digest_is_bound_into_corpus_digest(tmp_path: Path) -> None:
    module, plan = _single_plan(tmp_path)
    trusted = _trusted_source_digest(plan)
    original_corpus_digest = plan["corpus_digest"]
    plan["source_corpus_digest"] = "0" * 64
    _rehash(module, plan)

    assert plan["corpus_digest"] != original_corpus_digest
    with pytest.raises(ValueError, match="does not match the canonical corpus"):
        module.validate_plan(plan, expected_source_corpus_digest=trusted)
    with pytest.raises(ValueError, match="embedded source definitions"):
        module.validate_plan(
            plan, expected_source_corpus_digest=plan["source_corpus_digest"]
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"unexpected": "value"}, "unknown fields"),
        ({"trust_unverified_sdist_deps": 1}, "must be a boolean"),
        ({"requirements": [True]}, "must be a non-empty string"),
        ({"datetime": True}, "must be a non-empty string"),
        (
            {
                "indexes": [
                    {
                        "name": "pypi",
                        "url": "https://pypi.org/simple/",
                        "priority": 1,
                    }
                ]
            },
            "fields do not match",
        ),
    ],
)
def test_source_schema_rejects_unknown_fields_and_type_coercion(
    tmp_path: Path, overrides: dict[str, object], error: str
) -> None:
    module = _compiler()
    path = _write_scenarios(tmp_path, {"example": _definition(**overrides)})

    with pytest.raises((TypeError, ValueError), match=error):
        module.compile_plan([path])


def test_compiler_does_not_use_subprocess_or_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _compiler()
    path = _write_scenarios(tmp_path, {"example": _definition()})
    assert module._standard.cache_info().currsize == 0

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the static compiler crossed an execution boundary")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)

    plan = module.compile_plan([path])

    assert plan["scenario_count"] == 1
    assert plan["mapping_count"] == 3


@pytest.mark.parametrize("status", ["exact", "conditional"])
def test_non_unsupported_admission_requires_a_later_schema(status: str) -> None:
    module = _compiler()

    with pytest.raises(ValueError, match=f"{status} admission is unavailable"):
        module._validate_admission(
            {
                "status": status,
                "reasons": [],
                "postconditions": (
                    [] if status == "exact" else ["artifact-domain-equivalent"]
                ),
            },
            {"translation": "present"},
            "admission",
        )


def test_import_does_not_mutate_sys_path_or_load_the_corpus() -> None:
    before = list(sys.path)

    module = _compiler()

    assert sys.path == before
    assert module._standard.cache_info().currsize == 0
