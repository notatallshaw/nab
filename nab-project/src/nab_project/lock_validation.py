"""Producer records for checking a saved solution without choosing new versions."""

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
from .pyproject_files import (
    build_system_requires,
    dependency_groups,
    project_dependencies,
    project_name,
    project_optional_dependencies,
)
from .value import ValueType

if TYPE_CHECKING:
    from nab_provider.metadata import WheelMetadata
    from nab_provider.target import ResolveTarget

    from .inputs import ResolveInputs
    from .lockfile import LockInput

__all__ = [
    "capture_metadata",
    "content_digest",
    "make_validation_record",
    "snapshot_inputs",
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
    elif isinstance(value, (str, bool, int, float)):
        encoded = value
        if isinstance(value, str) and "://" in value:
            encoded = (
                _requirement(parse_requirement(value), base)
                if "@" in value.split("://", 1)[0]
                else _url(value, base)
            )
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


def _json(value: object) -> str:
    """Give hashes one representation regardless of mapping insertion order."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )


def content_digest(document: Mapping[str, Any]) -> str:
    """Hash install content, excluding disposable producer data."""
    content = {key: value for key, value in document.items() if key != "tool"}
    return hashlib.sha256(_json(content).encode()).hexdigest()


def capture_metadata(
    metadata: Mapping[tuple[str, Version], WheelMetadata], pins: Mapping[str, Version]
) -> dict[str, dict[str, Any]]:
    """Freeze effective metadata before a target's provider is released."""
    return {
        name: {
            "name": name,
            "version": str(version),
            "requires-python": str(item.requires_python or ""),
            "requires-dist": sorted(str(req) for req in item.requires_dist),
            "provides-extra": sorted(
                canonicalize_name(extra) for extra in item.provides_extra
            ),
        }
        for name, version in sorted(pins.items())
        for item in (metadata[(name, version)],)
    }


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
) -> dict[str, Any]:
    """Capture declarations, admission policy, targets and local metadata."""
    base = path.resolve().parent
    data = toml_io.load_path(path)
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
        "targets": [
            {
                "environment": dict(target.marker_env),
                "platform": _value(target.platform_spec, base) or {},
                "concrete-micro": target.concrete_micro,
                "micro-clauses": list(target.micro_clauses),
            }
            for target in targets
        ],
        "local": _value(local, base),
        "excluded": sorted(exclude),
        "build-requirements": build_requirements,
    }


def make_validation_record(
    document: Mapping[str, Any], lock_input: LockInput, base: Path
) -> dict[str, Any]:
    """Bind per-target dependency evidence to the exact emitted artifact choices."""
    metadata: dict[str, Any] = {}
    metadata_sets: dict[str, Any] = {}
    targets = []
    for label, target_lock in sorted(lock_input.targets.items()):
        selected: dict[str, str] = {}
        for name, item in sorted((target_lock.validation_metadata or {}).items()):
            normalized = _value(item, base)
            key = hashlib.sha256(_json(normalized).encode()).hexdigest()
            metadata[key] = normalized
            selected[name] = key
        selected_key = hashlib.sha256(_json(selected).encode()).hexdigest()
        metadata_sets[selected_key] = selected
        targets.append(
            {
                "label": label,
                "environment": dict(target_lock.target.marker_env),
                "selection": [list(member) for member in target_lock.target.selection],
                "micro-clauses": list(target_lock.target.micro_clauses),
                "platform": _value(target_lock.target.platform_spec, base) or {},
                "concrete-micro": target_lock.target.concrete_micro,
                "metadata-set": selected_key,
            }
        )
    inputs = lock_input.validation_inputs
    record = {
        "version": VALIDATION_VERSION,
        "inputs": inputs,
        "inputs-sha256": hashlib.sha256(_json(inputs).encode()).hexdigest(),
        "contents-sha256": content_digest(document),
        "metadata": metadata,
        "metadata-sets": metadata_sets,
        "targets": targets,
        "cacheable": inputs is not None
        and all(not local.get("dynamic") for local in inputs["local"].values()),
    }
    record["record-sha256"] = hashlib.sha256(_json(record).encode()).hexdigest()
    return record
