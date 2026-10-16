"""Read and select development dependency sources."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

import tomli

from nab_provider._vendor.packaging.markers import InvalidMarker, Marker
from nab_provider._vendor.packaging.utils import InvalidName, canonicalize_name
from nab_provider.archive import ArchiveRequest, ArchiveRequestError
from nab_provider.errors import ConfigError
from nab_provider.marker_holds import dependency_marker_holds

from . import toml_io

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from nab_provider._vendor.packaging.requirements import Requirement
    from nab_provider.target import ResolveTarget

    from ._resolve.engine import InstallContexts
    from .lockfile import TargetLock

__all__ = [
    "DevelopSource",
    "SelectedSources",
    "expand_credentials",
    "read_develop_sources",
    "select_sources",
]

_logger = logging.getLogger(__name__)
_KINDS = frozenset({"path", "url", "git", "hg", "bzr", "svn", "index"})
_SELECTORS = frozenset({"marker", "extra", "group"})
_FIELDS = {
    "path": {"editable", "subdirectory", "hashes"},
    "url": {"subdirectory", "hashes"},
    "git": {"rev", "tag", "branch", "subdirectory"},
    "hg": {"rev", "subdirectory"},
    "bzr": {"rev", "subdirectory"},
    "svn": {"rev", "subdirectory"},
    "index": set(),
}
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class DevelopSource:
    """One source alternative, with paths anchored to its declaring file."""

    name: str
    kind: str
    location: str
    editable: bool = False
    subdirectory: str | None = None
    revision: str | None = None
    revision_kind: str | None = None
    hashes: tuple[tuple[str, str], ...] = ()
    marker: Marker | None = None
    extra: str | None = None
    group: str | None = None
    label: str = field(default="", compare=False)

    @property
    def identity(self) -> tuple[object, ...]:
        """Identify the requested origin independently of its selectors."""
        return (
            self.kind,
            self.location,
            self.editable,
            self.subdirectory,
            self.revision,
            self.revision_kind,
            self.hashes,
        )

    def matches(
        self, environment: Mapping[str, str], context: tuple[str, str] | None
    ) -> bool:
        """Whether this entry applies to one target dependency occurrence."""
        if self.extra is not None and context != ("extra", self.extra):
            return False
        if self.group is not None and context != ("group", self.group):
            return False
        return self.marker is None or dependency_marker_holds(self.marker, environment)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"{label} must be a non-empty string"
        raise ConfigError(msg)
    return value


def _location(value: object, kind: str, directory: Path, label: str) -> str:
    """Resolve local paths and reject repository URLs with a VCS prefix."""
    location = _text(value, f"{label}.{kind}")
    if kind == "path":
        path = directory / location
        location = str(path.parent.resolve() / path.name)
    elif kind == "url":
        try:
            urlsplit(location)
            ArchiveRequest.parse(location)
        except (ValueError, ArchiveRequestError) as exc:
            msg = f"{label}.url: {exc}"
            raise ConfigError(msg) from exc
    elif kind in {"git", "hg", "bzr", "svn"} and location.startswith(
        tuple(f"{k}+" for k in _KINDS)
    ):
        msg = f"{label}.{kind} must not contain a VCS prefix"
        raise ConfigError(msg)
    return location


def _revision(raw: Mapping[str, Any], label: str) -> tuple[str | None, str | None]:
    """Read one typed revision selector."""
    revisions = {
        key: _text(raw[key], f"{label}.{key}")
        for key in ("rev", "tag", "branch")
        if key in raw
    }
    if len(revisions) > 1:
        msg = f"{label} cannot combine rev, tag and branch"
        raise ConfigError(msg)
    return next(iter(revisions), None), next(iter(revisions.values()), None)


def _path_options(
    raw: Mapping[str, Any], kind: str, location: str, label: str
) -> tuple[bool, str | None]:
    """Validate editability and the project directory within an origin."""
    editable = raw.get("editable", False)
    if not isinstance(editable, bool):
        msg = f"{label}.editable must be a boolean"
        raise ConfigError(msg)
    subdir = raw.get("subdirectory")
    if subdir is not None:
        subdir = _text(subdir, f"{label}.subdirectory")
        if Path(subdir).is_absolute() or ".." in subdir.split("/") or "\\" in subdir:
            msg = (
                f"{label}.subdirectory {subdir!r} escapes the source tree; "
                "use a relative path below the source"
            )
            raise ConfigError(msg)
    filename = urlsplit(location).path if kind == "url" else location
    if filename.endswith(".whl") and (subdir is not None or editable):
        msg = f"{label}: a wheel cannot have subdirectory or editable"
        raise ConfigError(msg)
    if kind == "path" and Path(location).is_file() and editable:
        msg = f"{label}: editable requires a project directory"
        raise ConfigError(msg)
    return editable, subdir


def _hashes(
    raw: Mapping[str, Any], kind: str, location: str, label: str
) -> dict[str, str]:
    """Validate a file's declared expected digests."""
    hashes = raw.get("hashes", {})
    if not isinstance(hashes, dict) or ("hashes" in raw and not hashes):
        msg = f"{label}.hashes must be a non-empty table"
        raise ConfigError(msg)
    validated: dict[str, str] = {}
    for algorithm, digest in hashes.items():
        if (
            not re.fullmatch(r"[a-z0-9_]+", algorithm)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-fA-F0-9]+", digest)
        ):
            msg = f"{label}.hashes must map lowercase algorithms to hexadecimal digests"
            raise ConfigError(msg)
        validated[algorithm] = digest.lower()
    if kind == "path" and hashes and Path(location).is_dir():
        msg = f"{label}: directory sources cannot have hashes"
        raise ConfigError(msg)
    return validated


