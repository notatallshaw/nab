"""Observe dedicated resolution over model-generated index metadata."""

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
from nab_provider.yanked_resolution import ContextFailure, SearchFrame, YankResolver
from nab_provider.yanking import mark_yanked
from nab_resolver.errors import ResolutionError

from .yanking_model import Artifact, Case, Key, Node, Solution, live_completions

if TYPE_CHECKING:
    from collections.abc import Callable

    from nab_provider.yank_candidates import CandidateData

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
        selected: dict[str, Candidate],
        candidate: Candidate,
        artifacts: dict[Key, Artifact],
        inherited: frozenset[str],
    ) -> None:
        self.inherited = inherited
        self.target = node_for(candidate.package)
        self.artifact = artifacts[candidate.base, candidate.label, candidate.withdrawn]
        self.selected = {
            node_for(name): artifacts[item.base, item.label, item.withdrawn]
            for name, item in {**selected, candidate.package: candidate}.items()
        }
        self.available = frozenset(node_for(name) for name in selected)


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
        self.search = YankResolver(
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
        self.original_inspect_choice = self.search.inspect_choice
        self.selected: dict[str, Candidate] = {}
        self.observations: list[Observation] = []
        self.error: ResolutionError | None = None

    def prepare(self, candidate: Candidate) -> CandidateData | None:
        self.observations.append(
            Observation(
                self.selected, candidate, self.artifacts, self.search.scope.live
            )
        )
        return self.original_prepare(candidate)

    def inspect_choice(
        self, frame: SearchFrame, candidate: Candidate
    ) -> SearchFrame | dict[str, Candidate] | ContextFailure:
        """Observe the real branch before it prepares its next candidate."""
        self.selected = frame.selected
        return self.original_inspect_choice(frame, candidate)

    def resolve(self) -> Solution | None:
        """Keep actual file identities and extras rather than comparing only versions."""
        with (
            patch.object(self.search.catalogue, "prepare", self.prepare),
            patch.object(self.search, "inspect_choice", self.inspect_choice),
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
                "live solution still possible",
                observation.selected,
                observation.artifact,
            )
