"""Compact fingerprints for checking a saved solution without choosing new versions."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider._vendor.packaging.specifiers import SpecifierSet
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider._vendor.packaging.version import Version
from nab_provider.pep508 import parse_requirement
from nab_provider.policy import (
    BuildPolicy,
    DecisionOrder,
    DistPolicy,
    ResolutionStrategy,
)
from nab_provider.requirements_file import (
    expand_extra_requirements,
    resolve_groups_to_requirements,
)
from nab_provider.serialization import SimpleSerialization
from nab_provider.value import Record, SlottedValue, field_names
from nab_provider.vcs_admission import VcsPolicy

from . import toml_io
from ._lockfile.builder import strip_userinfo
from .conflicts import ConflictKind, ConflictPolicy
from .input_source import InputFormat, read_input_document
from .pyproject_files import (
    build_system_requires,
    dependency_groups,
    project_dependencies,
    project_name,
    project_optional_dependencies,
)
from .value import ValueType

if TYPE_CHECKING:
    from nab_provider.target import ResolveTarget

    from .inputs import ResolveInputs

__all__ = [
    "VALIDATION_VERSION",
    "content_digest",
    "make_validation_record",
    "selection_inputs_digest",
    "snapshot_inputs",
    "validation_digest",
]

# This versions the validation format, independently of Nab's release number.
VALIDATION_VERSION = 1
_PREFERENCES = frozenset({"resolution", "decision_order", "constraints"})
_DERIVED_FIELDS = frozenset({"version_range", "source_label", "name_keyed"})


def _relative(path: Path, base: Path) -> str:
    """Keep source identities portable with the project that declared them."""
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:
        # Windows paths on different drives have no relative representation.
        return path.as_posix()


def _url(url: str, base: Path) -> str:
    """Remove URL credentials and normalize local source locations."""
    parts = urlsplit(url)
    if parts.scheme == "file" and parts.netloc in {"", "localhost"}:
        return "file:" + _relative(Path(unquote(parts.path)), base)
    return strip_userinfo(url)


def _requirement(requirement: Requirement, base: Path) -> str:
    """Normalize one declaration without mutating the provider's parsed value."""
    normalized = parse_requirement(str(requirement))
    normalized.name = canonicalize_name(normalized.name)
    normalized.extras = {canonicalize_name(extra) for extra in normalized.extras}
    if normalized.url is not None:
        normalized.url = _url(normalized.url, base)
    return str(normalized)


def _value(value: object, base: Path) -> Any:
    """Encode owned records at the validation-format boundary."""
    encoded: object
    if value is None:
        encoded = None
    elif isinstance(value, (str, bool, int, float)):
        encoded = value
        if isinstance(value, str) and "://" in value:
            encoded = (
                _requirement(parse_requirement(value), base)
                if "@" in value.split("://", 1)[0]
                else _url(value, base)
            )
    elif isinstance(
        value,
        (
            BuildPolicy,
            DecisionOrder,
            DistPolicy,
            ResolutionStrategy,
            SimpleSerialization,
            VcsPolicy,
            ConflictKind,
            ConflictPolicy,
        ),
    ):
        encoded = value.value
    elif isinstance(value, Requirement):
        encoded = _requirement(value, base)
    elif isinstance(value, (Version, SpecifierSet)):
        encoded = str(value)
    elif isinstance(value, datetime):
        encoded = value.isoformat()
    elif isinstance(value, Path):
        encoded = _relative(value, base)
    else:
        encoded = _collection(value, base)
    return encoded


def _collection(value: object, base: Path) -> Any:
    """Encode record and container fields after scalar normalization."""
    if isinstance(value, Mapping):
        return {
            str(key): _value(item, base)
            for key, item in sorted(value.items())
            if item is not None
        }
    if isinstance(value, (set, frozenset)):
        return sorted((_value(item, base) for item in value), key=_json)
    if isinstance(value, (tuple, list)):
        return [_value(item, base) for item in value]
    if isinstance(value, (Record, ValueType, SlottedValue)):
        names = (
            field_names(value) if isinstance(value, Record) else value.__match_args__
        )
        # Field names are the record owners' explicit serialization interface.
        fields = {
            name: getattr(value, name) for name in names if name not in _DERIVED_FIELDS
        }
        if "path" in fields and isinstance(fields["path"], str):
            fields["path"] = _relative(Path(fields["path"]), base)
        return _value(fields, base)
    message = f"unsupported validation input type: {type(value).__name__}"
    raise TypeError(message)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return datetime.isoformat(value)
    return str(value)