def _entry(name: str, raw: object, directory: Path, label: str) -> DevelopSource:
    if isinstance(raw, str):
        raw = {"path": raw}
    if not isinstance(raw, dict):
        msg = f"{label} must be a string or table"
        raise ConfigError(msg)
    kinds = _KINDS.intersection(raw)
    if len(kinds) != 1:
        msg = f"{label} must contain exactly one source kind"
        raise ConfigError(msg)
    kind = next(iter(kinds))
    unknown = raw.keys() - {kind} - _FIELDS[kind] - _SELECTORS
    if unknown:
        msg = f"{label} has unknown keys: {sorted(unknown)!r}"
        raise ConfigError(msg)
    location = _location(raw[kind], kind, directory, label)
    if "extra" in raw and "group" in raw:
        msg = f"{label} cannot select both extra and group"
        raise ConfigError(msg)
    selectors = {
        key: canonicalize_name(_text(raw[key], f"{label}.{key}"), validate=True)
        for key in ("extra", "group")
        if key in raw
    }
    revision_kind, revision = _revision(raw, label)
    editable, subdir = _path_options(raw, kind, location, label)
    hashes = _hashes(raw, kind, location, label)
    try:
        marker = (
            Marker(_text(raw["marker"], f"{label}.marker")) if "marker" in raw else None
        )
    except InvalidMarker as exc:
        msg = f"{label}.marker: {exc}"
        raise ConfigError(msg) from exc
    return DevelopSource(
        name,
        kind,
        location,
        editable,
        subdir,
        revision,
        revision_kind,
        tuple(sorted(hashes.items())),
        marker,
        selectors.get("extra"),
        selectors.get("group"),
        label,
    )


def _source_document(path: Path) -> Mapping[str, Any]:
    """Read a source table's file with a configuration error on read failure."""
    try:
        return toml_io.load_path(path)
    except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        msg = f"cannot read development sources from {path}: {exc}"
        raise ConfigError(msg) from exc


def _read(
    path: Path, document: Mapping[str, Any] | None = None
) -> dict[str, tuple[DevelopSource, ...]]:
    if document is None:
        document = _source_document(path)
    develop = document.get("develop", {})
    if not isinstance(develop, dict) or develop.keys() - {"sources"}:
        msg = f"{path}: [develop] accepts only sources"
        raise ConfigError(msg)
    sources = develop.get("sources", {})
    if not isinstance(sources, dict):
        msg = f"{path}: [develop.sources] must be a table"
        raise ConfigError(msg)
    result: dict[str, tuple[DevelopSource, ...]] = {}
    for name, entries in sources.items():
        try:
            normalized = canonicalize_name(name, validate=True)
            if normalized in result:
                msg = f"{path}: duplicate normalized source name {name!r}"
                raise ConfigError(msg)
            alternatives = entries if isinstance(entries, list) else [entries]
            if not alternatives:
                msg = f"{path}: source {name!r} cannot be an empty array"
                raise ConfigError(msg)
            parsed = tuple(
                _entry(
                    normalized, raw, path.parent, f"{path}: develop.sources.{name}[{i}]"
                )
                for i, raw in enumerate(alternatives)
            )
        except InvalidName as exc:
            msg = f"{path}: invalid source name or selector: {exc}"
            raise ConfigError(msg) from exc
        result[normalized] = parsed
        for entry in parsed[:-1]:
            if entry.marker is None and entry.extra is None and entry.group is None:
                _logger.warning(
                    "%s: later source alternatives are unreachable", entry.label
                )
                break
    return result


