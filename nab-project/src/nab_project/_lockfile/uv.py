"""Render the project graph consumed by uv sync and uv run."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlencode, urlsplit

from packaging.utils import canonicalize_name

from nab_index.file_urls import is_file_url, parse_file_url
from nab_markersets.markersets import MarkerSet
from nab_provider.marker_holds import intractable_as_error
from nab_provider.requirements_file import (
    expand_extra_requirements,
    iter_group_requirements,
)

from ..conflicts import ConflictPolicy
from ..lockfile import drop_workspace_pins
from ..paths import relative_lock_path
from ..pyproject_files import (
    build_system_requires,
    dependency_groups,
    project_dependencies,
    project_name,
    project_optional_dependencies,
)
from .native import file_digest
from .native_saved import uv_project_identity, write_native_document
from .pylock import LockValidationError, project_packages

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from packaging.pylock import Package, PackageArchive, PackageSdist, PackageWheel
    from packaging.requirements import Requirement

    from ..lockfile import LockInput, TargetLock
    from .pylock import ProjectedPackage


def _artifact(
    file: PackageWheel | PackageSdist, directory: Path, registry_path: Path | None
) -> dict[str, Any]:
    """Preserve the artifact location relative to its serving index."""
    data: dict[str, str | int | None]
    if file.path is None:
        data = {"url": file.url}
    elif registry_path is not None:
        data = {"path": relative_lock_path(directory / file.path, registry_path)}
    else:
        data = {"url": (directory / file.path).resolve().as_uri()}

    data["hash"] = file_digest(file.filename, file.hashes)
    if file.size is not None:
        data["size"] = file.size
    return data


def _distribution(package: Package, directory: Path) -> dict[str, Any]:
    """Encode the resolved source and the files available from that source."""
    if package.directory:
        local = package.directory
        path = Path(local.path)
        if local.subdirectory is not None:
            path /= local.subdirectory
        return {
            "source": {
                "editable" if local.editable is True else "directory": path.as_posix()
            }
        }

    if package.vcs:
        vcs = package.vcs
        if vcs.type != "git":
            msg = (
                f"uv cannot install {package.name} from {vcs.type}. "
                "Use --format pdm or a Git source."
            )
            raise LockValidationError(msg)

        # uv stores the requested revision in the query and the commit in the fragment.
        parameters = {}
        if vcs.requested_revision is not None:
            parameters["rev"] = vcs.requested_revision
        if vcs.subdirectory is not None:
            parameters["subdirectory"] = vcs.subdirectory

        parsed = urlsplit(vcs.url or "")
        query = "&".join(filter(None, (parsed.query, urlencode(parameters))))
        return {
            "source": {
                "git": parsed._replace(query=query, fragment=vcs.commit_id).geturl()
            }
        }

    if package.archive:
        return _archive(package.archive, package.name, directory)

    return _registry(package, directory)


def _archive(archive: PackageArchive, name: str, directory: Path) -> dict[str, Any]:
    """Uv locates path archives at their source, and registry files at their index."""
    url = archive.url or ""
    filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    artifact = {"hash": file_digest(name, archive.hashes)}
    if is_file_url(url):
        if archive.subdirectory is not None and Path(archive.subdirectory).parts:
            msg = (
                f"uv builds {name} from the archive root for a path source and cannot "
                f"select local archive subdirectory {archive.subdirectory!r}. "
                "Serve the archive over HTTPS or use --format pdm."
            )
            raise LockValidationError(msg)

        source = {"path": relative_lock_path(parse_file_url(url), directory)}
        artifact["filename"] = filename
    else:
        source = {"url": url}
        artifact["url"] = url
        if archive.subdirectory is not None:
            source["subdirectory"] = archive.subdirectory

    if filename.endswith(".whl"):
        return {"source": source, "wheels": [artifact]}

    return {"source": source, "sdist": artifact}


def _registry(package: Package, directory: Path) -> dict[str, Any]:
    """Store wheel and sdist paths relative to the index that serves them."""
    index = package.index or ""
    registry_path = parse_file_url(index) if is_file_url(index) else None
    registry = (
        relative_lock_path(registry_path, directory)
        if registry_path is not None
        else index.rstrip("/")
    )

    data: dict[str, Any] = {"source": {"registry": registry}}
    if package.sdist:
        data["sdist"] = _artifact(package.sdist, directory, registry_path)
    if package.wheels is not None:
        data["wheels"] = [
            _artifact(wheel, directory, registry_path) for wheel in package.wheels
        ]
    return data


def _requirement(req: Requirement) -> dict[str, Any]:
    """Record the root input uv compares with the manifest during a lock check."""
    data: dict[str, Any] = {"name": canonicalize_name(req.name)}
    if req.specifier:
        data["specifier"] = str(req.specifier)
    if req.url is not None:
        if is_file_url(req.url):
            data["path"] = parse_file_url(req.url).as_posix()
        else:
            data["url"] = req.url

    if req.extras:
        data["extras"] = sorted(req.extras)
    if req.marker:
        data["marker"] = str(req.marker)
    return data


def _requests(
    target: TargetLock, parent: str
) -> Iterator[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """Yield child names, requested child extras and activating parent extras."""
    by_parent = target.dependency_requirements
    declarations = by_parent.get(parent) if by_parent is not None else None

    for child in target.dependencies.get(parent, ()):
        records = declarations.get(child, ()) if declarations is not None else ()
        if not records:
            yield child, (), ()
        for record in records:
            yield child, record.dependency_extras, record.required_for_parent_extras


class _PackageGraph:
    """Index each resolved row by target, retaining distinct sources of one name."""

    def __init__(
        self, lock: LockInput, directory: Path, project: str, excluded: frozenset[str]
    ) -> None:
        self.excluded = excluded
        self.regions: dict[frozenset[str], str | None] = {}
        self.lock = lock
        self.project = project

        self.packages = project_packages(lock, directory)
        self.rows = [
            {
                "name": p.name,
                "version": str(p.version),
                **_distribution(p.source, directory),
            }
            for p in self.packages
        ]

        # A name can resolve to a different source or version on each target.
        self.by_target: dict[tuple[str, str], int] = {}
        self.by_name: dict[str, list[int]] = defaultdict(list)
        for i, package in enumerate(self.packages):
            self.by_name[package.name].append(i)
            for label in package.labels:
                self.by_target[label, package.name] = i

    def reference(
        self, index: int, extras: tuple[str, ...], marker: str | None
    ) -> dict[str, Any]:
        """Qualify references only when the name identifies more than one row."""
        row = self.rows[index]
        ref = {"name": row["name"]}
        if len(self.by_name[row["name"]]) > 1:
            ref.update(version=row["version"], source=row["source"])

        if extras:
            ref["extra"] = list(extras)
        if marker is not None:
            ref["marker"] = marker
        return ref

    def target_marker(self, label: str) -> str:
        """Include the uv conflict member when two forks share a platform."""
        target = self.lock.targets[label].target
        declarations = self.lock.target_environments
        if declarations is not None and label in declarations:
            text = declarations[label]
        else:
            text = target.environment_marker_string

        # uv encodes a selected conflict member in the extra marker variable.
        for kind, member in target.selection:
            token = f"{kind}-{len(self.project)}-{self.project}-{member}"
            text = f'({text}) and extra == "{token}"'
        return text

    def environment(self, labels: set[str], all_labels: set[str]) -> str | None:
        """Choose the resolved environments in which an edge is traversable."""
        if labels == all_labels:
            return None

        key = frozenset(labels)
        if key in self.regions:
            return self.regions[key]

        regions = MarkerSet.empty()
        for label in sorted(labels):
            regions |= MarkerSet.from_marker(self.target_marker(label))

        marker = regions.to_marker_string()
        self.regions[key] = marker
        return marker

    def endpoints(
        self, label: str, child: str, extras: tuple[str, ...]
    ) -> Iterator[tuple[int, tuple[str, ...]]]:
        """Follow omitted members until an edge reaches an installed distribution."""
        if child not in self.excluded:
            yield self.by_target[label, child], extras
            return

        # A workspace cycle can revisit a member with a different requested extra.
        pending = [(child, extras)]
        seen: set[tuple[str, tuple[str, ...]]] = set()
        target = self.lock.targets[label]
        while pending:
            name, requested = pending.pop()
            if (name, requested) in seen:
                continue

            seen.add((name, requested))

            if name not in self.excluded:
                yield self.by_target[label, name], requested
                continue

            # Only forward edges enabled by the incoming request.
            for dependency, dep_extras, parent_extras in _requests(target, name):
                if not parent_extras or not set(parent_extras).isdisjoint(requested):
                    pending.append((dependency, dep_extras))

    def attach_edges(self, parent: ProjectedPackage, row: dict[str, Any]) -> None:
        """Collect the dependency edges active in each target and parent extra."""
        by_extra: dict[str, dict[tuple[int, tuple[str, ...]], set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        # Group identical edges before constructing their environment marker.
        for label in parent.labels:
            for name, requested, parent_extras in _requests(
                self.lock.targets[label], parent.name
            ):
                for key in self.endpoints(label, name, requested):
                    for extra in parent_extras or ("",):
                        by_extra[extra][key].add(label)

        all_labels = set(parent.labels)
        for extra, edges in sorted(by_extra.items()):
            refs = [
                self.reference(i, requested, self.environment(labels, all_labels))
                for (i, requested), labels in sorted(edges.items())
            ]
            if extra:
                row.setdefault("optional-dependencies", {})[extra] = refs
            else:
                row["dependencies"] = refs

    def _root_choices(
        self, requirement: Requirement
    ) -> dict[tuple[int, tuple[str, ...]], set[str]]:
        """Find the installed endpoints of a project requirement in each target."""
        choices: dict[tuple[int, tuple[str, ...]], set[str]] = defaultdict(set)
        requested = tuple(sorted(requirement.extras))
        for index in self.by_name.get(canonicalize_name(requirement.name), ()):
            package = self.packages[index]
            if not requirement.specifier.contains(package.version, prereleases=True):
                continue

            if package.name not in self.excluded:
                choices[index, requested].update(package.labels)
                continue

            for label in package.labels:
                for key in self.endpoints(label, package.name, requested):
                    choices[key].add(label)
        return choices

    def root_edges(self, requirements: Sequence[Requirement]) -> list[dict[str, Any]]:
        edges: dict[tuple[int, tuple[str, ...], str | None], dict[str, Any]] = {}
        all_labels = set(self.lock.targets)

        for requirement in requirements:
            for (index, requested), labels in self._root_choices(requirement).items():
                guard = self.environment(labels, all_labels)
                if requirement.marker:
                    marker = MarkerSet.from_marker(requirement.marker)
                    if guard is not None:
                        marker &= MarkerSet.from_marker(guard)
                    if marker.is_empty():
                        continue

                    guard = marker.to_marker_string()

                key = (index, requested, guard)
                if key not in edges:
                    edges[key] = self.reference(*key)
        return list(edges.values())


def _group_inputs(
    lock: LockInput, document: Mapping[str, Any], main: Sequence[Requirement]
) -> dict[str, list[Requirement]]:
    """Collect named groups, including configured runtime and build roots."""
    groups = dependency_groups(document)
    declared = {canonicalize_name(name) for name in groups}
    selected = tuple(
        name for name in lock.active_groups if canonicalize_name(name) in declared
    )

    result = dict(iter_group_requirements(groups, selected))
    if lock.base_group is not None:
        result[lock.base_group] = list(main)
    if lock.build_group is not None:
        result[lock.build_group] = build_system_requires(
            document, Path("pyproject.toml")
        )
    return result


def _root_source(document: Mapping[str, Any]) -> dict[str, str]:
    """Honor uv's package setting before inferring it from the build system."""
    tool = document.get("tool", {})
    settings = tool.get("uv", {}) if isinstance(tool, dict) else None
    if not isinstance(settings, dict):
        msg = "Declare uv settings in a [tool.uv] table."
        raise LockValidationError(msg)

    package = settings.get("package", "build-system" in document)
    if not isinstance(package, bool):
        msg = "Set tool.uv.package to true to install the project or false to skip it."
        raise LockValidationError(msg)

    return {"editable" if package else "virtual": "."}


