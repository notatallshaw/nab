"""Apply development sources within one resolver context."""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode, urlsplit

from nab_provider._vendor.packaging.ranges import VersionRange
from nab_provider._vendor.packaging.utils import canonicalize_name
from nab_provider.archive import ArchiveRequest, ArchiveRequestError, is_archive_url
from nab_provider.errors import ConfigError
from nab_provider.marker_holds import dependency_marker_holds
from nab_provider.overrides import PackageOverride
from nab_provider.pep508 import parse_requirement
from nab_provider.policy import ArchiveSource, LocalSource, VcsSource
from nab_provider.provider import ListingFilterCache
from nab_provider.records import ACCEPTED_HASH_ALGORITHMS, IndexConfig
from nab_provider.vcs_admission import VcsConfig, admit_vcs_url, split_vcs_scheme

from .develop import DevelopSource, expand_credentials, select_sources
from .lockfile import ArtifactMemo

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from nab_provider._vendor.packaging.requirements import Requirement
    from nab_provider.target import ResolveTarget

    from ._resolve.engine import EngineSettings, InstallContexts
    from .fetch import FetchCoordinator
    from .inputs import ResolveInputs
    from .lockfile import TargetLock

_logger = logging.getLogger(__name__)
_NO_BUILD_ROOTS = frozenset[str]()


def _native_names(inputs: ResolveInputs) -> set[str]:
    """Find explicit native routes that take precedence over standard sources."""
    sources: tuple[LocalSource | VcsSource | ArchiveSource, ...] = (
        *(
            source
            for source in inputs.local_sources
            if canonicalize_name(source.name) not in inputs.workspace_member_names
        ),
        *inputs.vcs_sources,
        *inputs.archive_sources,
    )
    return {canonicalize_name(source.name) for source in sources} | {
        override.name
        for override in inputs.package_overrides
        if override.index is not None
    }


def _index_source(entry: DevelopSource, indexes: list[IndexConfig]) -> IndexConfig:
    """Reuse a configured index or add an index for this source's route only."""
    url = expand_credentials(entry.location)
    index = next(
        (index for index in indexes if index.url.rstrip("/") == url.rstrip("/")), None
    )
    if index is None:
        label = "develop-" + hashlib.sha256(url.encode()).hexdigest()
        if any(index.name == label for index in indexes):
            msg = (
                f"{entry.label}: index name {label!r} "
                "is already configured for another URL"
            )
            raise ConfigError(msg)
        index = IndexConfig(label, url)
        indexes.append(index)
    return index


def _git_source(entry: DevelopSource) -> VcsSource:
    """Keep tag and branch selectors in their distinct Git ref namespaces."""
    revision = entry.revision
    if entry.revision_kind in {"tag", "branch"}:
        prefix = "tags" if entry.revision_kind == "tag" else "heads"
        revision = f"refs/{prefix}/{revision}"
    url = "git+" + expand_credentials(entry.location)
    if revision is not None:
        url += "@" + revision
    if entry.subdirectory is not None:
        url += "#" + urlencode({"subdirectory": entry.subdirectory}, quote_via=quote)
    return VcsSource(entry.name, url)


def _archive_source(entry: DevelopSource) -> ArchiveSource:
    """Translate file declarations without inventing an expected digest."""
    url = (
        expand_credentials(entry.location)
        if entry.kind == "url"
        else Path(entry.location).as_uri()
    )
    fragment = {
        algorithm: digest
        for algorithm, digest in entry.hashes
        if algorithm in ACCEPTED_HASH_ALGORITHMS
    }
    if entry.hashes and not fragment:
        msg = f"{entry.label}: no supported hash algorithm"
        raise ConfigError(msg)
    parsed = ArchiveRequest.parse(url)
    for algorithm, digest in parsed.hashes:
        if algorithm in fragment and fragment[algorithm] != digest:
            msg = f"{entry.label}: conflicting {algorithm} hashes"
            raise ConfigError(msg)
        if not digest:
            msg = f"{entry.label}: {algorithm} hash cannot be empty"
            raise ConfigError(msg)
        fragment[algorithm] = digest
    if (
        entry.subdirectory is not None
        and parsed.subdirectory
        and entry.subdirectory != parsed.subdirectory
    ):
        msg = f"{entry.label}: conflicting subdirectories"
        raise ConfigError(msg)
    subdirectory = entry.subdirectory or parsed.subdirectory
    if subdirectory:
        if parsed.filename.endswith(".whl"):
            msg = f"{entry.label}: a wheel cannot have a subdirectory"
            raise ConfigError(msg)
        fragment["subdirectory"] = subdirectory
    return ArchiveSource(
        entry.name,
        parsed.url + "#" + urlencode(fragment, quote_via=quote),
    )