def read_develop_sources(
    path: Path,
    *,
    invocation_root: Path | None = None,
    document: Mapping[str, Any] | None = None,
) -> tuple[DevelopSource, ...]:
    """Read target and eligible invocation-root sources, merging by package name."""
    path = path.parent.resolve() / path.name
    root = Path.cwd() if invocation_root is None else invocation_root.resolve()
    source_root = root / "pyproject.toml"
    merged: dict[str, tuple[DevelopSource, ...]] = {}
    if (
        path.parent != root
        and path.parent.is_relative_to(root)
        and source_root.is_file()
    ):
        merged.update(_read(source_root))
    merged.update(_read(path, document))
    return tuple(entry for alternatives in merged.values() for entry in alternatives)


def expand_credentials(url: str) -> str:
    """Expand environment placeholders in URL credentials only."""
    parts = urlsplit(url)
    credentials, separator, host = parts.netloc.rpartition("@")
    if not separator:
        return url

    def replace(match: re.Match[str]) -> str:
        name = match[1]
        if name not in os.environ:
            msg = f"source credential variable {name!r} is not set"
            raise ConfigError(msg)
        return quote(os.environ[name], safe="")

    credentials = _VARIABLE.sub(replace, credentials)
    return urlunsplit(
        (
            parts.scheme,
            credentials + "@" + host,
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


@dataclass
class SelectedSources:
    """The origins selected by active root occurrences in one resolution."""

    entries: dict[str, DevelopSource]
    defaults: dict[str, DevelopSource]
    protected: frozenset[str]
    markers: frozenset[Marker]

    def validate_lock(self, lock: TargetLock) -> None:
        """Reject an active transitive occurrence that requests another origin."""
        names = {name for children in lock.dependencies.values() for name in children}
        for name in names:
            default = self.defaults.get(name)
            selected = self.entries.get(name)
            if (
                default is not None
                and selected is not None
                and default.identity != selected.identity
            ):
                msg = (
                    f"conflicting sources for {name!r}: "
                    "target selection and transitive occurrence"
                )
                raise ConfigError(msg)


def select_sources(
    entries: Sequence[DevelopSource],
    target: ResolveTarget,
    requirements: Sequence[Requirement],
    contexts: InstallContexts | None,
) -> SelectedSources:
    """Choose ordered alternatives per active target occurrence and check conflicts."""
    environment = target.marker_env
    alternatives: dict[str, list[DevelopSource]] = {}
    for entry in entries:
        alternatives.setdefault(entry.name, []).append(entry)

    def pick(name: str, context: tuple[str, str] | None) -> DevelopSource | None:
        return next(
            (
                entry
                for entry in alternatives.get(name, ())
                if entry.matches(environment, context)
            ),
            None,
        )

    defaults = {
        name: chosen
        for name in alternatives
        if (chosen := pick(name, None)) is not None
    }
    selected: dict[str, DevelopSource] = {}
    active = [
        req
        for req in requirements
        if req.marker is None or dependency_marker_holds(req.marker, environment)
    ]
    protected = frozenset(
        canonicalize_name(req.name) for req in active if req.url is not None
    )
    occurrences: Sequence[tuple[tuple[str, str] | None, Sequence[Requirement]]] = (
        [(None, requirements)]
        if contexts is None
        else [(None, contexts.project), *contexts.selectors.items()]
    )
    for context, declarations in occurrences:
        for req in declarations:
            name = canonicalize_name(req.name)
            if name in protected or (
                req.marker is not None
                and not dependency_marker_holds(req.marker, environment)
            ):
                continue
            chosen = pick(name, context)
            if chosen is None:
                continue
            previous = selected.get(name)
            if previous is not None and previous.identity != chosen.identity:
                msg = (
                    f"conflicting sources for {name!r}: "
                    f"{previous.label} and {chosen.label}"
                )
                raise ConfigError(msg)
            selected[name] = chosen
    selected = {**defaults, **selected}
    for name in protected:
        if name in selected:
            _logger.warning(
                "target direct reference for %s takes precedence over develop.sources",
                name,
            )
            del selected[name]
    return SelectedSources(
        selected,
        defaults,
        protected,
        frozenset(entry.marker for entry in entries if entry.marker is not None),
    )