def _json(value: object) -> str:
    """Give hashes one representation regardless of mapping insertion order."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_json_default,
    )


def validation_digest(value: object) -> str:
    """Hash a canonical validation-format value."""
    return hashlib.sha256(_json(value).encode()).hexdigest()


def content_digest(document: Mapping[str, Any]) -> str:
    """Bind producer data to standard installer fields."""
    return validation_digest(
        {key: value for key, value in document.items() if key != "tool"}
    )


def _requirements_without_bounds(requirements: Sequence[str]) -> list[str]:
    """Retain dependency identities without their version bounds."""
    result = set()
    for text in requirements:
        requirement = parse_requirement(text)
        requirement.name = canonicalize_name(requirement.name)
        requirement.extras = {canonicalize_name(extra) for extra in requirement.extras}
        requirement.specifier = SpecifierSet()
        result.add(str(requirement))
    return sorted(result)


def selection_inputs_digest(inputs: Mapping[str, Any]) -> str:
    """Hash inputs excluding dependency bounds, constraints and local versions."""
    local = {}
    for name, project in inputs["local"].items():
        fields = {
            key: value
            for key, value in project.items()
            if key not in {"version", "dependencies", "optional-dependencies"}
        }
        local[name] = {
            **fields,
            "dependencies": _requirements_without_bounds(
                project.get("dependencies", [])
            ),
            "optional-dependencies": {
                extra: _requirements_without_bounds(requirements)
                for extra, requirements in project.get(
                    "optional-dependencies", {}
                ).items()
            },
        }
    selection_inputs = {
        key: value
        for key, value in inputs.items()
        if key not in {"roots", "constraints", "local"}
    }
    selection_inputs["roots"] = {
        scope: _requirements_without_bounds(requirements)
        for scope, requirements in inputs["roots"].items()
    }
    selection_inputs["local"] = local
    return validation_digest(selection_inputs)


def _project_inputs(path: Path) -> dict[str, Any]:
    """Read local dependency inputs without executing a backend."""
    try:
        data = toml_io.load_path(path)
    except FileNotFoundError:
        return {"dynamic": ["metadata"]}
    project = data.get("project", {})
    if not isinstance(project, dict):
        message = f"{path}: project must be a table"
        raise TypeError(message)
    fields = {
        name: project[name]
        for name in (
            "name",
            "version",
            "requires-python",
            "dependencies",
            "optional-dependencies",
        )
        if name in project
    }
    dynamic = set(project.get("dynamic", [])) & set(
        fields.keys()
        | {"dependencies", "optional-dependencies", "requires-python", "version"}
    )
    if not dynamic and (
        not isinstance(project.get("name"), str)
        or not isinstance(project.get("version"), str)
    ):
        dynamic.add("metadata")
    if dynamic:
        fields["dynamic"] = sorted(dynamic)
    return fields


def _requirements(requirements: Iterable[Requirement], base: Path) -> list[str]:
    """Canonicalize a conjunction of dependency declarations."""
    return sorted({_requirement(requirement, base) for requirement in requirements})


def snapshot_inputs(
    path: Path,
    inputs: ResolveInputs,
    targets: Sequence[ResolveTarget],
    *,
    groups: Sequence[str] = (),
    extras: Sequence[str] = (),
    build_requirements: bool = False,
    exclude: frozenset[str] = frozenset(),
    input_format: InputFormat = InputFormat.PROJECT,
) -> dict[str, Any]:
    """Capture declarations, admission policy, targets and local metadata."""
    base = path.resolve().parent
    data = read_input_document(path, input_format=input_format)
    roots: dict[str, list[str]] = {}
    if build_requirements:
        roots["build"] = _requirements(build_system_requires(data, path), base)
    else:
        roots["base"] = (
            _requirements(project_dependencies(data), base) if "project" in data else []
        )
        optional = project_optional_dependencies(data)
        for extra in sorted(extras):
            roots["extra:" + canonicalize_name(extra)] = _requirements(
                expand_extra_requirements(optional, project_name(data), [extra]), base
            )
        table = dependency_groups(data)
        for group in sorted(
            (set(groups) | set(inputs.default_groups))
            - {inputs.base_group, inputs.build_group}
        ):
            roots["group:" + canonicalize_name(group)] = _requirements(
                resolve_groups_to_requirements(table, [group]), base
            )
        if inputs.build_group is not None:
            roots["build"] = _requirements(build_system_requires(data, path), base)
    policy = {
        name: getattr(inputs, name)
        for name in inputs.__match_args__
        if name not in _PREFERENCES
    }
    local: dict[str, Any] = {}
    for source in inputs.local_sources:
        directory = Path(source.path)
        if source.subdirectory is not None:
            directory /= source.subdirectory
        local[canonicalize_name(source.name)] = _project_inputs(
            directory / "pyproject.toml"
        )
    return {
        "roots": roots,
        "constraints": _requirements(
            (parse_requirement(text) for text in inputs.constraints), base
        ),
        "policy-sha256": hashlib.sha256(
            _json(_value(policy, base)).encode()
        ).hexdigest(),
        "targets": sorted(
            [
                {
                    "environment": dict(target.marker_env),
                    "platform": _value(target.platform_spec, base) or {},
                    "tags": _value(target.tags, base),
                    "concrete-micro": target.concrete_micro,
                    "micro-clauses": list(target.micro_clauses),
                }
                for target in targets
            ],
            key=_json,
        ),
        "local": _value(local, base),
        "excluded": sorted(exclude),
        "build-requirements": build_requirements,
    }


def make_validation_record(
    document: Mapping[str, Any], inputs: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep fingerprints while leaving package selections in standard Pylock fields."""
    return {
        "version": VALIDATION_VERSION,
        "all-inputs-sha256": validation_digest(inputs),
        "selection-inputs-sha256": selection_inputs_digest(inputs),
        "contents-sha256": content_digest(document),
    }