def _project_sources(
    inputs: ResolveInputs, entries: dict[str, DevelopSource]
) -> ResolveInputs:
    """Translate selected origins into nab's existing materializer and route inputs."""
    local = [
        source
        for source in inputs.local_sources
        if canonicalize_name(source.name) not in entries
    ]
    vcs = list(inputs.vcs_sources)
    archives = list(inputs.archive_sources)
    indexes = list(inputs.indexes)
    overrides = list(inputs.package_overrides)
    for name, entry in entries.items():
        if entry.kind == "path" and not Path(entry.location).is_file():
            local.append(
                LocalSource(
                    name,
                    entry.location,
                    editable=entry.editable,
                    subdirectory=entry.subdirectory,
                )
            )
        elif entry.kind == "index":
            index = _index_source(entry, indexes)
            overrides.append(
                PackageOverride(
                    requirement=parse_requirement(name),
                    name=name,
                    version_range=VersionRange.full(),
                    index=index.name,
                    source_label=entry.label,
                )
            )
        elif entry.kind == "git":
            vcs.append(_git_source(entry))
        elif entry.kind in {"url", "path"}:
            archives.append(_archive_source(entry))
        else:
            msg = f"{entry.label}: {entry.kind} sources are not implemented"
            raise NotImplementedError(msg)
    return inputs.replace(
        local_sources=tuple(local),
        vcs_sources=tuple(vcs),
        archive_sources=tuple(archives),
        indexes=tuple(indexes),
        package_overrides=tuple(overrides),
        develop_sources=(),
    )


def _declared_source_urls(
    inputs: ResolveInputs, entries: dict[str, DevelopSource], references: dict[str, str]
) -> frozenset[tuple[str, str]]:
    """Bind project permission to each selected package and exact source URL."""
    sources: tuple[VcsSource | ArchiveSource, ...] = (
        *inputs.vcs_sources,
        *inputs.archive_sources,
    )
    permitted: set[tuple[str, str]] = set()
    for source in sources:
        name = canonicalize_name(source.name)
        if name in entries or references.get(name) == source.url:
            permitted.add((name, source.url))
    return frozenset(permitted)


def _build_roots(
    inputs: ResolveInputs, target: ResolveTarget, contexts: InstallContexts | None
) -> frozenset[str]:
    """Find active build-group roots that cannot take development redirects."""
    if inputs.build_group is None or contexts is None:
        return _NO_BUILD_ROOTS
    requirements = contexts.selectors.get(
        ("group", canonicalize_name(inputs.build_group)), ()
    )
    return frozenset(
        canonicalize_name(req.name)
        for req in requirements
        if req.marker is None or dependency_marker_holds(req.marker, target.marker_env)
    )


def _validate_direct_source(
    name: str, url: str, vcs: VcsConfig, *, declared: bool
) -> None:
    """Validate archive syntax or apply the VCS allowlists for a root URL."""
    if not is_archive_url(url):
        admit_vcs_url(url, vcs, declared=declared)
        return
    try:
        parsed = ArchiveRequest.parse(url)
        urlsplit(url)
    except (ArchiveRequestError, ValueError) as exc:
        msg = f"direct source for {name!r}: {exc}"
        raise ConfigError(msg) from exc
    if parsed.hashes and not parsed.has_usable_hash:
        msg = f"direct source for {name!r} has an empty hash"
        raise ConfigError(msg)


def _direct_sources(
    requirements: Sequence[Requirement],
    target: ResolveTarget,
    settings: EngineSettings,
    *,
    declared: bool,
) -> dict[str, str]:
    """Collect active root URLs, refusing conflicting references by name."""
    references: dict[str, str] = {}
    for req in requirements:
        url = req.url
        if url is None or (
            not is_archive_url(url) and split_vcs_scheme(url)[0] is None
        ):
            continue
        if req.marker is not None and not settings.marker_holds(
            req.marker, target.marker_env
        ):
            continue
        name = canonicalize_name(req.name)
        _validate_direct_source(name, url, settings.inputs.vcs, declared=declared)
        previous = references.get(name)
        if previous is not None and previous != url:
            msg = f"conflicting direct sources for {name!r}"
            raise ConfigError(msg)
        references[name] = url
    return references


def _direct_source_inputs(
    inputs: ResolveInputs, references: dict[str, str]
) -> ResolveInputs:
    """Apply explicit root references ahead of configured sources for the same name."""
    return inputs.replace(
        local_sources=tuple(
            source
            for source in inputs.local_sources
            if canonicalize_name(source.name) not in references
        ),
        vcs_sources=(
            *(
                source
                for source in inputs.vcs_sources
                if canonicalize_name(source.name) not in references
            ),
            *(
                VcsSource(name, url)
                for name, url in references.items()
                if not is_archive_url(url)
            ),
        ),
        archive_sources=(
            *(
                source
                for source in inputs.archive_sources
                if canonicalize_name(source.name) not in references
            ),
            *(
                ArchiveSource(name, url)
                for name, url in references.items()
                if is_archive_url(url)
            ),
        ),
    )