def _project_root(
    graph: _PackageGraph, document: Mapping[str, Any], main: Sequence[Requirement]
) -> dict[str, Any]:
    """Record runtime, optional and development requirements on the root project."""
    root: dict[str, Any] = {
        "name": graph.project,
        "version": document.get("project", {}).get("version", "0"),
        "source": _root_source(document),
    }
    if graph.lock.base_group is None and (refs := graph.root_edges(main)):
        root["dependencies"] = refs

    # uv uses declared requirements to check freshness and resolved edges to install.
    metadata: dict[str, Any] = {}
    declarations = [_requirement(req) for req in main]
    optional = project_optional_dependencies(document)
    for extra in graph.lock.extras:
        reqs = expand_extra_requirements(
            optional, graph.project, [extra], bind_extra_markers=True
        )
        root.setdefault("optional-dependencies", {})[extra] = graph.root_edges(reqs)
        for req in reqs:
            value = _requirement(req)
            marker = MarkerSet.from_marker(f'extra == "{extra}"')
            if req.marker:
                marker &= MarkerSet.from_marker(req.marker)
            declarations.append({**value, "marker": marker.to_marker_string()})

    if declarations:
        metadata["requires-dist"] = declarations
    if graph.lock.extras:
        metadata["provides-extras"] = list(graph.lock.extras)

    for group, reqs in _group_inputs(graph.lock, document, main).items():
        root.setdefault("dev-dependencies", {})[group] = graph.root_edges(reqs)
        metadata.setdefault("requires-dev", {})[group] = [
            _requirement(req) for req in reqs
        ]

    if metadata:
        root["metadata"] = metadata
    return root


