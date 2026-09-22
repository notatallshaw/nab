"""Observe real proxy resolution over model-generated index metadata."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import patch

from nab_project._testing.coordinator_fake import make_coordinator
from nab_provider._vendor.packaging.requirements import Requirement
from nab_provider.marker_holds import dependency_marker_holds
from nab_provider.policy import ExtrasMode
from nab_provider.provider import Provider
from nab_provider.records import WheelFile
from nab_provider.resolver_inputs import ProxyConstraints, build_resolver_inputs
from nab_provider.tags import PlatformSpec
from nab_provider.target import ResolveTarget
from nab_provider.vcs_admission import VcsConfig
from nab_provider.yank_candidates import Candidate
from nab_provider.yanked_resolution import (
    MetadataProxy,
    PermissionProxy,
    YankProxyProvider,
)
from nab_provider.yanking import mark_yanked
from nab_resolver.errors import ResolutionError
from nab_resolver.types import Incompatibility

from .yanking_model import Artifact, Case, Key, Node, Solution, live_completions

if TYPE_CHECKING:
    from collections.abc import Callable

    from nab_provider.yank_candidates import CandidateData
    from nab_provider.yank_preference import PreferenceScope

TARGETS = {
    "linux": ResolveTarget.for_declared(
        python_version="3.11", spec=PlatformSpec("linux_x86_64")
    ),
    "win32": ResolveTarget.for_declared(
        python_version="3.11", spec=PlatformSpec("windows_amd64")
    ),
}


def node_for(package: str) -> Node:
    name, _, extra = package.partition("[")
    return Node(name, bool(extra))


class Observation:
    """One actual preparation call and the independently knowable state before it."""

    def __init__(
        self,
        search: YankProxyProvider,
        candidate: Candidate,
        artifacts: dict[Key, Artifact],
    ) -> None:
        self.inherited = search.scope.live
        self.target = node_for(candidate.package)
        self.artifact = artifacts[candidate.base, candidate.label, candidate.withdrawn]
        self.selected = {
            node_for(name): artifacts[item.base, item.label, item.withdrawn]
            for name, token in search.decisions.items()
            if isinstance(name, str)
            for item in (search.items[token],)
        }
        self.available = frozenset(
            node_for(name)
            for name, token in search.decisions.items()
            if isinstance(name, str)
            and search.catalogue.facts.get(search.items[token]) is not None
        )


class Harness:
    """Use real providers and inspect calls without replacing their decisions."""

    def __init__(
        self, case: Case, *, unavailable: frozenset[Key] = frozenset()
    ) -> None:
        self.case = case
        self.artifacts = {item.key: item for item in case.artifacts}
        self.by_url = {item.url: item for item in case.artifacts}
        listings: dict[str, list[WheelFile]] = {}
        metadata = {}
        for artifact in case.artifacts:
            file = WheelFile(
                filename=f"{artifact.name}-{artifact.version}-py3-none-any.whl",
                url=artifact.url,
                version=artifact.version,
                has_metadata=True,
                requires_python=None,
                upload_time=None,
            )
            if artifact.withdrawn:
                file = mark_yanked(file, reason="withdrawn")
            listings.setdefault(artifact.name, []).append(file)
            metadata[file.metadata_url] = (
                f"Metadata-Version: 2.2\nName: {artifact.name}\nVersion: {artifact.version}\nProvides-Extra: x\n"
                + "".join(
                    f"Requires-Dist: {req.text}\n" for req in artifact.dependencies
                )
                + "\n"
            )
        listings.setdefault("missing", [])
        self.port = make_coordinator(listings=listings, metadata_by_url=metadata)
        for key in unavailable:
            artifact = self.artifacts[key]
            url = artifact.url + ".metadata"
            self.port.index.record_offline_metadata_miss(
                artifact.name, artifact.version, url
            )
            self.port.index.store_metadata(
                artifact.name, artifact.version, None, metadata_url=url
            )
        target = TARGETS[case.platform]
        roots = [Requirement(r.text) for r in case.roots]
        constraints = [Requirement(r.text) for r in case.constraints]
        prepared = build_resolver_inputs(
            roots,
            VcsConfig(),
            environment=target.marker_env,
            marker_holds=dependency_marker_holds,
        )
        bounded = build_resolver_inputs(
            constraints,
            VcsConfig(),
            environment=target.marker_env,
            marker_holds=dependency_marker_holds,
            kind="constraint",
        )
        constraint_ranges = ProxyConstraints(bounded.ranges)
        active = {root.origin for root in (*prepared.roots, *bounded.roots)}
        declarations = [r for r in (*roots, *constraints) if str(r) in active]
        factory = partial(
            Provider,
            self.port,
            target=target,
            root_requirements=prepared.ranges,
            constraints=constraint_ranges,
            extras_mode=ExtrasMode.BACKTRACK,
            defer_yanked=True,
        )
        self.search = YankProxyProvider(
            factory(),
            factory,
            prepared.roots,
            declarations,
            constraint_ranges,
            preferences={},
        )
        self.original_prepare: Callable[[Candidate], CandidateData | None] = (
            self.search.catalogue.prepare
        )
        self.original_clauses = YankProxyProvider.consume_pending_clauses
        self.original_new_query = self.search.new_query
        self.current_query = self.search
        self.observations: list[Observation] = []
        self.clauses: list[tuple[PreferenceScope, Incompatibility]] = []
        self.error: ResolutionError | None = None

    def prepare(self, candidate: Candidate) -> CandidateData | None:
        self.observations.append(
            Observation(self.current_query, candidate, self.artifacts)
        )
        return self.original_prepare(candidate)

    def new_query(self, scope: PreferenceScope) -> YankProxyProvider:
        """Observe the actual query that owns each preparation and conflict."""
        query = self.original_new_query(scope)
        self.current_query = query
        return query

    def consume_clauses(self, query: YankProxyProvider) -> list[Incompatibility]:
        clauses = self.original_clauses(query)
        self.clauses.extend((query.scope, clause) for clause in clauses)
        return clauses

    def resolve(self) -> Solution | None:
        """Keep actual file identities and extras rather than comparing only versions."""

        def observe_clauses(query: YankProxyProvider) -> list[Incompatibility]:
            return self.consume_clauses(query)

        with (
            patch.object(self.search.catalogue, "prepare", self.prepare),
            patch.object(self.search, "new_query", self.new_query),
            patch.object(YankProxyProvider, "consume_pending_clauses", observe_clauses),
        ):
            try:
                pins, provider = self.search.resolve()
            except ResolutionError as exc:
                self.error = exc
                return None
        selected = set()
        for name, version in pins.items():
            if "[" in name:
                continue
            files = provider.dist_files_for(name, version)
            assert len(files) == 1, (name, files)
            selected.add(self.by_url[files[0].url].key)
        return Solution(frozenset(selected), frozenset(node_for(name) for name in pins))

    def check_preparations(self, valid: frozenset[Solution]) -> None:
        """Validate permission and live alternatives using only the graph model."""
        for observation in self.observations:
            selection = {
                node.name: artifact for node, artifact in observation.selected.items()
            }
            permitted = self.case.reachable(
                selection, frozenset(observation.selected), observation.available
            )
            assert observation.target in permitted, observation
            if not observation.artifact.withdrawn:
                continue
            assert not live_completions(
                self.case,
                valid,
                observation.selected,
                observation.available,
                observation.artifact.name,
                observation.inherited,
            ), (
                "scoped live solution still possible",
                observation.selected,
                observation.artifact,
            )

    def check_clauses(self, valid: frozenset[Solution]) -> None:
        """A conditional incompatibility must preserve valid graphs in its scope."""
        for scope, clause in self.clauses:
            assumed = {
                node_for(candidate.package): self.artifacts[
                    candidate.base, candidate.label, candidate.withdrawn
                ]
                for candidate in scope.fixed
            }
            scoped = frozenset(
                solution
                for solution in valid
                if all(
                    node not in solution.nodes or artifact.key in solution.files
                    for node, artifact in assumed.items()
                )
                and not any(
                    name in scope.live and withdrawn
                    for name, _, withdrawn in solution.files
                )
            )
            fixed: dict[Node, Artifact] = {}
            contradictory = False
            for term in clause.terms:
                assert term.is_positive()
                package = term.package
                if isinstance(package, (MetadataProxy, PermissionProxy)):
                    candidates = (self.search.items[package.candidate],)
                else:
                    candidates = tuple(
                        item
                        for token, item in self.search.items.items()
                        if item.package == package and token in term.constraint
                    )
                assert len(candidates) == 1
                candidate = candidates[0]
                node = node_for(candidate.package)
                artifact = self.artifacts[
                    candidate.base, candidate.label, candidate.withdrawn
                ]
                if node in fixed and fixed[node] != artifact:
                    contradictory = True
                fixed[node] = artifact
            assert contradictory or not any(
                solution.extends(fixed) for solution in scoped
            ), (scope, clause, fixed, scoped)