def _check_build_sources(
    lock: TargetLock, roots: frozenset[str], sources: dict[str, DevelopSource]
) -> None:
    """Refuse a combined lock that redirects a build requirement or its dependencies."""
    pending = list(roots)
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in sources:
            msg = (
                f"development source for build requirement {name!r} cannot be used "
                "in a combined build-group lock; "
                "use a separate --build-requirements lock"
            )
            raise ConfigError(msg)
        pending.extend(lock.dependencies.get(name, ()))


@dataclass(slots=True)
class _SourceScope:
    """Retain a selected origin's inputs and fetch state for one operation."""

    inputs: ResolveInputs
    coordinator: FetchCoordinator


class SourceContexts:
    """Share fetching only between contexts with identical selected origins."""

    def __init__(
        self, cache_dir: Path | None, *, allow_declarations: bool = True
    ) -> None:
        """Retain source state for one resolve operation."""
        self._cache_dir = cache_dir
        self._allow_declarations = allow_declarations
        self._scopes: dict[
            tuple[tuple[str, tuple[object, ...]], ...], _SourceScope
        ] = {}
        self._eligible: dict[str, str] = {}
        self._encountered: set[str] = set()

    def warn_unmatched_sources(self) -> None:
        """Report sources unused across every successful target and selection."""
        for name in sorted(self._eligible.keys() - self._encountered):
            _logger.warning(
                "%s: source for %s matches no resolved dependency",
                self._eligible[name],
                name,
            )

    @contextmanager
    def scope(
        self,
        target: ResolveTarget,
        requirements: Sequence[Requirement],
        contexts: InstallContexts | None,
        settings: EngineSettings,
    ) -> Generator[EngineSettings, None, None]:
        """Isolate listings, materializations and artifacts for one source selection."""
        references = _direct_sources(
            requirements, target, settings, declared=self._allow_declarations
        )
        native_names = _native_names(settings.inputs) - references.keys()
        for name in native_names.intersection(
            entry.name for entry in settings.inputs.develop_sources
        ):
            _logger.warning(
                "native source for %s takes precedence over develop.sources", name
            )
        entries = [
            entry
            for entry in settings.inputs.develop_sources
            if entry.name not in native_names
        ]
        selected = select_sources(entries, target, requirements, contexts)
        active_contexts: tuple[tuple[str, str] | None, ...] = (
            (None,) if contexts is None else (None, *contexts.selectors)
        )
        for entry in entries:
            if any(
                entry.matches(target.marker_env, context) for context in active_contexts
            ):
                self._eligible.setdefault(entry.name, entry.label)
        build_roots = _build_roots(settings.inputs, target, contexts)
        if build_roots.intersection(selected.entries):
            msg = (
                "development sources overlap build-group roots; "
                "use a separate --build-requirements lock"
            )
            raise ConfigError(msg)
        inputs = _project_sources(settings.inputs, selected.entries)
        if references:
            inputs = _direct_source_inputs(inputs, references)
        declared_sources = (
            _declared_source_urls(inputs, selected.entries, references)
            if self._allow_declarations
            else frozenset()
        )

        def validate(lock: TargetLock) -> None:
            selected.validate_lock(lock)
            _check_build_sources(lock, build_roots, selected.entries)
            self._encountered.update(lock.pins)

        # Keep development redirects out of backend environments.
        build_inputs = settings.inputs.replace(develop_sources=())
        origins: list[tuple[str, tuple[object, ...]]] = [
            (name, entry.identity) for name, entry in selected.entries.items()
        ]
        origins.extend((name, ("direct-url", url)) for name, url in references.items())
        key = tuple(sorted(origins))
        scope = self._scopes.get(key)
        if scope is None:
            coordinator = settings.coordinator.scoped_sources(
                inputs, build_inputs, cache_dir=self._cache_dir
            )
            scope = _SourceScope(inputs, coordinator)
            self._scopes[key] = scope
        with scope.coordinator:
            yield replace(
                settings,
                inputs=scope.inputs,
                coordinator=scope.coordinator,
                source_context=None,
                source_markers=selected.markers,
                redirected_dependencies=frozenset(selected.entries),
                resolved_direct_urls=frozenset(references.items()),
                declared_sources=declared_sources,
                listing_filter_cache=ListingFilterCache(),
                artifacts=ArtifactMemo(),
                source_validator=validate,
            )