def write_uv_lock(
    lock_input: LockInput,
    document: Mapping[str, Any],
    *,
    project_dir: Path,
    output_path: Path | None = None,
    reproduce: bool = False,
    script: bool = False,
    build_requirements: bool = False,
    excluded_workspace: frozenset[str] = frozenset(),
) -> str:
    """Write a uv project or script lock with dependency edges and artifact hashes."""
    name = canonicalize_name(project_name(document) or project_dir.resolve().name)
    directory = output_path.parent if output_path else project_dir
    graph = _PackageGraph(lock_input, directory, name, excluded_workspace)

    project = document.get("project", {})
    requires_python = lock_input.requires_python or project.get("requires-python") or ""
    result: dict[str, Any] = {
        "version": 1,
        "revision": 5,
        "requires-python": requires_python,
    }
    if lock_input.environments:
        result["supported-markers"] = [
            str(marker) for marker in lock_input.environments
        ]

    # uv can exclude a pair of extras, but a minimum selection permits both.
    conflicts = [
        [
            {"package": name, member.kind.value: member.name}
            for member in conflict.members
        ]
        for conflict in lock_input.conflicts
        if conflict.policy is not ConflictPolicy.AT_LEAST_ONE
    ]
    if conflicts:
        result["conflicts"] = conflicts

    with intractable_as_error():
        retained: list[dict[str, Any]] = []
        for package, row in zip(graph.packages, graph.rows, strict=True):
            if package.name in excluded_workspace:
                continue

            retained.append(row)
            graph.attach_edges(package, row)
            if len(graph.by_name[package.name]) > 1:
                row["resolution-markers"] = sorted(
                    {graph.target_marker(label) for label in package.labels}
                )

        # A script has manifest requirements instead of a root distribution.
        main = (
            build_system_requires(document, project_dir / "pyproject.toml")
            if build_requirements
            else project_dependencies(document)
        )
        if script:
            result["manifest"] = {"requirements": [_requirement(req) for req in main]}
        else:
            retained.append(_project_root(graph, document, main))

    result["package"] = sorted(retained, key=lambda row: (row["name"], row["version"]))
    return write_native_document(
        result,
        drop_workspace_pins(lock_input, excluded_workspace),
        format_name="uv",
        project_identity=uv_project_identity(document),
        output_path=output_path,
        reproduce=reproduce,
    )
